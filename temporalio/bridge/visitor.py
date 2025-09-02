from collections.abc import Mapping as AbcMapping
from collections.abc import Sequence as AbcSequence
from typing import Any, Awaitable, Callable

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from temporalio.api.common.v1.message_pb2 import Payload, SearchAttributes


class PayloadVisitor:
    def __init__(
        self, *, skip_search_attributes: bool = False, skip_headers: bool = False
    ):
        self.skip_search_attributes = skip_search_attributes
        self.skip_headers = skip_headers

    async def visit_payloads(
        self, f: Callable[[Payload], Awaitable[Payload]], root: Any
    ) -> None:
        if self.skip_search_attributes and isinstance(root, SearchAttributes):
            return

        if isinstance(root, Payload):
            root.CopyFrom(await f(root))
        elif isinstance(root, AbcMapping):
            for k, v in root.items():
                await self.visit_payloads(f, k)
                await self.visit_payloads(f, v)
        elif isinstance(root, AbcSequence) and not isinstance(
            root, (bytes, bytearray, str)
        ):
            for o in root:
                await self.visit_payloads(f, o)
        elif isinstance(root, Message):
            await self.visit_message(
                f,
                root,
            )

    async def visit_message(
        self, f: Callable[[Payload], Awaitable[Payload]], root: Message
    ) -> None:
        for field in root.DESCRIPTOR.fields:
            if self.skip_headers and field.name == "headers":
                continue

            # Repeated fields (including maps which are represented as repeated messages)
            if field.label == FieldDescriptor.LABEL_REPEATED:
                value = getattr(root, field.name)
                if (
                    field.message_type is not None
                    and field.message_type.GetOptions().map_entry
                ):
                    for k, v in value.items():
                        await self.visit_payloads(f, k)
                        await self.visit_payloads(f, v)
                else:
                    for item in value:
                        await self.visit_payloads(f, item)
            else:
                # Only descend into singular message fields if present
                if field.type == FieldDescriptor.TYPE_MESSAGE and root.HasField(
                    field.name
                ):
                    value = getattr(root, field.name)
                    await self.visit_payloads(f, value)
