import json
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from gitcontext.web import create_server


def test_web_ui_and_repository_api_work_end_to_end(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "auth.py").write_text(
        "def authenticate_user(token):\n    return token is not None\n"
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "index.sqlite"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = create_server("127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route: str, payload: dict) -> dict:
        request = Request(
            origin + route,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return json.load(response)

    try:
        with urlopen(origin + "/") as response:
            assert "Understand your codebase" in response.read().decode()
        scan = post("/api/scan", {"path": str(repository)})
        assert scan["files"] == ["auth.py"]
        indexed = post("/api/index", {"path": str(repository)})
        assert indexed["chunks"] > 0
        answer = post("/api/ask", {
            "path": str(repository), "question": "How authenticate user?",
        })
        assert "auth.py" in answer["answer"]
        assert answer["sources"][0]["path"] == "auth.py"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
