"""本地 xlsx 讀取。合成的社團經費表，非真實資料。"""

from __future__ import annotations

import io
import zipfile

from anila_core.ingestion.parser_registry import ParserRegistry, XlsxParser

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx() -> bytes:
    workbook = (
        f'<workbook xmlns="{NS}" xmlns:r="{R}"><sheets>'
        '<sheet name="經費" sheetId="1" r:id="rId1"/>'
        '<sheet name="備註" sheetId="2" state="hidden" r:id="rId2"/>'
        "</sheets></workbook>"
    )
    rels = (
        f'<Relationships xmlns="{PR}">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
        '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml" Type="x"/>'
        "</Relationships>"
    )
    shared = f'<sst xmlns="{NS}"><si><t>項目</t></si><si><t>金額</t></si><si><r><t>點</t></r><r><t>心</t></r></si></sst>'
    sheet1 = (
        f'<worksheet xmlns="{NS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>1200.5</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>合計|含稅</t></is></c>'
        '<c r="B3"><f>SUM(B2:C2)</f><v>1200.5</v></c><c r="C3" t="b"><v>1</v></c></row>'
        "</sheetData></worksheet>"
    )
    sheet2 = f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="B1" t="str"><v>僅供參考</v></c></row></sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", rels)
        package.writestr("xl/sharedStrings.xml", shared)
        package.writestr("xl/worksheets/sheet1.xml", sheet1)
        package.writestr("xl/worksheets/sheet2.xml", sheet2)
    return out.getvalue()


def test_reads_shared_inline_formula_and_hidden_sheet(tmp_path):
    path = tmp_path / "經費表.xlsx"
    path.write_bytes(_xlsx())
    parsed = XlsxParser().parse(str(path))
    text = parsed.content
    assert parsed.format == "xlsx"
    assert parsed.metadata["sheets"] == ["經費", "備註"]
    assert "## 工作表：經費" in text
    assert "## 工作表：備註（隱藏）" in text
    assert "| 項目 | 金額 |  |" in text
    assert "| 點心 |  | 1200.5 |" in text  # 依欄位字母對齊，空的 B2 留空
    assert "合計\\|含稅" in text  # 儲存格裡的 | 不會切壞表格
    assert "| 1200.5 | TRUE |" in text  # 公式取存檔時的結果
    assert "| 僅供參考 |" in text


def test_registry_reads_xlsx_locally_even_when_docling_is_registered(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: sentinel))
    assert isinstance(ParserRegistry.get("a.xlsx"), XlsxParser)
    assert ParserRegistry.get("a.docx") is sentinel
