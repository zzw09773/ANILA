"""模型畫的 SVG 進投影片前先消毒（2026-09-02）。

放行的是純向量圖：形狀、路徑、文字、漸層。拿掉：script、事件屬性、外部
連結／圖片（`http:`、`javascript:`）、foreignObject、iframe、use。太大或不是
SVG 就回 None，呼叫端把那張退回 standard。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

MAX_BYTES = 30_000
MAX_ELEMENTS = 400
SVG_NS = "http://www.w3.org/2000/svg"
_FORBIDDEN_TAGS = {"script", "foreignObject", "iframe", "image", "use", "animate", "set", "animateTransform"}
_BAD_VALUE_RE = re.compile(r"(javascript:|https?:|data:text/html)", re.IGNORECASE)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitize_svg(svg: str | None) -> str | None:
    if not isinstance(svg, str):
        return None
    text = svg.strip()
    if not text or len(text.encode("utf-8")) > MAX_BYTES or "<svg" not in text[:2000].lower():
        return None
    # ElementTree does not resolve external entities, but refuse DOCTYPE anyway.
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if _local(root.tag) != "svg":
        return None
    count = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if _local(child.tag) in _FORBIDDEN_TAGS:
                parent.remove(child)
    for el in root.iter():
        count += 1
        if count > MAX_ELEMENTS:
            return None
        for attr in list(el.attrib):
            name = _local(attr).lower()
            val = el.attrib[attr]
            if name.startswith("on") or _BAD_VALUE_RE.search(val or ""):
                del el.attrib[attr]
    if not root.attrib.get("viewBox"):
        root.set("viewBox", "0 0 1200 675")
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    out = ET.tostring(root, encoding="unicode")
    if "xmlns=" not in out[:200]:
        out = out.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    return out
