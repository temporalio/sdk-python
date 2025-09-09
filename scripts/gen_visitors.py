import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from google.protobuf.descriptor import Descriptor, FieldDescriptor

from temporalio.api.common.v1.message_pb2 import Payload, Payloads, SearchAttributes
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    WorkflowActivation,
)
from temporalio.bridge.proto.workflow_completion.workflow_completion_pb2 import (
    WorkflowActivationCompletion,
)

base_dir = Path(__file__).parent.parent


def name_for(desc: Descriptor) -> str:
    # Use fully-qualified name to avoid collisions; replace dots with underscores
    return desc.full_name.replace(".", "_")


def emit_loop(
    field_name: str,
    iter_expr: str,
    child_method: str,
) -> str:
    # Helper to emit a for-loop over a collection with optional headers guard
    if field_name == "headers":
        return f"""\
        if not self.skip_headers:
            for v in {iter_expr}:
                await self._visit_{child_method}(fs, v)"""
    else:
        return f"""\
        for v in {iter_expr}:
            await self._visit_{child_method}(fs, v)"""


def emit_singular(
    field_name: str, access_expr: str, child_method: str, check_presence: bool
) -> str:
    # Helper to emit a singular field visit with presence check and optional headers guard
    if check_presence:
        if field_name == "headers":
            return f"""\
        if not self.skip_headers:
            if o.HasField("{field_name}"):
                await self._visit_{child_method}(fs, {access_expr})"""
        else:
            return f"""\
        if o.HasField("{field_name}"):
            await self._visit_{child_method}(fs, {access_expr})"""
    else:
        if field_name == "headers":
            return f"""\
        if not self.skip_headers:
            await self._visit_{child_method}(fs, {access_expr})"""
        else:
            return f"""\
        await self._visit_{child_method}(fs, {access_expr})"""


class VisitorGenerator:
    def __init__(self):
        # Track which message descriptors have visitor methods generated
        self.generated: dict[str, bool] = {
            Payload.DESCRIPTOR.full_name: True,
            Payloads.DESCRIPTOR.full_name: True,
        }
        self.in_progress: set[str] = set()
        self.methods: list[str] = [
            """    async def _visit_temporal_api_common_v1_Payload(self, fs, o):
            await fs.visit_payload(o)
    """,
            """    async def _visit_temporal_api_common_v1_Payloads(self, fs, o):
            await fs.visit_payloads(o.payloads)
    """,
            """    async def _visit_payload_container(self, fs, o):
            await fs.visit_payloads(o)
    """,
        ]

    def check_repeated(self, child_desc, field, iter_expr) -> Optional[str]:
        # Special case for repeated payloads, handle them directly
        if child_desc.full_name == Payload.DESCRIPTOR.full_name:
            return emit_singular(field.name, iter_expr, "payload_container", False)
        else:
            child_needed = self.walk(child_desc)
            if child_needed:
                return emit_loop(
                    field.name,
                    iter_expr,
                    name_for(child_desc),
                )
            else:
                return None

    def walk(self, desc: Descriptor) -> bool:
        key = desc.full_name
        if key in self.generated:
            return self.generated[key]
        if key in self.in_progress:
            # Break cycles; if another path proves this node needed, we'll revisit
            return False

        needed = False
        self.in_progress.add(key)
        lines: list[str] = [f"    async def _visit_{name_for(desc)}(self, fs, o):"]
        # If this is the SearchAttributes message, allow skipping
        if desc.full_name == SearchAttributes.DESCRIPTOR.full_name:
            lines.append("        if self.skip_search_attributes:")
            lines.append("            return")

        # Group fields by oneof to generate if/elif chains
        oneof_fields: dict[int, list[FieldDescriptor]] = {}
        regular_fields: list[FieldDescriptor] = []

        for field in desc.fields:
            if field.type != FieldDescriptor.TYPE_MESSAGE:
                continue

            # Skip synthetic oneofs (proto3 optional fields)
            if field.containing_oneof is not None:
                oneof_idx = field.containing_oneof.index
                if oneof_idx not in oneof_fields:
                    oneof_fields[oneof_idx] = []
                oneof_fields[oneof_idx].append(field)
            else:
                regular_fields.append(field)

        # Process regular fields first
        for field in regular_fields:
            # Repeated fields (including maps which are represented as repeated messages)
            if field.label == FieldDescriptor.LABEL_REPEATED:
                if (
                    field.message_type is not None
                    and field.message_type.GetOptions().map_entry
                ):
                    val_fd = field.message_type.fields_by_name.get("value")
                    if (
                        val_fd is not None
                        and val_fd.type == FieldDescriptor.TYPE_MESSAGE
                    ):
                        child_desc = val_fd.message_type
                        child_needed = self.walk(child_desc)
                        if child_needed:
                            needed = True
                            lines.append(
                                emit_loop(
                                    field.name,
                                    f"o.{field.name}.values()",
                                    name_for(child_desc),
                                )
                            )

                    key_fd = field.message_type.fields_by_name.get("key")
                    if (
                        key_fd is not None
                        and key_fd.type == FieldDescriptor.TYPE_MESSAGE
                    ):
                        child_desc = key_fd.message_type
                        child_needed = self.walk(child_desc)
                        if child_needed:
                            needed = True
                            lines.append(
                                emit_loop(
                                    field.name,
                                    f"o.{field.name}.keys()",
                                    name_for(child_desc),
                                )
                            )
                else:
                    child = self.check_repeated(
                        field.message_type, field, f"o.{field.name}"
                    )
                    if child is not None:
                        needed = True
                        lines.append(child)
            else:
                child_desc = field.message_type
                child_needed = self.walk(child_desc)
                needed |= child_needed
                if child_needed:
                    lines.append(
                        emit_singular(
                            field.name, f"o.{field.name}", name_for(child_desc), True
                        )
                    )

        # Process oneof fields as if/elif chains
        for oneof_idx, fields in oneof_fields.items():
            oneof_lines = []
            first = True
            for field in fields:
                child_desc = field.message_type
                child_needed = self.walk(child_desc)
                needed |= child_needed
                if child_needed:
                    if_word = "if" if first else "elif"
                    first = False
                    line = emit_singular(
                        field.name, f"o.{field.name}", name_for(child_desc), True
                    ).replace("        if", f"        {if_word}", 1)
                    oneof_lines.append(line)
            if oneof_lines:
                lines.extend(oneof_lines)

        self.generated[key] = needed
        self.in_progress.discard(key)
        if needed:
            self.methods.append("\n".join(lines) + "\n")
        return needed

    def generate(self, roots: list[Descriptor]) -> str:
        """
        Generate Python source code that, given a function f(Payload) -> Payload,
        applies it to every Payload contained within a WorkflowActivation tree.

        The generated code defines async visitor functions for each reachable
        protobuf message type starting from WorkflowActivation, including support
        for repeated fields and map entries, and a convenience entrypoint
        function `visit`.
        """

        # We avoid importing google.api deps in service protos; expand by walking from
        # WorkflowActivationCompletion root which references many command messages.
        for r in roots:
            self.walk(r)

        header = """
import abc
from typing import Any, MutableSequence

from temporalio.api.common.v1.message_pb2 import Payload

class VisitorFunctions(abc.ABC):
    \"\"\"Set of functions which can be called by the visitor. Allows handling payloads as a sequence.\"\"\"
    @abc.abstractmethod
    async def visit_payload(self, payload: Payload) -> None:
        \"\"\"Called when encountering a single payload.\"\"\"
        raise NotImplementedError()

    @abc.abstractmethod
    async def visit_payloads(self, payloads: MutableSequence[Payload]) -> None:
        \"\"\"Called when encountering multiple payloads together.\"\"\"
        raise NotImplementedError()

class PayloadVisitor:
    \"\"\"A visitor for payloads. Applies a function to every payload in a tree of messages.\"\"\"
    def __init__(
        self, *, skip_search_attributes: bool = False, skip_headers: bool = False
    ):
        \"\"\"Creates a new payload visitor.\"\"\"
        self.skip_search_attributes = skip_search_attributes
        self.skip_headers = skip_headers

    async def visit(
        self, fs: VisitorFunctions, root: Any
    ) -> None:
        \"\"\"Visits the given root message with the given function.\"\"\"
        method_name = "_visit_" + root.DESCRIPTOR.full_name.replace(".", "_")
        method = getattr(self, method_name, None)
        if method is not None:
            await method(fs, root)
        else:
            raise ValueError(f"Unknown root message type: {root.DESCRIPTOR.full_name}")

"""

        return header + "\n".join(self.methods)


def write_generated_visitors_into_visitor_generated_py() -> None:
    """Write the generated visitor code into _visitor.py."""
    out_path = base_dir / "temporalio" / "bridge" / "_visitor.py"

    # Build root descriptors: WorkflowActivation, WorkflowActivationCompletion,
    # and all messages from selected API modules
    roots: list[Descriptor] = [
        WorkflowActivation.DESCRIPTOR,
        WorkflowActivationCompletion.DESCRIPTOR,
    ]

    code = VisitorGenerator().generate(roots)
    out_path.write_text(code)


if __name__ == "__main__":
    print("Generating temporalio/bridge/_visitor.py...", file=sys.stderr)
    write_generated_visitors_into_visitor_generated_py()
    subprocess.run(["uv", "run", "ruff", "format", "temporalio/bridge/_visitor.py"])
