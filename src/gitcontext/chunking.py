"""Turn parsed source blocks into bounded retrieval chunks."""

from dataclasses import replace
from pathlib import Path

from gitcontext.ingest.parser import ParsedBlock, parse_file


def chunk_file(
    file_path: Path,
    repository: Path,
    max_chars: int = 2400,
    overlap_lines: int = 8,
) -> list[ParsedBlock]:
    """Parse a source file and split unusually large declarations safely."""
    return [
        chunk
        for block in parse_file(file_path, repository)
        for chunk in chunk_block(block, max_chars, overlap_lines)
    ]


def chunk_block(
    block: ParsedBlock,
    max_chars: int = 2400,
    overlap_lines: int = 8,
) -> list[ParsedBlock]:
    """Split any parsed source or discussion block into bounded passages."""
    if len(block.content) <= max_chars:
        return [block]
    lines = block.content.splitlines()
    chunks = []
    start = 0
    while start < len(lines):
        end = start
        length = 0
        while end < len(lines) and (length + len(lines[end]) + 1 <= max_chars or end == start):
            length += len(lines[end]) + 1
            end += 1
        chunks.append(replace(
            block,
            start_line=block.start_line + start,
            end_line=block.start_line + end - 1,
            content="\n".join(lines[start:end]),
        ))
        if end == len(lines):
            break
        start = max(start + 1, end - overlap_lines)
    return chunks
