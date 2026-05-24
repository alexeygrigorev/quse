import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [ROOT / "quse", ROOT / "tests"]


def test_python_files_do_not_use_ternary_expressions() -> None:
    offenders: list[str] = []
    for base_path in PYTHON_PATHS:
        for source_path in base_path.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.IfExp):
                    relative_path = source_path.relative_to(ROOT)
                    offenders.append(f"{relative_path}:{node.lineno}")

    assert offenders == []
