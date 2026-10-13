"""Ключ SerpApi для авиабилетов (Google Flights) и цен Booking.com / Trip.com (Google Hotels):
`uv run tokyo-events setup-serpapi`.

Спрашивает ключ (ввод скрыт), проверяет запросом остатка на счёте (в тариф не засчитывается),
сохраняет в .env (в git не попадает) и предлагает положить в GitHub Secrets.
"""

from __future__ import annotations

import getpass
from pathlib import Path

import httpx

from .serpapi import SerpApi, SerpApiError
from .setup_telegram import github_repo, set_github_secret, update_env

STEPS = """\
Ключ SerpApi (бесплатно 250 запросов в месяц) — для авиабилетов и цен Booking.com / Trip.com

1) Откройте https://serpapi.com/users/sign_up и зарегистрируйтесь (бесплатный тариф Free, карта не нужна).
2) Подтвердите почту и телефон, если попросят.
3) Откройте https://serpapi.com/manage-api-key и скопируйте Your Private API Key.
"""


def check_key(key: str, client: httpx.Client) -> tuple[str | None, dict]:
    """(проблема или None, данные счёта)."""
    try:
        data = SerpApi(key, client).account()
    except SerpApiError as e:
        return f"SerpApi не принял ключ: {e}", {}
    return None, data


def main(env_path: Path = Path(".env")) -> int:
    print(STEPS)
    with httpx.Client(timeout=30) as client:
        for _ in range(3):
            key = getpass.getpass("API Key (ввод не отображается): ").strip()
            if not key:
                print("  Пусто.")
                continue
            print("  Проверяю…")
            problem, account = check_key(key, client)
            if problem:
                print(f"  ✗ {problem}")
                continue
            break
        else:
            print("\nКлюч не сохранён. Запустите команду снова.")
            return 1
    left = account.get("total_searches_left", account.get("plan_searches_left"))
    plan = account.get("plan_name") or "?"
    print(f"  ✓ Ключ работает · тариф {plan} · осталось запросов: {left if left is not None else '?'}")
    update_env(env_path, {"SERPAPI_API_KEY": key})
    print(f"\n✓ Сохранено в {env_path.resolve()} (в git не попадает)")
    if github_repo():
        answer = input("Сохранить в GitHub Secrets, чтобы бот на Actions проверял авиабилеты? [Y/n]: ")
        if answer.strip().lower() in ("", "y", "д", "да", "yes"):
            ok = set_github_secret("SERPAPI_API_KEY", key)
            print("✓ GitHub Secrets обновлены" if ok else "✗ Не получилось — проверьте `gh auth status`")
    searches = account.get("searches_per_month")
    if searches and int(searches) != 250:
        print(f"\nТариф даёт {searches} запросов в месяц — поставьте serpapi.monthly_searches: {searches} "
              "в config.yaml, чтобы бот проверял больше мест.")
    return 0
