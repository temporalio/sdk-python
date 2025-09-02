import sys
from pathlib import Path

from google.protobuf.descriptor import Descriptor, FieldDescriptor

from temporalio.api.common.v1.message_pb2 import Payload, Payloads, SearchAttributes
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    WorkflowActivation,
)
from temporalio.bridge.proto.workflow_completion.workflow_completion_pb2 import (
    WorkflowActivationCompletion,
)

base_dir = Path(__file__).parent.parent


def gen_workflow_activation_payload_visitor_code() -> str:
    """
    Generate Python source code that, given a function f(Payload) -> Payload,
    applies it to every Payload contained within a WorkflowActivation tree.

    The generated code defines async visitor functions for each reachable
    protobuf message type starting from WorkflowActivation, including support
    for repeated fields and map entries, and a convenience entrypoint
    function `visit_workflow_activation_payloads`.
    """

    def name_for(desc: Descriptor) -> str:
        # Use fully-qualified name to avoid collisions; replace dots with underscores
        return desc.full_name.replace(".", "_")

    def emit_loop(
        lines: list[str],
        field_name: str,
        iter_expr: str,
        var_name: str,
        child_method: str,
    ) -> None:
        # Helper to emit a for-loop over a collection with optional headers guard
        if field_name == "headers":
            lines.append("        if not self.skip_headers:")
            lines.append(f"            for {var_name} in {iter_expr}:")
            lines.append(
                f"                await self.visit_{child_method}(f, {var_name})"
            )
        else:
            lines.append(f"        for {var_name} in {iter_expr}:")
            lines.append(f"            await self.visit_{child_method}(f, {var_name})")

    def emit_singular(
        lines: list[str], field_name: str, access_expr: str, child_method: str
    ) -> None:
        # Helper to emit a singular field visit with presence check and optional headers guard
        if field_name == "headers":
            lines.append("        if not self.skip_headers:")
            lines.append(f"            if o.HasField('{field_name}'):")
            lines.append(
                f"                await self.visit_{child_method}(f, {access_expr})"
            )
        else:
            lines.append(f"        if o.HasField('{field_name}'):")
            lines.append(
                f"            await self.visit_{child_method}(f, {access_expr})"
            )

    # Track which message descriptors have visitor methods generated
    generated: dict[str, bool] = {}
    in_progress: set[str] = set()
    methods: list[str] = []

    def walk(desc: Descriptor) -> bool:
        key = desc.full_name
        if key in generated:
            return generated[key]
        if key in in_progress:
            # Break cycles; if another path proves this node needed, we'll revisit
            return False

        if desc.full_name == Payload.DESCRIPTOR.full_name:
            generated[key] = True
            methods.append(
                """    async def visit_temporal_api_common_v1_Payload(self, f, o):
        o.CopyFrom(await f(o))
"""
            )
            return True

        needed = False
        in_progress.add(key)
        lines: list[str] = [f"    async def visit_{name_for(desc)}(self, f, o):"]
        # If this is the SearchAttributes message, allow skipping
        if desc.full_name == SearchAttributes.DESCRIPTOR.full_name:
            lines.append("        if self.skip_search_attributes:")
            lines.append("            return")

        for field in desc.fields:
            if field.type != FieldDescriptor.TYPE_MESSAGE:
                continue

            # Repeated fields (including maps which are represented as repeated messages)
            if field.label == FieldDescriptor.LABEL_REPEATED:
                if (
                    field.message_type is not None
                    and field.message_type.GetOptions().map_entry
                ):
                    entry_desc = field.message_type
                    key_fd = entry_desc.fields_by_name.get("key")
                    val_fd = entry_desc.fields_by_name.get("value")

                    if (
                        val_fd is not None
                        and val_fd.type == FieldDescriptor.TYPE_MESSAGE
                    ):
                        child_desc = val_fd.message_type
                        child_needed = walk(child_desc)
                        needed |= child_needed
                        if child_needed:
                            emit_loop(
                                lines,
                                field.name,
                                f"o.{field.name}.values()",
                                "v",
                                name_for(child_desc),
                            )

                    if (
                        key_fd is not None
                        and key_fd.type == FieldDescriptor.TYPE_MESSAGE
                    ):
                        key_desc = key_fd.message_type
                        child_needed = walk(key_desc)
                        needed |= child_needed
                        if child_needed:
                            emit_loop(
                                lines,
                                field.name,
                                f"o.{field.name}.keys()",
                                "k",
                                name_for(key_desc),
                            )
                else:
                    child_desc = field.message_type
                    child_needed = walk(child_desc)
                    needed |= child_needed
                    if child_needed:
                        emit_loop(
                            lines,
                            field.name,
                            f"o.{field.name}",
                            "v",
                            name_for(child_desc),
                        )
            else:
                child_desc = field.message_type
                child_needed = walk(child_desc)
                needed |= child_needed
                if child_needed:
                    emit_singular(
                        lines, field.name, f"o.{field.name}", name_for(child_desc)
                    )

        generated[key] = needed
        in_progress.discard(key)
        if needed:
            methods.append("\n".join(lines) + "\n")
        return needed

    # Build root descriptors: WorkflowActivation, WorkflowActivationCompletion,
    # and all messages from selected API modules
    roots: list[Descriptor] = [
        WorkflowActivation.DESCRIPTOR,
        WorkflowActivationCompletion.DESCRIPTOR,
    ]

    # We avoid importing google.api deps in service protos; expand by walking from
    # WorkflowActivationCompletion root which references many command messages.

    for r in roots:
        walk(r)

    header = (
        "from typing import Any, Awaitable, Callable\n\n"
        "from temporalio.api.common.v1.message_pb2 import Payload\n\n\n"
        "class PayloadVisitor:\n"
        "    def __init__(self, *, skip_search_attributes: bool = False, skip_headers: bool = False):\n"
        "        self.skip_search_attributes = skip_search_attributes\n"
        "        self.skip_headers = skip_headers\n\n"
        "    async def visit(self, f: Callable[[Payload], Awaitable[Payload]], root: Any) -> None:\n"
        "        method_name = 'visit_' + root.DESCRIPTOR.full_name.replace('.', '_')\n"
        "        method = getattr(self, method_name, None)\n"
        "        if method is not None:\n"
        "            await method(f, root)\n\n"
    )

    return header + "\n".join(methods)


def write_generated_visitors_into_visitor_generated_py() -> None:
    """Write the generated visitor code into visitor_generated.py."""
    out_path = base_dir / "temporalio" / "bridge" / "visitor_generated.py"
    code = gen_workflow_activation_payload_visitor_code()
    out_path.write_text(code)


if __name__ == "__main__":
    print("Generating temporalio/bridge/visitor_generated.py...", file=sys.stderr)
    write_generated_visitors_into_visitor_generated_py()
