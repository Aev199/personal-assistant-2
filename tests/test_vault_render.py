import unittest

from bot.services.vault_manager import VaultManager


class VaultRenderTests(unittest.TestCase):
    def test_super_task_is_marker_not_checkbox(self) -> None:
        vault = VaultManager(cloud_adapter=object(), tz="UTC")
        md = vault._render_tasks_tree(
            [
                {
                    "id": 1,
                    "title": "Epic",
                    "assignee": "—",
                    "status": "todo",
                    "deadline": None,
                    "parent_task_id": None,
                    "kind": "super",
                },
                {
                    "id": 2,
                    "title": "Child",
                    "assignee": "Alice",
                    "status": "todo",
                    "deadline": None,
                    "parent_task_id": 1,
                    "kind": "task",
                },
            ]
        )

        self.assertIn("- 🧩 Epic (ID: 1)", md)
        self.assertIn("- [ ] Alice: Child (ID: 2)", md)

        for line in md.splitlines():
            if "🧩 Epic" in line:
                self.assertNotIn("- [", line)


if __name__ == "__main__":
    unittest.main()

