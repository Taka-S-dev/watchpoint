"""officetext can be taken out as a folder: it imports only the standard library, its three
dependencies and itself."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
ALLOWED = {"officetext", "docx", "openpyxl", "lxml", "pytest"}  # pytest: for these tests only


def test_imports_only_the_standard_library_its_dependencies_and_itself() -> None:
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                assert node.level == 1, f"{path.name}: a relative import reaches out of officetext"
                continue
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top in ALLOWED, f"{path.name} imports {name}"


def test_the_project_part_can_be_left_out() -> None:
    """project.py needs Microsoft Project; nothing else in the package imports it, so it can be removed."""
    for path in PACKAGE.glob("*.py"):
        if path.name == "project.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "from .project" not in text and "import project" not in text, path.name
