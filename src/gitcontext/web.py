"""FastAPI service and static web UI for GitContext."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.indexer import (
    index_remote_repository,
    index_repository,
    prepare_remote_repository,
    remote_repository_is_stale,
    repository_is_stale,
)
from gitcontext.ingest.remote import is_github_url
from gitcontext.ingest.repository import is_git_repository, scan_repository
from gitcontext.retrieval import retrieve_with_metrics
from gitcontext.vector_store import VectorStore

WEB_ROOT = Path(__file__).with_name("static")


class RepositoryRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class QuestionRequest(RepositoryRequest):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)


def _repository_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Repository folder does not exist: {path}")
    return path


def create_app() -> FastAPI:
    """Create the API app; exposed for tests and uvicorn deployments."""
    app = FastAPI(
        title="GitContext API",
        version="0.2.0",
        description="RAG backend for local folders and online GitHub repositories.",
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
        try:
            if is_github_url(request.path):
                token = Settings.for_repository(Path.cwd()).github_token
                remote, files = prepare_remote_repository(request.path, token)
                repository = remote.cache_path
                repository_name = remote.url
                is_git = True
            else:
                repository = _repository_path(request.path)
                files = scan_repository(repository)
                repository_name = str(repository)
                is_git = is_git_repository(repository)
            settings = Settings.for_repository(repository)
            indexed_chunks = (
                VectorStore(settings.database_path).count()
                if settings.database_path.is_file() else 0
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "repository": repository_name,
            "is_git": is_git,
            "remote": is_github_url(request.path),
            "count": len(files),
            "files": [file.relative_to(repository).as_posix() for file in files],
            "indexed_chunks": indexed_chunks,
        }

    @app.post("/api/index", tags=["repository"])
    def index(request: RepositoryRequest) -> dict[str, Any]:
        try:
            if is_github_url(request.path):
                summary = index_remote_repository(request.path)
            else:
                summary = index_repository(_repository_path(request.path))
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
        remote = is_github_url(request.path)
        if remote:
            token = Settings.for_repository(Path.cwd()).github_token
            try:
                refreshed, remote_repository = remote_repository_is_stale(
                    request.path, token
                )
                repository = remote_repository.cache_path
            except (OSError, RuntimeError, ValueError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        else:
            repository = _repository_path(request.path)
        settings = Settings.for_repository(repository)
        try:
            refreshed = (
                refreshed if remote else repository_is_stale(repository)
            )
            if refreshed:
                if remote:
                    index_remote_repository(request.path, remote=remote_repository)
                else:
                    index_repository(repository, refresh_external=False)
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
