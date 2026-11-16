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
from reportlab.platypus import Paragraph, SimpleDocTemplate


FONT = "ResearchKorean"
FONT_BOLD = "ResearchKoreanBold"
INLINE = re.compile(r"\[([^\]]+)\]\(<(https?://[^<>\s]+)>\)|\[([^\]]+)\]\((evidence\.json)\)|\*\*([^*]+)\*\*|`([^`]+)`")
NAVY = colors.HexColor("#16283f")
INK = colors.HexColor("#27394a")
BLUE = colors.HexColor("#185c88")
MUTED = colors.HexColor("#526779")
LINE = colors.HexColor("#d9e3ec")


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
    return "".join(fragments)


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
    }
    title = markdown.splitlines()[0].removeprefix("# ")
    story = []
    for line in markdown.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^(#{1,3}|  -|-) (.*)", line)
        if match:
            prefix, content = match.groups()
            markup = ("• " if prefix.endswith("-") else "") + paragraph_markup(content)
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

    document = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=52, rightMargin=52, topMargin=50,
                                 bottomMargin=59, title=title)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
