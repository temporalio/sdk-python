import shutil
from pathlib import Path

from pydoctor import driver, model

base_dir = Path(__file__).parent.parent


class ProtobufSystem(model.System):
    """Use static protobuf stubs for types created dynamically by the runtime."""

    def addModule(self, path: Path, parent: model.Package | None) -> None:
        super().addModule(path, parent)
        stub = path.with_suffix(".pyi")
        if path.name.endswith("_pb2.py") and stub.is_file():
            name = f"{parent.fullName()}.{path.stem}" if parent else path.stem
            module = self.allobjects[name]
            assert isinstance(module, model.Module)
            module.source_path = stub
            # These docstrings contain upstream proto comments, not Google-style Python docs.
            module.docformat = "plaintext"
            module.source_href = None
            self.setSourceHref(module, stub)


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
