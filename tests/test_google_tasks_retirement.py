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
        "bot/ui/task_card.py",
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
    bootstrap = _read("bot/bootstrap.py")
    deps = _read("bot/deps.py")

    assert 'os.getenv("GOOGLE_CLIENT_ID"' not in runtime
    assert 'os.getenv("GOOGLE_CLIENT_SECRET"' not in runtime
    assert 'os.getenv("GOOGLE_REFRESH_TOKEN"' not in runtime
    assert "gtasks" not in runtime
    assert "gtasks" not in lifecycle
    assert "GoogleTasksAdapter" not in bootstrap
    assert "gtasks" not in bootstrap
    assert "GoogleTasksAdapter" not in deps
    assert "gtasks" not in deps


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


def test_retired_google_tasks_files_are_removed():
    retired = [
        "bot/adapters/google_tasks_adapter.py",
        "bot/services/gtasks_service.py",
        "scripts/get_google_refresh_token.py",
        "scripts/get_google_refresh_token.exe",
        "scripts/get_google_refresh_token.dist",
        "tests/test_google_tasks_adapter.py",
    ]
    for relative in retired:
        assert not (ROOT / relative).exists(), relative
