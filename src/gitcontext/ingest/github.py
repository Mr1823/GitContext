"""Optional GitHub pull request, discussion, and incident ingestion."""

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from git import Repo
from git.exc import InvalidGitRepositoryError

from gitcontext.ingest.errors import GitHubAPIError
from gitcontext.records import ParsedBlock

GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$")
INCIDENT_LABELS = {"incident", "postmortem", "outage", "incident-report"}


def repository_slug(repository_path) -> str | None:
    """Read a GitHub owner/name pair from the repository's origin remote."""
    try:
        repo = Repo(repository_path, search_parent_directories=False)
        remote = repo.remote("origin").url
    except (InvalidGitRepositoryError, ValueError, AttributeError):
        return None
    match = GITHUB_RE.search(remote)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def github_documents(
    repository_path,
    token: str | None,
    pull_limit: int = 10,
    incident_limit: int = 10,
) -> tuple[list[ParsedBlock], list[ParsedBlock]]:
    """Fetch recent PR conversation and labeled incident records via GitHub REST."""
    slug = repository_slug(repository_path)
    if not slug:
        return [], []
    return github_documents_for_slug(slug, token, pull_limit, incident_limit)


def github_documents_for_slug(
    slug: str,
    token: str | None,
    pull_limit: int = 10,
    incident_limit: int = 10,
) -> tuple[list[ParsedBlock], list[ParsedBlock]]:
    """Fetch recent PR and incident context for a known GitHub owner/repository."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitContext-Codebase-Assistant",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://api.github.com/repos/{slug}"
    pull_docs: list[ParsedBlock] = []
    incident_docs: list[ParsedBlock] = []
    pulls = _get_json(f"{base}/pulls?state=all&sort=updated&direction=desc&per_page={pull_limit}", headers)
    for pull in pulls[:pull_limit]:
        number = pull["number"]
        comments = _get_json(f"{base}/issues/{number}/comments?per_page=100", headers)
        reviews = _get_json(f"{base}/pulls/{number}/comments?per_page=100", headers)
        body = _discussion_text(pull, comments, reviews)
        pull_docs.append(ParsedBlock(
            path=f"github/pull/{number}", start_line=1,
            end_line=max(1, len(body.splitlines())),
            symbol=f"PR #{number}: {pull.get('title', '')}", content=body,
            source_type="pull_request", author=(pull.get("user") or {}).get("login"),
            last_modified=pull.get("updated_at"), pr_number=number,
            source_url=pull.get("html_url"),
        ))

    issues = _get_json(f"{base}/issues?state=all&sort=updated&direction=desc&per_page=100", headers)
    for issue in issues:
        if "pull_request" in issue:
            continue
        labels = {label.get("name", "").lower() for label in issue.get("labels", [])}
        if not labels.intersection(INCIDENT_LABELS):
            continue
        comments = _get_json(f"{base}/issues/{issue['number']}/comments?per_page=100", headers)
        body = _discussion_text(issue, comments, [])
        number = issue["number"]
        incident_docs.append(ParsedBlock(
            path=f"github/incident/{number}", start_line=1,
            end_line=max(1, len(body.splitlines())),
            symbol=f"Incident #{number}: {issue.get('title', '')}", content=body,
            source_type="incident", author=(issue.get("user") or {}).get("login"),
            last_modified=issue.get("updated_at"), pr_number=None,
            source_url=issue.get("html_url"),
        ))
        if len(incident_docs) >= incident_limit:
            break
    return pull_docs, incident_docs


def github_commit_documents(
    slug: str,
    token: str | None,
    limit: int = 30,
) -> list[ParsedBlock]:
    """Fetch a bounded list of recent GitHub commit messages as searchable documents."""
    if limit <= 0:
        return []
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitContext-Codebase-Assistant",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    commits = _get_json(
        f"https://api.github.com/repos/{slug}/commits?per_page={min(limit, 100)}", headers
    )
    documents = []
    for item in commits[:limit]:
        commit = item.get("commit") or {}
        author_data = commit.get("author") or {}
        author = (item.get("author") or {}).get("login") or author_data.get("name") or "unknown"
        authored = author_data.get("date", "")
        message = commit.get("message", "").strip()
        sha = item.get("sha", "")
        content = (
            f"Commit {sha}\nAuthor: {author}\nDate: {authored}\n"
            f"Message:\n{message}\nURL: {item.get('html_url', '')}"
        )
        if not sha:
            continue
        documents.append(ParsedBlock(
            path=f"history/commit/{sha[:12]}",
            start_line=1,
            end_line=max(1, len(content.splitlines())),
            symbol=message.splitlines()[0][:160] if message else f"Commit {sha[:12]}",
            content=content,
            source_type="commit",
            commit_sha=sha,
            author=author,
            last_modified=authored or None,
            source_url=item.get("html_url"),
        ))
    return documents


def _get_json(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            return []
        raise GitHubAPIError(f"GitHub returned HTTP {error.code}.", error.code) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise GitHubAPIError("Could not fetch GitHub repository discussions.") from error
    return result if isinstance(result, list) else []


def _discussion_text(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> str:
    user = (issue.get("user") or {}).get("login", "unknown")
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    sections = [
        f"Title: {issue.get('title', '')}",
        f"Author: {user}",
        f"Created: {issue.get('created_at', '')}",
        f"Updated: {issue.get('updated_at', '')}",
        f"Labels: {', '.join(labels)}",
        f"URL: {issue.get('html_url', '')}",
        "Description:",
        (issue.get("body") or "").strip(),
    ]
    for comment in comments[:30]:
        author = (comment.get("user") or {}).get("login", "unknown")
        sections.append(f"Discussion by {author}:\n{(comment.get('body') or '').strip()}")
    for review in reviews[:30]:
        author = (review.get("user") or {}).get("login", "unknown")
        sections.append(f"Review by {author}:\n{(review.get('body') or '').strip()}")
    return "\n".join(section for section in sections if section)
