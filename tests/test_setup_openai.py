from __future__ import annotations

import httpx
import openai

from tokyo_events import setup_openai

KEY = "sk-proj-" + "x" * 60


class FakeResponses:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return type("R", (), {"status": "completed"})()


def _err(cls, status: int, message: str = "x"):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return cls(message, response=resp, body=None)


def setup(monkeypatch, keys, inputs=(), error=None, repo=False):
    responses = FakeResponses(error)
    monkeypatch.setattr(setup_openai, "_client", lambda key: type("C", (), {"responses": responses})())
    keys = list(keys)
    inputs = list(inputs)
    monkeypatch.setattr(setup_openai.getpass, "getpass", lambda prompt: keys.pop(0))
    monkeypatch.setattr("builtins.input", lambda prompt="": inputs.pop(0))
    monkeypatch.setattr(setup_openai, "github_repo", lambda: repo)
    secrets = {}
    monkeypatch.setattr(setup_openai, "set_github_secret", lambda n, v: secrets.update({n: v}) or True)
    return responses, secrets


def test_good_key_replaces_old_one_and_updates_github(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-old\nTELEGRAM_CHAT_ID=1\n")
    responses, secrets = setup(monkeypatch, ["не ключ", KEY], inputs=[""], repo=True)
    assert setup_openai.main(env) == 0
    assert env.read_text() == f"OPENAI_API_KEY={KEY}\nTELEGRAM_CHAT_ID=1\n"
    assert secrets == {"OPENAI_API_KEY": KEY}
    assert responses.calls[0]["model"] == "gpt-5.6-terra"  # проверяем именно разрешённую модель


def test_rejected_key_is_not_saved(tmp_path, monkeypatch, capsys):
    setup(monkeypatch, [KEY, KEY, KEY], error=_err(openai.AuthenticationError, 401))
    assert setup_openai.main(tmp_path / ".env") == 1
    assert not (tmp_path / ".env").exists()
    assert "не принял ключ" in capsys.readouterr().out


def test_no_money_message(monkeypatch):
    setup(monkeypatch, [], error=_err(openai.RateLimitError, 429, "You exceeded your current quota"))
    assert "нет средств" in setup_openai.check_key(KEY)


def test_no_model_access_message(monkeypatch):
    setup(monkeypatch, [], error=_err(openai.PermissionDeniedError, 403, "model not allowed"))
    assert "нет доступа к gpt-5.6-terra" in setup_openai.check_key(KEY)
