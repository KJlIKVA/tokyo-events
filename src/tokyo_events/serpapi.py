"""SerpApi — Google Flights и Google Hotels (в Google Hotels — цены Booking.com, Trip.com, Agoda, Expedia…).

У Booking.com и Trip.com открытого API цен нет (только для партнёров по договору), а Google сводит их цены
в одном месте. Бесплатно SerpApi даёт 250 запросов в месяц: бот считает запросы и не тратит больше
дневной доли (serpapi.monthly_searches / 30), а по остатку на счёте (запрос остатка бесплатный) не
уходит в ноль.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

SEARCH = "https://serpapi.com/search.json"
ACCOUNT = "https://serpapi.com/account.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS serp_usage (        -- каждый платный запрос SerpApi: чтобы не выйти за тариф
    at     TEXT NOT NULL,
    engine TEXT NOT NULL
);
"""


class SerpApiError(RuntimeError):
    pass


class SerpApi:
    def __init__(self, key: str, client: httpx.Client) -> None:
        self.key, self.client = key, client

    def account(self) -> dict[str, Any]:
        """Остаток запросов на счёте. Этот запрос в тариф не засчитывается."""
        return self._get(ACCOUNT, {})

    def search(self, engine: str, params: dict[str, Any]) -> dict[str, Any]:
        data = self._get(SEARCH, {"engine": engine, **params})
        error = data.get("error")
        if error and "hasn't returned any results" not in str(error):
            raise SerpApiError(str(error))
        return data

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.client.get(url, params={**params, "api_key": self.key}, timeout=60)
        except httpx.HTTPError as e:  # без URL в тексте: в нём ключ
            raise SerpApiError(f"нет ответа ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            raise SerpApiError(f"{r.status_code} {data.get('error', '') if isinstance(data, dict) else ''}".strip())
        return data if isinstance(data, dict) else {}


class Budget:
    """Сколько запросов SerpApi можно потратить сейчас: дневная доля тарифа минус уже потраченное сегодня,
    и не больше остатка на счёте за вычетом запаса."""

    def __init__(self, db: Any, api: SerpApi, now: datetime, monthly: int, reserve: int,
                 left: Callable[[], int | None] | None = None) -> None:
        self.db, self.api, self.now = db, api, now
        db.executescript(SCHEMA)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        used_today = db.execute("SELECT COUNT(*) FROM serp_usage WHERE at >= ?", (day,)).fetchone()[0]
        self.free = max(0, monthly // 30 - used_today)
        self.error: str | None = None   # ключ не принят — проверки выключены, в отчёт попадёт ошибка
        account_left = (left or self._account_left)()
        if account_left is not None:
            self.free = min(self.free, max(0, account_left - reserve))
        self.spent = 0

    def _account_left(self) -> int | None:
        try:
            data = self.api.account()
        except SerpApiError as e:
            if str(e).startswith(("401", "403")):
                self.error = f"SerpApi не принимает ключ: {e}"
                return 0
            log.warning("SerpApi: остаток не узнать: %s", e)
            return None
        left = data.get("total_searches_left", data.get("plan_searches_left"))
        return int(left) if left is not None else None

    def take(self, n: int = 1) -> bool:
        return self.spent + n <= self.free

    def search(self, engine: str, params: dict[str, Any]) -> dict[str, Any]:
        """Запрос с учётом: без остатка — SerpApiError (вызывающий проверяет take() заранее)."""
        if not self.take():
            raise SerpApiError("дневная доля запросов исчерпана")
        self.spent += 1
        self.db.execute("INSERT INTO serp_usage VALUES (?, ?)", (self.now.isoformat(timespec="seconds"), engine))
        try:
            return self.api.search(engine, params)
        except SerpApiError as e:
            if str(e).startswith(("401", "403", "429")):  # ключ не принят или запросы на счёте кончились
                self.free = self.spent
            raise

    def prune(self) -> None:
        cutoff = self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self.db.execute("DELETE FROM serp_usage WHERE at < ?", (cutoff.isoformat(timespec="seconds"),))
