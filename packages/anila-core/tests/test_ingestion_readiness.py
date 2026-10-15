"""Offline inventory exposes fixed capabilities without executing parsers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from anila_core.ingestion import readiness


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(readiness, "_CACHE_REPORT", None)
    monkeypatch.setattr(readiness, "_CACHE_DEADLINE", 0)


@pytest.fixture
def ready_modules(monkeypatch):
    seen = []

    def find(name):
        assert "." not in name
        seen.append(name)
        return object()

    monkeypatch.setattr(readiness.importlib.util, "find_spec", find)
    return seen


@pytest.fixture
def ready_tools(monkeypatch):
    calls = []

    def probe(tool):
        calls.append(tool)
        return (
            'List of available languages in "/private/corpus/path" (3):\nchi_tra\neng\nprivate_language\n'
            if tool == "tesseract"
            else "LibreOffice 99.9 secret-build-path"
        )

    monkeypatch.setattr(readiness, "_probe_tool", probe)
    return calls


def test_ready_inventory_has_exact_local_scope_and_never_exposes_probe_details(
    ready_modules, ready_tools
):
    report = readiness.diagnose_local_ingestion()
    assert report["status"] == "ready" and report["scope"] == "current_process"
    assert report["not_checked"] == [
        "ingestion_worker",
        "remote_docling",
        "production_image",
    ]
    assert ready_tools == ["libreoffice", "tesseract"]
    assert set(ready_modules) == {"fitz", "pymupdf4llm", "docx", "lxml", "odf"}
    assert len(report["checks"]) == 8
    assert all(check["type"] == "ready" for check in report["checks"])
    assert all(item["status"] == "ready" for item in report["formats"])
    rendered = json.dumps(report, ensure_ascii=False)
    for private in ("/private", "private_language", "99.9", "secret-build-path"):
        assert private not in rendered
    assert "尚未執行" in rendered and "未檢查" in report["message"]


@pytest.mark.parametrize("missing", [("chi_tra",), ("eng",), ("chi_tra", "eng")])
def test_missing_each_required_ocr_corpus_has_clear_type_and_fixed_names(
    ready_modules, monkeypatch, missing
):
    def probe(tool):
        return (
            "LibreOffice version"
            if tool == "libreoffice"
            else "header /private/path\n" + "\n".join({"chi_tra", "eng"} - set(missing))
        )

    monkeypatch.setattr(readiness, "_probe_tool", probe)
    report = readiness.diagnose_local_ingestion()
    check = next(item for item in report["checks"] if item["id"] == "tesseract")
    assert check["type"] == "offline_ocr_languages_missing"
    assert check["missing"] == sorted(missing)
    assert all(name in check["message"] for name in missing)
    formats = {item["id"]: item for item in report["formats"]}
    assert (
        formats["pdf_scanned"]["status"] == formats["image_ocr"]["status"] == "blocked"
    )
    assert formats["pdf_text"]["status"] == formats["docx"]["status"] == "ready"


@pytest.mark.parametrize(
    "tool,prefix", [("tesseract", "offline_ocr"), ("libreoffice", "office_converter")]
)
@pytest.mark.parametrize(
    "failure",
    ["missing", "unavailable", "timeout", "probe_failed", "probe_output_limit"],
)
def test_tool_failure_taxonomy_stays_local_and_sanitized(
    ready_modules, monkeypatch, tool, prefix, failure
):
    def probe(name):
        if name == tool:
            raise readiness._ProbeFailure(failure)
        return "chi_tra\neng\n" if name == "tesseract" else "LibreOffice version"

    monkeypatch.setattr(readiness, "_probe_tool", probe)
    report = readiness.diagnose_local_ingestion()
    check = next(item for item in report["checks"] if item["id"] == tool)
    assert report["status"] == "attention" and check["status"] == "blocked"
    assert check["type"] == f"{prefix}_{failure}"
    assert check["action"] and check["message"]
    assert check["missing"] == ([tool] if failure == "missing" else [])


def test_missing_pdf_dependency_does_not_claim_image_ocr_unavailable(
    ready_modules, ready_tools, monkeypatch
):
    monkeypatch.setattr(
        readiness.importlib.util,
        "find_spec",
        lambda name: None if name == "pymupdf4llm" else object(),
    )
    report = readiness.diagnose_local_ingestion()
    formats = {item["id"]: item for item in report["formats"]}
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["pdf"]["type"] == "parser_dependency_missing"
    assert checks["pdf"]["missing"] == ["pymupdf4llm"]
    assert formats["pdf_text"]["status"] == "blocked"
    assert formats["image_ocr"]["status"] == "ready"


def test_probe_runner_uses_only_fixed_binary_and_flag_with_no_shell(monkeypatch):
    requested = []
    monkeypatch.setattr(
        readiness.shutil, "which", lambda name: requested.append(name) or None
    )
    with pytest.raises(readiness._ProbeFailure) as missing:
        readiness._probe_tool("libreoffice")
    assert missing.value.kind == "missing"
    assert requested == ["libreoffice", "soffice"]
    with pytest.raises(KeyError):
        readiness._probe_tool("arbitrary-command")
    assert requested == ["libreoffice", "soffice"]


@pytest.mark.parametrize(
    "failure", [None, "probe_output_limit", "timeout", "orphan_timeout", "probe_failed"]
)
def test_probe_reader_has_hard_output_and_time_bounds_and_closes_process(
    monkeypatch, failure
):
    requested, reads, killed, closed = [], [], [], []
    process = SimpleNamespace(
        pid=1234567, returncode=0 if failure == "orphan_timeout" else None
    )
    process.stdout = SimpleNamespace(close=lambda: closed.append(True))
    process.poll = lambda: process.returncode

    def wait(timeout):
        process.returncode = 1 if failure == "probe_failed" else 0
        return process.returncode

    process.wait = wait
    process.kill = lambda: killed.append("fallback")
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/synthetic/" + name)
    monkeypatch.setattr(
        readiness.subprocess,
        "Popen",
        lambda args, **kw: requested.append((args, kw)) or process,
    )
    monkeypatch.setattr(readiness.os, "killpg", lambda pid, sig: killed.append(pid))
    clock = iter([0, 0, 3] if failure in ("timeout", "orphan_timeout") else [0] * 30)
    monkeypatch.setattr(readiness.time, "monotonic", lambda: next(clock))

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def register(self, stream, events):
            assert stream is process.stdout

        def select(self, remaining):
            return (
                []
                if failure in ("timeout", "orphan_timeout")
                else [(SimpleNamespace(fd=7), None)]
            )

    monkeypatch.setattr(readiness.selectors, "DefaultSelector", Selector)

    def read(fd, size):
        reads.append(size)
        assert size <= 4096
        if failure == "probe_output_limit":
            return b"x" * size
        return b"LibreOffice version" if len(reads) == 1 else b""

    monkeypatch.setattr(readiness.os, "read", read)
    if failure:
        with pytest.raises(readiness._ProbeFailure) as failed:
            readiness._probe_tool("libreoffice")
        assert failed.value.kind == (
            "timeout" if failure == "orphan_timeout" else failure
        )
    else:
        assert readiness._probe_tool("libreoffice") == "LibreOffice version"
    args, kwargs = requested[0]
    assert args == ["/synthetic/libreoffice", "--version"]
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    assert kwargs["stdin"] == readiness.subprocess.DEVNULL
    assert kwargs["stderr"] == readiness.subprocess.STDOUT
    assert len(reads) <= 5 and closed == [True]
    assert killed == [process.pid]


def test_process_local_cache_singleflights_parallel_requests_and_copies_results(
    ready_modules, ready_tools
):
    with ThreadPoolExecutor(max_workers=4) as executor:
        reports = list(
            executor.map(lambda _: readiness.diagnose_local_ingestion(), range(8))
        )
    assert ready_tools == ["libreoffice", "tesseract"]
    assert len({report["checked_at"] for report in reports}) == 1
    reports[0]["checks"][0]["status"] = "blocked"
    assert reports[1]["checks"][0]["status"] == "ready"
    readiness._CACHE_DEADLINE = 0
    readiness.diagnose_local_ingestion()
    assert ready_tools == ["libreoffice", "tesseract", "libreoffice", "tesseract"]
