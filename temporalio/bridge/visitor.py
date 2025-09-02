from typing import Awaitable, Callable, Any

from collections.abc import Mapping as AbcMapping, Sequence as AbcSequence

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from temporalio.api.common.v1.message_pb2 import Payload


async def visit_payloads(
    f: Callable[[Payload], Awaitable[Payload]], root: Any
) -> None:
    print("Visiting object: ", type(root))
    if isinstance(root, Payload):
        print("Applying to payload: ", root)
        root.CopyFrom(await f(root))
        print("Applied to payload: ", root)
    elif isinstance(root, AbcMapping):
        for k, v in root.items():
            await visit_payloads(f, k)
            await visit_payloads(f, v)
    elif isinstance(root, AbcSequence) and not isinstance(
        root, (bytes, bytearray, str)
    ):
        for o in root:
            await visit_payloads(f, o)
    elif isinstance(root, Message):
        await visit_message(f, root)


async def visit_message(
    f: Callable[[Payload], Awaitable[Payload]], root: Message
) -> None:
    print("Visiting Message: ", type(root))
    for field in root.DESCRIPTOR.fields:
        print("Evaluating Field: ", field.name)

        # Repeated fields (including maps which are represented as repeated messages)
        if field.label == FieldDescriptor.LABEL_REPEATED:
            value = getattr(root, field.name)
            if field.message_type is not None and field.message_type.GetOptions().map_entry:
                for k, v in value.items():
                    await visit_payloads(f, k)
                    await visit_payloads(f, v)
            else:
                for item in value:
                    await visit_payloads(f, item)
        else:
            # Only descend into singular message fields if present
            if field.type == FieldDescriptor.TYPE_MESSAGE and root.HasField(field.name):
                value = getattr(root, field.name)
                await visit_payloads(f, value)
