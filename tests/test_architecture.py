import ast
from pathlib import Path

from gitcontext.ingest.parser import ParsedBlock as ParserParsedBlock
from gitcontext.records import ParsedBlock

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "gitcontext"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT)
    if path.name == "__init__.py":
        suffix = ".".join(relative.parent.parts)
    else:
        suffix = ".".join(relative.with_suffix("").parts)
    return "gitcontext" + (f".{suffix}" if suffix else "")


def test_storage_uses_the_shared_record_contract() -> None:
    source = (PACKAGE_ROOT / "vector_store.py").read_text(encoding="utf-8")

    assert "from gitcontext.records import ParsedBlock" in source
    assert "gitcontext.ingest.parser" not in source
    assert ParserParsedBlock is ParsedBlock


def test_internal_python_import_graph_has_no_cycles() -> None:
    module_paths = {_module_name(path) for path in PACKAGE_ROOT.rglob("*.py")}
    graph = {module: set() for module in module_paths}

    for path in PACKAGE_ROOT.rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                graph[module].update(
                    alias.name for alias in node.names if alias.name in module_paths
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in module_paths:
                    graph[module].add(node.module)
                for alias in node.names:
                    child = f"{node.module}.{alias.name}"
                    if child in module_paths:
                        graph[module].add(child)

    visiting = set()
    visited = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"Circular import involving {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module)
