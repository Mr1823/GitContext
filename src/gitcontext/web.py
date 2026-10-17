"""Local HTTP interface for GitContext's repository workflow."""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.indexer import index_repository
from gitcontext.ingest.repository import is_git_repository, scan_repository
from gitcontext.retrieval import retrieve
from gitcontext.vector_store import VectorStore

MAX_REQUEST_BYTES = 1_000_000
WEB_ROOT = Path(__file__).with_name("static")


class GitContextHandler(BaseHTTPRequestHandler):
    """Serve the single-page interface and its JSON API."""

    server_version = "GitContext/0.1"

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/" or route == "/index.html":
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif route == "/static/app.css":
            self._send_file(WEB_ROOT / "app.css", "text/css; charset=utf-8")
        elif route == "/static/app.js":
            self._send_file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
        elif route == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in {"/api/scan", "/api/index", "/api/ask"}:
            self._send_json(404, {"error": "Not found"})
            return
        try:
            payload = self._read_json()
            repository = self._repository_path(payload)
            if route == "/api/scan":
                files = scan_repository(repository)
                database_path = Settings.for_repository(repository).database_path
                indexed_chunks = (
                    VectorStore(database_path).count() if database_path.is_file() else 0
                )
                self._send_json(200, {
                    "repository": str(repository),
                    "is_git": is_git_repository(repository),
                    "count": len(files),
                    "indexed_chunks": indexed_chunks,
                    "files": [file.relative_to(repository).as_posix() for file in files],
                })
            elif route == "/api/index":
                summary = index_repository(repository)
                self._send_json(200, {
                    "repository": str(summary.repository),
                    "source_files": summary.source_files,
                    "skipped_files": summary.skipped_files,
                    "chunks": summary.chunks,
                    "semantic_embeddings": summary.semantic_embeddings,
                    "database_path": str(summary.database_path),
                })
            else:
                self._answer(repository, payload)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})
        except NotADirectoryError as error:
            self._send_json(400, {"error": str(error)})
        except (OSError, RuntimeError, sqlite3.Error) as error:
            self._send_json(500, {"error": str(error) or "GitContext could not complete the request."})

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send requests as application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request size.") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is empty or too large.")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise TypeError("Request body must be a JSON object.")
        return payload

    @staticmethod
    def _repository_path(payload: dict[str, Any]) -> Path:
        value = payload.get("path")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Enter a repository folder path.")
        repository = Path(value).expanduser().resolve()
        if not repository.is_dir():
            raise NotADirectoryError(f"Repository folder does not exist: {repository}")
        return repository

    def _answer(self, repository: Path, payload: dict[str, Any]) -> None:
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Enter a question about the repository.")
        top_k = payload.get("top_k", 6)
        if not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise ValueError("Evidence count must be between 1 and 20.")
        settings = Settings.for_repository(repository)
        store = VectorStore(settings.database_path)
        if store.count() == 0:
            self._send_json(409, {"error": "This repository has no index yet. Build the index first."})
            return
        results = retrieve(question, store, top_k, settings.embedding_model)
        answer = answer_question(question, results, settings)
        self._send_json(200, {
            "answer": answer,
            "sources": [
                {key: item[key] for key in ("path", "start_line", "end_line", "symbol", "content", "score")}
                for item in results
            ],
        })

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "The web interface asset was not found."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")

    def log_message(self, format: str, *args: Any) -> None:
        """Keep routine HTTP logs concise."""
        super().log_message(format, *args)


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create the threaded local web server."""
    return ThreadingHTTPServer((host, port), GitContextHandler)
