from __future__ import annotations

import httpx

from tokyo_events import setup_rakuten


def fake(monkeypatch, status, inputs, keys, repo=False):
    real = httpx.Client
    monkeypatch.setattr(setup_rakuten.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(
                            lambda r: httpx.Response(status, json={"hotels": []} if status == 200 else
                                                     {"error": "wrong_parameter", "error_description": "invalid"}))))
    inputs, keys = list(inputs), list(keys)
    monkeypatch.setattr("builtins.input", lambda prompt="": inputs.pop(0))
    monkeypatch.setattr(setup_rakuten.getpass, "getpass", lambda prompt: keys.pop(0))
    monkeypatch.setattr(setup_rakuten, "github_repo", lambda: repo)
    secrets = {}
    monkeypatch.setattr(setup_rakuten, "set_github_secret", lambda n, v: secrets.update({n: v}) or True)
    return secrets


def test_good_keys_saved_and_pushed_to_github(tmp_path, monkeypatch):
    secrets = fake(monkeypatch, 200, ["1234567890", ""], ["KEY"], repo=True)
    env = tmp_path / ".env"
    assert setup_rakuten.main(env) == 0
    assert env.read_text() == "RAKUTEN_APPLICATION_ID=1234567890\nRAKUTEN_ACCESS_KEY=KEY\n"
    assert secrets == {"RAKUTEN_APPLICATION_ID": "1234567890", "RAKUTEN_ACCESS_KEY": "KEY"}


def test_no_vacancy_still_means_keys_ok(tmp_path, monkeypatch):
    fake(monkeypatch, 404, ["1"], ["KEY"])
    assert setup_rakuten.main(tmp_path / ".env") == 0


def test_rejected_keys_not_saved(tmp_path, monkeypatch, capsys):
    fake(monkeypatch, 400, ["1", "1", "1"], ["K", "K", "K"])
    assert setup_rakuten.main(tmp_path / ".env") == 1
    assert not (tmp_path / ".env").exists() and "не принял ключи" in capsys.readouterr().out
