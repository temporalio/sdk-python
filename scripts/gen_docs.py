import ast
import shutil
from pathlib import Path

from pydoctor import driver, model

base_dir = Path(__file__).parent.parent


class ProtobufSystem(model.System):
    """Expose dynamic protobuf types as names without documenting their internals.

    Pydoctor discovers .py files, not adjacent .pyi stubs. The generated .py
    files create types through runtime builder calls that its static analysis
    cannot resolve. Read public top-level class names from the stubs and register
    them as attributes so links resolve without generating full class pages.
    """

    def addModule(self, path: Path, parent: model.Package | None) -> None:
        super().addModule(path, parent)
        stub = path.with_suffix(".pyi")
        if path.name.endswith("_pb2.py") and stub.is_file():
            name = f"{parent.fullName()}.{path.stem}" if parent else path.stem
            module = self.allobjects[name]
            assert isinstance(module, model.Module)
            for node in ast.parse(stub.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    attribute = self.Attribute(self, node.name, module, stub)
                    attribute.kind = model.DocumentableKind.VARIABLE
                    attribute.parentMod = module
                    attribute.setLineNumber(node.lineno)
                    # The declaration is in the stub, not the runtime module.
                    if source_href := self._formatSourceHref(stub):
                        attribute.source_href = self.options.htmlsourcetemplate.format(
                            mod_source_href=source_href, lineno=str(node.lineno)
                        )
                    self.addObject(attribute)


if __name__ == "__main__":
    print("Generating documentation...")

    result = driver.main(["--quiet", "--system-class=__main__.ProtobufSystem"])
    if result:
        raise SystemExit(result)

    # Copy favicon
    shutil.copyfile(
        base_dir / "scripts" / "_img" / "favicon.ico",
        base_dir / "build" / "apidocs" / "favicon.ico",
    )
