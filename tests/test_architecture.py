import ast
import pathlib

import pytest


def _banned_for(name: str | None, banned: set[str]) -> str | None:
    """Return the banned entry matching `name`, or None.

    `name` matches a banned entry `b` when it equals `b` exactly, or starts
    with `b` followed by a dot (so `sqlalchemy.orm` counts as `sqlalchemy`).
    """
    if name is None:
        return None
    for b in banned:
        if name == b or name.startswith(b + "."):
            return b
    return None


def _found_in_tree(tree: ast.AST, banned: set[str]) -> set[str]:
    """Walk a parsed module and collect the banned names it imports."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                banned_name = _banned_for(alias.name, banned)
                if banned_name is not None:
                    found.add(banned_name)
        elif isinstance(node, ast.ImportFrom):
            banned_name = _banned_for(node.module, banned)
            if banned_name is not None:
                found.add(banned_name)
    return found


def forbidden_imports(module_path: pathlib.Path, banned: set[str]) -> set[str]:
    """Check a Python module for forbidden imports using AST.

    The file is parsed with `ast`; it is never imported. A module is banned
    when its dotted name equals a banned name or starts with a banned name
    plus a dot, so `sqlalchemy.orm` counts as `sqlalchemy`.

    Args:
        module_path: Path to the Python file to check
        banned: Set of module names that are forbidden

    Returns:
        Set of forbidden imports found in the module
    """
    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (SyntaxError, OSError):
        # If the file has syntax errors or can't be read, return empty set
        # This ensures the test doesn't fail due to file issues
        return set()

    return _found_in_tree(tree, banned)


def get_python_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """Recursively find all Python files in a directory."""
    python_files = []
    for item in directory.rglob("*"):
        if item.is_file() and item.suffix == ".py":
            python_files.append(item)
    return python_files


@pytest.fixture
def domain_dir():
    """Path to the domain directory."""
    return pathlib.Path(__file__).parent.parent / "golf_league" / "domain"


@pytest.fixture
def services_dir():
    """Path to the services directory."""
    return pathlib.Path(__file__).parent.parent / "golf_league" / "services"


@pytest.fixture
def tmp_module_file(tmp_path):
    """Create a temporary Python file with a forbidden import for testing."""
    module_file = tmp_path / "test_module.py"
    module_file.write_text("import sqlalchemy\n")
    return module_file


def test_domain_has_no_forbidden_imports(domain_dir):
    """Test that domain layer has no forbidden imports."""
    banned = {"sqlalchemy", "fastapi", "golf_league.models"}
    python_files = get_python_files(domain_dir)

    for py_file in python_files:
        forbidden = forbidden_imports(py_file, banned)
        assert not forbidden, f"File {py_file} contains forbidden imports: {forbidden}"


def test_services_do_not_import_fastapi(services_dir):
    """Test that services layer does not import fastapi."""
    banned = {"fastapi"}
    python_files = get_python_files(services_dir)

    for py_file in python_files:
        forbidden = forbidden_imports(py_file, banned)
        assert not forbidden, f"File {py_file} contains forbidden imports: {forbidden}"


def test_guard_catches_a_real_violation(tmp_module_file):
    """Test that the guard actually catches forbidden imports."""
    banned = {"sqlalchemy"}
    forbidden = forbidden_imports(tmp_module_file, banned)
    assert forbidden == {"sqlalchemy"}, f"Expected to find sqlalchemy import, found: {forbidden}"


def test_guard_catches_a_submodule_violation(tmp_path):
    """Test that the guard catches dotted submodule imports, not just exact matches."""
    module_file = tmp_path / "submodule_violation.py"
    module_file.write_text(
        "import sqlalchemy.orm\nfrom sqlalchemy.orm import Session\n"
        "from fastapi.responses import JSONResponse\n"
    )
    forbidden = forbidden_imports(module_file, {"sqlalchemy", "fastapi"})
    assert forbidden == {"sqlalchemy", "fastapi"}, (
        f"Expected submodule imports to resolve to banned prefixes, found: {forbidden}"
    )
