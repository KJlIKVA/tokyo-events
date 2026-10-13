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


def hotel(no, name, plans, review=4.4):
    """Запись отеля в формате VacantHotelSearch: plans = [(price_total, dinner, breakfast, plan_name)]."""
    rooms = []
    for price, dinner, breakfast, plan in plans:
        rooms.append({"roomBasicInfo": {"planName": plan, "withDinnerFlag": dinner, "withBreakfastFlag": breakfast,
                                        "reserveUrl": f"https://travel.rakuten.co.jp/{no}/{plan}"}})
        rooms.append({"dailyCharge": {"stayDate": "x", "rakutenCharge": price // 2, "total": price, "chargeFlag": 0}})
    return {"hotel": [{"hotelBasicInfo": {"hotelNo": no, "hotelName": name, "reviewAverage": review,
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

    def search(self, area, checkin, adults, nights, page=1):
        self.calls.append((checkin, nights))
        out = []
        for no in self.hotels:
            p = self.price(no, checkin, nights)
            if p:
                out.append(hotel(no, self.names.get(no, f"Рёкан {no}"), [(p, 1, 1, "2食付き")]))
        return out


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
    assert "💴 10 000 ¥ за ночь вместо 35 000 ¥ (обычно в этот день недели) · −71%" in text
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


def test_same_day_deal_comes_first_with_sound(cfg):
    only(cfg, ("today", 1), ("sat", 1))
    # сегодня (вт) дешевле других вторников на 20% — зелёная скидка, но «сегодня» — со звуком и первой
    api = FakeApi(lambda no, d, n: 24000 if d == TODAY else (12000 if d == SAT else 30000))
    report, sender, _ = run_hotels(cfg, api)
    assert report.sent == 2
    first, silent = sender.sent[0]
    assert first.startswith("🟢 ♨️ −20%") and "⚡ Сегодня, вт 13 – ср 14 октября" in first and not silent
    assert sender.sent[1][0].startswith("🟣 ♨️ −60%")


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
    api = FakeApi(lambda no, d, n: ((26000 if no == 1 else 20000) if d == SAT else 30000), hotels=(1, 2))
    _, sender, _ = run_hotels(cfg, api)
    heads = {t.split(" · ")[0]: s for t, s in sender.sent}
    assert heads == {"🟠 ♨️ −33%": False, "🟢 ♨️ −13%": True}  # зелёные — без звука


def test_price_drop_vs_history_and_no_repeat(cfg):
    only(cfg, ("sat", 1))
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

    def search(area, checkin, adults, nights, page=1):
        out = orig(area, checkin, adults, nights)
        for h in out:
            h["hotel"][0]["hotelBasicInfo"]["reviewAverage"] = 2.9
        return out

    api.search = search
    report, _, _ = run_hotels(cfg, api)
    assert report.sent == 0


def test_runs_only_at_configured_hours_and_needs_keys(cfg, monkeypatch):
    only(cfg, ("sat", 1))
    api = FakeApi(lambda no, d, n: 10000 if d == SAT else 35000)
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=13))
    assert report.checked == 0 and not api.calls
    report, _, _ = run_hotels(cfg, api, now=NOW.replace(hour=13), force=True)
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
        return httpx.Response(200, json={"hotels": [hotel(1, "A", [(20000, 1, 1, "p")])]})

    api = Rakuten("APP", "KEY", httpx.Client(transport=httpx.MockTransport(handler)),
                  origin="https://kjlikva.github.io", sleep=lambda s: None)
    area = HotelArea(name="Хаконэ", lat=35.2, lon=139.1, radius_km=3, onsen=True)
    assert len(api.search(area, date(2026, 10, 17), 2, 1)) == 1
    q = seen[0].url.params
    assert (q["applicationId"], q["checkoutDate"], q["adultNum"], q["datumType"], q["squeezeCondition"],
            q["searchPattern"], q["sort"]) == ("APP", "2026-10-18", "2", "1", "onsen", "1", "+roomCharge")
    assert seen[0].headers["accessKey"] == "KEY" and seen[0].headers["Origin"] == "https://kjlikva.github.io"
    assert api.search(area, date(2026, 10, 24), 2, 1) == []  # свободных номеров нет
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
