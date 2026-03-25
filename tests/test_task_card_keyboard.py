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

    def test_expanded_card_keeps_secondary_layer_compact(self) -> None:
        kb = task_card_kb(
            10,
            20,
            30,
            "todo",
            expanded=True,
            return_cb="nav:work:0",
            return_label="⬅ В работе",
        )

        rows = [[btn.text for btn in row] for row in kb.inline_keyboard]
        self.assertEqual(rows[0], ["⬅ В работе", "⬅️ Домой"])
        self.assertEqual(rows[1], ["🧩 Связи…", "↳ Подзадачи…"])
        self.assertEqual(rows[2], ["👤 Исполнитель", "⏸ Отложить"])
        self.assertEqual(rows[3], ["⋯ Свернуть"])

        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertNotIn("📁 В проект…", labels)
        self.assertNotIn("🧩 В суперзадачу…", labels)
        self.assertNotIn("⛓ Отвязать", labels)
        self.assertNotIn("📤 В Google Tasks", labels)

    def test_solo_card_hides_assignee_actions(self) -> None:
        primary = task_card_kb(
            10,
            20,
            None,
            "todo",
            expanded=False,
            persona_mode="solo",
        )
        expanded = task_card_kb(
            10,
            20,
            None,
            "todo",
            expanded=True,
            persona_mode="solo",
        )

        primary_labels = [btn.text for row in primary.inline_keyboard for btn in row]
        expanded_labels = [btn.text for row in expanded.inline_keyboard for btn in row]
        self.assertNotIn("👤 Исп.", primary_labels)
        self.assertNotIn("👤 Исполнитель", expanded_labels)
        self.assertIn("⏸ Отложить", expanded_labels)


if __name__ == "__main__":
    unittest.main()
