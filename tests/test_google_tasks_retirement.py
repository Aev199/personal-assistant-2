from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_google_tasks_is_not_in_active_capture_or_mutation_paths():
    active_paths = [
        "bot/services/pending_actions.py",
        "bot/services/freeform_intake.py",
        "bot/handlers/wizards.py",
        "bot/handlers/system.py",
        "bot/handlers/tasks.py",
    ]
    for path in active_paths:
        source = _read(path)
        assert "gtasks.create_task(" not in source, path
        assert "gtasks.patch_task(" not in source, path
        assert "gtasks.delete_task(" not in source, path
        assert "get_or_create_list_id(" not in source, path


def test_legacy_google_credentials_cannot_activate_runtime():
    runtime = _read("bot/runtime.py")
    lifecycle = _read("bot/lifecycle.py")

    assert 'os.getenv("GOOGLE_CLIENT_ID"' not in runtime
    assert 'os.getenv("GOOGLE_CLIENT_SECRET"' not in runtime
    assert 'os.getenv("GOOGLE_REFRESH_TOKEN"' not in runtime
    assert "gtasks.startup()" not in lifecycle
    assert "gtasks.close()" not in lifecycle


def test_personal_tasks_and_ideas_are_internal():
    pending = _read("bot/services/pending_actions.py")
    schema = _read("bot/db/schema.py")
    projects = _read("bot/db/projects.py")

    assert "INSERT INTO tasks" in pending
    assert "'personal'" in pending
    assert "INSERT INTO ideas" in pending
    assert "CREATE TABLE IF NOT EXISTS ideas" in schema
    assert "ensure_personal_project_id" in projects
    assert "'PERSONAL'" in projects


def test_google_export_is_not_offered_in_task_relations():
    tasks = _read("bot/handlers/tasks.py")
    assert "📤 В Google Tasks" not in tasks
    assert "🔄 Обновить Google Tasks" not in tasks
