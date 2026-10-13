"""Команды:

  tokyo-events run [--dry-run]   — один запуск (так его вызывает GitHub Actions)
  tokyo-events sources           — проверить источники: сколько записей, что прошло бы на оценку (без модели)
  tokyo-events test-telegram     — прислать в Telegram пример уведомления каждого ранга
  tokyo-events setup-telegram    — ввести токен бота и chat id (сохраняются в .env)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from .classify import Classifier, OpenAITransport, Verdict
from .config import env, load_config, load_dotenv
from .enrich import Details, Enricher, Venue
from .pipeline import Report, collect, http_fetcher, run
from .sources import Item
from .state import State
from .telegram import PrintTelegram, Telegram, format_message, parse_chat_ids

STATE_PATH = "state/state.json"


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
    with httpx.Client(timeout=30) as client:
        classifier = enricher = None
        if env("OPENAI_API_KEY"):
            transport = OpenAITransport(cfg.llm)
            classifier, enricher = Classifier(cfg, transport), Enricher(cfg, transport, client)
        report = run(cfg, state, http_fetcher(client), classifier, enricher, sender)
    if args.dry_run:
        print("dry-run: состояние не сохранено, в Telegram ничего не отправлено")
    else:
        state.save(args.state)
    print(report.summary())
    for e in report.errors:
        print("  ошибка:", e)
    # падаем только если не работает ничего: один недоступный источник — не повод для тревоги
    return 1 if _broken(report, len(cfg.sources)) else 0


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
            date_precision="exact", end_date="", price_ru="", is_free=True, official_url="",
            venues=[Venue(name="代々木公園 (Yoyogi Park)", address="東京都渋谷区代々木神園町2-1", dates_ru="")],
            summary_ru="Так выглядит зелёное уведомление: обычное событие, приходит без звука.")),
        ("orange", "exhibition", "", Details(
            found=True, title_ru="Пример: выставка с картиной Ван Гога", dates_ru="10 октября 2026 – 10 января 2027",
            date_precision="exact", end_date="", price_ru="взрослые 2 400 ¥", is_free=False,
            official_url="https://www.cincinnati-art2026.jp/", venues=[ueno],
            summary_ru="Так выглядит оранжевое уведомление: интереснее обычного.")),
        ("purple", "concert", "Ado", Details(
            found=True, title_ru="Пример: тур Ado", dates_ru="март 2027", date_precision="approximate",
            end_date="", price_ru="", is_free=False, official_url="",
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
    print(f"Отправлено 3 тестовых сообщения в {len(tg.chat_ids)} чат(а)")
    return 0


def cmd_setup_telegram(args: argparse.Namespace) -> int:
    from .setup_telegram import main as setup

    return setup()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="tokyo-events")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default=STATE_PATH)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="один запуск")
    r.add_argument("--dry-run", action="store_true",
                   help="печатать уведомления вместо отправки, не сохранять состояние")
    r.set_defaults(func=cmd_run)
    sub.add_parser("sources", help="проверить источники").set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram", help="тестовые уведомления").set_defaults(func=cmd_test_telegram)
    sub.add_parser("setup-telegram", help="ввести токен бота и chat id").set_defaults(func=cmd_setup_telegram)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадёт URL с токеном бота
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
