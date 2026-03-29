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
            return_label="back",
        )

        rows = [[btn.callback_data for btn in row] for row in kb.inline_keyboard]
        self.assertEqual(rows[0], ["task:10:done", "task:10:dl"])
        self.assertEqual(rows[1], ["task:10:move", "task:10:in_progress"])
        self.assertEqual(rows[2], ["task:10:more"])
        self.assertEqual(rows[3], ["nav:inbox:0", "nav:home"])

    def test_expanded_card_keeps_secondary_layer_compact(self) -> None:
        kb = task_card_kb(
            10,
            20,
            30,
            "todo",
            expanded=True,
            return_cb="nav:work:0",
            return_label="back",
        )

        rows = [[btn.callback_data for btn in row] for row in kb.inline_keyboard]
        self.assertEqual(rows[0], ["task:10:subtasks", "task:10:less"])
        self.assertEqual(rows[1], ["task:10:relations"])
        self.assertEqual(rows[2], ["task:10:assignee", "task:10:postpone"])
        self.assertEqual(rows[3], ["nav:work:0", "nav:home"])

        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertNotIn("task:10:move", callbacks)
        self.assertNotIn("task:10:to_super", callbacks)
        self.assertNotIn("task:10:detach", callbacks)
        self.assertNotIn("task:10:g_sync", callbacks)

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

        primary_callbacks = [btn.callback_data for row in primary.inline_keyboard for btn in row]
        expanded_callbacks = [btn.callback_data for row in expanded.inline_keyboard for btn in row]
        self.assertNotIn("task:10:assignee", primary_callbacks)
        self.assertNotIn("task:10:assignee", expanded_callbacks)
        self.assertIn("task:10:postpone", expanded_callbacks)


if __name__ == "__main__":
    unittest.main()
