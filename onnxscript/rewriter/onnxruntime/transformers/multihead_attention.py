r"""POC experimenting function aware pattern re-write.

In this case we don't want to spell-out the entire source pattern.
Instead, we want to replace an entire function call a new subgraph.

Source function: LlamaAttention
inputs (positional args, the names in function definition are unfortunately arbitrary and don't provide value):
    - hidden_states
    - position_id
    - attention_mask
    - q_proj.weight
    - k_proj.weight
    - v_proj.weight
    - cos_cached
    - sin_cached
    - o_proj.weight
outputs (similarly, positional)
    - present_value
    - present_key
    - attn_output (o_proj)

The rewriting algorithm is as follows:

The final new function graph should look like this:

    function_proj_q                     function_proj_k
            |                                   |
            |                                   |
com.microsoft::RotaryEmbedding     com.microsoft::RotaryEmbedding        function_proj_v
            \                                   /                              /
             \                                 /                              /
              \                               /                              /
               \---------------              /       -----------------------/
                        com.microsoft::MultiHeadAttention
                            |               |           |
                        attn_output   (present_key) (present_value)
                            |
                     function_proj_o
                            |
                        (output)

So all we need, is to locate 'function_proj_q', 'function_proj_k', 'function_proj_v', 'function_proj_o'.
Construct the 4 nodes with new contrib op nodes, and properly name their inputs/outputs.

"""

from __future__ import annotations

import abc
import dataclasses
import logging

import onnx
from onnx import helper as onnx_helper

import onnxscript
from onnxscript.rewriter import function_rule

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class AttnSizeConfig:
    num_attention_heads: int
    num_key_value_heads: int
    head_size: int
    hidden_size: int


class AttentionRewriteRule(function_rule.FunctionRewriteRule, abc.ABC):
    def infer_attn_size_config(self, function: onnx.FunctionProto) -> AttnSizeConfig:
        if len(function.output) != 3:
            raise function_rule.FunctionRewriteError(
                f"Unexpected number of outputs. Expected 3, got {len(function.output)}."
            )
        present_value, _, attn_output = function.output
        if (
            present_value_ir := self.lookup(function, present_value)
        ) is None or present_value_ir.shape is None:
            raise function_rule.FunctionRewriteError("Failed to find shape for present_value.")
        if (
            attn_output_ir := self.lookup(function, attn_output)
        ) is None or attn_output_ir.shape is None:
            raise function_rule.FunctionRewriteError("Failed to find shape for attn_output.")
        head_size = present_value_ir.shape[3]
        num_key_value_heads = present_value_ir.shape[1]
        hidden_size = attn_output_ir.shape[2]
        num_attention_heads = hidden_size // head_size
        return AttnSizeConfig(
            num_attention_heads=num_attention_heads,
            num_key_value_heads=num_key_value_heads,
            head_size=head_size,
            hidden_size=hidden_size,
        )


class MHALlama2RewriteRule(AttentionRewriteRule):
    FUNCTION_KEYWORD = "LlamaAttention"
    PACKAGE_NAME = "transformers"
    _version_controller = function_rule.VersionController()

    def __init__(self) -> None:
        super().__init__()

    @_version_controller.register_version(min_version="4.33", max_version="4.36")
    def _fusion_with_4d_cache(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        if len(function.input) != 9:
            raise function_rule.FunctionRewriteError(
                f"Unexpected number of inputs. Expected 9, got {len(function.input)}."
            )

        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        # Workaround onnxscript error by specifying the output shape here.
        cos_sin_gather_size = [attn_size_config.head_size // 2]
        expand_shape = [1, attn_size_config.num_attention_heads, 1, 1]

        def mha(
            hidden_states,
            position_id,
            attention_mask,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            cos_cached,
            sin_cached,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            # TODO(onnxscript)
            # ValueError: ERROR: Unsupported expression type <class 'ast.List'>.
            # at: Function 'mha', line 16
            #     cos = op.Slice(op.Squeeze(cos_cached, [0, 1]), [0], [cos_sin_gather_size], [1])
            # NOTE: Depending on transformers version, the shape of cos/sin is different.
            # In later version, the shape is [seq_len, head_size], so the Squeeze is not needed.
            # In this version, the shape is [1, 1, seq_len, head_size], hence the below Squeeze.
            cos = op.Slice(op.Squeeze(cos_cached, [0, 1]), [0], cos_sin_gather_size, [1])
            sin = op.Slice(op.Squeeze(sin_cached, [0, 1]), [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            # TODO(onnxscript)
            # ValueError: ERROR: Unsupported expression type <class 'ast.List'>.
            # expanded_mask = op.Expand(attention_mask, [1, self.num_heads, 1, 1])
            expanded_mask = op.Expand(attention_mask, expand_shape)

            mha_output, present_key, present_value = msft_op.MultiHeadAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                expanded_mask,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(mha_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(mha).to_function_proto(), (
            onnx.helper.make_operatorsetid("com.microsoft", 1),
        )

    @_version_controller.register_version(min_version="4.36", max_version="4.38")
    def _fusion_with_2d_cache(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        if len(function.input) != 9:
            raise function_rule.FunctionRewriteError(
                f"Unexpected number of inputs. Expected 9, got {len(function.input)}."
            )

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        # Workaround onnxscript error by specifying the output shape here.
        cos_sin_gather_size = [attn_size_config.head_size // 2]
        expand_shape = [1, attn_size_config.num_attention_heads, 1, 1]

        def mha(
            hidden_states,
            position_id,
            attention_mask,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            cos_cached,
            sin_cached,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            cos = op.Slice(cos_cached, [0], cos_sin_gather_size, [1])
            sin = op.Slice(sin_cached, [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            # TODO(onnxscript)
            # ValueError: ERROR: Unsupported expression type <class 'ast.List'>.
            # expanded_mask = op.Expand(attention_mask, [1, self.num_heads, 1, 1])
            expanded_mask = op.Expand(attention_mask, expand_shape)

            mha_output, present_key, present_value = msft_op.MultiHeadAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                expanded_mask,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(mha_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(mha).to_function_proto(), (
            onnx.helper.make_operatorsetid("com.microsoft", 1),
        )


class GQALlama2RewriteRule(AttentionRewriteRule):
    FUNCTION_KEYWORD = "LlamaAttention"
    PACKAGE_NAME = "transformers"
    _version_controller = function_rule.VersionController()

    def __init__(self) -> None:
        super().__init__()

    @_version_controller.register_version(min_version="4.33", max_version="4.36")
    def _fusion_with_4d_cache(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        if len(function.input) != 9:
            raise function_rule.FunctionRewriteError(
                f"Unexpected number of inputs. Expected 9, got {len(function.input)}."
            )

        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        # Workaround onnxscript error by specifying the output shape here.
        cos_sin_gather_size = [attn_size_config.head_size // 2]

        def gqa(
            hidden_states,
            position_id,
            attention_mask,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            cos_cached,
            sin_cached,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            # NOTE: Depending on transformers version, the shape of cos/sin is different.
            # In later version, the shape is [seq_len, head_size], so the Squeeze is not needed.
            # In this version, the shape is [1, 1, seq_len, head_size], hence the below Squeeze.
            cos = op.Slice(op.Squeeze(cos_cached, [0, 1]), [0], cos_sin_gather_size, [1])
            sin = op.Slice(op.Squeeze(sin_cached, [0, 1]), [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            batch_size = op.Slice(op.Shape(hidden_states), [0], [1], [0])
            sequence_length = op.Slice(op.Shape(hidden_states), [1], [2], [0])
            past_seq_lengths = op.ConstantOfShape(
                batch_size,
                value=onnx_helper.make_tensor(
                    "past_seq_lengths", onnx.TensorProto.INT32, [1], [0]
                ),
            )
            total_seq_lengths = op.Cast(sequence_length, to=onnx.TensorProto.INT32)

            gqa_output, present_key, present_value = msft_op.GroupQueryAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                past_seq_lengths,
                total_seq_lengths,
                kv_num_heads=attn_size_config.num_key_value_heads,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(gqa_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(gqa).to_function_proto(), (
            onnx.helper.make_operatorsetid("com.microsoft", 1),
        )

    @_version_controller.register_version(min_version="4.36", max_version="4.38")
    def _fusion_with_2d_cache(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        if len(function.input) != 9:
            raise function_rule.FunctionRewriteError(
                f"Unexpected number of inputs. Expected 9, got {len(function.input)}."
            )

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        # Workaround onnxscript error by specifying the output shape here.
        cos_sin_gather_size = [attn_size_config.head_size // 2]

        def gqa(
            hidden_states,
            position_id,
            attention_mask,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            cos_cached,
            sin_cached,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            cos = op.Slice(cos_cached, [0], cos_sin_gather_size, [1])
            sin = op.Slice(sin_cached, [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            batch_size = op.Slice(op.Shape(hidden_states), [0], [1], [0])
            sequence_length = op.Slice(op.Shape(hidden_states), [1], [2], [0])
            past_seq_lengths = op.ConstantOfShape(
                batch_size,
                value=onnx_helper.make_tensor(
                    "past_seq_lengths", onnx.TensorProto.INT32, [1], [0]
                ),
            )
            total_seq_lengths = op.Cast(sequence_length, to=onnx.TensorProto.INT32)

            gqa_output, present_key, present_value = msft_op.GroupQueryAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                past_seq_lengths,
                total_seq_lengths,
                kv_num_heads=attn_size_config.num_key_value_heads,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(gqa_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(gqa).to_function_proto(), (
            onnx.helper.make_operatorsetid("com.microsoft", 1),
        )


class GQALlamaSdpa2RewriteRule(AttentionRewriteRule):
    # TODO: There are a lot of duplicated code with `MHALlama2RewriteRule`.
    # The pitfall is that the source function signature is slightly different.
    # One has `attention_mask` as input while the other does not.
    # Possibly designing a function template system could help reduce the boilerplate.
    FUNCTION_KEYWORD = "LlamaSdpaAttention"
    PACKAGE_NAME = "transformers"
    _version_controller = function_rule.VersionController()

    def __init__(self) -> None:
        super().__init__()

    @_version_controller.register_version(min_version="4.36", max_version="4.38")
    def _fusion(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        cos_sin_gather_size = [attn_size_config.head_size // 2]

        def gqa(
            hidden_states,
            position_id,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            cos_cached,
            sin_cached,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            cos = op.Slice(cos_cached, [0], cos_sin_gather_size, [1])
            sin = op.Slice(sin_cached, [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            batch_size = op.Slice(op.Shape(hidden_states), [0], [1], [0])
            sequence_length = op.Slice(op.Shape(hidden_states), [1], [2], [0])
            past_seq_lengths = op.ConstantOfShape(
                batch_size,
                value=onnx_helper.make_tensor(
                    "past_seq_lengths", onnx.TensorProto.INT32, [1], [0]
                ),
            )
            total_seq_lengths = op.Cast(sequence_length, to=onnx.TensorProto.INT32)

            gqa_output, present_key, present_value = msft_op.GroupQueryAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                past_seq_lengths,
                total_seq_lengths,
                kv_num_heads=attn_size_config.num_key_value_heads,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(gqa_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(
            gqa,
        ).to_function_proto(), (onnx.helper.make_operatorsetid("com.microsoft", 1),)

    @_version_controller.register_version(min_version="4.38")
    def _fusion_without_cos_sin_cache(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_op = onnxscript.values.Opset("com.microsoft", 1)

        cos_sin_gather_size = [attn_size_config.head_size // 2]

        def gqa(
            hidden_states,
            position_id,
            causal_mask,
            cache_position,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            inv_freq,
            o_proj_weight,
        ):
            q = op.MatMul(hidden_states, op.Transpose(q_proj_weight, [1, 0]))
            k = op.MatMul(hidden_states, op.Transpose(k_proj_weight, [1, 0]))
            v = op.MatMul(hidden_states, op.Transpose(v_proj_weight, [1, 0]))

            # In 4.38 and later, cos/sin are not cached, but computed on the fly.
            # This can be further optimized by constant folding for scenarios where
            # the position_id is known at compile time.
            seq_len = op.Slice(op.Shape(hidden_states), [1], [2], [0])
            seq_len_scalar = op.Squeeze(seq_len, [0])
            t = op.Unsqueeze(
                op.Cast(op.Range(0, seq_len_scalar, 1), to=onnx.TensorProto.FLOAT), [1]
            )
            inv_freq = op.Cast(op.Unsqueeze(inv_freq, [0]), to=onnx.TensorProto.FLOAT)
            freqs = op.MatMul(t, inv_freq)

            emb = op.Concat(freqs, freqs, axis=-1)
            cos = op.CastLike(op.Cos(emb), hidden_states)
            sin = op.CastLike(op.Sin(emb), hidden_states)
            cos = op.Slice(cos, [0], cos_sin_gather_size, [1])
            sin = op.Slice(sin, [0], cos_sin_gather_size, [1])

            q_rope = msft_op.RotaryEmbedding(q, position_id, cos, sin, interleaved=False)
            k_rope = msft_op.RotaryEmbedding(k, position_id, cos, sin, interleaved=False)

            batch_size = op.Slice(op.Shape(hidden_states), [0], [1], [0])
            sequence_length = op.Slice(op.Shape(hidden_states), [1], [2], [0])
            past_seq_lengths = op.ConstantOfShape(
                batch_size,
                value=onnx_helper.make_tensor(
                    "past_seq_lengths", onnx.TensorProto.INT32, [1], [0]
                ),
            )
            total_seq_lengths = op.Cast(sequence_length, to=onnx.TensorProto.INT32)

            gqa_output, present_key, present_value = msft_op.GroupQueryAttention(
                q_rope,
                k_rope,
                v,
                None,
                None,
                past_seq_lengths,
                total_seq_lengths,
                kv_num_heads=attn_size_config.num_key_value_heads,
                num_heads=attn_size_config.num_attention_heads,
            )
            attn_output = op.MatMul(gqa_output, op.Transpose(o_proj_weight, [1, 0]))
            return present_value, present_key, attn_output

        return onnxscript.script(default_opset=onnxscript.opset18)(
            gqa,
        ).to_function_proto(), (onnx.helper.make_operatorsetid("com.microsoft", 1),)


class AttnPhi15RewriteRule(AttentionRewriteRule):
    FUNCTION_KEYWORD = "PhiAttention"
    PACKAGE_NAME = "transformers_modules"
    _version_controller = function_rule.VersionController()

    def __init__(self) -> None:
        super().__init__()

    @_version_controller.register_version()
    def _fusion(
        self, function: onnx.FunctionProto
    ) -> tuple[onnx.FunctionProto, tuple[onnx.OperatorSetIdProto]]:
        # Infer size configurations from the function.
        attn_size_config = self.infer_attn_size_config(function)

        # Code new pattern with onnxscript.
        op = onnxscript.opset18
        msft_opset = onnxscript.values.Opset("com.microsoft", 1)

        def phi_attention(
            hidden_states,
            position_id,
            attention_mask,
            q_proj_weight,
            q_proj_bias,
            k_proj_weight,
            k_proj_bias,
            v_proj_weight,
            v_proj_bias,
            cos_cached,
            sin_cached,
            dense_weight,
            dense_bias,
        ):
            qkv_weight = op.Transpose(
                op.Concat(q_proj_weight, k_proj_weight, v_proj_weight, axis=0),
                perm=[1, 0],
            )
            qkv_bias = op.Concat(q_proj_bias, k_proj_bias, v_proj_bias, axis=0)

            # [batch_size, sequence_length]
            attention_mask_shape = op.Slice(op.Shape(hidden_states), [0], [2], [0])

            # Create 2d mask to mimic 4d causal mask.
            attention_mask = op.ConstantOfShape(
                attention_mask_shape,
                value=onnx_helper.make_tensor("mask_value", onnx.TensorProto.INT32, [1], [1]),
            )
            attn_output, present = msft_opset.Attention(
                hidden_states,
                qkv_weight,
                qkv_bias,
                attention_mask,
                unidirectional=1,
                do_rotary=1,
                # Attention.rotary_embedding_dim only supports 32, 64 or 128
                rotary_embedding_dim=attn_size_config.head_size // 2 // 32 * 32,
                num_heads=attn_size_config.num_attention_heads,
            )
            present_key = op.Gather(present, 0)
            present_value = op.Gather(present, 1)
            output = op.Add(
                op.MatMul(attn_output, op.Transpose(dense_weight, [1, 0])), dense_bias
            )

            return present_value, present_key, output

        return onnxscript.script(default_opset=onnxscript.opset18)(
            phi_attention
        ).to_function_proto(), (onnx.helper.make_operatorsetid("com.microsoft", 1),)
