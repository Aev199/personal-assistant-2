import unittest

from bot.handlers.onboarding import (
    _base_project_code,
    _deserialize_intents,
    _serialize_intents,
    _suggested_project_names,
)
from bot.services.freeform_intake import IntakeIntent, ProjectOption


class OnboardingHelpersTests(unittest.TestCase):
    def test_suggests_repeated_new_project_only_once(self):
        intents = [
            IntakeIntent(action="task", title="Проверить сетку", project_name="Конвертер ЛИРА"),
            IntakeIntent(action="task", title="Починить экспорт", project_name="Конвертер ЛИРА"),
            IntakeIntent(action="personal_task", title="Купить кофе", project_name="Конвертер ЛИРА"),
        ]
        self.assertEqual(_suggested_project_names(intents, []), ["Конвертер ЛИРА"])

    def test_existing_project_is_not_suggested(self):
        projects = [ProjectOption(id=7, code="LIRA", name="Конвертер ЛИРА")]
        intents = [IntakeIntent(action="task", title="Проверить экспорт", project_name="Конвертер ЛИРА")]
        self.assertEqual(_suggested_project_names(intents, projects), [])

    def test_project_code_is_stable_and_ascii(self):
        self.assertEqual(_base_project_code("Конвертер ЛИРА"), "KL")
        self.assertEqual(_base_project_code("Geobase"), "GEOBASE")

    def test_intents_survive_fsm_serialization(self):
        original = [
            IntakeIntent(
                action="reminder",
                title="",
                reminder_text="Позвонить врачу",
                remind_at_local="2026-08-01 10:00",
                missing_fields=(),
            )
        ]
        restored = _deserialize_intents(_serialize_intents(original))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].reminder_text, "Позвонить врачу")
        self.assertEqual(restored[0].missing_fields, ())


if __name__ == "__main__":
    unittest.main()
