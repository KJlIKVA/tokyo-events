"""Ключи API 楽天トラベル для цен отелей: `uv run tokyo-events setup-rakuten`.

Спрашивает Application ID и Access Key (ввод скрыт), проверяет пробным запросом цен,
сохраняет в .env (в git не попадает) и предлагает положить в GitHub Secrets.
"""

from __future__ import annotations

import getpass
from datetime import date
from pathlib import Path

import httpx

from .config import HotelArea
from .hotels import Rakuten, RakutenError, saturdays
from .setup_telegram import github_repo, set_github_secret, update_env

STEPS = """\
Ключи 楽天トラベル (бесплатно) — для цен отелей и рёканов

1) Откройте https://webservice.rakuten.co.jp/ и войдите аккаунтом Rakuten (楽天会員, можно завести бесплатно).
2) «アプリID発行» / «New App»: название — «Tokyo Events», тип — Web / backend (сервер).
   Адрес сайта можно указать https://github.com/KJlIKVA/tokyo-events
3) На странице приложения скопируйте:
   • アプリケーションID (Application ID)
   • アクセスキー (Access Key)
"""

TEST_AREA = HotelArea(name="Хаконэ-Юмото", lat=35.2329, lon=139.1067)


def check_keys(app_id: str, access_key: str, client: httpx.Client) -> str | None:
    """Пробный запрос цен в Хаконэ на ближайшую субботу. None — ключи работают."""
    api = Rakuten(app_id, access_key, client, pause_s=0)
    try:
        api.search(TEST_AREA, saturdays(date.today(), 1)[0], adults=2, nights=1)
    except RakutenError as e:
        text = str(e)
        if text.startswith(("400", "401", "403")):
            return f"Rakuten не принял ключи: {text}"
        return f"Не удалось проверить: {text}"
    return None


def main(env_path: Path = Path(".env")) -> int:
    print(STEPS)
    with httpx.Client(timeout=30) as client:
        for _ in range(3):
            app_id = input("Application ID: ").strip()
            access_key = getpass.getpass("Access Key (ввод не отображается): ").strip()
            if not app_id or not access_key:
                print("  Нужны оба значения.")
                continue
            print("  Проверяю пробным запросом цен в Хаконэ…")
            problem = check_keys(app_id, access_key, client)
            if problem:
                print(f"  ✗ {problem}")
                continue
            break
        else:
            print("\nКлючи не сохранены. Запустите команду снова.")
            return 1
    print("  ✓ Ключи работают")
    update_env(env_path, {"RAKUTEN_APPLICATION_ID": app_id, "RAKUTEN_ACCESS_KEY": access_key})
    print(f"\n✓ Сохранено в {env_path.resolve()} (в git не попадает)")
    if github_repo():
        answer = input("Сохранить в GitHub Secrets, чтобы бот на Actions проверял цены? [Y/n]: ")
        if answer.strip().lower() in ("", "y", "д", "да", "yes"):
            ok = set_github_secret("RAKUTEN_APPLICATION_ID", app_id) and \
                set_github_secret("RAKUTEN_ACCESS_KEY", access_key)
            print("✓ GitHub Secrets обновлены" if ok else "✗ Не получилось — проверьте `gh auth status`")
    return 0
