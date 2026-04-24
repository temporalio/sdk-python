import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import cast

import google.protobuf.message
from google.protobuf.descriptor import Descriptor, FieldDescriptor

from temporalio.api.common.v1.message_pb2 import Payload, Payloads, SearchAttributes
from temporalio.api.workflowservice.v1.request_response_pb2 import (
    SignalWithStartWorkflowExecutionRequest,
    SignalWithStartWorkflowExecutionResponse,
)
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    WorkflowActivation,
)
from temporalio.bridge.proto.workflow_completion.workflow_completion_pb2 import (
    WorkflowActivationCompletion,
)

base_dir = Path(__file__).parent.parent
BASIC_IMPORTED_TYPES = {
    Payload.DESCRIPTOR.full_name: "Payload",
    Payloads.DESCRIPTOR.full_name: "Payloads",
    SearchAttributes.DESCRIPTOR.full_name: "SearchAttributes",
}


def normalize_python_module(module: str) -> str:
    if module.startswith("temporal.sdk.core."):
        return "temporalio.bridge.proto." + module[len("temporal.sdk.core.") :]
    return module


def nested_python_name(desc: Descriptor) -> str:
    names = [desc.name]
    current = desc.containing_type
    while current is not None:
        names.append(current.name)
        current = current.containing_type
    return ".".join(reversed(names))


def name_for(desc: Descriptor) -> str:
    return desc.full_name.replace(".", "_")


def python_type_for(desc: Descriptor) -> str:
    module = desc.file.package
    if module.startswith("temporal.api."):
        module = "temporalio.api." + module[len("temporal.api.") :]
        return f"{module}.{desc.name}"
    return "object"


def load_generated_system_module() -> ModuleType:
    module_path = (
        base_dir / "temporalio" / "nexus" / "system" / "_workflow_service_generated.py"
    )
    module_name = "temporalio_nexus_system_generated"
    spec = spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load generated system module from {module_path}")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_system_nexus_roots() -> list[Descriptor]:
    module = load_generated_system_module()
    roots: list[Descriptor] = []
    for operation in getattr(module, "__nexus_operation_registry__", {}).values():
        for proto_type in (operation.input_type, operation.output_type):
            if (
                isinstance(proto_type, type)
                and issubclass(proto_type, google.protobuf.message.Message)
                and proto_type.DESCRIPTOR is not None
            ):
                roots.append(cast(Descriptor, proto_type.DESCRIPTOR))
    deduped: list[Descriptor] = []
    seen: set[str] = set()
    for root in roots:
        if root.full_name not in seen:
            seen.add(root.full_name)
            deduped.append(root)
    return deduped


def emit_loop(field_name: str, iter_expr: str, child_method: str) -> str:
    if field_name == "headers":
        return f"""\
        if not self.skip_headers:
            for v in {iter_expr}:
                await self._visit_{child_method}(fs, v)"""
    if field_name == "search_attributes":
        return f"""\
        if not self.skip_search_attributes:
            for v in {iter_expr}:
                await self._visit_{child_method}(fs, v)"""
    return f"""\
        for v in {iter_expr}:
            await self._visit_{child_method}(fs, v)"""


def emit_singular(
    field_name: str, access_expr: str, child_method: str, presence_word: str | None
) -> str:
    if presence_word:
        if field_name == "headers":
            return f"""\
        if not self.skip_headers:
            {presence_word} o.HasField("{field_name}"):
                await self._visit_{child_method}(fs, {access_expr})"""
        return f"""\
        {presence_word} o.HasField("{field_name}"):
            await self._visit_{child_method}(fs, {access_expr})"""
    if field_name == "headers":
        return f"""\
        if not self.skip_headers:
            await self._visit_{child_method}(fs, {access_expr})"""
    return f"""\
        await self._visit_{child_method}(fs, {access_expr})"""


class PayloadVisitorGenerator:
    def __init__(self) -> None:
        self.generated: dict[str, bool] = {
            Payload.DESCRIPTOR.full_name: True,
            Payloads.DESCRIPTOR.full_name: True,
        }
        self.in_progress: set[str] = set()
        self.root_type_imports: dict[str, tuple[str, str]] = {}
        self.type_checking_modules: set[str] = set()
        self.methods: list[str] = [
            """\
    async def _visit_temporal_api_common_v1_Payload(
        self, fs: VisitorFunctions, o: Payload
    ):
        await fs.visit_payload(o)
    """,
            """\
    async def _visit_temporal_api_common_v1_Payloads(
        self, fs: VisitorFunctions, o: Payloads
    ):
        await fs.visit_payloads(o.payloads)
    """,
            """\
    async def _visit_payload_container(
        self, fs: VisitorFunctions, o: PayloadSequence
    ):
        await fs.visit_payloads(o)
    """,
        ]

    def generate(self, roots: list[Descriptor]) -> str:
        for root in roots:
            self.walk(root)

        extra_imports = "\n".join(
            f"from {module} import {class_name}"
            for class_name, module in sorted(
                set(self.root_type_imports.values()),
                key=lambda item: (item[1], item[0]),
            )
        )
        type_checking_imports = "\n".join(
            f"    import {module}" for module in sorted(self.type_checking_modules)
        )
        if extra_imports:
            extra_imports += "\n"
        if type_checking_imports:
            type_checking_imports = (
                "\nif TYPE_CHECKING:\n" + type_checking_imports + "\n"
            )

        header = """
from __future__ import annotations

# This file is generated by gen_payload_visitor.py. Changes should be made there.
from typing import TYPE_CHECKING
from google.protobuf.message import Message

import temporalio.nexus.system
from temporalio.api.common.v1.message_pb2 import Payload, Payloads, SearchAttributes
from temporalio.bridge._visitor_functions import PayloadSequence, VisitorFunctions
"""
        header += extra_imports
        header += type_checking_imports
        header += """

class PayloadVisitor:
    \"\"\"A visitor for payloads.
    Applies a function to every payload in a tree of messages.
    \"\"\"

    def __init__(
        self, *, skip_search_attributes: bool = False, skip_headers: bool = False
    ):
        \"\"\"Create a new payload visitor.\"\"\"
        self.skip_search_attributes = skip_search_attributes
        self.skip_headers = skip_headers

    async def visit(self, fs: VisitorFunctions, root: Message) -> None:
        \"\"\"Visit the given root message with the given function set.\"\"\"
        method_name = "_visit_" + root.DESCRIPTOR.full_name.replace(".", "_")
        method = getattr(self, method_name, None)
        if method is not None:
            await method(fs, root)
        else:
            raise ValueError(f"Unknown root message type: {root.DESCRIPTOR.full_name}")

    async def _visit_system_nexus_payload(
        self,
        fs: VisitorFunctions,
        service: str,
        operation: str,
        payload: Payload,
    ) -> None:
        new_payload = await temporalio.nexus.system.visit_payload(
            service,
            operation,
            payload,
            fs,
            self.skip_search_attributes,
        )
        if new_payload is None:
            await self._visit_temporal_api_common_v1_Payload(fs, payload)
            return

        if new_payload is not payload:
            payload.CopyFrom(new_payload)
        await fs.visit_system_nexus_envelope(payload)

"""
        return header + "\n".join(self.methods)

    def python_type_for_descriptor(self, desc: Descriptor) -> str:
        basic_type = BASIC_IMPORTED_TYPES.get(desc.full_name)
        if basic_type is not None:
            return basic_type
        cls = getattr(desc, "_concrete_class", None)
        if cls is None:
            return "Message"
        module = normalize_python_module(cls.__module__)
        if desc in (
            WorkflowActivation.DESCRIPTOR,
            WorkflowActivationCompletion.DESCRIPTOR,
            SignalWithStartWorkflowExecutionRequest.DESCRIPTOR,
            SignalWithStartWorkflowExecutionResponse.DESCRIPTOR,
        ):
            self.root_type_imports[desc.full_name] = (cls.__name__, module)
            return cls.__name__
        self.type_checking_modules.add(module)
        return f"{module}.{nested_python_name(desc)}"

    def check_repeated(
        self, child_desc: Descriptor, field: FieldDescriptor, iter_expr: str
    ) -> str | None:
        if child_desc.full_name == Payload.DESCRIPTOR.full_name:
            return emit_singular(field.name, iter_expr, "payload_container", None)
        child_needed = self.walk(child_desc)
        if child_needed:
            return emit_loop(field.name, iter_expr, name_for(child_desc))
        return None

    def walk(self, desc: Descriptor) -> bool:
        key = desc.full_name
        if key in self.generated:
            return self.generated[key]
        if key in self.in_progress:
            return True

        has_payload = False
        self.in_progress.add(key)
        body_lines: list[str] = []
        if desc.full_name == SearchAttributes.DESCRIPTOR.full_name:
            body_lines.append("        if self.skip_search_attributes:")
            body_lines.append("            return")

        oneof_fields: dict[int, list[FieldDescriptor]] = {}
        regular_fields: list[FieldDescriptor] = []

        for field in desc.fields:
            if field.type != FieldDescriptor.TYPE_MESSAGE:
                continue
            if field.containing_oneof is not None:
                oneof_fields.setdefault(field.containing_oneof.index, []).append(field)
            else:
                regular_fields.append(field)

        for field in regular_fields:
            if (
                desc.full_name == "coresdk.workflow_commands.ScheduleNexusOperation"
                and field.name == "input"
            ):
                has_payload = True
                body_lines.append(
                    """\
        if o.HasField("input"):
            await self._visit_system_nexus_payload(fs, o.service, o.operation, o.input)"""
                )
                continue

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
                            has_payload = True
                            body_lines.append(
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
                            has_payload = True
                            body_lines.append(
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
                        has_payload = True
                        body_lines.append(child)
            else:
                child_desc = field.message_type
                child_has_payload = self.walk(child_desc)
                has_payload |= child_has_payload
                if child_has_payload:
                    body_lines.append(
                        emit_singular(
                            field.name, f"o.{field.name}", name_for(child_desc), "if"
                        )
                    )

        for fields in oneof_fields.values():
            oneof_lines = []
            first = True
            for field in fields:
                child_desc = field.message_type
                child_has_payload = self.walk(child_desc)
                has_payload |= child_has_payload
                if child_has_payload:
                    oneof_lines.append(
                        emit_singular(
                            field.name,
                            f"o.{field.name}",
                            name_for(child_desc),
                            "if" if first else "elif",
                        )
                    )
                    first = False
            if oneof_lines:
                body_lines.extend(oneof_lines)

        self.generated[key] = has_payload
        self.in_progress.discard(key)
        if has_payload:
            annotation = self.python_type_for_descriptor(desc)
            lines = [
                f"    async def _visit_{name_for(desc)}("
                f"self, fs: VisitorFunctions, o: {annotation}"
                "):"
            ]
            lines.extend(body_lines)
            self.methods.append("\n".join(lines) + "\n")
        return has_payload


def write_bridge_visitors() -> None:
    out_path = base_dir / "temporalio" / "bridge" / "_visitor.py"
    roots = [
        WorkflowActivation.DESCRIPTOR,
        WorkflowActivationCompletion.DESCRIPTOR,
    ]
    out_path.write_text(PayloadVisitorGenerator().generate(roots))


def write_system_nexus_payload_visitors() -> None:
    out_path = base_dir / "temporalio" / "nexus" / "system" / "_payload_visitor.py"
    roots = discover_system_nexus_roots()
    out_path.write_text(PayloadVisitorGenerator().generate(roots))


if __name__ == "__main__":
    print("Generating temporalio/bridge/_visitor.py...", file=sys.stderr)
    write_bridge_visitors()
    print("Generating temporalio/nexus/system/_payload_visitor.py...", file=sys.stderr)
    write_system_nexus_payload_visitors()
    subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            "I",
            "--fix",
            "temporalio/bridge/_visitor.py",
            "temporalio/nexus/system/_payload_visitor.py",
        ],
        cwd=base_dir,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "format",
            "temporalio/bridge/_visitor.py",
            "temporalio/nexus/system/_payload_visitor.py",
        ],
        cwd=base_dir,
        check=True,
    )
