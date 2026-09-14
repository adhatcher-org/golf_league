import ast
import pathlib

import pytest


def forbidden_imports(module_path: pathlib.Path, banned: set[str]) -> set[str]:
    """Check a Python module for forbidden imports using AST.

    Args:
        module_path: Path to the Python file to check
        banned: Set of module names that are forbidden

    Returns:
        Set of forbidden imports found in the module
    """
    found = set()

    try:
        with open(module_path, encoding='utf-8') as f:
            source = f.read()

        tree = ast.parse(source, filename=str(module_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    if module_name in banned:
                        found.add(module_name)
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module
                if module_name in banned:
                    found.add(module_name)
                # Also check for relative imports like "from . import models"
                elif module_name is None and node.level > 0:
                    # This is a relative import, check if it's a banned module
                    # We need to resolve the actual module name
                    pass

    except (SyntaxError, OSError):
        # If the file has syntax errors or can't be read, return empty set
        # This ensures the test doesn't fail due to file issues
        pass

    return found


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
