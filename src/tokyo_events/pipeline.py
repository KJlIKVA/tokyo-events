"""Один запуск: собрать → отсеять старое и виденное → отобрать моделью → найти подробности → в Telegram."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .classify import BudgetExceeded, Classifier, LLMError, Verdict
from .config import Config, Source
from .enrich import Enricher
from .sources import Item, artist_matcher, fetch_source, mentions_artist
from .state import Notified, State
from .telegram import TelegramError, format_message

log = logging.getLogger(__name__)


class Sender(Protocol):
    def send(self, text: str, silent: bool = False) -> list[str]:
        """Список проблем с частью получателей; не дошло никуда — TelegramError."""


Fetcher = Callable[[Source], list[Item]]


@dataclass
class Report:
    fetched: int = 0
    new: int = 0
    candidates: int = 0
    sent: dict[str, int] = field(default_factory=lambda: {"green": 0, "orange": 0, "purple": 0})
    skipped: int = 0
    duplicates: int = 0
    no_dates: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        sent = ", ".join(f"{k}={v}" for k, v in self.sent.items())
        return (f"собрано {self.fetched}, новых {self.new}, на оценку {self.candidates}; отправлено: {sent}; "
                f"не событие/не подходит: {self.skipped}, дублей: {self.duplicates}, без чётких дат: {self.no_dates}; "
                f"ошибок: {len(self.errors)}")


def http_fetcher(client: httpx.Client) -> Fetcher:
    return lambda source: fetch_source(source, client)


def collect(cfg: Config, state: State, fetch: Fetcher, now: datetime, report: Report) -> list[Item]:
    """Новые находки, которые стоит показать модели. Остальное молча помечается просмотренным."""
    matcher = artist_matcher(cfg.artists)
    candidates: dict[str, Item] = {}
    title_keys: set[str] = set()

    def seen(it: Item) -> bool:
        return state.is_seen(it.key) or state.is_seen(it.title_key) or it.key in candidates \
            or it.title_key in title_keys

    def forget(it: Item) -> None:
        _mark_seen(state, it, now)

    for source in cfg.sources:
        try:
            items = fetch(source)
        except Exception as e:  # один упавший источник не должен ронять остальные
            msg = f"источник {source.id}: {type(e).__name__}: {str(e)[:200]}"
            log.warning(msg)
            report.errors.append(msg)
            continue
        report.fetched += len(items)
        fresh = []
        for it in items:
            if not seen(it):
                fresh.append(it)
                title_keys.add(it.title_key)  # дубль заголовка внутри одной ленты тоже отсеиваем
        report.new += len(fresh)
        first_time = source.id not in state.sources_ready
        # первый запуск источника: не вываливать всё за max_age_hours, только самое свежее
        max_age = timedelta(hours=cfg.filters.first_run_hours if first_time else cfg.filters.max_age_hours)
        undated_new = [it for it in fresh if it.published is None]
        # без дат «новое» = «не видели»: при первом запуске или перестройке сайта это всё старое
        baseline_undated = first_time or len(undated_new) > cfg.filters.max_new_undated
        if undated_new and baseline_undated and not first_time:
            log.warning("источник %s: разом %d новых ссылок без дат — похоже на перестройку сайта, запоминаю молча",
                        source.id, len(undated_new))
        for it in fresh:
            stale = baseline_undated if it.published is None else now - it.published > max_age
            off_topic = source.artists_only and not mentions_artist(it, matcher)
            if stale or off_topic:
                forget(it)
            else:
                candidates[it.key] = it
        if first_time:
            state.sources_ready.append(source.id)
    # сначала свежие; без даты — как самые свежие (на страницах без дат это только что появившиеся ссылки)
    return sorted(candidates.values(), key=lambda it: it.published or now, reverse=True)


def run(cfg: Config, state: State, fetch: Fetcher, classifier: Classifier | None, enricher: Enricher | None,
        sender: Sender, now: datetime | None = None) -> Report:
    now = (now or datetime.now(UTC)).astimezone(ZoneInfo(cfg.timezone))
    report = Report()
    candidates = collect(cfg, state, fetch, now, report)
    if len(candidates) > cfg.llm.max_items_per_run:
        log.info("на оценку %d находок, берём %d свежих, остальные — в следующий раз",
                 len(candidates), cfg.llm.max_items_per_run)
        candidates = candidates[: cfg.llm.max_items_per_run]
    report.candidates = len(candidates)
    if candidates and (classifier is None or enricher is None):
        report.errors.append("нет OPENAI_API_KEY — находки не оценены и подождут следующего запуска")
        return _finish(cfg, state, now, report)
    assert classifier is not None and enricher is not None

    ctx = _Ctx(cfg, state, enricher, sender, now, report)
    try:
        for start in range(0, len(candidates), cfg.llm.batch_size):
            batch = candidates[start: start + cfg.llm.batch_size]
            ids = [(f"n{start + i + 1}", it) for i, it in enumerate(batch)]
            memory = state.recent_notified(now, cfg.filters.memory_days)
            try:
                verdicts, tokens = classifier.classify(ids, memory, now, ctx.tokens_left())
            except LLMError as e:
                if isinstance(e, BudgetExceeded):
                    raise
                report.errors.append(f"модель: {e}")
                log.warning("модель: %s", e)
                continue
            ctx.spend(tokens)
            ctx.handle_batch(verdicts, dict(ids))
    except BudgetExceeded as e:
        report.errors.append(str(e))
        log.warning("%s — остальное подождёт следующего запуска", e)
    return _finish(cfg, state, now, report)


def _mark_seen(state: State, item: Item, now: datetime) -> None:
    state.mark_seen(item.key, now)
    state.mark_seen(item.title_key, now)


def _effective_rank(v: Verdict) -> str:
    # правило пользователя: концерт любимого артиста — всегда фиолетовый
    if v.relevant and v.artist and v.category == "concert":
        return "purple"
    return v.rank if v.relevant else "none"


@dataclass
class _Ctx:
    cfg: Config
    state: State
    enricher: Enricher
    sender: Sender
    now: datetime
    report: Report
    enriched: int = 0

    def _day(self) -> str:
        return self.now.astimezone(UTC).date().isoformat()

    def tokens_left(self) -> int:
        return self.cfg.llm.daily_token_limit - self.state.tokens_on(self._day())

    def spend(self, tokens: int) -> None:
        self.state.add_tokens(self._day(), tokens)

    def handle_batch(self, verdicts: list[Verdict], items: dict[str, Item]) -> None:
        notify = set(self.cfg.filters.notify_ranks)
        wanted = {v.id for v in verdicts if _effective_rank(v) in notify}
        dropped: set[str] = set()  # id находок пакета, отброшенных как дубли
        for v in verdicts:
            self._handle(v, items[v.id], wanted, dropped)

    def _is_duplicate(self, v: Verdict, wanted: set[str], dropped: set[str]) -> bool:
        ref = v.duplicate_of.strip()
        if not ref or ref == v.id:
            return False
        if ref in {n.key for n in self.state.notified}:
            return True
        if ref.startswith("n") and ref[1:].isdigit():
            # дубль находки этого же пакета — независимо от порядка: «оригинал» отправится сам,
            # если он подходит и сам не отброшен как дубль (так из пары взаимных ссылок останется одна)
            return ref in wanted and ref not in dropped
        log.warning("непонятная ссылка на дубль %r — считаю дублем, как решила модель", ref)
        return True

    def _handle(self, v: Verdict, item: Item, wanted: set[str], dropped: set[str]) -> bool:
        """True — уведомление отправлено."""
        cfg, state, now, report = self.cfg, self.state, self.now, self.report
        rank = _effective_rank(v)
        log.info("[%s] %s — %s (%s)%s%s", rank, item.title[:80], v.reason, item.source,
                 f" дубль={v.duplicate_of}" if v.duplicate_of else "",
                 f" обновление={v.update_of}" if v.update_of else "")
        if rank == "none" or rank not in cfg.filters.notify_ranks:
            report.skipped += 1
            _mark_seen(state, item, now)
            return False
        if self._is_duplicate(v, wanted, dropped):
            report.duplicates += 1
            dropped.add(v.id)
            _mark_seen(state, item, now)
            return False
        if self.enriched >= cfg.llm.enrich.max_per_run:
            log.info("лимит поиска подробностей за запуск — «%s» подождёт следующего запуска", item.title[:60])
            return False

        previous = next((n for n in state.notified if n.key == v.update_of.strip()), None) if v.update_of else None
        try:
            details, tokens = self.enricher.enrich(item, v, previous, now, self.tokens_left())
        except LLMError as e:
            if isinstance(e, BudgetExceeded):
                raise
            report.errors.append(f"модель: {e}")
            log.warning("подробности «%s»: %s", item.title[:60], e)
            return False  # не помечаем просмотренным: попробуем в следующий запуск
        self.enriched += 1
        self.spend(tokens)
        if not details.found:
            log.info("   не подтвердилось — пропуск")
            report.skipped += 1
            _mark_seen(state, item, now)
            return False
        if not details.has_dates:
            # «2027 год» — слишком размыто: пропускаем; когда объявят даты, придёт новая новость
            log.info("   нет чётких дат (%s: %s) — ждём, пока объявят", details.date_precision, details.dates_ru)
            report.no_dates += 1
            _mark_seen(state, item, now)
            return False
        # открытие: end_date — день открытия; недавно открывшееся (до 2 недель) — всё ещё новость
        grace = timedelta(days=14) if v.category == "opening" else timedelta(0)
        if details.ended(now.date() - grace):
            log.info("   уже закончилось (%s) — пропуск", details.end_date)
            report.skipped += 1
            _mark_seen(state, item, now)
            return False

        v = v.model_copy(update={"rank": rank})
        try:
            problems = self.sender.send(format_message(v, item, details, update=previous is not None),
                                        silent=rank in cfg.filters.silent_ranks)
        except TelegramError as e:
            report.errors.append(str(e))
            log.warning("%s", e)
            return False  # не помечаем просмотренным: попробуем в следующий запуск
        # часть аккаунтов не получила — сообщаем, но не повторяем: остальным пришёл бы дубль
        report.errors += [f"Telegram: {p}" for p in problems]
        _mark_seen(state, item, now)
        state.notified.append(Notified(
            key=item.key, rank=rank, title=details.title_ru or v.title_ru or item.title, when=details.dates_ru,
            where=", ".join(x.name for x in details.venues[:3]), artist=v.artist,
            at=now.isoformat(timespec="seconds")))
        report.sent[rank] += 1
        return True


def _finish(cfg: Config, state: State, now: datetime, report: Report) -> Report:
    state.prune(now, cfg.filters.seen_days, cfg.filters.memory_days)
    log.info("итог: %s", report.summary())
    return report
