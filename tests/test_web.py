import asyncio
from pathlib import Path

import httpx

from gitcontext.web import create_app


def test_fastapi_ui_and_repository_api_end_to_end(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "auth.py").write_text(
        "def authenticate_user(token):\n    return token is not None\n"
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "index.sqlite"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def workflow() -> None:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200
            assert "Understand your codebase" in home.text
            assert (await client.get("/static/app.js")).status_code == 200
            scan = await client.post("/api/scan", json={"path": str(repository)})
            assert scan.status_code == 200
            assert scan.json()["files"] == ["auth.py"]
            indexed = await client.post("/api/index", json={"path": str(repository)})
            assert indexed.status_code == 200
            assert indexed.json()["chunks"] > 0
            answer = await client.post("/api/ask", json={
                "path": str(repository), "question": "How authenticate user?",
            })
            assert answer.status_code == 200
            assert "auth.py" in answer.json()["answer"]
            assert answer.json()["sources"][0]["path"] == "auth.py"

    asyncio.run(workflow())
