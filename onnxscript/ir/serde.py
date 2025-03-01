# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
"""Serialize and deserialize the intermediate representation to/from ONNX protos."""

# NOTES for developers:
# NOTE: Do not import pathlib in the IR. It is slow. Use os.path methods instead.
#
# NOTE: Protobuf serialization
#     Initializing a protobuf message with initialized protobuf messages incurs
#     a copy and is slow. Instead, use proto.add() to add to a repeated field.
#     or initialize the message first and then set the fields if the fields are
#     plain Python objects.

from __future__ import annotations

__all__ = [
    # Tensors
    "DoubleDataTensor",
    "FloatDataTensor",
    "Int32DataTensor",
    "Int64DataTensor",
    "TensorProtoTensor",
    "UInt64DataTensor",
    # Deserialization
    "deserialize_attribute",
    "deserialize_function",
    "deserialize_graph",
    "deserialize_model",
    "deserialize_node",
    "deserialize_opset_import",
    "deserialize_tensor",
    "deserialize_type_proto_for_shape",
    "deserialize_type_proto_for_type",
    "deserialize_value_info_proto",
    # Serialization
    "serialize_attribute_into",
    "serialize_attribute",
    "serialize_dimension_into",
    "serialize_function_into",
    "serialize_function",
    "serialize_graph_into",
    "serialize_graph",
    "serialize_model_into",
    "serialize_model",
    "serialize_node_into",
    "serialize_node",
    "serialize_shape_into",
    "serialize_reference_attribute_into",
    "serialize_tensor_into",
    "serialize_tensor",
    "serialize_type_into",
    "serialize_value_into",
    "serialize_value",
]

import collections
import logging
import os
import typing
from typing import Any, List, Mapping, Sequence

import numpy as np
import onnx
import onnx.external_data_helper
import onnx.numpy_helper

from onnxscript.ir import (
    _core,
    _enums,
    _protocols,
)

if typing.TYPE_CHECKING:
    import google.protobuf.internal.containers as proto_containers

logger = logging.getLogger(__name__)
_FUNCTION_VALUE_INFO_SUPPORTED_VERSION = (
    10  # ONNX IR version where value info in functions was introduced
)


class TensorProtoTensor(_core.TensorBase):
    """A tensor initialized from a tensor proto."""

    def __init__(self, proto: onnx.TensorProto) -> None:
        self._proto = proto

    @property
    def name(self) -> str:
        return self._proto.name

    @property
    def shape(self) -> _core.Shape:
        return _core.Shape(self._proto.dims)

    @property
    def dtype(self) -> _enums.DataType:
        return _enums.DataType(self._proto.data_type)

    @property
    def doc_string(self) -> str:
        return self._proto.doc_string

    @property
    def raw(self) -> onnx.TensorProto:
        return self._proto

    def __repr__(self) -> str:
        return f"{self._repr_base()}({self.name!r})"

    def __array__(self, dtype: Any = None) -> np.ndarray:
        """Return the tensor as a numpy array, compatible with np.array."""
        return self.numpy().__array__(dtype)

    def numpy(self) -> np.ndarray:
        """Return the tensor as a numpy array."""
        return onnx.numpy_helper.to_array(self._proto)

    def tobytes(self) -> bytes:
        """Return the tensor as a byte string conformed to the ONNX specification, in little endian."""
        if not self._proto.HasField("raw_data"):
            raise ValueError(
                "Cannot convert non-raw tensor to bytes. Use a specialized tensor class like FloatDataTensor instead."
            )
        return self._proto.raw_data


class FloatDataTensor(TensorProtoTensor):  # pylint: disable=too-many-ancestors
    """Specialized tensor for float data.

    When serializing, the data can be stored in the float_data field.
    """

    compatible_types = frozenset((_enums.DataType.FLOAT, _enums.DataType.COMPLEX64))

    def __init__(self, proto: onnx.TensorProto) -> None:
        super().__init__(proto)
        if proto.data_type not in self.compatible_types:
            raise ValueError(
                f"Expected FLOAT or COMPLEX64 data type, got {_enums.DataType(proto.data_type)}"
            )

    def float_data(self) -> Sequence[float]:
        return self._proto.float_data

    def tobytes(self) -> bytes:
        return np.array(self._proto.float_data, dtype=np.float32).tobytes()


class Int32DataTensor(TensorProtoTensor):  # pylint: disable=too-many-ancestors
    compatible_types = frozenset(
        (
            _enums.DataType.INT32,
            _enums.DataType.INT16,
            _enums.DataType.INT8,
            _enums.DataType.INT4,
            _enums.DataType.UINT16,
            _enums.DataType.UINT8,
            _enums.DataType.UINT4,
            _enums.DataType.BOOL,
            _enums.DataType.FLOAT16,
            _enums.DataType.BFLOAT16,
            _enums.DataType.FLOAT8E4M3FN,
            _enums.DataType.FLOAT8E4M3FNUZ,
            _enums.DataType.FLOAT8E5M2,
            _enums.DataType.FLOAT8E5M2FNUZ,
        )
    )

    def __init__(self, proto: onnx.TensorProto) -> None:
        super().__init__(proto)
        if proto.data_type not in self.compatible_types:
            raise ValueError(
                "Expected INT32, INT16, INT8, INT4, UINT16, UINT8, UINT4, BOOL, "
                "FLOAT16, BFLOAT16, FLOAT8E4M3FN, FLOAT8E4M3FNUZ, FLOAT8E5M2, FLOAT8E5M2FNUZ "
                f"data type, got {_enums.DataType(proto.data_type)}"
            )

    def int32_data(self) -> Sequence[int]:
        return self._proto.int32_data

    def tobytes(self) -> bytes:
        return np.array(self._proto.int32_data, dtype=np.int32).tobytes()


class Int64DataTensor(TensorProtoTensor):  # pylint: disable=too-many-ancestors
    compatible_types = frozenset((_enums.DataType.INT64,))

    def __init__(self, proto: onnx.TensorProto) -> None:
        super().__init__(proto)
        if proto.data_type not in self.compatible_types:
            raise ValueError(
                f"Expected INT64 data type, got {_enums.DataType(proto.data_type)}"
            )

    def int64_data(self) -> Sequence[int]:
        return self._proto.int64_data

    def tobytes(self) -> bytes:
        return np.array(self._proto.int64_data, dtype=np.int64).tobytes()


class DoubleDataTensor(TensorProtoTensor):  # pylint: disable=too-many-ancestors
    compatible_types = frozenset((_enums.DataType.DOUBLE, _enums.DataType.COMPLEX128))

    def __init__(self, proto: onnx.TensorProto) -> None:
        super().__init__(proto)
        if proto.data_type not in self.compatible_types:
            raise ValueError(
                f"Expected DOUBLE or COMPLEX128 data type, got {_enums.DataType(proto.data_type)}"
            )

    def double_data(self) -> Sequence[float]:
        return self._proto.double_data

    def tobytes(self) -> bytes:
        return np.array(self._proto.double_data, dtype=np.float64).tobytes()


class UInt64DataTensor(TensorProtoTensor):  # pylint: disable=too-many-ancestors
    compatible_types = frozenset((_enums.DataType.UINT64, _enums.DataType.UINT32))

    def __init__(self, proto: onnx.TensorProto) -> None:
        super().__init__(proto)
        if proto.data_type not in self.compatible_types:
            raise ValueError(
                f"Expected UINT64 or UINT32 data type, got {_enums.DataType(proto.data_type)}"
            )

    def uint64_data(self) -> Sequence[int]:
        return self._proto.uint64_data

    def tobytes(self) -> bytes:
        return np.array(self._proto.uint64_data, dtype=np.uint64).tobytes()


def _get_field(proto: Any, field: str) -> Any:
    if proto.HasField(field):
        return getattr(proto, field)
    return None


# Deserialization


def deserialize_opset_import(
    protos: Sequence[onnx.OperatorSetIdProto],
) -> dict[str, int]:
    return {opset.domain: opset.version for opset in protos}


def _parse_experimental_function_value_info_name(
    name: str,
) -> tuple[str, str, str] | None:
    """Get the function domain, name and value name if the value info is for a function.

    The experimental format is:
    {function_domain}::{function_name}/{value_name}

    Args:
        name: The name stored in the value info.

    Returns:
        A tuple of the function domain, function name and value name if the value info is for a function.
        None otherwise.
    """
    parts = name.split("/")
    expected_parts = 2
    if len(parts) != expected_parts:
        return None
    function, value_name = parts
    parts = function.split("::")
    if len(parts) != expected_parts:
        return None
    # NOTE: There will not be overload because overloads are introduced in ONNX IR v10, which also
    # introduces the ValueInfoProto for functions
    function_domain, function_name = parts
    return function_domain, function_name, value_name


def deserialize_model(proto: onnx.ModelProto) -> _core.Model:
    graph = _deserialize_graph(proto.graph, [])
    graph.opset_imports.update(deserialize_opset_import(proto.opset_import))

    functions = []
    for func in proto.functions:
        functions.append(deserialize_function(func))

    model = _core.Model(
        graph,
        ir_version=proto.ir_version,
        producer_name=_get_field(proto, "producer_name"),
        producer_version=_get_field(proto, "producer_version"),
        domain=_get_field(proto, "domain"),
        model_version=_get_field(proto, "model_version"),
        doc_string=_get_field(proto, "doc_string"),
        functions=functions,
    )

    # Handle experimental value info for functions created by the dynamo exporter in IR version 9
    if model.ir_version < _FUNCTION_VALUE_INFO_SUPPORTED_VERSION:
        _deserialized_experimental_value_info_for_function_ir9(
            model.functions, proto.graph.value_info
        )

    return model


def _deserialized_experimental_value_info_for_function_ir9(
    functions: Mapping[_protocols.OperatorIdentifier, _core.Function],
    value_info_protos: Sequence[onnx.ValueInfoProto],
) -> None:
    """Deserialize value info for functions when they are stored in an experimental format.

    The experimental format is:
    {function_domain}::{function_name}/{value_name}
    """
    # Parse value info for functions from the main graph
    function_value_value_info_mapping: collections.defaultdict[
        _protocols.OperatorIdentifier,
        dict[str, onnx.ValueInfoProto],
    ] = collections.defaultdict(dict)
    for value_info_proto in value_info_protos:
        if (
            parsed := _parse_experimental_function_value_info_name(value_info_proto.name)
        ) is None:
            continue
        function_domain, function_name, value_name = parsed
        function_overload = ""
        # TODO(justinchuby): Create a constructor for OperatorIdentifier so we don't create tuples manually
        function_id = (function_domain, function_name, function_overload)
        function = functions.get(function_id)
        if function is None:
            # Function not found
            logger.warning(
                "Function with ID '%s' not found in model functions. Value info '%s' will be ignored.",
                function_id,
                value_info_proto.name,
            )
            continue
        function_value_value_info_mapping[function_id][value_name] = value_info_proto
    for function_id, function in functions.items():
        for input in function.inputs:
            if input.name in function_value_value_info_mapping[function_id]:
                deserialize_value_info_proto(
                    function_value_value_info_mapping[function_id][input.name], input
                )
        for node in function.nodes:
            for output in node.outputs:
                if output.name in function_value_value_info_mapping[function_id]:
                    deserialize_value_info_proto(
                        function_value_value_info_mapping[function_id][output.name],
                        output,
                    )
            # The function outputs are handled as well because they are also node outputs


def deserialize_graph(proto: onnx.GraphProto) -> _core.Graph:
    return _deserialize_graph(proto, [])


def _deserialize_graph(
    proto: onnx.GraphProto, scoped_values: list[dict[str, _core.Value]]
) -> _core.Graph:
    """Deserialize a graph proto, recursively if needed.

    Args:
        proto: The graph proto to deserialize.
        scoped_values: A list of dictionaries mapping value names to their corresponding Value objects.
            Every time we enter a new graph, a new scope is created and appended to this list to include
            all values defined in the scope.
        scoped_value_info: A list of dictionaries mapping value names to their corresponding ValueInfoProto.
    """
    # Create values for initializers and inputs
    initializers = [deserialize_tensor(tensor) for tensor in proto.initializer]
    inputs = [_core.Input(info.name) for info in proto.input]
    for info, value in zip(proto.input, inputs):
        deserialize_value_info_proto(info, value)

    # Initialize the values dictionary for this graph scope with the inputs and initializers
    values: dict[str, _core.Value] = {v.name: v for v in inputs}  # type: ignore[misc]
    scoped_values.append(values)
    for initializer in initializers:
        if initializer.name in values:
            # The initializer is for an input
            values[initializer.name].const_value = initializer
        else:
            # The initializer is for some other value. Create this value first
            initializer_value = _core.Value(
                None,
                def_index=None,
                name=initializer.name,
                # TODO(justinchuby): Fix type hinting for shape and dtype
                shape=initializer.shape,  # type: ignore
                type=_core.TensorType(initializer.dtype),
                const_value=initializer,
            )
            values[initializer.name] = initializer_value

    # Add ValueInfos for this graph scope
    value_info = {info.name: info for info in proto.value_info}

    # Deserialize nodes with all known values
    # TODO(justinchuby): Handle unsorted nodes
    nodes = [_deserialize_node(node, scoped_values, value_info) for node in proto.node]

    # Fill in values for graph outputs
    outputs = [deserialize_value_info_proto(info, values[info.name]) for info in proto.output]
    scoped_values.pop()
    return _core.Graph(
        inputs,
        outputs,
        nodes=nodes,
        initializers=initializers,
        doc_string=_get_field(proto, "doc_string"),
        name=_get_field(proto, "name"),
    )


def deserialize_function(proto: onnx.FunctionProto) -> _core.Function:
    inputs = [_core.Input(name) for name in proto.input]
    values: dict[str, _core.Value] = {v.name: v for v in inputs}  # type: ignore[misc]
    value_info = {info.name: info for info in getattr(proto, "value_info", [])}

    # TODO(justinchuby): Handle unsorted nodes
    nodes = [_deserialize_node(node, [values], value_info=value_info) for node in proto.node]
    outputs = [values[name] for name in proto.output]
    graph = _core.Graph(
        inputs,
        outputs,
        nodes=nodes,
        initializers=(),
        doc_string=_get_field(proto, "doc_string"),
        opset_imports=deserialize_opset_import(proto.opset_import),
        name=(
            f"{proto.name}_{proto.domain}" + f"__{proto.overload}"
            if hasattr(proto, "overload") and proto.overload
            else ""
        ),
    )
    attributes = [_deserialize_attribute(attr, []) for attr in proto.attribute_proto]
    # Attributes without defaults
    attributes += [
        _core.Attr(name, _enums.AttributeType.UNDEFINED, None) for name in proto.attribute
    ]
    return _core.Function(
        domain=proto.domain,
        name=proto.name,
        overload=getattr(proto, "overload", ""),
        graph=graph,
        attributes=typing.cast(List[_core.Attr], attributes),
    )


def deserialize_value_info_proto(
    proto: onnx.ValueInfoProto, value: _core.Value | None
) -> _core.Value:
    if value is None:
        value = _core.Value(None, def_index=None)
        value.name = proto.name
    value.shape = deserialize_type_proto_for_shape(proto.type)
    value.type = deserialize_type_proto_for_type(proto.type)
    return value


def deserialize_type_proto_for_shape(proto: onnx.TypeProto) -> _core.Shape | None:
    if proto.HasField("tensor_type"):
        if (shape_proto := _get_field(proto.tensor_type, "shape")) is None:
            return None
        # This logic handles when the shape is [] as well
        dim_protos = shape_proto.dim
        return _core.Shape([deserialize_dimension(d) for d in dim_protos])
    if proto.HasField("sparse_tensor_type"):
        if (shape_proto := _get_field(proto.sparse_tensor_type, "shape")) is None:
            return None
        dim_protos = shape_proto.dim
        return _core.Shape([deserialize_dimension(d) for d in dim_protos])
    if proto.HasField("sequence_type"):
        if (elem_type := _get_field(proto.sequence_type, "elem_type")) is None:
            return None
        return deserialize_type_proto_for_shape(elem_type)
    if proto.HasField("optional_type"):
        if (elem_type := _get_field(proto.optional_type, "elem_type")) is None:
            return None
        return deserialize_type_proto_for_shape(elem_type)
    if proto.HasField("map_type"):
        # TODO(justinchuby): Do we need to support map types?
        raise NotImplementedError("Map types are not supported yet")

    return None


def deserialize_type_proto_for_type(
    proto: onnx.TypeProto,
) -> _protocols.TypeProtocol | None:
    denotation = _get_field(proto, "denotation")
    if proto.HasField("tensor_type"):
        if (elem_type := _get_field(proto.tensor_type, "elem_type")) is None:
            return None
        return _core.TensorType(_enums.DataType(elem_type), denotation=denotation)
    if proto.HasField("sparse_tensor_type"):
        if (elem_type := _get_field(proto.sparse_tensor_type, "elem_type")) is None:
            return None
        return _core.SparseTensorType(_enums.DataType(elem_type), denotation=denotation)
    if proto.HasField("sequence_type"):
        # FIXME(justinchuby): Allow nested types being None
        if (elem_type := _get_field(proto.sequence_type, "elem_type")) is None:
            raise ValueError(f"SequenceTypeProto must have elem_type set: {proto}")
        nested_type = deserialize_type_proto_for_type(elem_type)
        if nested_type is None:
            raise ValueError(f"SequenceType must have elem_type set: {proto}")
        return _core.SequenceType(nested_type, denotation=denotation)
    if proto.HasField("optional_type"):
        # FIXME(justinchuby): Allow nested types being None
        if (elem_type := _get_field(proto.optional_type, "elem_type")) is None:
            raise ValueError(f"SequenceTypeProto must have elem_type set: {proto}")
        nested_type = deserialize_type_proto_for_type(elem_type)
        if nested_type is None:
            raise ValueError(f"SequenceType must have elem_type set: {proto}")
        return _core.OptionalType(nested_type, denotation=denotation)
    if proto.HasField("map_type"):
        # TODO(justinchuby): Do we need to support map types?
        raise NotImplementedError("Map types are not supported yet")

    return None


def deserialize_dimension(proto: onnx.TensorShapeProto.Dimension) -> _core.Dimension:
    value_field = proto.WhichOneof("value")
    denotation = _get_field(proto, "denotation")
    if value_field is not None:
        return _core.Dimension(getattr(proto, value_field), denotation=denotation)
    return _core.Dimension(None)


def deserialize_tensor(
    tensor: onnx.TensorProto, base_path: str | os.PathLike = ""
) -> _protocols.TensorProtocol:
    # TODO: Sanitize base_path
    if tensor.data_location == onnx.TensorProto.EXTERNAL:
        external_info = onnx.external_data_helper.ExternalDataInfo(tensor)
        return _core.ExternalTensor(
            path=os.path.join(base_path, external_info.location),
            offset=external_info.offset,
            length=external_info.length,
            dtype=_enums.DataType(tensor.data_type),
            name=tensor.name,
            shape=_core.Shape(tensor.dims),
            doc_string=tensor.doc_string,
        )
    # Check for the raw_data filed first. The rest of the repeating fields can be
    # empty and still valid, so we don't need to check their length
    # For example, int32_data can be empty and still be a valid tensor.
    if tensor.HasField("raw_data"):
        return TensorProtoTensor(tensor)
    if tensor.data_type in FloatDataTensor.compatible_types:
        return FloatDataTensor(tensor)
    if tensor.data_type in Int32DataTensor.compatible_types:
        return Int32DataTensor(tensor)
    if tensor.data_type in Int64DataTensor.compatible_types:
        return Int64DataTensor(tensor)
    if tensor.data_type in DoubleDataTensor.compatible_types:
        return DoubleDataTensor(tensor)
    if tensor.data_type in UInt64DataTensor.compatible_types:
        return UInt64DataTensor(tensor)
    raise ValueError(
        f"TensorProto(name={tensor.name}) does not have any data fields set and is not an external tensor."
    )


def deserialize_attribute(proto: onnx.AttributeProto) -> _core.Attr | _core.RefAttr:
    return _deserialize_attribute(proto, [])


def _deserialize_attribute(
    proto: onnx.AttributeProto, scoped_values: list[dict[str, _core.Value]]
) -> _core.Attr | _core.RefAttr:
    name = proto.name
    doc_string = _get_field(proto, "doc_string")
    type_ = _enums.AttributeType(proto.type)
    ref_attr_name = _get_field(proto, "ref_attr_name")
    if ref_attr_name:
        return _core.RefAttr(name, ref_attr_name, type_, doc_string=doc_string)

    if type_ == _enums.AttributeType.INT:
        return _core.AttrInt64(name, proto.i, doc_string=doc_string)
    if type_ == _enums.AttributeType.FLOAT:
        return _core.AttrFloat32(name, proto.f, doc_string=doc_string)
    if type_ == _enums.AttributeType.STRING:
        return _core.AttrString(name, proto.s.decode("utf-8"), doc_string=doc_string)
    if type_ == _enums.AttributeType.INTS:
        return _core.AttrInt64s(name, proto.ints, doc_string=doc_string)
    if type_ == _enums.AttributeType.FLOATS:
        return _core.AttrFloat32s(name, proto.floats, doc_string=doc_string)
    if type_ == _enums.AttributeType.STRINGS:
        return _core.AttrStrings(
            name, [s.decode("utf-8") for s in proto.strings], doc_string=doc_string
        )
    if type_ == _enums.AttributeType.TENSOR:
        return _core.AttrTensor(name, deserialize_tensor(proto.t), doc_string=doc_string)
    if type_ == _enums.AttributeType.GRAPH:
        return _core.AttrGraph(
            name, _deserialize_graph(proto.g, scoped_values), doc_string=doc_string
        )
    if type_ == _enums.AttributeType.TENSORS:
        return _core.AttrTensors(
            name,
            [deserialize_tensor(t) for t in proto.tensors],
            doc_string=doc_string,
        )
    if type_ == _enums.AttributeType.GRAPHS:
        return _core.AttrGraphs(
            name,
            [_deserialize_graph(g, scoped_values) for g in proto.graphs],
            doc_string=doc_string,
        )
    # TODO: Handle type protos etc.
    raise ValueError(f"Unsupported attribute type: '{type_}'")


def deserialize_node(proto: onnx.NodeProto) -> _core.Node:
    return _deserialize_node(proto, scoped_values=[], value_info={})


def _deserialize_node(
    proto: onnx.NodeProto,
    scoped_values: list[dict[str, _core.Value]],
    value_info: dict[str, onnx.ValueInfoProto],
) -> _core.Node:
    node_inputs: list[_core.Value | None] = []
    for name in proto.input:
        if name == "":
            # Empty input
            node_inputs.append(None)
            continue
        found = False
        for values in reversed(scoped_values):
            if name not in values:
                continue
            node_inputs.append(values[name])
            found = True
            break
        if not found:
            raise ValueError(
                f"Input '{name}' of node '{proto.name}({proto.domain}::{proto.op_type}:{getattr(proto, 'overload', '')})' not found in any scope"
                f" (current depth: {len(scoped_values)})"
            )
    node = _core.Node(
        proto.domain,
        proto.op_type,
        node_inputs,
        [_deserialize_attribute(a, scoped_values) for a in proto.attribute],
        overload=getattr(proto, "overload", ""),
        num_outputs=len(proto.output),
        name=proto.name,
    )

    for output, value in zip(proto.output, node.outputs):
        value.name = output
        if output in value_info:
            deserialize_value_info_proto(value_info[output], value)
        else:
            logger.debug(
                "ValueInfoProto not found for output '%s' in node '%s' of type '%s'",
                output,
                proto.name,
                proto.op_type,
            )
        scoped_values[-1][output] = value
    for prop in getattr(proto, "metadata_props", []):
        node.metadata_props[prop.key] = prop.value
    return node


# Serialization


def serialize_model(model: _protocols.ModelProtocol) -> onnx.ModelProto:
    return serialize_model_into(onnx.ModelProto(), from_=model)


def serialize_model_into(
    model_proto: onnx.ModelProto, from_: _protocols.ModelProtocol
) -> onnx.ModelProto:
    """Serialize an IR model to an ONNX model proto."""
    model_proto.ir_version = from_.ir_version
    if from_.producer_name:
        model_proto.producer_name = from_.producer_name
    if from_.producer_version:
        model_proto.producer_version = from_.producer_version
    if from_.domain:
        model_proto.domain = from_.domain
    if from_.model_version:
        model_proto.model_version = from_.model_version
    if from_.doc_string:
        model_proto.doc_string = from_.doc_string
    # Sort names for deterministic serialization
    _serialize_opset_imports_into(model_proto.opset_import, from_.opset_imports)
    if from_.metadata_props:
        _serialize_metadata_props_into(model_proto.metadata_props, from_.metadata_props)
    serialize_graph_into(model_proto.graph, from_.graph)

    create_value_info_in_functions = from_.ir_version >= _FUNCTION_VALUE_INFO_SUPPORTED_VERSION
    for func in from_.functions.values():
        serialize_function_into(
            model_proto.functions.add(),
            from_=func,
            create_value_info=create_value_info_in_functions,
        )
        if not create_value_info_in_functions:
            # Create them in the main graph instead
            _serialize_experimental_value_info_for_function_ir9_into(model_proto.graph, func)
    return model_proto


def _should_create_value_info_for_value(value: _protocols.ValueProtocol) -> bool:
    """Check if value info should be created for a value.

    Args:
        value: The value to check.

    Returns:
        True if value info should be created for the value.
    """
    # No need to serialize value info if it is not set
    return not (value.shape is None and value.type is None)


def _serialize_experimental_value_info_for_function_ir9_into(
    graph_proto: onnx.GraphProto, function: _protocols.FunctionProtocol
) -> None:
    """Serialize value info for functions in an experimental format for IR version 9.

    Because IRv9 and older does not have ValueInfoProto for functions, we give the value info
    special names and store them in the main graph instead.

    The experimental format is:
    {function_domain}::{function_name}/{value_name}

    Args:
        graph_proto: The graph proto to create ValueInfoProto in.
        function: The function to serialize.
    """
    # TODO(justinchuby): In the future, we can decide if it is a good idea to simply iterate over
    # all values in the function and call serialize_value_into instead.
    function_qualified_name = f"{function.domain}::{function.name}"

    def format_name(value_name: str) -> str:
        return f"{function_qualified_name}/{value_name}"

    for input in function.inputs:
        if not input.name:
            logging.warning(
                "Function '%s': Value name not set for function input: %s",
                function_qualified_name,
                input,
            )
            continue
        if not _should_create_value_info_for_value(input):
            # No need to serialize value info if it is not set
            continue
        serialize_value_into(graph_proto.value_info.add(), input, name=format_name(input.name))
    for node in function.nodes:
        for node_output in node.outputs:
            if not node_output.name:
                logging.warning(
                    "Function '%s': Value name not set for node output: %s",
                    function_qualified_name,
                    node_output,
                )
                continue
            if not _should_create_value_info_for_value(node_output):
                # No need to serialize value info if it is not set
                continue
            serialize_value_into(
                graph_proto.value_info.add(),
                node_output,
                name=format_name(node_output.name),
            )


def _serialize_opset_imports_into(
    opset_ids: proto_containers.RepeatedCompositeFieldContainer[onnx.OperatorSetIdProto],
    from_: Mapping[str, int],
) -> None:
    """Serialize opset imports into a repeated field of OperatorSetId protos.

    Args:
        opset_ids: The repeated field to serialize into.
        from_: The mapping of opset domains to versions to serialize.
    """
    # Sort names for deterministic serialization
    for domain, version in from_.items():
        opset_ids.add(domain=domain, version=version)


def _serialize_metadata_props_into(
    string_string_entries: proto_containers.RepeatedCompositeFieldContainer[
        onnx.StringStringEntryProto
    ],
    from_: Mapping[str, str],
) -> None:
    """Serialize metadata properties into a repeated field of string-string entries.

    Args:
        string_string_entries: The repeated field to serialize into.
        from_: The mapping of metadata properties to serialize.
    """
    # Sort names for deterministic serialization
    for key in sorted(from_):
        string_string_entries.add(key=key, value=from_[key])


def serialize_graph(
    graph: _protocols.GraphProtocol | _protocols.GraphViewProtocol,
) -> onnx.GraphProto:
    graph_proto = onnx.GraphProto()
    serialize_graph_into(graph_proto, from_=graph)
    return graph_proto


def serialize_graph_into(
    graph_proto: onnx.GraphProto,
    from_: _protocols.GraphProtocol | _protocols.GraphViewProtocol,
) -> None:
    if from_.name:
        graph_proto.name = from_.name
    if from_.doc_string:
        graph_proto.doc_string = from_.doc_string
    for input_ in from_.inputs:
        serialize_value_into(graph_proto.input.add(), input_)
    # TODO(justinchuby): Support sparse_initializer
    for initializer in from_.initializers.values():
        serialize_tensor_into(graph_proto.initializer.add(), from_=initializer)
    for node in from_.nodes:
        serialize_node_into(graph_proto.node.add(), from_=node)
        for node_output in node.outputs:
            if not _should_create_value_info_for_value(node_output):
                # No need to serialize value info if it is not set
                continue
            if node_output.is_graph_output():
                # No need to serialize value info for these outputs because they are also graph outputs
                continue
            serialize_value_into(graph_proto.value_info.add(), node_output)
    for output in from_.outputs:
        serialize_value_into(graph_proto.output.add(), from_=output)
    if from_.metadata_props:
        _serialize_metadata_props_into(graph_proto.metadata_props, from_.metadata_props)


def serialize_function(
    function: _protocols.FunctionProtocol, *, create_value_info: bool = True
) -> onnx.FunctionProto:
    """Serialize an IR function as a FunctionProto.

    Args:
        function: The function to serialize.
        create_value_info: Whether to create ValueInfoProto for nodes in the function. This is supported
            starting from ONNX IR version 10.
    """
    function_proto = onnx.FunctionProto()
    serialize_function_into(
        function_proto, from_=function, create_value_info=create_value_info
    )
    return function_proto


def serialize_function_into(
    function_proto: onnx.FunctionProto,
    from_: _protocols.FunctionProtocol,
    *,
    create_value_info: bool = True,
) -> None:
    """Serialize an IR function into a FunctionProto.

    Args:
        function_proto: The proto to serialize into.
        from_: The function to serialize.
        create_value_info: Whether to create ValueInfoProto for nodes in the function. This is supported
            starting from ONNX IR version 10.
    """
    if from_.domain:
        function_proto.domain = from_.domain
    if from_.name:
        function_proto.name = from_.name
    if from_.overload:
        function_proto.overload = from_.overload
    if from_.doc_string:
        function_proto.doc_string = from_.doc_string
    if from_.opset_imports:
        # A valid ONNX graph should have at least one opset import, that is
        # the default ONNX opset.
        # Here we check for emptiness before serializing to keep the logic consistent
        _serialize_opset_imports_into(function_proto.opset_import, from_.opset_imports)
    if from_.metadata_props:
        _serialize_metadata_props_into(function_proto.metadata_props, from_.metadata_props)
    for input_ in from_.inputs:
        function_proto.input.append(input_.name)
        if not _should_create_value_info_for_value(input_):
            # No need to serialize value info if it is not set
            continue
        if not create_value_info:
            continue
        serialize_value_into(function_proto.value_info.add(), input_)
    for attr in from_.attributes.values():
        if attr.value is not None:
            serialize_attribute_into(function_proto.attribute_proto.add(), from_=attr)
        else:
            # ONNX does not record type information if the attribute does not have a default
            function_proto.attribute.append(attr.name)
    for func_output in from_.outputs:
        function_proto.output.append(func_output.name)
        # No need to serialize value info for function outputs because they are
        # also node outputs
    for node in from_.nodes:
        serialize_node_into(function_proto.node.add(), from_=node)
        # Record value info for outputs
        for node_output in node.outputs:
            if not _should_create_value_info_for_value(node_output):
                # No need to serialize value info if it is not set
                continue
            if not create_value_info:
                continue
            serialize_value_into(function_proto.value_info.add(), node_output)


def serialize_node(node: _protocols.NodeProtocol) -> onnx.NodeProto:
    node_proto = onnx.NodeProto()
    serialize_node_into(node_proto, from_=node)
    return node_proto


def serialize_node_into(node_proto: onnx.NodeProto, from_: _protocols.NodeProtocol) -> None:
    node_proto.op_type = from_.op_type
    if from_.domain:
        # If the domain is "", we can assume the default domain and not set it
        node_proto.domain = from_.domain
    if from_.name:
        node_proto.name = from_.name
    if from_.overload:
        node_proto.overload = from_.overload
    if from_.doc_string:
        node_proto.doc_string = from_.doc_string
    if from_.metadata_props:
        _serialize_metadata_props_into(node_proto.metadata_props, from_.metadata_props)
    for input_ in from_.inputs:
        if input_ is None:
            node_proto.input.append("")
        else:
            node_proto.input.append(input_.name)
    for output in from_.outputs:
        node_proto.output.append(output.name)
    for attr in from_.attributes.values():
        if isinstance(attr, _core.Attr):
            serialize_attribute_into(node_proto.attribute.add(), from_=attr)
        elif isinstance(attr, _core.RefAttr):
            serialize_reference_attribute_into(node_proto.attribute.add(), from_=attr)
        # Handle protocol attributes for completeness. We do not check them first because
        # calling isinstance on a protocol can be slow.
        # Most of the time, we will have Attr or RefAttr so the two branches below
        # will not be taken.
        elif isinstance(attr, _protocols.AttributeProtocol):
            serialize_attribute_into(node_proto.attribute.add(), from_=attr)
        elif isinstance(attr, _protocols.ReferenceAttributeProtocol):
            serialize_reference_attribute_into(node_proto.attribute.add(), from_=attr)
        else:
            raise TypeError(f"Unsupported attribute type: {type(attr)}")


def serialize_tensor(tensor: _protocols.TensorProtocol) -> onnx.TensorProto:
    tensor_proto = onnx.TensorProto()
    serialize_tensor_into(tensor_proto, from_=tensor)
    return tensor_proto


def serialize_tensor_into(
    tensor_proto: onnx.TensorProto, from_: _protocols.TensorProtocol
) -> None:
    if isinstance(from_, TensorProtoTensor):
        # Directly copy from the tensor proto if it is available
        tensor_proto.CopyFrom(from_.raw)
        return

    tensor_proto.name = from_.name
    if from_.doc_string:
        tensor_proto.doc_string = from_.doc_string
    tensor_proto.data_type = from_.dtype.value
    tensor_proto.dims.extend(from_.shape.numpy())
    if isinstance(from_, _core.ExternalTensor):
        # Store external tensors as is
        tensor_proto.data_location = onnx.TensorProto.EXTERNAL
        for k, v in {
            "location": os.fspath(from_.path),
            "offset": from_.offset,
            "length": from_.length,
        }.items():
            if v is not None:
                entry = tensor_proto.external_data.add()
                entry.key = k
                entry.value = str(v)
    else:
        tensor_proto.raw_data = from_.tobytes()


def serialize_attribute(attribute: _protocols.AttributeProtocol) -> onnx.AttributeProto:
    attribute_proto = onnx.AttributeProto()
    serialize_attribute_into(attribute_proto, from_=attribute)
    return attribute_proto


def serialize_attribute_into(
    attribute_proto: onnx.AttributeProto, from_: _protocols.AttributeProtocol
) -> None:
    attribute_proto.name = from_.name
    if from_.doc_string:
        attribute_proto.doc_string = from_.doc_string
    _fill_in_value_for_attribute(attribute_proto, from_.type, from_.value)


def _fill_in_value_for_attribute(
    attribute_proto: onnx.AttributeProto, type_: _enums.AttributeType, value: Any
) -> None:
    if type_ == _enums.AttributeType.INT:
        attribute_proto.i = value
        attribute_proto.type = onnx.AttributeProto.INT
    elif type_ == _enums.AttributeType.FLOAT:
        attribute_proto.f = value
        attribute_proto.type = onnx.AttributeProto.FLOAT
    elif type_ == _enums.AttributeType.STRING:
        attribute_proto.s = value.encode("utf-8")
        attribute_proto.type = onnx.AttributeProto.STRING
    elif type_ == _enums.AttributeType.INTS:
        attribute_proto.ints.extend(value)
        attribute_proto.type = onnx.AttributeProto.INTS
    elif type_ == _enums.AttributeType.FLOATS:
        attribute_proto.floats.extend(value)
        attribute_proto.type = onnx.AttributeProto.FLOATS
    elif type_ == _enums.AttributeType.STRINGS:
        attribute_proto.strings.extend([s.encode("utf-8") for s in value])
        attribute_proto.type = onnx.AttributeProto.STRINGS
    elif type_ == _enums.AttributeType.TENSOR:
        serialize_tensor_into(attribute_proto.t, value)
        attribute_proto.type = onnx.AttributeProto.TENSOR
    elif type_ == _enums.AttributeType.GRAPH:
        serialize_graph_into(attribute_proto.g, value)
        attribute_proto.type = onnx.AttributeProto.GRAPH
    elif type_ == _enums.AttributeType.TENSORS:
        for tensor in value:
            serialize_tensor_into(attribute_proto.tensors.add(), tensor)
        attribute_proto.type = onnx.AttributeProto.TENSORS
    elif type_ == _enums.AttributeType.GRAPHS:
        for graph in value:
            serialize_graph_into(attribute_proto.graphs.add(), graph)
        attribute_proto.type = onnx.AttributeProto.GRAPHS
    else:
        raise TypeError(f"Unsupported attribute type: {type_}")


def serialize_reference_attribute_into(
    attribute_proto: onnx.AttributeProto, from_: _protocols.ReferenceAttributeProtocol
) -> None:
    attribute_proto.name = from_.name
    attribute_proto.ref_attr_name = from_.ref_attr_name
    if from_.doc_string:
        attribute_proto.doc_string = from_.doc_string
    attribute_proto.type = typing.cast(onnx.AttributeProto.AttributeType, from_.type.value)


def serialize_value(value: _protocols.ValueProtocol, *, name: str = "") -> onnx.ValueInfoProto:
    """Serialize a value into a ValueInfoProto.

    Args:
        value: The proto to serialize into.
        from_: The value to serialize.
        name: A custom name to set for the value info. If not provided, the name from the value will be used.
    """
    value_info_proto = onnx.ValueInfoProto()
    serialize_value_into(value_info_proto, value, name=name)
    return value_info_proto


def serialize_value_into(
    value_info_proto: onnx.ValueInfoProto,
    from_: _protocols.ValueProtocol,
    *,
    name: str = "",
) -> None:
    """Serialize a value into a ValueInfoProto.

    Args:
        value_info_proto: The proto to serialize into.
        from_: The value to serialize.
        name: A custom name to set for the value info. If not provided, the name from the value will be used.
    """
    if name:
        value_info_proto.name = name
    else:
        value_info_proto.name = from_.name
    if from_.metadata_props:
        _serialize_metadata_props_into(value_info_proto.metadata_props, from_.metadata_props)
    if from_.shape is not None:
        serialize_shape_into(value_info_proto.type, from_.shape)
    if from_.type is not None:
        serialize_type_into(value_info_proto.type, from_.type)


def serialize_type_into(type_proto: onnx.TypeProto, from_: _protocols.TypeProtocol) -> None:
    if from_.denotation:
        type_proto.denotation = from_.denotation
    if isinstance(from_, _core.TensorType):
        tensor_type_proto = type_proto.tensor_type
        tensor_type_proto.elem_type = from_.dtype.value
    elif isinstance(from_, _core.SparseTensorType):
        sparse_tensor_type_proto = type_proto.sparse_tensor_type
        sparse_tensor_type_proto.elem_type = from_.dtype.value
    elif isinstance(from_, _core.SequenceType):
        sequence_type_proto = type_proto.sequence_type
        serialize_type_into(sequence_type_proto.elem_type, from_.elem_type)
    elif isinstance(from_, _core.OptionalType):
        optional_type_proto = type_proto.optional_type
        serialize_type_into(optional_type_proto.elem_type, from_.elem_type)
    else:
        raise TypeError(f"Unsupported type: {from_}")


def serialize_shape_into(type_proto: onnx.TypeProto, from_: _protocols.ShapeProtocol) -> None:
    tensor_type_proto = type_proto.tensor_type
    # When from is empty, we still need to set the shape field to an empty list by touching it
    tensor_type_proto.shape.ClearField("dim")
    for dim in from_:
        serialize_dimension_into(tensor_type_proto.shape.dim.add(), from_=dim)


def serialize_dimension_into(
    dim_proto: onnx.TensorShapeProto.Dimension, from_: _protocols.DimensionProtocol
) -> None:
    value = from_.value
    if from_.denotation:
        dim_proto.denotation = from_.denotation
    if isinstance(value, int):
        dim_proto.dim_value = value
    elif isinstance(value, str):
        dim_proto.dim_param = value
