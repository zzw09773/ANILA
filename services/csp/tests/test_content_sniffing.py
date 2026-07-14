from __future__ import annotations

import struct
import zipfile
import zlib
from io import BytesIO

import pytest

from app.services.content_sniffing import (
    ContentValidationError,
    INGESTION_EXTENSIONS,
    validate_content,
    validate_zip_archive,
)


_MAIN_TYPES = {
    "docx": (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml",
    ),
    "pptx": (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml",
    ),
    "xlsx": (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml",
    ),
}


def office_bytes(
    kind: str,
    *,
    extra: dict[str, bytes] | None = None,
    content_type: str | None = None,
    oversized_content_types: bool = False,
) -> bytes:
    root, main_type = _MAIN_TYPES[kind]
    main_type = content_type or main_type
    types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{root}" ContentType="{main_type}"/>'
        "</Types>"
    ).encode()
    if oversized_content_types:
        types += b" " * (2 * 1024 * 1024)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(root, b"<root/>")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def odt_bytes(*, macro: bool = False) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, b"application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>',
        )
        archive.writestr("META-INF/manifest.xml", "<manifest/>")
        if macro:
            archive.writestr("Scripts/python/evil.py", "print('evil')")
    return output.getvalue()


def png_bytes() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def jpeg_bytes() -> bytes:
    app = b"\xff\xe0" + struct.pack(">H", 4) + b"JF"
    frame_payload = b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    frame = b"\xff\xc0" + struct.pack(">H", len(frame_payload) + 2) + frame_payload
    scan_payload = b"\x01\x01\x00\x00\x3f\x00"
    scan = b"\xff\xda" + struct.pack(">H", len(scan_payload) + 2) + scan_payload
    return b"\xff\xd8" + app + frame + scan + b"\x00\xff\xd9"


def gif_bytes() -> bytes:
    return b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0) + b";"


def bmp_bytes() -> bytes:
    output = bytearray(58)
    output[:2] = b"BM"
    output[2:6] = struct.pack("<I", len(output))
    output[10:14] = struct.pack("<I", 54)
    output[14:18] = struct.pack("<I", 40)
    output[18:26] = struct.pack("<ii", 1, 1)
    output[26:30] = struct.pack("<HH", 1, 24)
    return bytes(output)


def webp_bytes() -> bytes:
    payload = b"\x2f"
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload + b"\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
def test_ooxml_container_is_identified_from_structure(kind: str) -> None:
    result = validate_content(
        office_bytes(kind),
        filename=f"safe.{kind}",
        declared_mime={
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }[kind],
    )
    assert result.container == kind


def test_odt_requires_real_odf_structure_and_rejects_macros() -> None:
    assert validate_content(odt_bytes(), filename="safe.odt").container == "odt"
    with pytest.raises(ContentValidationError, match="script/macro"):
        validate_content(odt_bytes(macro=True), filename="unsafe.odt")


@pytest.mark.parametrize(
    ("filename", "content", "mime", "message"),
    [
        ("spoof.docx", b"%PDF-1.7\n%%EOF", None, "signature"),
        ("spoof.pdf", b"%PDF-1.7\n%%EOF", "image/png", "MIME"),
        ("truncated.pdf", b"%PDF-1.7\n", None, "truncated"),
        ("header.png", b"\x89PNG\r\n\x1a\n", None, "truncated"),
        ("bad.doc", b"\xd0\xcf\x11\xe0", None, "truncated"),
        ("bad.rtf", b"{not-rtf}", None, "RTF"),
        ("bad.html", b"plain text only", None, "HTML"),
    ],
)
def test_extension_mime_and_truncated_spoofs_are_rejected(
    filename: str, content: bytes, mime: str | None, message: str
) -> None:
    with pytest.raises(ContentValidationError, match=message):
        validate_content(content, filename=filename, declared_mime=mime)


def test_wrong_ooxml_container_and_active_content_are_rejected() -> None:
    with pytest.raises(ContentValidationError, match="wrong OOXML"):
        validate_content(office_bytes("xlsx"), filename="lie.docx")
    with pytest.raises(ContentValidationError, match="active/macro"):
        validate_content(
            office_bytes("docx", extra={"word/vbaProject.bin": b"macro"}),
            filename="hidden.docx",
        )
    with pytest.raises(ContentValidationError, match="macro-enabled"):
        validate_content(
            office_bytes(
                "pptx",
                content_type="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
            ),
            filename="hidden.pptx",
        )
    with pytest.raises(ContentValidationError, match="macro-enabled"):
        validate_content(office_bytes("docx"), filename="explicit.docm")


def test_oversized_ooxml_header_is_rejected() -> None:
    with pytest.raises(ContentValidationError, match="oversized"):
        validate_content(
            office_bytes("docx", oversized_content_types=True),
            filename="oversized.docx",
        )


def test_pdf_and_zip_polyglots_are_rejected() -> None:
    polyglot_pdf = b"%PDF-1.7\n" + office_bytes("docx") + b"\n%%EOF"
    with pytest.raises(ContentValidationError, match="polyglot"):
        validate_content(polyglot_pdf, filename="polyglot.pdf")
    with pytest.raises(ContentValidationError, match="trailing/polyglot"):
        validate_content(office_bytes("docx") + b"<script/>", filename="poly.docx")


def test_zip_archive_rejects_traversal_and_bomb_ratio() -> None:
    traversal = BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", "escape")
    with pytest.raises(ContentValidationError, match="unsafe ZIP"):
        validate_zip_archive(
            traversal.getvalue(), filename="batch.zip", declared_mime="application/zip"
        )

    bomb = BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", b"0" * (2 * 1024 * 1024))
    with pytest.raises(ContentValidationError, match="ratio"):
        validate_zip_archive(
            bomb.getvalue(), filename="batch.zip", declared_mime="application/zip"
        )


def test_explicit_pdf_image_text_html_rtf_doc_and_odt_signatures() -> None:
    ole_header = bytearray(512)
    ole_header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    ole_header[28:30] = b"\xfe\xff"
    ole_header[30:32] = struct.pack("<H", 9)
    ole = bytes(ole_header)
    samples = (
        (b"%PDF-1.7\n1 0 obj\nendobj\n%%EOF", "a.pdf"),
        (png_bytes(), "a.png"),
        (b"plain UTF-8 text", "a.txt"),
        (b"<!doctype html><html><body>safe</body></html>", "a.html"),
        (b"{\\rtf1\\ansi safe}", "a.rtf"),
        (ole, "a.doc"),
        (odt_bytes(), "a.odt"),
    )
    for content, filename in samples:
        assert validate_content(
            content,
            filename=filename,
            allowed_extensions=INGESTION_EXTENSIONS,
        ).extension == "." + filename.rsplit(".", 1)[1]


def test_active_svg_and_html_are_rejected() -> None:
    with pytest.raises(ContentValidationError, match="active"):
        validate_content(
            b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
            filename="unsafe.svg",
            allowed_extensions=frozenset({".svg"}),
        )
    with pytest.raises(ContentValidationError, match="active HTML"):
        validate_content(b"<html><script>alert(1)</script></html>", filename="x.html")


@pytest.mark.parametrize(
    ("content", "filename", "container"),
    [
        (jpeg_bytes(), "safe.jpg", "jpeg"),
        (jpeg_bytes(), "safe.jpeg", "jpeg"),
        (gif_bytes(), "safe.gif", "gif"),
        (bmp_bytes(), "safe.bmp", "bmp"),
        (webp_bytes(), "safe.webp", "webp"),
    ],
)
def test_supported_image_structures_are_accepted(
    content: bytes, filename: str, container: str
) -> None:
    assert validate_content(content, filename=filename).container == container
