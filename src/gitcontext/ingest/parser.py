"""Extract readable, line-addressable blocks from source files."""

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedBlock:
    path: str
    start_line: int
    end_line: int
    symbol: str
    content: str


def parse_file(file_path: Path, repository: Path) -> list[ParsedBlock]:
    """Parse Python declarations; return line blocks for other supported files."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []

    relative_path = file_path.relative_to(repository).as_posix()
    lines = source.splitlines()
    if file_path.suffix.lower() == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return _line_blocks(relative_path, lines)
        declarations = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        declarations.sort(key=lambda node: (node.lineno, -getattr(node, "end_lineno", node.lineno)))
        blocks = []
        for node in declarations:
            start_line = min(
                [node.lineno] + [decorator.lineno for decorator in node.decorator_list]
            )
            end_line = getattr(node, "end_lineno", node.lineno)
            blocks.append(ParsedBlock(
                relative_path, start_line, end_line, node.name,
                "\n".join(lines[start_line - 1:end_line]),
            ))
        if blocks:
            top_level = [
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            covered = set()
            for node in top_level:
                start_line = min(
                    [node.lineno] + [decorator.lineno for decorator in node.decorator_list]
                )
                covered.update(range(start_line, getattr(node, "end_lineno", node.lineno) + 1))
            uncovered = [index for index in range(1, len(lines) + 1) if index not in covered]
            if uncovered:
                groups: list[list[int]] = []
                for line_number in uncovered:
                    if not groups or line_number > groups[-1][-1] + 1:
                        groups.append([])
                    groups[-1].append(line_number)
                for group in groups:
                    content_lines = lines[group[0] - 1:group[-1]]
                    while content_lines and not content_lines[0].strip():
                        content_lines.pop(0)
                        group.pop(0)
                    while content_lines and not content_lines[-1].strip():
                        content_lines.pop()
                        group.pop()
                    if content_lines:
                        blocks.append(ParsedBlock(
                            relative_path, group[0], group[-1], "module",
                            "\n".join(content_lines),
                        ))
            return sorted(blocks, key=lambda block: (block.start_line, block.symbol))
    return _line_blocks(relative_path, lines)


def _line_blocks(path: str, lines: list[str], size: int = 80) -> list[ParsedBlock]:
    """Split non-Python or declaration-free files into overlapping line blocks."""
    blocks = []
    for offset in range(0, len(lines), size - 10):
        content_lines = lines[offset:offset + size]
        if not any(line.strip() for line in content_lines):
            continue
        blocks.append(ParsedBlock(
            path, offset + 1, offset + len(content_lines), "", "\n".join(content_lines)
        ))
    return blocks
