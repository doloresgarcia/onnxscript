from onnxscript.rewriter import pattern
from onnxscript.rewriter.broadcast_to_matmul import check_if_need_reshape

op = pattern.onnxop


# Pattern to match against
def reshape_gemm_reshape_pattern(input_a, input_b, input_c, shape_a, shape_c):
    reshape_a = op.Reshape(input_a, shape_a)
    # TODO: Temporary workaround to support benchmodels.
    # Tracked by https://github.com/microsoft/onnx-rewriter/issues/197.
    gemm = op.Gemm(reshape_a, input_b, input_c, alpha=1.0, beta=1.0)
    return op.Reshape(gemm, shape_c)


def matmul_add(input_a, input_b, input_c, shape_a, shape_d):
    matmul = op.MatMul(input_a, input_b)
    return op.Add(matmul, input_c)


rule = pattern.RewriteRule(reshape_gemm_reshape_pattern, matmul_add, check_if_need_reshape)
