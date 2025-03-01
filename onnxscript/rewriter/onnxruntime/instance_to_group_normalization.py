from __future__ import annotations

import logging
from typing import Any

import numpy as np
import onnx

import onnxscript._legacy_ir as ir
from onnxscript.rewriter import pattern

op = pattern.onnxop
msft_op = pattern.msft_op
torch_module_op = pattern.torch_module_op

logger = logging.getLogger(__name__)


def _check_if_simulated_instance_norm_is_used_impl(
    input_x,
    adjusted_input_shape,
    original_input_shape,
    weight_for_norm,
    bias_for_norm,
    weight_full,
    bias_full,
    **kwargs,
) -> bool:
    if not np.all(weight_for_norm.value_as_np_array == 1):
        return False
    if not np.all(bias_for_norm.value_as_np_array == 0):
        return False

    input_rank_minus_one = len(input_x.shape) - 1
    weight_full_rank = len(weight_full.shape)
    bias_full_rank = len(bias_full.shape)
    if weight_full_rank != input_rank_minus_one or bias_full_rank != input_rank_minus_one:
        return False

    input_rank = len(input_x.shape)
    if input_rank != 4:
        return False

    weight_full_shape = weight_full.shape
    if not all(dim == 1 for dim in weight_full_shape[1:]):
        return False
    bias_full_shape = bias_full.shape
    if not all(dim == 1 for dim in bias_full_shape[1:]):
        return False

    adjusted_input_shape = adjusted_input_shape.value_as_np_array
    g = weight_for_norm.shape[0]
    if adjusted_input_shape is None or adjusted_input_shape.tolist() != [0, g, -1]:
        return False

    # NOTE: Restrict the rule to only support constant shape
    original_input_shape = original_input_shape.value_as_np_array
    if original_input_shape is None or original_input_shape.tolist() != input_x.shape:
        return False

    return True


def check_if_simulated_instance_norm_is_used(
    match_bindings: dict[str, ir.Value | Any],
) -> bool:
    """Check if the simulated instance normalization is used.

    In torchlib with opset18, onnx.GroupNorm is using wrong definition, so
    we use InstanceNormalization to simulate GroupNormalization. We need to check if there are arguments created to simulation.
    If there are, then we need to replace the pattern. If they are not used, then we don't need to replace the pattern.

    To validate this, we need to check the following:
    1. weight_for_norm are all 1 and bias_for_norm are all 0, as they are created for the simulation.
    2. weight_full and bias_full are unsqueezed to be easily broadcastable.
    3. input rank should be 4
    4. weight_full and bias_full should have ones except first dim.
    5. adjusted_input_shape is a constant tensor of form [0, g, -1]
    6. original_input_shape is the same as input_x shape.

    Args:
        match_bindings: The match binding dictionary from a MatchResult.

    Returns:
        bool: True if the simulated instance normalization is used, False otherwise.
    """
    return _check_if_simulated_instance_norm_is_used_impl(**match_bindings)


def instance_simulates_group_normalization_pattern(
    input_x,
    adjusted_input_shape,
    original_input_shape,
    weight_for_norm,
    bias_for_norm,
    weight_full,
    bias_full,
    epsilon,
    match_bindings: dict[str, ir.Value | Any] | None = None,
):
    adjusted_input = op.Reshape(input_x, adjusted_input_shape)
    inst_norm = op.InstanceNormalization(
        adjusted_input, weight_for_norm, bias_for_norm, epsilon=epsilon
    )
    adjusted_inst_norm = op.Reshape(inst_norm, original_input_shape)
    mul = op.Mul(adjusted_inst_norm, weight_full)
    return op.Add(mul, bias_full)


def group_normalization(
    input_x,
    adjusted_input_shape,
    original_input_shape,
    weight_for_norm,
    bias_for_norm,
    weight_full,
    bias_full,
    epsilon,
    match_bindings: dict[str, ir.Value | Any] | None = None,
):
    # com.microsoft.GroupNorm only supports NHWC for now
    nhwc_input = op.Transpose(input_x, perm=[0, 2, 3, 1])
    # com.microsoft.GroupNorm only supports gamma and beta as float type
    weight_full = op.Cast(weight_full, to=onnx.TensorProto.FLOAT)
    reshape_to_1d = op.Constant(value_ints=[-1])
    weight_full = op.Reshape(weight_full, reshape_to_1d)
    bias_full = op.Cast(bias_full, to=onnx.TensorProto.FLOAT)
    bias_full = op.Reshape(bias_full, reshape_to_1d)
    # re-obtain attribute groups
    groups = match_bindings["weight_for_norm"].shape[0]
    output = msft_op.GroupNorm(
        nhwc_input,
        weight_full,
        bias_full,
        activation=0,
        channels_last=1,
        epsilon=epsilon,
        groups=groups,
    )
    return op.Transpose(output, perm=[0, 3, 1, 2])


# Register the rewrite rules
instance_norm_to_group_norm_rule = pattern.RewriteRule(
    instance_simulates_group_normalization_pattern,
    pattern.ReplacementPatternFunction(group_normalization, delay_run=True),
    check_if_simulated_instance_norm_is_used,
)

# NOTE: instance_norm_to_group_norm_rule is subset of instance_norm_to_group_norm_with_silu_rule,
# so we need to run instance_norm_to_group_norm_with_silu_rule first.
rules = pattern.RewriteRuleSet([instance_norm_to_group_norm_rule])
