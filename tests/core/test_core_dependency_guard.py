"""AC-1.1: ``lockbot.core`` must stay installable without the backend stack.

``core/`` is published as a standalone package, so a FastAPI or SQLAlchemy import
anywhere under it would make the chat bot depend on the platform's web stack. The
check is static (AST-based), so it also catches imports hidden inside functions.
"""

import ast
import os
import pathlib
import subprocess
import sys

import pytest

CORE_DIR = pathlib.Path(__file__).resolve().parents[2] / "python" / "lockbot" / "core"
FORBIDDEN_ROOTS = frozenset({"fastapi", "sqlalchemy"})

CORE_MODULES = sorted(CORE_DIR.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    """Collect every top-level package name imported anywhere in the module."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_modules_were_found():
    """Guard the guard: an empty file list would make every check below vacuous."""
    assert len(CORE_MODULES) > 5, f"Expected to find core modules under {CORE_DIR}"


@pytest.mark.parametrize("module_path", CORE_MODULES, ids=lambda p: str(p.relative_to(CORE_DIR)))
def test_core_module_does_not_import_backend_stack(module_path):
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    offenders = _imported_roots(tree) & FORBIDDEN_ROOTS
    assert not offenders, f"{module_path.relative_to(CORE_DIR)} imports {sorted(offenders)}"


def test_importing_core_handler_does_not_pull_in_backend_stack():
    """Importing the shared dispatcher must not load FastAPI or SQLAlchemy transitively."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import lockbot.core.handler; "
            "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('fastapi', 'sqlalchemy')); "
            "print(loaded); sys.exit(1 if loaded else 0)",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"backend stack was imported: {result.stdout.strip()}"
