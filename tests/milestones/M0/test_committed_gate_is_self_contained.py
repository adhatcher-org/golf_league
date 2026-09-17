"""Guard against the class of defect found at M0 gate attempt 1.

Gate attempt 1 failed because `make check` passed only thanks to things
*outside* the committed project: `make lint` called a bare `ruff`, which
happened to be resolvable from a globally installed Homebrew binary, and
`httpx2` (the package `starlette.testclient.TestClient` imports so the test
suite's `TestClient` fixtures work) was present in the developer's `.venv`
but missing from `uv.lock`. Both defects were invisible on a machine that
happened to already have the right tools lying around ambiently, and both
would have broken a genuinely fresh checkout (e.g. CI's
`uv sync --frozen --extra dev` followed by `make check`, without ever
activating the venv or otherwise touching global PATH state).

These tests re-derive the check from the committed files only: they read
`Makefile` and `uv.lock` from disk and use `tomllib`/`importlib` — no shelling
out to `make`, no network, and no hard-coded package versions.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The recipes that "make check" (directly or transitively) runs, per the
# Makefile's own `check: lint test test-with-cov security dependency-check`
# line. These are exactly the targets the gate-1 defect could hide in.
_CHECK_RECIPE_TARGETS = (
    "lint",
    "test",
    "test-with-cov",
    "coverage",
    "security",
    "dependency-check",
)


def _makefile_recipe_lines(makefile_text: str, target: str) -> list[str]:
    """Return the tab-indented recipe lines belonging to `target:`.

    A Makefile target's recipe is every line immediately following its
    `target:` header that starts with a tab, up to the first line that
    doesn't (a blank line, a comment, or the next target).
    """
    lines = makefile_text.splitlines()
    header = re.compile(rf"^{re.escape(target)}\s*:")
    recipe: list[str] = []
    in_target = False
    for line in lines:
        if header.match(line):
            in_target = True
            continue
        if in_target:
            if line.startswith("\t"):
                recipe.append(line[1:])
                continue
            # First non-tab line ends this target's recipe.
            break
    return recipe


def test_every_check_recipe_command_runs_through_uv() -> None:
    """No `lint`/`test`/`coverage`/`security`/`dependency-check` line calls a
    bare ambient tool; every shell command in those recipes is `uv run ...`
    or `uv pip ...`.

    This is the regression check for gate-1 defect 1: `Makefile:14` used to
    read `\truff check .`, which resolves whatever `ruff` happens to be on
    PATH rather than the project's locked dev dependency in `.venv`. This
    test reads the committed `Makefile` text directly (it never invokes
    `make` or a shell), so it fails the moment any of these recipes
    regresses to a bare tool invocation, regardless of what happens to be
    installed globally on the machine running the test.
    """
    makefile_text = (_REPO_ROOT / "Makefile").read_text()

    checked_any = False
    for target in _CHECK_RECIPE_TARGETS:
        recipe_lines = _makefile_recipe_lines(makefile_text, target)
        assert recipe_lines, f"expected at least one recipe line for target {target!r}"

        for raw_line in recipe_lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("@echo"):
                # Pure documentation/echo lines (e.g. the `help` target's
                # style, if it ever leaked in) never invoke a tool.
                continue

            # A recipe line may chain multiple shell commands; every segment
            # must itself go through uv.
            for segment in re.split(r"&&|;|\|", line):
                segment = segment.strip()
                if not segment:
                    continue
                checked_any = True
                assert segment.startswith("uv run ") or segment.startswith(
                    "uv pip "
                ), (
                    f"Makefile target {target!r} runs a command that does not go "
                    f"through 'uv run'/'uv pip': {segment!r} (full line: {raw_line!r}). "
                    "A bare tool invocation resolves whatever happens to be on the "
                    "ambient PATH instead of the project's locked dev dependency, "
                    "which is exactly the class of bug that broke gate attempt 1's "
                    "'make check' (a bare 'ruff check .')."
                )

    assert checked_any, "expected to find at least one command to check across all targets"


def _preferred_http_client_backend_package() -> str:
    """Return the package name `starlette.testclient.TestClient` imports
    first for its HTTP transport, read statically from the installed
    module's source (never imported/executed), so this doesn't hard-code a
    package name or version and stays correct if the backend is ever
    renamed back to plain `httpx`.
    """
    spec = importlib.util.find_spec("starlette.testclient")
    assert spec is not None and spec.origin, (
        "starlette.testclient must be resolvable (it backs FastAPI's TestClient, "
        "which tests/conftest.py's `client` fixture and the M0 exit-criteria tests use)"
    )
    source = Path(spec.origin).read_text()

    # starlette/testclient.py tries its preferred HTTP client backend as
    # `import <name> as httpx`, falling back to the historical `httpx` on
    # ModuleNotFoundError. We only need the first (preferred) name.
    match = re.search(r"^\s*import\s+(\w+)\s+as\s+httpx\b", source, flags=re.M)
    assert match, (
        "could not find an 'import X as httpx' line in starlette/testclient.py; "
        "the HTTP backend detection in this test needs updating to match the "
        "installed starlette version"
    )
    return match.group(1)


def test_http_client_backend_used_by_testclient_is_in_the_lock() -> None:
    """The HTTP client package that backs every `TestClient(...)` in the
    suite (via `starlette.testclient`, used by `tests/conftest.py`'s
    `client` fixture and every `tests/milestones/M0` test that builds the
    real app) is present in the committed `uv.lock`.

    This is the regression check for gate-1 defect 2: `httpx2` was
    installed in a developer's `.venv` (e.g. pulled in transitively) but
    absent from `uv.lock`, so `uv sync --frozen` in a genuinely fresh
    checkout would not install it and every `TestClient`-based test would
    fail. Parsing is done with `tomllib` against the committed lock file
    only, no version numbers are hard-coded, and nothing is installed or
    downloaded.
    """
    backend_package = _preferred_http_client_backend_package()

    with (_REPO_ROOT / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)

    locked_names = {pkg["name"] for pkg in lock.get("package", [])}

    assert backend_package in locked_names, (
        f"{backend_package!r} is imported by starlette.testclient (the engine behind "
        "every TestClient in this suite) but is not a package in the committed "
        "uv.lock. `uv sync --frozen` on a fresh checkout would not install it, and "
        "every test that builds a TestClient would fail there even though it "
        f"passes on a machine where {backend_package!r} happens to already be "
        "installed."
    )
