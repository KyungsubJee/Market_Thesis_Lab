from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from pypdf import PdfReader
except ModuleNotFoundError:
    PdfReader = None
try:
    from research_prototype.report_pdf import appendix_table, paragraph_markup, register_fonts, write_pdf, FONT, MISSING
    from reportlab.lib.styles import ParagraphStyle
except ModuleNotFoundError as error:
    if error.name != "reportlab":
        raise
    paragraph_markup = write_pdf = None


@unittest.skipIf(paragraph_markup is None, "PDF renderer requires reportlab")
class PdfSignTests(unittest.TestCase):
    def test_business_gap_widths_keep_seven_rows_compact_without_changing_legacy(self):
        register_fonts()
        body = ParagraphStyle("gap-test", fontName=FONT, wordWrap="CJK")
        note = "분기 영업이익률과 확인된 최신 분기 실적을 확인하지 못했습니다. 자료 확인 전 중기 후보 선정은 보류합니다."
        rows = [["중요도", "자료·분석기간", "수집 상태", "판단 영향"],
                *[["필수", "LLY · 중기 · 1~2분기", "**미확보**", note] for _ in range(7)]]
        table = appendix_table(rows, body, 480)
        self.assertEqual(table._colWidths, [480 * part for part in (0.08, 0.22, 0.12, 0.58)])
        self.assertEqual(table._cellvalues[1][2].style.textColor, MISSING)
        self.assertLess(table.wrap(480, 700)[1], 420)
        legacy = appendix_table([["중요도", "자료", "수집 상태", "기준·참고"]], body, 480)
        self.assertEqual(legacy._colWidths, [480 * part for part in (0.23, 0.34, 0.25, 0.18)])

    def test_negative_sign_survives_plain_bold_literal_and_entity_markup(self):
        markup = paragraph_markup("SPY −2.33% · **−4.76%** · `−3배` · &#8722;10.52%")
        self.assertEqual(markup, "SPY -2.33% · <b>-4.76%</b> · -3배 · -10.52%")

    @unittest.skipIf(PdfReader is None, "PDF extraction check requires optional pypdf")
    def test_rendered_pdf_retains_negative_returns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sign-regression.pdf"
            write_pdf(path, "# 수익률 검증\n\nSPY −2.33% · **−4.76%**\n\nKOSPI −10.52%")
            text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
        for value in ("-2.33%", "-4.76%", "-10.52%"):
            self.assertIn(value, text)
        self.assertNotIn("\x00", text)


if __name__ == "__main__":
    unittest.main()
