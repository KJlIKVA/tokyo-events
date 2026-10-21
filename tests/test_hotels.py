from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from tokyo_events.config import HotelArea, Stay
from tokyo_events.events_db import EventStore
from tokyo_events.hotels import (
    Rakuten,
    check_hotels,
    find_deals,
    format_deal,
    parse_offers,
    room_price,
    saturdays,
    stay_dates,
    stay_ru,
)
from tokyo_events.telegram import PrintTelegram

TODAY = date(2026, 10, 13)  # вторник
NOW = datetime(2026, 10, 13, 9, 5, tzinfo=ZoneInfo("Asia/Tokyo"))


def hotel(no, name, plans, review=4.4, min_charge=None):
    """Запись отеля в формате VacantHotelSearch: plans = [(price_total, dinner, breakfast, plan_name)]."""
    rooms = []
    for price, dinner, breakfast, plan in plans:
        rooms.append({"roomBasicInfo": {"planName": plan, "withDinnerFlag": dinner, "withBreakfastFlag": breakfast,
                                        "reserveUrl": f"https://travel.rakuten.co.jp/{no}/{plan}"}})
        rooms.append({"dailyCharge": {"stayDate": "x", "rakutenCharge": price // 2, "total": price, "chargeFlag": 0}})
    return {"hotel": [{"hotelBasicInfo": {"hotelNo": no, "hotelName": name, "reviewAverage": review,
                                          "hotelMinCharge": min_charge,
                                          "address1": "神奈川県", "address2": "足柄下郡箱根町湯本1",
                                          "planListUrl": f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}"}},
                      {"roomInfo": rooms}]}


def test_stay_dates_and_text():
    assert saturdays(TODAY, 3) == [date(2026, 10, 17), date(2026, 10, 24), date(2026, 10, 31)]
    assert saturdays(date(2026, 10, 17), 1) == [date(2026, 10, 17)]  # в субботу — эта же суббота
    assert stay_dates(Stay(checkin="fri", nights=2), TODAY, 2) == [date(2026, 10, 16), date(2026, 10, 23)]
    assert stay_dates(Stay(checkin="today"), TODAY, 3) == [date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27)]
    assert stay_ru("2026-10-16", 1) == "пт 16 – сб 17 октября"
    assert stay_ru("2026-10-30", 2) == "пт 30 октября – вс 1 ноября (2 ночи)"


def test_parse_offers_picks_cheapest_two_meal_plan():
    raw = [hotel(1, "Рёкан А", [(18000, 0, 0, "素泊まり"), (30000, 1, 1, "2食付き"), (26000, 1, 1, "早割2食")])]
    offers = parse_offers(raw, "Хаконэ", "2026-10-17", 1, adults=2, two_meals=True)
    assert [(o.name, o.price, o.plan_name) for o in offers] == [("Рёкан А", 26000, "早割2食")]
    assert offers[0].url == "https://travel.rakuten.co.jp/1/早割2食" and offers[0].review == 4.4
    assert parse_offers(raw, "Хаконэ", "2026-10-17", 1, 2, two_meals=False)[0].price == 18000


def test_room_price_variants():
    assert room_price({"total": 30000, "rakutenCharge": 15000, "chargeFlag": 0}, 2) == 30000
    assert room_price({"rakutenCharge": 15000, "chargeFlag": 0}, 2) == 30000   # за человека × 2
    assert room_price({"rakutenCharge": 15000, "chargeFlag": 1}, 2) == 15000   # за номер


class FakeApi:
    """price(hotel_no, checkin, nights) → цена 2食 или None (нет номеров)."""

    def __init__(self, price, hotels=(1,), names=None):
        self.price, self.hotels, self.names = price, hotels, names or {}
        self.calls = []

    def search(self, area, checkin, adults, nights, two_meals=False, page=1):
        self.calls.append((checkin, nights))
        out = []
        for no in self.hotels:
            p = self.price(no, checkin, nights)
            if p:
                out.append(hotel(no, self.names.get(no, f"Рёкан {no}"), [(p, 1, 1, "2食付き")]))
        return out, 1


def only(cfg, *stays):
    cfg.hotels.stays = [Stay(checkin=c, nights=n) for c, n in stays]


def run_hotels(cfg, api, store=None, now=NOW, force=False):
    cfg.hotels.areas = [HotelArea(name="Хаконэ-Юмото", lat=35.23, lon=139.10)]
    store = store or EventStore(":memory:")
    sender = PrintTelegram()
    report = check_hotels(cfg, store, sender, now, httpx.Client(), force=force, api=api)
    return report, sender, store


SAT = date(2026, 10, 17)


def test_ryokan_35k_to_10k_is_purple(cfg):
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: (10000 if d == SAT else 35000) if no == 1 else 30000, hotels=(1, 2),
                  names={1: "Рёкан Юмото"})
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 1 and len(api.calls) == 4  # 4 субботы
    text, silent = sender.sent[0]
    assert text.startswith("🟣 ♨️ −71% · <b>Рёкан Юмото</b> · Хаконэ-Юмото") and not silent
    assert "📅 сб 17 – вс 18 октября · 2 взрослых · ужин и завтрак" in text
    assert "💴 10 000 ¥ за ночь вместо 35 000 ¥ (самое дешёвое в другие недели) · −71%" in text
    assert "Забронировать на Rakuten Travel" in text and "google.com/maps" in text


def test_friday_two_nights_compared_with_other_fridays(cfg):
    only(cfg, ("fri", 1), ("fri", 2))
    fri = date(2026, 10, 16)
    # пятница на 2 ночи на ближайшей неделе дешевле; пятница на 1 ночь — как обычно
    api = FakeApi(lambda no, d, n: 18000 if (d == fri and n == 2) else 30000)
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 1
    assert "📅 пт 16 – вс 18 октября (2 ночи)" in sender.sent[0][0] and "за первую ночь" in sender.sent[0][0]
    assert len(api.calls) == 8  # 4 пятницы × (1 и 2 ночи)


def test_same_day_deal_has_sound_and_order_is_by_color(cfg):
    only(cfg, ("today", 1), ("sat", 1))
    cfg.hotels.max_per_area = 5
    # сегодня (вт) дешевле других вторников на 65% — со звуком и первой, хотя на субботу −70%: цвет тот же
    api = FakeApi(lambda no, d, n: 10500 if d == TODAY else (9000 if d == SAT else 30000))
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 2
    today_text, silent = sender.sent[0]
    assert today_text.startswith("🟣 ♨️ −65%") and "⚡ Сегодня, вт 13 – ср 14 октября" in today_text and not silent
    assert sender.sent[1][0].startswith("🟣 ♨️ −70%")


def test_today_and_tomorrow_only_with_big_discount(cfg):
    """Правило пользователя (21.10.2026): заезд сегодня или завтра — только при скидке от 60%, иначе не успеть."""
    only(cfg, ("today", 1), ("fri", 1))
    cfg.hotels.max_per_area = 5
    thursday = NOW.replace(day=15)  # завтра пятница
    fri = date(2026, 10, 16)
    api = FakeApi(lambda no, d, n: 15000 if d in (date(2026, 10, 15), fri) else 30000)  # −50% сегодня и в пятницу
    report, _, _ = run_hotels(cfg, api, now=thursday)
    assert report.sent == 0
    api = FakeApi(lambda no, d, n: 12000 if d == fri else 30000)  # пятница −60%
    report, sender, _ = run_hotels(cfg, api, now=thursday)
    assert report.sent == 1 and sender.sent[0][0].startswith("🟣 ♨️ −60%")
    api = FakeApi(lambda no, d, n: 15000 if d == fri else 30000)  # во вторник та же пятница −50% — через 3 дня, можно
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 1


def test_price_history_stores_only_changes(cfg):
    """Одна и та же цена при каждой проверке — одна строка: иначе база растёт на ~15 МБ в день."""
    only(cfg, ("sat", 1))
    store = EventStore(":memory:")
    for hour in (9, 15, 19):
        run_hotels(cfg, FakeApi(lambda no, d, n: 30000), store, now=NOW.replace(hour=hour))
    rows = store.db.execute("SELECT price, seen FROM hotel_price_runs ORDER BY stay_date").fetchall()
    assert [tuple(r) for r in rows] == [(30000, 3)] * 4
    run_hotels(cfg, FakeApi(lambda no, d, n: 25000), store, now=NOW.replace(day=14, hour=9))
    assert store.db.execute("SELECT COUNT(*) FROM hotel_price_runs").fetchone()[0] == 8


def test_old_price_table_is_migrated(cfg):
    from tokyo_events.hotels import prepare_db

    db = EventStore(":memory:").db
    db.execute("CREATE TABLE hotel_prices (hotel_no TEXT, stay_date TEXT, nights INTEGER, observed_at TEXT, "
               "price INTEGER, area TEXT, plan_name TEXT, raw TEXT)")
    obs = [("09", 30000), ("15", 30000), ("19", 20000), ("21", 30000)]
    db.executemany("INSERT INTO hotel_prices VALUES ('1', '2026-10-17', 1, ?, ?, 'x', '', '')",
                   [(f"2026-10-13T{h}:00:00+09:00", p) for h, p in obs])
    prepare_db(db)
    rows = db.execute("SELECT price, last_at, seen FROM hotel_price_runs ORDER BY last_at").fetchall()
    assert [tuple(r) for r in rows] == [(30000, "2026-10-13T15:00:00+09:00", 2),
                                        (20000, "2026-10-13T19:00:00+09:00", 1),
                                        (30000, "2026-10-13T21:00:00+09:00", 1)]
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'hotel_prices'").fetchone()


def test_one_best_deal_per_area_per_run(cfg):
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: ({1: 20000, 2: 10000, 3: 25000}[no] if d == SAT else 30000), hotels=(1, 2, 3))
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 1 and sender.sent[0][0].startswith("🟣 ♨️ −67%")


def test_only_nearest_weeks_are_alerted(cfg):
    only(cfg, ("sat", 1))
    third = date(2026, 10, 31)  # 3-я суббота: дешевле, но о ней не сообщаем (alert_weeks = 2)
    api = FakeApi(lambda no, d, n: 10000 if d == third else 30000)
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0


def test_small_discounts_are_ignored(cfg):
    only(cfg, ("sat", 1))
    # −5% — меньше порога; у второго −12%, но экономия 1 200 ¥ — меньше min_saving_yen
    api = FakeApi(lambda no, d, n: (28500 if d == SAT else 30000) if no == 1 else (8800 if d == SAT else 10000),
                  hotels=(1, 2))
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0 and report.offers == 8


def test_color_tiers_and_silence(cfg):
    only(cfg, ("sat", 1))
    cfg.hotels.max_per_area = 5
    api = FakeApi(lambda no, d, n: ((26000 if no == 1 else 20000) if d == SAT else 30000), hotels=(1, 2))
    _, sender, _ = run_hotels(cfg, api)
    heads = {t.split(" · ")[0]: s for t, s in sender.sent}
    assert heads == {"🟠 ♨️ −33%": False, "🟢 ♨️ −13%": True}  # зелёные — без звука


def test_price_drop_vs_history_and_no_repeat(cfg):
    only(cfg, ("sat", 1))
    cfg.hotels.max_per_area = 5
    store = EventStore(":memory:")
    report, _, store = run_hotels(cfg, FakeApi(lambda no, d, n: 30000), store)
    assert report.sent == 0
    evening = NOW.replace(hour=19)
    api = FakeApi(lambda no, d, n: 20000)  # подешевело везде — к обычной цене не скидка, к прошлой — да
    report, sender, store = run_hotels(cfg, api, store, now=evening)
    assert report.sent == 2 and "(раньше на эту дату)" in sender.sent[0][0]
    report, _, store = run_hotels(cfg, api, store, now=evening + timedelta(hours=2), force=True)
    assert report.sent == 0  # не повторяем
    report, sender, store = run_hotels(cfg, FakeApi(lambda no, d, n: 15000), store,
                                       now=evening + timedelta(hours=4), force=True)
    assert report.sent == 2 and all(t.startswith("🔴 Ещё дешевле") for t, _ in sender.sent)


def test_low_rated_hotels_skipped(cfg):
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: 10000 if d == SAT else 35000)
    orig = api.search

    def search(area, checkin, adults, nights, two_meals=False, page=1):
        out, pages = orig(area, checkin, adults, nights)
        for h in out:
            h["hotel"][0]["hotelBasicInfo"]["reviewAverage"] = 2.9
        return out, pages

    api.search = search
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0


def test_runs_only_at_configured_hours_and_needs_keys(cfg, monkeypatch):
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: 10000 if d == SAT else 35000)
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=7))
    assert report.checked == 0 and not api.calls
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=7), force=True)
    assert report.sent == 1
    monkeypatch.delenv("RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("RAKUTEN_ACCESS_KEY", raising=False)
    cfg.hotels.areas = [HotelArea(name="x", lat=1, lon=1)]
    assert check_hotels(cfg, EventStore(":memory:"), PrintTelegram(), NOW, httpx.Client()).checked == 0


def test_api_down_stops_early(cfg):
    from tokyo_events.hotels import RakutenError

    class Down:
        calls = 0

        def search(self, *a, **k):
            Down.calls += 1
            raise RakutenError("403 origin")

    only(cfg, ("sat", 1), ("fri", 1))
    report, _, _ = run_hotels(cfg, Down())
    assert Down.calls == 5 and report.checked == 0 and len(report.errors) == 5


def test_rakuten_request_params_and_errors():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params["checkinDate"] == "2026-10-24":
            return httpx.Response(404, json={"error": "not_found"})
        if request.url.params["checkinDate"] == "2026-10-31":
            return httpx.Response(403, json={"errors": {"errorCode": 403, "errorMessage": "Invalid Access Key"}})
        return httpx.Response(200, json={"hotels": [hotel(1, "A", [(20000, 1, 1, "p")])],
                                         "pagingInfo": {"pageCount": 3}})

    api = Rakuten("APP", "KEY", httpx.Client(transport=httpx.MockTransport(handler)),
                  origin="https://kjlikva.github.io", sleep=lambda s: None)
    area = HotelArea(name="Хаконэ", lat=35.2, lon=139.1, radius_km=3, onsen=True)
    hotels, pages = api.search(area, date(2026, 10, 17), 2, 1, two_meals=True)
    assert len(hotels) == 1 and pages == 3
    q = seen[0].url.params
    assert (q["applicationId"], q["checkoutDate"], q["adultNum"], q["datumType"], q["squeezeCondition"],
            q["searchPattern"], q["sort"]) == ("APP", "2026-10-18", "2", "1", "onsen,dinner,breakfast", "0",
                                               "+roomCharge")
    assert seen[0].headers["accessKey"] == "KEY" and seen[0].headers["Origin"] == "https://kjlikva.github.io"
    assert api.search(area, date(2026, 10, 24), 2, 1) == ([], 0)  # свободных номеров нет
    try:
        api.search(area, date(2026, 10, 31), 2, 1)
        raise AssertionError("ожидалась ошибка")
    except Exception as e:
        assert "403 Invalid Access Key" in str(e)


def test_find_deals_needs_enough_other_weeks(cfg):
    from tokyo_events.hotels import SCHEMA, Offer

    store = EventStore(":memory:")
    store.db.executescript(SCHEMA)
    sats = [s.isoformat() for s in saturdays(TODAY, 2)]
    offers = [Offer("1", "A", "x", sats[0], 1, 10000, "", "u"), Offer("1", "A", "x", sats[1], 1, 35000, "", "u")]
    assert find_deals(store.db, offers, {(sats[0], 1)}, cfg, TODAY) == []  # одна другая неделя — мало


def test_format_deal_escapes(cfg):
    from tokyo_events.hotels import Deal, Offer

    o = Offer("1", "Рёкан <A&B>", "Хаконэ", "2026-10-17", 1, 10000, "план <1>", "https://x.jp/?a=1&b=2", review=4.5)
    text = format_deal(Deal(o, 35000, 71, "usual", "purple"), cfg)
    assert "<b>Рёкан &lt;A&amp;B&gt;</b>" in text and 'href="https://x.jp/?a=1&amp;b=2"' in text
    assert "⭐ 4.5 · план: «план &lt;1&gt;»" in text and text.endswith("#отели #Хаконэ")


def test_more_pages_in_big_cities(cfg):
    only(cfg, ("sat", 1))
    cfg.hotels.max_pages = 2
    cfg.hotels.max_per_area = 5

    class Paged(FakeApi):
        def search(self, area, checkin, adults, nights, two_meals=False, page=1):
            self.calls.append((checkin, page))
            p = 10000 if checkin == SAT else 35000
            return [hotel(page, f"Отель {page}", [(p, 1, 1, "2食")])], 5

    api = Paged(lambda *a: None)
    report, _, _ = run_hotels(cfg, api)
    assert len(api.calls) == 8 and report.offers == 8 and report.sent == 2  # 4 субботы × 2 страницы из 5


def test_null_fields_from_api():
    raw = [hotel(1, "Рёкан", [(30000, 1, 1, "x")])]
    raw[0]["hotel"][1]["roomInfo"][0]["roomBasicInfo"]["planName"] = None   # бывает в живом API
    raw[0]["hotel"][0]["hotelBasicInfo"]["address2"] = None
    o = parse_offers(raw, "Хаконэ", "2026-10-17", 1, 2, two_meals=True)[0]
    assert o.plan_name == "" and o.address == "神奈川県"


def test_delayed_schedule_catches_up_once(cfg):
    """GitHub запустил «15 часов» в 16:40 — проверка всё равно проходит, но один раз."""
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: 30000)
    store = EventStore(":memory:")
    run_hotels(cfg, api, store=store)                                    # 9:05 — плановая
    calls = len(api.calls)
    run_hotels(cfg, api, store=store, now=NOW.replace(hour=13))          # с 9 ч уже проверяли
    assert len(api.calls) == calls
    run_hotels(cfg, api, store=store, now=NOW.replace(hour=16, minute=40))  # 15-часовая опоздала
    assert len(api.calls) > calls
    calls = len(api.calls)
    run_hotels(cfg, api, store=store, now=NOW.replace(hour=17))
    assert len(api.calls) == calls


def test_no_fake_deal_when_cheap_rooms_on_other_dates_sold_out(cfg):
    """Урэсино 13.10.2026: на другие пятницы остался только люкс за 330 000 ¥, а минимум отеля — 9 900 ¥
    с человека: 79 200 ¥ — не скидка. Настоящая скидка рядом с минимумом проходит."""
    only(cfg, ("sat", 1))
    offers = []
    for d, price in ((SAT, 79200), (SAT + timedelta(weeks=1), 330000), (SAT + timedelta(weeks=2), 330000),
                     (SAT + timedelta(weeks=3), 330000)):
        offers += parse_offers([hotel(1, "Бэссо", [(price, 1, 1, "2食")], min_charge=9900)], "Урэсино",
                               d.isoformat(), 1, 2, True)
    alert = {(SAT.isoformat(), 1)}
    db = EventStore(":memory:").db
    from tokyo_events.hotels import SCHEMA
    db.executescript(SCHEMA)
    assert find_deals(db, offers, alert, cfg, TODAY) == []
    for o in offers:
        o.min_charge = 45000                       # минимум 45 000 ¥ с человека — 79 200 ¥ за двоих правда дёшево
    assert [d.pct for d in find_deals(db, offers, alert, cfg, TODAY)] == [76]


def test_usual_price_is_cheapest_other_week_not_median(cfg):
    only(cfg, ("sat", 1))
    prices = {SAT: 10000, SAT + timedelta(weeks=1): 20000, SAT + timedelta(weeks=2): 35000,
              SAT + timedelta(weeks=3): 35000}
    offers = [o for d, price in prices.items()
              for o in parse_offers([hotel(1, "Рёкан", [(price, 1, 1, "2食")], min_charge=5000)], "Хаконэ",
                                    d.isoformat(), 1, 2, True)]
    db = EventStore(":memory:").db
    from tokyo_events.hotels import SCHEMA
    db.executescript(SCHEMA)
    deals = find_deals(db, offers, {(SAT.isoformat(), 1)}, cfg, TODAY)
    assert [(d.baseline, d.pct) for d in deals] == [(20000, 50)]
