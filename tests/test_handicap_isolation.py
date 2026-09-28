"""Keep the future course-handicap helper out of all live application paths."""

import ast
from pathlib import Path

import pytest
from test_architecture import forbidden_imports

APPLICATION_DIR = Path(__file__).parent.parent / "golf_league"
HELPER_PATH = APPLICATION_DIR / "domain" / "handicap.py"


def _package_level_handicap_imports(module_path: Path) -> set[str]:
    """Find ``from golf_league.domain import handicap`` imports, including aliases."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    return {
        "golf_league.domain.handicap"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "golf_league.domain"
        and any(alias.name == "handicap" for alias in node.names)
    }


def test_no_application_module_imports_domain_handicap():
    for module_path in APPLICATION_DIR.rglob("*.py"):
        if module_path == HELPER_PATH:
            continue
        direct_imports = forbidden_imports(
            module_path, {"golf_league.domain.handicap"}
        )
        package_imports = _package_level_handicap_imports(module_path)
        assert not direct_imports | package_imports, (
            f"{module_path} imports golf_league.domain.handicap"
        )


@pytest.mark.parametrize(
    ("source", "expects_direct", "expects_package"),
    [
        ("import golf_league.domain.handicap\n", True, False),
        ("from golf_league.domain import handicap\n", False, True),
        ("from golf_league.domain import handicap as handicap_module\n", False, True),
    ],
)
def test_isolation_guards_detect_all_handicap_import_forms(
    tmp_path, source, expects_direct, expects_package
):
    module_path = tmp_path / "future_live_path.py"
    module_path.write_text(source, encoding="utf-8")

    direct_imports = forbidden_imports(module_path, {"golf_league.domain.handicap"})
    package_imports = _package_level_handicap_imports(module_path)

    assert bool(direct_imports) is expects_direct
    assert bool(package_imports) is expects_package
