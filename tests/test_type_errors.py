"""
This file contains a test allowing assertions to be made that an expected type error is in
fact produced by the type-checker. I.e. that the type checker is not delivering a false
negative.

To use the test, add a comment of the following form to your test code:

    # assert-type-error-pyright: 'No overloads for "execute_operation" match' await
    nexus_client.execute_operation(  # type: ignore

The `type: ignore` is only necessary if your test code is being type-checked.

This is a copy of https://github.com/nexus-rpc/sdk-python/blob/main/tests/test_type_errors.py

Until a shared library is created, please keep the two in sync.
"""

import itertools
import json
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path

import pytest


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Dynamically generate test cases for files with type error assertions."""
    if metafunc.function.__name__ in [
        "test_type_errors_pyright",
        "test_type_errors_mypy",
    ]:
        tests_dir = Path(__file__).parent
        files_with_assertions = []

        for test_file in tests_dir.rglob("test_*.py"):
            if test_file.name == "test_type_errors.py":
                continue

            if _has_type_error_assertions(test_file):
                files_with_assertions.append(test_file)

        metafunc.parametrize(
            "test_file",
            files_with_assertions,
            ids=lambda f: str(f.relative_to(tests_dir)),
        )


@pytest.mark.skipif(platform.system() == "Windows", reason="TODO: broken on Windows")
def test_type_errors_pyright(test_file: Path):
    """
    Validate type error assertions in a single test file using pyright.

    For each line with a comment of the form `# assert-type-error-pyright: "regex"`,
    verify that pyright reports an error on the next non-comment line matching the regex.
    Also verify that there are no unexpected type errors.
    """
    _test_type_errors(
        test_file,
        _get_expected_errors(test_file, "pyright"),
        _get_pyright_errors(test_file),
    )


def _test_type_errors(
    test_file: Path,
    expected_errors: dict[int, str],
    actual_errors: dict[int, str],
) -> None:
    for line_num, expected_pattern in sorted(expected_errors.items()):
        if line_num not in actual_errors:
            pytest.fail(
                f"{test_file}:{line_num}: Expected type error matching '{expected_pattern}' but no error found"
            )

        actual_msg = actual_errors[line_num]
        if not re.search(expected_pattern, actual_msg):
            pytest.fail(
                f"{test_file}:{line_num}: Expected error matching '{expected_pattern}' but got '{actual_msg}'"
            )


def _has_type_error_assertions(test_file: Path) -> bool:
    """Check if a file contains any type error assertions."""
    with open(test_file) as f:
        return any(re.search(r"# assert-type-error-\w+:", line) for line in f)


def _get_expected_errors(test_file: Path, type_checker: str) -> dict[int, str]:
    """Parse expected type errors from comments in a file for the specified type checker."""
    expected_errors = {}

    with open(test_file) as f:
        lines = zip(itertools.count(1), f)
        for line_num, line in lines:
            if match := re.search(
                rf'# assert-type-error-{re.escape(type_checker)}:\s*["\'](.+)["\']',
                line,
            ):
                pattern = match.group(1)
                for line_num, line in lines:
                    if line := line.strip():
                        if not line.startswith("#"):
                            expected_errors[line_num] = pattern
                            break

    return expected_errors


def _get_pyright_errors(test_file: Path) -> dict[int, str]:
    """Run pyright on a file and parse the actual type errors."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        # Create a temporary config file to disable type ignore comments
        config_data = {"enableTypeIgnoreComments": False}
        json.dump(config_data, f)
        config_path = f.name

    try:
        result = subprocess.run(
            ["uv", "run", "pyright", "--project", config_path, str(test_file)],
            capture_output=True,
            text=True,
        )

        actual_errors = {}
        abs_path = test_file.resolve()

        for line in result.stdout.splitlines():
            # pyright output format: /full/path/to/file.py:line:column - error: message (error_code)
            if match := re.match(
                rf"\s*{re.escape(str(abs_path))}:(\d+):\d+\s*-\s*error:\s*(.+)", line
            ):
                line_num = int(match.group(1))
                error_msg = match.group(2).strip()
                # Remove error code in parentheses if present
                error_msg = re.sub(r"\s*\([^)]+\)$", "", error_msg)
                actual_errors[line_num] = error_msg

        return actual_errors
    finally:
        if os.path.exists(config_path):
            os.unlink(config_path)


def _get_mypy_errors(test_file: Path) -> dict[int, str]:  # pyright: ignore[reportUnusedFunction]
    """Run mypy on a file and parse the actual type errors.

    Note: mypy does not have a direct equivalent to pyright's enableTypeIgnoreComments=false,
    so type ignore comments will still be respected by mypy. Users should avoid placing
    # type: ignore comments on lines they want to test, or manually remove them for testing.
    """
    result = subprocess.run(
        ["uv", "run", "mypy", str(test_file)],
        capture_output=True,
        text=True,
    )

    actual_errors = {}
    abs_path = test_file.resolve()

    for line in result.stdout.splitlines():
        # mypy output format: file.py:line: error: message
        if match := re.match(
            rf"{re.escape(str(abs_path))}:(\d+):\s*error:\s*(.+)", line
        ):
            line_num = int(match.group(1))
            error_msg = match.group(2).strip()
            actual_errors[line_num] = error_msg

    return actual_errors
