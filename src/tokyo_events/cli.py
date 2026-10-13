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
    classifier = Classifier(cfg, OpenAITransport(cfg.llm)) if env("OPENAI_API_KEY") else None
    sender = PrintTelegram() if args.dry_run else _telegram()
    with httpx.Client(timeout=30) as client:
        report = run(cfg, state, http_fetcher(client), classifier, sender)
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
    samples = [
        ("green", "festival", "", "Осенний фестиваль роз в саду Кюфурукава", "до 31 октября", "Кита, Токио",
         "Сотни сортов роз у старинного особняка, вход в сад платный."),
        ("orange", "exhibition", "", "Выставка Ван Гога", "пример", "Музей в Уэно",
         "Так будет выглядеть оранжевое уведомление — интереснее обычного."),
        ("purple", "concert", "Ado", "Ado — новый тур", "пример", "Tokyo Dome",
         "Так будет выглядеть фиолетовое уведомление: концерт любимого артиста или событие масштаба страны."),
    ]
    for rank, cat, artist, title, when, where, summary in samples:
        v = Verdict(id="t", relevant=True, rank=rank, category=cat, artist=artist, duplicate_of="", title_ru=title,  # type: ignore[arg-type]
                    when_ru=when, where_ru=where, summary_ru=summary, reason="тест")
        text = "Тест Tokyo Events\n\n" + format_message(v, Item(source="test", title=title, link="https://github.com"))
        for problem in tg.send(text, silent=rank == "green"):
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
