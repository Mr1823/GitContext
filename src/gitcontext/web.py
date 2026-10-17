"""FastAPI service and static web UI for GitContext."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.indexer import (
    index_remote_repository,
    prepare_remote_repository,
    remote_repository_is_stale,
)
from gitcontext.retrieval import retrieve_with_metrics
from gitcontext.vector_store import VectorStore

WEB_ROOT = Path(__file__).with_name("static")
PUBLIC_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepositoryRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class QuestionRequest(RepositoryRequest):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)


def _public_github_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=(
                "Enter a public GitHub repository URL in this format: "
                "https://github.com/owner/repository."
            ),
        ) from error
    parts = parsed.path.split("/")
    slug = "/".join(parts[1:]) if len(parts) == 3 and parts[0] == "" else ""
    expected = f"https://github.com/{slug}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not PUBLIC_REPOSITORY.fullmatch(slug)
        or any(part in {".", ".."} for part in slug.split("/"))
        or value != expected
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Enter a public GitHub repository URL in this format: "
                "https://github.com/owner/repository."
            ),
        )
    return expected


def create_app() -> FastAPI:
    """Create the API app; exposed for tests and uvicorn deployments."""
    app = FastAPI(
        title="GitContext API",
        version="0.2.0",
        description="RAG API for public GitHub repositories.",
    )
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/scan", tags=["repository"])
    def scan(request: RepositoryRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        try:
            token = Settings.for_repository(Path.cwd()).github_token
            remote, files = prepare_remote_repository(repository_url, token)
            repository = remote.cache_path
            settings = Settings.for_repository(repository)
            indexed_chunks = (
                VectorStore(settings.database_path).count()
                if settings.database_path.is_file() else 0
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "repository": remote.url,
            "is_git": True,
            "remote": True,
            "count": len(files),
            "files": [file.relative_to(repository).as_posix() for file in files],
            "indexed_chunks": indexed_chunks,
        }

    @app.post("/api/index", tags=["repository"])
    def index(request: RepositoryRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        try:
            summary = index_remote_repository(repository_url)
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        return {
            "repository": str(summary.repository),
            "source_files": summary.source_files,
            "skipped_files": summary.skipped_files,
            "chunks": summary.chunks,
            "semantic_embeddings": summary.semantic_embeddings,
            "database_path": str(summary.database_path),
            "changed_files": summary.changed_files,
            "removed_files": summary.removed_files,
            "commits": summary.commits,
            "pull_requests": summary.pull_requests,
            "incidents": summary.incidents,
            "warnings": summary.warnings,
        }

    @app.post("/api/ask", tags=["retrieval"])
    def ask(request: QuestionRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        token = Settings.for_repository(Path.cwd()).github_token
        try:
            refreshed, remote_repository = remote_repository_is_stale(
                repository_url, token
            )
            repository = remote_repository.cache_path
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        settings = Settings.for_repository(repository)
        try:
            if refreshed:
                index_remote_repository(repository_url, remote=remote_repository)
            store = VectorStore(settings.database_path)
            if store.count() == 0:
                raise HTTPException(
                    status_code=409,
                    detail="This repository has no index yet. Build the index first.",
                )
            results, elapsed_ms = retrieve_with_metrics(
                request.question, store, request.top_k,
                settings.embedding_model, settings.reranker_model,
            )
            answer = answer_question(request.question, results, settings)
        except HTTPException:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        return {
            "answer": answer,
            "retrieval_ms": round(elapsed_ms, 2),
            "index_refreshed": refreshed,
            "sources": [
                {key: item.get(key) for key in (
                    "path", "start_line", "end_line", "symbol", "content", "score",
                    "source_type", "commit_sha", "author", "last_modified",
                    "pr_number", "source_url",
                )}
                for item in results
            ],
        }

    return app


app = create_app()
