"""Bounded OOXML page-marker evidence, without claiming physical pagination.

body_block counts emitted paragraph/table blocks, not raw XML child indices.
All block/row/cell/paragraph coordinates and source-text offsets are zero-based.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MAX_PAGE_EVIDENCE = 128


@dataclass
class DocxPageEvidence:
    counts: Counter[str] = field(default_factory=Counter)
    events: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, kind: str, source: str, offset: int, location: dict[str, int]) -> None:
        self.counts[kind] += 1
        if len(self.events) < MAX_PAGE_EVIDENCE:
            self.events.append({
                'kind': kind,
                'source': source,
                'confidence': 'cached_layout_not_verified' if kind == 'last_rendered_page_break' else 'declared_break_not_physical_page',
                # Counts source w:t characters only; not an offset in rendered Markdown.
                'source_text_char_offset': offset,
                'location': dict(location),
            })

    def paragraph(self, paragraph: Any, location: dict[str, int]) -> None:
        element = paragraph._p
        before = element.find(f'{_W}pPr/{_W}pageBreakBefore')
        if before is not None and before.get(f'{_W}val', 'true').lower() not in {'false', '0', 'off'}:
            self._record('page_break_before', 'w:pPr/w:pageBreakBefore', 0, location)
        offset = 0
        for child in element.iter():
            if child.tag == f'{_W}t':
                offset += len(child.text or '')
            elif child.tag == f'{_W}br' and child.get(f'{_W}type') == 'page':
                self._record('explicit_page_break', 'w:br[type=page]', offset, location)
            elif child.tag == f'{_W}lastRenderedPageBreak':
                self._record('last_rendered_page_break', 'w:lastRenderedPageBreak', offset, location)
        section = element.find(f'{_W}pPr/{_W}sectPr/{_W}type')
        if section is not None and section.get(f'{_W}val') in {'nextPage', 'oddPage', 'evenPage'}:
            kind = section.get(f'{_W}val')
            self._record('section_page_break', f'w:pPr/w:sectPr/w:type[{kind}]', offset, location)

    def metadata(self) -> dict[str, Any]:
        cached = self.counts['last_rendered_page_break'] > 0
        declared = sum(self.counts.values()) > self.counts['last_rendered_page_break']
        if cached and declared:
            quality = 'mixed_markers_no_physical_pages'
        elif cached:
            quality = 'cached_markers_no_physical_pages'
        elif declared:
            quality = 'declared_breaks_no_physical_pages'
        else:
            quality = 'unknown'
        return {
            'page_location_source': 'docx_ooxml_markers',
            'page_location_quality': quality,
            'page_location_notice': 'DOCX 尚未排版，不提供實體頁碼；只保留本文及表格內的直接頁界標記。lastRenderedPageBreak 是未驗證的排版快取；樣式繼承、自動換頁及省略類型的節界未據此推算頁碼。',
            'docx_page_marker_counts': dict(self.counts),
            'docx_page_boundary_evidence': self.events,
            'docx_page_boundary_evidence_limit': MAX_PAGE_EVIDENCE,
            'docx_page_boundary_evidence_truncated': sum(self.counts.values()) > len(self.events),
        }
