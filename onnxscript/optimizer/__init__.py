import logging
from typing import Any

import onnx

from onnxscript import rewriter
from onnxscript.optimizer.constant_folding import fold_constants
from onnxscript.optimizer.copy_propagation import (
    do_copy_propagation,
    do_sequence_simplification,
)
from onnxscript.optimizer.remove_unused import remove_unused_nodes
from onnxscript.optimizer.remove_unused_function import remove_unused_functions
from onnxscript.optimizer.simple_function_folding import (
    inline_functions_with_unused_outputs,
    inline_simple_functions,
)
from onnxscript.rewriter import (
    broadcast_to_matmul,
    cast_constant_of_shape,
    gemm_to_matmul_add,
    no_op,
)

logger = logging.getLogger(__name__)


def optimize(
    model: onnx.ModelProto,
    num_iterations: int = 2,
    *,
    onnx_shape_inference: bool = True,
    stop_if_no_change: bool = True,
    external_data_folder: str = "",
    **kwargs: Any,
) -> onnx.ModelProto:
    """Optimize the model. Perform optimizations and clean-ups such as constant folding, dead code elimination, etc.

    Args:
        model (onnx.ModelProto): The model to optimize.
        num_iterations (int, optional): Number of iterations to perform.
        onnx_shape_inference (bool, optional): Whether to perform onnx shape inference on the model.
            Set this to False to turn off onnx shape inference, and rely on model carried shapes and types.
            This is useful for models produced by PyTorch 2.2+ dynamo onnx exporter, where the model carries
            the symbolic shapes recorded from dynamo tracing.
        stop_if_no_change (bool, optional): Whether to stop if no change is detected.
        external_data_folder (str, optional): The folder to store external data.
        **kwargs: Additional keyword arguments. For BC purposes.
    """
    if kwargs.pop("function_aware_folding", None) is not None:
        logger.warning(
            "'function_aware_folding' is deprecated. 'optimize' now supports both fully inlined models and models with functions. "
            "To achieve the same behavior as 'function_aware_folding=True' before, set 'onnx_shape_inference=False'. "
            "This would turn off incremental onnx shape inference and rely on model carried shapes and types. "
            "See 'onnx_shape_inference' for more details."
        )
    for _ in range(num_iterations):
        if onnx_shape_inference:
            if model.ByteSize() < 1024 * 1024 * 1024 * 2:
                model = onnx.shape_inference.infer_shapes(
                    model, check_type=True, strict_mode=True, data_prop=True
                )
            else:
                logger.warning(
                    "The model size is too large for full model shape inference. "
                    "Skipping this step."
                )

        inline_simple_functions(model)
        modified = fold_constants(
            model, external_data_folder, onnx_shape_inference=onnx_shape_inference
        )

        remove_unused_nodes(model)
        inline_simple_functions(model)
        remove_unused_functions(model)
        inline_functions_with_unused_outputs(model)
        # NOTE: This is general rewrite rules
        model = rewriter.rewrite(
            model,
            pattern_rewrite_rules=[
                *no_op.rules.rules,  # TODO: merge this rule into constant folding?
                *broadcast_to_matmul.rules.rules,
                gemm_to_matmul_add.rule,
                *cast_constant_of_shape.rules.rules,
            ],
        )
        if stop_if_no_change and not modified:
            logger.debug("Stopping after %d iterations.", _)
            break

    for node in model.graph.node:
        logger.debug("Node %s::%s name %s.", node.domain, node.op_type, node.name)

    for function in model.functions:
        for node in function.node:
            logger.debug(
                "Function %s::%s node %s::%s name %s.",
                function.domain,
                function.name,
                node.domain,
                node.op_type,
                node.name,
            )

    # do_sequence_simplification(model)
    return model


__all__ = [
    "fold_constants",
    "remove_unused_nodes",
    "optimize",
    "do_copy_propagation",
    "do_sequence_simplification",
]
