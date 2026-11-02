"""Fetch GitHub repository snapshots without requiring a user-side clone."""

import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from gitcontext.ingest.errors import GitHubAPIError
from gitcontext.ingest.repository import IGNORED_DIRECTORIES, SUPPORTED_EXTENSIONS

GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_SOURCE_FILE_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 250 * 1024 * 1024
MAX_SNAPSHOT_FILES = 25_000
MAX_REPO_MB_ENV = "GITCONTEXT_MAX_REPO_MB"
MAX_FILES_ENV = "GITCONTEXT_MAX_FILES"


def _positive_limit(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer.") from error
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _check_repository_size(size_kb: Any) -> None:
    max_mb = _positive_limit(MAX_REPO_MB_ENV, 100)
    if not isinstance(size_kb, int) or size_kb < 0:
        raise RuntimeError("GitHub did not return a valid repository size.")
    if size_kb > max_mb * 1024:
        raise ValueError(
            f"GitHub repository is {size_kb / 1024:.1f} MB; "
            f"the configured limit is {max_mb} MB ({MAX_REPO_MB_ENV})."
        )


def _check_repository_blob_count(repository: "RemoteRepository", token: str | None) -> int:
    max_files = _positive_limit(MAX_FILES_ENV, 1500)
    tree = _github_json(
        f"https://api.github.com/repos/{repository.slug}/git/trees/"
        f"{quote(repository.head_sha, safe='')}?recursive=1",
        token,
    )
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise TypeError("GitHub returned an unexpected repository tree.")
    if any(not isinstance(item, dict) for item in tree["tree"]):
        raise TypeError("GitHub returned an unexpected repository tree entry.")
    blob_count = sum(item.get("type") == "blob" for item in tree["tree"])
    if blob_count > max_files:
        raise ValueError(
            f"GitHub repository contains {blob_count} files; "
            f"the configured limit is {max_files} ({MAX_FILES_ENV})."
        )
    if tree.get("truncated"):
        raise ValueError(
            f"GitHub could not verify the complete file count within the "
            f"configured limit ({MAX_FILES_ENV}={max_files})."
        )
    return blob_count


@dataclass(frozen=True)
class RemoteRepository:
    slug: str
    url: str
    branch: str
    head_sha: str
    cache_path: Path


def parse_github_url(value: str) -> str | None:
    """Return owner/repository for a GitHub repository URL or SSH remote."""
    value = value.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        host = "github.com"
    else:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http", "ssh"}:
            return None
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
    if host not in {"github.com", "www.github.com"}:
        return None
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2:
        return None
    slug = "/".join(parts)
    if any(part in {".", ".."} for part in parts):
        return None
    return slug if GITHUB_REPOSITORY.fullmatch(slug) else None


def is_github_url(value: str) -> bool:
    return parse_github_url(value) is not None


def _cache_root() -> Path:
    configured = os.getenv("GITCONTEXT_REMOTE_CACHE")
    if configured:
        return Path(configured).expanduser().resolve()
    data_dir = os.getenv("GITCONTEXT_DATA_DIR")
    if data_dir:
        return (Path(data_dir).expanduser() / "remotes").resolve()
    base = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    return (base / "gitcontext" / "remotes").resolve()


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitContext-Codebase-Assistant",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_json(url: str, token: str | None) -> dict | list:
    request = Request(url, headers=_headers(token))
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            raise GitHubAPIError(
                "GitHub repository was not found or is private. Check the URL and "
                "set GITHUB_TOKEN for private repositories.", error.code
            ) from error
        raise GitHubAPIError(f"GitHub returned HTTP {error.code}.", error.code) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise GitHubAPIError("Could not reach GitHub.") from error
    if isinstance(result, (dict, list)):
        return result
    raise GitHubAPIError("GitHub returned an unexpected response.")


def get_remote_repository(
    value: str,
    token: str | None = None,
    *,
    public_only: bool = False,
) -> RemoteRepository:
    """Resolve the default branch and latest commit for a GitHub repository."""
    slug = parse_github_url(value)
    if not slug:
        raise ValueError("Enter a GitHub repository URL such as https://github.com/owner/repo.")
    try:
        metadata = _github_json(f"https://api.github.com/repos/{slug}", token)
    except GitHubAPIError as error:
        if public_only and error.status_code == 404:
            raise ValueError(
                "This interface accepts public GitHub repositories only. "
                "Check the URL and repository visibility."
            ) from error
        raise
    if not isinstance(metadata, dict):
        raise TypeError("GitHub did not return repository details.")
    if public_only and metadata.get("private") is not False:
        raise ValueError("This interface accepts public GitHub repositories only.")
    _check_repository_size(metadata.get("size"))
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("GitHub repository has no default branch.")
    commits = _github_json(
        f"https://api.github.com/repos/{slug}/commits?sha={quote(branch, safe='')}&per_page=1",
        token,
    )
    if not isinstance(commits, list) or not commits or not commits[0].get("sha"):
        raise RuntimeError("Could not find the latest commit for this GitHub repository.")
    owner, name = slug.split("/", maxsplit=1)
    return RemoteRepository(
        slug=slug,
        url=f"https://github.com/{owner}/{name}",
        branch=branch,
        head_sha=commits[0]["sha"],
        cache_path=_cache_root() / owner / name,
    )


def ensure_remote_snapshot(
    repository: RemoteRepository,
    token: str | None = None,
    *,
    force: bool = False,
) -> Path:
    """Download only supported text/source files into GitContext's managed cache."""
    root = repository.cache_path
    marker = root / ".gitcontext" / "remote.json"
    if not force:
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = None
        if isinstance(saved, dict) and saved.get("head_sha") == repository.head_sha:
            from gitcontext.ingest.repository import scan_repository

            if scan_repository(root):
                saved_blob_count = saved.get("blob_count")
                if isinstance(saved_blob_count, int):
                    max_files = _positive_limit(MAX_FILES_ENV, 1500)
                    if saved_blob_count > max_files:
                        raise ValueError(
                            f"GitHub repository contains {saved_blob_count} files; "
                            f"the configured limit is {max_files} ({MAX_FILES_ENV})."
                        )
                    return root
                saved["blob_count"] = _check_repository_blob_count(repository, token)
                marker.write_text(json.dumps(saved), encoding="utf-8")
                return root

    blob_count = _check_repository_blob_count(repository, token)

    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.name == ".gitcontext":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    archive_url = (
        f"https://api.github.com/repos/{repository.slug}/tarball/"
        f"{quote(repository.head_sha, safe='')}"
    )
    request = Request(archive_url, headers=_headers(token))
    total_bytes = 0
    files_written = 0
    try:
        with urlopen(request, timeout=90) as response, tarfile.open(
            fileobj=response, mode="r|gz"
        ) as archive:
            for member in archive:
                relative = _archive_relative_path(member.name)
                if (
                    relative is None
                    or not member.isfile()
                    or relative.suffix.lower() not in SUPPORTED_EXTENSIONS
                    or any(part in IGNORED_DIRECTORIES for part in relative.parts)
                ):
                    continue
                if member.size > MAX_SOURCE_FILE_BYTES:
                    continue
                total_bytes += member.size
                if total_bytes > MAX_SNAPSHOT_BYTES:
                    raise RuntimeError(
                        "Repository source snapshot exceeds the 250 MB download limit."
                    )
                if files_written >= MAX_SNAPSHOT_FILES:
                    raise RuntimeError(
                        "Repository contains more than 25,000 supported files."
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    content = stream.read(MAX_SOURCE_FILE_BYTES + 1)
                if len(content) > MAX_SOURCE_FILE_BYTES:
                    continue
                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                files_written += 1
    except HTTPError as error:
        if error.code == 404:
            raise GitHubAPIError(
                "GitHub could not provide this repository snapshot. For a private "
                "repository, check that GITHUB_TOKEN has repository read access.",
                error.code,
            ) from error
        raise GitHubAPIError(f"GitHub returned HTTP {error.code}.", error.code) from error
    except (URLError, TimeoutError, tarfile.TarError, OSError) as error:
        raise GitHubAPIError("Could not download the GitHub repository.") from error

    if not files_written:
        raise RuntimeError("No supported source or documentation files were found in the repository.")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "slug": repository.slug,
            "url": repository.url,
            "branch": repository.branch,
            "head_sha": repository.head_sha,
            "blob_count": blob_count,
        }),
        encoding="utf-8",
    )
    return root


def _archive_relative_path(name: str) -> PurePosixPath | None:
    """Strip the archive's top-level directory and reject unsafe member paths."""
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or ".." in path.parts
        or any("\\" in part or ":" in part for part in path.parts)
    ):
        return None
    relative = PurePosixPath(*path.parts[1:])
    if not relative.parts or ".gitcontext" in relative.parts:
        return None
    return relative
