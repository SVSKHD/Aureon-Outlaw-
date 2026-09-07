import ast
from pathlib import Path


def test_exactly_one_final_router_definition():
    root = Path(__file__).resolve().parents[1] / "src" / "xau_mt5_bot"
    definitions = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions.extend(
            (path.name, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "final_decision_router"
        )
    assert definitions == [("decision_router.py", "final_decision_router")]
