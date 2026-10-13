from __future__ import annotations

import json
import stat

import httpx

from tokyo_events import setup_telegram

TOKEN = "1234567890:" + "A" * 35


def test_update_env_replaces_and_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# комментарий\nOPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=old\n")
    setup_telegram.update_env(env, {"TELEGRAM_BOT_TOKEN": "new", "TELEGRAM_CHAT_ID": "42"})
    assert env.read_text() == "# комментарий\nOPENAI_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN=new\nTELEGRAM_CHAT_ID=42\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def fake_telegram(monkeypatch, updates=None, send_ok=True):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "tokyo_events_bot"}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": updates or []})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {}} if send_ok else
                                  {"ok": False, "description": "Forbidden: bot can't initiate conversation"})
        return httpx.Response(404, json={"ok": False})

    real_client = httpx.Client
    monkeypatch.setattr(setup_telegram.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(setup_telegram, "github_repo", lambda: False)
    return calls


def answers(monkeypatch, token: list[str], inputs: list[str]) -> None:
    monkeypatch.setattr(setup_telegram.getpass, "getpass", lambda prompt: token.pop(0))
    monkeypatch.setattr("builtins.input", lambda prompt="": inputs.pop(0))


def test_manual_chat_id(tmp_path, monkeypatch):
    calls = fake_telegram(monkeypatch)
    answers(monkeypatch, ["не токен", TOKEN], ["123456789", ""])
    assert setup_telegram.main(tmp_path / ".env") == 0
    assert ("sendMessage", {"chat_id": "123456789",
                            "text": "✅ Tokyo Events подключён. Сюда будут приходить события 🟢🟠🟣"}) in calls
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=123456789\n" == (tmp_path / ".env").read_text()


def test_auto_detect_chat_id(tmp_path, monkeypatch):
    fake_telegram(monkeypatch, updates=[{"message": {"chat": {"id": 987, "first_name": "R"}}}])
    answers(monkeypatch, [TOKEN], ["", ""])
    assert setup_telegram.main(tmp_path / ".env") == 0
    assert "TELEGRAM_CHAT_ID=987" in (tmp_path / ".env").read_text()


def test_second_account_manual_and_auto(tmp_path, monkeypatch):
    calls = fake_telegram(monkeypatch, updates=[{"message": {"chat": {"id": 111, "first_name": "Я"}}},
                                                {"message": {"chat": {"id": 222, "first_name": "Второй"}}}])
    # первый — вручную, второй — «+» (автопоиск предложит только ещё не подключённый 222), повтор 111 — игнор
    answers(monkeypatch, [TOKEN], ["111", "+", "111", ""])
    assert setup_telegram.main(tmp_path / ".env") == 0
    assert "TELEGRAM_CHAT_ID=111,222" in (tmp_path / ".env").read_text()
    assert [b["chat_id"] for m, b in calls if m == "sendMessage"] == ["111", "222"]


def test_bad_second_account_is_not_saved(tmp_path, monkeypatch):
    fake_telegram(monkeypatch)
    answers(monkeypatch, [TOKEN], ["111", "не число", ""])
    assert setup_telegram.main(tmp_path / ".env") == 0
    assert (tmp_path / ".env").read_text().endswith("TELEGRAM_CHAT_ID=111\n")


def test_nothing_saved_if_bot_cannot_write(tmp_path, monkeypatch):
    fake_telegram(monkeypatch, send_ok=False)
    answers(monkeypatch, [TOKEN], ["123", ""])
    assert setup_telegram.main(tmp_path / ".env") == 1
    assert not (tmp_path / ".env").exists()
