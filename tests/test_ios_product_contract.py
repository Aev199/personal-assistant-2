from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "AssistantPocket"


def _read(relative: str) -> str:
    return (IOS / relative).read_text(encoding="utf-8")


def test_ios_home_stays_attention_first_without_tab_bar():
    source = _read("App/ContentView.swift")

    assert 'Text("Сейчас")' in source
    assert 'Text("Дальше")' in source
    assert 'assistant.captureDraft' in source
    assert "TabView" not in source


def test_ios_does_not_embed_model_or_classifier_logic():
    swift = "\n".join(path.read_text(encoding="utf-8") for path in IOS.rglob("*.swift"))
    lowered = swift.lower()

    assert "gemini" not in lowered
    assert "deepseek" not in lowered
    assert "classify_intake" not in lowered
    assert "llm" not in lowered


def test_ideas_stay_off_the_default_attention_surface():
    home = _read("App/ContentView.swift")
    backlog = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "IdeasView" not in home
    assert "IdeasView" in backlog
    assert 'navigationTitle("Идеи")' in ideas
    assert "swipeActions" in ideas
    assert "TabView" not in ideas


def test_widget_shared_keychain_does_not_hardcode_app_group_as_access_group():
    source = _read("Shared/WidgetSharedSettings.swift")

    assert "kSecAttrAccessGroup" not in source
    assert "group.0ee1e5aa54499877" not in source
    assert 'service = "com.aev199.assistantpocket.widget-shared"' in source


def test_widget_keeps_esign_safe_static_configuration():
    source = _read("Widget/AssistantWidget.swift")

    assert "StaticConfiguration" in source
    assert "AppIntentConfiguration" not in source
    assert "WidgetSharedSettings" in source


def test_capture_is_loss_resistant():
    content = _read("App/ContentView.swift")
    outbox = _read("App/CaptureOutbox.swift")

    assert "CaptureOutbox.enqueue" in content
    assert "flushOutbox()" in content
    assert "UserDefaults.standard" in outbox
