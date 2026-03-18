import unittest

from bot.ui.task_card import task_card_kb


class TaskCardKeyboardTests(unittest.TestCase):
    def test_primary_inbox_card_prioritizes_daily_actions(self) -> None:
        kb = task_card_kb(
            10,
            20,
            None,
            "todo",
            is_inbox=True,
            expanded=False,
            return_cb="nav:inbox:0",
            return_label="⬅ Inbox",
        )

        rows = [[btn.text for btn in row] for row in kb.inline_keyboard]
        self.assertEqual(rows[0], ["✅ Готово", "🗓 Срок"])
        self.assertEqual(rows[1], ["📁 В проект…", "⚡ В работу"])
        self.assertEqual(rows[2], ["⋯ Ещё"])
        self.assertEqual(rows[3], ["⬅ Inbox", "⬅️ Домой"])

    def test_expanded_card_moves_structural_actions_out_of_primary_layer(self) -> None:
        kb = task_card_kb(
            10,
            20,
            30,
            "todo",
            expanded=True,
            return_cb="nav:work:0",
            return_label="⬅ В работе",
        )

        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("👤 Исполнитель", labels)
        self.assertIn("⏸ Отложить", labels)
        self.assertIn("📁 В проект…", labels)
        self.assertIn("🧩 Суперзадача…", labels)


if __name__ == "__main__":
    unittest.main()
