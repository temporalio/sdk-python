from pathlib import Path

from pydoctor import model  # type: ignore[reportMissingTypeStubs]
from pydoctor.options import Options  # type: ignore[reportMissingTypeStubs]

from scripts.gen_docs import ProtobufSystem


def test_protobuf_docs_expose_names_without_class_details(tmp_path: Path) -> None:
    package = tmp_path / "example"
    package.mkdir()
    (package / "__init__.py").write_text(
        'from .message_pb2 import Payload, Status\n__all__ = ["Payload", "Status"]\n'
    )
    (package / "message_pb2.py").write_text(
        'raise RuntimeError("Documentation must not import generated modules")\n'
    )
    stub = package / "message_pb2.pyi"
    stub.write_text(
        "class Payload:\n"
        '    """An upstream proto comment with `non-Python link text`."""\n'
        "    data: bytes\n"
        "class _Status:\n"
        "    ValueType = int\n"
        "class Status(_Status):\n"
        "    ACTIVE: int\n"
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

    for name, lineno in (("Payload", 1), ("Status", 6)):
        attribute = system.find_object(f"example.{name}")
        assert isinstance(attribute, model.Attribute)
        assert attribute.kind == model.DocumentableKind.VARIABLE
        assert attribute.source_path == stub
        assert attribute.linenumber == lineno
        assert attribute.source_href == (
            f"https://example.com/source/example/message_pb2.pyi#L{lineno}"
        )
        assert attribute.docstring is None
        assert not attribute.contents
        assert attribute.isVisible
    module = system.allobjects["example.message_pb2"]
    assert isinstance(module, model.Module)
    assert module.source_path == package / "message_pb2.py"
    assert module.docformat is None
    assert module.source_href == "https://example.com/source/example/message_pb2.py"
    assert system.objForFullName("example.message_pb2._Status") is None
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
