"""Source boundaries using AST/lexical syntax only; uploaded code stays data."""

from __future__ import annotations

import ast
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

MAX_SOURCE_SCAN_BYTES = 1024 * 1024
MAX_SOURCE_TOKENS = 100_000
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


@dataclass(frozen=True)
class _Token:
    value: str
    offset: int


_TOKEN = re.compile(r"[A-Za-z_]\w*|::|->|&&|\|\||[^\s]")
# Preprocessing-number boundaries keep C++ digit separators inside the number.
# Numeric validity/types still belong to a compiler; only token boundaries matter.
_NUMBER = re.compile(r"(?:[0-9]|\.[0-9])(?:[eEpP][+-]|[\w.]|'[A-Za-z0-9_])*")
_NOT_FUNCTION = {
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
    "alignas",
    "return",
    "void",
    "int",
    "char",
    "long",
    "short",
    "float",
    "double",
    "bool",
    "__attribute__",
    "__declspec",
}


def _conservative(lines: int, reason: str) -> list[dict[str, Any]]:
    return [
        {
            "line_start": 1,
            "line_end": lines,
            "symbol": "module",
            "source_boundary_quality": "conservative",
            "source_boundary_reason": reason,
            **(
                {
                    "source_scan_byte_limit": MAX_SOURCE_SCAN_BYTES,
                    "source_scan_token_limit": MAX_SOURCE_TOKENS,
                }
                if reason == "source_scan_limit"
                else {}
            ),
        }
    ]


def summarize_boundary_quality(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Parser-facing summary; lexical boundaries are not a compiler guarantee."""
    reasons = sorted(
        {unit["source_boundary_reason"] for unit in units if unit.get("source_boundary_reason")}
    )
    if any(unit.get("source_boundary_quality") == "conservative" for unit in units):
        return {
            "source_boundary_quality": "conservative",
            "source_boundary_reasons": reasons,
            "quality_notice": "部分程式語法無法可靠靜態分界，已完整保留來源；請依原檔行號核對函式範圍。",
        }
    quality = (
        "ast"
        if units and all(unit.get("source_boundary_quality") == "ast" for unit in units)
        else "lexical"
    )
    return {
        "source_boundary_quality": quality,
        "source_boundary_reasons": reasons,
        **(
            {
                "quality_notice": "C/C++ 函式範圍採靜態詞法辨識，未編譯或展開巨集；請依原文行號核對，並非完整語意解析。"
            }
            if quality == "lexical"
            else {}
        ),
    }


def _lex_cpp(text: str) -> tuple[list[_Token], dict[int, int], str | None]:
    """One forward scan, excluding literals/comments without evaluating macros."""
    tokens: list[_Token] = []
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    cursor = 0
    while cursor < len(text):
        if len(tokens) >= MAX_SOURCE_TOKENS:
            return tokens, pairs, "source_scan_limit"
        if text[cursor].isspace():
            cursor += 1
            continue
        if text[cursor] == "#" and not text[text.rfind("\n", 0, cursor) + 1 : cursor].strip():
            end = text.find("\n", cursor)
            end = len(text) if end < 0 else end
            directive = text[cursor + 1 : end].strip().split(maxsplit=1)
            if directive and directive[0] not in {"include", "pragma", "line"}:
                return tokens, pairs, "preprocessor_branch_or_macro"
            if text[cursor:end].rstrip().endswith("\\"):
                return tokens, pairs, "continued_preprocessor_directive"
            cursor = end
            continue
        if text.startswith("//", cursor):
            end = text.find("\n", cursor)
            if end >= 0 and text[cursor:end].rstrip("\r").endswith("\\"):
                return tokens, pairs, "continued_line_comment"
            cursor = len(text) if end < 0 else end
            continue
        if text.startswith("/*", cursor):
            end = text.find("*/", cursor + 2)
            if end < 0:
                return tokens, pairs, "unterminated_comment"
            cursor = end + 2
            continue
        raw = re.match(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(', text[cursor : cursor + 24])
        if raw:
            end = text.find(")" + raw.group(1) + '"', cursor + raw.end())
            if end < 0:
                return tokens, pairs, "unterminated_literal"
            cursor = end + len(raw.group(1)) + 2
            continue
        if text[cursor] in "\"'":
            if tokens and tokens[-1].value == "operator":
                return tokens, pairs, "literal_operator_declarator"
            quote = text[cursor]
            cursor += 1
            while cursor < len(text) and text[cursor] != quote:
                cursor += 2 if text[cursor] == "\\" else 1
            if cursor >= len(text):
                return tokens, pairs, "unterminated_literal"
            cursor += 1
            continue
        match = _NUMBER.match(text, cursor) or _TOKEN.match(text, cursor)
        assert match is not None
        value = match.group()
        index = len(tokens)
        tokens.append(_Token(value, cursor))
        if value in {"(", "[", "{"}:
            stack.append((value, index))
        elif value in {")", "]", "}"}:
            if not stack or stack[-1][0] != {")": "(", "]": "[", "}": "{"}[value]:
                return tokens, pairs, "unbalanced_delimiters"
            _, opened = stack.pop()
            pairs[opened] = index
            pairs[index] = opened
        cursor = match.end()
    return tokens, pairs, "unbalanced_delimiters" if stack else None


def _scope_prefix(values: list[str], index: int) -> str:
    first = index
    while (
        first >= 2
        and values[first - 1] == "::"
        and re.fullmatch(r"[A-Za-z_]\w*", values[first - 2])
    ):
        first -= 2
    return "".join(values[first:index])


def _callable(
    tokens: list[_Token], pairs: dict[int, int], first: int, end: int
) -> tuple[str, int] | None:
    assignment = first
    while assignment < end:
        if (
            tokens[assignment].value == "template"
            and assignment + 1 < end
            and tokens[assignment + 1].value == "<"
        ):
            assignment += 2
            angle_depth = 1
            while assignment < end and angle_depth:
                value = tokens[assignment].value
                if value in {"(", "[", "{"}:
                    assignment = pairs[assignment] + 1
                    continue
                angle_depth += (value == "<") - (value == ">")
                assignment += 1
            continue
        if tokens[assignment].value in {"(", "[", "{"}:
            assignment = pairs[assignment] + 1
            continue
        if (
            tokens[assignment].value == "="
            and assignment > first
            and assignment + 1 < end
            and tokens[assignment + 1].value == "["
            and re.fullmatch(r"[A-Za-z_]\w*", tokens[assignment - 1].value)
        ):
            return "lambda:" + tokens[assignment - 1].value, assignment
        assignment += 1
    index = first
    while index < end:
        value = tokens[index].value
        if value in {"[", "{"}:
            index = pairs[index] + 1
            continue
        if value != "(":
            index += 1
            continue
        close = pairs[index]
        prefix = [token.value for token in tokens[first:index]]
        if "operator" in prefix:
            op = len(prefix) - 1 - prefix[::-1].index("operator")
            tail = prefix[op + 1 :]
            if (
                not tail
                and close == index + 1
                and close + 1 < end
                and tokens[close + 1].value == "("
            ):
                index = close + 1
                continue
            operator = "".join(tail)
            if tail and re.match(r"[A-Za-z_]", tail[0]):
                operator = " " + operator
            return _scope_prefix(prefix, op) + "operator" + operator, close
        name = prefix[-1] if prefix else ""
        if re.fullmatch(r"[A-Za-z_]\w*", name) and name not in _NOT_FUNCTION:
            if len(prefix) >= 2 and prefix[-2] == "~":
                return _scope_prefix(prefix, len(prefix) - 2) + "~" + name, close
            return _scope_prefix(prefix, len(prefix) - 1) + name, close
        index = close + 1
    return None


def _cpp_spans(text: str) -> tuple[list[tuple[int, int, str]], str | None]:
    tokens, pairs, reason = _lex_cpp(text)
    if reason:
        return [], reason
    line_starts = [0, *[match.end() for match in re.finditer("\n", text)]]
    spans: list[tuple[int, int, str]] = []

    def scope(first: int, end: int, depth: int = 0) -> str | None:
        if depth > 64:
            return "scope_depth_limit"
        boundary = index = first
        while index < end:
            value = tokens[index].value
            if value == "template" and index + 1 < end and tokens[index + 1].value == "<":
                index += 2
                angle_depth = 1
                while index < end and angle_depth:
                    item = tokens[index].value
                    if item in {"(", "[", "{"}:
                        index = pairs[index] + 1
                        continue
                    angle_depth += (item == "<") - (item == ">")
                    index += 1
                if angle_depth:
                    return "template_parameters_unresolved"
                continue
            if value in {"(", "["}:
                index = pairs[index] + 1
                continue
            if value == ";":
                declaration = _callable(tokens, pairs, boundary, index)
                if (
                    declaration
                    and declaration[1] + 1 < index
                    and tokens[declaration[1] + 1].value
                    in {"int", "char", "float", "double", "long", "short", "unsigned", "signed"}
                ):
                    return "knr_parameter_declarations"
                boundary = index + 1
            elif (
                value == ":"
                and index == boundary + 1
                and tokens[boundary].value in {"public", "private", "protected"}
            ):
                boundary = index + 1
            elif value == "{":
                if index - boundary > 4096:
                    return "declarator_complexity_limit"
                header = [token.value for token in tokens[boundary:index]]
                candidate = _callable(tokens, pairs, boundary, index)
                if candidate:
                    symbol, parameter_end = candidate
                    suffix = [token.value for token in tokens[parameter_end + 1 : index]]
                    if symbol.split("::")[-1].isupper():
                        return "macro_function_declarator"
                    if "try" in suffix:
                        return "function_try_block"
                    requires_body = bool(suffix and suffix[-1] == "requires")
                    if suffix and suffix[-1] == ")":
                        opened = pairs[index - 1]
                        requires_body = (
                            opened > parameter_end and tokens[opened - 1].value == "requires"
                        )
                    initializer = (
                        ":" in suffix
                        and bool(header)
                        and (
                            re.fullmatch(r"[A-Za-z_]\w*", header[-1]) is not None
                            or header[-1] in {">", "]"}
                        )
                    )
                    if requires_body or initializer:
                        index = pairs[index] + 1
                        continue
                    spans.append(
                        (
                            bisect_right(line_starts, tokens[boundary].offset),
                            bisect_right(line_starts, tokens[pairs[index]].offset),
                            symbol,
                        )
                    )
                elif header == ["extern"] or any(
                    word in {"namespace", "class", "struct", "union"} for word in header
                ):
                    nested_reason = scope(index + 1, pairs[index], depth + 1)
                    if nested_reason:
                        return nested_reason
                elif header and header[-1] == ")":
                    return "unrecognized_callable_declarator"
                index = pairs[index]
                boundary = index + 1
            index += 1
        return None

    reason = scope(0, len(tokens))
    return sorted(spans), reason


def source_units(text: str, language: str) -> list[dict[str, Any]]:
    """Partition every source line exactly once, retaining uncertain syntax."""
    # Bound AST and lexical allocations before constructing line/token arrays.
    # Larger multi-function files may be split into smaller source uploads;
    # this scan policy is independent of the per-structure embedding guard.
    if len(text) > MAX_SOURCE_SCAN_BYTES or len(text.encode("utf-8")) > MAX_SOURCE_SCAN_BYTES:
        line_count = sum(1 for _ in _LINE_BREAK.finditer(text)) + int(
            bool(text) and text[-1] not in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
        )
        return _conservative(line_count, "source_scan_limit")
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    quality = "ast" if language == "python" else "lexical"
    if language == "python":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return _conservative(len(lines), "python_syntax_unavailable")

        def visit(nodes: list[ast.stmt], scope: str = "") -> None:
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, scope + node.name + ".")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                    spans.append((start, node.end_lineno or node.lineno, scope + node.name))

        visit(tree.body)
    else:
        if re.search(r"\r(?!\n)|[\v\f\x1c-\x1e\x85\u2028\u2029]", text):
            return _conservative(len(lines), "unsupported_source_line_break")
        spans, reason = _cpp_spans(text)
        if reason:
            return _conservative(len(lines), reason)
    grouped: list[dict[str, Any]] = []
    for start, end, symbol in spans:
        if grouped and start <= grouped[-1]["line_end"]:
            previous = grouped[-1]
            previous["line_end"] = max(previous["line_end"], end)
            previous.setdefault("symbols", [previous["symbol"]]).append(symbol)
            previous.update(
                source_boundary_quality="conservative", source_boundary_reason="shared_source_line"
            )
        else:
            grouped.append(
                {
                    "line_start": start,
                    "line_end": end,
                    "symbol": symbol,
                    "source_boundary_quality": quality,
                }
            )
    units: list[dict[str, Any]] = []
    cursor = 1
    for unit in grouped:
        if unit["line_start"] > cursor:
            units.append(
                {
                    "line_start": cursor,
                    "line_end": unit["line_start"] - 1,
                    "symbol": "module",
                    "source_boundary_quality": quality,
                }
            )
        units.append(unit)
        cursor = unit["line_end"] + 1
    if cursor <= len(lines):
        units.append(
            {
                "line_start": cursor,
                "line_end": len(lines),
                "symbol": "module",
                "source_boundary_quality": quality,
            }
        )
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
