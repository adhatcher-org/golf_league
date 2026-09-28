"""Keep the future course-handicap helper out of all live application paths."""

import ast
from pathlib import Path

import pytest
from test_architecture import forbidden_imports

APPLICATION_DIR = Path(__file__).parent.parent / "golf_league"
HELPER_PATH = APPLICATION_DIR / "domain" / "handicap.py"


def _resolved_handicap_imports(
    module_path: Path, application_dir: Path = APPLICATION_DIR
) -> set[str]:
    """Find absolute and relative ``ImportFrom`` paths that reach the helper."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _resolved_import_target(node, module_path, application_dir)
        if target == "golf_league.domain.handicap":
            imports.add(target)
        elif target == "golf_league.domain" and any(
            alias.name == "handicap" for alias in node.names
        ):
            imports.add("golf_league.domain.handicap")
    return imports


def _resolved_import_target(
    node: ast.ImportFrom, module_path: Path, application_dir: Path
) -> str | None:
    """Resolve an ``ImportFrom`` module against its source package path."""
    if node.level == 0:
        return node.module

    try:
        relative_path = module_path.relative_to(application_dir)
    except ValueError:
        return None

    # An __init__.py belongs to its containing package, as does a regular
    # module for the purpose of resolving a relative import.
    package_parts = [application_dir.name, *relative_path.parent.parts]

    segments_to_remove = node.level - 1
    if segments_to_remove >= len(package_parts):
        return None
    if segments_to_remove:
        package_parts = package_parts[:-segments_to_remove]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def test_no_application_module_imports_domain_handicap():
    for module_path in APPLICATION_DIR.rglob("*.py"):
        if module_path == HELPER_PATH:
            continue
        direct_imports = forbidden_imports(
            module_path, {"golf_league.domain.handicap"}
        )
        resolved_imports = _resolved_handicap_imports(module_path)
        assert not direct_imports | resolved_imports, (
            f"{module_path} imports golf_league.domain.handicap"
        )


@pytest.mark.parametrize(
    ("relative_path", "source", "expects_direct", "expects_resolved"),
    [
        ("services/example.py", "import golf_league.domain.handicap\n", True, False),
        (
            "services/example.py",
            "from golf_league.domain import handicap\n",
            False,
            True,
        ),
        (
            "services/example.py",
            "from golf_league.domain import handicap as handicap_module\n",
            False,
            True,
        ),
        (
            "services/example.py",
            "from ..domain.handicap import course_handicap\n",
            False,
            True,
        ),
        (
            "services/example.py",
            "from ..domain import handicap\n",
            False,
            True,
        ),
        (
            "domain/example.py",
            "from .handicap import course_handicap\n",
            False,
            True,
        ),
        (
            "domain/example.py",
            "from . import handicap as handicap_module\n",
            False,
            True,
        ),
        (
            "domain/__init__.py",
            "from .handicap import course_handicap\n",
            False,
            True,
        ),
        ("domain/__init__.py", "from . import handicap\n", False, True),
        ("__init__.py", "from ..domain import handicap\n", False, False),
    ],
)
def test_isolation_guards_detect_all_handicap_import_forms(
    tmp_path, relative_path, source, expects_direct, expects_resolved
):
    application_dir = tmp_path / "golf_league"
    module_path = application_dir / relative_path
    module_path.parent.mkdir(parents=True)
    module_path.write_text(source, encoding="utf-8")

    direct_imports = forbidden_imports(module_path, {"golf_league.domain.handicap"})
    resolved_imports = _resolved_handicap_imports(module_path, application_dir)

    assert bool(direct_imports) is expects_direct
    assert bool(resolved_imports) is expects_resolved
