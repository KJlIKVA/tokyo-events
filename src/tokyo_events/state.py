"""Состояние между запусками: что уже видели, что уже отправили, сколько токенов потрачено.

Один JSON-файл. В GitHub Actions он живёт в ветке `state` (scripts/state.sh).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

VERSION = 1


@dataclass
class Notified:
    key: str
    rank: str
    title: str
    when: str
    where: str
    artist: str
    at: str  # ISO-время отправки


@dataclass
class State:
    seen: dict[str, str] = field(default_factory=dict)          # ключ находки → ISO-время
    sources_ready: list[str] = field(default_factory=list)      # источники, у которых уже есть «база»
    notified: list[Notified] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)        # UTC-дата → токены модели

    # --- просмотренное ---
    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark_seen(self, key: str, now: datetime) -> None:
        self.seen.setdefault(key, now.isoformat(timespec="seconds"))

    # --- токены ---
    def tokens_on(self, day: str) -> int:
        return self.tokens.get(day, 0)

    def add_tokens(self, day: str, n: int) -> None:
        self.tokens[day] = self.tokens.get(day, 0) + n

    # --- память об отправленном ---
    def recent_notified(self, now: datetime, days: int) -> list[Notified]:
        border = now - timedelta(days=days)
        return [n for n in self.notified if datetime.fromisoformat(n.at) >= border]

    def prune(self, now: datetime, seen_days: int, memory_days: int) -> None:
        seen_border = now - timedelta(days=seen_days)
        self.seen = {k: t for k, t in self.seen.items() if datetime.fromisoformat(t) >= seen_border}
        self.notified = self.recent_notified(now, memory_days)
        day_border = (now - timedelta(days=7)).date().isoformat()
        self.tokens = {d: n for d, n in self.tokens.items() if d >= day_border}

    # --- файл ---
    @classmethod
    def load(cls, path: str | Path) -> State:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise ValueError(f"{p}: неизвестная версия состояния {data.get('version')}")
        return cls(
            seen=dict(data.get("seen", {})),
            sources_ready=list(data.get("sources_ready", [])),
            notified=[Notified(**n) for n in data.get("notified", [])],
            tokens=dict(data.get("tokens", {})),
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "seen": self.seen, "sources_ready": self.sources_ready,
                "notified": [asdict(n) for n in self.notified], "tokens": self.tokens}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)  # атомарно: оборванная запись не портит прежнее состояние
