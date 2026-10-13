"""Ключи API 楽天トラベル для цен отелей: `uv run tokyo-events setup-rakuten`.

Спрашивает Application ID и Access Key (ввод скрыт), проверяет пробным запросом цен,
сохраняет в .env (в git не попадает) и предлагает положить в GitHub Secrets.
"""

from __future__ import annotations

import getpass
from datetime import date
from pathlib import Path

import httpx

from .config import HotelArea, load_config
from .hotels import Rakuten, RakutenError, saturdays
from .setup_telegram import github_repo, set_github_secret, update_env


def _origin() -> str:
    try:
        return load_config().hotels.origin or DEFAULT_ORIGIN
    except OSError:
        return DEFAULT_ORIGIN


DEFAULT_ORIGIN = "https://kjlikva.github.io"
ORIGIN = _origin()

STEPS = f"""\
Ключи 楽天トラベル (бесплатно) — для цен отелей и рёканов

1) Откройте https://webservice.rakuten.co.jp/ и войдите аккаунтом Rakuten (楽天会員, можно завести бесплатно).
2) Создайте приложение («アプリID発行» / «New App»):
   • название — Tokyo Events, тип — Web Application;
   • адрес приложения (URL) — https://github.com/KJlIKVA/tokyo-events;
   • разрешённый сайт (許可されたWebサイト / Allowed websites) — {ORIGIN.removeprefix("https://")}
     (бот отправляет запросы от его имени);
   • ожидаемое число запросов в секунду (QPS) — 1.
3) На странице приложения скопируйте:
   • アプリケーションID (Application ID)
   • アクセスキー (Access Key)
"""

TEST_AREA = HotelArea(name="Хаконэ-Юмото", lat=35.2329, lon=139.1067)


def check_keys(app_id: str, access_key: str, client: httpx.Client) -> str | None:
    """Пробный запрос цен в Хаконэ на ближайшую субботу. None — ключи работают."""
    api = Rakuten(app_id, access_key, client, origin=ORIGIN, pause_s=0)
    try:
        api.search(TEST_AREA, saturdays(date.today(), 1)[0], adults=2, nights=1, two_meals=True)
    except RakutenError as e:
        text = str(e)
        if "access key" in text.lower() or "applicationid" in text.lower().replace(" ", ""):
            return f"Rakuten не принял ключи: {text}"
        if text.startswith("403"):
            return (f"Rakuten не пустил запрос: {text}. Проверьте, что в приложении разрешённый сайт — "
                    f"{ORIGIN.removeprefix('https://')}")
        if text.startswith(("400", "401")):
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
