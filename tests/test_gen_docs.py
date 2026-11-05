from pathlib import Path

from pydoctor import epydoc2stan, model  # type: ignore[reportMissingTypeStubs]
from pydoctor.options import Options  # type: ignore[reportMissingTypeStubs]

from scripts.gen_docs import ProtobufSystem


def test_protobuf_docs_use_static_stubs(tmp_path: Path) -> None:
    package = tmp_path / "example"
    package.mkdir()
    (package / "__init__.py").write_text(
        'from .message_pb2 import Payload\n__all__ = ["Payload"]\n'
    )
    (package / "message_pb2.py").write_text(
        'raise RuntimeError("Documentation must not import generated modules")\n'
    )
    stub = package / "message_pb2.pyi"
    stub.write_text(
        "class Payload:\n"
        '    """An upstream proto comment with `non-Python link text`."""\n'
        "    data: bytes\n"
    )
    options = Options.from_args(
        [
            "--docformat=google",
            "--html-viewsource-base=https://example.com/source",
            f"--project-base-dir={tmp_path}",
        ]
    )
    system = ProtobufSystem(options)
    builder = system.systemBuilder(system)
    builder.addModule(package)
    builder.buildModules()

    payload = system.find_object("example.Payload")
    assert isinstance(payload, model.Class)
    module = system.allobjects["example.message_pb2"]
    assert isinstance(module, model.Module)
    assert module.source_path == stub
    assert module.docformat == "plaintext"
    assert module.source_href == "https://example.com/source/example/message_pb2.pyi"
    assert "data" in payload.contents
    epydoc2stan.format_docstring(payload)
    assert system.violations == 0


def test_docs_keep_normal_python_sources(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("class Actual:\n    pass\n")
    source.with_suffix(".pyi").write_text("class StubOnly:\n    pass\n")
    system = ProtobufSystem()
    builder = system.systemBuilder(system)
    builder.addModule(source)
    builder.buildModules()

    assert system.find_object("module.Actual") is not None
    assert system.objForFullName("module.StubOnly") is None
