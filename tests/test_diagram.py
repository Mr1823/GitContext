import asyncio
import json
import re
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from gitcontext.cli import app as cli_app
from gitcontext.config import Settings
from gitcontext.diagram import (
    _display_edges,
    _display_http_edges,
    _fallback_groups,
    _load_diagram_extra,
    _make_external_nodes,
    _make_nodes,
    _merge_external_edges,
    _refine_directory_nodes,
    _validate_http_citations,
    _validate_labels,
    analysed_paths,
    analyze_http,
    cache_key,
    content_digest,
    extract_imports,
    extract_unresolved_imports,
    generate_diagram,
    render_mermaid,
    sanitize_label,
    validate_mermaid,
)
from gitcontext.ingest.parser import ParsedBlock
from gitcontext.vector_store import VectorStore
from gitcontext.web import create_app


def test_extracts_only_internal_python_and_relative_js_imports() -> None:
    contents = {
        "main.py": "import os\nfrom pkg.auth import login\n",
        "pkg/__init__.py": "",
        "pkg/auth.py": "from .tokens import verify\nimport requests\n",
        "pkg/tokens.py": "def verify():\n    return True\n",
        "web/app.js": (
            'import { button } from "./button";\n'
            'const thirdParty = require("axios");\n'
            'const missing = require("../not-indexed");\n'
        ),
        "web/button.ts": "export const button = true;\n",
    }

    edges = extract_imports(contents)

    assert [(edge["source_file"], edge["target_file"]) for edge in edges] == [
        ("main.py", "pkg/auth.py"),
        ("pkg/auth.py", "pkg/tokens.py"),
        ("web/app.js", "web/button.ts"),
    ]


def test_matches_express_calls_to_routes_and_reports_unmatched_calls() -> None:
    template = chr(96)
    dynamic_template = "/api/products/" + "$" + "{id}"
    contents = {
        "server/index.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const productsRouter = require('./routes/products');\n"
            "const ordersRouter = require('./routes/orders');\n"
            "app.use('/api/products', productsRouter);\n"
            "app.use('/api/orders', ordersRouter);\n"
        ),
        "server/routes/products.js": (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "router.get('/', handler);\n"
            "router.get('/:id', handler);\n"
            "module.exports = router;\n"
        ),
        "server/routes/orders.js": (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "router.post('/', handler);\n"
            "module.exports = router;\n"
        ),
        "client/app.js": (
            "import axios from 'axios';\n"
            "axios.get('/api/products');\n"
            + "axios.get(" + template + dynamic_template + template + ");\n"
            + "fetch('/api/orders', { method: 'POST' });\n"
            + "const api = axios.create({ baseURL: '/api' });\n"
            + "api.get('/products/42');\n"
            + "api.get('/orders/7');\n"
            + "const endpoint = getEndpoint();\n"
            + "axios.get(endpoint);\n"
            + "axios.get('/api/missing');\n"
        ),
    }
    result = analyze_http(contents, list(contents))

    assert result["backend_detected"] == "Express"
    assert [
        (
            item["method"], item["path"], item["source_file"],
            item["target_file"], item["match"],
            item["call_site"], item["route_site"],
        )
        for item in result["http_edges"]
    ] == [
        (
            "GET", "/api/products", "client/app.js",
            "server/routes/products.js", "route",
            {"path": "client/app.js", "line": 2},
            {"path": "server/routes/products.js", "line": 3},
        ),
        (
            "GET", "/api/products/:param", "client/app.js",
            "server/routes/products.js", "route",
            {"path": "client/app.js", "line": 3},
            {"path": "server/routes/products.js", "line": 4},
        ),
        (
            "POST", "/api/orders", "client/app.js",
            "server/routes/orders.js", "route",
            {"path": "client/app.js", "line": 4},
            {"path": "server/routes/orders.js", "line": 3},
        ),
        (
            "GET", "/api/products/42", "client/app.js",
            "server/routes/products.js", "route",
            {"path": "client/app.js", "line": 6},
            {"path": "server/routes/products.js", "line": 4},
        ),
        (
            "GET", "/api/orders/7", "client/app.js",
            "server/routes/orders.js", "prefix",
            {"path": "client/app.js", "line": 7},
            {"path": "server/index.js", "line": 6},
        ),
    ]
    assert [
        (item["call_site"], item["reason"])
        for item in result["unmatched_calls"]
    ] == [
        ({"path": "client/app.js", "line": 9}, "dynamic_url"),
        ({"path": "client/app.js", "line": 10}, "no_matching_route"),
    ]


def test_matches_fastapi_router_prefixes() -> None:
    contents = {
        "api/main.py": (
            "from fastapi import FastAPI\n"
            "from api.items import router as items_router\n"
            "app = FastAPI()\n"
            "app.include_router(items_router, prefix='/api/items')\n"
        ),
        "api/items.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/v1')\n"
            "@router.get('/{item_id}')\n"
            "def read_item(item_id: int):\n"
            "    return item_id\n"
        ),
        "frontend/app.js": "fetch('/api/items/v1/11');\n",
    }

    result = analyze_http(contents, list(contents))

    assert result["backend_detected"] == "FastAPI"
    assert result["http_edges"] == [{
        "source_file": "frontend/app.js",
        "target_file": "api/items.py",
        "method": "GET",
        "path": "/api/items/v1/11",
        "match": "route",
        "call_site": {"path": "frontend/app.js", "line": 1},
        "route_site": {"path": "api/items.py", "line": 3},
        "unknown_base_prefix": False,
    }]


def test_resolves_fetch_wrapper_calls_without_reporting_the_wrapper_body() -> None:
    contents = {
        "server/web.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.post('/api/items')\n"
            "def create_item():\n"
            "    return {}\n"
        ),
        "static/app.js": (
            "async function api(route, payload) {\n"
            "  const response = await fetch(route, { method: 'POST' });\n"
            "  return response;\n"
            "}\n"
            "api('/api/items', {});\n"
        ),
    }

    result = analyze_http(contents, list(contents))

    assert result["http_edges"] == [{
        "source_file": "static/app.js",
        "target_file": "server/web.py",
        "method": "POST",
        "path": "/api/items",
        "match": "route",
        "call_site": {"path": "static/app.js", "line": 5},
        "route_site": {"path": "server/web.py", "line": 3},
        "unknown_base_prefix": False,
    }]
    assert result["unmatched_calls"] == []


def test_follows_imported_axios_factory_returned_by_a_local_hook() -> None:
    contents = {
        "server/index.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const productsRouter = require('./routes/products');\n"
            "app.use('/api/products', productsRouter);\n"
        ),
        "server/routes/products.js": (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "router.get('/', handler);\n"
            "module.exports = router;\n"
        ),
        "src/hooks/useAxiosSecure.js": (
            "import axios from 'axios';\n"
            "const useAxiosSecure = () => {\n"
            "  const instance = axios.create({ baseURL: '/api' });\n"
            "  return [instance];\n"
            "};\n"
            "export default useAxiosSecure;\n"
        ),
        "src/pages/Products.jsx": (
            "import useAxiosSecure from '../hooks/useAxiosSecure';\n"
            "const [axiosSecure] = useAxiosSecure();\n"
            "axiosSecure.get('/products');\n"
        ),
    }

    result = analyze_http(contents, list(contents))

    assert len(result["http_edges"]) == 1
    assert result["http_edges"][0]["method"] == "GET"
    assert result["http_edges"][0]["path"] == "/api/products"
    assert result["http_edges"][0]["call_site"] == {
        "path": "src/pages/Products.jsx", "line": 3,
    }


def test_unresolved_imports_include_aliases_and_missing_barrels(tmp_path: Path) -> None:
    contents = {
        "src/app.js": (
            'import { missing } from "@/missing";\n'
            'import { button } from "./components";\n'
        ),
    }

    unresolved = extract_unresolved_imports(
        contents, contents, repository=tmp_path
    )

    assert [(item["line"], item["specifier"]) for item in unresolved] == [
        (1, "@/missing"),
        (2, "./components"),
    ]


def test_group_edges_merge_imports_and_http_by_group_pair() -> None:
    nodes = _make_nodes(
        ["frontend/app.js", "frontend/other.js", "server/routes.js"],
        layer_by_file={
            "frontend/app.js": "frontend",
            "frontend/other.js": "frontend",
            "server/routes.js": "backend",
        },
    )
    groups = [
        {"id": "g1", "name": "Browser client", "node_ids": ["n1", "n2"]},
        {"id": "g2", "name": "Server routes", "node_ids": ["n3"]},
    ]
    file_edges = [
        {"source_file": "frontend/app.js", "target_file": "server/routes.js"},
        {"source_file": "frontend/other.js", "target_file": "server/routes.js"},
    ]
    http_edges = [
        {"source_file": source, "target_file": "server/routes.js",
         "method": "GET", "path": "/api/items"}
        for source in ("frontend/app.js", "frontend/other.js")
    ]

    imports = _display_edges(nodes, file_edges, groups)
    calls = _display_http_edges(nodes, http_edges, groups, len(imports) + 1)

    assert len(imports) == len(calls) == 1
    assert imports[0]["import_count"] == 2
    assert calls[0]["api_count"] == 2


def test_diagram_extra_rejects_unknown_endpoints_and_sanitizes_hostile_labels(
    tmp_path: Path,
) -> None:
    (tmp_path / "diagram.extra.json").write_text(
        json.dumps({
            "nodes": [{
                "id": "stripe", "label": 'Stripe "[]#;\n service', "kind": "service",
            }],
            "edges": [{
                "from": "stripe", "to": "missing.py",
                "label": 'calls "payments"; #\n now',
            }],
        }),
        encoding="utf-8",
    )
    extra, _ = _load_diagram_extra(tmp_path)
    external_nodes = _make_external_nodes(extra["nodes"], 2)
    source_nodes = [{"id": "n1", "path": "src/app.js", "files": ["src/app.js"]}]
    groups = [{"id": "g1", "name": "Frontend", "node_ids": ["n1"]}]

    assert extra["nodes"][0]["label"] == "Stripe service"
    assert extra["edges"][0]["label"] == "calls payments now"
    with pytest.raises(ValueError, match="unknown node"):
        _merge_external_edges(
            extra["edges"], external_nodes, groups, 1, source_nodes
        )


def test_external_node_shapes_and_hostile_mermaid_text_pass_template_validation(
    tmp_path: Path,
) -> None:
    source_nodes = _make_nodes(["src/checkout.py"])
    definitions = [
        {"key": "browser", "label": 'Browser "[]#;\n actor', "kind": "actor"},
        {"key": "stripe", "label": "Stripe", "kind": "service"},
        {"key": "mongodb", "label": "MongoDB", "kind": "database"},
    ]
    external_nodes = _make_external_nodes(definitions, 2)
    groups = [{"id": "g1", "name": "Checkout", "node_ids": ["n1"]}]
    extra_edges = [
        {"from": "src/checkout.py", "to": key, "label": "connects system"}
        for key in ("browser", "stripe", "mongodb")
    ]
    edges = _merge_external_edges(
        extra_edges, external_nodes, groups, 1, source_nodes
    )
    labels = {
        "n1": {"phrase": "checkout.py", "fallback": True},
        **{
            node["id"]: {"phrase": node["label"]}
            for node in external_nodes
        },
        **{
            edge["id"]: {"phrase": edge["label"]}
            for edge in edges
        },
    }
    mermaid = render_mermaid(
        source_nodes + external_nodes, edges, groups, labels
    )

    validate_mermaid(mermaid, source_nodes + external_nodes, groups, edges)
    assert "n2((Browser actor))" in mermaid
    assert "n3([Stripe])" in mermaid
    assert "n4[(MongoDB)]" in mermaid
    assert not re.search(r"[#;]", mermaid)
    assert 'Browser "[]' not in mermaid


def test_http_citations_must_reference_real_source_lines() -> None:
    edge = {
        "source_file": "client/app.js",
        "target_file": "server/routes.js",
        "call_site": {"path": "client/app.js", "line": 4},
        "route_site": {"path": "server/routes.js", "line": 1},
    }

    with pytest.raises(ValueError, match="invalid indexed citation"):
        _validate_http_citations(
            [edge],
            {"client/app.js", "server/routes.js"},
            {"client/app.js": "fetch('/api');", "server/routes.js": "router.get('/')"},
        )


def test_cache_key_changes_when_diagram_version_changes(monkeypatch) -> None:
    import gitcontext.diagram as diagram_module

    first = cache_key("repo", "commit", "digest")
    monkeypatch.setattr(
        diagram_module, "DIAGRAM_VERSION", diagram_module.DIAGRAM_VERSION + 1
    )

    assert cache_key("repo", "commit", "digest") != first


def test_diagram_excludes_tests_by_default_and_collapses_largest_folder_first() -> None:
    paths = [f"src/core/file_{index}.py" for index in range(12)]
    paths += [f"src/util/file_{index}.py" for index in range(10)]
    paths += [f"tests/test_case_{index}.py" for index in range(8)]

    assert len(analysed_paths(paths)) == 22
    assert not any("tests/" in path for path in analysed_paths(paths))

    included = analysed_paths(paths, include_tests=True)
    nodes = _make_nodes(included)

    assert len(included) == 30
    assert len(nodes) == 19
    collapsed = [node for node in nodes if len(node["files"]) > 1]
    assert len(collapsed) == 1
    assert collapsed[0]["path"] == "src/core"
    assert len(collapsed[0]["files"]) == 12
    assert sum(len(node["files"]) == 1 for node in nodes) == 18


def test_directory_refinement_uses_spare_slots_for_largest_subfolders() -> None:
    files = [f"src/pages/page_{index}.jsx" for index in range(8)]
    files += [f"src/components/component_{index}.jsx" for index in range(5)]
    files += [f"src/hooks/hook_{index}.jsx" for index in range(4)]
    files += ["src/App.jsx", "src/main.jsx"]
    nodes = [{"id": "n1", "kind": "source", "path": "src", "files": files}]

    refined = _refine_directory_nodes(nodes, max_nodes=4)

    assert len(refined) == 4
    assert {path for node in refined for path in node["files"]} == set(files)
    assert {node["path"] for node in refined} >= {
        "src/pages", "src/components", "src/hooks",
    }


def test_fallback_groups_merge_singletons_and_avoid_single_node_wrappers() -> None:
    paths = [f"server/routes/route_{index}.js" for index in range(5)]
    paths += [f"src/pages/page_{index}.jsx" for index in range(4)]
    paths += ["shared/helpers.js", "utils/format.js", "entry.js", "main.js"]
    nodes = _make_nodes(paths)
    layers = {
        path: (
            "backend" if path.startswith("server/") else
            "frontend" if path.startswith("src/") else "shared"
        )
        for path in paths
    }

    groups = _fallback_groups(nodes, layers)

    assert 5 <= len(groups) <= 8
    assert all(len(group["node_ids"]) >= 2 for group in groups)
    node_layers = {
        node["id"]: {layers[path] for path in node["files"]}
        for node in nodes
    }
    assert all(
        not {"frontend", "backend"}.issubset(
            set().union(*(node_layers[node_id] for node_id in group["node_ids"]))
        )
        for group in groups
    )


def test_nodes_include_isolated_python_and_typescript_files() -> None:
    nodes = _make_nodes(["main.py", "unused.py", "ui/widget.ts"])

    assert [node["files"] for node in nodes] == [
        ["main.py"], ["ui/widget.ts"], ["unused.py"]
    ]


def test_cache_digest_tracks_only_analysed_file_contents() -> None:
    paths = ["main.py", "README.md", "settings.json"]
    original = {"main.py": "import auth", "README.md": "old", "settings.json": "{}"}
    digest = content_digest(paths, original)

    assert content_digest(
        paths, {"main.py": "import auth", "README.md": "new", "settings.json": "[]"}
    ) == digest
    assert content_digest(paths, {"main.py": "import other"}) != digest
    assert cache_key("local-a", None, digest) == cache_key("local-b", None, digest)
    assert cache_key("https://github.com/o/r", "head-a", digest) != cache_key(
        "https://github.com/o/r", "head-b", digest
    )


def test_include_tests_changes_diagram_content_digest() -> None:
    paths = ["src/main.py", "tests/test_main.py"]
    contents = {"src/main.py": "pass", "tests/test_main.py": "assert True"}

    assert content_digest(paths, contents) != content_digest(
        paths, contents, include_tests=True
    )


def test_rejects_llm_label_for_edge_not_in_the_graph() -> None:
    task = {
        "id": "e1", "kind": "edge", "files": [],
        "evidence": [{
            "chunk_id": 7, "path": "main.py", "start_line": 1,
            "end_line": 1, "content": "from auth import login",
        }],
    }

    with pytest.raises(ValueError, match="unknown node or edge id"):
        _validate_labels(
            {"labels": [{"id": "e99", "phrase": "imports auth", "chunk_id": 7}]},
            [task],
        )


def test_rejects_extra_llm_diagram_entities() -> None:
    task = {
        "id": "n1", "kind": "node", "files": ["main.py"],
        "evidence": [{"chunk_id": 7, "path": "main.py", "start_line": 1,
                      "end_line": 1, "content": "# main flow"}],
    }

    with pytest.raises(ValueError, match="cannot add nodes, edges"):
        _validate_labels(
            {"labels": [{"id": "n1", "phrase": "main flow", "chunk_id": 7}],
             "edges": [{"source": "n1", "target": "n2"}]},
            [task],
        )


def test_mermaid_sanitizes_hostile_paths_and_labels() -> None:
    hostile_path = 'src/bad"[]#;\nclick.py'
    nodes = [{"id": "n1", "path": "src", "files": [hostile_path]}]
    groups = [{"id": "g1", "name": 'source";\n#group', "node_ids": ["n1"]}]
    labels = {
        "n1": {"phrase": 'handles "auth"; # click | now', "path": hostile_path}
    }

    mermaid = render_mermaid(nodes, [], groups, labels)

    validate_mermaid(mermaid, nodes, groups, [])
    assert "n1[" in mermaid
    assert hostile_path not in mermaid
    assert "handles auth click now [bad click.py]" in mermaid
    assert not re.search(r"[#;|]", mermaid)
    assert sanitize_label('x"] ;#\n y|<z>') == "x y z"


def _settings(repository: Path, monkeypatch, *, llm: bool = False) -> Settings:
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(repository / "index.sqlite"))
    if llm:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GITCONTEXT_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("GITCONTEXT_RERANKER_MODEL", raising=False)
    return Settings.for_repository(repository)


def _indexed_store(repository: Path, settings: Settings) -> VectorStore:
    store = VectorStore(settings.database_path)
    chunks = [
        ParsedBlock("main.py", 1, 2, "main", "# starts main flow\nfrom auth import login"),
        ParsedBlock("auth.py", 1, 3, "login", "# handles auth tokens\ndef login():\n    return True"),
    ]
    store.replace_all(chunks)
    for chunk in chunks:
        store.replace_file(
            chunk.path,
            sha256(chunk.content.encode()).hexdigest(),
            [chunk],
        )
    return store


def _fake_llm(prompt: str, settings: Settings) -> str:
    del settings
    if "Purpose-based starting groups:" in prompt:
        candidates = json.loads(
            prompt.split("Purpose-based starting groups: ", 1)[1]
            .split("\nNode files:", 1)[0]
        )
        node_ids = [node_id for group in candidates for node_id in group["node_ids"]]
        return json.dumps({"groups": [{"name": "Source files", "node_ids": node_ids}]})
    items = json.loads(prompt.split("Items and evidence: ", 1)[1])
    labels = []
    for item in items:
        evidence = item["evidence"][0]
        if item["kind"] == "edge":
            phrase = "reads auth"
        elif item["files"][0].endswith("main.py"):
            phrase = "starts main flow"
        else:
            phrase = "handles auth tokens"
        labels.append({"id": item["id"], "phrase": phrase,
                       "chunk_id": evidence["chunk_id"]})
    return json.dumps({"labels": labels})


def test_cache_hit_uses_zero_llm_calls(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, monkeypatch, llm=True)
    store = _indexed_store(tmp_path, settings)
    contents = {
        "main.py": "# starts main flow\nfrom auth import login\n",
        "auth.py": "# handles auth tokens\ndef login():\n    return True\n",
    }
    paths = [*contents, "README.md", "settings.toml", "static/app.css"]
    calls = []

    def fake(prompt: str, config: Settings) -> str:
        calls.append(prompt)
        return _fake_llm(prompt, config)

    first = generate_diagram(
        tmp_path, str(tmp_path), None, paths, contents, "GitContext source", store,
        settings, llm_complete=fake,
    )
    second = generate_diagram(
        tmp_path, str(tmp_path), None, paths, contents, "GitContext source", store,
        settings, llm_complete=lambda *_: pytest.fail("cache hit called the LLM"),
    )

    assert first["cache_hit"] is False
    assert first["llm_calls_made"] == 2
    assert first["labels_status"] == "llm"
    assert first["not_analysed"] == [
        {"extension": ".css", "file_count": 1},
        {"extension": ".md", "file_count": 1},
        {"extension": ".toml", "file_count": 1},
    ]
    assert second["cache_hit"] is True
    assert second["llm_calls_made"] == 0
    assert len(calls) == 2
    assert second["mermaid"].startswith("flowchart TB\n")


def test_invalid_json_retries_once_with_the_validation_error(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, llm=True)
    store = _indexed_store(tmp_path, settings)
    contents = {
        "main.py": "# starts main flow\nfrom auth import login",
        "auth.py": "# handles auth tokens\ndef login():\n    return True",
    }
    prompts = []

    def retrying_llm(prompt: str, config: Settings) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "not valid json"
        return _fake_llm(prompt, config)

    diagnostics: list[dict[str, str]] = []
    result = generate_diagram(
        tmp_path, str(tmp_path), None, [*contents], contents, "", store, settings,
        llm_complete=retrying_llm, run_diagnostics=diagnostics,
    )

    assert result["llm_calls_made"] == 3
    assert "Previous response error" in prompts[1]
    assert sum(item["type"] == "retry" for item in diagnostics) == 1


def test_diagram_api_only_reads_existing_index(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "remotes"
    repository = cache / "owner" / "repo"
    repository.mkdir(parents=True)
    monkeypatch.setenv("GITCONTEXT_REMOTE_CACHE", str(cache))
    monkeypatch.delenv("GITCONTEXT_DATABASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("gitcontext.web._cache_root", lambda: cache)

    def forbidden(*args, **kwargs):
        raise AssertionError("Diagram API triggered an index or remote operation")

    monkeypatch.setattr("gitcontext.web.prepare_remote_repository", forbidden)
    monkeypatch.setattr("gitcontext.web.index_remote_repository", forbidden)
    settings = Settings.for_repository(repository)
    store = VectorStore(settings.database_path)
    source = "from auth import login\n"
    auth = "def login():\n    return True\n"
    for path, content in (("main.py", source), ("auth.py", auth)):
        store.replace_file(
            path, sha256(content.encode()).hexdigest(),
            [ParsedBlock(path, 1, len(content.splitlines()), "", content)],
        )
    store.set_value("remote_url", "https://github.com/owner/repo")
    store.set_value("remote_head_sha", "abc123")
    store.set_value("head_sha", "abc123")

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await client.get(
                "/api/diagram", params={"repo": "https://github.com/owner/repo"}
            )
            assert result.status_code == 200
            assert result.json()["commit_sha"] == "abc123"
            assert result.json()["labels_status"] == "fallback (no LLM key)"
            assert result.json()["llm_calls_made"] == 0
            assert result.json()["cache_hit"] is False
            cached = await client.get(
                "/api/diagram", params={"repo": "https://github.com/owner/repo"}
            )
            assert cached.status_code == 200
            assert cached.json()["cache_hit"] is True
            assert cached.json()["llm_calls_made"] == 0

    asyncio.run(workflow())


def test_diagram_api_returns_409_without_an_index(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "remotes"
    monkeypatch.setattr("gitcontext.web._cache_root", lambda: cache)

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/diagram", params={"repo": "https://github.com/owner/repo"}
            )
            assert response.status_code == 409
            assert "no existing index" in response.json()["detail"]

    asyncio.run(workflow())


def test_diagram_api_rejects_non_github_urls(tmp_path: Path) -> None:
    invalid_values = [str(tmp_path), "file:///tmp/repository", "https://example.com/o/r"]

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for value in invalid_values:
                response = await client.get("/api/diagram", params={"repo": value})
                assert response.status_code == 400
                assert "https://github.com/owner/repository" in response.json()["detail"]

    asyncio.run(workflow())


def test_cli_diagram_writes_json_and_mermaid_from_existing_index(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    main = "# starts main flow\nfrom auth import login"
    auth = "# handles auth tokens\ndef login():\n    return True"
    (repository / "main.py").write_text(main, encoding="utf-8")
    (repository / "auth.py").write_text(auth, encoding="utf-8")
    (repository / "README.md").write_text("GitContext repository", encoding="utf-8")
    settings = _settings(repository, monkeypatch)
    _indexed_store(repository, settings)
    output = tmp_path / "diagram-output"

    result = CliRunner().invoke(cli_app, ["diagram", str(repository), "--out", str(output)])

    assert result.exit_code == 0, result.output
    payload = json.loads((output / "diagram.json").read_text(encoding="utf-8"))
    assert payload["labels_status"] == "fallback (no LLM key)"
    assert (output / "diagram.mmd").read_text(encoding="utf-8").startswith("flowchart TB\n")
    assert "Nodes" in result.output and "Edges" in result.output


def test_cli_diagram_has_include_tests_option() -> None:
    result = CliRunner().invoke(cli_app, ["diagram", "--help"])

    assert result.exit_code == 0
    assert "--include-tests" in result.output
