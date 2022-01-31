from dataclasses import dataclass

import temporalio.api.common.v1
import temporalio.converter


async def test_default():
    async def assert_payload(
        input, expected_encoding, expected_data, *, expected_decoded_input=None
    ):
        payload = await temporalio.converter.default().encode(input)
        # Only check none if that's the expected encoding
        if expected_encoding is None:
            assert payload is None
            return
        # Check encoding and data
        if isinstance(expected_encoding, str):
            expected_encoding = expected_encoding.encode()
        assert payload.metadata["encoding"] == expected_encoding
        if isinstance(expected_data, str):
            expected_data = expected_data.encode()
        assert payload.data == expected_data
        # Decode and check
        actual_input, ok = await temporalio.converter.default().decode(payload)
        assert ok
        if expected_decoded_input is None:
            expected_decoded_input = input
        assert actual_input == expected_decoded_input
        return payload

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

    class NonSerializableClass:
        pass

    await assert_payload(NonSerializableClass(), None, None)

    @dataclass
    class MyDataClass:
        foo: str
        bar: int

    await assert_payload(
        MyDataClass(foo="somestr", bar=123),
        "json/plain",
        '{"bar":123,"foo":"somestr"}',
        expected_decoded_input={"foo": "somestr", "bar": 123},
    )


async def test_binary_proto():
    # We have to test this separately because by default it never encodes
    # anything
    conv = temporalio.converter.BinaryProtoPayloadConverter()
    proto = temporalio.api.common.v1.WorkflowExecution(workflow_id="id1", run_id="id2")
    payload = await conv.encode(proto)
    assert payload.metadata["encoding"] == b"binary/protobuf"
    assert (
        payload.metadata["messageType"] == b"temporal.api.common.v1.WorkflowExecution"
    )
    assert payload.data == proto.SerializeToString()
    decoded, ok = await conv.decode(payload)
    assert ok
    assert decoded == proto


async def test_multiple():
    payloads = await temporalio.converter.default().encode_multiple(
        [{"foo": "bar"}, {"baz": "qux"}]
    )
    assert len(payloads.payloads) == 2
    assert payloads.payloads[0].metadata["encoding"] == b"json/plain"
    assert payloads.payloads[0].data == b'{"foo":"bar"}'
    assert payloads.payloads[1].metadata["encoding"] == b"json/plain"
    assert payloads.payloads[1].data == b'{"baz":"qux"}'
    values, ok = await temporalio.converter.default().decode_multiple(payloads)
    assert ok
    assert len(values) == 2
    assert values[0] == {"foo": "bar"}
    assert values[1] == {"baz": "qux"}
