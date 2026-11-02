"""FastAPI service and static web UI for GitContext."""

import asyncio
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gitcontext.answer import answer_question, ensure_supported_citations
from gitcontext.config import Settings
from gitcontext.diagram import (
    generate_diagram,
    indexed_readme,
    reconstruct_indexed_contents,
    source_extension,
)
from gitcontext.indexer import (
    index_remote_repository,
    prepare_remote_repository,
    remote_repository_is_stale,
)
from gitcontext.ingest.errors import GitHubAPIError
from gitcontext.ingest.remote import _cache_root
from gitcontext.retrieval import retrieve_with_metrics
from gitcontext.vector_store import VectorStore

WEB_ROOT = Path(__file__).with_name("static")
PUBLIC_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPOSITORY_LOCKS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_RATE_EVENTS: defaultdict[tuple[str, str], deque[float]] = defaultdict(deque)


def _repository_lock_key(repository_url: str) -> str:
    return repository_url.removeprefix("https://github.com/").casefold()


async def _acquire_repository_lock(repository_url: str) -> asyncio.Lock:
    lock = _REPOSITORY_LOCKS[_repository_lock_key(repository_url)]
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="An indexing or refresh job is already running for this repository. Try again shortly.",
        )
    await lock.acquire()
    return lock


def _rate_limit_dependency(scope: str, limit: int, window_seconds: int):
    async def check_rate_limit(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        events = _RATE_EVENTS[(scope, client_ip)]
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(window_seconds - (now - events[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit reached for {scope}. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)

    return check_rate_limit


ASK_RATE_LIMIT = Depends(_rate_limit_dependency("ask", 15, 60))
DIAGRAM_RATE_LIMIT = Depends(_rate_limit_dependency("diagram", 15, 60))
SCAN_RATE_LIMIT = Depends(_rate_limit_dependency("scan", 15, 60))
INDEX_RATE_LIMIT = Depends(_rate_limit_dependency("index", 5, 300))


def _diagram_for_repository(repository_url: str) -> dict[str, Any]:
    owner, name = repository_url.removeprefix("https://github.com/").split("/")
    repository = _cache_root() / owner / name
    settings = Settings.for_repository(repository)
    if not settings.database_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="This repository has no existing index. Build its index first.",
        )
    store = VectorStore(settings.database_path)
    if store.value("remote_url") != repository_url:
        raise HTTPException(
            status_code=409,
            detail="This repository has no existing index. Build its index first.",
        )
    commit_sha = store.value("remote_head_sha")
    if not commit_sha:
        raise HTTPException(
            status_code=409,
            detail="This repository has no indexed commit. Build its index first.",
        )
    hashes = store.file_hashes()
    paths = sorted(hashes)
    source_files = [path for path in paths if source_extension(path) in {
        ".py", ".js", ".jsx", ".ts", ".tsx"
    }]
    if not source_files:
        raise HTTPException(
            status_code=409,
            detail="The existing index contains no Python or JS/TS source files.",
        )
    chunks = store.all()
    contents = reconstruct_indexed_contents(chunks, source_files)
    return generate_diagram(
        repository, repository_url, commit_sha, paths, contents,
        indexed_readme(chunks), store, settings, stored_hashes=hashes,
    )


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
        return FileResponse(WEB_ROOT / "build" / "index.html")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(404)
    async def not_found(request: Request, error: Exception):
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return HTMLResponse(
            status_code=404,
            content=(
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="theme-color" content="#111410"><meta name="robots" content="noindex">'
                '<title>Page not found - GitContext</title>'
                '<style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
                'background:#111410;color:#eef1e9;font:16px/1.6 system-ui,sans-serif}'
                'main{max-width:34rem;padding:2rem}h1{font-size:2rem;margin:0 0 .5rem}'
                'p{color:#b6beb0}a{display:inline-flex;min-height:44px;align-items:center;'
                'padding:0 1rem;border-radius:6px;background:#b9f279;color:#172111;'
                'font-weight:600;text-decoration:none}a:focus-visible{outline:3px solid white;'
                'outline-offset:3px}</style></head><body><main><h1>Page not found</h1>'
                '<p>That GitContext page does not exist.</p>'
                '<a href="/">Return to GitContext</a></main></body></html>'
            ),
        )

    @app.post("/api/scan", tags=["repository"], dependencies=[SCAN_RATE_LIMIT])
    async def scan(request: RepositoryRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        lock = await _acquire_repository_lock(repository_url)
        try:
            try:
                token = Settings.for_repository(Path.cwd()).github_token
                remote, files = await asyncio.to_thread(
                    prepare_remote_repository, repository_url, token, public_only=True
                )
                repository = remote.cache_path
                settings = Settings.for_repository(repository)
                indexed_chunks = (
                    await asyncio.to_thread(VectorStore(settings.database_path).count)
                    if settings.database_path.is_file() else 0
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            except GitHubAPIError as error:
                raise HTTPException(
                    status_code=502, detail="GitHub could not complete the repository request."
                ) from error
            except (OSError, RuntimeError) as error:
                raise HTTPException(
                    status_code=500, detail="Could not scan the repository."
                ) from error
            return {
                "repository": remote.url,
                "is_git": True,
                "remote": True,
                "count": len(files),
                "files": [file.relative_to(repository).as_posix() for file in files],
                "indexed_chunks": indexed_chunks,
            }
        finally:
            lock.release()

    @app.post("/api/index", tags=["repository"], dependencies=[INDEX_RATE_LIMIT])
    async def index(request: RepositoryRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        lock = await _acquire_repository_lock(repository_url)
        try:
            summary = await asyncio.to_thread(
                index_remote_repository, repository_url, public_only=True
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except GitHubAPIError as error:
            raise HTTPException(
                status_code=502, detail="GitHub could not complete the repository request."
            ) from error
        except (OSError, RuntimeError) as error:
            raise HTTPException(status_code=500, detail="Could not build the repository index.") from error
        finally:
            lock.release()
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

    @app.post("/api/ask", tags=["retrieval"], dependencies=[ASK_RATE_LIMIT])
    async def ask(request: QuestionRequest) -> dict[str, Any]:
        repository_url = _public_github_url(request.path)
        token = Settings.for_repository(Path.cwd()).github_token
        lock = await _acquire_repository_lock(repository_url)
        try:
            try:
                refreshed, remote_repository = await asyncio.to_thread(
                    remote_repository_is_stale, repository_url, token, public_only=True
                )
                repository = remote_repository.cache_path
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            except GitHubAPIError as error:
                raise HTTPException(
                    status_code=502,
                    detail="GitHub could not check or refresh this repository.",
                ) from error
            except (OSError, RuntimeError) as error:
                raise HTTPException(
                    status_code=500, detail="Could not answer from this repository."
                ) from error
            settings = Settings.for_repository(repository)
            if refreshed:
                await asyncio.to_thread(
                    index_remote_repository, repository_url, remote=remote_repository
                )
            store = VectorStore(settings.database_path)
            if store.count() == 0:
                raise HTTPException(
                    status_code=409,
                    detail="This repository has no index yet. Build the index first.",
                )
            results, elapsed_ms = await asyncio.to_thread(
                retrieve_with_metrics, request.question, store, request.top_k,
                settings.embedding_model, settings.reranker_model,
            )
            answer = await asyncio.to_thread(
                lambda: ensure_supported_citations(
                    answer_question(request.question, results, settings), results
                )
            )
        except HTTPException:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(
                status_code=500, detail="Could not answer from this repository."
            ) from error
        finally:
            lock.release()
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

    @app.get("/api/diagram", tags=["repository"], dependencies=[DIAGRAM_RATE_LIMIT])
    async def diagram(repo: str) -> dict[str, Any]:
        repository_url = _public_github_url(repo)
        lock = await _acquire_repository_lock(repository_url)
        try:
            return await asyncio.to_thread(_diagram_for_repository, repository_url)
        except HTTPException:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(
                status_code=500, detail="Could not generate the repository diagram."
            ) from error
        finally:
            lock.release()

    return app


app = create_app()
