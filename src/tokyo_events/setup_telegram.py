"""Подключение бота: `uv run tokyo-events setup-telegram`.

Спрашивает токен (ввод скрыт) и chat id, проверяет их у Telegram, присылает тестовое сообщение
и сохраняет в .env (файл не попадает в git). Если у репозитория уже есть GitHub — предлагает
положить их и в GitHub Secrets.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")


def update_env(path: Path, values: dict[str, str]) -> None:
    """Заменить или дописать ключи в .env, остальные строки не трогать. Права 600 — только владелец."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in left and not line.lstrip().startswith("#"):
            out.append(f"{key}={left.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _api(client: httpx.Client, token: str, method: str, **payload: object) -> dict:
    try:
        r = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        # без текста исключения: в нём может быть URL с токеном
        return {"ok": False, "description": f"нет ответа от Telegram ({type(e).__name__})"}


def _find_chats(client: httpx.Client, token: str) -> list[tuple[str, str]]:
    data = _api(client, token, "getUpdates", allowed_updates=["message"])
    chats: dict[str, str] = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            chats[str(chat["id"])] = name
    return list(chats.items())


def github_repo() -> bool:
    if not shutil.which("gh"):
        return False
    r = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
    return r.returncode == 0 and "github.com" in r.stdout


def set_github_secret(name: str, value: str) -> bool:
    # значение — через stdin, не в аргументах командной строки (их видно в списке процессов)
    r = subprocess.run(["gh", "secret", "set", name], input=value, text=True, capture_output=True)
    return r.returncode == 0


def main(env_path: Path = Path(".env")) -> int:
    print("Подключение Telegram-бота Tokyo Events\n")
    print("1) В @BotFather: /newbot → получите токен.")
    print("2) Откройте своего нового бота и нажмите Start (или напишите ему что угодно).\n")

    with httpx.Client(timeout=20) as client:
        while True:
            token = getpass.getpass("Токен бота (ввод не отображается): ").strip()
            if not TOKEN_RE.match(token):
                print("  Не похоже на токен (вид: 1234567890:AA…). Попробуйте ещё раз.")
                continue
            me = _api(client, token, "getMe")
            if me.get("ok"):
                print(f"  ✓ Бот найден: @{me['result'].get('username')}")
                break
            print(f"  ✗ Telegram не принял токен: {me.get('description', '')}")

        chat_ids: list[str] = []
        answer = input("\nВаш chat id (Enter — найти автоматически): ").strip()
        while True:
            chat_id = answer or _pick_chat(client, token, exclude=chat_ids)
            if chat_id and chat_id not in chat_ids and _check_chat(client, token, chat_id):
                chat_ids.append(chat_id)
            print(f"\nПодключено аккаунтов: {len(chat_ids)}")
            print("Ещё один аккаунт? С него сначала откройте бота и нажмите Start.")
            answer = input("  chat id другого аккаунта, «+» — найти автоматически, Enter — готово: ").strip()
            if not answer:
                break
            if answer == "+":
                answer = ""

    if not chat_ids:
        print("\nНи один чат не подключён — ничего не сохранено. Запустите команду снова.")
        return 1
    joined = ",".join(chat_ids)
    update_env(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": joined})
    print(f"\n✓ Сохранено в {env_path.resolve()}: {len(chat_ids)} аккаунт(а) (файл в git не попадает)")

    if github_repo() and input("Сохранить и в GitHub Secrets этого репозитория? [y/N]: ").strip().lower() == "y":
        ok = set_github_secret("TELEGRAM_BOT_TOKEN", token) and set_github_secret("TELEGRAM_CHAT_ID", joined)
        print("✓ GitHub Secrets обновлены" if ok else "✗ Не получилось — проверьте `gh auth status`")
    return 0


def _pick_chat(client: httpx.Client, token: str, exclude: list[str]) -> str | None:
    """Найти chat id по сообщениям боту. Уже подключённые не предлагаем."""
    chats = [(cid, name) for cid, name in _find_chats(client, token) if cid not in exclude]
    if not chats:
        print("  Бот не видел новых сообщений. Откройте бота с нужного аккаунта, нажмите Start и попробуйте снова.")
        return None
    if len(chats) == 1:
        print(f"  Найден: {chats[0][1]} ({chats[0][0]})")
        return chats[0][0]
    for i, (cid, name) in enumerate(chats, 1):
        print(f"  {i}. {name} ({cid})")
    choice = input("  Номер чата: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(chats):
        print("  Неверный номер.")
        return None
    return chats[int(choice) - 1][0]


def _check_chat(client: httpx.Client, token: str, chat_id: str) -> bool:
    """Проверить chat id тестовым сообщением."""
    if not re.fullmatch(r"-?\d+", chat_id):
        print("  ✗ chat id — это число (например 123456789).")
        return False
    sent = _api(client, token, "sendMessage", chat_id=chat_id,
                text="✅ Tokyo Events подключён. Сюда будут приходить события 🟢🟠🟣")
    if not sent.get("ok"):
        print(f"  ✗ Не удалось написать в чат {chat_id}: {sent.get('description', '')}")
        print("    Проверьте chat id и что с этого аккаунта нажат Start у бота.")
        return False
    print(f"  ✓ {chat_id}: тестовое сообщение отправлено — проверьте Telegram")
    return True
