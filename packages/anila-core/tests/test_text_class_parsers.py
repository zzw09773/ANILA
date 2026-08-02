"""Text-class parser registry entries for chat + KB ingestion.

Pins: listed extensions extract via PlainTextParser; archives stay unsupported
(no blanket unknown→text fallback); binary-as-text is refused; NULs stripped.
"""

from __future__ import annotations

import inspect
import io
import struct
import unicodedata
import zipfile

import pytest

from anila_core.ingestion.errors import ParseError
from anila_core.ingestion.parsers import extract_text
import anila_core.ingestion.parser_registry as parser_registry


def test_extract_text_py_round_trip() -> None:
    sample = (
        "#!/usr/bin/env python3\n"
        "def greet(name: str) -> str:\n"
        "    return f'hello {name}'\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(greet('anila'))\n"
    )
    text, _metadata, images = extract_text(
        "helper.py", sample.encode("utf-8"), "text/x-python",
    )
    assert "def greet(name: str)" in text
    assert "hello {name}" in text
    assert text == sample
    assert images == {}


def test_extract_text_csv_round_trip() -> None:
    sample = (
        "id,name,score\n"
        "1,alpha,9.5\n"
        "2,beta,8.1\n"
        "3,gamma,7.0\n"
    )
    text, _metadata, images = extract_text(
        "scores.csv", sample.encode("utf-8"), "text/csv",
    )
    assert "id,name,score" in text
    assert "gamma,7.0" in text
    assert text == sample
    assert images == {}


def test_extract_text_yaml_round_trip() -> None:
    sample = (
        "service: anila-csp\n"
        "replicas: 2\n"
        "env:\n"
        "  - name: LOG_LEVEL\n"
        "    value: info\n"
        "features:\n"
        "  attachments: true\n"
    )
    text, _metadata, images = extract_text(
        "deploy.yaml", sample.encode("utf-8"), "application/x-yaml",
    )
    assert "service: anila-csp" in text
    assert "attachments: true" in text
    assert text == sample
    assert images == {}


def test_extract_text_sql_round_trip() -> None:
    sample = (
        "-- seed users\n"
        "CREATE TABLE users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  email TEXT NOT NULL UNIQUE\n"
        ");\n"
        "INSERT INTO users (email) VALUES ('ops@example.mil');\n"
    )
    text, _metadata, images = extract_text(
        "seed.sql", sample.encode("utf-8"), "application/sql",
    )
    assert "CREATE TABLE users" in text
    assert "ops@example.mil" in text
    assert text == sample
    assert images == {}


def test_extract_text_zip_remains_unsupported() -> None:
    """Load-bearing: archives must NOT fall through to PlainTextParser."""
    # Minimal local-file header bytes; content is irrelevant — routing is by ext.
    blob = b"PK\x03\x04" + b"\x00" * 26 + b"not-a-real-archive"
    with pytest.raises(ParseError) as exc_info:
        extract_text("bundle.zip", blob, "application/zip")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert ".zip" in (err.user_message or "")
    # L2: user-visible message must not dump the full registry listing.
    assert "Supported:" not in (err.user_message or "")
    assert "registry_message" in (err.details or {})
    assert "Supported:" in (err.details or {}).get("registry_message", "")


def test_extract_text_strips_nul_bytes() -> None:
    """BOM'd UTF-16LE NULs are stripped; BOM-less UTF-16LE is refused.

    Owner 2026-08-02: no BOM-less UTF-16 speculation. PowerShell-style
    UTF-16LE without BOM must surface an actionable re-save message.
    """
    blob = "hello\n".encode("utf-16-le")
    with pytest.raises(ParseError) as exc_info:
        extract_text("ps.log", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")
    # BOM path still strips NULs and keeps the text.
    text, _metadata, images = extract_text(
        "ps.log", b"\xff\xfe" + blob, "text/plain",
    )
    assert "\x00" not in text
    assert "hello" in text
    assert images == {}


def test_extract_text_binary_high_replacement_unsupported() -> None:
    """Renamed binary must not extract as ok garbage billed as tokens."""
    # Synthetic blob with high U+FFFD ratio under utf-8 errors=replace
    # (mirrors /bin/ls first 20 KB behaviour without depending on host fs).
    blob = bytes(range(256)) * 40  # 10 KiB of non-text
    with pytest.raises(ParseError) as exc_info:
        extract_text("fake.py", blob, "text/x-python")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "") or "text" in (err.user_message or "").lower()


@pytest.mark.parametrize(
    "ext",
    [".py", ".txt", ".md", ".json", ".html", ".htm", ".dat", ".dcm", ".inp", ".out"],
)
def test_extract_text_renamed_binary_unsupported_across_text_family(ext: str) -> None:
    """M1: text-family (incl. DATCOM .dat) share _decode_text_bytes — binary never ok."""
    blob = bytes(range(256)) * 40
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"evil{ext}", blob, "application/octet-stream")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "\x00" not in (err.user_message or "")
    assert "文字" in (err.user_message or "")


def test_extract_text_legacy_big5_fails_visibly() -> None:
    """Owner 2026-08-01: no Big5 fallback — must be unsupported, not wrong CJK."""
    # Long enough to miss the short-file floor; high U+FFFD under UTF-8 replace.
    sample = ("姓名,單位,代號\n王小明,資訊室,A01\n" * 40)
    blob = sample.encode("big5")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "Big5" in (err.user_message or "")
    assert "UTF-8" in (err.user_message or "")
    # Must NOT silently surface the original traditional characters.
    assert "王小明" not in (err.user_message or "")


def test_extract_text_legacy_gbk_fails_visibly_not_wrong_traditional() -> None:
    """Pins the GBK→假傳統 failure mode is gone: refuse, do not invent 儂壽…."""
    # Owner-verified sample that formerly decoded via cp950 into wrong text.
    line = "机关单位设备清单：计算机、打印机、扫描仪\n"
    blob = (line * 20).encode("gbk")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "GBK" in (err.user_message or "")
    assert "机关" not in (err.user_message or "")
    assert "儂壽" not in (err.user_message or "")


def test_extract_text_legacy_latin1_unsupported_when_long() -> None:
    """latin-1/cp1252 are also out — long accented payload must not round-trip."""
    sample = ("café naïve résumé Ångström\n" * 20)
    blob = sample.encode("latin-1")
    with pytest.raises(ParseError) as exc_info:
        extract_text("notes.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "Latin-1" in (err.user_message or "") or "Windows-1252" in (
        err.user_message or ""
    )


def test_extract_text_utf16le_cjk_bomless_refused() -> None:
    """BOM-less UTF-16LE CJK must be refused with re-save-as-UTF-8 guidance."""
    sample = "姓名,單位,代號\n王小明,資訊室,A01\n"
    blob = sample.encode("utf-16-le")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")
    assert "王小明" not in (err.user_message or "")


def test_extract_text_utf16be_cjk_bomless_refused() -> None:
    """BOM-less UTF-16BE CJK must be refused with re-save-as-UTF-8 guidance."""
    sample = "姓名,單位,代號\n王小明,資訊室,A01\n"
    blob = sample.encode("utf-16-be")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")
    assert "王小明" not in (err.user_message or "")


def test_extract_text_short_file_one_bad_byte_ok() -> None:
    """M2 floor: a 9-char CSV with one bad byte must not hard-reject."""
    blob = b"a,b,c\xff\n"
    text, _metadata, _images = extract_text("tiny.csv", blob, "text/csv")
    assert "a,b,c" in text
    assert "\x00" not in text


def test_extract_text_high_nul_ratio_unsupported() -> None:
    """Load-bearing: _MAX_RAW_NUL_RATIO refuses high-NUL after UTF-16 fails.

    Short + high NUL would otherwise ride the length floor as U+FFFD garbage.
    Mutating the gate to 1.0 makes this test go RED.
    """
    blob = b"\x00\xff\x00\xfe\x00"  # nul_ratio=0.6, odd length → UTF-16 refuses
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


def test_extract_text_high_control_ratio_unsupported() -> None:
    """Load-bearing: _MAX_CONTROL_RATIO rejects C0-heavy clean UTF-8.

    Mutating _MAX_CONTROL_RATIO to 1.0 makes this test go RED (would return
    the control bytes as 'ok' text via the clean-UTF-8 path).
    """
    blob = bytes([0x01, 0x02, 0x03, 0x04, 0x05]) * 40  # 200 bytes, valid UTF-8
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


def test_extract_text_short_file_hits_length_floor() -> None:
    """_MIN_RATIO_LENGTH floor: short mostly-text + bad bytes stay best-effort.

    Pure control/binary shorts must NOT fail-open (round-10 filler-closure);
    the floor only helps when some readable scalar is already present.
    """
    blob = b"a,b,c\xff\n"
    text, _metadata, _images = extract_text("tiny.csv", blob, "text/csv")
    assert "a,b,c" in text
    assert "\ufffd" in text
    assert len(text) < 200
    assert "\x00" not in text


def test_extract_text_short_pure_control_unsupported() -> None:
    """Short fail-open hole closed: 49×DEL / pure C0 must not accept."""
    for blob in (b"\x7f" * 49, bytes([0x01, 0x02, 0x03, 0x04, 0x05]) * 3):
        with pytest.raises(ParseError) as exc_info:
            extract_text("evil.txt", blob, "text/plain")
        assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


_DATCOM_DECK = (
    "CASEID AlbatrossII\n"
    " $FLTCON NALPHA=  10.0,\n"
    "         ALSCHD= -4.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0,\n"
    "                  12.0, 14.0,\n"
    "         NMACH=  1.0,\n"
    "         MACH= 0.102,\n"
    "         NALT=  1.0,\n"
    "         ALT= 6000.0,\n"
    "         WT=  1200.0$\n"
    "NACA-W-4-7418-33\n"
    "NACA-H-4-2412\n"
    "NACA-V-4-0015\n"
)


def test_extract_text_datcom_dcm_round_trip() -> None:
    """USAF Digital DATCOM deck: CASEID + $FLTCON + NACA cards intact."""
    # Representative excerpt of owner sample ~/下載/test.dcm (plain ASCII).
    sample = _DATCOM_DECK
    text, _metadata, images = extract_text(
        "albatross.dcm", sample.encode("ascii"), "text/plain",
    )
    assert text == sample
    assert "CASEID AlbatrossII" in text
    assert "$FLTCON" in text
    assert "WT=  1200.0$" in text
    assert "NACA-W-4-7418-33" in text
    assert "NACA-H-4-2412" in text
    assert "NACA-V-4-0015" in text
    assert images == {}


def test_extract_text_dat_ascii_round_trip() -> None:
    sample = "TITLE DATCOM OUTPUT\n CL = 0.42\n CD = 0.031\n"
    text, _metadata, images = extract_text(
        "run.dat", sample.encode("ascii"), "text/plain",
    )
    assert text == sample
    assert "CL = 0.42" in text
    assert images == {}


@pytest.mark.parametrize("name", ["for005", "for006"])
def test_extract_text_extensionless_datcom_by_content(name: str) -> None:
    """DATCOM for005/for006 have no extension — content gates must accept ASCII."""
    sample = _DATCOM_DECK
    text, _metadata, images = extract_text(
        name, sample.encode("ascii"), "application/octet-stream",
    )
    assert text == sample
    assert "CASEID AlbatrossII" in text
    assert "$FLTCON" in text
    assert "NACA-W-4-7418-33" in text
    assert "\x00" not in text
    assert images == {}


def test_extract_text_extensionless_binary_unsupported() -> None:
    """Extensionless binary must be refused by decode guards, not by name."""
    try:
        with open("/bin/ls", "rb") as fh:
            blob = fh.read(20 * 1024)
    except OSError:
        blob = bytes(range(256)) * 80  # 20 KiB synthetic fallback
    assert len(blob) >= 1024
    with pytest.raises(ParseError) as exc_info:
        extract_text("for006", blob, "application/octet-stream")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "\x00" not in (err.user_message or "")


@pytest.mark.parametrize(
    "name,sample",
    [
        (
            "README",
            "# ANILA\n\nNotebookLM-style intranet platform.\n\n## Build\n\nmake test\n",
        ),
        (
            "Makefile",
            ".PHONY: test\ntest:\n\tpytest -q\n\ninstall:\n\tpip install -e .\n",
        ),
        (
            "Dockerfile",
            "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install .\n",
        ),
        (
            "LICENSE",
            "MIT License\n\nCopyright (c) 2026 ANILA\n\nPermission is hereby granted...\n",
        ),
    ],
)
def test_extract_text_extensionless_named_text_files(
    name: str, sample: str,
) -> None:
    text, _metadata, images = extract_text(
        name, sample.encode("utf-8"), "application/octet-stream",
    )
    assert text == sample
    assert "\x00" not in text
    assert images == {}


def test_extract_text_svg_not_plain_text() -> None:
    """F2: .svg is image-only — must not route to PlainTextParser."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
    with pytest.raises(ParseError) as exc_info:
        extract_text("drawing.svg", svg, "image/svg+xml")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "Supported:" not in (exc_info.value.user_message or "")


# ── Low-entropy engineering binaries (HIGH regression) ───────────────────


def _zip_stored(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", payload)
    return buf.getvalue()


def _int32_array(n: int = 6000, start: int = 1000) -> bytes:
    return struct.pack("<" + "i" * n, *range(start, start + n))


def _tecplot_binary(n_floats: int = 2000) -> bytes:
    return b"#!TDV112" + struct.pack(
        "<" + "f" * n_floats, *[float(i) * 0.1 for i in range(n_floats)]
    )


@pytest.mark.parametrize(
    "name,factory",
    [
        ("bundle", lambda: _zip_stored(b"hellohellohello\n" * 150)),
        ("model.dat", lambda: _zip_stored(b"hellohellohello\n" * 150)),
        ("mesh.dat", _int32_array),
        ("mesh", _int32_array),
        ("results.dat", _tecplot_binary),
        ("results", _tecplot_binary),
        # Large ZIP_STORED of ASCII: low NUL, escapes lane gate — soft floor must refuse.
        ("bundle_large", lambda: _zip_stored(b"hellohellohello\n" * 30_000)),
    ],
)
def test_extract_text_low_entropy_binary_unsupported(name: str, factory) -> None:
    """Engineering binaries must not bill/inject as ok CJK mojibake."""
    blob = factory()
    with pytest.raises(ParseError) as exc_info:
        extract_text(name, blob, "application/octet-stream")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "\x00" not in (err.user_message or "")


def _repo_han_prose(n: int) -> str:
    """First ``n`` Han characters of this repo's ``SYSTEM-MAP.md``.

    C3: fixtures on the UTF-16 path must come from the population the path
    actually serves. Repeated-phrase fixtures ("姓名單位王小明"×10) have a
    uniq_ratio of 0.001-0.10; real prose runs 0.93 (n=40) to 0.35 (n=900),
    and every round that tuned a "is this real text" predicate passed on
    the former and failed on the latter.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = root / "SYSTEM-MAP.md"
    if not src.is_file():  # authority doc — absence is a real failure
        pytest.fail(f"prose fixture source missing: {src}")
    han = [c for c in src.read_text(encoding="utf-8") if 0x4E00 <= ord(c) <= 0x9FFF]
    assert len(han) >= n, "SYSTEM-MAP.md no longer has enough Han prose"
    return "".join(han[:n])


def test_repo_prose_fixture_is_not_a_repeated_phrase() -> None:
    """C3 guard: the fixture must keep real-prose vocabulary spread."""
    bands = {40: (0.80, 1.00), 150: (0.60, 0.90), 400: (0.40, 0.70),
             900: (0.25, 0.50)}
    for n, (lo, hi) in bands.items():
        s = _repo_han_prose(n)
        uniq = len(set(s)) / len(s)
        assert lo <= uniq <= hi, f"n={n} uniq_ratio={uniq:.3f} outside {lo}-{hi}"


def test_extract_text_utf16_bom_ascii_dominant_ok() -> None:
    """Contract: UTF-16 is supported for ASCII-dominant documents."""
    sample = (
        "# deployment notes\n"
        "restart the csp container, then reload nginx.\n"
        "owner: 王小明 (資訊室)\n"
    ) * 20
    assert sum(1 for c in sample if ord(c) < 0x80) * 2 > len(sample)
    for raw in (sample.encode("utf-16"),
                b"\xfe\xff" + sample.encode("utf-16-be")):
        text, _metadata, _images = extract_text("notes.txt", raw, "text/plain")
        assert text == sample
        assert "王小明" in text


@pytest.mark.parametrize("n", [40, 150, 400, 900, 1500])
def test_extract_text_utf16_bom_cjk_refused_with_contract_message(n: int) -> None:
    """Contract: predominantly non-ASCII UTF-16 must be re-saved as UTF-8.

    Owner ruling 2026-08-02. This is a stated limit, not a mystery: the
    identical content in UTF-8 is accepted byte-exact (asserted below), and
    the refusal names the fix. Rounds 8-13 tried to keep this content by
    judging whether it "reads as prose"; every attempt either billed a
    binary or refused real Chinese. Do NOT re-open by tuning a predicate.
    """
    sample = _repo_han_prose(n)
    with pytest.raises(ParseError) as exc_info:
        extract_text("note.txt", sample.encode("utf-16"), "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "非 ASCII" in (err.user_message or "")
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")
    # The same content in UTF-8 is untouched (I1).
    text, _metadata, _images = extract_text(
        "note.txt", sample.encode("utf-8"), "text/plain",
    )
    assert text == sample


def test_extract_text_utf16_three_line_chinese_note_refused_utf8_ok() -> None:
    """Notepad "Unicode" 3-line note: refused, but the UTF-8 save works."""
    note = "會議紀錄\n王小明報告專案進度\n下週追蹤氣隙部署\n"
    with pytest.raises(ParseError) as exc_info:
        extract_text("note.txt", note.encode("utf-16"), "text/plain")
    assert "另存" in (exc_info.value.user_message or "")
    text, _metadata, _images = extract_text(
        "note.txt", note.encode("utf-8"), "text/plain",
    )
    assert text == note


def test_extract_text_utf8_sig_round_trip() -> None:
    sample = "CASEID AlbatrossII\n $FLTCON NALPHA=  10.0,\n"
    text, _metadata, _images = extract_text(
        "deck.dcm", sample.encode("utf-8-sig"), "text/plain",
    )
    assert "CASEID AlbatrossII" in text
    assert "$FLTCON" in text


def test_mutation_bom_utf16_ascii_dominance_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing: ASCII dominance ALONE stops a BOM'd instrument band.

    A BOM'd uint16 band must stay unsupported. Round 14's version of this
    test said "alone" but neutered three predicates
    (``_is_ascii_dominant`` + ``_is_text_like`` + ``_readable_ratio``), so
    it could not tell which one mattered. Measured 2026-08-02: neutering
    ``_is_ascii_dominant`` on its own bills the band, while neutering
    either of the other two on its own does not. The test now performs the
    single mutation its docstring describes.
    """
    import struct

    blob = b"\xff\xfe" + struct.pack(
        "<" + "H" * 4000, *([(20000 + i) & 0xFFFF for i in range(4000)]),
    )
    with pytest.raises(ParseError) as exc_info:
        extract_text("results.dat", blob, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    # The other two, each on their own, must NOT be enough.
    for name, stub in (("_is_text_like", lambda _t: True),
                       ("_readable_ratio", lambda _t: 1.0)):
        with monkeypatch.context() as mp:
            mp.setattr(parser_registry, name, stub)
            with pytest.raises(ParseError):
                extract_text("results.dat", blob, "application/octet-stream")

    monkeypatch.setattr(parser_registry, "_is_ascii_dominant", lambda _t: True)
    text, _metadata, _images = extract_text(
        "results.dat", blob, "application/octet-stream",
    )
    assert len(text) > 100
    assert "\x00" not in text


def test_mutation_clean_utf8_counts_nuls_as_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing: clean-UTF-8 control_ratio must include raw NULs.

    7-bit-clean + high NUL + no lane dominance must be unsupported. If
    controls are measured only after NUL strip, this blob returns ok
    ``HELLO WORLD`` — the hole-(a) regression.
    """
    blob = (b"HELLO WORLD\n" * 30) + (b"\x00" * 200)
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    real_control = parser_registry._control_ratio
    monkeypatch.setattr(
        parser_registry,
        "_control_ratio",
        lambda text: real_control(text.replace("\x00", "")),
    )
    text, _metadata, _images = extract_text("evil.txt", blob, "text/plain")
    assert "HELLO WORLD" in text
    assert "\x00" not in text


def test_mutation_soft_utf8_rejects_residual_controls() -> None:
    """Load-bearing: long low-FFFD + residual C0 must not soft-accept.

    Large ZIP_STORED of ASCII has tiny U+FFFD; the pre-fix soft floor
    (``repl <= max`` alone) would return ok. Pin soft-residual C0 > 0 and
    clustered so ``_soft_residual_controls_reject`` (shared with F3) keeps
    the ZIP unsupported.
    """
    blob = _zip_stored(b"hellohellohello\n" * 30_000)
    assert len(blob) > parser_registry._MIN_RATIO_LENGTH
    with pytest.raises(ParseError) as exc_info:
        extract_text("bundle", blob, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    decoded = blob.decode("utf-8", errors="replace")
    soft = parser_registry._strip_nuls(decoded)
    assert parser_registry._replacement_ratio(soft) <= parser_registry._MAX_REPLACEMENT_RATIO
    # C1: assert on the load-bearing helper, not the dead ratio wrapper.
    assert len(parser_registry._soft_residual_control_indices(soft)) > 0
    assert parser_registry._soft_residual_controls_reject(soft)
    assert "hellohellohello" in soft


def _uint16_band(n: int, base: int, *, be: bool = False) -> bytes:
    fmt = (">" if be else "<") + "H" * n
    return struct.pack(fmt, *[(base + (i % 500)) & 0xFFFF for i in range(n)])


@pytest.mark.parametrize(
    "name,factory",
    [
        ("results.dat", lambda: _uint16_band(4000, 20000)),
        ("for005", lambda: _uint16_band(4000, 20000)),
        ("scan.txt", lambda: _uint16_band(4000, 20000)),
        ("results.dat", lambda: _uint16_band(4000, 24000)),
        ("for005", lambda: _uint16_band(4000, 24000)),
        ("scan.txt", lambda: _uint16_band(4000, 24000)),
        ("results.dat", lambda: _uint16_band(4000, 32768)),
        ("for005", lambda: _uint16_band(4000, 32768)),
        ("scan.txt", lambda: _uint16_band(4000, 32768)),
        ("results.dat", lambda: _uint16_band(4000, 40000)),
        ("for005", lambda: _uint16_band(4000, 40000)),
        ("scan.txt", lambda: _uint16_band(4000, 40000)),
        ("results.dat", lambda: _uint16_band(4000, 20000, be=True)),
        ("for005", lambda: _uint16_band(4000, 32768, be=True)),
        ("scan.txt", lambda: _uint16_band(4000, 40000, be=True)),
        # int16 pressure ~25000 (positive mid-band) as little-endian words
        ("results.dat", lambda: struct.pack("<" + "h" * 4000, *([25000] * 4000))),
        ("for005", lambda: struct.pack("<" + "h" * 4000, *([25000] * 4000))),
        ("scan.txt", lambda: struct.pack("<" + "h" * 4000, *([25000] * 4000))),
        # uint16 greyscale raster ~30000
        ("results.dat", lambda: _uint16_band(4000, 30000)),
        ("for005", lambda: _uint16_band(4000, 30000)),
        ("scan.txt", lambda: _uint16_band(4000, 30000)),
    ],
    ids=[
        "u16-20000-le-dat", "u16-20000-le-for005", "u16-20000-le-txt",
        "u16-24000-le-dat", "u16-24000-le-for005", "u16-24000-le-txt",
        "u16-32768-le-dat", "u16-32768-le-for005", "u16-32768-le-txt",
        "u16-40000-le-dat", "u16-40000-le-for005", "u16-40000-le-txt",
        "u16-20000-be-dat", "u16-32768-be-for005", "u16-40000-be-txt",
        "i16-25000-dat", "i16-25000-for005", "i16-25000-txt",
        "u16-30000-dat", "u16-30000-for005", "u16-30000-txt",
    ],
)
def test_extract_text_uint16_instrument_band_unsupported(name: str, factory) -> None:
    """F1: uint16 mid-scale / CJK-codepoint bands must never bill as ok CJK."""
    blob = factory()
    with pytest.raises(ParseError) as exc_info:
        extract_text(name, blob, "application/octet-stream")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (err.user_message or "")
    assert "\x00" not in (err.user_message or "")


@pytest.mark.parametrize(
    "line,label",
    [
        ("\x1b[32mINFO\x1b[0m  service started ok\n", "ansi-5.9pct"),
        ("\x1b[32mINFO\x1b[0m \x1b[36mmod\x1b[0m service started ok\n", "ansi-9.3pct"),
    ],
)
def test_extract_text_ansi_coloured_log_accepted(line: str, label: str) -> None:
    """F3: ANSI CSI ESC must not trip _MAX_CONTROL_RATIO on .log."""
    blob = (line * 40).encode("ascii")
    # Precondition: raw C0 (incl. ESC) would exceed the 5% gate.
    raw_ctrl = sum(
        1 for c in blob.decode("ascii") if ord(c) < 32 and c not in "\n\r\t"
    ) / len(blob.decode("ascii"))
    assert raw_ctrl > parser_registry._MAX_CONTROL_RATIO, label
    text, _metadata, images = extract_text("app.log", blob, "text/plain")
    assert text == blob.decode("ascii")
    assert "service started ok" in text
    assert images == {}


def test_extract_text_form_feed_and_stray_byte_accepted() -> None:
    """F4: Fortran FF page break + one legacy stray must soft-accept."""
    body = (
        "TITLE DATCOM FOR006 LISTING\n"
        + ("CL = 0.42  CD = 0.031  XCP = -0.12\n" * 8)
        + "\x0c"
        + ("PAGE 2 CONTINUED\n" * 4)
    )
    blob = body.encode("ascii") + b"\xff"
    assert len(blob) >= parser_registry._MIN_RATIO_LENGTH
    text, _metadata, images = extract_text("for006", blob, "application/octet-stream")
    assert "TITLE DATCOM FOR006 LISTING" in text
    assert "\x0c" in text
    assert "\ufffd" in text
    assert "\x00" not in text
    assert images == {}


@pytest.mark.parametrize("ctrl", ["\x0c", "\x0b"])
def test_extract_text_form_feed_or_vt_alone_accepted(ctrl: str) -> None:
    sample = ("LINE OF OUTPUT DATA FOLLOWS\n" * 10) + ctrl + ("MORE\n" * 5)
    text, _metadata, _images = extract_text(
        "listing.out", sample.encode("ascii"), "text/plain",
    )
    assert text == sample
    assert ctrl in text


def test_extract_text_csv_crlf_round_trip() -> None:
    sample = "id,name\r\n1,alpha\r\n2,beta\r\n"
    text, _metadata, images = extract_text(
        "scores.csv", sample.encode("ascii"), "text/csv",
    )
    assert text == sample
    assert images == {}


def test_extract_text_python_escaped_esc_literal_accepted() -> None:
    """Escaped ``\\x1b`` in source is four ASCII chars — not a lone ESC byte."""
    sample = "#!/usr/bin/env python3\nreset = \"\\x1b[0m\"\n" + ("print(reset)\n" * 20)
    assert "\x1b" not in sample  # the source text has backslash-x, not ESC
    text, _metadata, images = extract_text(
        "ansi_helper.py", sample.encode("utf-8"), "text/x-python",
    )
    assert text == sample
    assert images == {}


# ── ESC / FF / VT: structural CSI exemption (HIGH regression pin) ─────────


def test_extract_text_pure_esc_unsupported() -> None:
    """I1/I4: 100% ESC must not bill as ok (unbounded _TEXTUAL_C0 exemption)."""
    blob = b"\x1b" * 4000
    with pytest.raises(ParseError) as exc_info:
        extract_text("spool.log", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


def test_extract_text_pure_esc_ff_vt_mix_unsupported() -> None:
    blob = (b"\x1b\x0c\x0b" * 40_000)  # 120 KB, 100% exempt-under-old-rule
    assert len(blob) >= 100_000
    with pytest.raises(ParseError) as exc_info:
        extract_text("spool.log", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


@pytest.mark.parametrize("esc_pct", [0.20, 0.50])
def test_extract_text_esc_diluted_printable_unsupported(esc_pct: float) -> None:
    n = 10_000
    n_esc = int(n * esc_pct)
    body = (b"INFO service ok\n" * 800)[: n - n_esc] + (b"\x1b" * n_esc)
    assert abs(body.count(0x1B) / len(body) - esc_pct) < 0.02
    with pytest.raises(ParseError) as exc_info:
        extract_text("spool.log", body, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


@pytest.mark.parametrize("ctrl_pct", [0.30, 0.90])
def test_extract_text_exempt_c0_mix_diluted_unsupported(ctrl_pct: float) -> None:
    n = 10_000
    n_ctrl = int(n * ctrl_pct)
    pads = [0x1B, 0x0C, 0x0B]
    ctrl = bytes(pads[i % 3] for i in range(n_ctrl))
    body = (b"A" * (n - n_ctrl)) + ctrl
    with pytest.raises(ParseError) as exc_info:
        extract_text("spool.log", body, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


def _esc_interleave(payload: bytes, k: int) -> bytes:
    """Insert one ESC after every ``k`` payload bytes."""
    out = bytearray()
    for i, b in enumerate(payload):
        out.append(b)
        if (i + 1) % k == 0:
            out.append(0x1B)
    return bytes(out)


@pytest.mark.parametrize("k", [1, 2, 4])
@pytest.mark.parametrize(
    "name,factory",
    [
        ("elf", lambda: open("/bin/ls", "rb").read(20_000)),
        ("int32", lambda: _int32_array(4000)),
        ("tecplot", lambda: _tecplot_binary(1500)),
    ],
)
def test_extract_text_esc_interleave_keeps_binary_unsupported(
    name: str, factory, k: int,
) -> None:
    """I2: exempt-C0 interleave must not flip a rejected binary to ok."""
    try:
        blob = factory()
    except OSError:
        if name == "elf":
            blob = bytes(range(256)) * 80
        else:
            raise
    raw = blob if k == 0 else _esc_interleave(blob, k)
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"{name}.dat", raw, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


def test_mutation_unbounded_esc_exemption_reopens_pure_esc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing: blanket ESC exemption + neutered text-like accepts pure ESC.

    Round-10 ``_is_text_like`` alone refuses non-graphic scalars; both the
    old control-ratio hole and the positive gate must be cleared for
    4000×ESC to bill as ok again.
    """
    blob = b"\x1b" * 4000
    with pytest.raises(ParseError):
        extract_text("spool.log", blob, "text/plain")

    # Old unbounded exemption (pre-fix): remove ESC/FF/VT from numerator.
    monkeypatch.setattr(
        parser_registry, "_TEXTUAL_C0", frozenset("\n\r\t\f\v\x1b"),
    )

    def _unbounded(text: str) -> float:
        if not text:
            return 0.0
        return sum(
            1 for c in text
            if ord(c) < 32 and c not in parser_registry._TEXTUAL_C0
        ) / len(text)

    def _unbounded_indices(text: str) -> list[int]:
        return [
            i for i, c in enumerate(text)
            if ord(c) < 32
            and c not in parser_registry._TEXTUAL_C0
            and c not in "\f\v"
        ]

    monkeypatch.setattr(parser_registry, "_control_ratio", _unbounded)
    # C1: patch the function the soft floor actually calls. The former
    # target ``_soft_residual_control_ratio`` had no production caller, so
    # this mutation round proved nothing.
    monkeypatch.setattr(
        parser_registry, "_soft_residual_control_indices", _unbounded_indices,
    )
    monkeypatch.setattr(parser_registry, "_is_text_like", lambda _text: True)
    text, _metadata, _images = extract_text("spool.log", blob, "text/plain")
    assert text == "\x1b" * 4000


@pytest.mark.parametrize(
    "ext",
    [".txt", ".md", ".json", ".html", ".rtf"],
)
def test_extract_text_decode_guard_pinned_on_all_five_parsers(ext: str) -> None:
    """I6 (behaviour): all five call sites refuse control-heavy UTF-8."""
    blob = bytes([0x01, 0x02, 0x03, 0x04, 0x05]) * 40  # valid UTF-8, C0-heavy
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"evil{ext}", blob, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


@pytest.mark.parametrize(
    "cls_name",
    ["PlainTextParser", "MarkdownParser", "JsonParser", "HtmlParser", "RtfParser"],
)
def test_five_parsers_source_calls_decode_text_bytes(cls_name: str) -> None:
    """I6 (source): reverting any parser to ``path.read_text()`` makes this RED.

    Behavioural pins alone are brittle for Html/Json (post-processors can
    still raise); the parse() body must call ``_decode_text_bytes``.
    """
    import inspect

    cls = getattr(parser_registry, cls_name)
    src = inspect.getsource(cls.parse)
    assert "_decode_text_bytes" in src, f"{cls_name}.parse must decode via guard"
    assert "path.read_text()" not in src, (
        f"{cls_name}.parse must not use bare path.read_text()"
    )


@pytest.mark.parametrize(
    "name,factory",
    [
        ("note.rtf", lambda: _int32_array(4000)),
        ("note.rtf", lambda: ("姓名,單位,代號\n王小明\n" * 40).encode("big5")),
        ("note.rtf", lambda: open("/bin/ls", "rb").read(20_000)),
    ],
    ids=["rtf-int32", "rtf-big5", "rtf-elf"],
)
def test_extract_text_rtf_binary_unsupported(name: str, factory) -> None:
    """I6 RtfParser pin: binary-as-.rtf must be E_PARSE_FORMAT_UNSUPPORTED."""
    try:
        blob = factory()
    except OSError:
        blob = bytes(range(256)) * 80
    with pytest.raises(ParseError) as exc_info:
        extract_text(name, blob, "application/rtf")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


@pytest.mark.parametrize(
    "filename",
    [
        "test.dcm", "for005", "for006", "results.dat",
        "test.out", "test.inp", "test.txt", "deck",
    ],
)
@pytest.mark.parametrize(
    "source",
    ["/home/c1147259/下載/test.dcm", "/home/c1147259/下載/test.out"],
)
def test_extract_text_owner_datcom_byte_exact_under_every_name(
    filename: str, source: str,
) -> None:
    """I3: owner DATCOM fixtures stay byte-exact under every text-class name.

    C4 decision: this skip stays host-conditional on purpose — the point is
    the owner's own two files, which a synthetic deck cannot stand in for.
    Coverage does not vanish on other hosts:
    ``test_extract_text_datcom_dcm_round_trip`` and
    ``test_extract_text_extensionless_datcom_by_content`` carry inline decks
    and run everywhere.
    """
    path = __import__("pathlib").Path(source)
    if not path.is_file():
        pytest.skip(f"owner fixture missing: {source}")
    blob = path.read_bytes()
    text, _metadata, images = extract_text(
        filename, blob, "application/octet-stream",
    )
    assert text == blob.decode("utf-8")
    assert images == {}


# ── Round 10: positive readable-text (filler-closed) ──────────────────────


def _interleave(payload: bytes, filler: bytes, k: int) -> bytes:
    out = bytearray()
    for i in range(0, len(payload), k):
        out += payload[i : i + k]
        out += filler
    return bytes(out)


def _gzip_blob(n_lines: int = 400) -> bytes:
    import gzip

    return gzip.compress(b"payload line\n" * n_lines)


def _png_blob(w: int = 40, h: int = 40) -> bytes:
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b""
    for y in range(h):
        raw += b"\x00" + bytes([(x * 7 + y * 13) % 256 for x in range(w * 3)])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _a1_payloads() -> dict[str, bytes]:
    try:
        elf = open("/bin/true", "rb").read(4096)
    except OSError:
        elf = bytes(range(256)) * 16
    return {
        "elf": elf,
        "int32": _int32_array(1024),
        "tecplot": _tecplot_binary(400),
        "zip_stored_small": _zip_stored(b"hello reviewer\n" * 3),
        "zip_stored_large": _zip_stored(b"hellohellohello\n" * 30_000),
        "gzip": _gzip_blob(),
        "png": _png_blob(),
        "random": bytes((i * 37 + 11) % 256 for i in range(4096)),
        "uint16_20000_le": _uint16_band(2000, 20000),
        "uint16_20000_be": _uint16_band(2000, 20000, be=True),
        "uint16_24000_le": _uint16_band(2000, 24000),
        "uint16_24000_be": _uint16_band(2000, 24000, be=True),
        "uint16_32768_le": _uint16_band(2000, 32768),
        "uint16_32768_be": _uint16_band(2000, 32768, be=True),
        "uint16_40000_le": _uint16_band(2000, 40000),
        "uint16_40000_be": _uint16_band(2000, 40000, be=True),
        "int16_pressure": struct.pack("<" + "h" * 2000, *([1200] * 2000)),
        "uint16_raster": _uint16_band(3000, 0),
    }


def test_a1_exhaustive_single_byte_filler_closed() -> None:
    """A1/I-B: every f in range(256) × k in {1,2,3,4} × payload → unsupported.

    Written as a loop (not parametrize) so the next filler byte cannot be
    missed by omitting it from a list.
    """
    payloads = _a1_payloads()
    total = 0
    accepted = 0
    for _name, payload in payloads.items():
        for f in range(256):
            for k in (1, 2, 3, 4):
                total += 1
                raw = _interleave(payload, bytes([f]), k)
                try:
                    extract_text(f"evil_{_name}.dat", raw, "application/octet-stream")
                    accepted += 1
                except ParseError as exc:
                    assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert accepted == 0, f"A1 leaks: {accepted}/{total} accepted"
    assert total == len(payloads) * 256 * 4


def test_a2_multi_byte_filler_still_unsupported() -> None:
    """A2: 2-byte / 3-byte repeating fillers over ELF, int32, stored ZIP."""
    payloads = {
        "elf": _a1_payloads()["elf"],
        "int32": _int32_array(1024),
        "zip": _zip_stored(b"hello reviewer\n" * 3),
    }
    fillers = (b"\x20\x20", b"ab", b"\x1b\x00", b"\x00\xff\x00", b"\x1b[m")
    total = accepted = 0
    for _name, payload in payloads.items():
        for filler in fillers:
            for k in (1, 2, 3, 4):
                total += 1
                raw = _interleave(payload, filler, k)
                try:
                    extract_text(f"evil_{_name}.dat", raw, "application/octet-stream")
                    accepted += 1
                except ParseError as exc:
                    assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert accepted == 0, f"A2 leaks: {accepted}/{total}"
    assert total == len(payloads) * len(fillers) * 4


@pytest.mark.parametrize(
    "label,blob",
    [
        ("del", b"\x7f" * 4000),
        ("c1_nel", "\x85".encode("utf-8") * 4000),
        ("iaa", "\ufff9".encode("utf-8") * 2000),
        ("zwsp", "\u200b".encode("utf-8") * 4000),
    ],
)
def test_a3_non_graphic_scalars_unsupported(label: str, blob: bytes) -> None:
    """I-C: entirely non-graphic scalars must not bill as ok text."""
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"{label}.txt", blob, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "文字" in (exc_info.value.user_message or "")


def test_a5_mutation_text_like_true_reopens_filler_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5 part 1: neutering UTF-8 gates + parity-smuggle reopens a low-NUL weave.

    SPACE⊗ELF is now caught earlier by the high-NUL BOM-less refusal (no
    UTF-16 speculation). This pin uses a low-NUL one-sided weave so the
    surviving UTF-8 gates remain demonstrably load-bearing.
    """
    raw = b"".join(bytes([ord("x"), 0x7F]) for _ in range(2000))
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", raw, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    monkeypatch.setattr(parser_registry, "_is_text_like", lambda _t: True)
    monkeypatch.setattr(parser_registry, "_control_ratio", lambda _t: 0.0)
    monkeypatch.setattr(
        parser_registry, "_soft_residual_controls_reject", lambda _t: False,
    )
    monkeypatch.setattr(parser_registry, "_readable_ratio", lambda _t: 1.0)
    monkeypatch.setattr(
        parser_registry, "_parity_lanes_show_filler_smuggle", lambda _r: False,
    )
    text, _metadata, _images = extract_text("evil.txt", raw, "text/plain")
    assert len(text) > 100


def test_a5_mutation_parity_smuggle_still_holds_a1() -> None:
    """A5 part 2: surviving parity-smuggle gate keeps A1 closed."""
    payloads = {
        "elf": _a1_payloads()["elf"],
        "int32": _int32_array(1024),
        "zip": _zip_stored(b"hello\n" * 3),
        "u16": _uint16_band(2000, 20000),
    }
    accepted = 0
    total = 0
    for name, payload in payloads.items():
        for f in range(256):
            for k in (1, 2, 3, 4):
                total += 1
                raw = _interleave(payload, bytes([f]), k)
                try:
                    extract_text(f"{name}.dat", raw, "application/octet-stream")
                    accepted += 1
                except ParseError:
                    pass
    assert accepted == 0, f"A1 reopened: {accepted}/{total}"


def test_extract_text_python_literal_csi_esc_accepted() -> None:
    """Python source with a literal ESC-CSI sequence in a string stays ok."""
    sample = (
        "#!/usr/bin/env python3\n"
        "reset = \"\x1b[0m\"\n"
        + ("print(reset)\n" * 20)
    )
    assert "\x1b" in sample
    text, _metadata, images = extract_text(
        "ansi_helper.py", sample.encode("utf-8"), "text/x-python",
    )
    assert text == sample
    assert images == {}


def test_extract_text_sparse_latin1_lossy_soft_accept() -> None:
    """Owner trade-off: ~1.5% U+FFFD from sparse latin-1 stays lossy ok."""
    lines = [
        b"status update line %02d system ok cafe\xe9 resume notes\n" % i
        for i in range(40)
    ]
    blob = b"".join(lines)
    text, _metadata, _images = extract_text("notes.txt", blob, "text/plain")
    ratio = text.count("\ufffd") / len(text)
    assert 0.01 < ratio < 0.05
    assert "status update line" in text


def test_extract_text_bomless_utf16_mixed_ascii_cjk() -> None:
    """BOM-less UTF-16 mixed ASCII+CJK is refused with re-save guidance."""
    sample = ("id,name,score\n" * 5) + ("姓名,王小明,A01\n" * 10)
    blob = sample.encode("utf-16-le")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")


@pytest.mark.parametrize("endian", ["utf-16-le", "utf-16-be"])
def test_extract_text_bomless_utf16_mixed_cjk_phrase(endian: str) -> None:
    """BOM-less mixed-han phrase (nul≈0, high U+FFFD) is refused, not guessed."""
    sample = "中文測試內容" * 40
    blob = sample.encode(endian)
    assert blob.count(0) / len(blob) < 0.01
    utf8_view = parser_registry._strip_nuls(blob.decode("utf-8", errors="replace"))
    assert parser_registry._replacement_ratio(utf8_view) > parser_registry._MAX_REPLACEMENT_RATIO
    with pytest.raises(ParseError) as exc_info:
        extract_text("note.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    assert "另存" in (err.user_message or "")


def test_soft_floor_accepts_scattered_c0_with_one_fffd() -> None:
    """Soft floor shares F3 clustering: stray C0 + one ``\\xff`` still ok."""
    listing = ("      SUBROUTINE FOO(X)\n      X = X + 1.0\n" * 200)[:5000]
    body = bytearray(listing.encode("ascii"))
    step = max(1, len(body) // 10)
    for i in range(10):
        body[min(i * step, len(body) - 1)] = 0x1A
    body[100] = 0xFF
    text, _metadata, _images = extract_text("for006", bytes(body), "application/octet-stream")
    assert "SUBROUTINE" in text
    assert "\ufffd" in text


@pytest.mark.parametrize(
    "label,sample,encoding",
    [
        ("big5-phrase", "這是國家機關資料報表" * 40, "big5"),
        ("gbk-phrase", "机关单位设备清单计算机打印机" * 40, "gbk"),
    ],
)
def test_legacy_dbcs_not_lifted_as_utf16_hangul(
    label: str, sample: str, encoding: str,
) -> None:
    """Multi-char Big5/GBK must not soft-lift via dual-companion UTF-16."""
    blob = sample.encode(encoding)
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"{label}.txt", blob, "text/plain")
    err = exc_info.value
    assert err.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert "UTF-8" in (err.user_message or "")
    # Must not surface Hangul/CJK mojibake as accepted content.
    assert err.user_message is not None


def test_extract_text_pure_hiragana_utf8_accepted() -> None:
    """Smear heuristic must not kill legitimate single-script East-Asian text."""
    sample = ("あいうえおかきくけこさしすせそたちつてと\n" * 8)
    text, _metadata, _images = extract_text("note.txt", sample.encode("utf-8"), "text/plain")
    assert text == sample


def test_extract_text_cjk_single_unicode_page_utf8_accepted() -> None:
    """Chars confined to one CJK 256-block page remain readable UTF-8 text."""
    sample = "".join(chr(0x4E00 + i) for i in range(40)) * 2 + "\n"
    text, _metadata, _images = extract_text("note.txt", sample.encode("utf-8"), "text/plain")
    assert text == sample


@pytest.mark.parametrize(
    "cls_name,ext",
    [
        ("PlainTextParser", ".txt"),
        ("MarkdownParser", ".md"),
        ("JsonParser", ".json"),
        ("HtmlParser", ".html"),
        ("RtfParser", ".rtf"),
    ],
)
def test_a6_mutation_read_text_loses_behavioural_guard(
    monkeypatch: pytest.MonkeyPatch, cls_name: str, ext: str,
) -> None:
    """A6: reverting one parser to path.read_text makes C0-heavy blob accept."""
    from pathlib import Path

    blob = bytes([0x01, 0x02, 0x03, 0x04, 0x05]) * 40
    with pytest.raises(ParseError) as exc_info:
        extract_text(f"evil{ext}", blob, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    cls = getattr(parser_registry, cls_name)

    def _broken_parse(self, file_path: str):  # noqa: ANN001
        path = Path(file_path)
        content = path.read_text(encoding="utf-8", errors="replace")
        if cls_name == "JsonParser":
            import json

            try:
                content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                pass
        elif cls_name == "HtmlParser":
            # Minimal pass-through — enough to show decode guard is gone.
            pass
        elif cls_name == "RtfParser":
            try:
                from striprtf.striprtf import rtf_to_text

                content = rtf_to_text(content, errors="ignore") or content
            except Exception:
                pass
        return parser_registry.ParsedDocument(
            content=content or "\x01",
            metadata={"title": path.stem},
            source_path=file_path,
            format=ext.lstrip(".") or "txt",
        )

    monkeypatch.setattr(cls, "parse", _broken_parse)
    text, _metadata, _images = extract_text(
        f"evil{ext}", blob, "application/octet-stream",
    )
    # Behavioural RED: control bytes survive as "ok" extracted text.
    assert text
    assert "\x01" in text or "\ufffd" in text or len(text) >= 10


# ── Round-11: F1–F4 invariants ─────────────────────────────────────────────


def _smuggle(payload: bytes, filler: bytes = b"abcdefgh") -> bytes:
    """Reviewer surrogate-avoiding interleave: (filler, payload) units."""
    out = bytearray()
    j = 0
    for p in payload:
        if 0xD8 <= p <= 0xDF:
            out += bytes([p, 0x4E])
        else:
            out += bytes([filler[j % len(filler)], p])
            j += 1
    return bytes(out)


def _parity_interleave(payload: bytes, filler: bytes, order: str) -> bytes:
    out = bytearray()
    j = 0
    for p in payload:
        f = filler[j % len(filler)]
        j += 1
        out += bytes([f, p] if order == "fp" else [p, f])
    return bytes(out)


def _f1_payloads() -> dict[str, bytes]:
    try:
        elf = open("/bin/ls", "rb").read(30_000)
    except OSError:
        elf = bytes(range(256)) * 120
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello reviewer\n" * 20)
    return {
        "elf": elf,
        "int32": _int32_array(2048),
        "tecplot": _tecplot_binary(800),
        "zip": buf.getvalue(),
        "uint16": _uint16_band(2000, 20000),
    }


def test_f1_multi_byte_filler_smuggle_closed() -> None:
    """I-1: rejected P interleaved with any printable F stays rejected."""
    payloads = _f1_payloads()
    fillers = [bytes(range(0x61, 0x61 + n)) for n in range(1, 9)]  # a..abcdefgh
    total = accepted = 0
    for name, payload in payloads.items():
        for filler in fillers:
            for order in ("fp", "pf"):
                total += 1
                raw = _parity_interleave(payload, filler, order)
                try:
                    extract_text(f"evil_{name}.dat", raw, "application/octet-stream")
                    accepted += 1
                except ParseError as exc:
                    assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
            total += 1
            try:
                extract_text(
                    f"evil_{name}.dat",
                    _smuggle(payload, filler),
                    "application/octet-stream",
                )
                accepted += 1
            except ParseError as exc:
                assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert accepted == 0, f"F1 leaks: {accepted}/{total} accepted"
    assert total == len(payloads) * len(fillers) * 3


def test_f1_mutation_parity_smuggle_check_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverse mutation: parity-smuggle alone holds a low-NUL one-sided weave.

    High-NUL ELF weaves are refused by the BOM-less high-NUL path before
    soft floors; this pin uses ``x``⊗DEL so the smuggle gate is what
    blocks the clean UTF-8 accept after other gates are neutered.
    """
    raw = b"".join(bytes([ord("x"), 0x7F]) for _ in range(2000))
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", raw, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    monkeypatch.setattr(parser_registry, "_is_text_like", lambda _t: True)
    monkeypatch.setattr(parser_registry, "_control_ratio", lambda _t: 0.0)
    monkeypatch.setattr(
        parser_registry, "_soft_residual_controls_reject", lambda _t: False,
    )
    monkeypatch.setattr(parser_registry, "_readable_ratio", lambda _t: 1.0)
    # Smuggle still on → clean path refuses (falls to low-NUL generic).
    with pytest.raises(ParseError) as exc_info:
        extract_text("evil.txt", raw, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"

    monkeypatch.setattr(
        parser_registry, "_parity_lanes_show_filler_smuggle", lambda _r: False,
    )
    text, _metadata, _images = extract_text("evil.txt", raw, "text/plain")
    assert len(text) > 100


def test_f2_utf16_prose_plus_uint16_band_rejected() -> None:
    """I-2 conservative: whole file rejected; band scalars must not bill."""
    prose = (
        "The pressure vessel analysis shows nominal results for case.\n" * 100
    )[:2206]
    p_raw = prose.encode("utf-16-le")
    band = struct.pack(
        "<" + "H" * 4096, *([0x4E00 + (i % 100) for i in range(4096)]),
    )
    raw = p_raw + band
    with pytest.raises(ParseError) as exc_info:
        extract_text("mixed.txt", raw, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    # BOM + band with ASCII breakers every 255 scalars must not dilute I-2.
    units = []
    for i in range(4096):
        units.append(0x4E00 + (i % 100))
        if (i + 1) % 255 == 0:
            units.append(ord("."))
    bom_diluted = b"\xff\xfe" + struct.pack("<" + "H" * len(units), *units)
    with pytest.raises(ParseError) as exc_info:
        extract_text("bom_band.txt", bom_diluted, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    # BOM-less prose / roster refused; BOM pure CJK with punctuation still ok.
    with pytest.raises(ParseError) as exc_info:
        extract_text("prose.txt", p_raw, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    roster = (("id,name,score\n" * 5) + ("姓名,王小明,A01\n" * 10)).encode("utf-16-le")
    with pytest.raises(ParseError) as exc_info:
        extract_text("roster.txt", roster, "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    # Chinese-dominant UTF-16 is refused by the contract (not by a guess
    # about whether it "reads as prose") — and says so.
    zh = ("這是一段有標點的中文內容，用來確認 BOM 路徑。\n" * 40)
    with pytest.raises(ParseError) as exc_info:
        extract_text("zh.txt", zh.encode("utf-16"), "text/plain")
    assert "非 ASCII" in (exc_info.value.user_message or "")
    assert "另存" in (exc_info.value.user_message or "")
    # BOM'd ASCII prose still ok (content checks, not encoding guess).
    text, _, _ = extract_text(
        "prose_bom.txt", b"\xff\xfe" + p_raw, "text/plain",
    )
    assert "pressure vessel" in text


def test_f3_one_stray_c0_in_large_listing_accepted() -> None:
    """I-3: residual-control is a density, not a zero."""
    listing = ("      SUBROUTINE FOO(X)\n      X = X + 1.0\n" * 2400)[:96000]
    assert len(listing) == 96000
    base = listing.encode("ascii")
    for b in (0x1A, 0x07, 0x1F, 0x01, 0x00, 0x7F):
        text, _, _ = extract_text("deck.txt", base + bytes([b]), "text/plain")
        assert "SUBROUTINE FOO" in text
    # ≥5% C0 stays unsupported.
    heavy = bytearray(base)
    for i in range(0, len(heavy), 20):
        heavy[i] = 0x01
    assert sum(1 for x in heavy if x == 0x01) / len(heavy) >= 0.05
    with pytest.raises(ParseError) as exc_info:
        extract_text("deck.txt", bytes(heavy), "text/plain")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


def test_f4_visible_symbols_and_spaces_readable() -> None:
    """I-4: So / Zs render visibly and count as readable."""
    box = (
        "┌──────────┬──────────┐\n"
        "│  CSP     │  Router  │\n"
        "├──────────┼──────────┤\n"
        "│  Redis   │  Postgres│\n"
        "└──────────┴──────────┘\n"
    )
    text, _, _ = extract_text("arch.txt", box.encode("utf-8"), "text/plain")
    assert text == box
    checklist = "工作項目：\n" + ("● 完成 ★ 審查 ✓ 通過\n" * 30)
    text, _, _ = extract_text("todo.txt", checklist.encode("utf-8"), "text/plain")
    assert "●" in text and "✓" in text
    indented = ("　" * 4 + "這是全形空白縮排的中文段落內容。\n") * 40
    assert indented.count("\u3000") / len(indented) >= 0.15
    text, _, _ = extract_text("note.txt", indented.encode("utf-8"), "text/plain")
    assert text == indented
    for label, blob in (
        ("del", b"\x7f" * 4000),
        ("nel", "\x85".encode("utf-8") * 4000),
        ("zwsp", "\u200b".encode("utf-8") * 4000),
        ("iaa", "\ufff9".encode("utf-8") * 2000),
    ):
        with pytest.raises(ParseError) as exc_info:
            extract_text(f"{label}.txt", blob, "text/plain")
        assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


# ── Round-12: I-1'/I-2'/I-3'/I-4' ──────────────────────────────────────────


def _utf16_variants(text: str) -> dict[str, bytes]:
    return {
        "le_bom": b"\xff\xfe" + text.encode("utf-16-le"),
        "be_bom": b"\xfe\xff" + text.encode("utf-16-be"),
        "le": text.encode("utf-16-le"),
        "be": text.encode("utf-16-be"),
    }


@pytest.mark.parametrize(
    "label,sample",
    [
        ("han255", "中" * 255),
        ("han256", "中" * 256),
        ("han1000", "中" * 1000),
        ("table", "\n".join("測" * 12 for _ in range(60))),
        ("ja", "これはテストの文章です " * 50),
        ("ko", "이것은테스트문장입니다 " * 50),
        ("ru", "Привет мир. " * 40),
        ("ps", "Get-Item 檔案清單報表 " * 40),
        (
            "py",
            "# coding: utf-8\n"
            + ("x = 1  # comment\n" * 200)
            + "# "
            + ("注释文字" * 100)
            + "\n",
        ),
    ],
)
def test_f2_utf16_scripts_follow_ascii_dominance(
    label: str, sample: str,
) -> None:
    """Contract: BOM'd UTF-16 accepts iff the decoded text is ASCII-dominant.

    Expectation is COMPUTED from the sample, not hard-coded per label, so
    the test states the contract rather than a table of remembered
    verdicts. Punctuation, script and vocabulary spread are all irrelevant
    to the outcome — that is the point of the round-14 rewrite.

    BOM-less is never guessed: some shapes (e.g. ``中`` → ``-N``) are clean
    UTF-8 under T1 and must keep the UTF-8 byte interpretation; the rest
    refuse with re-save-as-UTF-8 guidance.
    """
    ascii_dominant = sum(1 for c in sample if ord(c) < 0x80) * 2 > len(sample)
    for tag, raw in _utf16_variants(sample).items():
        if tag in {"le_bom", "be_bom"}:
            if ascii_dominant:
                text, _, _ = extract_text(f"{label}_{tag}.txt", raw, "text/plain")
                assert text == sample, f"{label}/{tag} not byte-exact"
            else:
                with pytest.raises(ParseError) as exc_info:
                    extract_text(f"{label}_{tag}.txt", raw, "text/plain")
                assert "非 ASCII" in (exc_info.value.user_message or ""), (
                    f"{label}/{tag} must name the ASCII-dominance contract"
                )
                assert "另存" in (exc_info.value.user_message or "")
            continue
        try:
            text, _, _ = extract_text(f"{label}_{tag}.txt", raw, "text/plain")
        except ParseError as exc:
            assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
            assert "UTF-8" in (exc.user_message or "")
            assert "另存" in (exc.user_message or ""), f"{label}/{tag}"
            continue
        # T1: clean UTF-8 view wins — must not equal the UTF-16 sample.
        assert text != sample, f"{label}/{tag} must not silently UTF-16-decode"
        assert "\x00" not in text
        assert text == parser_registry._strip_nuls(
            raw.decode("utf-8", errors="strict")
        ), f"{label}/{tag} must be the UTF-8 byte interpretation"


def test_f2_utf8_mono_han_still_ok() -> None:
    sample = "中" * 1000
    text, _, _ = extract_text("han.txt", sample.encode("utf-8"), "text/plain")
    assert text == sample


def test_f1_diluted_filler_alphabet_closed() -> None:
    """I-1': Latin-1 / CJK dilution of the filler lane must not reopen smuggle."""
    import random as _rnd

    try:
        elf = open("/bin/ls", "rb").read()
    except OSError:
        elf = bytes(range(256)) * 500
    offsets = [12849, 94336, 128021, 133825, 135689]
    payloads: dict[str, bytes] = {}
    for off in offsets:
        chunk = elf[off : off + 4000]
        if len(chunk) >= 500:
            payloads[f"elf_{off}"] = chunk
    payloads["scrub20k"] = bytes(
        b if not (0xD8 <= b <= 0xDF) else 0xD7 for b in elf[:20000]
    )
    payloads["int32"] = _int32_array(2048)
    payloads["uint16"] = _uint16_band(2000, 20000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello reviewer\n" * 200)
    payloads["zip"] = buf.getvalue()

    latin = [0xA1, 0xAB, 0xBB, 0xA0]
    accepted = total = 0
    for _name, payload in payloads.items():
        for alen in (1, 3, 8, 16, 32):
            for frac_i in range(0, 51, 5):
                frac = frac_i / 100.0
                for phase in (0, 1):
                    for alphabet in (
                        list(range(0x41, 0x41 + alen)),
                        (latin * ((alen + 3) // 4))[:alen],
                        ([0x4E, 0x6C, 0x80, 0x9F, 0x5B] * 8)[:alen],
                    ):
                        total += 1
                        _rnd.seed(total)
                        fs = [
                            (
                                _rnd.choice(alphabet)
                                if _rnd.random() < frac
                                else alphabet[i % len(alphabet)]
                            )
                            for i in range(len(payload))
                        ]
                        raw = bytearray()
                        for i, b in enumerate(payload):
                            if phase == 0:
                                raw.append(fs[i])
                                raw.append(b)
                            else:
                                raw.append(b)
                                raw.append(fs[i])
                        try:
                            extract_text(
                                "evil.dat", bytes(raw), "application/octet-stream",
                            )
                            accepted += 1
                        except ParseError as exc:
                            assert exc.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert accepted == 0, f"F1 dilution leaks: {accepted}/{total}"
    assert total > 1000


def test_f3_scattered_strays_scale_with_length() -> None:
    """I-4': 10 MB listing survives many uniform strays; clustering still rejects ZIP."""
    listing = ("      SUBROUTINE FOO(X)\n      X = X + 1.0\n" * 300000)
    base = listing[:10_000_000].encode("ascii")
    assert len(base) == 10_000_000
    for nstray in (3, 10, 100):
        body = bytearray(base)
        step = len(body) // nstray
        for i in range(nstray):
            body[i * step] = 0x1A
        text, _, _ = extract_text("deck.txt", bytes(body), "text/plain")
        assert "SUBROUTINE FOO" in text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello reviewer\n" * 200)
    with pytest.raises(ParseError) as exc_info:
        extract_text("a.zip", buf.getvalue(), "application/zip")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


@pytest.mark.parametrize("n", [1, 50, 150, 199])
def test_f4_replacement_char_never_counts_as_readable(n: int) -> None:
    """I-3': U+FFFD is failed decoding, not readable evidence."""
    with pytest.raises(ParseError) as exc_info:
        extract_text("bin.dat", b"\xff" * n, "application/octet-stream")
    assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


def test_f2_bomless_cjk_fraction_monotonic() -> None:
    """BOM-less UTF-16 CJK fraction sweep: every point refused (no guessing)."""
    import random as _rnd

    pattern: list[str] = []
    for pct in range(0, 51):
        n = 400
        n_cjk = int(n * pct / 100)
        chars = ["中"] * n_cjk + list(("Hello world. " * 40)[: n - n_cjk])
        _rnd.seed(pct)
        _rnd.shuffle(chars)
        sample = "".join(chars)
        with pytest.raises(ParseError) as exc_info:
            extract_text(
                "mix.txt", sample.encode("utf-16-le"), "text/plain",
            )
        assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
        assert "UTF-8" in (exc_info.value.user_message or "")
        pattern.append("R")
    assert pattern == ["R"] * 51
    # BOM'd counterparts follow the ASCII-dominance contract: the boundary
    # is a strict majority of ASCII scalars and nothing else.
    for pct, expect_ok in ((0, True), (49, True), (50, False), (60, False)):
        n = 400
        n_cjk = int(n * pct / 100)
        chars = ["中"] * n_cjk + list(("Hello world. " * 40)[: n - n_cjk])
        _rnd.seed(pct)
        _rnd.shuffle(chars)
        sample = "".join(chars)
        raw = b"\xff\xfe" + sample.encode("utf-16-le")
        if expect_ok:
            text, _, _ = extract_text("mix.txt", raw, "text/plain")
            assert text == sample
        else:
            with pytest.raises(ParseError) as exc_info:
                extract_text("mix.txt", raw, "text/plain")
            assert "非 ASCII" in (exc_info.value.user_message or "")

# ── Round-14: the ASCII-dominance support contract ─────────────────────────
#
# One sentence: UTF-16 is supported for ASCII-dominant documents; content
# that is predominantly non-ASCII must be saved as UTF-8.
#
# These tests exist to stop the next round from "fixing" the refusal of
# Chinese-dominant UTF-16 by adding a predicate that judges whether decoded
# non-ASCII text is real. That family (vocabulary spread, punctuation quota,
# Unicode-band shape, prose structure) failed six consecutive rounds — every
# version either billed a binary or refused a real document.


def _bom_pair_verdicts(body: bytes) -> tuple[bool, bool]:
    """(accepted_without_bom, accepted_with_bom) for one body."""
    out = []
    for raw in (body, b"\xff\xfe" + body):
        try:
            parser_registry._decode_text_bytes(raw)
            out.append(True)
        except ParseError:
            out.append(False)
    return out[0], out[1]


def test_r14_i2_bom_privilege_is_exactly_utf16_ascii() -> None:
    """I2, exhaustively: which constant fills does a BOM turn into text?

    All 65 536 uint16 constant fills. Acceptance with a BOM must coincide
    exactly with "the fill IS an ASCII scalar" — i.e. the body is
    byte-identical to the UTF-16LE encoding of an ASCII document. No
    instrument band, no CJK-looking fill, nothing above U+007F may pass.
    """
    accepted = []
    for v in range(0x10000):
        body = struct.pack("<" + "H" * 300, *([v] * 300))
        try:
            text = parser_registry._decode_text_bytes(b"\xff\xfe" + body)
        except ParseError:
            continue
        accepted.append(v)
        assert all(ord(c) < 0x80 for c in text), f"non-ASCII accepted: {v:#06x}"
    assert accepted, "sanity: ASCII fills must still work"
    assert max(accepted) < 0x80, (
        f"BOM privileged a non-ASCII fill: {[hex(v) for v in accepted if v >= 0x80]}"
    )
    assert len(accepted) == 98, f"expected the 98 text-forming ASCII scalars, got {len(accepted)}"


def _accepts(raw: bytes) -> bool:
    try:
        parser_registry._decode_text_bytes(raw)
        return True
    except ParseError:
        return False


# Round-16 F1. What stood here was a permit wearing a pin's name: it was
# called ``..._bom_does_not_rescue_binaries`` while its own assertion
# allowed the residual to grow from 18 flips to 58 on this host, and its
# docstring's population did not match the 14 files / 5 853 slices it
# really swept. Acceptance review refuted it; this is the rewrite.
#
# The property under test: prepending a byte-order mark to bytes that were
# refused must not turn them into an accept. It is split three ways by how
# strong the guarantee can honestly be.
#
#   1. Hard zero, any host — whole binaries, and every 4 KiB slice of a
#      system binary under 1 MiB.
#   2. Hard zero, any host, deterministic — a generated corpus (PNG / ZIP /
#      int32 / uint16 bands / seeded urandom / ASCII weave) that does not
#      depend on which distribution is installed.
#   3. The RECORD — the large glibc/CPython files carry UTF-32 locale and
#      transliteration tables that read as UTF-16 once the high halves are
#      stripped as NUL, so the residual is not zero. Exploiting it needs a
#      deliberate slice at a specific offset PLUS a BOM the file does not
#      contain, so it is recorded rather than wished away. Recorded means
#      the exact set: file by file, offset by offset, byte order by byte
#      order. One extra flip on the same file is red.
#
# Measured 2026-08-02 on this host: 14 files, 5 853 slices, 18 flips.
_F3_STRIDE = 4093          # prime, so slices are not header-aligned
_F3_SLICE = 4096
_F3_LARGE_FILE_BYTES = 1_000_000
_F3_SYSTEM_BINARIES = (
    "/usr/bin/python3",
    "/lib/x86_64-linux-gnu/libc.so.6",
    "/usr/lib/locale/locale-archive",
    "/lib/x86_64-linux-gnu/libcrypto.so.3",
    "/usr/lib/x86_64-linux-gnu/libstdc++.so.6",
    "/lib/x86_64-linux-gnu/libm.so.6",
    "/lib/x86_64-linux-gnu/libz.so.1",
    "/bin/bash", "/bin/ls", "/bin/cat", "/bin/date", "/bin/grep",
    "/bin/dircolors", "/bin/sed",
)
# basename -> (exact byte size when recorded, {(slice offset, bom order)}).
# The size is part of the key: a rebuilt libc is a different population and
# must not be silently checked against another build's offsets.
_F3_RECORDED_FLIPS: dict[str, tuple[int, frozenset[tuple[int, str]]]] = {
    "python3": (5917224, frozenset({(3990675, "be")})),
    "libc.so.6": (2220400, frozenset({
        (1833664, "le"), (1837757, "be"), (1841850, "le"),
        (1845943, "be"), (1850036, "le"),
    })),
    "locale-archive": (6075312, frozenset({
        (253766, "le"), (261952, "le"), (270138, "le"), (286510, "le"),
        (306975, "be"), (311068, "le"), (5861176, "le"), (5877548, "le"),
        (5885734, "le"), (5893920, "le"), (5902106, "le"), (5918478, "le"),
    })),
    "libcrypto.so.3": (4455728, frozenset()),
    "libstdc++.so.6": (2260296, frozenset()),
    "libm.so.6": (940560, frozenset()),
    "libz.so.1": (108936, frozenset()),
    "bash": (1396520, frozenset()),
    "ls": (138216, frozenset()),
    "cat": (35288, frozenset()),
    "date": (104968, frozenset()),
    "grep": (182728, frozenset()),
    "dircolors": (39440, frozenset()),
    "sed": (113224, frozenset()),
}


def _bom_flip_offsets(blob: bytes) -> set[tuple[int, str]]:
    """Slices of ``blob`` that a prepended BOM turns from refuse to accept."""
    flips: set[tuple[int, str]] = set()
    off = 0
    while off + _F3_SLICE <= len(blob):
        chunk = blob[off:off + _F3_SLICE]
        if not _accepts(chunk):
            for tag, bom in (("le", b"\xff\xfe"), ("be", b"\xfe\xff")):
                if _accepts(bom + chunk):
                    flips.add((off, tag))
        off += _F3_STRIDE
    return flips


def _f3_generated_corpus() -> dict[str, bytes]:
    """Binaries this test builds itself, so the zero does not need a host."""
    import random

    rnd = random.Random(20260802)
    return {
        "png": _png_blob() * 20,
        "zip": _zip_stored(b"hellohellohello\n" * 30_000),
        "int32": _int32_array(60_000),
        "uint16-20000": _uint16_band(120_000, 20000),
        "uint16-32768": _uint16_band(120_000, 32768),
        "urandom": bytes(rnd.randrange(256) for _ in range(240_000)),
        "ascii-weave": bytes(
            b for p in bytes(range(32, 127)) * 2000 for b in (ord("a"), p)
        ),
    }


def test_bom_prefix_accepts_nothing_in_the_generated_binary_corpus() -> None:
    """Hard zero on a corpus that is identical on every host.

    Seven generated binaries, whole and sliced at an unaligned stride, both
    byte orders. Measured 2026-08-02: 443 slices, 0 flips. This one carries
    no host caveat — if it ever goes red, a gate stopped firing.
    """
    slices = 0
    flips: list[tuple[str, int, str]] = []
    for name, blob in _f3_generated_corpus().items():
        for tag, bom in (("plain", b""), ("le", b"\xff\xfe"),
                         ("be", b"\xfe\xff")):
            assert not _accepts(bom + blob), f"whole binary accepted: {name} {tag}"
        slices += max(0, (len(blob) - _F3_SLICE) // _F3_STRIDE + 1)
        flips += [(name, off, tag) for off, tag in _bom_flip_offsets(blob)]
    assert slices >= 400, f"corpus shrank: {slices} slices"
    assert flips == [], f"BOM turned a generated binary slice into text: {flips}"


def test_bom_prefix_accepts_no_whole_binary_and_no_small_binary_slice() -> None:
    """Hard zero over the system binaries where it can be a hard zero.

    Covers: every candidate that exists on this host, whole, in all three
    forms (plain / LE BOM / BE BOM); plus every 4 KiB slice, at stride
    4093, of each of those files under 1 MiB. It does NOT cover slices of
    the large glibc/CPython files — those carry a non-zero residual and are
    pinned by name and offset in the test below, not asserted to be zero.
    """
    import pathlib

    paths = [pathlib.Path(p) for p in _F3_SYSTEM_BINARIES]
    paths = [p for p in paths if p.is_file()]
    assert paths, "no system binaries available on this host"

    small_slices = 0
    for path in paths:
        blob = path.read_bytes()
        for tag, bom in (("plain", b""), ("le", b"\xff\xfe"),
                         ("be", b"\xfe\xff")):
            assert not _accepts(bom + blob), f"whole binary accepted: {path} {tag}"
        if len(blob) >= _F3_LARGE_FILE_BYTES:
            continue
        small_slices += max(0, (len(blob) - _F3_SLICE) // _F3_STRIDE + 1)
        flips = _bom_flip_offsets(blob)
        assert not flips, f"BOM rescued a slice of {path}: {sorted(flips)[:10]}"
    assert small_slices > 300, f"too few small-binary slices: {small_slices}"


def test_bom_flip_residual_equals_the_recorded_set_file_by_file() -> None:
    """The residual is a record, not a budget: growth by one flip is red.

    For every recorded binary still present at its recorded byte size, the
    set of (offset, byte order) flips must equal the recorded set exactly.
    There is no headroom and no ratio — a file that gains a flip fails, and
    so does a file that gains one and loses another.

    Files that are absent or rebuilt are skipped individually and named in
    the failure message, so a different distribution relaxes the pin for
    that file alone; if none of the fourteen still matches, the record no
    longer describes this host and the test says so rather than passing.
    """
    import pathlib

    checked: list[str] = []
    unmatched: list[str] = []
    for spec in _F3_SYSTEM_BINARIES:
        path = pathlib.Path(spec)
        record = _F3_RECORDED_FLIPS.get(path.name)
        if record is None or not path.is_file():
            unmatched.append(f"{path.name}: absent")
            continue
        size, expected = record
        if path.stat().st_size != size:
            unmatched.append(
                f"{path.name}: {path.stat().st_size} bytes, recorded {size}"
            )
            continue
        blob = path.read_bytes()
        observed = _bom_flip_offsets(blob)
        assert observed == set(expected), (
            f"{path.name}: BOM-flip set changed. "
            f"new {sorted(observed - set(expected))[:10]}, "
            f"gone {sorted(set(expected) - observed)[:10]}"
        )
        # A flip may never come from a slice that already starts with a BOM:
        # the BOM is always something an attacker prepends by hand.
        for off, _tag in observed:
            assert blob[off:off + 2] not in (b"\xff\xfe", b"\xfe\xff"), (
                path.name, off
            )
        checked.append(path.name)

    assert checked, (
        "no recorded binary matched this host, so the residual record was "
        f"not exercised at all: {unmatched}"
    )
    # 18 flips over 14 files / 5 853 slices, recorded 2026-08-02.
    total = sum(len(_F3_RECORDED_FLIPS[name][1]) for name in checked)
    assert total <= 18, f"recorded set itself grew: {total} flips in {checked}"


def test_r14_natural_bom_prefixed_slices_never_accept() -> None:
    """A2: slices that really begin FF FE / FE FF inside binaries."""
    import pathlib

    found = accepted = 0
    for p in ("/bin/ls", "/bin/dircolors", "/bin/bash"):
        path = pathlib.Path(p)
        if not path.is_file():
            continue
        blob = path.read_bytes()
        for i in range(len(blob) - 1):
            pair = blob[i:i + 2]
            if pair not in (b"\xff\xfe", b"\xfe\xff"):
                continue
            chunk = blob[i:i + 4000]
            if len(chunk) < 4000:
                continue
            found += 1
            try:
                parser_registry._decode_text_bytes(chunk)
                accepted += 1
            except ParseError:
                pass
    assert found > 100, f"expected many natural BOMs in system binaries, got {found}"
    assert accepted == 0, f"{accepted}/{found} natural-BOM slices accepted"


@pytest.mark.parametrize("n", [150, 400, 900, 1500])
def test_r14_contract_boundary_is_a_strict_ascii_majority(n: int) -> None:
    """A4: measured boundary — accept iff ASCII scalars are a strict majority.

    The boundary does not move with length, script or punctuation. 50/50 is
    refused because "dominant" means more than half, not "close enough".
    """
    import random as _rnd

    def _mix(pct: int) -> str:
        n_ascii = round(n * pct / 100)
        chars = list(("Requirement note. " * 4000)[:n_ascii]) + ["測"] * (n - n_ascii)
        _rnd.seed(n * 100 + pct)
        _rnd.shuffle(chars)
        return "".join(chars)

    for pct in (0, 20, 49, 50, 51, 80, 100):
        sample = _mix(pct)
        ascii_frac = sum(1 for c in sample if ord(c) < 0x80) / len(sample)
        raw = b"\xff\xfe" + sample.encode("utf-16-le")
        if ascii_frac > 0.5:
            text, _, _ = extract_text("mix.txt", raw, "text/plain")
            assert text == sample, f"n={n} pct={pct} not byte-exact"
        else:
            with pytest.raises(ParseError) as exc_info:
                extract_text("mix.txt", raw, "text/plain")
            assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"


def test_r14_refusal_reasons_are_distinguishable() -> None:
    """I4: the advice must match the cause, and leak nothing.

    Before round 14 every high-NUL body — all 21 whole system binaries
    included — was told "this looks like UTF-16, re-save as UTF-8", which
    is useless advice for an ELF.
    """
    import pathlib

    cases: dict[str, bytes] = {
        "png": _png_blob(),
        "big5": ("姓名,單位,代號\n王小明,資訊室,A01\n" * 40).encode("big5"),
        "utf16_cjk": _repo_han_prose(400).encode("utf-16"),
        "utf16_bomless_ascii": "Get-ChildItem\r\nWrite-Host ok\r\n".encode("utf-16-le"),
        "int32": _int32_array(4000),
    }
    elf = pathlib.Path("/bin/ls")
    if elf.is_file():
        cases["elf"] = elf.read_bytes()

    messages: dict[str, str] = {}
    for label, blob in cases.items():
        with pytest.raises(ParseError) as exc_info:
            extract_text(f"{label}.txt", blob, "application/octet-stream")
        msg = exc_info.value.user_message or ""
        messages[label] = msg
        # No filesystem paths, parser internals or NULs may reach the user.
        assert "/" not in msg, f"{label}: path-like text in message: {msg}"
        assert "\x00" not in msg
        for leak in ("parser", "_decode", "Traceback", "ratio", "utf-16-le",
                     "Supported:"):
            assert leak not in msg, f"{label}: internals leaked: {msg}"

    # Not text at all → must NOT advise re-saving.
    for label in ("png", "int32", *(["elf"] if "elf" in messages else [])):
        assert "不是可讀的文字檔" in messages[label], (
            f"{label} must be told it is not text: {messages[label]}"
        )
        assert "另存" not in messages[label], (
            f"{label} must not be told to re-save: {messages[label]}"
        )
    # Plausible text readings → actionable, and distinct from each other.
    assert "Big5" in messages["big5"] and "另存" in messages["big5"]
    assert "非 ASCII" in messages["utf16_cjk"] and "另存" in messages["utf16_cjk"]
    assert "BOM" in messages["utf16_bomless_ascii"]
    assert "另存" in messages["utf16_bomless_ascii"]
    assert len({messages[k] for k in ("big5", "utf16_cjk",
                                      "utf16_bomless_ascii", "png")}) == 4


# Round-15 F4(a): the round-14 version of this generator emitted every
# 3-byte script space-free and every 2-byte script with spaces, so the one
# shape that actually failed — a space-free two-byte script, whose UTF-8
# lead/trail parity split looks exactly like a tiny filler alphabet — was
# never generated, and neither was any combining-mark script. Units below
# are all space-free; ``spacing`` re-introduces spaces and newlines, so
# every script is exercised in every shape and the docstring is true.
_R15_SCRIPT_UNITS = {
    "zh": "本院內部網路平台需求說明文件內容摘要記錄",
    "zh-punct": "本院內部網路平台，需求說明。文件內容摘要；記錄。",
    "ja": "これはテストの文章です内部ネットワーク資料",
    "ko": "이것은테스트문장입니다내부망자료기록",
    # 2-byte scripts, space-free: ue == 2 on the lead lane, uo >= 24 on the
    # trail lane — the parity shape that was refused from 64 bytes up.
    "ru": "Приветмирвнутренняясетьотчетабвгдежзийклмнопрстуфхцчшщъыьэюя",
    "el": "Γειασουκόσμεεσωτερικόδίκτυοέγγραφοαβγδεζηθικλμνξοπρστυφχψω",
    "ar": "مرحبابالعالمالشبكةالداخليةوثيقةتقرير",
    "he": "שלוםעולםזהומסמךפנימישלהרשת",
    # Combining-mark scripts (F2): Mn/Mc/Me are 18-41% of these bodies.
    "ar-harakat": "مَرْحَبًابِالْعَالَمِهَذِهِوَثِيقَةٌدَاخِلِيَّةٌ",
    "he-niqqud": "שָׁלוֹםעוֹלָםזֶהוּמִסְמָךְפְּנִימִי",
    "th": "ภาษาไทยเป็นภาษาราชการของประเทศไทยและใช้อักษรไทย",
    "deva": "यहएकआंतरिकनेटवर्कदस्तावेज़हैजिसमेंविवरणदियागयाहै",
    "vi-nfd": unicodedata.normalize("NFD", "Đâylàtàiliệunộibộcủamạnglưới"),
}


def _r15_shape(unit: str, spacing: str) -> str:
    """``unit`` as-is, broken by spaces, or broken by newlines."""
    if spacing == "bare":
        return unit
    sep, step = (" ", 8) if spacing == "spaced" else ("\n", 12)
    return sep.join(unit[i:i + step] for i in range(0, len(unit), step))


def test_r15_i1_generator_covers_the_shapes_that_failed() -> None:
    """Meta-pin: the generator really emits the two missed shapes.

    Round 14's docstring claimed coverage its parameters did not provide.
    This test fails if a future edit narrows the corpus back.
    """
    space_free_two_byte = set()
    combining = set()
    for label, unit in _R15_SCRIPT_UNITS.items():
        for spacing in ("bare", "spaced", "lines"):
            sample = _r15_shape(unit, spacing)
            if spacing == "bare" and all(0x80 <= ord(c) < 0x800 for c in sample):
                space_free_two_byte.add(label)
            if any(unicodedata.category(c) in ("Mn", "Mc", "Me")
                   for c in sample):
                combining.add(label)
    assert space_free_two_byte >= {"ru", "el", "ar", "he"}, space_free_two_byte
    assert combining >= {"ar-harakat", "he-niqqud", "th", "deva", "vi-nfd"}, (
        combining
    )
    # 3-byte scripts must still be present in the space-free shape too.
    for label in ("zh", "ja", "ko"):
        bare = _r15_shape(_R15_SCRIPT_UNITS[label], "bare")
        assert " " not in bare and "\n" not in bare, label


# Round 16. Round 15 asserted that every entry above accepts in every
# shape at every length, and bought that with two exemptions that
# acceptance review refuted; the owner ruled removal over narrowing
# (2026-08-02). This is the envelope that remains, MEASURED over all
# 13 x 3 x 4 = 156 cases on 2026-08-02 — 48 refused, 108 byte-exact.
#
# Two causes, neither of which knows what a script is:
#   * parity lanes — a whitespace-free run of >= 24 distinct two-byte
#     letters is a tiny lead alphabet against a rich trail alphabet
#     (``ru``/``el`` bare; ``ar``/``he`` bare stay under 24 distinct and
#     accept, which is the proof the rule is alphabet size, not script);
#   * readability floor — bodies that are 30-46% combining marks
#     (``ar-harakat``, ``he-niqqud``, ``deva``, ``vi-nfd``) measure
#     0.54-0.70 readable and fall under the 0.85 floor. ``th`` measures
#     0.94 and keeps working at every length and shape.
_R16_REFUSED_SHAPES = frozenset(
    (label, spacing, reps)
    for label, cases in {
        "ru": [("bare", r) for r in (1, 3, 20, 200)],
        "el": [("bare", r) for r in (1, 3, 20, 200)],
        "ar-harakat": ([("bare", r) for r in (3, 20, 200)]
                       + [(s, r) for s in ("spaced", "lines")
                          for r in (1, 3, 20, 200)]),
        "deva": ([("bare", r) for r in (3, 20, 200)]
                 + [(s, r) for s in ("spaced", "lines")
                    for r in (1, 3, 20, 200)]),
        "he-niqqud": [(s, r) for s in ("bare", "spaced", "lines")
                      for r in (3, 20, 200)],
        "vi-nfd": [(s, r) for s in ("bare", "spaced", "lines")
                   for r in (3, 20, 200)],
    }.items()
    for spacing, reps in cases
)


@pytest.mark.parametrize("label", sorted(_R15_SCRIPT_UNITS))
@pytest.mark.parametrize("spacing", ["bare", "spaced", "lines"])
@pytest.mark.parametrize("reps", [1, 3, 20, 200])
def test_r16_utf8_script_corpus_envelope_is_exactly_measured(
    label: str, spacing: str, reps: int,
) -> None:
    """I3: every case is byte-exact, or refused by NAME of the limit.

    The refused set is pinned exactly, so widening it (a future gate that
    starts eating Chinese) and narrowing it (a new script exemption of the
    kind that was just removed) both turn this red. Nothing is ever
    silently mangled, and nothing that decodes is called "not a text file".
    """
    sample = _r15_shape(_R15_SCRIPT_UNITS[label], spacing) * reps
    raw = sample.encode("utf-8")
    if (label, spacing, reps) in _R16_REFUSED_SHAPES:
        with pytest.raises(ParseError) as exc_info:
            extract_text(f"{label}.txt", raw, "text/plain")
        assert exc_info.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
        assert (exc_info.value.user_message
                == parser_registry._UNSUPPORTED_TEXT_CONTENT_MESSAGE), (
            f"{label}/{spacing}/{reps} refused with the wrong advice: "
            f"{exc_info.value.user_message}"
        )
    else:
        text, _metadata, _images = extract_text(
            f"{label}.txt", raw, "text/plain",
        )
        assert text == sample


# ── Round-15: F1 (script-mixing clause deleted) + F2 (combining marks) ──────
#
# F1 deleted the "2-6 distinct chars spanning hangul+cyrillic/other" clause
# from ``_is_text_like``. Measured with and without it over 8 712
# binary x filler x ratio x BOM cases (13 payloads, 21 filler alphabets
# including SPACE and NUL, six unequal ratios, both interleave orders):
# the accepted set was byte-identical. The clause carried none of the
# binary refusal — the parity / control / NUL gates do — while being the
# sole cause of refusing ordinary bullet lists, arrow maps and tables.
#
# F2 admits Mn / Mc / Me as readable. Arabic with harakat, Hebrew with
# niqqud, Devanagari, Thai and NFD Vietnamese are 18-41% combining marks,
# so excluding them dragged readable_ratio under the 0.85 floor and refused
# ordinary prose with the "not a text file" message.

_R15_ORDINARY_DOCUMENTS = {
    "bullet-list": "\n".join("• 項目" for _ in range(80)),
    "bullet-inline": "• 項目 " * 80,
    "tree-diagram": "├─ src\n│  ├─ app\n│  │  └─ main\n│  └─ lib\n└─ docs\n" * 12,
    "tree-bare": "├└│─" * 100,
    "arrow-map": "\n".join("A → B" for _ in range(60)),
    "arrow-inline": "A → B; " * 60,
    "checkbox-list": "\n".join(("☑ done" if i % 2 else "☐ todo")
                               for i in range(60)),
    "checkbox-bare": "☐☑ " * 80,
    "box-diagram": ("┌──────────┐\n│  ANILA   │\n├──────────┤\n"
                    "│  csp     │\n└──────────┘\n" * 10),
    "box-letters": "─│ box ─│ diagram " * 40,
    "degree-only": "25℃",
    "pipe-table": "\n".join("│1│2│3│4│5│" for _ in range(60)),
    "pipe-digits": "│1│2│3│" * 60,
    "math-note": "∑∫∂√≠≤≥±∞\n" * 40,
    "math-bare": "∑∫ " * 80,
}


@pytest.mark.parametrize("label", sorted(_R15_ORDINARY_DOCUMENTS))
def test_r15_f1_ordinary_symbol_documents_extract_byte_exact(label: str) -> None:
    """F1: bullet / arrow / box / tree / checkbox / ℃ / table / math docs.

    Every one of these was refused as 「不是可讀的文字檔」 by the deleted
    script-mixing clause. They are the ordinary output of any note-taking
    or diagramming tool and must round-trip byte-exact.
    """
    sample = _R15_ORDINARY_DOCUMENTS[label]
    text, _metadata, _images = extract_text(
        f"{label}.txt", sample.encode("utf-8"), "text/plain",
    )
    assert text == sample


def test_r16_i1_no_script_predicate_survives_under_any_name() -> None:
    """I1: no predicate may exempt a body on the basis of its script.

    ``_script_block_name`` (round 15's F1 deletion) mapped scalars to
    script names. ``_is_utf8_two_byte_script_body`` (round 16's removal)
    exempted a body from the parity gate when every non-ASCII sequence was
    two bytes wide, i.e. when it looked like a two-byte script. Neither may
    come back, here or under another name — the parity gate must decide
    from lane statistics alone and never from a decode.
    """
    for banned_attr in ("_script_block_name", "_is_utf8_two_byte_script_body"):
        assert not hasattr(parser_registry, banned_attr), banned_attr
    src = inspect.getsource(parser_registry._is_text_like)
    for banned in ("hangul", "cyrillic", "_script_block_name", "blocks"):
        assert banned not in src, f"{banned!r} reappeared in _is_text_like"
    parity_body = "\n".join(
        line for line in inspect.getsource(
            parser_registry._parity_lanes_show_filler_smuggle,
        ).split('"""')[-1].splitlines()
        if not line.lstrip().startswith("#")
    )
    for banned in ("decode(", "_is_utf8_two_byte_script_body", "unicodedata"):
        assert banned not in parity_body, (
            f"{banned!r} reappeared in the parity gate's executable body"
        )


def test_r16_i2_readable_ratio_has_no_positional_rule() -> None:
    """I2: one rule for combining marks, checked by behaviour not by eye.

    Round 15's rule looked backwards for a base, so the same multiset of
    scalars scored differently depending on their order — that is exactly
    how one readable scalar could carry an unbounded run of marks. The
    ratio must now depend only on WHICH scalars are present.
    """
    import random

    pool = list("Aa1中 ·│") * 6 + ["̀", "ְ", "॑"] * 6
    for seed in range(25):
        shuffled = pool[:]
        random.Random(seed).shuffle(shuffled)
        assert parser_registry._readable_ratio("".join(shuffled)) == (
            parser_registry._readable_ratio("".join(sorted(pool)))
        ), f"order changed the readable ratio (seed {seed})"

    # A readable base followed by 99 marks is 1/100 readable, not 100/100.
    assert parser_registry._readable_ratio("A" + "̀" * 99) == 0.01


_R15_COMBINING_PROSE = {
    # label: (unit, minimum combining-mark share the sample must really have)
    "thai": ("ภาษาไทยเป็นภาษาราชการของประเทศไทย และใช้อักษรไทยในการเขียนเอกสาร ",
             0.05),
    "hebrew-niqqud": ("שָׁלוֹם עוֹלָם זֶהוּ מִסְמָךְ פְּנִימִי שֶׁל הָרֶשֶׁת ", 0.30),
    "devanagari": ("यह एक आंतरिक नेटवर्क दस्तावेज़ है जिसमें विवरण दिया गया है ", 0.20),
    "arabic-harakat": ("مَرْحَبًا بِالْعَالَمِ هَذِهِ وَثِيقَةٌ دَاخِلِيَّةٌ ", 0.30),
}


@pytest.mark.parametrize("label", sorted(_R15_COMBINING_PROSE))
def test_r16_combining_mark_prose_outcome_follows_the_one_mark_rule(
    label: str,
) -> None:
    """I2: one rule — a mark never counts — and the outcome follows from it.

    Round 15 counted marks behind a readable base, which made all four of
    these accept and also let one readable scalar carry an unbounded run of
    marks. With the positional case removed, the outcome is decided by the
    0.85 readable floor alone: Thai measures 0.94 and keeps working, the
    three heavier scripts measure 0.54-0.62 and are refused BY NAME of the
    limit — never as "not a text file" (I3).
    """
    unit, min_share = _R15_COMBINING_PROSE[label]
    sample = (unit * (200 // len(unit) + 1))[:200]
    share = sum(
        1 for c in sample if unicodedata.category(c) in ("Mn", "Mc", "Me")
    ) / len(sample)
    assert share >= min_share, f"{label} fixture lost its marks: {share:.1%}"
    ratio = parser_registry._readable_ratio(sample)
    raw = sample.encode("utf-8")
    if ratio >= parser_registry._MIN_READABLE_RATIO:
        assert label == "thai", f"{label} unexpectedly clears the floor"
        text, _metadata, _images = extract_text(
            f"{label}.txt", raw, "text/plain",
        )
        assert text == sample
    else:
        with pytest.raises(ParseError) as exc_info:
            extract_text(f"{label}.txt", raw, "text/plain")
        assert (exc_info.value.user_message
                == parser_registry._UNSUPPORTED_TEXT_CONTENT_MESSAGE), (
            f"{label} ({ratio:.2f} readable) got the wrong advice: "
            f"{exc_info.value.user_message}"
        )


def test_r16_vietnamese_nfc_extracts_nfd_is_refused_by_name() -> None:
    """The NFC/NFD split is the honest cost of dropping the mark exemption.

    NFC Vietnamese carries no marks at all and is byte-exact. NFD is 23%
    Mn, measures 0.67 readable, and is refused — with the limit named, so
    the user can act (re-save NFC / write in Chinese or English) instead of
    being told a valid UTF-8 file is not text.
    """
    nfc = ("Đây là tài liệu nội bộ của mạng lưới trung tâm nghiên cứu " * 4)[:200]
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc

    text, _metadata, _images = extract_text(
        "vi-nfc.txt", nfc.encode("utf-8"), "text/plain",
    )
    assert text == nfc

    with pytest.raises(ParseError) as exc_info:
        extract_text("vi-nfd.txt", nfd.encode("utf-8"), "text/plain")
    assert (exc_info.value.user_message
            == parser_registry._UNSUPPORTED_TEXT_CONTENT_MESSAGE)


@pytest.mark.parametrize(
    "label,unit",
    [
        ("ru", "абвгдежзийклмнопрстуфхцчшщъыьэюяАБВГДЕЖЗИЙКЛМНОПРСТУФ"),
        ("el", "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"),
        ("ar", "ابتثجحخدذرزسشصضطظعغفقكلمنهوىءآأؤإئة"),
        ("he", "אבגדהוזחטיכלמנסעפצקרשתםןץףך"),
    ],
)
@pytest.mark.parametrize("length", [31, 32, 64, 200, 400])
def test_r16_space_free_two_byte_runs_stop_at_64_bytes(
    label: str, unit: str, length: int,
) -> None:
    """The cost of removing the exemption, pinned at its exact boundary.

    Round 15 made these accept at every length via
    ``_is_utf8_two_byte_script_body``; acceptance review showed the same
    predicate admitted machine-generated byte patterns, and the owner ruled
    removal (2026-08-02). Measured envelope: 31 characters (62 bytes) is
    under the 64-byte parity window and accepts; 32 characters (64 bytes)
    and up is refused — with the limit NAMED, not as "not a text file".

    ``ar``/``he`` in ``_R15_SCRIPT_UNITS`` still accept space-free at every
    length because their samples stay under 24 distinct trail bytes. The
    rule is alphabet size, not script.

    Correcting the round-15 record (F4): that round reported "one wider
    character declines, two accept", measured only at regularly spaced
    positions. Re-measured on this code 2026-08-02, over a 400-character
    Cyrillic run: ONE inserted three-byte character accepts 92/100, two
    ADJACENT decline 0/340, two scattered accept 77/85. The cause is byte
    parity — an odd-length insertion moves every following lead byte onto
    the other lane, so neither lane is a tiny alphabet any more. That makes
    the gate more permissive for real text, not less safe: the same
    perturbation over the whole adversarial matrix (4 914 weaves; +1 byte,
    +3 bytes, +1 byte mid-body) accepted 0.
    """
    sample = (unit * (length // len(unit) + 1))[:length]
    raw = sample.encode("utf-8")
    assert all(0x80 <= ord(c) < 0x800 for c in sample)
    if len(raw) < 64:
        text, _metadata, _images = extract_text(
            f"{label}.txt", raw, "text/plain",
        )
        assert text == sample
    else:
        with pytest.raises(ParseError) as exc_info:
            extract_text(f"{label}.txt", raw, "text/plain")
        assert (exc_info.value.user_message
                == parser_registry._UNSUPPORTED_TEXT_CONTENT_MESSAGE)


def test_r16_parity_alphabet_rule_over_the_whole_two_byte_space() -> None:
    """F3: pin the check on the population it actually decides.

    The exemption's own test sampled natural Cyrillic against two negatives
    that happened not to satisfy the helper, so it never touched the 1 920
    scalars the helper served. What now carries that decision is the
    tiny-filler clause of ``_parity_lanes_show_filler_smuggle``, and this
    sweeps its whole population: every two-byte lead family (0xC2-0xDF, all
    of U+0080-U+07FF), every alphabet size within each family, whitespace-
    free, 400 characters.

    The rule, measured 2026-08-02 and identical in all 29 usable families
    (0xCC is entirely combining marks, so no readable run exists there): a
    whitespace-free run accepts while it uses fewer than 24 distinct
    scalars and is refused from 24 up. Nothing depends on which block the
    scalars come from — if a script exemption comes back under any name,
    some family stops matching and this goes red.
    """
    threshold = 24
    families = 0
    for lead in range(0xC2, 0xE0):
        base = (lead & 0x1F) << 6
        block = [chr(base + i) for i in range(64)]
        readable = [c for c in block if parser_registry._is_readable_char(c)]
        if len(readable) < 2:
            continue  # 0xCC is entirely combining marks: no run to build
        families += 1
        # The full readable block, space-free, is refused in every family.
        full = "".join((readable * (400 // len(readable) + 1)))[:400]
        if len(readable) >= threshold:
            assert not _accepts(full.encode("utf-8")), (
                f"lead {lead:#04x}: {len(readable)} distinct scalars accepted"
            )
        # Alphabet-size sweep. k == 1 is decided by the mono-symbol pad
        # rule, not by parity, so it is excluded here by construction.
        for k in range(2, len(readable) + 1):
            alphabet = readable[:k]
            sample = "".join(alphabet * (400 // k + 1))[:400]
            accepted = _accepts(sample.encode("utf-8"))
            assert accepted == (k < threshold), (
                f"lead {lead:#04x}, {k} distinct scalars: "
                f"{'accepted' if accepted else 'refused'}, expected the "
                f"opposite — the boundary moved off {threshold}"
            )
    assert families == 29, f"expected 29 usable two-byte lead families, got {families}"
