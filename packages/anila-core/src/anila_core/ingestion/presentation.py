"""Read PPTX slides in presentation order using the standard library."""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .errors import ParseError
from .parser_registry import ImageRef, ParsedDocument, _new_image_id

_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_CHART = "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart"
_SMART_ART = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds"


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    raw = archive.read(name)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ParseError.corrupt("簡報包含不支援的 XML 宣告，請另存為 PPTX 後上傳。")
    return ET.fromstring(raw)


def _relationships(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    if name not in archive.namelist():
        return {}
    return {
        str(item.get("Id")): str(item.get("Target"))
        for item in _xml(archive, name)
        if item.get("TargetMode") != "External"
    }


def _text(element: ET.Element) -> str:
    return "\n".join(
        "".join(node.text or "" for node in para.iter(_A + "t")) for para in element.iter(_A + "p")
    ).strip()


class PptxParser:
    """Preserve slide numbers, text boxes, table rows, and embedded figures."""

    def parse(self, file_path: str) -> ParsedDocument:
        slides: list[str] = []
        images: dict[str, ImageRef] = {}
        unsupported: list[dict[str, object]] = []
        with zipfile.ZipFile(file_path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                raise ParseError.too_large("簡報解壓後超過 100 MB，請分成較小的簡報上傳。")
            presentation = _xml(archive, "ppt/presentation.xml")
            rels = _relationships(archive, "ppt/_rels/presentation.xml.rels")
            for number, item in enumerate(presentation.iter(_P + "sldId"), 1):
                target = rels.get(item.get(_R + "id", ""))
                if not target:
                    raise ParseError.corrupt("簡報的投影片索引已損毀，請另存為 PPTX 後上傳。")
                name = posixpath.normpath(posixpath.join("ppt", target.lstrip("/")))
                if target.startswith("/"):
                    name = target.lstrip("/")
                slide = _xml(archive, name)
                slide_rels = _relationships(
                    archive,
                    posixpath.join(
                        posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels"
                    ),
                )
                parts: list[str] = []
                tree = slide.find(".//" + _P + "spTree")
                if tree is not None:
                    for shape in tree.iter():
                        if shape.tag in {_CHART, _SMART_ART}:
                            unsupported.append(
                                {
                                    "slide": number,
                                    "kind": "chart" if shape.tag == _CHART else "smartart",
                                }
                            )
                        if shape.tag == _P + "sp":
                            text = _text(shape)
                            if text:
                                parts.append(text)
                        elif shape.tag == _A + "tbl":
                            rows = [
                                [
                                    _text(cell).replace("|", "\\|").replace("\n", "<br>")
                                    for cell in row.findall(_A + "tc")
                                ]
                                for row in shape.findall(_A + "tr")
                            ]
                            if rows:
                                table = ["| " + " | ".join(row) + " |" for row in rows]
                                table.insert(1, "|" + "---|" * len(rows[0]))
                                parts.append("\n".join(table))
                        elif shape.tag == _P + "pic":
                            blip = shape.find(".//" + _A + "blip")
                            rid = blip.get(_R + "embed", "") if blip is not None else ""
                            image_target = slide_rels.get(rid)
                            if not image_target:
                                continue
                            image_path = posixpath.normpath(
                                posixpath.join(posixpath.dirname(name), image_target)
                            )
                            if image_target.startswith("/"):
                                image_path = image_target.lstrip("/")
                            data = archive.read(image_path)
                            image_id = _new_image_id()
                            import mimetypes

                            images[image_id] = ImageRef(
                                image_id,
                                data,
                                mimetypes.guess_type(image_path)[0] or "image/png",
                                number,
                            )
                            parts.append(f"[[IMAGE:{image_id}]]")
                slides.append("\n\n".join(parts).replace("\f", " "))
        return ParsedDocument(
            content="\f".join(slides),
            metadata={
                "title": Path(file_path).stem,
                "pages": len(slides),
                "slide_count": len(slides),
                "parser": "pptx-native",
                "embedded_images": len(images),
                **(
                    {
                        "unsupported_objects": unsupported,
                        "quality": "partial",
                        "quality_notice": "簡報包含圖表或 SmartArt，原生解析只保留可讀文字、表格與圖片；這些物件的內容與版面可能缺漏，請核對原檔。",
                    }
                    if unsupported
                    else {}
                ),
            },
            source_path=file_path,
            format="pptx",
            images=images,
        )
