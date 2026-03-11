import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.freeform_intake import _render_screen


class FreeformRenderScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_screen_does_not_pass_tz_name(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=11))
        deps = SimpleNamespace(tz_name="Europe/Moscow")

        with patch("bot.services.freeform_intake.ui_render_help", AsyncMock(return_value=777)) as render_help:
            result = await _render_screen(message, db_pool=SimpleNamespace(), deps=deps, screen="help", payload={})

        self.assertEqual(result, 777)
        self.assertEqual(render_help.await_count, 1)
        self.assertNotIn("tz_name", render_help.await_args.kwargs)

    async def test_projects_screen_passes_tz_name(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=22))
        deps = SimpleNamespace(tz_name="Europe/Moscow")

        with patch(
            "bot.services.freeform_intake.ui_render_projects_portfolio",
            AsyncMock(return_value=778),
        ) as render_projects:
            result = await _render_screen(message, db_pool=SimpleNamespace(), deps=deps, screen="projects", payload={})

        self.assertEqual(result, 778)
        self.assertEqual(render_projects.await_count, 1)
        self.assertIn("tz_name", render_projects.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
