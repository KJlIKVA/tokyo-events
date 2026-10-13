from __future__ import annotations

from tokyo_events.events_db import Event
from tokyo_events.pipeline import run
from tokyo_events.state import State

from .conftest import NOW, FailingTelegram, FakeFetcher, FakeTransport, PartlyFailingTelegram, details, item


def ready(cfg) -> State:
    """Состояние после первого запуска: у всех источников уже есть «база»."""
    return State(sources_ready=[s.id for s in cfg.sources])


def test_ranks_sound_and_artist_concert_is_always_purple(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {
        "Фестиваль в парке": {"rank": "green", "category": "festival", "title_ru": "Фестиваль"},
        "Ван Гог в Уэно": {"rank": "orange", "category": "exhibition", "title_ru": "Ван Гог"},
        # модель ошиблась с рангом — правило пользователя сильнее
        "Ado 全国ツアー開催決定": {"rank": "green", "category": "concert", "artist": "Ado", "title_ru": "Ado — тур"},
    }
    fetch = FakeFetcher({
        "gn_tokyo_festivals": [item("Фестиваль в парке"), item("Ван Гог в Уэно")],
        "gn_natalie_artists": [item("Ado 全国ツアー開催決定", source="gn_natalie_artists")],
    })
    transport.details = {"Фестиваль в парке": details("Фестиваль"), "Ван Гог в Уэно": details("Ван Гог"),
                         "Ado 全国ツアー開催決定": details("Ado — тур")}
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent == {"green": 1, "orange": 1, "purple": 1}
    texts = {t.split("\n")[0]: silent for t, silent in sender.sent}
    assert texts == {"🟢 <b>Фестиваль</b>": True, "🟠 <b>Ван Гог</b>": False, "🟣 <b>Ado — тур</b>": False}
    assert sum("Артист: Ado." in pr for pr in transport.enrich_prompts) == 1


def test_second_run_sends_nothing_again(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {"Фестиваль": {"rank": "green", "category": "festival"}}
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]})
    run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    calls = len(transport.prompts)
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.candidates == 0 and len(sender.sent) == 1 and len(transport.prompts) == calls


def test_old_items_are_skipped_silently(cfg, store, classifier, enricher, transport, sender):
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Старое", hours_ago=100), item("Свежее", hours_ago=2)]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.candidates == 1
    assert "Старое" not in transport.prompts[0]
    assert state.is_seen(item("Старое").key)  # больше не всплывёт


def test_first_run_takes_only_last_hours(cfg, store, classifier, enricher, transport, sender):
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Вчерашнее", hours_ago=30), item("Утреннее", hours_ago=3)]})
    report = run(cfg, State(), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.candidates == 1 and "Утреннее" in transport.prompts[0]


def test_undated_page_baseline_then_new_links(cfg, store, classifier, enricher, transport, sender):
    state = State()
    old = [item(f"news {i}", source="radwimps_official", hours_ago=None, artist="RADWIMPS") for i in range(10)]
    report = run(cfg, state, store, FakeFetcher({"radwimps_official": old}), classifier, enricher, sender, now=NOW)
    assert report.candidates == 0  # первый запуск страницы без дат — всё старое

    new = item("RADWIMPS ツアー2027開催決定", source="radwimps_official", hours_ago=None, artist="RADWIMPS")
    transport.rules = {new.title: {"rank": "purple", "category": "concert", "artist": "RADWIMPS"}}
    report = run(cfg, state, store, FakeFetcher({"radwimps_official": [new, *old]}), classifier, enricher, sender, now=NOW)
    assert report.sent["purple"] == 1
    assert "официальный источник: RADWIMPS" in transport.prompts[-1]


def test_site_rebuild_with_many_new_undated_links_is_not_news(cfg, store, classifier, enricher, sender):
    state = ready(cfg)
    flood = [item(f"x {i}", source="yorushika_official", hours_ago=None) for i in range(cfg.filters.max_new_undated + 1)]
    report = run(cfg, state, store, FakeFetcher({"yorushika_official": flood}), classifier, enricher, sender, now=NOW)
    assert report.candidates == 0 and all(state.is_seen(it.key) for it in flood)


def test_artists_only_sources_need_an_artist_mention(cfg, store, classifier, enricher, transport, sender):
    fetch = FakeFetcher({"gn_natalie_artists": [
        item("Adoのドームツアー決定", source="gn_natalie_artists"),            # японский текст вплотную к имени
        item("Adobeの新機能", source="gn_natalie_artists"),                    # Ado внутри слова — не она
        item("他のバンドがツアー", source="gn_natalie_artists"),
        item("新ツアー", source="gn_natalie_artists", summary="美波が全国ツアーを開催"),
    ]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.candidates == 2
    assert "Adoのドーム" in transport.prompts[0] and "Adobe" not in transport.prompts[0]


def test_duplicates_of_sent_events_are_dropped(cfg, store, classifier, enricher, transport, sender):
    state = ready(cfg)
    store.add(Event(id="abc123", rank="orange", category="exhibition", artist="", title="Ван Гог",
                    first_sent_at=NOW.isoformat()))
    transport.rules = {
        "Ван Гог снова": {"rank": "orange", "category": "exhibition", "duplicate_of": "abc123"},
        "Фест A": {"rank": "green", "category": "festival"},
        "Фест A (другое СМИ)": {"rank": "green", "category": "festival", "duplicate_of": "n2"},
    }
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Ван Гог снова", hours_ago=1), item("Фест A", hours_ago=2),
                                                 item("Фест A (другое СМИ)", hours_ago=3)]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.duplicates == 2 and report.sent["green"] == 1
    assert "abc123 | orange | Ван Гог | ? | ? | ?" in transport.prompts[0]  # модель видит, что уже отправлено
    assert len(transport.enrich_prompts) == 1  # на дубли не тратим второй шаг


def test_duplicate_of_skipped_item_is_still_sent(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {
        "Реклама": {"rank": "none"},
        "Событие": {"rank": "green", "category": "festival", "duplicate_of": "n1"},
    }
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Реклама", hours_ago=1), item("Событие", hours_ago=2)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1


def test_gallery_copies_of_one_article_are_one_item(cfg, store, classifier, enricher, sender):
    fetch = FakeFetcher({
        "gn_tokyo_openings": [item("画像29 / 45＞「グランスタ東京」がオープン - ウォーカープラス", source="gn_tokyo_openings"),
                              item("画像1 / 45＞「グランスタ東京」がオープン - ウォーカープラス", source="gn_tokyo_openings")],
        "gn_tokyo_en": [item("＜画像3 / 9＞「グランスタ東京」がオープン - Yahoo!ニュース", source="gn_tokyo_en")],
    })
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.candidates == 1


def test_telegram_failure_keeps_item_for_next_run(cfg, classifier, store, enricher, transport):
    transport.rules = {"Фестиваль": {"rank": "green", "category": "festival"}}
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]})
    report = run(cfg, state, store, fetch, classifier, enricher, FailingTelegram(), now=NOW)
    assert report.errors and not state.is_seen(item("Фестиваль").key)


def test_llm_failure_keeps_items_and_reports_error(cfg, store, classifier, enricher, transport, sender):
    transport.fail = True
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.errors and not state.is_seen(item("Фестиваль").key)


def test_daily_token_limit_stops_calls(cfg, store, sender):
    from tokyo_events.classify import Classifier
    from tokyo_events.enrich import Enricher

    transport = FakeTransport(tokens=990_000)
    classifier, enricher = Classifier(cfg, transport), Enricher(cfg, transport)
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item(f"Событие {i}", hours_ago=1 + i / 10) for i in range(12)]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert len(transport.prompts) == 1  # второй пакет вылез бы за 1 000 000 токенов
    assert any("лимит токенов" in e for e in report.errors)
    assert state.tokens_on("2026-10-13") == 990_000


def test_one_broken_source_does_not_stop_others(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {"Фестиваль": {"rank": "green", "category": "festival"}}
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]})
    fetch.fail = {"gn_natalie_artists"}
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1 and len(report.errors) == 1


def test_no_api_key_leaves_items_for_later(cfg, store, sender):
    state = ready(cfg)
    report = run(cfg, state, store, FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]}), None, None, sender, now=NOW)
    assert report.errors and not state.is_seen(item("Фестиваль").key)


def test_rank_filter_from_config(cfg, store, classifier, enricher, transport, sender):
    cfg.filters.notify_ranks = ["orange", "purple"]
    transport.rules = {"Фестиваль": {"rank": "green", "category": "festival"}}
    report = run(cfg, ready(cfg), store, FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]}), classifier, enricher, sender,
                 now=NOW)
    assert report.sent["green"] == 0 and report.skipped == 1


def test_partial_telegram_failure_is_reported_but_not_resent(cfg, store, classifier, enricher, transport):
    transport.rules = {"Фестиваль": {"rank": "green", "category": "festival"}}
    state = ready(cfg)
    report = run(cfg, state, store, FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]}), classifier, enricher,
                 PartlyFailingTelegram(), now=NOW)
    assert report.sent["green"] == 1 and state.is_seen(item("Фестиваль").key)
    assert report.errors == ["Telegram: чат …789: Telegram: 403 Forbidden: bot was blocked by the user"]


# --- шаг 2: подробности, даты, обновления ---------------------------------------------------------

def green(*titles):
    return {t: {"rank": "green", "category": "festival"} for t in titles}


def test_no_clear_dates_means_no_notification_yet(cfg, store, classifier, enricher, transport, sender):
    transport.rules = green("Открытие в 2027", "Где-то весной", "Неизвестно когда")
    transport.details = {
        "Открытие в 2027": details(dates_ru="2027 год", date_precision="year_only"),
        "Где-то весной": details(dates_ru="весна 2027", date_precision="approximate", end_date=""),
        "Неизвестно когда": details(dates_ru="", date_precision="unknown"),
    }
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Открытие в 2027", hours_ago=1), item("Где-то весной", hours_ago=2),
                                                 item("Неизвестно когда", hours_ago=3)]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1 and report.no_dates == 2  # «весна 2027» — примерные даты, это можно
    assert "📅 весна 2027" in sender.sent[0][0]
    # пропущенные без дат не попали в базу: новость с датами потом придёт как новое событие
    assert [(e.title, e.dates_ru, e.date_precision) for e in store.recent()] == [
        ("Где-то весной", "весна 2027", "approximate")]


def test_unconfirmed_or_ended_events_are_skipped(cfg, store, classifier, enricher, transport, sender):
    transport.rules = green("Не подтвердилось", "Уже прошло")
    transport.details = {"Не подтвердилось": details(found=False),
                         "Уже прошло": details(end_date="2026-10-01")}
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("Не подтвердилось"), item("Уже прошло", hours_ago=2)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 0 and report.skipped == 2


def sent_event(store, **kw):
    e = Event(id="k1", rank="purple", category="concert", artist="Ado", title="Ado — тур", dates_ru="весна 2027",
              date_precision="approximate", venues=[{"name": "東京ドーム", "address": "", "area_ru": "",
                                                   "on_map": True, "dates_ru": ""}],
              first_sent_at=NOW.isoformat(), updated_at=NOW.isoformat())
    for k, val in kw.items():
        setattr(e, k, val)
    store.add(e)
    return e


def update_rules(title):
    return {title: {"rank": "purple", "category": "concert", "artist": "Ado", "update_of": "k1"}}


def test_update_with_new_dates_and_price_is_red(cfg, store, classifier, enricher, transport, sender):
    sent_event(store)
    title = "Ado ツアー日程・チケット料金発表"
    transport.rules = update_rules(title)
    transport.details = {title: details("Ado — тур", dates_ru="11–12 марта 2027", start_date="2027-03-11",
                                        end_date="2027-03-12", price_ru="9 900 ¥",
                                        venues=[{"name": "東京ドーム", "address": "", "area_ru": "", "on_map": True,
                                                 "dates_ru": ""}])}
    fetch = FakeFetcher({"gn_natalie_artists": [item(title, source="gn_natalie_artists")]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.updates == 1 and report.sent["purple"] == 0
    text, silent = sender.sent[0]
    assert text.startswith("🔴 <b>Обновление: Ado — тур</b>") and not silent  # обновление — со звуком
    assert "Что изменилось:\n• Даты уточнились: весна 2027 → 11–12 марта 2027\n• Объявлена цена: 9 900 ¥" in text
    assert "Возможно, это ОБНОВЛЕНИЕ уже отправленного события «Ado — тур»" in transport.enrich_prompts[0]
    e = store.get("k1")
    assert (e.dates_ru, e.price_ru, e.date_precision, e.updates_sent) == ("11–12 марта 2027", "9 900 ¥", "exact", 1)
    assert [c[1] for c in store.changes("k1")] == ["dates", "price"]


def test_update_without_changes_is_silent(cfg, store, classifier, enricher, transport, sender):
    sent_event(store, dates_ru="11–12 марта 2027", start_date="2027-03-11", end_date="2027-03-12",
               date_precision="exact", price_ru="9 900 ¥")
    title = "Ado ツアー詳細"
    transport.rules = update_rules(title)
    transport.details = {title: details("Ado — тур", dates_ru="11 и 12 марта 2027", start_date="2027-03-11",
                                        end_date="2027-03-12", price_ru="9,900円",
                                        venues=[{"name": "東京ドーム (Tokyo Dome)", "address": "", "area_ru": "",
                                                 "on_map": True, "dates_ru": ""}])}
    state = ready(cfg)
    fetch = FakeFetcher({"gn_natalie_artists": [item(title, source="gn_natalie_artists")]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    # даты те же (другими словами), цена та же (другой записью), площадка та же (с английским названием)
    assert report.updates == 0 and not sender.sent and report.duplicates == 1
    assert state.is_seen(item(title).key)


def test_cancellation_is_red_even_without_dates(cfg, store, classifier, enricher, transport, sender):
    sent_event(store)
    title = "Ado 公演中止"
    transport.rules = update_rules(title)
    transport.details = {title: details(dates_ru="", date_precision="unknown", start_date="", end_date="",
                                        status="cancelled", venues=[])}
    fetch = FakeFetcher({"gn_natalie_artists": [item(title, source="gn_natalie_artists")]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.updates == 1
    assert "• Отменено: по плану → отменено" in sender.sent[0][0]
    assert "📅 весна 2027" in sender.sent[0][0]  # остальное — из базы
    assert store.get("k1").status == "cancelled"


def test_only_one_update_check_per_event_per_run(cfg, store, classifier, enricher, transport, sender):
    sent_event(store)
    titles = ["Ado 日程発表 (СМИ 1)", "Ado 日程発表 (СМИ 2)", "Ado 日程発表 (СМИ 3)"]
    transport.rules = {t: update_rules(t)[t] for t in titles}
    fetch = FakeFetcher({"gn_natalie_artists": [item(t, source="gn_natalie_artists", hours_ago=1 + i / 10)
                                                for i, t in enumerate(titles)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert len(transport.enrich_prompts) == 1 and report.updates == 1 and report.duplicates == 2


def test_update_of_unknown_key_is_treated_as_new_event(cfg, store, classifier, enricher, transport, sender):
    title = "Ado 新ツアー"
    transport.rules = {title: {"rank": "purple", "category": "concert", "artist": "Ado", "update_of": "нет-такого"}}
    fetch = FakeFetcher({"gn_natalie_artists": [item(title, source="gn_natalie_artists")]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["purple"] == 1 and report.updates == 0 and store.count() == 1


def test_sources_are_recorded(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {"Фест": {"rank": "green", "category": "festival"}}
    it = item("Фест")
    run(cfg, ready(cfg), store, FakeFetcher({"gn_tokyo_festivals": [it]}), classifier, enricher, sender, now=NOW)
    rows = store.db.execute("SELECT event_id, item_key, kind FROM event_sources").fetchall()
    assert [tuple(r) for r in rows] == [(it.key, it.key, "new")]


def test_duplicate_pointing_forward_in_batch_is_dropped(cfg, store, classifier, enricher, transport, sender):
    # модель пометила дублем ПЕРВУЮ статью, сославшись на вторую — отправиться должна ровно одна
    transport.rules = {
        "Выставка JJK (статья 1)": {"rank": "green", "category": "exhibition", "duplicate_of": "n2"},
        "Выставка JJK (статья 2)": {"rank": "green", "category": "exhibition"},
    }
    fetch = FakeFetcher({"gn_tokyo_en": [item("Выставка JJK (статья 1)", source="gn_tokyo_en", hours_ago=1),
                                          item("Выставка JJK (статья 2)", source="gn_tokyo_en", hours_ago=2)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1 and report.duplicates == 1
    assert len(transport.enrich_prompts) == 1  # на дубль не тратим поиск


def test_mutual_duplicates_keep_one(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {
        "A": {"rank": "green", "category": "festival", "duplicate_of": "n2"},
        "B": {"rank": "green", "category": "festival", "duplicate_of": "n1"},
    }
    fetch = FakeFetcher({"gn_tokyo_festivals": [item("A", hours_ago=1), item("B", hours_ago=2)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1 and report.duplicates == 1


def test_enrich_failure_keeps_item_for_next_run(cfg, classifier, store, enricher, transport, sender):
    transport.rules = green("Фестиваль")
    transport.fail_enrich = True
    state = ready(cfg)
    report = run(cfg, state, store, FakeFetcher({"gn_tokyo_festivals": [item("Фестиваль")]}), classifier, enricher, sender,
                 now=NOW)
    assert report.sent["green"] == 0 and report.errors and not state.is_seen(item("Фестиваль").key)


def test_enrich_limit_per_run(cfg, classifier, store, enricher, transport, sender):
    cfg.llm.enrich.max_per_run = 2
    titles = [f"Событие {i}" for i in range(4)]
    transport.rules = green(*titles)
    state = ready(cfg)
    fetch = FakeFetcher({"gn_tokyo_festivals": [item(t, hours_ago=1 + i / 10) for i, t in enumerate(titles)]})
    report = run(cfg, state, store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 2 and len(transport.enrich_prompts) == 2
    assert not state.is_seen(item("Событие 3").key)  # подождёт следующего запуска


def test_recent_opening_is_still_news(cfg, store, classifier, enricher, transport, sender):
    transport.rules = {"Новый парк": {"rank": "green", "category": "opening"},
                       "Старое открытие": {"rank": "green", "category": "opening"}}
    transport.details = {"Новый парк": details(end_date="2026-10-10"),       # открылся 3 дня назад
                         "Старое открытие": details(end_date="2026-09-01")}  # давно
    fetch = FakeFetcher({"gn_tokyo_openings": [item("Новый парк", source="gn_tokyo_openings"),
                                                item("Старое открытие", source="gn_tokyo_openings", hours_ago=2)]})
    report = run(cfg, ready(cfg), store, fetch, classifier, enricher, sender, now=NOW)
    assert report.sent["green"] == 1 and report.skipped == 1
