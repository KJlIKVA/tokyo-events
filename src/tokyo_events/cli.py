"""Команды:

  tokyo-events run [--dry-run]   — один запуск (так его вызывает GitHub Actions)
  tokyo-events sources           — проверить источники: сколько записей, что прошло бы на оценку (без модели)
  tokyo-events test-telegram     — прислать в Telegram пример уведомления каждого ранга
  tokyo-events setup-telegram    — ввести токен бота и chat id (сохраняются в .env)
  tokyo-events setup-openai      — ввести свой ключ OpenAI (проверка, .env и GitHub Secrets)
  tokyo-events setup-rakuten     — ключи 楽天トラベル: цены отелей и рёканов
  tokyo-events events [--github] — показать базу событий и историю обновлений
  tokyo-events clear-chat        — удалить сообщения бота во всех чатах (Telegram даёт удалять не старше 48 часов)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .classify import Classifier, OpenAITransport, Verdict
from .config import env, load_config, load_dotenv
from .enrich import Details, Enricher, Venue
from .events_db import Event, EventStore, diff
from .hotels import check_hotels
from .pipeline import Report, collect, http_fetcher, run
from .sources import Item
from .state import State
from .telegram import CIRCLES, PrintTelegram, Telegram, format_message, format_update, parse_chat_ids

STATE_PATH = "state/state.json"
EVENTS_DB = "state/events.db"


def _telegram() -> Telegram:
    return Telegram(env("TELEGRAM_BOT_TOKEN", required=True) or "",
                    parse_chat_ids(env("TELEGRAM_CHAT_ID", required=True) or ""))


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    if not args.dry_run and not (env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")):
        # бот ещё не подключён (setup-telegram): ничего не трогаем — память и находки дождутся настройки
        print("::warning::Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — запуск пропущен. "
              "Подключите бота: uv run tokyo-events setup-telegram")
        return 0
    sender = PrintTelegram() if args.dry_run else _telegram()
    store = EventStore(args.events)
    migrate_notified(state, store)
    with httpx.Client(timeout=30) as client:
        classifier = enricher = None
        if env("OPENAI_API_KEY"):
            transport = OpenAITransport(cfg.llm)
            classifier, enricher = Classifier(cfg, transport), Enricher(cfg, transport, client)
        report = run(cfg, state, store, http_fetcher(client), classifier, enricher, sender)
        now = datetime.now(UTC).astimezone(ZoneInfo(cfg.timezone))
        hotels = check_hotels(cfg, store, sender, now, client, force=args.hotels_now)
    if args.dry_run:
        print("dry-run: состояние и база не сохранены, в Telegram ничего не отправлено")
    else:
        store.commit()
        store.compact()
        state.save(args.state)
    store.close()
    print(report.summary())
    if hotels.checked or hotels.errors:
        print(f"отели: запросов {hotels.checked}, предложений {hotels.offers}, скидок отправлено {hotels.sent}")
    for e in report.errors + hotels.errors:
        print("  ошибка:", e)
    # падаем только если не работает ничего: один недоступный источник — не повод для тревоги
    hotels_broken = bool(hotels.errors) and hotels.checked == 0
    return 1 if _broken(report, len(cfg.sources)) or hotels_broken else 0


def migrate_notified(state: State, store: EventStore) -> None:
    """Старая память «отправленного» (список в state.json) → база событий. Один раз."""
    if not state.notified:
        return
    known = store.ids()
    for n in state.notified:
        if n.key not in known:
            store.add(Event(id=n.key, rank=n.rank, category="other", artist=n.artist, title=n.title,
                            dates_ru=n.when, venues=[{"name": x, "address": "", "area_ru": "", "on_map": True,
                                                      "dates_ru": ""} for x in n.where.split(", ") if x],
                            first_sent_at=n.at, updated_at=n.at))
    state.notified = []


def cmd_events(args: argparse.Namespace) -> int:
    """Показать базу событий. --github — скачать свежую из ветки state публичного репозитория."""
    path = args.events
    if args.github:
        import subprocess
        import tempfile

        remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
        repo = remote.removesuffix(".git").split("github.com/")[-1]
        r = httpx.get(f"https://raw.githubusercontent.com/{repo}/state/events.db", follow_redirects=True, timeout=30)
        if r.status_code != 200:
            print(f"Базы на GitHub пока нет ({r.status_code}) — она появится после первого запуска с базой")
            return 1
        path = tempfile.mkstemp(suffix=".db")[1]
        Path(path).write_bytes(r.content)
    store = EventStore(path)
    events = store.recent(limit=1000)
    print(f"Событий в базе: {len(events)}\n")
    for e in events:
        print(f"{CIRCLES.get(e.rank, '?')} {e.title}")
        print(f"   📅 {e.dates_ru or '—'}   💴 {e.price_ru or ('бесплатно' if e.is_free else '—')}"
              f"   📍 {e.where or '—'}" + (f"   [{e.status}]" if e.status != "scheduled" else ""))
        for at, fld, old, new in store.changes(e.id):
            print(f"   🔴 {at[:16]} {fld}: {old or '—'} → {new}")
        print()
    store.close()
    return 0


def _broken(report: Report, n_sources: int) -> bool:
    source_errors = sum(1 for e in report.errors if e.startswith("источник "))
    other_errors = len(report.errors) - source_errors
    return source_errors == n_sources or other_errors > 0


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(UTC).astimezone(tz)
    with httpx.Client(timeout=30) as client:
        fetch = http_fetcher(client)
        counts: dict[str, int] = {}

        def counting(source):  # type: ignore[no-untyped-def]
            items = fetch(source)
            counts[source.id] = len(items)
            return items

        report = Report()
        candidates = collect(cfg, state, counting, now, report)
    for s in cfg.sources:
        print(f"{s.id:24} {counts.get(s.id, 'ОШИБКА')!s:>6} записей")
    for e in report.errors:
        print("  ", e)
    print(f"\nПрошло бы на оценку моделью: {len(candidates)}")
    for it in candidates[:40]:
        when = it.published.astimezone(tz).strftime("%m-%d %H:%M") if it.published else "без даты"
        print(f"  {when:11} [{it.source}] {it.title[:100]}")
    return 0


def cmd_test_telegram(args: argparse.Namespace) -> int:
    tg = _telegram()
    ueno = Venue(name="上野の森美術館 (Ueno Royal Museum)", address="東京都台東区上野公園1-2", dates_ru="")
    samples = [
        ("green", "festival", "", Details(
            found=True, title_ru="Пример: осенний фестиваль в парке", dates_ru="1–3 ноября 2026, 10:00–17:00",
            date_precision="exact", start_date="", end_date="", status="scheduled", location="tokyo", discount_pct=0,
            price_ru="", is_free=True,
            official_url="",
            venues=[Venue(name="代々木公園 (Yoyogi Park)", address="東京都渋谷区代々木神園町2-1", dates_ru="")],
            summary_ru="Так выглядит зелёное уведомление: обычное событие, приходит без звука.")),
        ("orange", "exhibition", "", Details(
            found=True, title_ru="Пример: выставка с картиной Ван Гога", dates_ru="10 октября 2026 – 10 января 2027",
            date_precision="exact", start_date="", end_date="", status="scheduled", location="tokyo", discount_pct=0,
            price_ru="взрослые 2 400 ¥",
            is_free=False,
            official_url="https://www.cincinnati-art2026.jp/", venues=[ueno],
            summary_ru="Так выглядит оранжевое уведомление: интереснее обычного.")),
        ("purple", "concert", "Ado", Details(
            found=True, title_ru="Пример: тур Ado", dates_ru="март 2027", date_precision="approximate",
            start_date="", end_date="", status="scheduled", location="tokyo", discount_pct=0,
            price_ru="", is_free=False, official_url="",
            venues=[Venue(name="東京ドーム (Tokyo Dome)", address="東京都文京区後楽1-3-61", dates_ru="11–12 марта"),
                    Venue(name="京セラドーム大阪 (Kyocera Dome Osaka)", address="大阪市西区千代崎3-中2-1",
                          dates_ru="25–26 марта")],
            summary_ru="Так выглядит фиолетовое уведомление: концерт любимого артиста или событие масштаба страны.")),
    ]
    for rank, cat, artist, details in samples:
        v = Verdict(id="t", relevant=True, rank=rank, category=cat, artist=artist, duplicate_of="",  # type: ignore[arg-type]
                    update_of="", title_ru=details.title_ru, reason="тест")
        item = Item(source="test", title=details.title_ru, link="https://github.com/KJlIKVA/tokyo-events")
        for problem in tg.send("Тест Tokyo Events\n\n" + format_message(v, item, details), silent=rank == "green"):
            print("  не дошло:", problem)
    # пример 🔴 обновления: у тура объявили цену и добавили площадку
    rank, cat, artist, details = samples[2]
    event = Event(id="t", rank=rank, category=cat, artist=artist, title=details.title_ru, dates_ru=details.dates_ru,
                  venues=[details.venues[0].model_dump()])
    new = details.model_copy(update={"price_ru": "9 900 ¥", "summary_ru": "Так выглядит красное уведомление: "
                                     "по уже присланному событию появилось что-то новое."})
    v = Verdict(id="t", relevant=True, rank=rank, category=cat, artist=artist, duplicate_of="",  # type: ignore[arg-type]
                update_of="t", title_ru=details.title_ru, reason="тест")
    item = Item(source="test", title=details.title_ru, link="https://github.com/KJlIKVA/tokyo-events")
    changes = diff(event, new.model_dump())
    for problem in tg.send("Тест Tokyo Events\n\n" + format_update(event, v, item, new, changes)):
        print("  не дошло:", problem)
    print(f"Отправлено 4 тестовых сообщения в {len(tg.chat_ids)} чат(а)")
    return 0


def cmd_setup_telegram(args: argparse.Namespace) -> int:
    from .setup_telegram import main as setup

    return setup()


def cmd_clear_chat(args: argparse.Namespace) -> int:
    tg = _telegram()
    if not args.yes and input(f"Удалить сообщения бота в {len(tg.chat_ids)} чат(ах)? [y/N]: ").strip().lower() != "y":
        print("Отменено")
        return 1
    keep = tg.own_messages_hint()
    for chat_id in tg.chat_ids:
        n = tg.clear(chat_id, keep.get(chat_id, set()))
        print(f"чат …{chat_id[-3:]}: очищено (проверено {n} сообщений, ваши собственные не трогали)")
    return 0


def cmd_setup_rakuten(args: argparse.Namespace) -> int:
    from .setup_rakuten import main as setup

    return setup()


def cmd_setup_openai(args: argparse.Namespace) -> int:
    from .setup_openai import main as setup

    return setup()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="tokyo-events")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default=STATE_PATH)
    p.add_argument("--events", default=EVENTS_DB, help="база событий (SQLite)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="один запуск")
    r.add_argument("--hotels-now", action="store_true",
                   help="проверить цены отелей сейчас, не дожидаясь часа")
    r.add_argument("--dry-run", action="store_true",
                   help="печатать уведомления вместо отправки, не сохранять состояние")
    r.set_defaults(func=cmd_run)
    sub.add_parser("sources", help="проверить источники").set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram", help="тестовые уведомления").set_defaults(func=cmd_test_telegram)
    sub.add_parser("setup-telegram", help="ввести токен бота и chat id").set_defaults(func=cmd_setup_telegram)
    sub.add_parser("setup-openai", help="ввести свой ключ OpenAI").set_defaults(func=cmd_setup_openai)
    sub.add_parser("setup-rakuten", help="ключи 楽天トラベル для цен отелей").set_defaults(func=cmd_setup_rakuten)
    ev = sub.add_parser("events", help="показать базу событий")
    ev.add_argument("--github", action="store_true", help="взять свежую базу из GitHub (ветка state)")
    ev.set_defaults(func=cmd_events)
    c = sub.add_parser("clear-chat", help="удалить сообщения бота во всех чатах (не старше 48 часов)")
    c.add_argument("--yes", action="store_true", help="без подтверждения")
    c.set_defaults(func=cmd_clear_chat)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадёт URL с токеном бота
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
