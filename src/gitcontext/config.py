"""Environment-backed application settings."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for indexing, retrieval, and answer generation."""

    database_path: Path
    embedding_model: str | None
    llm_api_key: str | None
    llm_model: str
    llm_base_url: str
    reranker_model: str | None
    github_token: str | None
    github_enabled: bool
    history_limit: int

    @classmethod
    def for_repository(cls, repository: Path) -> "Settings":
        """Load settings, using a repository-local database by default."""
        repo = repository.resolve()
        return cls(
            database_path=Path(os.getenv(
                "GITCONTEXT_DATABASE", str(repo / ".gitcontext" / "index.sqlite")
            )),
            embedding_model=os.getenv("GITCONTEXT_EMBEDDING_MODEL") or None,
            llm_api_key=os.getenv("OPENAI_API_KEY") or None,
            llm_model=os.getenv("GITCONTEXT_LLM_MODEL", "gpt-4o-mini"),
            llm_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            reranker_model=os.getenv("GITCONTEXT_RERANKER_MODEL") or None,
            github_token=os.getenv("GITHUB_TOKEN") or None,
            github_enabled=os.getenv("GITCONTEXT_INGEST_GITHUB", "true").lower()
            in {"1", "true", "yes", "on"},
            history_limit=max(0, int(os.getenv("GITCONTEXT_HISTORY_LIMIT", "200"))),
        )
