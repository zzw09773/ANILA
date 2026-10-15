"""Source boundaries without evaluating or executing uploaded code."""

from __future__ import annotations

import ast
import re
from bisect import bisect_right
from typing import Any


def source_units(text: str, language: str) -> list[dict[str, Any]]:
    """Partition every line into functions/classes and remaining source."""
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    if language == "python":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            # An incomplete source remains one intact, correctly numbered unit.
            tree = None
        if tree is not None:

            def visit(nodes: list[ast.stmt], scope: str = "") -> None:
                for node in nodes:
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, scope + node.name + ".")
                        continue
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                    spans.append((start, node.end_lineno or node.lineno, scope + node.name))

            visit(tree.body)
    else:
        # Mask comments/literals with same-length whitespace; braces inside them
        # cannot terminate a function. Raw C++ strings retain their delimiter.
        pattern = re.compile(
            r'R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\(.*?\)(?P=delimiter)"'
            r'|//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
            re.DOTALL,
        )
        masked = pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
        # Constructor initializer lists can contain braced subobjects before
        # the function body. Keep the source whole when that boundary is ambiguous.
        if re.search(r"\)\s*(?:noexcept(?:\([^)]*\))?\s*)?:", masked):
            return [
                {
                    "line_start": 1,
                    "line_end": len(lines),
                    "symbol": "module",
                    "source_boundary_quality": "conservative",
                }
            ]
        line_starts = [0, *[match.end() for match in re.finditer("\n", text)]]
        stack: list[tuple[int, str] | None] = []
        boundary = 0
        for i, char in enumerate(masked):
            if char == "{":
                header = masked[boundary:i]
                candidates = re.findall(r"([A-Za-z_~][\w:~]*)\s*\(", header)
                symbol = next(
                    (
                        name
                        for name in candidates
                        if name
                        not in {
                            "if",
                            "for",
                            "while",
                            "switch",
                            "catch",
                            "sizeof",
                            "alignof",
                            "decltype",
                            "noexcept",
                            "requires",
                            "static_assert",
                        }
                    ),
                    None,
                )
                if (
                    symbol
                    and not any(frame is not None for frame in stack)
                    and not re.match(
                        r"\s*(?:namespace\b|(?:class|struct|enum|union)\s+\w+\s*(?::|$))",
                        header,
                    )
                    and "=" not in header.split("(", 1)[0]
                ):
                    start = boundary + len(header) - len(header.lstrip())
                    stack.append((bisect_right(line_starts, start), symbol))
                else:
                    stack.append(None)
                boundary = i + 1
            elif char == "}" and stack:
                frame = stack.pop()
                if frame is not None:
                    spans.append((frame[0], bisect_right(line_starts, i), frame[1]))
                boundary = i + 1
            elif char == ";":
                boundary = i + 1
        # Incomplete functions remain intact through the last source line.
        for frame in stack:
            if frame is not None:
                spans.append((frame[0], len(lines), frame[1]))
        spans.sort()
    units: list[dict[str, Any]] = []
    cursor = 1
    for start, end, symbol in spans:
        # Two declarations on one line must share one unit to avoid data loss.
        start = max(start, cursor)
        if end < start:
            continue
        if start > cursor:
            units.append({"line_start": cursor, "line_end": start - 1, "symbol": "module"})
        units.append({"line_start": start, "line_end": end, "symbol": symbol})
        cursor = end + 1
    if cursor <= len(lines):
        units.append({"line_start": cursor, "line_end": len(lines), "symbol": "module"})
    merged: list[dict[str, Any]] = []
    leading = None
    for unit in units:
        body = "".join(lines[unit["line_start"] - 1 : unit["line_end"]])
        if not body.strip():
            if merged:
                merged[-1]["line_end"] = unit["line_end"]
            elif leading is None:
                leading = unit["line_start"]
            continue
        if leading is not None:
            unit["line_start"] = leading
            leading = None
        merged.append(unit)
    return merged
