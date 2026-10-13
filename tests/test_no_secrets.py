"""Репозиторий публичный: в отслеживаемых файлах не должно быть токенов."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return [p for p in Path(".").rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts]
    return [Path(p) for p in out.splitlines() if Path(p).is_file()]


def test_no_secrets_in_repo():
    found = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found += [f"{path}: {name}" for name, rx in PATTERNS.items() if rx.search(text)]
    assert not found, found


def test_env_is_ignored():
    assert ".env" in Path(".gitignore").read_text().splitlines()
