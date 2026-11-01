"""Offset-preserving lexical masking and delimiter ranges for lightweight parsers."""

from __future__ import annotations

import re

_LITERAL_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|""".*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`',
    re.DOTALL,
)


def blank(text: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in text)


def mask_source(text: str) -> str:
    """Keep every offset and newline while hiding comments and literal content."""
    return _LITERAL_RE.sub(lambda m: blank(m.group()), text)


def delimiter_pairs(masked: str, opening: str, closing: str) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for position, char in enumerate(masked):
        if char == opening:
            stack.append(position)
        elif char == closing and stack:
            pairs[stack.pop()] = position
    return pairs
