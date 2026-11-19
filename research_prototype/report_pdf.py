"""Render the prototype's Markdown reports with embedded Korean type and links."""

from html import escape, unescape
import os
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

from .coverage import APPENDIX_TABLE_HEADER, CAUTION_STATUS, GAP_STATUS, STATUS


FONT = "ResearchKorean"
FONT_BOLD = "ResearchKoreanBold"
INLINE = re.compile(r"\[([^\]]+)\]\(<(https?://[^<>\s]+)>\)|\[([^\]]+)\]\((evidence\.json)\)|\*\*([^*]+)\*\*|`([^`]+)`")
NAVY = colors.HexColor("#16283f")
INK = colors.HexColor("#27394a")
BLUE = colors.HexColor("#185c88")
MUTED = colors.HexColor("#526779")
LINE = colors.HexColor("#d9e3ec")
MISSING = colors.HexColor("#a21b27")
CAUTION = colors.HexColor("#835308")


def register_fonts():
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    configured = os.environ.get("RESEARCH_PDF_FONT")
    options = [Path(configured)] if configured else [
        windows / "malgun.ttf", Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    ]
    regular = next((path for path in options if path.is_file()), None)
    if regular is None:
        raise ValueError("Install a Korean TrueType font or set RESEARCH_PDF_FONT to its path.")
    bold = regular.with_name({"malgun.ttf": "malgunbd.ttf", "NanumGothic.ttf": "NanumGothicBold.ttf"}.get(
        regular.name, regular.name,
    ))
    pdfmetrics.registerFont(TTFont(FONT, str(regular)))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold if bold.is_file() else regular)))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)


def paragraph_markup(line):
    fragments = []
    start = 0
    for match in INLINE.finditer(line):
        fragments.append(escape(unescape(line[start:match.start()])))
        source, url, local_label, local_url, bold, literal = match.groups()
        if url:
            fragments.append(f'<link href="{escape(unescape(url), quote=True)}" color="#185c88">'
                             f'{escape(unescape(source))}</link>')
        elif local_url:
            fragments.append(escape(f"{local_label} ({local_url})"))
        elif bold:
            fragments.append(f"<b>{escape(unescape(bold))}</b>")
        else:
            fragments.append(escape(unescape(literal)))
        start = match.end()
    fragments.append(escape(unescape(line[start:])))
    # The embedded Korean font can omit U+2212, making losses look positive.
    return "".join(fragments).replace("\u2212", "-")


def appendix_table(rows, body, width):
    cell = ParagraphStyle("table-cell", parent=body, fontSize=9.3, leading=12.4, spaceAfter=0)
    header = ParagraphStyle("table-header", parent=cell, fontName=FONT_BOLD, textColor=colors.white)
    gap = ParagraphStyle("table-gap", parent=cell, textColor=MISSING)
    caution = ParagraphStyle("table-caution", parent=cell, textColor=CAUTION)
    gap_labels = {STATUS[status] for status in GAP_STATUS}
    caution_labels = {STATUS[status] for status in CAUTION_STATUS}
    cells = []
    for index, row in enumerate(rows):
        paragraphs = []
        for column, value in enumerate(row):
            style = header if index == 0 else cell
            if index > 0 and column == 2:
                label = value.strip("*")
                if label in gap_labels:
                    style = gap
                elif label in caution_labels:
                    style = caution
            paragraphs.append(Paragraph(paragraph_markup(value), style))
        cells.append(paragraphs)
    widths_by_header = {
        ("종목", "검토 단계", "결과 구분", "핵심 이유"): (0.27, 0.13, 0.16, 0.44),
        ("중요도", "자료·분석기간", "수집 상태", "판단 영향"): (0.08, 0.22, 0.12, 0.58),
    }
    proportions = widths_by_header.get(tuple(rows[0]) if rows else (), (0.23, 0.34, 0.25, 0.18))
    table = Table(cells, colWidths=[width * part for part in proportions],
                  repeatRows=1, hAlign="LEFT", spaceAfter=8)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f7fb")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def write_pdf(path, markdown):
    register_fonts()
    body = ParagraphStyle("body", fontName=FONT, fontSize=10.4, leading=15.6, textColor=INK, spaceAfter=7,
                          wordWrap="CJK", alignment=TA_LEFT, allowWidows=0, allowOrphans=0)
    styles = {
        "#": ParagraphStyle("title", parent=body, fontName=FONT_BOLD, fontSize=22, leading=28,
                            textColor=NAVY, spaceAfter=16, keepWithNext=True),
        "##": ParagraphStyle("section", parent=body, fontName=FONT_BOLD, fontSize=15.5, leading=21,
                             textColor=NAVY, spaceBefore=13, spaceAfter=8, keepWithNext=True),
        "###": ParagraphStyle("heading", parent=body, fontName=FONT_BOLD, fontSize=11.5, leading=17,
                              textColor=BLUE, spaceBefore=8, spaceAfter=5, keepWithNext=True),
        "-": ParagraphStyle("bullet", parent=body, leftIndent=12, firstLineIndent=-12),
        "  -": ParagraphStyle("nested", parent=body, leftIndent=22, firstLineIndent=-12),
        ">": ParagraphStyle("decision", parent=body, backColor=colors.HexColor("#edf4f9"), borderPadding=7,
                            leftIndent=7, textColor=NAVY, spaceBefore=3, spaceAfter=8),
    }
    decision_header = ParagraphStyle("decision-header", parent=styles[">"], keepWithNext=True)
    lines = markdown.splitlines()
    title = lines[0].removeprefix("# ")
    document = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=52, rightMargin=52, topMargin=50,
                                 bottomMargin=59, title=title)
    story = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
            continue
        if (line in {APPENDIX_TABLE_HEADER, "| 종목 | 검토 단계 | 결과 구분 | 핵심 이유 |",
                     "| 중요도 | 자료·분석기간 | 수집 상태 | 판단 영향 |"}
                and index < len(lines) and lines[index] == "| --- | --- | --- | --- |"):
            raw_rows = [line]
            index += 1
            while index < len(lines) and lines[index].startswith("| ") and lines[index].endswith(" |"):
                raw_rows.append(lines[index])
                index += 1
            rows = [[cell.strip() for cell in row.strip("|").split("|")] for row in raw_rows]
            story.append(appendix_table(rows, body, document.width - 12))
            continue
        match = re.match(r"^(#{1,3}|  -|-|>) (.*)", line)
        if match:
            prefix, content = match.groups()
            markup = ("• " if prefix.endswith("-") else "") + paragraph_markup(content)
            if prefix == ">" and content.startswith("**") and index < len(lines) and lines[index].startswith("> "):
                story.append(Paragraph(markup, decision_header))
            else:
                story.append(Paragraph(markup, styles[prefix]))
        else:
            story.append(Paragraph(paragraph_markup(line), body))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(document.leftMargin, 42, A4[0] - document.rightMargin, 42)
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 8.3)
        canvas.drawString(document.leftMargin, 28, title)
        canvas.drawRightString(A4[0] - document.rightMargin, 28, str(document.page))
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
