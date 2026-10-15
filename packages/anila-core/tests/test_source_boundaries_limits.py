"""Static C/C++ boundaries and bounded intact chunks; source is never executed."""

import pytest

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.source_code import source_units


COMPLEX_FUNCTIONS = [
    ("Widget::Widget(int value) : first{value}, second(2)\n{\n  ready();\n}\n", "Widget::Widget"),
    ("int Widget::operator()(int value) const\n{\n  return value;\n}\n", "Widget::operator()"),
    ("int Widget::operator[](int index) const\n{\n  return index;\n}\n", "Widget::operator[]"),
    ("Widget::operator bool() const\n{\n  return true;\n}\n", "Widget::operator bool"),
    (
        "template <typename T>\nauto lookup(T value) noexcept(noexcept(value + value)) -> decltype(value + value)\nrequires requires(T item) { item + item; }\n{\n  return value;\n}\n",
        "lookup",
    ),
    (
        "auto transform = []<typename T>(T value) mutable noexcept -> T\n{\n  return value;\n};\n",
        "lambda:transform",
    ),
    (
        "std::ostream& operator<<(std::ostream& out, const Widget& value)\n{\n  return out;\n}\n",
        "operator<<",
    ),
    (
        "Widget& Widget::operator=(const Widget& other)\n{\n  return *this;\n}\n",
        "Widget::operator=",
    ),
    ("Widget::~Widget() noexcept\n{\n  release();\n}\n", "Widget::~Widget"),
    (
        "template <typename T>\nauto read(T value) -> int\nrequires requires(T item) { { item.size() } -> Same<int>; }\n{\n  return value.size();\n}\n",
        "read",
    ),
    (
        "template<auto Callback = []() { return 1; }>\nint execute()\n{\n  return Callback();\n}\n",
        "execute",
    ),
]


@pytest.mark.parametrize("body,symbol", COMPLEX_FUNCTIONS)
def test_complex_cpp_function_keeps_its_body_and_following_function(body, symbol):
    text = body + "int after() { return 7; }\n"
    units = source_units(text, "cpp")
    selected = [unit for unit in units if unit["symbol"] == symbol]
    assert len(selected) == 1
    unit = selected[0]
    content = "".join(text.splitlines(keepends=True)[unit["line_start"] - 1 : unit["line_end"]])
    assert body.rstrip(";\n") in content
    assert any(item["symbol"] == "after" for item in units)
    assert unit["source_boundary_quality"] == "lexical"


@pytest.mark.parametrize(
    "strategy", ["fixed", "hierarchical", "markdown-aware", "pdf-page", "cjk-sentence", "semantic"]
)
@pytest.mark.parametrize("kind", ["source", "table", "fence"])
def test_intact_structure_over_budget_is_marked_and_never_truncated(strategy, kind):
    if kind == "source":
        text = "int long_function() {\n" + "  call();\n" * 30 + "}\n"
        metadata = {"source_units": source_units(text, "cpp"), "source_language": "cpp"}
    elif kind == "table":
        text = "| A | B |\n|---|---|\n" + "| cell | content |\n" * 30
        metadata = {}
    else:
        text = "```cpp\n" + "call();\n" * 30 + "```\n"
        metadata = {}
    metadata["quality_notice"] = "Existing parser limitation."
    chunks = get_chunker(strategy).chunk(
        text,
        metadata,
        {
            "size": 8,
            "overlap": 0,
            "max_leaf_tokens": 8,
            "max_page_tokens": 8,
            "max_tokens": 8,
            "target_tokens": 8,
            "min_segment_tokens": 8,
        },
    )
    assert "".join(chunk.content for chunk in chunks) == text
    assert all(chunk.metadata["oversize"] is True for chunk in chunks)
    assert all(chunk.metadata["atomic_byte_limit"] == 16 * 1024 for chunk in chunks)
    assert all("quality_notice" in chunk.metadata for chunk in chunks)
    assert all(chunk.metadata["token_count_is_estimate"] is True for chunk in chunks)
    assert all(
        "Existing parser limitation." in chunk.metadata["quality_notice"]
        and "16 KiB" in chunk.metadata["quality_notice"]
        for chunk in chunks
    )


@pytest.mark.parametrize(
    "text,reason",
    [
        ("#if FEATURE\nint first() { return 1; }\n#endif\n", "preprocessor_branch_or_macro"),
        ("DEFINE_HANDLER(task) { process(); }\n", "macro_function_declarator"),
        (
            "Widget::Widget() try : value(1) { ready(); } catch (...) { recover(); }\n",
            "function_try_block",
        ),
        ("int (*factory())(int) { return nullptr; }\n", "unrecognized_callable_declarator"),
        ("int broken() {\n  return 1;\n", "unbalanced_delimiters"),
        ('int value() { const char *v = R"tag(unfinished\n', "unterminated_literal"),
        ('int operator""_suffix(const char *s) { return 1; }\n', "literal_operator_declarator"),
        ("int old_style(a, b)\nint a;\nint b;\n{ return a+b; }\n", "knr_parameter_declarations"),
    ],
)
def test_ambiguous_source_is_intact_and_carries_specific_quality_reason(text, reason):
    from anila_core.ingestion.source_code import summarize_boundary_quality

    units = source_units(text, "cpp")
    assert len(units) == 1
    assert units[0]["line_start"] == 1
    assert units[0]["line_end"] == len(text.splitlines())
    assert units[0]["source_boundary_quality"] == "conservative"
    assert units[0]["source_boundary_reason"] == reason
    summary = summarize_boundary_quality(units)
    assert summary["source_boundary_reasons"] == [reason]
    assert "quality_notice" in summary
    chunk = get_chunker("fixed").chunk(
        text, {"source_units": units, **summary}, {"size": 8, "overlap": 0}
    )[0]
    assert chunk.content == text
    assert chunk.metadata["source_boundary_reason"] == reason
    assert chunk.metadata["source_boundary_quality"] == "conservative"
    if chunk.metadata["oversize"]:
        assert "16 KiB" in chunk.metadata["quality_notice"]


@pytest.mark.parametrize(
    "language,text",
    [
        (
            "c",
            "#include <stdio.h>\nint first() { return 1; } int second() { return 2; }\nint third() { return 3; }\n",
        ),
        (
            "cpp",
            "namespace report {\nstruct Widget {\nWidget() : value{1}, other{2} { ready(); } int lookup() { return 4; }\n};\n}\n",
        ),
        (
            "cpp",
            'int outer() {\n auto inner = [](int v) { return v + 1; };\n const char *brace = R"tag( } { fake() {} )tag";\n return inner(1);\n}\nint after() { return 2; }\n',
        ),
        (
            "cpp",
            "template<typename T = int>\n[[nodiscard]] auto identity(T value = T{}) -> T { return value; }\nint after() { return 2; }\n",
        ),
    ],
)
def test_each_original_line_is_emitted_once_including_shared_declarations(language, text):
    units = source_units(text, language)
    lines = text.splitlines(keepends=True)
    indices = [line for unit in units for line in range(unit["line_start"], unit["line_end"] + 1)]
    assert indices == list(range(1, len(lines) + 1))
    assert (
        "".join("".join(lines[unit["line_start"] - 1 : unit["line_end"]]) for unit in units) == text
    )
    if "first()" in text:
        assert units[1]["symbols"] == ["first", "second"]
        assert units[1]["source_boundary_reason"] == "shared_source_line"
    if "auto inner" in text:
        assert [unit["symbol"] for unit in units] == ["outer", "after"]
    if "identity" in text:
        assert [unit["symbol"] for unit in units] == ["identity", "after"]


def test_default_lambda_parameter_belongs_to_the_named_function():
    text = "int apply(int (*callback)(int) = [](int value) { return value + 1; })\n{\n  return callback(1);\n}\nint after() { return 2; }\n"
    units = source_units(text, "cpp")
    assert [unit["symbol"] for unit in units] == ["apply", "after"]
    assert units[0]["line_start"] == 1 and units[0]["line_end"] == 4


def test_c_linkage_scope_keeps_nested_functions():
    text = 'extern "C" {\nint first() { return 1; }\nint second() { return 2; }\n}\n'
    units = source_units(text, "cpp")
    assert [unit["symbol"] for unit in units if unit["symbol"] != "module"] == ["first", "second"]


@pytest.mark.parametrize(
    "body,quality",
    [
        ("Widget::Widget() : value{1} { ready(); }\n", "lexical"),
        ("#if FEATURE\nint value() { return 1; }\n#endif\n", "conservative"),
    ],
)
def test_real_parser_summary_reaches_parsed_document_and_chunk(tmp_path, body, quality):
    from anila_core.ingestion.parser_registry import ParserRegistry

    path = tmp_path / "summary.cpp"
    path.write_text(body, encoding="utf-8")
    parsed = ParserRegistry.parse(str(path))
    assert parsed.metadata["source_boundary_quality"] == quality
    assert "quality_notice" in parsed.metadata
    chunks = get_chunker("hierarchical").chunk(parsed.content, parsed.metadata, {})
    assert "".join(chunk.content for chunk in chunks) == body
    assert all(chunk.metadata["source_boundary_quality"] == quality for chunk in chunks)
    assert all("quality_notice" in chunk.metadata for chunk in chunks)


@pytest.mark.parametrize("language", ["cpp", "python"])
def test_source_scan_byte_limit_precedes_ast_or_token_allocation(monkeypatch, language):
    from anila_core.ingestion import source_code

    def forbidden(*args, **kwargs):
        raise AssertionError("scanner must stop before AST/token construction")

    monkeypatch.setattr(source_code, "_lex_cpp", forbidden)
    monkeypatch.setattr(source_code.ast, "parse", forbidden)
    text = ";" * (source_code.MAX_SOURCE_SCAN_BYTES + 1)
    units = source_code.source_units(text, language)
    assert units[0]["source_boundary_quality"] == "conservative"
    assert units[0]["source_boundary_reason"] == "source_scan_limit"
    assert units[0]["source_scan_byte_limit"] == 1024 * 1024


def test_token_scan_stops_before_constructing_remaining_tokens(monkeypatch):
    from anila_core.ingestion import source_code

    original = source_code._Token
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert calls <= source_code.MAX_SOURCE_TOKENS
        return original(*args, **kwargs)

    monkeypatch.setattr(source_code, "_Token", counted)
    text = ";" * (source_code.MAX_SOURCE_TOKENS + 1000) + '"unterminated'
    units = source_code.source_units(text, "cpp")
    assert calls == source_code.MAX_SOURCE_TOKENS
    assert units[0]["source_boundary_reason"] == "source_scan_limit"
    assert units[0]["source_scan_token_limit"] == 100_000
    assert units[0]["line_end"] == 1


def test_many_small_functions_over_atomic_file_size_remain_independent():
    text = "".join(f"int function_{i}() {{ return {i}; }}\n" for i in range(700))
    assert 16 * 1024 < len(text.encode("utf-8")) < 1024 * 1024
    units = source_units(text, "cpp")
    assert len(units) == 700
    assert all(unit["source_boundary_quality"] == "lexical" for unit in units)
    chunks = get_chunker("hierarchical").chunk(text, {"source_units": units}, {})
    assert len(chunks) == 700
    assert "".join(chunk.content for chunk in chunks) == text


@pytest.mark.parametrize("newline", ["\r", "\u2028"])
def test_unusual_line_breaks_preserve_all_lines_with_conservative_reason(newline):
    text = newline.join(["int first() { return 1; }", "int second() { return 2; }", ""])
    units = source_units(text, "cpp")
    assert len(units) == 1
    assert units[0]["line_end"] == len(text.splitlines())
    assert units[0]["source_boundary_reason"] == "unsupported_source_line_break"


@pytest.mark.parametrize(
    "strategy", ["fixed", "hierarchical", "markdown-aware", "pdf-page", "cjk-sentence", "semantic"]
)
@pytest.mark.parametrize("kind", ["source", "table", "fence"])
def test_hard_limit_blocks_atomic_before_embedding_without_truncation(strategy, kind):
    from anila_core.ingestion.chunking_plugins.structure import validate_structure_limits
    from anila_core.ingestion.errors import ChunkError

    if kind == "source":
        text = "int huge() {\n" + "  call();\n" * 1900 + "}\n"
        metadata = {"source_units": source_units(text, "cpp"), "source_filename": "huge.cpp"}
    elif kind == "table":
        text = "| A |\n|---|\n" + "| 檢核內容 |\n" * 1900
        metadata = {}
    else:
        text = "```cpp\n" + "call();\n" * 2400 + "```\n"
        metadata = {}
    for operation in [
        lambda: validate_structure_limits(text, metadata),
        lambda: get_chunker(strategy).chunk(text, metadata, {"max_leaf_tokens": 100_000}),
    ]:
        with pytest.raises(ChunkError) as error:
            operation()
        assert error.value.code == "E_CHUNK_TOO_LARGE"
        assert error.value.retryable is False
        assert error.value.details["actual_bytes"] > 16 * 1024
        assert error.value.details["atomic_byte_limit"] == 16 * 1024
        assert error.value.details["limit_policy"] == "platform_atomic_utf8"
        assert "未截斷" in error.value.user_message
        if kind == "source":
            assert error.value.details["source_filename"] == "huge.cpp"
            assert error.value.details["line_start"] == 1


@pytest.mark.parametrize("payload", ["a" * (16 * 1024 - 8), "中" * ((16 * 1024 - 8) // 3)])
def test_hard_limit_measures_utf8_bytes_not_characters_or_estimated_tokens(payload):
    from anila_core.ingestion.chunking_plugins.structure import validate_structure_limits
    from anila_core.ingestion.errors import ChunkError

    within = "```\n" + payload + "\n```"
    validate_structure_limits(within, {})
    with pytest.raises(ChunkError):
        validate_structure_limits(
            "```\n" + payload + "a" * (16 * 1024 - len(within.encode("utf-8")) + 1) + "\n```", {}
        )
@pytest.mark.parametrize("preserve_pages", [False, True])
@pytest.mark.parametrize("reliable_pages", [False, True])
def test_pdf_atomic_page_limit_depends_on_strategy_and_reliable_markers(
    preserve_pages, reliable_pages
):
    from anila_core.ingestion.chunking_plugins.structure import validate_structure_limits
    from anila_core.ingestion.errors import ChunkError

    text = "界" * 6000
    metadata = {"format": "pdf", "pages": 1 if reliable_pages else 2}
    if preserve_pages and reliable_pages:
        with pytest.raises(ChunkError) as blocked:
            validate_structure_limits(text, metadata, preserve_pages=preserve_pages)
        assert blocked.value.code == "E_CHUNK_TOO_LARGE"
    else:
        validate_structure_limits(text, metadata, preserve_pages=preserve_pages)
