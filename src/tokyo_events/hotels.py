"""Скидки на отели и рёканы по реальным ценам — API 楽天トラベル (VacantHotelSearch). Без ИИ.

Варианты заезда — из config.yaml (hotels.stays): пятница на 1 или 2 ночи, суббота, «сегодня» (день в день).
Для каждого места (hotels.areas, по всей Японии) берём цены на ближайшие недели и сравниваем цену на ближайшую дату:
- с ценами того же отеля на тот же вариант в другие недели (обычная цена);
- с прошлыми ценами того же отеля на ту же дату (подешевело).
Скидка от порога deals (10%) и экономия заметная — уведомление с цветом по размеру скидки.
"""

from __future__ import annotations

import html
import json
import logging
import statistics
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import Config, HotelArea, Stay, env
from .events_db import EventStore

log = logging.getLogger(__name__)

API = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
CIRCLES = {"green": "🟢", "orange": "🟠", "purple": "🟣"}
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
             "ноября", "декабря"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_prices (      -- каждое наблюдение цены
    hotel_no    TEXT NOT NULL,
    stay_date   TEXT NOT NULL,                 -- дата заезда
    nights      INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    price       INTEGER NOT NULL,              -- за номер на всех гостей за (первую) ночь, ¥
    area        TEXT NOT NULL,
    plan_name   TEXT NOT NULL DEFAULT '',
    raw         TEXT NOT NULL DEFAULT ''       -- исходные rakutenCharge/total/chargeFlag — для проверки
);
CREATE INDEX IF NOT EXISTS hotel_prices_idx ON hotel_prices (hotel_no, stay_date, nights);
CREATE TABLE IF NOT EXISTS hotel_alerts (      -- о чём уже сообщили
    hotel_no  TEXT NOT NULL,
    stay_date TEXT NOT NULL,
    nights    INTEGER NOT NULL,
    price     INTEGER NOT NULL,
    pct       INTEGER NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (hotel_no, stay_date, nights)
);
"""


class RakutenError(RuntimeError):
    pass


@dataclass
class Offer:
    hotel_no: str
    name: str
    area: str
    stay_date: str
    nights: int
    price: int
    plan_name: str
    url: str
    address: str = ""
    review: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Deal:
    offer: Offer
    baseline: int
    pct: int
    basis: str      # usual — обычная цена в другие недели; history — раньше на эту дату
    rank: str
    today: bool = False          # заезд сегодня
    cheaper_again: bool = False  # уже сообщали, а стало ещё дешевле


# --- даты ---------------------------------------------------------------------------

def stay_dates(stay: Stay, today: date, weeks: int) -> list[date]:
    """Даты заезда варианта на `weeks` недель вперёд. «today» — сегодня и тот же день недели дальше
    (они нужны как обычная цена)."""
    if stay.checkin == "today":
        first = today
    else:
        first = today + timedelta(days=(WEEKDAY_NUM[stay.checkin] - today.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(weeks)]


def saturdays(today: date, count: int) -> list[date]:
    return stay_dates(Stay(checkin="sat", nights=1), today, count)


# --- API ---------------------------------------------------------------------------

class Rakuten:
    def __init__(self, app_id: str, access_key: str, client: httpx.Client, origin: str = "",
                 sleep: Callable[[float], None] = time.sleep, pause_s: float = 1.1) -> None:
        self.app_id, self.access_key, self.client, self.origin = app_id, access_key, client, origin
        self.sleep, self.pause_s = sleep, pause_s

    def search(self, area: HotelArea, checkin: date, adults: int, nights: int, page: int = 1) -> list[dict]:
        """Сырые записи отелей. Нет свободных номеров — пустой список."""
        params = {
            "applicationId": self.app_id, "format": "json", "formatVersion": 1,
            "checkinDate": checkin.isoformat(), "checkoutDate": (checkin + timedelta(days=nights)).isoformat(),
            "adultNum": adults, "latitude": area.lat, "longitude": area.lon, "datumType": 1,
            "searchRadius": area.radius_km, "searchPattern": 1, "sort": "+roomCharge", "hits": 30, "page": page,
        }
        if area.onsen:
            params["squeezeCondition"] = "onsen"
        # с 2026 года Rakuten пускает запросы только с сайта, указанного в приложении (заголовок Origin)
        headers = {"accessKey": self.access_key, **({"Origin": self.origin} if self.origin else {})}
        for attempt in range(3):
            self.sleep(self.pause_s)  # API просит не частить: не чаще раза в секунду
            try:
                r = self.client.get(API, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise RakutenError(f"нет ответа ({type(e).__name__})") from None
            if r.status_code == 404:  # not_found — свободных номеров нет
                return []
            if r.status_code == 429 and attempt < 2:
                self.sleep(10.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RakutenError(f"{r.status_code} {_error_text(r)}")
            return r.json().get("hotels", [])
        raise RakutenError("429: слишком много запросов")


def _error_text(r: httpx.Response) -> str:
    """Текст ошибки API: новый формат {"errors": {"errorMessage"}} или старый {"error_description"}."""
    try:
        data = r.json()
    except ValueError:
        return ""
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, dict):
        return str(errors.get("errorMessage", ""))
    return str(data.get("error_description") or data.get("error") or "")


def parse_offers(hotels: list[dict], area: str, stay_date: str, nights: int, adults: int,
                 two_meals: bool) -> list[Offer]:
    """Самый дешёвый подходящий план каждого отеля."""
    best: dict[str, Offer] = {}
    for entry in hotels:
        parts = entry.get("hotel", entry if isinstance(entry, list) else [])
        info: dict = {}
        rooms: list = []
        for part in parts:
            info = part.get("hotelBasicInfo", info)
            rooms = part.get("roomInfo", rooms)
        if not info or not rooms:
            continue
        plan: dict = {}
        for room in rooms:  # список чередует roomBasicInfo и dailyCharge
            if "roomBasicInfo" in room:
                plan = room["roomBasicInfo"]
                continue
            charge = room.get("dailyCharge")
            if not charge or not plan:
                continue
            if two_meals and not (plan.get("withDinnerFlag") == 1 and plan.get("withBreakfastFlag") == 1):
                continue
            price = room_price(charge, adults)
            if not price:
                continue
            no = str(info.get("hotelNo"))
            if no not in best or price < best[no].price:
                best[no] = Offer(
                    hotel_no=no, name=info.get("hotelName", ""), area=area, stay_date=stay_date, nights=nights,
                    price=price, plan_name=plan.get("planName", ""),
                    url=plan.get("reserveUrl") or info.get("planListUrl") or info.get("hotelInformationUrl", ""),
                    address=f"{info.get('address1', '')}{info.get('address2', '')}",
                    review=float(info["reviewAverage"]) if info.get("reviewAverage") else None,
                    raw={k: charge.get(k) for k in ("rakutenCharge", "total", "chargeFlag")})
    return list(best.values())


def room_price(charge: dict, adults: int) -> int:
    """Цена за номер на всех гостей за ночь (API даёт цену первой ночи). total — «итого за ночь»;
    если его нет, считаем из rakutenCharge (chargeFlag 0 — за человека, 1 — за номер)."""
    total = charge.get("total")
    if total:
        return int(total)
    per = charge.get("rakutenCharge")
    if not per:
        return 0
    return int(per) * (adults if charge.get("chargeFlag") == 0 else 1)


# --- поиск скидок ---------------------------------------------------------------------------

def find_deals(db: Any, offers: list[Offer], alert: set[tuple[str, int]], cfg: Config, today: date) -> list[Deal]:
    """Скидки для дат из alert ((дата, ночей)): к обычной цене того же отеля (тот же день недели и число
    ночей в другие недели) и к прошлым ценам на ту же дату."""
    h = cfg.hotels
    groups: dict[tuple[str, int, int], dict[str, Offer]] = {}
    for o in offers:
        key = (o.hotel_no, o.nights, date.fromisoformat(o.stay_date).weekday())
        groups.setdefault(key, {})[o.stay_date] = o
    since = (today - timedelta(days=h.history_days)).isoformat()
    deals: list[Deal] = []
    for dates in groups.values():
        for stay, o in dates.items():
            if (stay, o.nights) not in alert or (o.review is not None and o.review < h.min_review):
                continue
            others = [x.price for d, x in dates.items() if d != stay]
            history = [r[0] for r in db.execute(
                "SELECT price FROM hotel_prices WHERE hotel_no = ? AND stay_date = ? AND nights = ? "
                "AND observed_at >= ?", (o.hotel_no, stay, o.nights, since))]
            candidates = []
            if len(others) >= h.min_compare:
                candidates.append((int(statistics.median(others)), "usual"))
            if history:
                candidates.append((int(statistics.median(history)), "history"))
            for baseline, basis in sorted(candidates, reverse=True):
                pct = round((1 - o.price / baseline) * 100) if baseline else 0
                rank = _rank(pct, cfg)
                if rank and baseline - o.price >= h.min_saving_yen:
                    deals.append(Deal(o, baseline, pct, basis, rank, today=stay == today.isoformat()))
                    break
    return deals


def _rank(pct: int, cfg: Config) -> str | None:
    t = cfg.deals
    return "purple" if pct >= t.purple_pct else "orange" if pct >= t.orange_pct else "green" if pct >= t.min_pct \
        else None


# --- уведомление ---------------------------------------------------------------------------

def yen(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ¥"


def stay_ru(stay: str, nights: int) -> str:
    """«пт 16 – сб 17 октября», «пт 30 октября – вс 1 ноября (2 ночи)»."""
    d = date.fromisoformat(stay)
    out = d + timedelta(days=nights)
    wd, wo = WEEKDAYS_RU[d.weekday()], WEEKDAYS_RU[out.weekday()]
    if out.month == d.month:
        text = f"{wd} {d.day} – {wo} {out.day} {MONTHS_RU[d.month - 1]}"
    else:
        text = f"{wd} {d.day} {MONTHS_RU[d.month - 1]} – {wo} {out.day} {MONTHS_RU[out.month - 1]}"
    return text + (f" ({nights} ночи)" if nights in (2, 3, 4) else f" ({nights} ночей)" if nights > 4 else "")


def format_deal(deal: Deal, cfg: Config, two_meals: bool = True) -> str:
    e = html.escape
    o, h = deal.offer, cfg.hotels
    head = "🔴 Ещё дешевле · " if deal.cheaper_again else f"{CIRCLES[deal.rank]} ♨️ −{deal.pct}% · "
    when = ("⚡ Сегодня, " if deal.today else "") + stay_ru(o.stay_date, o.nights)
    meals = "ужин и завтрак" if two_meals else "любой план"
    basis = "обычно в этот день недели" if deal.basis == "usual" else "раньше на эту дату"
    per = " за первую ночь" if o.nights > 1 else " за ночь"
    maps = "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
        {"api": "1", "query": f"{o.name} {o.address}".strip()})
    blocks = [
        f"{head}<b>{e(o.name)}</b> · {e(o.area)}",
        f"📅 {when} · {h.adults} взрослых · {meals}",
        f"💴 {yen(o.price)}{per} вместо {yen(deal.baseline)} ({basis}) · −{deal.pct}%",
        f'🗺 <a href="{e(maps, quote=True)}">{e(o.name)}</a>',
        f'🌐 <a href="{e(o.url, quote=True)}">Забронировать на Rakuten Travel</a>',
    ]
    extra = " · ".join(x for x in (f"⭐ {o.review:.1f}" if o.review else "", f"план: «{e(o.plan_name)}»"
                                   if o.plan_name else "") if x)
    if extra:
        blocks.append(extra)
    blocks.append(f"#отели #{o.area.replace(' ', '_').replace('-', '_')}")
    return "\n\n".join(blocks)


# --- запуск ---------------------------------------------------------------------------

@dataclass
class HotelReport:
    checked: int = 0
    offers: int = 0
    sent: int = 0
    errors: list[str] = field(default_factory=list)


def check_hotels(cfg: Config, store: EventStore, sender: Any, now: datetime, client: httpx.Client,
                 force: bool = False, api: Rakuten | None = None) -> HotelReport:
    report = HotelReport()
    h = cfg.hotels
    if not h.enabled:
        return report
    app_id, key = env("RAKUTEN_APPLICATION_ID"), env("RAKUTEN_ACCESS_KEY")
    if api is None and not (app_id and key):
        log.info("отели: нет RAKUTEN_APPLICATION_ID / RAKUTEN_ACCESS_KEY — проверка цен выключена")
        return report
    if not force and now.hour not in h.run_at_hours:
        log.info("отели: цены проверяются в %s ч по Токио — сейчас пропуск", h.run_at_hours)
        return report
    api = api or Rakuten(app_id or "", key or "", client, origin=env("RAKUTEN_ORIGIN") or h.origin)
    db = store.db
    db.executescript(SCHEMA)
    today = now.date()
    plan: list[tuple[date, int, bool]] = []  # (дата заезда, ночей, сообщать ли о скидке)
    for stay in h.stays:
        dates = stay_dates(stay, today, h.weeks_ahead)
        alert_n = 1 if stay.checkin == "today" else h.alert_weeks
        plan += [(d, stay.nights, i < alert_n) for i, d in enumerate(dates)]
    plan = sorted(set(plan))
    alert = {(d.isoformat(), n) for d, n, a in plan if a}
    offers: list[Offer] = []
    for area in h.areas:
        two_meals = (area.meals or h.meals) == "two_meals"
        for checkin, nights in sorted({(d, n) for d, n, _ in plan}):
            try:
                raw = api.search(area, checkin, h.adults, nights)
            except RakutenError as e:
                report.errors.append(f"отели {area.name} {checkin}: {e}")
                log.warning("отели %s %s: %s", area.name, checkin, e)
                if len(report.errors) >= 5 and not report.checked:
                    log.warning("отели: API не отвечает — прекращаю проверку")
                    return report
                continue
            report.checked += 1
            offers += parse_offers(raw, area.name, checkin.isoformat(), nights, h.adults, two_meals)
    report.offers = len(offers)
    meals_by_area = {a.name: (a.meals or h.meals) == "two_meals" for a in h.areas}
    deals = sorted(find_deals(db, offers, alert, cfg, today), key=lambda d: (-d.today, -d.pct))
    at = now.isoformat(timespec="seconds")
    db.executemany("INSERT INTO hotel_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(o.hotel_no, o.stay_date, o.nights, at, o.price, o.area, o.plan_name, json.dumps(o.raw))
                    for o in offers])
    for deal in deals:
        if report.sent >= h.max_alerts_per_run:
            break
        o = deal.offer
        prev = db.execute("SELECT price FROM hotel_alerts WHERE hotel_no = ? AND stay_date = ? AND nights = ?",
                          (o.hotel_no, o.stay_date, o.nights)).fetchone()
        if prev:
            if o.price > prev[0] * (1 - cfg.deals.min_pct / 100):
                continue  # уже сообщали, заметно дешевле не стало
            deal.cheaper_again = True
        silent = deal.rank in cfg.filters.silent_ranks and not deal.cheaper_again and not deal.today
        try:
            problems = sender.send(format_deal(deal, cfg, meals_by_area.get(o.area, True)), silent=silent)
        except Exception as e:  # Telegram недоступен: попробуем в следующий запуск
            report.errors.append(f"отели: {e}")
            continue
        report.errors += [f"Telegram: {p}" for p in problems or []]
        db.execute("INSERT OR REPLACE INTO hotel_alerts VALUES (?, ?, ?, ?, ?, ?)",
                   (o.hotel_no, o.stay_date, o.nights, o.price, deal.pct, at))
        report.sent += 1
    # прошедшие даты не нужны
    db.execute("DELETE FROM hotel_prices WHERE stay_date < ?", (today.isoformat(),))
    db.execute("DELETE FROM hotel_alerts WHERE stay_date < ?", (today.isoformat(),))
    log.info("отели: запросов %d, предложений %d, скидок отправлено %d, ошибок %d",
             report.checked, report.offers, report.sent, len(report.errors))
    return report
