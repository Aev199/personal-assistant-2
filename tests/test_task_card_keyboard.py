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
        self.assertEqual(rows, [["task:10:done", "task:10:dl", "task:10:more"], ["nav:inbox:0"]])

    def test_expanded_card_exposes_only_common_edits(self) -> None:
        kb = task_card_kb(
            10,
            20,
            30,
            "todo",
            expanded=True,
            return_cb="nav:work:0",
            return_label="back",
        )

        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        for action in ("move", "assignee", "postpone", "less"):
            self.assertIn(f"task:10:{action}", callbacks)
        for action in ("in_progress", "subtasks", "relations", "gtasks"):
            self.assertNotIn(f"task:10:{action}", callbacks)
        self.assertIn("nav:work:0", callbacks)

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
