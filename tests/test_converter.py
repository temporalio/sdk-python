from dataclasses import dataclass

import pytest

import temporalio.api.common.v1
import temporalio.converter
from temporalio.api.common.v1 import Payload as AnotherNameForPayload


async def test_default():
    async def assert_payload(
        input,
        expected_encoding,
        expected_data,
        *,
        expected_decoded_input=None,
        type_hint=None
    ):
        payloads = await temporalio.converter.default().encode([input])
        # Check encoding and data
        assert len(payloads) == 1
        if isinstance(expected_encoding, str):
            expected_encoding = expected_encoding.encode()
        assert payloads[0].metadata["encoding"] == expected_encoding
        if isinstance(expected_data, str):
            expected_data = expected_data.encode()
        assert payloads[0].data == expected_data
        # Decode and check
        actual_inputs = await temporalio.converter.default().decode(
            payloads, [type_hint]
        )
        assert len(actual_inputs) == 1
        if expected_decoded_input is None:
            expected_decoded_input = input
        assert actual_inputs[0] == expected_decoded_input
        return payloads[0]

    # Basic types
    await assert_payload(None, "binary/null", "")
    await assert_payload(b"some binary", "binary/plain", "some binary")
    payload = await assert_payload(
        temporalio.api.common.v1.WorkflowExecution(workflow_id="id1", run_id="id2"),
        "json/protobuf",
        '{"runId":"id2","workflowId":"id1"}',
    )
    assert (
        payload.metadata["messageType"] == b"temporal.api.common.v1.WorkflowExecution"
    )
    await assert_payload(
        {"foo": "bar", "baz": "qux"}, "json/plain", '{"baz":"qux","foo":"bar"}'
    )
    await assert_payload("somestr", "json/plain", '"somestr"')
    await assert_payload(1234, "json/plain", "1234")
    await assert_payload(12.34, "json/plain", "12.34")
    await assert_payload(True, "json/plain", "true")
    await assert_payload(False, "json/plain", "false")

    # Unknown type
    with pytest.raises(RuntimeError) as excinfo:

        class NonSerializableClass:
            pass

        await assert_payload(NonSerializableClass(), None, None)
    assert "has no known converter" in str(excinfo.value)

    @dataclass
    class MyDataClass:
        foo: str
        bar: int

    # Data class without type hint is just dict
    await assert_payload(
        MyDataClass(foo="somestr", bar=123),
        "json/plain",
        '{"bar":123,"foo":"somestr"}',
        expected_decoded_input={"foo": "somestr", "bar": 123},
    )

    # Data class with type hint reconstructs the class
    await assert_payload(
        MyDataClass(foo="somestr", bar=123),
        "json/plain",
        '{"bar":123,"foo":"somestr"}',
        type_hint=MyDataClass,
    )


async def test_binary_proto():
    # We have to test this separately because by default it never encodes
    # anything since JSON proto takes precedence
    conv = temporalio.converter.BinaryProtoPayloadConverter()
    proto = temporalio.api.common.v1.WorkflowExecution(workflow_id="id1", run_id="id2")
    payload = await conv.encode(proto)
    assert payload.metadata["encoding"] == b"binary/protobuf"
    assert (
        payload.metadata["messageType"] == b"temporal.api.common.v1.WorkflowExecution"
    )
    assert payload.data == proto.SerializeToString()
    decoded = await conv.decode(payload)
    assert decoded == proto
