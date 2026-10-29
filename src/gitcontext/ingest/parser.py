"""Extract readable, line-addressable blocks from source files."""

import ast
import re
from pathlib import Path

from gitcontext.records import ParsedBlock


def parse_file(
    file_path: Path, repository: Path, source: str | None = None
) -> list[ParsedBlock]:
    """Parse declarations structurally where a Tree-sitter grammar is available."""
    if source is None:
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return []

    relative_path = file_path.relative_to(repository).as_posix()
    lines = source.splitlines()
    source_type = "documentation" if file_path.suffix.lower() in {
        ".md", ".mdx", ".rst", ".txt"
    } else "code"
    if file_path.suffix.lower() in {".md", ".mdx"}:
        headings = [
            (index, re.sub(r"^#+\s*", "", line).strip())
            for index, line in enumerate(lines, start=1)
            if re.match(r"^#{1,6}\s+", line)
        ]
        if headings:
            sections = []
            for offset, (start, title) in enumerate(headings):
                end = headings[offset + 1][0] - 1 if offset + 1 < len(headings) else len(lines)
                content = "\n".join(lines[start - 1:end]).strip()
                if content:
                    sections.append(ParsedBlock(
                        relative_path, start, end, title, content,
                        source_type="documentation",
                    ))
            return sections
        return _line_blocks(relative_path, lines, source_type="documentation")
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
                source_type=source_type,
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
                            source_type=source_type,
                        ))
            return sorted(blocks, key=lambda block: (block.start_line, block.symbol))
    structured = _parse_tree_sitter(relative_path, file_path.suffix.lower(), source, lines)
    return structured or _line_blocks(relative_path, lines, source_type=source_type)


def _parse_tree_sitter(
    path: str, extension: str, source: str, lines: list[str]
) -> list[ParsedBlock]:
    """Extract class, method, and function nodes from JS, TS, and Java files."""
    try:
        from tree_sitter import Language, Parser

        if extension in {".js", ".jsx"}:
            import tree_sitter_javascript as grammar

            language = Language(grammar.language())
        elif extension in {".ts", ".tsx"}:
            import tree_sitter_typescript as grammar

            language = Language(
                grammar.language_tsx() if extension == ".tsx" else grammar.language_typescript()
            )
        elif extension == ".java":
            import tree_sitter_java as grammar

            language = Language(grammar.language())
        else:
            return []
    except ImportError:
        return []

    parser = Parser(language)
    tree = parser.parse(source.encode("utf-8"))
    target_types = {
        "class_declaration", "interface_declaration", "enum_declaration",
        "function_declaration", "function_definition", "method_definition",
        "method_declaration", "constructor_declaration", "abstract_method_declaration",
    }
    blocks = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in target_types:
            start = node.start_point.row + 1
            end = node.end_point.row + (1 if node.end_point.column else 0)
            name_node = node.child_by_field_name("name")
            symbol = (
                name_node.text.decode("utf-8", errors="replace")
                if name_node and name_node.text else node.type
            )
            blocks.append(ParsedBlock(
                path, start, max(start, end), symbol,
                "\n".join(lines[start - 1:max(start, end)]),
            ))
        stack.extend(reversed(node.children))
    if not blocks:
        return []
    declaration_nodes = [
        node for node in tree.root_node.named_children
        if node.type in target_types
        or (node.type == "export_statement" and any(
            child.type in target_types for child in node.named_children
        ))
    ]
    covered_ranges = [
        (node.start_point.row, node.end_point.row)
        for node in declaration_nodes
    ]
    for node in tree.root_node.named_children:
        if any(node.start_point.row >= start and node.end_point.row <= end
               for start, end in covered_ranges):
            continue
        start = node.start_point.row + 1
        end = max(start, node.end_point.row + (1 if node.end_point.column else 0))
        content = "\n".join(lines[start - 1:end]).strip()
        if content:
            blocks.append(ParsedBlock(path, start, end, "module", content))
    return sorted(blocks, key=lambda block: (block.start_line, block.end_line, block.symbol))


def _line_blocks(
    path: str, lines: list[str], size: int = 80, source_type: str = "code"
) -> list[ParsedBlock]:
    """Split non-Python or declaration-free files into overlapping line blocks."""
    blocks = []
    for offset in range(0, len(lines), size - 10):
        content_lines = lines[offset:offset + size]
        if not any(line.strip() for line in content_lines):
            continue
        blocks.append(ParsedBlock(
            path, offset + 1, offset + len(content_lines), "",
            "\n".join(content_lines), source_type=source_type,
        ))
    return blocks
