"""Авиабилеты (Google Flights) и Booking/Trip.com (Google Hotels) через SerpApi — на подставных ответах."""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from tokyo_events.events_db import EventStore
from tokyo_events.flights import check_flights, flights_url, parse_explore
from tokyo_events.google_hotels import check_google_hotels, parse_properties, plan_today
from tokyo_events.serpapi import Budget, SerpApi, SerpApiError
from tokyo_events.telegram import PrintTelegram

NOW = datetime(2026, 10, 13, 9, 5, tzinfo=ZoneInfo("Asia/Tokyo"))


class FakeSerp:
    """Ответы по engine: функция (params) → dict."""

    def __init__(self, **engines):
        self.engines, self.calls = engines, []

    def account(self):
        return {"total_searches_left": 240}

    def search(self, engine, params):
        self.calls.append((engine, params))
        return self.engines[engine](params)


def budget(db, api, now=NOW, monthly=250, left=240):
    return Budget(db, api, now, monthly, reserve=5, left=lambda: left)


def explore(prices: dict[str, int]):
    def answer(params):
        return {"destinations": [
            {"name": {"CTS": "Саппоро", "OKA": "Наха", "ICN": "Сеул"}[code], "country": "Япония" if code != "ICN"
             else "Южная Корея", "destination_airport": {"code": code}, "start_date": "2026-11-06",
             "end_date": "2026-11-08", "flight_price": price, "airline": "Peach", "number_of_stops": 0,
             "flight_duration": 95} for code, price in prices.items()]}
    return answer


def seed_history(db, cfg, search, dest, price, days=5):
    from tokyo_events.flights import SCHEMA
    db.executescript(SCHEMA)
    for i in range(1, days + 1):
        at = (NOW - timedelta(days=i)).isoformat(timespec="seconds")
        db.execute("INSERT INTO flight_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (search, dest, dest, at, price, "2026-11-06", "2026-11-08", "Peach"))


# --- SerpApi и бюджет ---------------------------------------------------------------------------

def test_budget_daily_share_and_account_reserve():
    db = EventStore(":memory:").db
    b = budget(db, FakeSerp(), monthly=250, left=240)
    assert b.free == 8                                   # 250 / 30
    assert budget(db, FakeSerp(), left=7).free == 2      # на счёте 7, запас 5
    assert budget(db, FakeSerp(), left=None).free == 8   # остаток не узнать — только дневная доля
    api = FakeSerp(x=lambda p: {})
    b = budget(db, api)
    for _ in range(3):
        b.search("x", {})
    assert budget(db, api).free == 5                     # сегодня уже потрачено 3
    assert budget(db, api, now=NOW + timedelta(days=1)).free == 8


def test_serpapi_errors_without_key_in_text():
    def handler(request):
        if "account" in request.url.path:
            return httpx.Response(401, json={"error": "Invalid API key."})
        if request.url.params["engine"] == "empty":
            return httpx.Response(200, json={"error": "Google hasn't returned any results for this query."})
        raise httpx.ConnectTimeout("timeout", request=request)

    api = SerpApi("SECRET-KEY", httpx.Client(transport=httpx.MockTransport(handler)))
    for call in (api.account, lambda: api.search("x", {})):
        try:
            call()
        except SerpApiError as e:
            assert "SECRET-KEY" not in str(e)
        else:
            raise AssertionError("ожидалась ошибка")
    assert api.search("empty", {}).get("destinations") is None   # «нет результатов» — не ошибка


# --- авиабилеты ---------------------------------------------------------------------------

def test_parse_explore_and_link(cfg):
    fares = parse_explore(explore({"CTS": 9800})({}), cfg.flights.searches[0])
    assert [(f.dest, f.price, f.start_date, f.end_date) for f in fares] == [("CTS", 9800, "2026-11-06", "2026-11-08")]
    no_end = parse_explore({"destinations": [{"destination_airport": {"code": "OKA"}, "flight_price": 15000,
                                               "start_date": "2026-11-06"}]}, cfg.flights.searches[0])
    assert no_end[0].end_date == "2026-11-08"            # выходные — +2 дня
    assert "Flights+to+CTS+from+TYO+on+2026-11-06+through+2026-11-08" in flights_url(fares[0])


def test_flight_deal_vs_usual_price_verified_by_google(cfg):
    store = EventStore(":memory:")
    db = store.db
    seed_history(db, cfg, "выходные_япония", "CTS", 20000)
    seed_history(db, cfg, "выходные_япония", "OKA", 30000)
    api = FakeSerp(google_travel_explore=explore({"CTS": 9800, "OKA": 29000}),
                   google_flights=lambda p: {"price_insights": {"price_level": "low",
                                                                "typical_price_range": [18000, 26000]}})
    cfg.flights.searches = cfg.flights.searches[:1]
    sender = PrintTelegram()
    report = check_flights(cfg, db, sender, NOW, budget(db, api))
    assert report.checked == 1 and report.fares == 2 and report.sent == 1
    text, silent = sender.sent[0]
    assert text.startswith("🟣 ✈️ −51% · <b>Саппоро</b> · из Токио") and not silent
    assert "9 800 ¥ вместо 20 000 ¥" in text and "обычно 18 000 ¥ – 26 000 ¥" in text
    assert "Peach · прямой · 1 ч 35 мин" in text and "пт 6 – вс 8 ноября (2 ночи)" in text
    assert [e for e, _ in api.calls] == ["google_travel_explore", "google_flights"]
    # на следующий день то же самое — не повторяем
    sender2 = PrintTelegram()
    check_flights(cfg, db, sender2, NOW + timedelta(days=1), budget(db, api, now=NOW + timedelta(days=1)))
    assert sender2.sent == []


def test_flights_wait_for_history_and_skip_high_google_price(cfg):
    db = EventStore(":memory:").db
    api = FakeSerp(google_travel_explore=explore({"CTS": 9800}),
                   google_flights=lambda p: {"price_insights": {"price_level": "high"}})
    cfg.flights.searches = cfg.flights.searches[:1]
    assert check_flights(cfg, db, PrintTelegram(), NOW, budget(db, api)).sent == 0   # истории ещё нет
    db2 = EventStore(":memory:").db
    seed_history(db2, cfg, "выходные_япония", "CTS", 20000)
    assert check_flights(cfg, db2, PrintTelegram(), NOW, budget(db2, api)).sent == 0  # Google: цена высокая


def test_flights_once_a_day_and_no_key(cfg):
    db = EventStore(":memory:").db
    api = FakeSerp(google_travel_explore=explore({"CTS": 9800}))
    check_flights(cfg, db, PrintTelegram(), NOW, budget(db, api))
    calls = len(api.calls)
    check_flights(cfg, db, PrintTelegram(), NOW.replace(hour=13), budget(db, api))
    assert len(api.calls) == calls                       # с 9 ч уже проверяли
    assert check_flights(cfg, db, PrintTelegram(), NOW, None).checked == 0
    assert check_flights(cfg, db, PrintTelegram(), NOW.replace(hour=7), budget(db, api)).checked == 0


# --- Google Hotels (Booking.com, Trip.com, Agoda…) ---------------------------------------------

def gh_answer(props):
    return lambda params: {"properties": [
        {"name": name, "property_token": token, "overall_rating": rating, "deal": deal,
         "gps_coordinates": {"latitude": 35.23, "longitude": 139.10},
         "rate_per_night": {"extracted_lowest": price},
         "prices": [{"source": s, "rate_per_night": {"extracted_lowest": v}} for s, v in sources]}
        for token, name, price, deal, rating, sources in props]}


def test_parse_properties_google_deal_and_sources():
    data = gh_answer([("t1", "箱根の宿", 12000, "32% less than usual", 4.4,
                       [("Booking.com", 12900), ("Trip.com", 12000), ("Agoda", 13400)])])({})
    p = parse_properties(data, "Хаконэ", "2026-10-17", 1)[0]
    assert p.google_pct == 32 and p.price == 12000 and p.rating == 4.4
    assert p.prices == [("Trip.com", 12000), ("Booking.com", 12900), ("Agoda", 13400)]


def test_google_hotels_deal_message_and_one_per_area(cfg):
    db = EventStore(":memory:").db
    api = FakeSerp(google_hotels=gh_answer([
        ("t1", "箱根の宿", 12000, "32% less than usual", 4.4, [("Booking.com", 12900), ("Trip.com", 12000)]),
        ("t2", "箱根ホテル", 9000, "20% less than usual", 4.1, [("Agoda", 9000)]),
        ("t3", "安宿", 5000, "50% less than usual", 3.0, [("Trip.com", 5000)]),       # рейтинг ниже 3.5
        ("t4", "普通の宿", 20000, None, 4.5, [("Booking.com", 20000)])]))
    cfg.google_hotels.areas = cfg.google_hotels.areas[:1]
    cfg.google_hotels.alert_weeks = 1
    sender = PrintTelegram()
    report = check_google_hotels(cfg, db, sender, NOW, budget(db, api))
    assert report.checked == 1 and report.offers == 4 and report.sent == 1
    text, _ = sender.sent[0]
    assert text.startswith("🟠 🏨 −32% · <b>箱根の宿</b> · Хаконэ")
    assert "🏷 Trip.com 12 000 ¥ · Booking.com 12 900 ¥" in text and "сб 17 – вс 18 октября" in text
    assert "по Google — ниже обычного" in text
    params = api.calls[0][1]
    assert params["q"] == "箱根 温泉旅館" and params["check_in_date"] == "2026-10-17" and params["currency"] == "JPY"


def test_google_hotels_use_leftover_budget_and_rotate(cfg):
    db = EventStore(":memory:").db
    api = FakeSerp(google_hotels=gh_answer([]))
    b = budget(db, api)
    b.spent = 4                                          # авиабилеты уже потратили 4 из 8
    report = check_google_hotels(cfg, db, PrintTelegram(), NOW, b)
    assert report.checked == 4 and len(api.calls) == 4
    today = plan_today(cfg, date(2026, 10, 13), 4)
    tomorrow = plan_today(cfg, date(2026, 10, 14), 4)
    assert len({(a.name, d) for a, d, _ in today + tomorrow}) == 8   # завтра — другие места


def test_bad_key_or_no_searches_left_stops_spending():
    db = EventStore(":memory:").db

    class Rejecting(FakeSerp):
        def account(self):
            raise SerpApiError("401 Invalid API key.")

    b = Budget(db, Rejecting(), NOW, 250, 5)
    assert b.free == 0 and b.error and "401" in b.error

    def out_of_searches(params):
        raise SerpApiError("429 Your account has run out of searches.")

    b = budget(db, FakeSerp(x=out_of_searches))
    with contextlib.suppress(SerpApiError):
        b.search("x", {})
    assert not b.take()
