"""Document structure regressions using serialized OOXML and real chunkers."""

import io
import zipfile

import pytest

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import ParseError
from anila_core.ingestion.parser_registry import ParsedDocument, ParserRegistry
from anila_core.ingestion.parsers import extract_text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_short_fence_followed_immediately_by_heading_preserves_lines_and_offsets(newline):
    from anila_core.ingestion.chunking_plugins.structure import mask_fenced_code

    text = newline.join(
        [
            "```python",
            "# Not a heading",
            "value = 1",
            "```",
            "## Exceptions",
            "After the code.",
            "",
        ]
    )
    chunks = get_chunker("markdown-aware").chunk(text, {}, {"max_leaf_tokens": 1024})
    following = [chunk for chunk in chunks if chunk.metadata["heading"] == "Exceptions"]
    assert len(following) == 1
    chunk = following[0]
    assert chunk.metadata["heading_path"] == ["Exceptions"]
    assert chunk.metadata["heading_level"] == 2
    start = text.index("## Exceptions")
    assert chunk.metadata["offset_chars"] == start
    assert chunk.content == text[start:]
    assert all("Not a heading" not in chunk.metadata["heading_path"] for chunk in chunks)
    masked = mask_fenced_code(text)
    assert len(masked) == len(text)
    assert masked[:start] == "".join(char if char in "\r\n" else " " for char in text[:start])
    assert masked[start:] == text[start:]


@pytest.mark.parametrize("strategy", ["hierarchical", "markdown-aware", "fixed"])
@pytest.mark.parametrize(
    "block",
    [
        "| Item | Amount |\n|---|---|\n| Travel | 12 |",
        "```python\n# code, not a heading\nvalue = 12\n```",
    ],
)
def test_following_prose_keeps_heading_and_original_offset(strategy, block):
    text = (
        "# Rules\n\n## Travel\n\nBefore the object.\n\n" + block + "\n\nFollowing travel rules.\n"
    )
    chunks = get_chunker(strategy).chunk(
        text, {}, {"max_leaf_tokens": 16, "size": 128, "overlap": 0, "overlap_tokens": 0}
    )
    following = [c for c in chunks if "Following travel rules." in c.content]
    assert following
    for chunk in following:
        assert chunk.metadata["heading_path"] == ["Rules", "Travel"]
        if strategy == "markdown-aware":
            assert chunk.metadata["heading"] == "Travel"
            assert chunk.metadata["heading_level"] == 2
        offset = chunk.metadata["offset_chars"]
        assert text[offset : offset + len(chunk.content)] == chunk.content
    by_key = {chunk.chunk_key: chunk for chunk in chunks}
    if strategy == "hierarchical":
        for chunk in following:
            parent = by_key[chunk.metadata["parent_chunk_key"]]
            assert parent.content == "Travel"
            assert parent.metadata["heading_path"] == ["Rules", "Travel"]


def test_fixed_offsets_do_not_reset_after_multiple_objects():
    text = "# Rules\n\nBefore\n\n| A |\n|---|\n| B |\n\nBetween\n\n```\nx\n```\n\nAfter\n"
    chunks = get_chunker("fixed").chunk(text, {}, {"size": 128, "overlap": 0})
    for chunk in chunks:
        start = chunk.metadata["offset_chars"]
        assert text[start : start + len(chunk.content)] == chunk.content


def test_hierarchical_keeps_original_whitespace_offsets_and_new_heading():
    text = "# Rules\n\n| A |\n|---|\n| B |\n\nFirst paragraph.\n \n\nSecond paragraph.\n\n## Exceptions\n\nAfter exceptions."
    chunks = get_chunker("hierarchical").chunk(text, {}, {})
    leaves = [chunk for chunk in chunks if chunk.metadata.get("chunk_type") == "leaf"]
    for chunk in leaves:
        start = chunk.metadata["offset_chars"]
        assert text[start : start + len(chunk.content)] == chunk.content
        if "First paragraph" in chunk.content:
            assert chunk.metadata["heading_path"] == ["Rules"]
        if "After exceptions" in chunk.content:
            assert chunk.metadata["heading_path"] == ["Rules", "Exceptions"]


def test_page_splitting_keeps_document_offsets_after_table():
    text = "First page.\f# Travel\n\n| A |\n|---|\n| B |\n\nAfter table."
    chunks = get_chunker("fixed").chunk(
        text, {"format": "pdf", "pages": 2}, {"size": 128, "overlap": 0}
    )
    for chunk in chunks:
        start = chunk.metadata["offset_chars"]
        assert text[start : start + len(chunk.content)] == chunk.content


@pytest.mark.parametrize(
    "metadata", [{"pages": 5}, {"page_count": 5, "has_page_boundaries": False}]
)
def test_docling_unpaged_five_page_markdown_has_unknown_citations(metadata):
    chunks = get_chunker("pdf-page").chunk(
        "# Remote report\n\nAll five pages in one markdown string.",
        {"format": "pdf", "actual_parser": "docling-remote", **metadata},
        {},
    )
    assert chunks
    for chunk in chunks:
        assert "page" not in chunk.metadata
        assert "total_pages" not in chunk.metadata
        assert chunk.metadata["page_location_quality"] == "unknown"
        assert chunk.metadata["declared_total_pages"] == 5


def presentation_with_object(kind):
    """An OOXML presentation containing an actual chart/diagram graphicFrame.

    The object lives in a related part, not a textbox that merely says chart.
    These fixtures exercise package parsing; they do not certify rendering.
    """
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    if kind == "chart":
        uri = "http://schemas.openxmlformats.org/drawingml/2006/chart"
        object_xml = f'<c:chart xmlns:c="{uri}" r:id="rObject"/>'
        target, object_type = "charts/chart1.xml", "chart"
        data = f'<c:chartSpace xmlns:c="{uri}"><c:chart><c:plotArea><c:barChart><c:barDir val="col"/><c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>Travel budget</c:v></c:tx><c:val><c:numLit><c:formatCode>General</c:formatCode><c:ptCount val="1"/><c:pt idx="0"><c:v>123</c:v></c:pt></c:numLit></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>'
    else:
        uri = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
        object_xml = f'<dgm:relIds xmlns:dgm="{uri}" r:dm="rObject" r:lo="rLayout" r:qs="rStyle" r:cs="rColors"/>'
        target, object_type = "diagrams/data1.xml", "diagramData"
        data = f'<dgm:dataModel xmlns:dgm="{uri}" xmlns:a="{a}"><dgm:ptLst><dgm:pt modelId="0" type="doc"/><dgm:pt modelId="1"><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Approval workflow</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst><dgm:cxnLst/><dgm:bg/><dgm:whole/></dgm:dataModel>'
    files = {
        "[Content_Types].xml": f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/><Override PartName="/ppt/{target}" ContentType="application/vnd.openxmlformats-officedocument.drawingml.{object_type}+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{rel}"><Relationship Id="rPresentation" Type="{r}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="rSlide"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{rel}"><Relationship Id="rSlide" Type="{r}/slide" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{r}"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Annual report</a:t></a:r></a:p></p:txBody></p:sp><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="Object"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/><a:ext cx="4000000" cy="3000000"/></p:xfrm><a:graphic><a:graphicData uri="{uri}">{object_xml}</a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{rel}"><Relationship Id="rObject" Type="{r}/{object_type}" Target="../{target}"/></Relationships>',
        "ppt/" + target: data,
    }
    if kind == "smartart":
        for name, relation_type, element in [
            ("layout1", "diagramLayout", "layoutDef"),
            ("style1", "diagramQuickStyle", "styleDef"),
            ("colors1", "diagramColors", "colorsDef"),
        ]:
            files["ppt/diagrams/" + name + ".xml"] = (
                f'<dgm:{element} xmlns:dgm="{uri}" uniqueId="urn:synthetic:{name}"/>'
            )
            rid = {"layout1": "rLayout", "style1": "rStyle", "colors1": "rColors"}[name]
            files["ppt/slides/_rels/slide1.xml.rels"] = files[
                "ppt/slides/_rels/slide1.xml.rels"
            ].replace(
                "</Relationships>",
                f'<Relationship Id="{rid}" Type="{r}/{relation_type}" Target="../diagrams/{name}.xml"/></Relationships>',
            )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


@pytest.mark.parametrize("kind", ["chart", "smartart"])
@pytest.mark.parametrize("recovery", ["disabled", "timeout", "empty", "available"])
def test_real_pptx_objects_are_partial_and_attempt_approved_recovery(monkeypatch, kind, recovery):
    calls = []

    class Remote:
        def parse(self, path):
            calls.append(path)
            with zipfile.ZipFile(path) as archive:
                assert any(
                    name.startswith("ppt/charts/") or name.startswith("ppt/diagrams/")
                    for name in archive.namelist()
                )
            if recovery == "timeout":
                raise TimeoutError("synthetic unavailable")
            return ParsedDocument(
                content="Recovered object: 123" if recovery == "available" else "",
                metadata={},
                format="pptx",
                source_path=path,
            )

    monkeypatch.setattr(
        ParserRegistry,
        "_get_docling_parser",
        classmethod(lambda cls: None if recovery == "disabled" else Remote()),
    )
    text, metadata, _ = extract_text("objects.pptx", presentation_with_object(kind))
    assert "Annual report" in text
    assert metadata["unsupported_objects"] == [{"slide": 1, "kind": kind}]
    assert metadata["quality"] == "partial"
    assert "SmartArt" in metadata["quality_notice"]
    assert bool(calls) == (recovery != "disabled")
    if recovery == "available":
        assert "Recovered object: 123" in text
        assert metadata["actual_parser"] == "pptx-native+docling"
    else:
        assert metadata["actual_parser"] == "pptx-native"
    chunk = get_chunker("fixed").chunk(text, metadata, {})[0]
    assert chunk.metadata["quality"] == "partial"
    assert chunk.metadata["unsupported_objects"][0]["kind"] == kind


@pytest.mark.parametrize("recover", [False, True])
def test_chart_only_presentation_attempts_recovery_before_empty_failure(monkeypatch, recover):
    original = presentation_with_object("chart")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            body = source.read(name)
            if name == "ppt/slides/slide1.xml":
                body = body.replace(b"Annual report", b"")
            target.writestr(name, body)

    class Remote:
        def parse(self, path):
            return ParsedDocument(
                content="Chart budget 123", metadata={}, format="pptx", source_path=path
            )

    monkeypatch.setattr(
        ParserRegistry,
        "_get_docling_parser",
        classmethod(lambda cls: Remote() if recover else None),
    )
    if recover:
        text, metadata, _ = extract_text("chart-only.pptx", buffer.getvalue())
        assert "Chart budget 123" in text
        assert metadata["quality"] == "partial"
    else:
        with pytest.raises(ParseError) as error:
            extract_text("chart-only.pptx", buffer.getvalue())
        assert error.value.details["reason"] == "presentation_objects_unavailable"


def test_remote_unpaged_presentation_does_not_invent_slide_citations(monkeypatch):
    original = presentation_with_object("chart")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            body = source.read(name)
            if name == "ppt/presentation.xml":
                body = body.replace(
                    b"</p:sldIdLst>", b'<p:sldId id="257" r:id="rOther"/></p:sldIdLst>'
                )
            elif name == "ppt/_rels/presentation.xml.rels":
                body = body.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rOther" Target="slides/slide2.xml"/></Relationships>',
                )
            target.writestr(name, body)
        target.writestr(
            "ppt/slides/slide2.xml",
            source.read("ppt/slides/slide1.xml").replace(b"Annual report", b"Second page"),
        )
        target.writestr(
            "ppt/slides/_rels/slide2.xml.rels", source.read("ppt/slides/_rels/slide1.xml.rels")
        )

    class Remote:
        def parse(self, path):
            return ParsedDocument(
                content="Chart text without known slide location",
                metadata={"pages": 2},
                format="pptx",
                source_path=path,
            )

    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: Remote()))
    text, metadata, _ = extract_text("two-slides.pptx", buffer.getvalue())
    assert text.split("\f") == ["Annual report", "Second page"]
    assert metadata["presentation_recovery"] == "remote_slide_locations_unknown"
    assert metadata["quality"] == "partial"
