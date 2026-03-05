import unittest

from bot.handlers.tasks import _task_return_context


class TaskReturnContextTests(unittest.TestCase):
    def test_all_tasks_with_filter_and_page(self) -> None:
        cb, label = _task_return_context("all_tasks", {"filter": "today", "page": 2})
        self.assertEqual(cb, "nav:all:today:2")
        self.assertEqual(label, "⬅ Все задачи")

    def test_all_tasks_legacy_payload_without_filter(self) -> None:
        cb, label = _task_return_context("all_tasks", {"page": 3})
        self.assertEqual(cb, "nav:all:3")
        self.assertEqual(label, "⬅ Все задачи")

    def test_all_tasks_invalid_filter_falls_back_to_legacy(self) -> None:
        cb, label = _task_return_context("all_tasks", {"filter": "weird", "page": 5})
        self.assertEqual(cb, "nav:all:5")
        self.assertEqual(label, "⬅ Все задачи")

    def test_all_tasks_non_numeric_page_normalized_to_zero(self) -> None:
        cb, _ = _task_return_context("all_tasks", {"filter": "overdue", "page": "bad"})
        self.assertEqual(cb, "nav:all:overdue:0")

    def test_unknown_screen_returns_none(self) -> None:
        cb, label = _task_return_context("unknown", {})
        self.assertIsNone(cb)
        self.assertIsNone(label)


if __name__ == "__main__":
    unittest.main()
