import asyncio
from pathlib import Path

import httpx

from gitcontext import indexer
from gitcontext.ingest.remote import RemoteRepository
from gitcontext.web import create_app


def test_fastapi_ui_and_repository_api_end_to_end(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "auth.py").write_text(
        "def authenticate_user(token):\n    return token is not None\n"
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "index.sqlite"))
    monkeypatch.setenv("GITCONTEXT_INGEST_GITHUB", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    remote = RemoteRepository(
        "owner/repo", "https://github.com/owner/repo", "main", "abc123", repository
    )
    monkeypatch.setattr(
        "gitcontext.web.prepare_remote_repository",
        lambda value, token=None: (remote, [repository / "auth.py"]),
    )
    monkeypatch.setattr(
        "gitcontext.indexer.ensure_remote_snapshot",
        lambda repository, token=None: repository.cache_path,
    )
    monkeypatch.setattr(
        "gitcontext.web.index_remote_repository",
        lambda value: indexer.index_remote_repository(value, remote=remote),
    )
    monkeypatch.setattr(
        "gitcontext.web.remote_repository_is_stale",
        lambda value, token=None: indexer.remote_repository_is_stale(
            value, token, remote=remote
        ),
    )

    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200
            assert "Ask a question. Get the file it came from." in home.text
            assert (await client.get("/static/app.js")).status_code == 200
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
            answer = await client.post("/api/ask", json={
                "path": "https://github.com/owner/repo",
                "question": "How authenticate user?",
            })
            assert answer.status_code == 200
            assert "auth.py" in answer.json()["answer"]
            assert answer.json()["sources"][0]["path"] == "auth.py"

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
    monkeypatch.setattr(
        "gitcontext.web.prepare_remote_repository",
        lambda value, token=None: (remote, [source]),
    )
    monkeypatch.setattr(
        "gitcontext.indexer.ensure_remote_snapshot",
        lambda repository, token=None: repository.cache_path,
    )
    monkeypatch.setattr(
        "gitcontext.web.index_remote_repository",
        lambda value: indexer.index_remote_repository(value, remote=remote),
    )
    monkeypatch.setattr(
        "gitcontext.web.remote_repository_is_stale",
        lambda value, token=None: indexer.remote_repository_is_stale(value, remote=remote),
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
