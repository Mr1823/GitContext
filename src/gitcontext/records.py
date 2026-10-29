"""Dependency-neutral records shared by parsing, indexing, and storage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedBlock:
    """A line-addressable source or repository-history passage."""

    path: str
    start_line: int
    end_line: int
    symbol: str
    content: str
    source_type: str = "code"
    commit_sha: str | None = None
    author: str | None = None
    last_modified: str | None = None
    pr_number: int | None = None
    source_url: str | None = None
