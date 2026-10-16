# 文件結構與離線工程證據

本文說明 core 文件解析、結構切塊與合成樣本量測。合成樣本及單機量測不代表院內驗收、出貨映像就緒、OCR 辨識品質或供應商模型容量已通過。

## DOCX 頁界來源

`ingestion/docx_locations.py` 記錄本文與表格中直接存在的 OOXML 標記：`w:br[type=page]`、`w:pageBreakBefore`、`w:lastRenderedPageBreak`，以及明確標示 `nextPage`／`oddPage`／`evenPage` 的節界。標記只證明文件內有該宣告或快取；不提供 `page`、`pages`、`page_count` 或 `total_pages`。

`page_location_source=docx_ooxml_markers`；`page_location_quality` 區分宣告標記、未驗證排版快取、兩者混合與未知。`page_location_notice` 說明限制並沿各切塊策略傳遞。`pdf-page` 策略處理 DOCX 或本機 DOCX 轉檔來源時採無頁碼切塊，避免把未排版 Word 文字稱為「第 1 頁／共 1 頁」。真實 PDF 頁碼沿既有 PDF 路徑處理。

文件層的 `docx_page_boundary_evidence` 最多 128 筆，完整計數保留在 `docx_page_marker_counts`；超出時標示 `docx_page_boundary_evidence_truncated`。每筆包含來源與信任限制、零起算的已輸出本文區塊／表格列欄／段落座標，以及僅計 `w:t` 的字元位置。`body_block` 是解析器已輸出的段落／表格順序，不是原始 XML 子節點序號；字元位置也不是 Markdown offset。切塊僅攜帶小型計數與來源／限制，不重複攜帶整份 evidence。

樣式繼承、自動換頁、未寫明類型的節界與省略的包裝內容不據此推算實體頁碼。合成 60 段 DOCX 經現有 LibreOffice 轉出 12 頁 PDF，首尾段落均保留；原 DOCX 解析仍標未知，這是分開驗證排版與來源的工程證據，不是任意 Word 文件的頁碼保證。

## C／C++ 函式邊界

`1'000`、`0xCA'FE` 等 C++ 數字分隔符屬於 preprocessing-number 邊界，與字元常值分開辨識，不能吞過下一個函式。來源只作靜態解析，不執行程式，也不宣稱完整編譯語意。

數值分隔符、字元常值對照與既有 75 個複雜／保守邊界案例均有回歸。合成 400／2000 個函式分別保留 400／2000 個區塊，每一來源行恰好出現一次；2000 函式檔共 74,890 bytes，超過 16 KiB 的整檔仍可依小函式分塊。400 函式樣本另由既有 g++ 以 C++14 `-fsyntax-only` 通過，未執行程式。巨集、函式 try-block、無法確認的宣告及掃描上限仍沿既有保守品質與原因；單一完整結構超過 16 KiB UTF-8 仍明確拒絕。

## 合成樣本與量測

從 `/tmp` 執行，輸出目錄須為全新的 `/tmp` 子目錄：

```bash
env -i PATH=/home/c1147259/anaconda3/bin:/usr/bin:/bin LANG=C.UTF-8 python3 -I -S -B /path/to/ANILA/tools/benchmarks/document_structure.py --output /tmp/anila-document-evidence-new
```

工具在相依套件／應用程式匯入前安裝檔案與網路 guard，不允許子程序或模型呼叫。使用既有 python-docx、PyMuPDF 產生 DOCX、三頁實體 PDF、繁中掃描 PNG、預期文字及 C++；量測逐案例捕捉明確錯誤，其餘案例繼續。掃描 PNG 使用已安裝 PyMuPDF 的內建 CJK 字型，沒有取得新字型或模型。

單機合成樣本量測：標記 DOCX 約 11 ms，自動換頁 DOCX 約 117 ms，400 函式約 76 ms，2000 函式約 370 ms；逐案例 Python allocation peak 約 1.0–5.5 MB，程序累積 RSS peak 約 95 MiB。`tracemalloc` 不包含所有原生庫配置；RSS 是程序累積峰值，不能當成逐檔淨用量。單次合成小樣本不提供正式吞吐量、尖峰容量或安全上限的推論。

## OCR 與工具缺口

固定工具盤點可用 LibreOffice 7.3.7.2、Poppler 22.02.0、g++ 11.4.0；既有 Python 庫包含 python-docx 1.2.0、PyMuPDF 1.26.7、pymupdf4llm 0.2.9、Pillow 10.3.0、lxml 6.0.2。這是工程環境觀察，不是 worker 映像 SBOM；本機 docling／odfpy 未提供。

Tesseract 執行檔及標準位置的 `chi_tra`、`eng` 語料缺少時，真實繁中 OCR 無法驗證。合成掃描樣本會得到 `E_PARSE_BAD_CONFIG`／`offline_ocr_missing`，沒有假成功或院外 fallback。取得新工具或官方資產前，須完成並核准版本、來源、授權、作者國別及離線出貨計畫。
