import asyncio
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gitcontext import indexer
from gitcontext.ingest.errors import GitHubAPIError
from gitcontext.ingest.remote import RemoteRepository
from gitcontext.records import ParsedBlock
from gitcontext.vector_store import VectorStore
from gitcontext.web import _RATE_EVENTS, _REPOSITORY_LOCKS, create_app


@pytest.fixture(autouse=True)
def reset_rate_events():
    _RATE_EVENTS.clear()
    yield
    _RATE_EVENTS.clear()


def test_fastapi_ui_and_repository_api_end_to_end(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "cache" / "owner" / "repo"
    repository.mkdir(parents=True)
    (repository / "auth.py").write_text(
        "def authenticate_user(token):\n    return token is not None\n"
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("GITCONTEXT_INGEST_GITHUB", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("gitcontext.web._cache_root", lambda: tmp_path / "cache")
    remote = RemoteRepository(
        "owner/repo", "https://github.com/owner/repo", "main", "abc123", repository
    )
    monkeypatch.setattr(
        "gitcontext.web.prepare_remote_repository",
        lambda value, token=None, **kwargs: (remote, [repository / "auth.py"]),
    )
    monkeypatch.setattr(
        "gitcontext.indexer.ensure_remote_snapshot",
        lambda repository, token=None: repository.cache_path,
    )
    monkeypatch.setattr(
        "gitcontext.web.index_remote_repository",
        lambda value, **kwargs: indexer.index_remote_repository(value, remote=remote),
    )
    monkeypatch.setattr(
        "gitcontext.web.remote_repository_is_stale",
        lambda value, token=None, **kwargs: indexer.remote_repository_is_stale(
            value, token, remote=remote
        ),
    )

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200
            assert '<div id="app-root"></div>' in home.text
            script = re.search(r'src="([^"]+\.js)"', home.text)
            assert script
            assert (await client.get(script.group(1))).status_code == 200
            assert (await client.get("/static/app.css?v=16")).status_code == 200
            scan = await client.post(
                "/api/scan", json={"path": "https://github.com/owner/repo"}
            )
            assert scan.status_code == 200
            assert scan.json()["files"] == ["auth.py"]
            indexed = await client.post(
                "/api/index", json={"path": "https://github.com/owner/repo"}
            )
            assert indexed.status_code == 200
            assert indexed.json()["chunks"] > 0
            progress = await client.get(
                "/api/index/progress",
                params={"repo": "https://github.com/owner/repo"},
            )
            assert progress.status_code == 200
            assert progress.json()["status"] == "complete"
            source = await client.get(
                "/api/file",
                params={"repo": "https://github.com/owner/repo", "path": "auth.py"},
            )
            assert source.status_code == 200
            assert "authenticate_user" in source.json()["content"]
            traversal = await client.get(
                "/api/file",
                params={"repo": "https://github.com/owner/repo", "path": "../repo/auth.py"},
            )
            assert traversal.status_code == 400
            answer = await client.post("/api/ask", json={
                "path": "https://github.com/owner/repo",
                "question": "How authenticate user?",
            })
            assert answer.status_code == 200
            assert "auth.py" in answer.json()["answer"]
            assert answer.json()["sources"][0]["path"] == "auth.py"

    asyncio.run(workflow())


def test_web_404_returns_branded_html_for_browser_and_json_for_api() -> None:
    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/missing-page", headers={"accept": "text/html"})
            assert page.status_code == 404
            assert "Page not found" in page.text
            assert 'href="/">Return to GitContext' in page.text

            api = await client.get("/missing-api-route", headers={"accept": "application/json"})
            assert api.status_code == 404
            assert api.json() == {"detail": "Not found"}

    asyncio.run(workflow())


def test_web_scan_accepts_github_url_without_local_folder(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "managed-cache"
    repository.mkdir()
    source = repository / "main.py"
    source.write_text("def main():\n    return True\n")
    remote = RemoteRepository(
        "owner/repo", "https://github.com/owner/repo", "main", "abc123", repository
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "remote.sqlite"))
    monkeypatch.setenv("GITCONTEXT_INGEST_GITHUB", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    index_calls = []
    monkeypatch.setattr(
        "gitcontext.web.prepare_remote_repository",
        lambda value, token=None, **kwargs: (remote, [source]),
    )
    monkeypatch.setattr(
        "gitcontext.indexer.ensure_remote_snapshot",
        lambda repository, token=None: repository.cache_path,
    )

    def index_remote(value, **kwargs):
        index_calls.append(value)
        return indexer.index_remote_repository(value, remote=remote)

    monkeypatch.setattr(
        "gitcontext.web.index_remote_repository",
        index_remote,
    )
    monkeypatch.setattr(
        "gitcontext.web.remote_repository_is_stale",
        lambda value, token=None, **kwargs: (True, remote),
    )

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/scan", json={"path": "https://github.com/owner/repo"}
            )
            assert response.status_code == 200
            assert response.json()["repository"] == remote.url
            assert response.json()["remote"] is True
            assert response.json()["files"] == ["main.py"]
            indexed = await client.post(
                "/api/index", json={"path": "https://github.com/owner/repo"}
            )
            assert indexed.status_code == 200
            assert indexed.json()["chunks"] > 0
            answer = await client.post(
                "/api/ask",
                json={
                    "path": "https://github.com/owner/repo",
                    "question": "What does main return?",
                },
            )
            assert answer.status_code == 200
            assert "main.py" in answer.json()["answer"]
            assert answer.json()["index_refreshed"] is True
            assert index_calls == [remote.url, remote.url]

    asyncio.run(workflow())


def test_web_repository_routes_reject_non_github_inputs_before_access(
    tmp_path: Path, monkeypatch
) -> None:
    def unexpected_access(*args, **kwargs):
        raise AssertionError("Invalid repository input reached repository access")

    monkeypatch.setattr("gitcontext.web.prepare_remote_repository", unexpected_access)
    monkeypatch.setattr("gitcontext.web.index_remote_repository", unexpected_access)
    monkeypatch.setattr("gitcontext.web.remote_repository_is_stale", unexpected_access)

    invalid_values = [str(tmp_path), "file:///tmp/repository", "https://example.com/o/r"]

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in invalid_values:
                requests = [
                    client.post("/api/scan", json={"path": path}),
                    client.post("/api/index", json={"path": path}),
                    client.post(
                        "/api/ask", json={"path": path, "question": "What is here?"}
                    ),
                ]
                for response in await asyncio.gather(*requests):
                    assert response.status_code == 400
                    assert "https://github.com/owner/repository" in response.json()["detail"]

    asyncio.run(workflow())


def test_web_repository_upstream_failures_return_gateway_errors(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise GitHubAPIError("GitHub returned HTTP 503. private upstream details")

    monkeypatch.setattr("gitcontext.web.prepare_remote_repository", unavailable)
    monkeypatch.setattr("gitcontext.web.index_remote_repository", unavailable)
    monkeypatch.setattr("gitcontext.web.remote_repository_is_stale", unavailable)

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    "/api/scan", json={"path": "https://github.com/owner/repo"}
                ),
                await client.post(
                    "/api/index", json={"path": "https://github.com/owner/repo"}
                ),
                await client.post(
                    "/api/ask",
                    json={"path": "https://github.com/owner/repo", "question": "Where?"},
                ),
            ]

        for response in responses:
            assert response.status_code == 502
            assert "private upstream details" not in response.text

    asyncio.run(workflow())


def test_web_ask_hides_internal_answer_errors(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(database))
    monkeypatch.setenv("GITCONTEXT_INGEST_GITHUB", "false")
    store = VectorStore(database)
    store.replace_all([ParsedBlock("auth.py", 1, 2, "login", "def login(): pass")])
    monkeypatch.setattr(
        "gitcontext.web.remote_repository_is_stale", lambda *args, **kwargs: (
            False,
            RemoteRepository(
                "owner/repo", "https://github.com/owner/repo", "main", "abc123", tmp_path
            ),
        )
    )

    def fail_answer(*args, **kwargs):
        raise RuntimeError("private provider response and secret detail")

    monkeypatch.setattr("gitcontext.web.answer_question", fail_answer)

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/ask",
                json={
                    "path": "https://github.com/owner/repo",
                    "question": "Where is login?",
                },
            )
        assert response.status_code == 500
        assert response.json() == {"detail": "Could not answer from this repository."}
        assert "private provider response" not in response.text

    asyncio.run(workflow())


def test_web_index_rate_limit_returns_429(monkeypatch) -> None:
    _RATE_EVENTS.clear()

    def reject_index(*args, **kwargs):
        raise ValueError("fixture stops before indexing")

    monkeypatch.setattr("gitcontext.web.index_remote_repository", reject_index)

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(5):
                response = await client.post(
                    "/api/index", json={"path": "https://github.com/owner/repo"}
                )
                assert response.status_code == 400
            limited = await client.post(
                "/api/index", json={"path": "https://github.com/owner/repo"}
            )
        assert limited.status_code == 429
        assert "Rate limit reached for index" in limited.json()["detail"]
        assert int(limited.headers["retry-after"]) > 0

    asyncio.run(workflow())


def test_web_concurrent_index_for_same_repository_returns_409(monkeypatch, tmp_path: Path) -> None:
    _RATE_EVENTS.clear()
    key = "owner/repo"
    _REPOSITORY_LOCKS.pop(key, None)
    started = threading.Event()
    finish = threading.Event()

    def slow_index(*args, **kwargs):
        started.set()
        assert finish.wait(timeout=3)
        return SimpleNamespace(
            repository=tmp_path,
            source_files=1,
            skipped_files=0,
            chunks=1,
            semantic_embeddings=False,
            database_path=tmp_path / "index.sqlite",
            changed_files=1,
            removed_files=0,
            commits=0,
            pull_requests=0,
            incidents=0,
            warnings=[],
        )

    monkeypatch.setattr("gitcontext.web.index_remote_repository", slow_index)

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post(
                "/api/index", json={"path": "https://github.com/Owner/Repo"}
            ))
            assert await asyncio.to_thread(started.wait, 1)
            second = await client.post(
                "/api/index", json={"path": "https://github.com/owner/repo"}
            )
            finish.set()
            first_response = await first
        assert first_response.status_code == 200
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]

    asyncio.run(workflow())
