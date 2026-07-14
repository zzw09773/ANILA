"""Offline, fail-closed content identification for CSP upload boundaries.

The filename and multipart Content-Type are claims, not evidence.  This module
identifies the bytes independently, then requires all three signals to agree.
It intentionally uses only the Python standard library so the same checks run
inside an air-gapped CSP image.
"""

from __future__ import annotations

import json
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


class ContentValidationError(ValueError):
    """The claimed file format does not match a safe supported structure."""


@dataclass(frozen=True)
class SniffedContent:
    extension: str
    media_type: str
    container: str


MIME_BY_EXTENSION: dict[str, tuple[str, ...]] = {
    ".txt": ("text/plain",),
    ".md": ("text/markdown", "text/plain"),
    ".json": ("application/json",),
    ".html": ("text/html",),
    ".htm": ("text/html",),
    ".rtf": ("application/rtf", "text/rtf"),
    ".pdf": ("application/pdf",),
    ".doc": ("application/msword",),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".odt": ("application/vnd.oasis.opendocument.text",),
    ".png": ("image/png",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".webp": ("image/webp",),
    ".gif": ("image/gif",),
    ".bmp": ("image/bmp",),
    ".svg": ("image/svg+xml",),
    ".zip": ("application/zip", "application/x-zip-compressed"),
}

INGESTION_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".json", ".html", ".htm", ".rtf", ".pdf",
        ".doc", ".docx", ".pptx", ".xlsx", ".odt", ".png", ".jpg",
        ".jpeg", ".webp", ".gif", ".bmp",
    }
)

_OOXML_ROOTS = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
}
_OOXML_MAIN_TYPES = {
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml"
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml"
    ),
}
_BINARY_PREFIXES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"RIFF",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
)
_MAX_XML_BYTES = 2 * 1024 * 1024
_MAX_ZIP_ENTRIES = 5000
_MAX_ZIP_MEMBER = 64 * 1024 * 1024
_MAX_ZIP_TOTAL = 1024 * 1024 * 1024
_MAX_RATIO = 200


def _normalise_mime(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _extension(filename: str) -> str:
    clean = (filename or "").replace("\\", "/")
    if "\x00" in clean or "\r" in clean or "\n" in clean:
        raise ContentValidationError("filename contains control characters")
    extension = Path(clean).suffix.lower()
    if extension in {".docm", ".dotm", ".xlsm", ".xltm", ".pptm", ".potm"}:
        raise ContentValidationError("macro-enabled Office files are prohibited")
    return extension


def _require_claims(
    *, filename: str, declared_mime: str | None, allowed_extensions: frozenset[str]
) -> tuple[str, str]:
    extension = _extension(filename)
    if extension not in allowed_extensions or extension not in MIME_BY_EXTENSION:
        raise ContentValidationError(f"unsupported file extension: {extension or '<none>'}")
    allowed_mimes = MIME_BY_EXTENSION[extension]
    claimed = _normalise_mime(declared_mime)
    if claimed is not None and claimed not in allowed_mimes:
        raise ContentValidationError(
            f"multipart MIME {claimed!r} does not match extension {extension}"
        )
    return extension, allowed_mimes[0]


def _reject_foreign_prefix(data: bytes, expected: tuple[bytes, ...]) -> None:
    for prefix in _BINARY_PREFIXES:
        if data.startswith(prefix) and prefix not in expected:
            raise ContentValidationError("content signature does not match extension")


def _safe_text(data: bytes) -> str:
    if b"\x00" in data:
        raise ContentValidationError("text file contains NUL bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContentValidationError("text file must be valid UTF-8") from exc
    controls = sum(ord(char) < 32 and char not in "\t\r\n\f" for char in text)
    if controls > max(4, len(text) // 1000):
        raise ContentValidationError("text file contains excessive control bytes")
    return text


def _validate_pdf(data: bytes) -> None:
    first_line = data.splitlines()[0] if data else b""
    if (
        not re.fullmatch(br"%PDF-[12]\.[0-9]", first_line)
        or len(first_line) > 1024
        or len(data) < 12
    ):
        raise ContentValidationError("truncated or invalid PDF header")
    stripped = data.rstrip(b"\x00\t\r\n ")
    if not stripped.endswith(b"%%EOF"):
        raise ContentValidationError("truncated PDF: missing terminal EOF marker")
    if b"PK\x03\x04" in data or re.search(br"<\s*(?:html|script)\b", data[:4096], re.I):
        raise ContentValidationError("PDF polyglot content is prohibited")


def _validate_png(data: bytes) -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ContentValidationError("invalid PNG signature")
    offset = len(signature)
    seen_ihdr = seen_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ContentValidationError("truncated PNG chunk header")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > _MAX_ZIP_MEMBER or end > len(data):
            raise ContentValidationError("oversized or truncated PNG chunk")
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ContentValidationError("PNG chunk CRC mismatch")
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                raise ContentValidationError("PNG must begin with one IHDR chunk")
            width, height = struct.unpack(">II", payload[:8])
            if width == 0 or height == 0:
                raise ContentValidationError("PNG dimensions must be non-zero")
            seen_ihdr = True
        if kind == b"IEND":
            if length != 0 or end != len(data):
                raise ContentValidationError("PNG trailing/polyglot bytes prohibited")
            seen_iend = True
            break
        offset = end
    if not seen_ihdr or not seen_iend:
        raise ContentValidationError("truncated PNG")


def _validate_image(data: bytes, extension: str) -> None:
    if extension == ".png":
        _validate_png(data)
    elif extension in {".jpg", ".jpeg"}:
        if len(data) < 12 or not data.startswith(b"\xff\xd8\xff"):
            raise ContentValidationError("truncated or invalid JPEG")
        if b"PK\x03\x04" in data:
            raise ContentValidationError("JPEG polyglot content is prohibited")
        offset = 2
        seen_sof = seen_sos = False
        while offset < len(data):
            if data[offset] != 0xFF:
                raise ContentValidationError("invalid JPEG marker stream")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                raise ContentValidationError("truncated JPEG marker")
            marker = data[offset]
            offset += 1
            if marker == 0xD9:
                if offset != len(data) or not seen_sof or not seen_sos:
                    raise ContentValidationError("invalid JPEG terminal marker")
                return
            if marker in {0x01, *range(0xD0, 0xD8)}:
                continue
            if offset + 2 > len(data):
                raise ContentValidationError("truncated JPEG segment header")
            length = struct.unpack(">H", data[offset : offset + 2])[0]
            if length < 2 or offset + length > len(data):
                raise ContentValidationError("oversized or truncated JPEG segment")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 8 or data[offset + 3 : offset + 7] == b"\x00\x00\x00\x00":
                    raise ContentValidationError("invalid JPEG frame header")
                seen_sof = True
            if marker == 0xDA:
                seen_sos = True
                eoi = data.rfind(b"\xff\xd9")
                if eoi < offset + length or eoi != len(data) - 2 or not seen_sof:
                    raise ContentValidationError("truncated JPEG scan")
                return
            offset += length
        raise ContentValidationError("truncated JPEG")
    elif extension == ".gif":
        if len(data) < 14 or data[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ContentValidationError("truncated or invalid GIF")
        if data[-1:] != b";" or data[6:10] == b"\x00\x00\x00\x00":
            raise ContentValidationError("invalid GIF structure")
    elif extension == ".bmp":
        if len(data) < 54 or not data.startswith(b"BM"):
            raise ContentValidationError("truncated or invalid BMP")
        if struct.unpack("<I", data[2:6])[0] != len(data):
            raise ContentValidationError("BMP length header mismatch")
        if struct.unpack("<I", data[14:18])[0] < 40:
            raise ContentValidationError("unsupported BMP DIB header")
        pixel_offset = struct.unpack("<I", data[10:14])[0]
        width, height = struct.unpack("<ii", data[18:26])
        if pixel_offset < 54 or pixel_offset >= len(data) or width <= 0 or height == 0:
            raise ContentValidationError("invalid BMP dimensions or pixel offset")
    elif extension == ".webp":
        if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ContentValidationError("truncated or invalid WebP")
        if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
            raise ContentValidationError("WebP RIFF length mismatch")
        if data[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}:
            raise ContentValidationError("unsupported WebP primary chunk")
        chunk_size = struct.unpack("<I", data[16:20])[0]
        if 20 + chunk_size + (chunk_size & 1) > len(data):
            raise ContentValidationError("truncated WebP primary chunk")


def _validate_xml(data: bytes, *, expected_root: str, active_content: bool) -> None:
    if len(data) > _MAX_XML_BYTES:
        raise ContentValidationError("XML header/document exceeds inspection limit")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ContentValidationError("DTD/entity declarations are prohibited")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ContentValidationError("invalid or truncated XML") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != expected_root:
        raise ContentValidationError(f"unexpected XML root; expected {expected_root}")
    if active_content:
        forbidden = {"script", "foreignobject", "iframe", "object", "embed"}
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() in forbidden:
                raise ContentValidationError("active XML/SVG content is prohibited")
            for key, value in element.attrib.items():
                local = key.rsplit("}", 1)[-1].lower()
                probe = value.strip().lower()
                if local.startswith("on") or probe.startswith(("javascript:", "data:text/html")):
                    raise ContentValidationError("active XML/SVG attribute is prohibited")


def _validate_zip_layout(data: bytes, *, total_limit: int = _MAX_ZIP_TOTAL) -> zipfile.ZipFile:
    if not data.startswith(b"PK\x03\x04"):
        raise ContentValidationError("ZIP container must start at byte zero")
    search_start = max(0, len(data) - 65557)
    eocd = data.rfind(b"PK\x05\x06", search_start)
    if eocd < 0 or eocd + 22 > len(data):
        raise ContentValidationError("truncated ZIP end-of-central-directory")
    comment_length = struct.unpack("<H", data[eocd + 20 : eocd + 22])[0]
    if eocd + 22 + comment_length != len(data):
        raise ContentValidationError("ZIP trailing/polyglot bytes prohibited")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ContentValidationError("invalid ZIP container") from exc
    if not entries or len(entries) > _MAX_ZIP_ENTRIES:
        archive.close()
        raise ContentValidationError("ZIP entry count is empty or excessive")
    total = 0
    names: set[str] = set()
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        folded = name.casefold()
        mode = entry.external_attr >> 16
        if (
            not name
            or "\x00" in name
            or pure.is_absolute()
            or ".." in pure.parts
            or (pure.parts and ":" in pure.parts[0])
            or folded in names
            or stat.S_ISLNK(mode)
            or entry.flag_bits & 0x1
        ):
            archive.close()
            raise ContentValidationError("unsafe ZIP member path/type/encryption")
        names.add(folded)
        if entry.file_size > _MAX_ZIP_MEMBER:
            archive.close()
            raise ContentValidationError("oversized ZIP member")
        if entry.file_size and (
            entry.compress_size == 0
            or entry.file_size > max(1024 * 1024, entry.compress_size * _MAX_RATIO)
        ):
            archive.close()
            raise ContentValidationError("ZIP compression ratio exceeds safety limit")
        total += entry.file_size
        if total > total_limit:
            archive.close()
            raise ContentValidationError("ZIP expanded size exceeds safety limit")
    return archive


def validate_zip_archive(
    data: bytes, *, filename: str, declared_mime: str | None
) -> None:
    """Validate the outer bulk-upload archive, not its member formats."""
    _require_claims(
        filename=filename,
        declared_mime=declared_mime,
        allowed_extensions=frozenset({".zip"}),
    )
    archive = _validate_zip_layout(data)
    archive.close()


def _validate_office_container(data: bytes, extension: str) -> None:
    archive = _validate_zip_layout(data, total_limit=256 * 1024 * 1024)
    try:
        entries = archive.infolist()
        names = {entry.filename.replace("\\", "/") for entry in entries}
        lowered_names = {name.lower() for name in names}
        if extension == ".odt":
            if (
                entries[0].filename != "mimetype"
                or entries[0].compress_type != zipfile.ZIP_STORED
                or archive.read(entries[0])
                != b"application/vnd.oasis.opendocument.text"
                or "content.xml" not in names
                or "META-INF/manifest.xml" not in names
            ):
                raise ContentValidationError("invalid or wrong ODT container")
            if any(
                name.startswith(("scripts/", "basic/"))
                or "/scripts/" in name
                or "/basic/" in name
                for name in lowered_names
            ):
                raise ContentValidationError("ODT script/macro content is prohibited")
            _validate_xml(archive.read("content.xml"), expected_root="document-content", active_content=False)
            return

        if "[Content_Types].xml" not in names or "_rels/.rels" not in names:
            raise ContentValidationError("OOXML package lacks required roots")
        roots = {kind: path in names for kind, path in _OOXML_ROOTS.items()}
        detected = [kind for kind, present in roots.items() if present]
        if detected != [extension]:
            raise ContentValidationError(
                f"wrong OOXML container: claimed {extension}, detected {detected}"
            )
        active_names = (
            "vbaproject.bin", "vbadata.xml", "activex/", "/activex/",
            "embeddings/", "/embeddings/", "oleobject", "attachedtemplate",
        )
        if any(any(token in name for token in active_names) for name in lowered_names):
            raise ContentValidationError("OOXML active/macro content is prohibited")
        content_types = archive.read("[Content_Types].xml")
        if len(content_types) > _MAX_XML_BYTES:
            raise ContentValidationError("OOXML content-types header is oversized")
        lowered_types = content_types.lower()
        if any(
            token in lowered_types
            for token in (b"macroenabled", b"vbaproject", b"activex", b"oleobject")
        ):
            raise ContentValidationError("OOXML macro-enabled content type prohibited")
        _validate_xml(content_types, expected_root="types", active_content=False)
        if _OOXML_MAIN_TYPES[extension].encode() not in content_types:
            raise ContentValidationError("OOXML main content type does not match extension")
        for name in names:
            if not name.lower().endswith(".rels"):
                continue
            raw = archive.read(name)
            if len(raw) > _MAX_XML_BYTES:
                raise ContentValidationError("OOXML relationships header is oversized")
            if b"TargetMode=\"External\"" in raw or b"TargetMode='External'" in raw:
                lowered = raw.lower()
                if b"/hyperlink" not in lowered:
                    raise ContentValidationError("OOXML external active relationship prohibited")
    except (KeyError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise ContentValidationError("truncated or corrupt Office container") from exc
    finally:
        archive.close()


def validate_content(
    data: bytes,
    *,
    filename: str,
    declared_mime: str | None = None,
    allowed_extensions: frozenset[str] = INGESTION_EXTENSIONS,
) -> SniffedContent:
    """Identify and validate one complete upload before it is persisted."""
    if not data:
        raise ContentValidationError("empty file")
    extension, canonical_mime = _require_claims(
        filename=filename,
        declared_mime=declared_mime,
        allowed_extensions=allowed_extensions,
    )
    if extension in _OOXML_ROOTS or extension == ".odt":
        _reject_foreign_prefix(data, (b"PK\x03\x04",))
        _validate_office_container(data, extension)
        container = extension.removeprefix(".")
    elif extension == ".pdf":
        _reject_foreign_prefix(data, (b"%PDF-",))
        _validate_pdf(data)
        container = "pdf"
    elif extension in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        expected = {
            ".png": (b"\x89PNG\r\n\x1a\n",),
            ".jpg": (b"\xff\xd8\xff",),
            ".jpeg": (b"\xff\xd8\xff",),
            ".webp": (b"RIFF",),
            ".gif": (b"GIF87a", b"GIF89a"),
            ".bmp": (b"BM",),
        }[extension]
        _reject_foreign_prefix(data, expected)
        _validate_image(data, extension)
        container = canonical_mime.removeprefix("image/")
    elif extension == ".svg":
        _reject_foreign_prefix(data, ())
        _validate_xml(data, expected_root="svg", active_content=True)
        container = "svg"
    elif extension == ".doc":
        signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        _reject_foreign_prefix(data, (signature,))
        if len(data) < 512 or not data.startswith(signature):
            raise ContentValidationError("truncated or invalid OLE Word document")
        if data[28:30] != b"\xfe\xff" or struct.unpack("<H", data[30:32])[0] not in {9, 12}:
            raise ContentValidationError("invalid OLE compound-file header")
        if any(token in data for token in (b"_VBA_PROJECT", b"Macros", b"VBA")):
            raise ContentValidationError("legacy Word macro content is prohibited")
        container = "ole-doc"
    else:
        _reject_foreign_prefix(data, ())
        text = _safe_text(data)
        lowered = text.lstrip().lower()
        if extension == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise ContentValidationError("invalid or truncated JSON") from exc
        elif extension in {".html", ".htm"}:
            if not re.search(r"<(?:!doctype\s+html|html|head|body|article|p|div)\b", lowered):
                raise ContentValidationError("HTML structure not detected")
            if re.search(r"<(?:script|iframe|object|embed)\b|\son[a-z]+\s*=|javascript:", lowered):
                raise ContentValidationError("active HTML content is prohibited")
        elif extension == ".rtf":
            if not lowered.startswith("{\\rtf") or not text.rstrip().endswith("}"):
                raise ContentValidationError("truncated or invalid RTF")
            if len(re.findall(r"(?<!\\)\{", text)) != len(
                re.findall(r"(?<!\\)\}", text)
            ):
                raise ContentValidationError("unbalanced RTF groups")
            if re.search(r"\\(?:object|objdata|field)\b", lowered):
                raise ContentValidationError("active/embedded RTF content is prohibited")
        container = extension.removeprefix(".")
    return SniffedContent(extension, canonical_mime, container)


__all__ = [
    "ContentValidationError",
    "INGESTION_EXTENSIONS",
    "MIME_BY_EXTENSION",
    "SniffedContent",
    "validate_content",
    "validate_zip_archive",
]
