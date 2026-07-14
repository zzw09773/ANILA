from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from app.modules.artifacts import blob_store


def _office_bytes(kind: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    root, main_type = {
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
    }[kind]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/{root}" ContentType="{main_type}"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(root, "<root/>")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


_PPTX = _office_bytes("pptx")
_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _store(tmp_path, content: bytes = _PPTX):
    return blob_store.store_stream(
        io.BytesIO(content),
        storage_root=tmp_path,
        artifact_type="slides",
        declared_sha256=hashlib.sha256(content).hexdigest(),
        declared_size=len(content),
        media_type=_MIME,
        original_filename="deck.pptx",
    )


def test_blob_survives_new_resolver_instance_and_matches_bytes(tmp_path):
    stored = _store(tmp_path)
    path = blob_store.resolve_blob_path(str(tmp_path), stored.key)
    assert path.read_bytes() == _PPTX
    assert stored.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stored.size_bytes == path.stat().st_size


def test_tampered_hash_and_size_never_publish_blob(tmp_path):
    with pytest.raises(blob_store.BlobValidationError, match="SHA-256"):
        blob_store.store_stream(
            io.BytesIO(_PPTX),
            storage_root=tmp_path,
            artifact_type="slides",
            declared_sha256="0" * 64,
            declared_size=len(_PPTX),
            media_type=_MIME,
            original_filename="deck.pptx",
        )
    assert not [path for path in tmp_path.rglob("*.pptx")]


def test_atomic_publication_refuses_duplicate_destination(tmp_path, monkeypatch):
    tokens = iter(["a" * 64, "b" * 64, "c" * 64, "b" * 64])
    monkeypatch.setattr(blob_store.secrets, "token_hex", lambda _size: next(tokens))
    first = _store(tmp_path)
    with pytest.raises(FileExistsError):
        _store(tmp_path)
    assert blob_store.resolve_blob_path(tmp_path, first.key).read_bytes() == _PPTX
    assert len(list(tmp_path.rglob("*.pptx"))) == 1


@pytest.mark.parametrize(
    "key",
    ["../secret", "aa/../../secret.pptx", "/absolute.pptx", "aa/not-opaque.pptx"],
)
def test_blob_key_path_traversal_is_rejected(tmp_path, key):
    with pytest.raises(blob_store.BlobValidationError):
        blob_store.resolve_blob_path(tmp_path, key)


def test_mime_and_magic_are_both_enforced(tmp_path):
    content = b"not-a-zip"
    with pytest.raises(blob_store.BlobValidationError, match="ZIP|OOXML"):
        blob_store.store_stream(
            io.BytesIO(content),
            storage_root=tmp_path,
            artifact_type="slides",
            declared_sha256=hashlib.sha256(content).hexdigest(),
            declared_size=len(content),
            media_type=_MIME,
            original_filename="deck.pptx",
        )


def test_wrong_ooxml_container_and_macro_never_publish(tmp_path):
    for content in (
        _office_bytes("xlsx"),
        _office_bytes("pptx", extra={"ppt/vbaProject.bin": b"macro"}),
    ):
        with pytest.raises(blob_store.BlobValidationError):
            _store(tmp_path, content)
    assert not list(tmp_path.rglob("*.pptx"))
