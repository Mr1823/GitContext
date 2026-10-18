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
from urllib.parse import urlsplit

from gitcontext.answer import complete_json
from gitcontext.config import Settings
from gitcontext.retrieval import retrieve
from gitcontext.vector_store import VectorStore

DIAGRAM_VERSION = 6
MAX_NODES = 25
MIN_GROUPS = 5
MAX_GROUPS = 8
LABEL_BATCH_SIZE = 10
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
JS_IMPORT_RE = re.compile(
    r"\bimport\s+(?:[\s\S]*?\s+from\s+)?(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)"
)
JS_REQUIRE_RE = re.compile(
    r"\brequire\s*\(\s*(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*\)"
)
JS_DYNAMIC_IMPORT_RE = re.compile(
    r"\bimport\s*\(\s*(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*\)"
)
JS_EXPORT_RE = re.compile(
    r"\bexport\s+[^;\n]*?\s+from\s*(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)"
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
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
JS_CALL_RE = re.compile(
    r"(?P<callee>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\("
)


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
    repository_identity: str, commit_sha: str | None, digest: str,
    extra_digest: str = "",
) -> str:
    if commit_sha:
        identity = (
            f"{repository_identity}\0{commit_sha}\0{digest}\0{extra_digest}\0"
            f"{DIAGRAM_VERSION}"
        )
    else:
        identity = f"{digest}\0{extra_digest}\0{DIAGRAM_VERSION}"
    return "diagram:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def extract_imports(
    contents: Mapping[str, str], *, include_tests: bool = False,
    repository: Path | None = None,
) -> list[dict[str, Any]]:
    """Return deduplicated file-to-file imports for Python, JS, and TS."""
    files = analysed_paths(contents, include_tests=include_tests)
    edges, _ = _extract_import_graph(contents, files, repository)
    return edges


def extract_unresolved_imports(
    contents: Mapping[str, str], paths: Iterable[str], repository: Path | None = None,
    *, include_tests: bool = False,
) -> list[dict[str, Any]]:
    """List local JavaScript/TypeScript imports that do not resolve to a file."""
    _, unresolved = _extract_import_graph(
        contents, analysed_paths(paths, include_tests=include_tests), repository
    )
    return unresolved


def _extract_import_graph(
    contents: Mapping[str, str], files: list[str], repository: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    module_paths: dict[str, set[str]] = defaultdict(set)
    for path in files:
        if source_extension(path) == ".py":
            for module in _python_module_names(path):
                module_paths[module].add(path)

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved: dict[tuple[str, int, str], dict[str, Any]] = {}
    aliases = _path_aliases(repository)
    file_set = set(files)
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
            matches = (
                list(JS_IMPORT_RE.finditer(source))
                + list(JS_EXPORT_RE.finditer(source))
                + list(JS_REQUIRE_RE.finditer(source))
                + list(JS_DYNAMIC_IMPORT_RE.finditer(source))
            )
            seen_specifiers: set[tuple[int, str]] = set()
            for match in sorted(matches, key=lambda item: item.start()):
                specifier = match.group("path")
                line = source.count("\n", 0, match.start()) + 1
                if (line, specifier) in seen_specifiers:
                    continue
                seen_specifiers.add((line, specifier))
                local = _is_local_import(specifier, aliases)
                if not local:
                    continue
                target, exists = _resolve_local_import(
                    path, specifier, file_set, repository, aliases
                )
                if target:
                    statement = source.splitlines()[line - 1].strip()
                    edges.setdefault((path, target), {
                        "source_file": path,
                        "target_file": target,
                        "line": line,
                        "statement": statement,
                    })
                elif not exists:
                    unresolved[(path, line, specifier)] = {
                        "source_file": path,
                        "line": line,
                        "specifier": specifier,
                    }
    return [edges[key] for key in sorted(edges)], [
        unresolved[key] for key in sorted(unresolved)
    ]


def _path_aliases(repository: Path | None) -> list[tuple[str, str]]:
    aliases: dict[str, str] = {}
    if repository is not None:
        root = Path(repository)
        default_root = "src" if (root / "src").is_dir() else "."
        aliases.update({"@/": default_root, "~/": default_root})
        for config_name in ("tsconfig.json", "jsconfig.json"):
            config_path = root / config_name
            try:
                config = json.loads(_strip_json_comments(config_path.read_text("utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            compiler = config.get("compilerOptions", {})
            base_url = (root / compiler.get("baseUrl", ".")).resolve()
            for alias, targets in compiler.get("paths", {}).items():
                if not isinstance(alias, str) or not isinstance(targets, list):
                    continue
                prefix = alias.split("*", 1)[0]
                for target in targets:
                    if isinstance(target, str):
                        aliases[prefix] = (base_url / target.split("*", 1)[0]).resolve().as_posix()
    return sorted(aliases.items(), key=lambda item: -len(item[0]))


def _strip_json_comments(value: str) -> str:
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    return re.sub(r"(^|\s)//.*?$", r"\1", value, flags=re.MULTILINE)


def _is_local_import(specifier: str, aliases: list[tuple[str, str]]) -> bool:
    return specifier.startswith(("./", "../", "/", "src/")) or any(
        prefix and specifier.startswith(prefix) for prefix, _ in aliases
    )


def _resolve_local_import(
    source: str, specifier: str, files: set[str], repository: Path | None,
    aliases: list[tuple[str, str]],
) -> tuple[str | None, bool]:
    roots: list[Path | PurePosixPath] = []
    if specifier.startswith(("./", "../")):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
        if base != ".." and not base.startswith(("../", "/")):
            roots.append(PurePosixPath(base))
    else:
        matched = False
        for prefix, target_root in aliases:
            if prefix and specifier.startswith(prefix):
                suffix = specifier[len(prefix):]
                if repository is not None and Path(target_root).is_absolute():
                    roots.append(Path(target_root) / suffix)
                else:
                    roots.append(PurePosixPath(posixpath.join(target_root, suffix)))
                matched = True
                break
        if not matched and specifier.startswith("src/"):
            roots.append(PurePosixPath(specifier))
        elif not matched and specifier.startswith("/"):
            roots.append(PurePosixPath(specifier.lstrip("/")))

    found_non_source = False
    for root in roots:
        candidates = _import_candidates(root)
        for candidate in candidates:
            relative = (
                candidate.resolve().relative_to(Path(repository).resolve()).as_posix()
                if isinstance(candidate, Path) and repository is not None
                and candidate.is_absolute()
                and _is_relative_to(candidate.resolve(), Path(repository).resolve())
                else candidate.as_posix()
            )
            if relative in files:
                return relative, True
            if (
                isinstance(candidate, Path) and candidate.is_file()
            ) or (
                repository is not None and (Path(repository) / relative).is_file()
            ):
                found_non_source = True
    return None, found_non_source


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _import_candidates(root: Path | PurePosixPath) -> list[Path | PurePosixPath]:
    raw = root.as_posix()
    has_extension = bool(PurePosixPath(raw).suffix)
    candidates: list[Path | PurePosixPath] = [root]
    extensions = (".js", ".jsx", ".ts", ".tsx")
    if not has_extension:
        candidates.extend(type(root)(raw + extension) for extension in extensions)
        candidates.extend(type(root)(posixpath.join(raw, "index" + extension))
                          for extension in extensions)
        candidates.append(type(root)(posixpath.join(raw, "index")))
    return candidates


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


def _split_js_arguments(source: str, opening: int) -> tuple[list[str], int]:
    arguments = []
    start = opening + 1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == ")" and not depth:
            arguments.append(source[start:index].strip())
            return arguments, index + 1
        elif char == "," and not depth:
            arguments.append(source[start:index].strip())
            start = index + 1
    return [], len(source)


def _iter_js_calls(source: str) -> Iterable[tuple[str, list[str], int, int]]:
    masked = _mask_js_literals(source)
    for match in JS_CALL_RE.finditer(masked):
        arguments, end = _split_js_arguments(source, match.end() - 1)
        if end > match.end():
            yield match.group("callee"), arguments, match.start(), end


def _mask_js_literals(source: str) -> str:
    output = list(source)
    index = 0
    quote = ""
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            if char != "\n":
                output[index] = " "
            if char == "\\":
                index += 1
                if index < len(source) and source[index] != "\n":
                    output[index] = " "
            elif char == quote:
                quote = ""
        elif char in ("'", '"', chr(96)):
            quote = char
            output[index] = " "
        elif char == "/" and next_char == "/":
            while index < len(source) and source[index] != "\n":
                output[index] = " "
                index += 1
            continue
        elif char == "/" and next_char == "*":
            output[index] = output[index + 1] = " "
            index += 2
            while index < len(source):
                if source[index] != "\n":
                    output[index] = " "
                if source[index:index + 2] == "*/":
                    output[index] = output[index + 1] = " "
                    index += 1
                    break
                index += 1
        index += 1
    return "".join(output)


def _matching_js_brace(source: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _fetch_wrappers(source: str) -> tuple[dict[str, str], list[tuple[int, int]]]:
    masked = _mask_js_literals(source)
    wrappers: dict[str, str] = {}
    ranges = []
    pattern = re.compile(
        r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*"
        r"\(([^)]*)\)\s*\{"
    )
    for match in pattern.finditer(masked):
        opening = match.end() - 1
        closing = _matching_js_brace(masked, opening)
        if closing is None:
            continue
        parameters = [part.strip() for part in match.group(2).split(",")]
        if not parameters or not re.fullmatch(r"[A-Za-z_$][\w$]*", parameters[0]):
            continue
        body_start, body_end = opening + 1, closing
        body = source[body_start:body_end]
        for callee, arguments, _, _ in _iter_js_calls(body):
            if callee != "fetch" or not arguments or arguments[0].strip() != parameters[0]:
                continue
            method = "GET"
            if len(arguments) > 1:
                match_method = re.search(
                    r"\bmethod\s*:\s*(['\"])(GET|POST|PUT|PATCH|DELETE)\1",
                    arguments[1], flags=re.IGNORECASE,
                )
                if match_method:
                    method = match_method.group(2).upper()
            wrappers[match.group(1)] = method
            ranges.append((match.start(), closing + 1))
            break
    return wrappers, ranges


def _js_string_literal(value: str) -> str | None:
    value = value.strip()
    if len(value) < 2 or value[0] not in "'\"`" or value[-1] != value[0]:
        return None
    content = value[1:-1]
    if value[0] == "`" and "${" in content:
        return None
    return (content.replace("\\n", "\n").replace("\\r", "\r")
            .replace("\\t", "\t").replace("\\'", "'")
            .replace('\\"', '"').replace("\\\\", "\\"))


def _known_js_values(contents: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    functions: dict[str, str] = {}
    for source in contents.values():
        patterns = (
            (
            r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"\([^)]*\)\s*=>\s*\{([^{}]*)\}"
            ),
            r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{([^{}]*)\}",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, source, flags=re.DOTALL):
                returned = re.search(r"\breturn\s+([^;\n]+)", match.group(2))
                if not returned:
                    continue
                expression = returned.group(1).strip()
                literal = _js_string_literal(expression)
                if literal is not None:
                    functions[match.group(1)] = literal
    for source in contents.values():
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)", source
        ):
            name, expression = match.group(1), match.group(2).strip()
            literal = _static_js_value(expression, values, functions)
            if literal is not None:
                values[name] = literal
    return values, functions


def _static_js_value(
    expression: str, values: Mapping[str, str], functions: Mapping[str, str],
) -> str | None:
    expression = expression.strip().strip("() ")
    literal = _js_string_literal(expression)
    if literal is not None:
        return literal
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expression):
        return values.get(expression)
    call = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\(\s*\)", expression)
    if call:
        return functions.get(call.group(1))
    return None


def _template_value(
    expression: str, values: Mapping[str, str], functions: Mapping[str, str],
) -> tuple[str | None, bool]:
    expression = expression.strip()
    if not (expression.startswith("`") and expression.endswith("`")):
        literal = _static_js_value(expression, values, functions)
        return literal, literal is None
    content = expression[1:-1]
    matches = list(re.finditer(r"\$\{([^{}]+)\}", content))
    if not matches:
        return content, False
    output = []
    cursor = 0
    unresolved_prefix = False
    for match in matches:
        before = content[cursor:match.start()]
        output.append(before)
        resolved = _static_js_value(match.group(1), values, functions)
        is_leading_prefix = not "".join(output).strip("/") and content[match.end():].startswith("/")
        if resolved is not None:
            output.append(resolved)
        elif is_leading_prefix:
            unresolved_prefix = True
        else:
            output.append(":param")
        cursor = match.end()
    output.append(content[cursor:])
    return "".join(output), unresolved_prefix


def _normalise_http_path(value: str) -> str:
    value = value.split("#", 1)[0].split("?", 1)[0].strip()
    if "://" in value:
        value = urlsplit(value).path or "/"
    value = re.sub(r"\$\{[^{}]*\}", ":param", value)
    value = re.sub(r"\{[^{}]+\}", ":param", value)
    value = re.sub(r"(?<=/):[A-Za-z_$][\w$]*", ":param", value)
    value = re.sub(r"/{2,}", "/", value)
    if not value.startswith("/"):
        value = "/" + value
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def _join_http_paths(prefix: str, path: str) -> str:
    if not prefix or prefix == "/":
        return _normalise_http_path(path)
    return _normalise_http_path(prefix.rstrip("/") + "/" + path.lstrip("/"))


def _resolve_call_path(
    expression: str, base_url: str | None, values: Mapping[str, str],
    functions: Mapping[str, str],
) -> tuple[str | None, bool]:
    request, unresolved_prefix = _template_value(expression, values, functions)
    if request is None:
        return None, False
    if "://" in request:
        return _normalise_http_path(request), False
    if base_url:
        resolved_base = _static_js_value(base_url, values, functions)
        if resolved_base is not None:
            return _join_http_paths(resolved_base, request), False
        if request.startswith("/"):
            return _normalise_http_path(request), True
        return None, False
    if request.startswith("/"):
        return _normalise_http_path(request), unresolved_prefix
    return None, False


def _js_bindings(
    source_path: str, source: str, paths: list[str], repository: Path | None,
) -> dict[str, str]:
    aliases = _path_aliases(repository)
    file_set = set(paths)
    bindings: dict[str, str] = {}
    import_pattern = re.compile(
        r"\bimport\s+(?P<bindings>[\s\S]*?)\s+from\s*"
        r"(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)"
    )
    for match in import_pattern.finditer(source):
        target, _ = _resolve_local_import(
            source_path, match.group("path"), file_set, repository, aliases
        )
        if not target:
            continue
        spec = match.group("bindings").strip()
        default = spec.split(",", 1)[0].strip()
        if default and not default.startswith(("{", "*")):
            bindings[default] = target
        namespace = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", spec)
        if namespace:
            bindings[namespace.group(1)] = target
        named = re.search(r"\{([^}]+)\}", spec)
        if named:
            for item in named.group(1).split(","):
                parts = re.split(r"\s+as\s+", item.strip())
                if parts and parts[-1].isidentifier():
                    bindings[parts[-1]] = target
    require_pattern = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*"
        r"(['\"])([^'\"]+)\2\s*\)"
    )
    for match in require_pattern.finditer(source):
        target, _ = _resolve_local_import(
            source_path, match.group(3), file_set, repository, aliases
        )
        if target:
            bindings[match.group(1)] = target
    return bindings


def _extract_express_routes(
    contents: Mapping[str, str], paths: list[str], repository: Path | None,
) -> tuple[list[dict[str, Any]], set[str], bool]:
    js_files = [path for path in paths if source_extension(path) in {".js", ".jsx", ".ts", ".tsx"}]
    sources = {path: contents.get(path, "") for path in js_files}
    imports = {path: _js_bindings(path, source, js_files, repository)
               for path, source in sources.items()}
    routes_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mounts = []
    direct_routes = []
    detected = any(re.search(r"\bexpress\b|\.Router\s*\(", source)
                   for source in sources.values())
    for path, source in sources.items():
        router_names = set(re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"(?:[A-Za-z_$][\w$]*\.)?Router\s*\(", source
        ))
        if not router_names and re.search(r"\brouter\s*\.\s*(?:get|post|put|patch|delete)\s*\(", source):
            router_names.add("router")
        for callee, arguments, start, _ in _iter_js_calls(source):
            line = source.count("\n", 0, start) + 1
            parts = callee.split(".")
            method = parts[-1].upper() if len(parts) > 1 else ""
            receiver = parts[-2] if len(parts) > 1 else ""
            if callee.endswith(".use") and receiver in {"app", "server", "api"} and len(arguments) >= 2:
                prefix = _js_string_literal(arguments[0])
                target = imports[path].get(arguments[1].strip())
                if target is None:
                    require_match = re.fullmatch(
                        r"require\s*\(\s*(['\"])([^'\"]+)\1\s*\)",
                        arguments[1].strip(),
                    )
                    if require_match:
                        target, _ = _resolve_local_import(
                            path, require_match.group(2), set(js_files), repository,
                            _path_aliases(repository),
                        )
                if prefix is not None and target:
                    mounts.append({
                        "file": target, "prefix": prefix, "line": line,
                        "source_file": path,
                    })
                    detected = True
                continue
            if method not in HTTP_METHODS or not arguments:
                continue
            if receiver not in router_names and receiver not in {"app", "server"}:
                continue
            route_path = _js_string_literal(arguments[0])
            if route_path is None:
                continue
            route = {
                "method": method, "path": _normalise_http_path(route_path),
                "file": path, "line": line, "prefix_only": False,
            }
            if receiver in {"app", "server"}:
                direct_routes.append(route)
            else:
                routes_by_file[path].append(route)
            detected = True

    routes = list(direct_routes)
    for mount in mounts:
        mounted = routes_by_file.get(mount["file"], [])
        routes.append({
            "method": "*", "path": _normalise_http_path(mount["prefix"]),
            "file": mount["file"], "line": mount["line"],
            "mount_file": mount["source_file"], "mount_line": mount["line"],
            "prefix_only": True,
        })
        if mounted:
            routes.extend({
                **route,
                "path": _join_http_paths(mount["prefix"], route["path"]),
                "mount_file": mount["source_file"], "mount_line": mount["line"],
            } for route in mounted)
    return routes, {mount["file"] for mount in mounts} | {
        route["file"] for path_routes in routes_by_file.values() for route in path_routes
    } | {route["file"] for route in direct_routes} | {
        mount["source_file"] for mount in mounts
    }, detected


def _extract_fastapi_routes(
    contents: Mapping[str, str], paths: list[str],
) -> tuple[list[dict[str, Any]], set[str], bool]:
    routes: list[dict[str, Any]] = []
    backend_files: set[str] = set()
    detected = False
    python_files = [path for path in paths if source_extension(path) == ".py"]
    module_paths: dict[str, str] = {}
    for path in python_files:
        for module in _python_module_names(path):
            module_paths[module] = path
    trees: dict[str, ast.Module] = {}
    routers: dict[tuple[str, str], dict[str, Any]] = {}
    for path in python_files:
        source = contents.get(path, "")
        if "fastapi" in source.lower() or "apirouter" in source.lower():
            detected = True
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        trees[path] = tree
        router_prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if (not isinstance(node, (ast.Assign, ast.AnnAssign))
                    or not isinstance(node.value, ast.Call)):
                continue
            call_name = _ast_call_name(node.value.func)
            if call_name.split(".")[-1] != "APIRouter":
                continue
            prefix_node = next((keyword.value for keyword in node.value.keywords
                                if keyword.arg == "prefix"), None)
            prefix = ""
            if prefix_node is not None:
                if not isinstance(prefix_node, ast.Constant) or not isinstance(
                    prefix_node.value, str
                ):
                    continue
                prefix = prefix_node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    router_prefixes[target.id] = prefix
                    routers[(path, target.id)] = {"prefix": prefix, "routes": []}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (not isinstance(decorator, ast.Call)
                        or not isinstance(decorator.func, ast.Attribute)):
                    continue
                method = decorator.func.attr.upper()
                if method not in HTTP_METHODS or not decorator.args:
                    continue
                route_value = decorator.args[0]
                if not isinstance(route_value, ast.Constant) or not isinstance(route_value.value, str):
                    continue
                receiver = (
                    decorator.func.value.id
                    if isinstance(decorator.func.value, ast.Name) else ""
                )
                route = {
                    "method": method,
                    "path": _join_http_paths(router_prefixes.get(receiver, ""), route_value.value),
                    "file": path, "line": decorator.lineno, "prefix_only": False,
                }
                if receiver in router_prefixes:
                    routers[(path, receiver)]["routes"].append(route)
                else:
                    routes.append(route)
                    backend_files.add(path)
                detected = True

    for path, tree in trees.items():
        bindings: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = _python_import_base(path, node.module, node.level)
                target = module_paths.get(base)
                if target:
                    for alias in node.names:
                        if alias.name != "*":
                            bindings[alias.asname or alias.name] = (target, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = module_paths.get(alias.name)
                    if target:
                        bindings[alias.asname or alias.name.split(".")[0]] = (
                            target, alias.name.rsplit(".", 1)[-1]
                        )
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call)
                    or _ast_call_name(node.func).split(".")[-1] != "include_router"
                    or not node.args or not isinstance(node.args[0], ast.Name)):
                continue
            imported = bindings.get(node.args[0].id)
            target_file, router_name = imported or (path, node.args[0].id)
            definition = routers.get((target_file, router_name))
            if definition is None:
                continue
            prefix_node = next((keyword.value for keyword in node.keywords
                                if keyword.arg == "prefix"), None)
            include_prefix = ""
            if prefix_node is not None:
                if not isinstance(prefix_node, ast.Constant) or not isinstance(
                    prefix_node.value, str
                ):
                    continue
                include_prefix = prefix_node.value
            backend_files.update({path, target_file})
            if definition["routes"]:
                routes.extend({
                    **route,
                    "path": _join_http_paths(include_prefix, route["path"]),
                } for route in definition["routes"])
            else:
                routes.append({
                    "method": "*",
                    "path": _join_http_paths(include_prefix, definition["prefix"]),
                    "file": target_file, "line": node.lineno,
                    "mount_file": path, "mount_line": node.lineno,
                    "prefix_only": True,
                })
            detected = True
    return routes, backend_files, detected


def _ast_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ast_call_name(node.value)}.{node.attr}"
    return ""


def _unsupported_backend(contents: Mapping[str, str]) -> str | None:
    patterns = (
        ("Flask", r"\b(?:from\s+flask|import\s+flask)\b"),
        ("Django", r"\b(?:from\s+django|import\s+django)\b"),
        ("Koa", r"\b(?:from\s+koa|require\s*\(\s*['\"]koa)"),
        ("Hono", r"\b(?:from\s+hono|require\s*\(\s*['\"]hono)"),
    )
    joined = "\n".join(contents.values())
    return next(
        (name for name, pattern in patterns if re.search(pattern, joined, re.IGNORECASE)),
        None,
    )


def _path_pattern_matches(route: str, request: str) -> bool:
    route_parts = route.strip("/").split("/") if route != "/" else []
    request_parts = request.strip("/").split("/") if request != "/" else []
    if len(route_parts) != len(request_parts):
        return False
    return all(
        expected.startswith(":") or expected == actual
        for expected, actual in zip(route_parts, request_parts)
    )


def analyze_http(
    contents: Mapping[str, str], paths: Iterable[str], repository: Path | None = None,
    *, include_tests: bool = False,
) -> dict[str, Any]:
    """Find statically evidenced frontend calls and backend routes."""
    source_paths = analysed_paths(paths, include_tests=include_tests)
    express_routes, express_backend_files, express_detected = _extract_express_routes(
        contents, source_paths, repository
    )
    fastapi_routes, fastapi_backend_files, fastapi_detected = _extract_fastapi_routes(
        contents, source_paths
    )
    if express_detected:
        backend_detected = "Express"
        routes = express_routes
        backend_files = express_backend_files | {
            path for path in source_paths if path.startswith(("server/", "backend/"))
        }
    elif fastapi_detected:
        backend_detected = "FastAPI"
        routes = fastapi_routes
        backend_files = fastapi_backend_files
    else:
        unsupported = _unsupported_backend(contents)
        backend_detected = unsupported or "none"
        routes = []
        backend_files = set()

    javascript = {
        path: contents.get(path, "") for path in source_paths
        if source_extension(path) in {".js", ".jsx", ".ts", ".tsx"}
        and path not in backend_files
        and not path.startswith(("server/", "backend/"))
    }
    values, functions = _known_js_values(javascript)
    imports_by_file = {
        path: _js_bindings(path, source, source_paths, repository)
        for path, source in javascript.items()
    }
    instances: dict[tuple[str, str], str | None] = {}
    module_factories: dict[str, str | None] = {}
    for path, source in javascript.items():
        for callee, arguments, _, _ in _iter_js_calls(source):
            if callee != "axios.create" or not arguments:
                continue
            base_match = re.search(r"\bbaseURL\s*:\s*([^,}\n]+)", arguments[0])
            module_factories[path] = base_match.group(1).strip() if base_match else None
            break
    for path, source in javascript.items():
        for callee, arguments, start, _ in _iter_js_calls(source):
            before = source[:start]
            if callee != "axios.create" or not arguments:
                target = imports_by_file[path].get(callee)
                base_url = module_factories.get(target) if target else None
                if base_url is None and target not in module_factories:
                    continue
                assignment = re.search(
                    r"\b(?:const|let|var)\s+(?:\[\s*([A-Za-z_$][\w$]*)\s*\]"
                    r"|([A-Za-z_$][\w$]*))\s*=\s*$", before,
                )
                if assignment:
                    variable = assignment.group(1) or assignment.group(2)
                    instances[(path, variable)] = base_url
                continue
            assignment = re.search(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*$", before
            )
            if not assignment:
                continue
            base_match = re.search(r"\bbaseURL\s*:\s*([^,}\n]+)", arguments[0])
            instances[(path, assignment.group(1))] = (
                base_match.group(1).strip() if base_match else None
            )

    http_edges = []
    unmatched = []
    frontend_files = set()
    for path, source in javascript.items():
        wrappers, wrapper_ranges = _fetch_wrappers(source)
        for callee, arguments, start, _ in _iter_js_calls(source):
            if any(begin <= start < end for begin, end in wrapper_ranges):
                continue
            parts = callee.split(".")
            if not arguments:
                continue
            method = ""
            base = None
            if callee == "fetch":
                method = "GET"
                if len(arguments) > 1:
                    method_match = re.search(
                        r"\bmethod\s*:\s*(['\"])(GET|POST|PUT|PATCH|DELETE)\1",
                        arguments[1], flags=re.IGNORECASE,
                    )
                    if method_match:
                        method = method_match.group(2).upper()
            elif callee in wrappers:
                method = wrappers[callee]
            elif len(parts) == 2 and parts[1].upper() in HTTP_METHODS:
                method = parts[1].upper()
                if parts[0] != "axios":
                    base = instances.get((path, parts[0]))
                    if (path, parts[0]) not in instances:
                        continue
            else:
                continue
            frontend_files.add(path)
            request_path, unknown_prefix = _resolve_call_path(
                arguments[0], base, values, functions
            )
            line = source.count("\n", 0, start) + 1
            call_site = {"path": path, "line": line}
            if request_path is None:
                unmatched.append({
                    "call_site": call_site, "method": method,
                    "expression": sanitize_label(arguments[0])[:180],
                    "reason": "dynamic_url",
                    "unknown_base_prefix": bool(unknown_prefix),
                })
                continue
            candidates = [route for route in routes if route["method"] in {method, "*"}]
            exact_matches = [route for route in candidates
                             if not route.get("prefix_only")
                             and _path_pattern_matches(route["path"], request_path)]
            if exact_matches:
                ranked = sorted(
                    exact_matches,
                    key=lambda route: (
                        -sum(not part.startswith(":") for part in route["path"].strip("/").split("/") if part),
                        -len(route["path"].strip("/").split("/") if route["path"] != "/" else []),
                        route["file"], route["line"],
                    ),
                )
                chosen = ranked[0]
            else:
                prefix_matches = [route for route in candidates
                                  if route.get("prefix_only")
                                  and (request_path == route["path"]
                                       or request_path.startswith(route["path"].rstrip("/") + "/"))]
                if prefix_matches:
                    chosen = max(prefix_matches, key=lambda route: (len(route["path"]), route["file"]))
                else:
                    unmatched.append({
                        "call_site": call_site, "method": method,
                        "path": request_path, "reason": "no_matching_route",
                        "unknown_base_prefix": bool(unknown_prefix),
                    })
                    continue
            http_edges.append({
                "source_file": path, "target_file": chosen["file"],
                "method": method, "path": request_path,
                "match": "prefix" if chosen.get("prefix_only") else "route",
                "call_site": call_site,
                "route_site": (
                    {
                        "path": chosen["mount_file"],
                        "line": chosen["mount_line"],
                    }
                    if chosen.get("prefix_only")
                    else {"path": chosen["file"], "line": chosen["line"]}
                ),
                "unknown_base_prefix": bool(unknown_prefix),
            })

    if not routes and backend_detected not in {"none"}:
        backend_note = f"backend routes not detected: {backend_detected}"
    elif backend_detected == "none":
        backend_note = "backend routes not detected: unknown"
    else:
        backend_note = None
    return {
        "http_edges": http_edges,
        "unmatched_calls": unmatched,
        "backend_detected": backend_detected,
        "backend_routes_note": backend_note,
        "frontend_files": frontend_files,
        "backend_files": backend_files,
    }


def _source_layers(
    contents: Mapping[str, str], paths: Iterable[str], http_analysis: Mapping[str, Any],
) -> dict[str, str]:
    frontend_files = set(http_analysis["frontend_files"])
    backend_files = set(http_analysis["backend_files"])
    layers = {}
    for path in paths:
        parts = {part.lower() for part in PurePosixPath(path).parts}
        source = contents.get(path, "")
        extension = source_extension(path)
        if (
            path in backend_files or extension == ".py"
            or parts.intersection({"server", "backend", "routes", "models"})
        ):
            layers[path] = "backend"
        elif (
            path in frontend_files
            or extension in {".jsx", ".tsx"}
            or parts.intersection({"frontend", "client", "pages", "components", "static"})
            or re.search(r"\bfrom\s+['\"]react['\"]|document\.|window\.", source)
        ):
            layers[path] = "frontend"
        else:
            layers[path] = "shared"
    return layers


def _display_http_edges(
    nodes: list[dict[str, Any]], http_edges: list[dict[str, Any]],
    groups: list[dict[str, Any]], first_id: int,
) -> list[dict[str, Any]]:
    group_for_node = {
        node_id: group["id"] for group in groups for node_id in group["node_ids"]
    }
    node_for_file = {path: node["id"] for node in nodes for path in node["files"]}
    aggregate: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in http_edges:
        source_node = node_for_file.get(edge["source_file"])
        target_node = node_for_file.get(edge["target_file"])
        source_group = group_for_node.get(source_node)
        target_group = group_for_node.get(target_node)
        if source_group is not None and target_group is not None and source_group != target_group:
            aggregate[(source_group, target_group)].append(edge)
    grouped = []
    for index, ((source, target), items) in enumerate(sorted(aggregate.items())):
        counts = Counter((item["method"], item["path"]) for item in items)
        method, path = min(counts, key=lambda key: (-counts[key], key))
        grouped.append({
            "id": f"e{first_id + index}", "source": source, "target": target,
            "kind": "http", "http_edges": items, "api_count": len(items),
            "method": method, "path": path, "label": f"{method} {path}",
        })
    return grouped


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


def _load_diagram_extra(repository: Path) -> tuple[dict[str, Any], str]:
    path = Path(repository) / "diagram.extra.json"
    if not path.is_file():
        return {"nodes": [], "edges": []}, "absent"
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read diagram.extra.json: {error}") from error
    if not isinstance(value, dict) or set(value) - {"nodes", "edges"}:
        raise ValueError(
            'diagram.extra.json must contain only "nodes" and "edges" arrays.'
        )
    nodes = value.get("nodes", [])
    edges = value.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise TypeError('diagram.extra.json "nodes" and "edges" must be arrays.')
    if len(nodes) >= MAX_NODES:
        raise ValueError(f"diagram.extra.json must define fewer than {MAX_NODES} external nodes.")
    seen_keys: set[str] = set()
    normalized_nodes = []
    for item in nodes:
        if not isinstance(item, dict) or set(item) != {"id", "label", "kind"}:
            raise ValueError('Each extra node needs "id", "label", and "kind".')
        key = item.get("id")
        label = sanitize_label(item.get("label", ""))
        node_type = item.get("kind")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
            raise ValueError("Extra node ids must be lowercase keys such as browser or mongodb.")
        if key in seen_keys:
            raise ValueError(f"Duplicate extra node id: {key}.")
        if not label or len(WORD_RE.findall(label)) > 5:
            raise ValueError(f"Extra node {key} needs a label of at most 5 words.")
        if not isinstance(node_type, str) or node_type not in {"actor", "service", "database"}:
            raise ValueError(f"Extra node {key} has an unsupported type.")
        seen_keys.add(key)
        normalized_nodes.append({"key": key, "label": label, "kind": node_type})
    normalized_edges = []
    for item in edges:
        if not isinstance(item, dict) or set(item) != {"from", "to", "label"}:
            raise ValueError('Each extra edge needs "from", "to", and "label".')
        source, target = item.get("from"), item.get("to")
        label = sanitize_label(item.get("label", ""))
        if not isinstance(source, str) or not isinstance(target, str):
            raise TypeError("Extra edge endpoints must be strings.")
        if not label or len(WORD_RE.findall(label)) > 5:
            raise ValueError("Extra edge labels need between 1 and 5 words.")
        normalized_edges.append({"from": source, "to": target, "label": label})
    return {"nodes": normalized_nodes, "edges": normalized_edges}, hashlib.sha256(raw).hexdigest()


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
    extra, extra_digest = _load_diagram_extra(repository)
    http_analysis = analyze_http(
        contents, source_files, repository, include_tests=include_tests
    )
    _validate_http_citations(
        http_analysis["http_edges"], set(source_files), contents
    )
    layer_by_file = _source_layers(contents, source_files, http_analysis)
    digest = content_digest(
        source_files, contents, stored_hashes, include_tests=include_tests
    )
    key = cache_key(repository_identity, commit_sha, digest, extra_digest)
    cached = store.value(key)
    if cached:
        payload = json.loads(cached)
        if payload.get("http_edges") != http_analysis["http_edges"]:
            raise ValueError("The cached HTTP edges do not match the indexed source.")
        validate_diagram(
            payload, set(source_files), extract_imports(
                contents, include_tests=include_tests, repository=repository
            ), http_edges=http_analysis["http_edges"], contents=contents,
            layer_by_file=layer_by_file,
        )
        return {**payload, "llm_calls_made": 0, "cache_hit": True}

    raw_edges, unresolved_imports = _extract_import_graph(
        contents, source_files, repository
    )
    file_edges = [
        edge for edge in raw_edges
        if edge["source_file"] in source_files and edge["target_file"] in source_files
    ]
    source_nodes = _make_nodes(
        source_files, MAX_NODES - len(extra["nodes"]), layer_by_file
    )
    source_nodes = _refine_directory_nodes(
        source_nodes, MAX_NODES - len(extra["nodes"])
    )
    not_analysed = _not_analysed(paths)
    chunks = [chunk for chunk in store.all() if chunk.get("source_type") == "code"]
    chunks_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_path[chunk["path"]].append(chunk)
    for values in chunks_by_path.values():
        values.sort(key=lambda item: (item["start_line"], item["id"]))

    llm_calls = 0
    labels_status = "llm"
    groups_status = "llm"
    if settings.llm_api_key and source_nodes:
        try:
            raw_groups, count = _name_groups(
                source_nodes, readme, settings, llm_complete, run_diagnostics,
                layer_by_file,
            )
            llm_calls += count
            groups = _number_groups(raw_groups)
            label_tasks = _label_tasks(
                source_nodes, file_edges, groups, chunks_by_path, store, settings
            )
            model_tasks = [task for task in label_tasks if task["evidence"]]
            model_labels, count = _label_batches(
                model_tasks, settings, llm_complete, run_diagnostics
            )
            llm_calls += count
            labels = {**_fallback_labels(
                [task for task in label_tasks if not task["evidence"]], source_nodes,
                groups,
            ), **_labels_by_id(model_labels)}
            if len(model_tasks) != len(label_tasks):
                labels_status = "fallback (no indexed chunk for some labels)"
        except _LLMValidationError as error:
            llm_calls += error.calls
            groups = _number_groups(_fallback_groups(source_nodes, layer_by_file))
            label_tasks = _label_tasks(
                source_nodes, file_edges, groups, chunks_by_path, store, settings,
                retrieve_evidence=False,
            )
            labels = _fallback_labels(label_tasks, source_nodes, groups)
            labels_status = f"fallback ({error.reason})"
            groups_status = "fallback"
    elif source_nodes:
        groups = _number_groups(_fallback_groups(source_nodes, layer_by_file))
        label_tasks = _label_tasks(
            source_nodes, file_edges, groups, chunks_by_path, store, settings,
            retrieve_evidence=False,
        )
        labels = _fallback_labels(label_tasks, source_nodes, groups)
        labels_status = "fallback (no LLM key)"
        groups_status = "fallback"
    else:
        groups = []
        labels = {}
        labels_status = "no source nodes"
        groups_status = "fallback"

    edges = _display_edges(source_nodes, file_edges, groups)
    grouped_http_edges = _display_http_edges(
        source_nodes, http_analysis["http_edges"], groups, len(edges) + 1
    )
    edges.extend(grouped_http_edges)
    external_nodes = _make_external_nodes(extra["nodes"], len(source_nodes) + 1)
    external_edges = _merge_external_edges(
        extra["edges"], external_nodes, groups, len(edges) + 1, source_nodes
    )
    nodes = source_nodes + external_nodes
    edges.extend(external_edges)
    for edge in grouped_http_edges:
        labels[edge["id"]] = {
            "id": edge["id"], "kind": "http_edge", "phrase": edge["label"],
            "chunk_id": None, "path": None, "start_line": None, "end_line": None,
        }
    for node in external_nodes:
        labels[node["id"]] = {
            "id": node["id"], "kind": "external", "phrase": node["label"],
            "chunk_id": None, "path": None, "start_line": None, "end_line": None,
        }
    for edge in external_edges:
        labels[edge["id"]] = {
            "id": edge["id"], "kind": "external_edge", "phrase": edge["label"],
            "chunk_id": None, "path": None, "start_line": None, "end_line": None,
        }
    display_labels = _display_labels(labels, nodes)
    label_entities = display_labels["nodes"] | display_labels["edges"]
    layout_columns = max(
        [len(group["node_ids"]) for group in groups]
        + [len(external_nodes), 1]
    )
    layout_rows = len(groups) + (1 if external_nodes else 0)
    payload = {
        "repository": repository_identity,
        "commit_sha": commit_sha,
        "content_digest": digest,
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "groups_status": groups_status,
        "import_edges": file_edges,
        "http_edges": http_analysis["http_edges"],
        "http_edge_count": len(http_analysis["http_edges"]),
        "http_routes_note": http_analysis["backend_routes_note"],
        "unmatched_calls": http_analysis["unmatched_calls"],
        "unmatched_calls_count": len(http_analysis["unmatched_calls"]),
        "unmatched_calls_first_10": http_analysis["unmatched_calls"][:10],
        "backend_detected": http_analysis["backend_detected"],
        "labels": display_labels,
        "citations": _citations(label_entities),
        "not_analysed": not_analysed,
        "unresolved_imports": unresolved_imports,
        "unresolved_import_count": len(unresolved_imports),
        "unresolved_imports_first_10": unresolved_imports[:10],
        "labels_status": labels_status,
        "layout": {"rows": layout_rows, "columns": layout_columns},
        "mermaid": render_mermaid(nodes, edges, groups, label_entities),
    }
    validate_diagram(
        payload, set(source_files), file_edges,
        http_edges=http_analysis["http_edges"], contents=contents,
        layer_by_file=layer_by_file,
    )
    store.set_value(key, json.dumps(payload, sort_keys=True))
    return {**payload, "llm_calls_made": llm_calls, "cache_hit": False}


def _validate_http_citations(
    http_edges: list[dict[str, Any]], source_files: set[str],
    contents: Mapping[str, str],
) -> None:
    for edge in http_edges:
        if edge.get("source_file") not in source_files or edge.get("target_file") not in source_files:
            raise ValueError("HTTP edge cites a file outside the indexed source files.")
        for citation_key in ("call_site", "route_site"):
            citation = edge.get(citation_key)
            if not isinstance(citation, dict):
                raise TypeError(f"HTTP edge is missing its {citation_key} citation.")
            path, line = citation.get("path"), citation.get("line")
            lines = contents.get(path, "").splitlines()
            if path not in source_files or not isinstance(line, int) or not 1 <= line <= len(lines):
                raise ValueError(
                    f"HTTP edge has an invalid indexed citation: {path}:{line}."
                )


def _not_analysed(paths: Iterable[str]) -> list[dict[str, Any]]:
    extensions = Counter(
        source_extension(path) or "[no extension]"
        for path in paths if source_extension(path) not in SOURCE_EXTENSIONS
    )
    return [
        {"extension": extension, "file_count": count}
        for extension, count in sorted(extensions.items())
    ]


def _make_nodes(
    source_files: list[str], max_nodes: int = MAX_NODES,
    layer_by_file: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    active = [{"path": path, "files": [path]} for path in sorted(set(source_files))]
    if max_nodes < 1 and active:
        raise ValueError("The diagram node cap leaves no room for source files.")
    while len(active) > max_nodes:
        candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in active:
            node_path = PurePosixPath(node["path"])
            folder = node_path if node["path"] not in node["files"] else node_path.parent
            if folder.as_posix() not in {"", "."}:
                layer = _node_layer(node, layer_by_file)
                candidates[f"{layer}\0{folder.as_posix()}"].append(node)
        options = [
            (key, nodes) for key, nodes in candidates.items()
            if len(nodes) > 1
        ]
        if not options:
            by_layer: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for node in active:
                by_layer[_node_layer(node, layer_by_file)].append(node)
            choices = [nodes for nodes in by_layer.values() if len(nodes) > 1]
            if not choices:
                break
            selected = max(choices, key=len)
            folder = _common_parent(
                path for node in selected for path in node["files"]
            )
        else:
            key, selected = max(
                options,
                key=lambda item: (
                    sum(len(node["files"]) for node in item[1]),
                    len(item[1]), len(PurePosixPath(item[0]).parts), item[0],
                ),
            )
            _, folder = key.split("\0", 1)
        if not selected:
            break
        selected_ids = {id(node) for node in selected}
        merged = {
            "path": folder,
            "files": sorted(path for node in selected for path in node["files"]),
        }
        active = [node for node in active if id(node) not in selected_ids] + [merged]

    active.sort(key=lambda node: (node["path"], node["files"]))
    return [
        {"id": f"n{index}", "kind": "source", **node}
        for index, node in enumerate(active, 1)
    ]


def _refine_directory_nodes(
    nodes: list[dict[str, Any]], max_nodes: int = MAX_NODES,
) -> list[dict[str, Any]]:
    """Use spare node slots to show the largest top-level directory's subfolders."""
    refined = [dict(node) for node in nodes]
    while len(refined) < max_nodes:
        candidates = sorted(
            (node for node in refined if len(node["files"]) > 1),
            key=lambda node: (-len(node["files"]), node["path"]),
        )
        split = False
        for candidate in candidates:
            prefix = "" if candidate["path"] in {"", "."} else candidate["path"].rstrip("/") + "/"
            buckets: dict[str, list[str]] = defaultdict(list)
            for path in candidate["files"]:
                if prefix and not path.startswith(prefix):
                    continue
                remainder = path.removeprefix(prefix)
                child = remainder.split("/", 1)[0]
                buckets[child].append(path)
            expandable = [
                (name, files) for name, files in buckets.items()
                if len(files) > 1
            ]
            remainder_files = sorted(
                path for name, files in buckets.items()
                if len(files) == 1 for path in files
            )
            if len(expandable) < 2 and not (expandable and remainder_files):
                continue
            capacity = max_nodes - len(refined) + 1
            if capacity < 2:
                break
            ordered = sorted(expandable, key=lambda item: (-len(item[1]), item[0]))
            chosen = ordered[:capacity - 1]
            chosen_names = {name for name, _ in chosen}
            remainder_files = sorted(
                path for name, files in buckets.items()
                if name not in chosen_names for path in files
            )
            pieces = [
                {"path": _common_parent(files), "files": sorted(files)}
                for _, files in chosen
            ]
            if remainder_files:
                pieces.append({
                    "path": _common_parent(remainder_files),
                    "files": remainder_files,
                })
            if len(pieces) < 2:
                continue
            refined.remove(candidate)
            refined.extend(pieces)
            split = True
            break
        if not split:
            break
    refined.sort(key=lambda node: (node["path"], node["files"]))
    return [
        {"kind": "source", **node, "id": f"n{index}"}
        for index, node in enumerate(refined, 1)
    ]


def _common_parent(paths: Iterable[str]) -> str:
    parents = [list(PurePosixPath(path).parts[:-1]) for path in paths]
    if not parents:
        return "."
    common = parents[0]
    for parts in parents[1:]:
        while common and parts[:len(common)] != common:
            common.pop()
    return "/".join(common) or "."


def _node_display_name(node: dict[str, Any]) -> str:
    if len(node["files"]) == 1:
        return PurePosixPath(node["files"][0]).name
    return (PurePosixPath(node["path"]).name or "repository") + "/"


def _candidate_groups(
    nodes: list[dict[str, Any]], layer_by_file: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {"name": group["name"], "node_ids": group["node_ids"]}
        for group in _fallback_groups(nodes, layer_by_file)
    ]


def _name_groups(
    nodes: list[dict[str, Any]], readme: str, settings: Settings,
    llm_complete: Callable[[str, Settings], str],
    run_diagnostics: list[dict[str, str]] | None = None,
    layer_by_file: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    candidates = _candidate_groups(nodes, layer_by_file)
    node_files = {node["id"]: node["files"] for node in nodes}
    layer_by_node = {
        node["id"]: _node_layer(node, layer_by_file) for node in nodes
    }
    prompt = (
        "Group source modules by purpose, starting from these folders. "
        "Never put frontend and backend files in the same group. "
        "Return 5 to 8 groups when there are at least 10 nodes. Avoid groups containing only one node. "
        "Do not add, remove, or rename node ids. Return JSON only as "
        '{"groups":[{"name":"...","node_ids":["n1"]}]}. '
        f"Purpose-based starting groups: {json.dumps(candidates, ensure_ascii=False)}\n"
        f"Node files: {json.dumps(node_files, ensure_ascii=False)}\n"
        f"README: {readme[:12000]}"
    )
    result, calls = _validated_json(
        prompt, settings, llm_complete,
        lambda value: _validate_groups(value, nodes, layer_by_node),
        run_diagnostics=run_diagnostics, phase="groups",
    )
    return result, calls


def _validate_groups(
    value: Any, nodes: list[dict[str, Any]],
    layer_by_node: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
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
        if layer_by_node and {layer_by_node[node_id] for node_id in ids}.issuperset(
            {"frontend", "backend"}
        ):
            raise ValueError("Frontend and backend files cannot share a purpose group.")
        seen.extend(ids)
        groups.append({"name": name, "node_ids": ids})
    if set(seen) != expected or len(seen) != len(expected):
        raise ValueError("Every node must appear in exactly one group.")
    if len(nodes) >= 10 and not MIN_GROUPS <= len(groups) <= MAX_GROUPS:
        raise ValueError(f"Diagrams with at least 10 nodes need {MIN_GROUPS} to {MAX_GROUPS} groups.")
    if len(nodes) >= 10 and any(len(group["node_ids"]) < 2 for group in groups):
        raise ValueError("Purpose groups cannot wrap a single node when there are 10 or more nodes.")
    names = [group["name"].casefold() for group in groups]
    if len(names) != len(set(names)):
        raise ValueError("Purpose group names must be unique.")
    return groups


def _node_layer(
    node: dict[str, Any], layer_by_file: Mapping[str, str] | None,
) -> str:
    if not layer_by_file:
        return "shared"
    layers = {layer_by_file.get(path, "shared") for path in node["files"]}
    if len(layers) > 1:
        return "mixed"
    return next(iter(layers), "shared")


def _fallback_groups(
    nodes: list[dict[str, Any]], layer_by_file: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        parts = PurePosixPath(node["files"][0]).parts
        folder_name = parts[0] if len(parts) > 1 else "repository root"
        layer = _node_layer(node, layer_by_file)
        grouped[(layer, folder_name)].append(node)
    named_groups = {
        name if layer == "shared" else f"{name} {layer}": list(values)
        for (layer, name), values in sorted(grouped.items())
    }
    group_layers = {
        name if layer == "shared" else f"{name} {layer}": layer
        for layer, name in grouped
    }
    for name in sorted(named_groups):
        if name not in named_groups or len(named_groups[name]) != 1:
            continue
        layer = group_layers[name]
        targets = [
            target for target in named_groups
            if target != name and group_layers[target] == layer
            and len(named_groups[target]) > 1
        ]
        if not targets:
            continue
        target = max(
            targets,
            key=lambda item: (
                _shared_path_prefix(named_groups[name], named_groups[item]),
                len(named_groups[item]), item,
            ),
        )
        named_groups[target].extend(named_groups.pop(name))
        group_layers.pop(name)

    if len(nodes) >= 10:
        while len(named_groups) < MIN_GROUPS:
            eligible = [
                name for name, items in named_groups.items() if len(items) >= 4
            ]
            if not eligible or len(named_groups) >= MAX_GROUPS:
                break
            name = max(eligible, key=lambda item: (len(named_groups[item]), item))
            ordered = sorted(named_groups.pop(name), key=lambda node: node["files"])
            middle = len(ordered) // 2
            core, support = f"{name} core", f"{name} support"
            named_groups[core], named_groups[support] = ordered[:middle], ordered[middle:]
            group_layers[core] = group_layers.pop(name)
            group_layers[support] = group_layers[core]

    return [
        {"name": name, "node_ids": sorted(node["id"] for node in values)}
        for name, values in sorted(named_groups.items())
    ]


def _merge_smallest_group(grouped: dict[str, list[dict[str, Any]]]) -> str:
    smallest = min(grouped, key=lambda name: (len(grouped[name]), name))
    return _merge_group(grouped, smallest)


def _merge_group(
    grouped: dict[str, list[dict[str, Any]]], smallest: str,
    exclude: set[str] | None = None,
) -> str:
    choices = [name for name in grouped if name != smallest and name not in (exclude or set())]
    if not choices:
        choices = [name for name in grouped if name != smallest]
    target = max(
        choices,
        key=lambda name: (
            _shared_path_prefix(grouped[smallest], grouped[name]),
            len(grouped[name]), name,
        ),
    )
    grouped[target].extend(grouped.pop(smallest))
    return target


def _split_largest_group(grouped: dict[str, list[dict[str, Any]]]) -> None:
    eligible = [name for name, items in grouped.items() if len(items) >= 4]
    if not eligible:
        raise ValueError("Cannot make 5 to 8 purpose groups without singleton groups.")
    name = max(eligible, key=lambda item: (len(grouped[item]), item))
    ordered = sorted(grouped.pop(name), key=lambda node: node["files"])
    middle = len(ordered) // 2
    grouped[f"{name} core"] = ordered[:middle]
    grouped[f"{name} support"] = ordered[middle:]


def _increase_group_count(grouped: dict[str, list[dict[str, Any]]]) -> None:
    if any(len(items) >= 4 for items in grouped.values()):
        _split_largest_group(grouped)
        return
    triples = sorted(name for name, items in grouped.items() if len(items) == 3)
    if len(triples) < 2:
        raise ValueError("Cannot reach 5 purpose groups without single-node wrappers.")
    selected = triples[:2]
    singletons = []
    for name in selected:
        ordered = sorted(grouped.pop(name), key=lambda node: node["files"])
        grouped[f"{name} core"] = ordered[:2]
        singletons.extend(ordered[2:])
    shared_name = "Shared modules"
    suffix = 2
    while shared_name in grouped:
        shared_name = f"Shared modules {suffix}"
        suffix += 1
    grouped[shared_name] = singletons


def _purpose_for_path(path: str) -> str:
    parts = [part.lower() for part in PurePosixPath(path).parts]
    filename = PurePosixPath(path).stem.lower()
    joined = "/".join(parts)
    if "gitcontext" in parts:
        if "ingest" in parts or filename in {"chunking", "parser", "history", "github", "remote", "repository"}:
            return "Repository ingestion"
        if filename in {"cli", "web", "app"} or "static" in parts:
            return "User interfaces"
        if filename in {"answer", "evaluation"}:
            return "Answer and evaluation"
        if filename in {"retrieval", "embeddings"}:
            return "Search and embeddings"
        return "Index and storage"
    if "server" in parts:
        if "models" in parts or "db" in parts or filename in {"database", "connect"}:
            return "Data models and storage"
        if "routes" in parts or "api" in parts:
            return "Backend API"
        return "Server support"
    if "pages" in parts:
        return "Pages and workflows"
    if "components" in parts:
        return "Interface components"
    if "hooks" in parts:
        return "Application hooks"
    if "context" in parts or "providers" in parts or any(
        token in filename for token in ("auth", "route", "login")
    ):
        return "Authentication and state"
    if "layouts" in parts or "routes" in parts:
        return "Application structure"
    if "utils" in parts or "shared" in parts:
        return "Shared application logic"
    if filename in {"index", "app", "main"}:
        return "Application entry points"
    if "test" in joined or "spec" in filename:
        return "Test support"
    return "Shared application logic"


def _shared_path_prefix(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> int:
    left_parts = {tuple(PurePosixPath(path).parts[:-1]) for node in left for path in node["files"]}
    right_parts = {tuple(PurePosixPath(path).parts[:-1]) for node in right for path in node["files"]}
    return max((
        sum(1 for a, b in zip(a_parts, b_parts) if a == b)
        for a_parts in left_parts for b_parts in right_parts
    ), default=0)


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
            "Import edge labels, when useful, must be action verbs of at most 4 words, "
            "supported by the supplied import code. Do not write 'imports <name>'. "
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
            if re.fullmatch(r"imports?\s+\w+", phrase.lower()):
                raise ValueError("Import labels must describe an action, not repeat an import name.")
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
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    node_by_id = {node["id"]: node for node in nodes}
    labels = {}
    for task in tasks:
        if task["kind"] == "node":
            node = node_by_id[task["id"]]
            path = node["files"][0] if node["files"] else node["path"]
            if len(node["files"]) > 1:
                phrase = _node_display_name(node).rstrip("/") + "/"
            else:
                phrase = PurePosixPath(path).name
            evidence = task["evidence"]
        else:
            phrase = ""
            path = None
            evidence = task["evidence"]
        if task["kind"] == "edge":
            phrase = ""
        chunk = evidence[0] if evidence else None
        labels[task["id"]] = {
            "id": task["id"],
            "kind": task["kind"],
            "phrase": phrase,
            "chunk_id": chunk["chunk_id"] if chunk else None,
            "path": chunk["path"] if chunk else path,
            "start_line": chunk["start_line"] if chunk else None,
            "end_line": chunk["end_line"] if chunk else None,
            "fallback": True,
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
    group_for_node = {
        node_id: group["id"] for group in groups for node_id in group["node_ids"]
    }
    aggregate: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    node_for_file = {path: node["id"] for node in nodes for path in node["files"]}
    for edge in file_edges:
        source_node = node_for_file.get(edge["source_file"])
        target_node = node_for_file.get(edge["target_file"])
        source_group = group_for_node.get(source_node)
        target_group = group_for_node.get(target_node)
        if source_group is not None and target_group is not None and source_group != target_group:
            aggregate[(source_group, target_group)].append(edge)
    return [
        {
            "id": f"e{index}", "source": source, "target": target,
            "file_edges": edges, "import_count": len(edges), "kind": "imports",
        }
        for index, ((source, target), edges) in enumerate(sorted(aggregate.items()), 1)
    ]


def _make_external_nodes(
    definitions: list[dict[str, Any]], first_id: int,
) -> list[dict[str, Any]]:
    return [
        {
            "id": f"n{first_id + index}", "kind": "external",
            "external_key": item["key"], "label": item["label"],
            "external_kind": item["kind"], "path": None, "files": [],
        }
        for index, item in enumerate(definitions)
    ]


def _merge_external_edges(
    definitions: list[dict[str, Any]], external_nodes: list[dict[str, Any]],
    groups: list[dict[str, Any]], first_id: int,
    source_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not definitions:
        return []
    external_ids = {node["external_key"]: node["id"] for node in external_nodes}
    group_for_file = {
        path: group["id"] for group in groups for node_id in group["node_ids"]
        for node in source_nodes if node["id"] == node_id for path in node["files"]
    }
    group_for_path = {
        node["path"]: group["id"] for group in groups for node_id in group["node_ids"]
        for node in source_nodes if node["id"] == node_id
    }

    def endpoint(value: str) -> str:
        if value in external_ids:
            return external_ids[value]
        if value in group_for_file:
            return group_for_file[value]
        if value in group_for_path:
            return group_for_path[value]
        raise ValueError(f"diagram.extra.json refers to unknown node {value!r}.")

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in definitions:
        source, target = endpoint(item["from"]), endpoint(item["to"])
        if source == target:
            raise ValueError("diagram.extra.json edge endpoints resolve to the same node.")
        key = (source, target)
        merged.setdefault(key, {
            "source": source, "target": target, "label": item["label"],
            "file_edges": [], "import_count": 0, "kind": "external",
        })
    return [
        {"id": f"e{first_id + index}", **edge}
        for index, edge in enumerate(
            (merged[key] for key in sorted(merged)), 0
        )
    ]


def render_mermaid(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
    groups: list[dict[str, Any]], labels: Mapping[str, dict[str, Any]],
) -> str:
    lines = ["flowchart TB"]
    node_by_id = {node["id"]: node for node in nodes}
    for group in groups:
        group_id = group["id"]
        lines.append(f'  subgraph {group_id}["{sanitize_label(group["name"])}"]')
        lines.append("    direction LR")
        for node_id in group["node_ids"]:
            label = labels[node_id]
            node = node_by_id[node_id]
            filename = sanitize_label(_node_display_name(node))
            display = (
                sanitize_label(label["phrase"])
                if label.get("fallback")
                else f'{sanitize_label(label["phrase"])} [{filename}]'
            )
            lines.append(f'    {node_id}["{display}"]')
        lines.append("  end")
    for node in nodes:
        if node.get("kind") != "external":
            continue
        label = sanitize_label(labels[node["id"]]["phrase"])
        shape = {
            "actor": f'(({label}))',
            "service": f'([{label}])',
            "database": f'[({label})]',
        }[node["external_kind"]]
        lines.append(f'  {node["id"]}{shape}')
    for edge in edges:
        label = sanitize_label(labels[edge["id"]]["phrase"])
        if edge.get("kind") == "imports":
            label = (
                f"{label} ({edge['import_count']} imports)"
                if label else f"{edge['import_count']} imports"
            )
        elif edge.get("kind") == "http":
            label = f"{label} ({edge['api_count']} API calls)"
        if label:
            arrow = "-.->" if edge.get("kind") == "http" else "-->"
            lines.append(
                f'  {edge["source"]} {arrow}|"{label}"| {edge["target"]}'
            )
        else:
            lines.append(f'  {edge["source"]} --> {edge["target"]}')
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


def _display_labels(
    labels: Mapping[str, dict[str, Any]], nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {"nodes": {}, "edges": {}}
    node_by_id = {node["id"]: node for node in nodes}
    for label_id, label in labels.items():
        item = dict(label)
        if item["kind"] == "node":
            node = node_by_id[label_id]
            filename = sanitize_label(_node_display_name(node))
            item["text"] = (
                sanitize_label(item["phrase"])
                if item.get("fallback")
                else f"{item['phrase']} [{filename}]".strip()
            )
            result["nodes"][label_id] = item
        elif item["kind"] == "external":
            item["text"] = item["phrase"]
            result["nodes"][label_id] = item
        else:
            item["text"] = item["phrase"]
            result["edges"][label_id] = item
    return result


def validate_diagram(
    payload: dict[str, Any], source_files: set[str],
    file_edges: list[dict[str, Any]],
    *, http_edges: list[dict[str, Any]] | None = None,
    contents: Mapping[str, str] | None = None,
    layer_by_file: Mapping[str, str] | None = None,
) -> None:
    http_edges = http_edges or []
    node_ids = {node["id"] for node in payload["nodes"]}
    if len(node_ids) != len(payload["nodes"]) or any(
        not re.fullmatch(r"n[1-9][0-9]*", node_id) for node_id in node_ids
    ):
        raise ValueError("Diagram node ids must be unique generated n1, n2, ... ids.")
    if len(node_ids) > MAX_NODES:
        raise ValueError(f"Diagram exceeds the {MAX_NODES}-node cap.")
    source_nodes = [node for node in payload["nodes"] if node.get("kind") != "external"]
    external_nodes = [node for node in payload["nodes"] if node.get("kind") == "external"]
    for node in source_nodes:
        if not node["files"] or not set(node["files"]).issubset(source_files):
            raise ValueError(f"Diagram node {node['id']} has no real source files.")
        if node["path"] != "." and node["path"] not in source_files and not all(
            file.startswith(node["path"].rstrip("/") + "/") for file in node["files"]
        ):
            raise ValueError(f"Diagram node {node['id']} does not map to a file or folder.")
    external_keys = set()
    for node in external_nodes:
        key = node.get("external_key")
        if (not isinstance(key, str) or key in external_keys or node.get("files")
                or node.get("external_kind") not in {"actor", "service", "database"}):
            raise ValueError("External nodes must have unique keys, a supported shape, and no source files.")
        external_keys.add(key)
    if (payload.get("groups_status") == "llm" and len(source_nodes) >= 10
            and not MIN_GROUPS <= len(payload["groups"]) <= MAX_GROUPS):
        raise ValueError(f"Diagrams with at least 10 source nodes need {MIN_GROUPS} to {MAX_GROUPS} purpose groups.")
    grouped = [node_id for group in payload["groups"] for node_id in group["node_ids"]]
    source_node_ids = {node["id"] for node in source_nodes}
    if set(grouped) != source_node_ids or len(grouped) != len(source_node_ids):
        raise ValueError("Every source node must belong to exactly one purpose group.")
    group_ids = {group["id"] for group in payload["groups"]}
    if len(group_ids) != len(payload["groups"]) or any(
        not re.fullmatch(r"g[1-9][0-9]*", group_id) for group_id in group_ids
    ):
        raise ValueError("Diagram group ids must be unique generated g1, g2, ... ids.")
    for group in payload["groups"]:
        layers = {
            layer_by_file.get(path, "shared")
            for node in source_nodes if node["id"] in group["node_ids"]
            for path in node["files"]
        } if layer_by_file else set()
        if {"frontend", "backend"}.issubset(layers):
            raise ValueError("Frontend and backend files cannot share a purpose group.")
    allowed_edges = {(edge["source_file"], edge["target_file"]) for edge in file_edges}
    if payload.get("import_edges") != file_edges:
        raise ValueError("File-level imports do not match the extracted import graph.")
    node_for_file = {path: node["id"] for node in source_nodes for path in node["files"]}
    group_for_node = {
        node_id: group["id"] for group in payload["groups"] for node_id in group["node_ids"]
    }
    external_node_ids = {node["id"] for node in external_nodes}
    edge_ids = set()
    import_pairs: set[tuple[str, str]] = set()
    http_pairs: set[tuple[str, str]] = set()
    external_pairs: set[tuple[str, str]] = set()
    expected_imports: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in file_edges:
        source_group = group_for_node[node_for_file[item["source_file"]]]
        target_group = group_for_node[node_for_file[item["target_file"]]]
        if source_group != target_group:
            expected_imports[(source_group, target_group)].append(item)
    expected_http_pairs = {
        (
            group_for_node[node_for_file[item["source_file"]]],
            group_for_node[node_for_file[item["target_file"]]],
        )
        for item in http_edges
        if group_for_node[node_for_file[item["source_file"]]]
        != group_for_node[node_for_file[item["target_file"]]]
    }
    for edge in payload["edges"]:
        if edge["id"] in edge_ids or not re.fullmatch(r"e[1-9][0-9]*", edge["id"]):
            raise ValueError("Diagram edge ids must be unique generated e1, e2, ... ids.")
        edge_ids.add(edge["id"])
        if edge.get("kind") == "imports":
            if edge["source"] not in group_ids or edge["target"] not in group_ids:
                raise ValueError("Import edges must connect purpose groups only.")
            if edge["source"] == edge["target"]:
                raise ValueError("Within-group imports are omitted from the architecture view.")
            if not edge["file_edges"] or any(
                (item["source_file"], item["target_file"]) not in allowed_edges
                for item in edge["file_edges"]
            ):
                raise ValueError("Diagram edge is not backed by the extracted import graph.")
            expected_pairs = {
                (
                    group_for_node[node_for_file[item["source_file"]]],
                    group_for_node[node_for_file[item["target_file"]]],
                )
                for item in edge["file_edges"]
            }
            if expected_pairs != {(edge["source"], edge["target"])}:
                raise ValueError("Merged diagram edge does not match its source and target groups.")
            if edge.get("import_count") != len(edge["file_edges"]):
                raise ValueError("Grouped import edge count does not match the merged imports.")
            pair = (edge["source"], edge["target"])
            if edge["file_edges"] != expected_imports.get(pair):
                raise ValueError("Grouped import edge omits or duplicates file-level imports.")
            if pair in import_pairs:
                raise ValueError("Duplicate import edges between the same purpose groups.")
            import_pairs.add(pair)
        elif edge.get("kind") == "http":
            if edge["source"] not in group_ids or edge["target"] not in group_ids:
                raise ValueError("HTTP edges must connect purpose groups only.")
            if edge["source"] == edge["target"]:
                raise ValueError("Within-group HTTP calls are omitted from the architecture view.")
            expected = [
                item for item in http_edges
                if group_for_node.get(node_for_file.get(item["source_file"])) == edge["source"]
                and group_for_node.get(node_for_file.get(item["target_file"])) == edge["target"]
            ]
            if not expected or edge.get("http_edges") != expected:
                raise ValueError("Grouped HTTP edge does not match its file-level calls.")
            if edge.get("api_count") != len(expected):
                raise ValueError("Grouped HTTP edge count does not match its API calls.")
            pair = (edge["source"], edge["target"])
            if pair in http_pairs:
                raise ValueError("Duplicate HTTP edges between the same purpose groups.")
            http_pairs.add(pair)
        elif edge.get("kind") == "external":
            valid_endpoints = group_ids | external_node_ids
            if edge["source"] not in valid_endpoints or edge["target"] not in valid_endpoints:
                raise ValueError("External edges must connect existing nodes or groups.")
            if edge.get("file_edges") or edge.get("import_count") != 0:
                raise ValueError("External edges cannot claim source imports.")
            pair = (edge["source"], edge["target"])
            if pair in external_pairs:
                raise ValueError("Duplicate extra edges between the same nodes.")
            external_pairs.add(pair)
        else:
            raise ValueError("Diagram edge has an unknown kind.")
    for kind in ("nodes", "edges"):
        expected_ids = node_ids if kind == "nodes" else edge_ids
        if set(payload["labels"][kind]) != expected_ids:
            raise ValueError(f"{kind.title()} and labels do not match.")
        for entity_id, label in payload["labels"][kind].items():
            entity = (
                next(node for node in payload["nodes"] if node["id"] == entity_id)
                if kind == "nodes"
                else next(edge for edge in payload["edges"] if edge["id"] == entity_id)
            )
            if not label.get("phrase") and not (
                kind == "edges" and entity.get("kind") == "imports"
            ):
                raise ValueError(f"{kind[:-1].title()} label cannot be empty.")
            limit = (
                5 if kind == "nodes" or entity.get("kind") == "external" else 4
            )
            if len(WORD_RE.findall(label["phrase"])) > limit:
                raise ValueError(f"{kind[:-1].title()} label exceeds {limit} words.")
            if entity_id not in node_ids and kind == "nodes":
                raise ValueError("A node label refers to an unknown node.")
            if not re.fullmatch(r"e[1-9][0-9]*", entity_id) and kind == "edges":
                raise ValueError("An edge label refers to an unknown edge.")
    if contents is not None:
        _validate_http_citations(http_edges, source_files, contents)
    if http_pairs != expected_http_pairs:
        raise ValueError("Grouped HTTP edges do not cover all cross-group API calls.")
    if payload.get("http_edge_count") != len(http_edges):
        raise ValueError("HTTP edge count does not match the file-level HTTP edges.")
    if payload.get("unmatched_calls_count") != len(payload.get("unmatched_calls", [])):
        raise ValueError("Unmatched call count does not match its list.")
    if payload.get("unresolved_import_count") != len(payload.get("unresolved_imports", [])):
        raise ValueError("Unresolved import count does not match its list.")
    validate_mermaid(
        payload["mermaid"], payload["nodes"], payload["groups"], payload["edges"]
    )


def validate_mermaid(
    text: str, nodes: list[dict[str, Any]], groups: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Check the constrained syntax emitted by render_mermaid, not Mermaid rendering."""
    lines = text.splitlines()
    if not lines or lines[0] != "flowchart TB" or not text.endswith("\n"):
        raise ValueError("Mermaid text does not match the flowchart template.")
    expected_nodes = {node["id"] for node in nodes}
    source_nodes = {node["id"] for node in nodes if node.get("kind") != "external"}
    external_nodes = {node["id"] for node in nodes if node.get("kind") == "external"}
    external_kinds = {
        node["id"]: node.get("external_kind")
        for node in nodes if node.get("kind") == "external"
    }
    expected_groups = {group["id"] for group in groups}
    expected_edges = {
        (
            edge["source"], edge["target"],
            "http" if edge.get("kind") == "http" else None,
        )
        for edge in edges
    }
    found_nodes: set[str] = set()
    found_shapes: dict[str, str] = {}
    found_groups: set[str] = set()
    found_edges: set[tuple[str, str, str | None]] = set()
    in_group = False
    for line in lines[1:]:
        if re.fullmatch(r'  subgraph g[1-9][0-9]*\["[^"#;|\r\n]*"\]', line):
            group_id = re.search(r"subgraph (g[1-9][0-9]*)", line).group(1)
            if in_group or group_id in found_groups:
                raise ValueError("Mermaid group structure is invalid.")
            found_groups.add(group_id)
            in_group = True
        elif line == "    direction LR":
            if not in_group:
                raise ValueError("Mermaid direction must be inside a group.")
        elif re.fullmatch(r'    n[1-9][0-9]*\["[^"#;|\r\n]*"\]', line):
            if not in_group:
                raise ValueError("Mermaid nodes must be inside a group.")
            node_id = re.match(r"    (n[1-9][0-9]*)", line).group(1)
            if node_id in found_nodes:
                raise ValueError("Mermaid node ids must be unique.")
            found_nodes.add(node_id)
            found_shapes[node_id] = "source"
        elif re.fullmatch(r'  n[1-9][0-9]*\(\([^"#;|\r\n]*\)\)', line):
            if in_group:
                raise ValueError("External Mermaid nodes must remain outside source groups.")
            node_id = re.match(r"  (n[1-9][0-9]*)", line).group(1)
            if node_id in found_nodes:
                raise ValueError("Mermaid node ids must be unique.")
            found_nodes.add(node_id)
            found_shapes[node_id] = "actor"
        elif re.fullmatch(r'  n[1-9][0-9]*\(\[[^"#;|\r\n]*\]\)', line):
            if in_group:
                raise ValueError("External Mermaid nodes must remain outside source groups.")
            node_id = re.match(r"  (n[1-9][0-9]*)", line).group(1)
            if node_id in found_nodes:
                raise ValueError("Mermaid node ids must be unique.")
            found_nodes.add(node_id)
            found_shapes[node_id] = "service"
        elif re.fullmatch(r'  n[1-9][0-9]*\[\([^"#;|\r\n]*\)\]', line):
            if in_group:
                raise ValueError("External Mermaid nodes must remain outside source groups.")
            node_id = re.match(r"  (n[1-9][0-9]*)", line).group(1)
            if node_id in found_nodes:
                raise ValueError("Mermaid node ids must be unique.")
            found_nodes.add(node_id)
            found_shapes[node_id] = "database"
        elif line == "  end":
            if not in_group:
                raise ValueError("Mermaid group closing token has no group.")
            in_group = False
        elif re.fullmatch(
            r"  (?:n[1-9][0-9]*|g[1-9][0-9]*) (?:-->|-\.->)"
            r"(?:\|[^|#;\r\n]*\|)? (?:n[1-9][0-9]*|g[1-9][0-9]*)",
            line,
        ):
            match = re.match(
                r"  ((?:n|g)[1-9][0-9]*) (?P<arrow>-->|-\.->)", line
            )
            target = re.search(r" (n[1-9][0-9]*|g[1-9][0-9]*)$", line).group(1)
            kind = "http" if match.group("arrow") == "-.->" else None
            found_edges.add((match.group(1), target, kind))
        else:
            raise ValueError("Mermaid text contains syntax outside the generated template.")
    if in_group or found_nodes != expected_nodes or found_groups != expected_groups:
        raise ValueError("Mermaid nodes or groups do not match the JSON output.")
    if not external_nodes.isdisjoint(source_nodes) or not source_nodes.issubset(found_nodes):
        raise ValueError("Mermaid external and source nodes are inconsistent.")
    if any(found_shapes.get(node_id) != external_kinds[node_id]
           for node_id in external_nodes):
        raise ValueError("Mermaid external node shapes do not match their declared kinds.")
    if found_edges != expected_edges:
        raise ValueError("Mermaid edges do not match the JSON output.")

