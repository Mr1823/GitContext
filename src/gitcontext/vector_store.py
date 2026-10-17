"""Small persistent SQLite store for code chunks and optional embeddings."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from gitcontext.ingest.parser import ParsedBlock


class VectorStore:
    """Persist indexed chunks locally; vectors are optional JSON arrays."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding TEXT
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def replace_all(
        self,
        chunks: list[ParsedBlock],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Replace the current index atomically."""
        if embeddings is not None and len(embeddings) != len(chunks):
            raise ValueError("Embedding count must match chunk count")
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks")
            connection.executemany(
                "INSERT INTO chunks(path,start_line,end_line,symbol,content,embedding) "
                "VALUES(?,?,?,?,?,?)",
                [(
                    chunk.path, chunk.start_line, chunk.end_line, chunk.symbol,
                    chunk.content,
                    json.dumps(embeddings[index]) if embeddings is not None else None,
                ) for index, chunk in enumerate(chunks)],
            )

    def all(self) -> list[dict[str, Any]]:
        """Return indexed chunks in stable file and line order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT path,start_line,end_line,symbol,content,embedding FROM chunks "
                "ORDER BY path,start_line"
            ).fetchall()
        return [{
            "path": row["path"], "start_line": row["start_line"],
            "end_line": row["end_line"], "symbol": row["symbol"],
            "content": row["content"],
            "embedding": json.loads(row["embedding"]) if row["embedding"] else None,
        } for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
