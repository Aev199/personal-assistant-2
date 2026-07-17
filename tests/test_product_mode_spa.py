import unittest
from unittest.mock import AsyncMock, patch

from bot.services.product_mode_spa import _replace_spa_anchor


class ProductModeSpaTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_message_replaces_spa_anchor(self) -> None:
        render = AsyncMock(return_value=321)
        pool = object()
        bot = object()
        markup = object()

        with patch("bot.services.product_mode_spa.ui_render", render):
            message_id = await _replace_spa_anchor(
                pool,
                bot=bot,
                chat_id=123,
                text="morning",
                reply_markup=markup,
            )

        self.assertEqual(message_id, 321)
        render.assert_awaited_once_with(
            bot=bot,
            db_pool=pool,
            chat_id=123,
            text="morning",
            reply_markup=markup,
            screen=None,
            payload=None,
            force_new=True,
            parse_mode="HTML",
        )

    async def test_failed_anchor_replacement_is_not_marked_as_sent(self) -> None:
        with patch(
            "bot.services.product_mode_spa.ui_render",
            AsyncMock(return_value=0),
        ):
            with self.assertRaises(RuntimeError):
                await _replace_spa_anchor(
                    object(),
                    bot=object(),
                    chat_id=123,
                    text="morning",
                    reply_markup=object(),
                )


if __name__ == "__main__":
    unittest.main()
