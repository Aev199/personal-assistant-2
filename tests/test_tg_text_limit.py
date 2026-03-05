import unittest

from bot.utils.telegram import TG_TEXT_SAFE_LIMIT_UNITS, _tg_utf16_units, fit_telegram_text


class TelegramTextLimitTests(unittest.TestCase):
    def test_plain_compacts_by_lines_and_fits_limit(self) -> None:
        text = "\n".join([f"line {i} " + ("x" * 50) for i in range(250)])
        out = fit_telegram_text(text, parse_mode=None)
        self.assertLessEqual(_tg_utf16_units(out), TG_TEXT_SAFE_LIMIT_UNITS)
        self.assertIn("ещё", out)
        self.assertIn("строк", out)

    def test_html_compacts_and_uses_italic_suffix(self) -> None:
        text = "\n".join([f"<b>line {i}</b> " + ("x" * 50) for i in range(250)])
        out = fit_telegram_text(text, parse_mode="HTML")
        self.assertLessEqual(_tg_utf16_units(out), TG_TEXT_SAFE_LIMIT_UNITS)
        self.assertIn("<i>… и ещё", out)
        self.assertIn("</i>", out)

    def test_first_line_too_long_strips_tags_and_trims(self) -> None:
        text = "<b>" + ("x" * 10000) + "</b>"
        out = fit_telegram_text(text, parse_mode="HTML")
        self.assertLessEqual(_tg_utf16_units(out), TG_TEXT_SAFE_LIMIT_UNITS)
        self.assertNotIn("<b>", out)


if __name__ == "__main__":
    unittest.main()

