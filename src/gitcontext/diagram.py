"""Extract and label a bounded Mermaid view of indexed source imports."""

import ast
import hashlib
import json
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from gitcontext.answer import complete_json
from gitcontext.config import Settings
from gitcontext.retrieval import retrieve
from gitcontext.vector_store import VectorStore

DIAGRAM_VERSION = 2
MAX_NODES = 25
LABEL_BATCH_SIZE = 10
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
JS_IMPORT_RE = re.compile(
    r"\bimport\s+(?:[\s\S]*?\s+from\s+)?(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)"
)
JS_REQUIRE_RE = re.compile(
    r"\brequire\s*\(\s*(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*\)"
)
UNSAFE_LABEL_CHARS = re.compile(r"[\"'\[\]#;|\\\r\n<>]")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
EDGE_VERBS = {
    "build", "call", "check", "collect", "compile", "configure", "create",
    "define", "encode", "expose", "extract", "fetch", "generate", "handle",
    "import", "index", "load", "map", "open", "parse", "provide", "read",
    "register", "resolve", "retrieve", "return", "run", "select", "send",
    "serve", "store", "update", "use", "validate", "write",
}


def source_extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def is_test_path(path: str) -> bool:
    relative = PurePosixPath(path)
    filename = relative.name.lower()
    return (
        any(part.lower() in {"test", "tests", "__tests__"} for part in relative.parts[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or re.search(r"(?:^|[._-])(?:test|spec)\.[^.]+$", filename) is not None
    )


def analysed_paths(paths: Iterable[str], *, include_tests: bool = False) -> list[str]:
    return sorted(
        path for path in paths
        if source_extension(path) in SOURCE_EXTENSIONS
        and (include_tests or not is_test_path(path))
    )


def content_digest(
    paths: Iterable[str], contents: Mapping[str, str],
    stored_hashes: Mapping[str, str] | None = None,
    *, include_tests: bool = False,
) -> str:
    digest = hashlib.sha256()
    for path in analysed_paths(paths, include_tests=include_tests):
        value = (stored_hashes or {}).get(path)
        if value is None:
            value = hashlib.sha256(contents.get(path, "").encode("utf-8")).hexdigest()
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def cache_key(
    repository_identity: str, commit_sha: str | None, digest: str
) -> str:
    if commit_sha:
        identity = f"{repository_identity}\0{commit_sha}\0{digest}\0{DIAGRAM_VERSION}"
    else:
        identity = f"{digest}\0{DIAGRAM_VERSION}"
    return "diagram:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def extract_imports(
    contents: Mapping[str, str], *, include_tests: bool = False,
) -> list[dict[str, Any]]:
    """Return deduplicated file-to-file imports for Python, JS, and TS."""
    files = analysed_paths(contents, include_tests=include_tests)
    module_paths: dict[str, set[str]] = defaultdict(set)
    for path in files:
        if source_extension(path) == ".py":
            for module in _python_module_names(path):
                module_paths[module].add(path)

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for path in files:
        extension = source_extension(path)
        source = contents[path]
        if extension == ".py":
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                imports: list[str] = []
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = _python_import_base(path, node.module, node.level)
                    if base:
                        imports.append(base)
                        imports.extend(f"{base}.{alias.name}" for alias in node.names
                                       if alias.name != "*")
                for module in imports:
                    targets = module_paths.get(module, set())
                    if len(targets) == 1:
                        target = next(iter(targets))
                        edges.setdefault((path, target), {
                            "source_file": path,
                            "target_file": target,
                            "line": node.lineno,
                            "statement": source.splitlines()[node.lineno - 1].strip(),
                        })
        else:
            matches = list(JS_IMPORT_RE.finditer(source)) + list(JS_REQUIRE_RE.finditer(source))
            for match in sorted(matches, key=lambda item: item.start()):
                target = _resolve_js_import(path, match.group("path"), files)
                if target:
                    line = source.count("\n", 0, match.start()) + 1
                    statement = source.splitlines()[line - 1].strip()
                    edges.setdefault((path, target), {
                        "source_file": path,
                        "target_file": target,
                        "line": line,
                        "statement": statement,
                    })
    return [edges[key] for key in sorted(edges)]


def _python_module_names(path: str) -> set[str]:
    relative = PurePosixPath(path)
    parts = list(relative.parts)
    if not parts:
        return set()
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = PurePosixPath(parts[-1]).stem
    if not parts:
        return set()
    names = {".".join(parts)}
    if parts[0] == "src" and len(parts) > 1:
        names.add(".".join(parts[1:]))
    return names


def _python_import_base(path: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    relative = PurePosixPath(path)
    parts = list(relative.parts)
    package = parts[:-1]
    keep = max(0, len(package) - (level - 1))
    base = package[:keep]
    if parts[:1] == ["src"] and len(base) > 1:
        base = base[1:]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _resolve_js_import(source: str, specifier: str, files: list[str]) -> str | None:
    if not specifier.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if base == ".." or base.startswith(("../", "/")):
        return None
    file_set = set(files)
    suffix = PurePosixPath(base).suffix.lower()
    candidates = [base] if suffix else [base + ext for ext in (".js", ".jsx", ".ts", ".tsx")]
    if not suffix:
        candidates.extend(
            posixpath.join(base, "index" + ext)
            for ext in (".js", ".jsx", ".ts", ".tsx")
        )
    return next((candidate for candidate in candidates if candidate in file_set), None)


def reconstruct_indexed_contents(
    chunks: list[dict[str, Any]], paths: Iterable[str]
) -> dict[str, str]:
    """Reassemble source text from line-addressed chunks in an existing index."""
    allowed = set(paths)
    lines_by_path: dict[str, dict[int, str]] = defaultdict(dict)
    for chunk in chunks:
        path = chunk["path"]
        if path not in allowed or chunk.get("source_type") != "code":
            continue
        for offset, line in enumerate(chunk["content"].splitlines()):
            lines_by_path[path].setdefault(chunk["start_line"] + offset, line)
    contents = {}
    for path in allowed:
        lines = lines_by_path.get(path, {})
        contents[path] = "\n".join(
            lines.get(number, "") for number in range(1, max(lines, default=0) + 1)
        )
    return contents


def indexed_readme(chunks: list[dict[str, Any]]) -> str:
    candidates = [
        chunk for chunk in chunks
        if chunk.get("source_type") == "documentation"
        and PurePosixPath(chunk["path"]).parent == PurePosixPath(".")
        and PurePosixPath(chunk["path"]).stem.lower() == "readme"
    ]
    return "\n".join(chunk["content"] for chunk in sorted(
        candidates, key=lambda item: item["start_line"]
    ))


def generate_diagram(
    repository: Path,
    repository_identity: str,
    commit_sha: str | None,
    all_paths: Iterable[str],
    contents: Mapping[str, str],
    readme: str,
    store: VectorStore,
    settings: Settings,
    *,
    stored_hashes: Mapping[str, str] | None = None,
    llm_complete: Callable[[str, Settings], str] = complete_json,
    include_tests: bool = False,
    run_diagnostics: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Generate or load a diagram using only the supplied repository snapshot."""
    paths = sorted(set(all_paths))
    source_files = analysed_paths(paths, include_tests=include_tests)
    digest = content_digest(
        source_files, contents, stored_hashes, include_tests=include_tests
    )
    key = cache_key(repository_identity, commit_sha, digest)
    cached = store.value(key)
    if cached:
        payload = json.loads(cached)
        validate_mermaid(payload.get("mermaid", ""), payload.get("nodes", []),
                         payload.get("groups", []), payload.get("edges", []))
        return {**payload, "llm_calls_made": 0, "cache_hit": True}

    raw_edges = extract_imports(contents, include_tests=include_tests)
    file_edges = [edge for edge in raw_edges
                  if edge["source_file"] in source_files and edge["target_file"] in source_files]
    nodes = _make_nodes(source_files)
    not_analysed = _not_analysed(paths)
    chunks = [chunk for chunk in store.all() if chunk.get("source_type") == "code"]
    chunks_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_path[chunk["path"]].append(chunk)
    for values in chunks_by_path.values():
        values.sort(key=lambda item: (item["start_line"], item["id"]))

    llm_calls = 0
    labels_status = "llm"
    if settings.llm_api_key and nodes:
        try:
            raw_groups, count = _name_groups(
                nodes, readme, settings, llm_complete, run_diagnostics
            )
            llm_calls += count
            groups = _number_groups(raw_groups)
            label_tasks = _label_tasks(
                nodes, file_edges, groups, chunks_by_path, store, settings
            )
            model_tasks = [task for task in label_tasks if task["evidence"]]
            model_labels, count = _label_batches(
                model_tasks, settings, llm_complete, run_diagnostics
            )
            llm_calls += count
            labels = {**_fallback_labels(
                [task for task in label_tasks if not task["evidence"]], nodes
            ), **_labels_by_id(model_labels)}
            if len(model_tasks) != len(label_tasks):
                labels_status = "fallback (no indexed chunk for some labels)"
        except _LLMValidationError as error:
            llm_calls += error.calls
            groups = _number_groups(_fallback_groups(nodes))
            label_tasks = _label_tasks(
                nodes, file_edges, groups, chunks_by_path, store, settings,
                retrieve_evidence=False,
            )
            labels = _fallback_labels(label_tasks, nodes)
            labels_status = f"fallback ({error.reason})"
    elif nodes:
        groups = _number_groups(_fallback_groups(nodes))
        label_tasks = _label_tasks(
            nodes, file_edges, groups, chunks_by_path, store, settings,
            retrieve_evidence=False,
        )
        labels = _fallback_labels(label_tasks, nodes)
        labels_status = "fallback (no LLM key)"
    else:
        groups = []
        labels = {}
        labels_status = "no source nodes"

    edges = _display_edges(nodes, file_edges, groups)
    display_labels = _display_labels(labels)
    payload = {
        "repository": repository_identity,
        "commit_sha": commit_sha,
        "content_digest": digest,
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "labels": display_labels,
        "citations": _citations(display_labels["nodes"] | display_labels["edges"]),
        "not_analysed": not_analysed,
        "labels_status": labels_status,
        "mermaid": render_mermaid(nodes, edges, groups, display_labels["nodes"] | display_labels["edges"]),
    }
    validate_diagram(payload, set(source_files), file_edges)
    store.set_value(key, json.dumps(payload, sort_keys=True))
    return {**payload, "llm_calls_made": llm_calls, "cache_hit": False}


def _not_analysed(paths: Iterable[str]) -> list[dict[str, Any]]:
    extensions = Counter(
        source_extension(path) or "[no extension]"
        for path in paths if source_extension(path) not in SOURCE_EXTENSIONS
    )
    return [
        {"extension": extension, "file_count": count}
        for extension, count in sorted(extensions.items())
    ]


def _make_nodes(source_files: list[str]) -> list[dict[str, Any]]:
    active = [{"path": path, "files": [path]} for path in sorted(set(source_files))]
    while len(active) > MAX_NODES:
        candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        max_depth = max(
            (len(PurePosixPath(node["path"]).parts) for node in active), default=0
        )
        found_at_depth = False
        for depth in range(1, max_depth + 1):
            candidates.clear()
            for node in active:
                node_path = PurePosixPath(node["path"])
                folder = node_path.parent
                for _ in range(depth - 1):
                    folder = folder.parent
                candidates[folder.as_posix()].append(node)
            if any(len(nodes) > 1 for nodes in candidates.values()):
                found_at_depth = True
                break
        if not found_at_depth:
            break

        folder, selected = min(
            (
                (folder, nodes) for folder, nodes in candidates.items()
                if len(nodes) > 1
            ),
            key=lambda item: (-sum(len(node["files"]) for node in item[1]), item[0]),
        )
        selected_ids = {id(node) for node in selected}
        merged = {
            "path": folder,
            "files": sorted(path for node in selected for path in node["files"]),
        }
        active = [node for node in active if id(node) not in selected_ids] + [merged]

    active.sort(key=lambda node: (node["path"], node["files"]))
    return [{"id": f"n{index}", **node} for index, node in enumerate(active, 1)]


def _candidate_groups(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        path = node["path"]
        folder = path if path not in node["files"] else PurePosixPath(path).parent.as_posix()
        groups[folder].append(node["id"])
    return [
        {"folder": folder, "node_ids": sorted(node_ids)}
        for folder, node_ids in sorted(groups.items())
    ]


def _name_groups(
    nodes: list[dict[str, Any]], readme: str, settings: Settings,
    llm_complete: Callable[[str, Settings], str],
    run_diagnostics: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    candidates = _candidate_groups(nodes)
    node_files = {node["id"]: node["files"] for node in nodes}
    prompt = (
        "Name the existing folder groups and merge groups only when their files clearly belong together. "
        "Do not add, remove, or rename node ids. Return JSON only as "
        '{"groups":[{"name":"...","node_ids":["n1"]}]}. '
        f"Candidate groups: {json.dumps(candidates, ensure_ascii=False)}\n"
        f"Node files: {json.dumps(node_files, ensure_ascii=False)}\n"
        f"README: {readme[:12000]}"
    )
    result, calls = _validated_json(
        prompt, settings, llm_complete, lambda value: _validate_groups(value, nodes),
        run_diagnostics=run_diagnostics, phase="groups",
    )
    return result, calls


def _validate_groups(value: Any, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
        raise TypeError("Expected a groups array.")
    if set(value) != {"groups"}:
        raise ValueError("Group output cannot add fields or diagram entities.")
    expected = {node["id"] for node in nodes}
    seen: list[str] = []
    groups = []
    for item in value["groups"]:
        if not isinstance(item, dict) or not isinstance(item.get("node_ids"), list):
            raise TypeError("Each group must contain a node_ids array.")
        if set(item) != {"name", "node_ids"}:
            raise ValueError("Groups may contain only a name and existing node ids.")
        name = sanitize_label(item.get("name", ""))
        if not name:
            raise ValueError("Group names cannot be empty.")
        ids = item["node_ids"]
        if not ids:
            raise ValueError("Groups cannot be empty.")
        if any(not isinstance(node_id, str) or node_id not in expected for node_id in ids):
            raise ValueError("A group contains a node id that does not exist.")
        seen.extend(ids)
        groups.append({"name": name, "node_ids": ids})
    if set(seen) != expected or len(seen) != len(expected):
        raise ValueError("Every node must appear in exactly one group.")
    return groups


def _fallback_groups(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for candidate in _candidate_groups(nodes):
        folder = candidate["folder"]
        name = PurePosixPath(folder).name if folder != "." else "Repository root"
        groups.append({"name": name, "node_ids": candidate["node_ids"]})
    return groups


def _label_tasks(
    nodes: list[dict[str, Any]], file_edges: list[dict[str, Any]],
    groups: list[dict[str, Any]], chunks_by_path: Mapping[str, list[dict[str, Any]]],
    store: VectorStore, settings: Settings, *, retrieve_evidence: bool = True,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for node in nodes:
        evidence = _node_evidence(node, chunks_by_path, store, settings, retrieve_evidence)
        tasks.append({"id": node["id"], "kind": "node", "files": node["files"],
                      "evidence": evidence})
    edges = _display_edges(nodes, file_edges, groups)
    for edge in edges:
        evidence = _edge_evidence(edge, file_edges, chunks_by_path, store, settings,
                                  retrieve_evidence)
        tasks.append({"id": edge["id"], "kind": "edge", "files": edge["file_edges"],
                      "evidence": evidence})
    return tasks


def _node_evidence(
    node: dict[str, Any], chunks_by_path: Mapping[str, list[dict[str, Any]]],
    store: VectorStore, settings: Settings, use_retrieval: bool,
) -> list[dict[str, Any]]:
    candidates = [chunk for path in node["files"] for chunk in chunks_by_path.get(path, [])]
    ranked: list[dict[str, Any]] = []
    if use_retrieval and candidates:
        query = "purpose behavior " + " ".join(node["files"])
        results = retrieve(query, store, limit=20, embedding_model=settings.embedding_model,
                           reranker_model=settings.reranker_model)
        allowed = set(node["files"])
        ranked = [item for item in results if item["path"] in allowed]
    selected = ranked or candidates
    return [_chunk_evidence(item) for item in selected[:2]]


def _edge_evidence(
    edge: dict[str, Any], file_edges: list[dict[str, Any]],
    chunks_by_path: Mapping[str, list[dict[str, Any]]], store: VectorStore,
    settings: Settings, use_retrieval: bool,
) -> list[dict[str, Any]]:
    source_files = sorted({item["source_file"] for item in edge["file_edges"]})
    related = [item for item in file_edges if item["source_file"] in source_files]
    query = " ".join(
        f"{item['statement']} {item['target_file']}" for item in related
    )
    ranked: list[dict[str, Any]] = []
    if use_retrieval and query:
        results = retrieve(query, store, limit=30, embedding_model=settings.embedding_model,
                           reranker_model=settings.reranker_model)
        allowed = set(source_files)
        ranked = [item for item in results if item["path"] in allowed]
    candidates = [chunk for path in source_files for chunk in chunks_by_path.get(path, [])]
    selected = ranked or candidates
    return [_chunk_evidence(item) for item in selected[:4]]


def _chunk_evidence(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": int(chunk["id"]),
        "path": chunk["path"],
        "start_line": int(chunk["start_line"]),
        "end_line": int(chunk["end_line"]),
        "content": chunk["content"][:5000],
    }


def _label_batches(
    tasks: list[dict[str, Any]], settings: Settings,
    llm_complete: Callable[[str, Settings], str],
    run_diagnostics: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    labels = []
    calls = 0
    for offset in range(0, len(tasks), LABEL_BATCH_SIZE):
        batch = tasks[offset:offset + LABEL_BATCH_SIZE]
        prompt = (
            "Write labels only for the supplied node and edge ids. Never add ids, nodes, or edges. "
            "Node phrases must be at most 5 words and copied from one supplied chunk. "
            "Edge labels must be verb phrases of at most 4 words, supported by the supplied import code. "
            "Cite one supplied chunk id for each label. Return JSON only as "
            '{"labels":[{"id":"n1","phrase":"...","chunk_id":1}]}. '
            f"Items and evidence: {json.dumps(batch, ensure_ascii=False)}"
        )
        try:
            result, made = _validated_json(
                prompt, settings, llm_complete,
                lambda value, current=batch: _validate_labels(value, current),
                run_diagnostics=run_diagnostics, phase="labels",
            )
        except _LLMValidationError as error:
            raise _LLMValidationError(error.reason, calls + error.calls) from error
        calls += made
        labels.extend(result)
    return labels, calls


def _validate_labels(value: Any, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("labels"), list):
        raise TypeError("Expected a labels array.")
    if set(value) != {"labels"}:
        raise ValueError("Label output cannot add nodes, edges, or other fields.")
    expected = {task["id"]: task for task in tasks}
    labels = []
    seen = []
    for item in value["labels"]:
        if not isinstance(item, dict) or item.get("id") not in expected:
            raise ValueError(f"Label {item.get('id') if isinstance(item, dict) else '?'} refers to an unknown node or edge id.")
        if set(item) != {"id", "phrase", "chunk_id"}:
            raise ValueError(f"Label {item['id']} contains fields outside the allowed id, phrase, and chunk id.")
        task = expected[item["id"]]
        phrase = sanitize_label(item.get("phrase", ""))
        limit = 5 if task["kind"] == "node" else 4
        if not phrase or len(WORD_RE.findall(phrase)) > limit:
            raise ValueError(f"Label {item['id']} is empty or exceeds the {limit}-word {task['kind']} limit.")
        chunk_id = item.get("chunk_id")
        allowed = {entry["chunk_id"] for entry in task["evidence"]}
        if chunk_id not in allowed:
            raise ValueError(f"Label {item['id']} cites a chunk that was not supplied for its item.")
        cited = next(entry for entry in task["evidence"] if entry["chunk_id"] == chunk_id)
        if task["kind"] == "node" and not _phrase_in_text(phrase, cited["content"]):
            raise ValueError(f"Node label {item['id']} must be copied from its cited chunk.")
        if task["kind"] == "edge" and not WORD_RE.findall(cited["content"]):
            raise ValueError(f"Edge label {item['id']} must cite a source chunk with code.")
        if task["kind"] == "edge":
            first_word = WORD_RE.findall(phrase.lower())[0]
            if first_word not in EDGE_VERBS and not first_word.endswith(("s", "ed", "ing")):
                raise ValueError(f"Edge label {item['id']} must begin with a verb.")
        labels.append({
            "id": item["id"],
            "kind": task["kind"],
            "phrase": phrase,
            "chunk_id": int(chunk_id),
            "path": cited["path"],
            "start_line": cited["start_line"],
            "end_line": cited["end_line"],
        })
        seen.append(item["id"])
    if set(seen) != set(expected) or len(seen) != len(expected):
        raise ValueError("Every supplied node and edge must have exactly one label.")
    return labels


def _phrase_in_text(phrase: str, content: str) -> bool:
    normalize = lambda value: " ".join(WORD_RE.findall(value.lower()))
    return f" {normalize(phrase)} " in f" {normalize(content)} "


def _validated_json(
    prompt: str, settings: Settings, llm_complete: Callable[[str, Settings], str],
    validator: Callable[[Any], Any],
    *, run_diagnostics: list[dict[str, str]] | None = None, phase: str,
) -> tuple[Any, int]:
    error_message = ""
    for attempt in range(2):
        try:
            if attempt and run_diagnostics is not None:
                run_diagnostics.append({"type": "retry", "phase": phase})
            response = llm_complete(
                prompt if not error_message else f"{prompt}\nPrevious response error: {error_message}",
                settings,
            )
            value = json.loads(response)
            return validator(value), attempt + 1
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            error_message = str(error)
            if run_diagnostics is not None:
                run_diagnostics.append({
                    "type": "rejected_response", "phase": phase,
                    "attempt": str(attempt + 1), "reason": error_message[:240],
                })
    raise _LLMValidationError(error_message, 2)


class _LLMValidationError(ValueError):
    def __init__(self, reason: str, calls: int):
        super().__init__(reason)
        self.reason = reason
        self.calls = calls


def _labels_by_id(labels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in labels}


def _fallback_labels(
    tasks: list[dict[str, Any]], nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node_by_id = {node["id"]: node for node in nodes}
    labels = {}
    for task in tasks:
        if task["kind"] == "node":
            node = node_by_id[task["id"]]
            path = node["files"][0]
            phrase = _filename_words(PurePosixPath(path).stem)
            evidence = task["evidence"]
        else:
            source_path = task["files"][0]["source_file"] if task["files"] else "module"
            target_path = task["files"][0]["target_file"] if task["files"] else "module"
            phrase = f"imports {_filename_words(PurePosixPath(target_path).stem)}"
            path = source_path
            evidence = task["evidence"]
        phrase = " ".join(WORD_RE.findall(phrase)[:5 if task["kind"] == "node" else 4]) or "source"
        chunk = evidence[0] if evidence else None
        labels[task["id"]] = {
            "id": task["id"],
            "kind": task["kind"],
            "phrase": phrase,
            "chunk_id": chunk["chunk_id"] if chunk else None,
            "path": chunk["path"] if chunk else path,
            "start_line": chunk["start_line"] if chunk else None,
            "end_line": chunk["end_line"] if chunk else None,
        }
    return labels


def _filename_words(value: str) -> str:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return " ".join(WORD_RE.findall(split.replace("_", " ").replace("-", " ").lower())[:5])


def sanitize_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(UNSAFE_LABEL_CHARS.sub(" ", value).split())


def _number_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(groups, key=lambda group: min(group["node_ids"], default=""))
    numbered = []
    for group in ordered:
        group_id = f"g{len(numbered) + 1}"
        numbered.append({"id": group_id, "name": group["name"],
                         "node_ids": sorted(group["node_ids"])})
    return numbered


def _display_edges(
    nodes: list[dict[str, Any]], file_edges: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    node_for_file = {path: node["id"] for node in nodes for path in node["files"]}
    group_for_node = {
        node_id: group["id"] for group in groups for node_id in group["node_ids"]
    }
    base: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in file_edges:
        source = node_for_file.get(edge["source_file"])
        target = node_for_file.get(edge["target_file"])
        if source and target and source != target:
            base[(source, target)].append(edge)
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    by_group_target: dict[tuple[int, str], list[tuple[str, str]]] = defaultdict(list)
    for source, target in sorted(base):
        source_group = group_for_node.get(source)
        target_group = group_for_node.get(target)
        if source_group is not None and source_group != target_group:
            by_group_target[(source_group, target)].append((source, target))
    merged_pairs = {
        pair for pairs in by_group_target.values() if len({source for source, _ in pairs}) > 1
        for pair in pairs
    }
    for (source, target), edges in sorted(base.items()):
        if (source, target) in merged_pairs:
            continue
        aggregate[(source, target)] = {
            "source": source, "target": target, "source_nodes": [source],
            "file_edges": edges,
        }
    for (group_index, target), pairs in sorted(by_group_target.items()):
        if len({source for source, _ in pairs}) <= 1:
            continue
        source_nodes = sorted({source for source, _ in pairs})
        edge_sources = [edge for pair in pairs for edge in base[pair]]
        group_id = group_index
        aggregate[(group_id, target)] = {
            "source": group_id, "target": target,
            "source_nodes": source_nodes, "file_edges": edge_sources,
        }
    return [
        {"id": f"e{index}", **edge}
        for index, edge in enumerate(sorted(aggregate.values(),
                                            key=lambda item: (item["source"], item["target"])), 1)
    ]


def render_mermaid(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
    groups: list[dict[str, Any]], labels: Mapping[str, dict[str, Any]],
) -> str:
    lines = ["flowchart LR"]
    for group in groups:
        group_id = group["id"]
        lines.append(f'  subgraph {group_id}["{sanitize_label(group["name"])}"]')
        lines.append("    direction TB")
        for node_id in group["node_ids"]:
            label = labels[node_id]
            display = sanitize_label(label["phrase"])
            filename = sanitize_label(PurePosixPath(label["path"]).name)
            display = f"{display} [{filename}]"
            lines.append(f'    {node_id}["{display}"]')
        lines.append("  end")
    for edge in edges:
        label = sanitize_label(labels[edge["id"]]["phrase"])
        lines.append(f'  {edge["source"]} -->|{label}| {edge["target"]}')
    return "\n".join(lines) + "\n"


def _citations(labels: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    citations = {}
    for label in labels.values():
        if label.get("chunk_id") is None:
            continue
        citation = {
            "chunk_id": label["chunk_id"], "path": label["path"],
            "start_line": label["start_line"], "end_line": label["end_line"],
        }
        citations[(citation["chunk_id"], citation["path"])] = citation
    return sorted(citations.values(), key=lambda item: (item["path"], item["start_line"] or 0))


def _display_labels(labels: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {"nodes": {}, "edges": {}}
    for label_id, label in labels.items():
        item = dict(label)
        if item["kind"] == "node":
            filename = sanitize_label(PurePosixPath(item["path"]).name)
            item["text"] = f"{item['phrase']} [{filename}]"
            result["nodes"][label_id] = item
        else:
            item["text"] = item["phrase"]
            result["edges"][label_id] = item
    return result


def validate_diagram(
    payload: dict[str, Any], source_files: set[str],
    file_edges: list[dict[str, Any]],
) -> None:
    node_ids = {node["id"] for node in payload["nodes"]}
    if len(node_ids) != len(payload["nodes"]) or any(
        not re.fullmatch(r"n[1-9][0-9]*", node_id) for node_id in node_ids
    ):
        raise ValueError("Diagram node ids must be unique generated n1, n2, ... ids.")
    for node in payload["nodes"]:
        if not node["files"] or not set(node["files"]).issubset(source_files):
            raise ValueError(f"Diagram node {node['id']} has no real source files.")
        if node["path"] != "." and node["path"] not in source_files and not all(
            file.startswith(node["path"].rstrip("/") + "/") for file in node["files"]
        ):
            raise ValueError(f"Diagram node {node['id']} does not map to a file or folder.")
    grouped = [node_id for group in payload["groups"] for node_id in group["node_ids"]]
    if set(grouped) != node_ids or len(grouped) != len(node_ids):
        raise ValueError("Every diagram node must belong to exactly one group.")
    group_ids = {group["id"] for group in payload["groups"]}
    if len(group_ids) != len(payload["groups"]) or any(
        not re.fullmatch(r"g[1-9][0-9]*", group_id) for group_id in group_ids
    ):
        raise ValueError("Diagram group ids must be unique generated g1, g2, ... ids.")
    allowed_edges = {(edge["source_file"], edge["target_file"]) for edge in file_edges}
    node_for_file = {path: node["id"] for node in payload["nodes"] for path in node["files"]}
    nodes_for_group = {group["id"]: set(group["node_ids"]) for group in payload["groups"]}
    edge_ids = set()
    for edge in payload["edges"]:
        if edge["id"] in edge_ids or not re.fullmatch(r"e[1-9][0-9]*", edge["id"]):
            raise ValueError("Diagram edge ids must be unique generated e1, e2, ... ids.")
        edge_ids.add(edge["id"])
        if edge["target"] not in node_ids:
            raise ValueError("Diagram edge target is not a node.")
        if edge["source"] not in node_ids and edge["source"] not in {
            group["id"] for group in payload["groups"]
        }:
            raise ValueError("Diagram edge source is not a node or group.")
        if not edge["file_edges"] or any(
            (item["source_file"], item["target_file"]) not in allowed_edges
            for item in edge["file_edges"]
        ):
            raise ValueError("Diagram edge is not backed by the extracted import graph.")
        expected_sources = {node_for_file[item["source_file"]] for item in edge["file_edges"]}
        expected_targets = {node_for_file[item["target_file"]] for item in edge["file_edges"]}
        if expected_targets != {edge["target"]}:
            raise ValueError("Diagram edge target does not match the imported files.")
        if edge["source"] in group_ids:
            if not expected_sources.issubset(nodes_for_group[edge["source"]]):
                raise ValueError("Merged diagram edge does not match its source group.")
        elif expected_sources != {edge["source"]}:
            raise ValueError("Diagram edge source does not match its imported files.")
    for kind in ("nodes", "edges"):
        expected_ids = node_ids if kind == "nodes" else edge_ids
        if set(payload["labels"][kind]) != expected_ids:
            raise ValueError(f"{kind.title()} and labels do not match.")
        for entity_id, label in payload["labels"][kind].items():
            if not label.get("phrase"):
                raise ValueError(f"{kind[:-1].title()} label cannot be empty.")
            limit = 5 if kind == "nodes" else 4
            if len(WORD_RE.findall(label["phrase"])) > limit:
                raise ValueError(f"{kind[:-1].title()} label exceeds {limit} words.")
            if entity_id not in node_ids and kind == "nodes":
                raise ValueError("A node label refers to an unknown node.")
            if not re.fullmatch(r"e[1-9][0-9]*", entity_id) and kind == "edges":
                raise ValueError("An edge label refers to an unknown edge.")
    validate_mermaid(payload["mermaid"], payload["nodes"], payload["groups"], payload["edges"])


def validate_mermaid(
    text: str, nodes: list[dict[str, Any]], groups: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Check the constrained syntax emitted by render_mermaid, not Mermaid rendering."""
    lines = text.splitlines()
    if not lines or lines[0] != "flowchart LR" or not text.endswith("\n"):
        raise ValueError("Mermaid text does not match the flowchart template.")
    expected_nodes = {node["id"] for node in nodes}
    expected_groups = {group["id"] for group in groups}
    expected_edges = {(edge["source"], edge["target"]) for edge in edges}
    found_nodes: set[str] = set()
    found_groups: set[str] = set()
    found_edges: set[tuple[str, str]] = set()
    in_group = False
    for line in lines[1:]:
        if re.fullmatch(r'  subgraph g[1-9][0-9]*\["[^"#;|\r\n]*"\]', line):
            group_id = re.search(r"subgraph (g[1-9][0-9]*)", line).group(1)
            if in_group or group_id in found_groups:
                raise ValueError("Mermaid group structure is invalid.")
            found_groups.add(group_id)
            in_group = True
        elif line == "    direction TB":
            if not in_group:
                raise ValueError("Mermaid direction must be inside a group.")
        elif re.fullmatch(r'    n[1-9][0-9]*\["[^"#;|\r\n]*"\]', line):
            if not in_group:
                raise ValueError("Mermaid nodes must be inside a group.")
            node_id = re.match(r"    (n[1-9][0-9]*)", line).group(1)
            if node_id in found_nodes:
                raise ValueError("Mermaid node ids must be unique.")
            found_nodes.add(node_id)
        elif line == "  end":
            if not in_group:
                raise ValueError("Mermaid group closing token has no group.")
            in_group = False
        elif re.fullmatch(r"  (?:n[1-9][0-9]*|g[1-9][0-9]*) -->\|[^|#;\r\n]*\| n[1-9][0-9]*", line):
            match = re.match(r"  ((?:n|g)[1-9][0-9]*) -->\|", line)
            target = re.search(r"\| (n[1-9][0-9]*)$", line).group(1)
            found_edges.add((match.group(1), target))
        else:
            raise ValueError("Mermaid text contains syntax outside the generated template.")
    if in_group or found_nodes != expected_nodes or found_groups != expected_groups:
        raise ValueError("Mermaid nodes or groups do not match the JSON output.")
    if found_edges != expected_edges:
        raise ValueError("Mermaid edges do not match the JSON output.")

