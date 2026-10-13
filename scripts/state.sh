#!/usr/bin/env bash
# Память бота живёт в ветке `state`: один коммит без истории, чтобы файлы не раздували репозиторий.
#   state/state.json — что уже видели, расход токенов;
#   state/events.db  — база событий (SQLite): отправленное, подробности, история обновлений.
#
#   scripts/state.sh pull — скачать оба файла из ветки state
#   scripts/state.sh push — сохранить оба файла в ветку state
#
# Защиты: pull отличает «ветки ещё нет» от сетевой ошибки (при ошибке — стоп, никакого пустого состояния:
# иначе бот забыл бы всё отправленное и прислал повторно); push — только после успешного pull,
# только целые файлы и только если ветку никто не изменил за это время (--force-with-lease).
set -euo pipefail
JSON=state/state.json
DB=state/events.db
MARK=state/.pulled   # SHA скачанного коммита или «none», если ветки не было
REMOTE="${STATE_REMOTE:-https://x-access-token:${GITHUB_TOKEN:?нужен GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY:?нужен GITHUB_REPOSITORY}.git}"

check_json() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("version")==1, "не та версия"; print(len(d.get("seen",{})))' "$1"
}

check_db() {
  python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("state: база событий повреждена")
tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print(db.execute("SELECT COUNT(*) FROM events").fetchone()[0] if "events" in tables else 0)
PY
}

case "${1:-}" in
  pull)
    mkdir -p state
    rm -f "$MARK" "$JSON.tmp" "$DB.tmp"
    set +e
    git ls-remote --exit-code --heads "$REMOTE" state >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 2 ]; then
      echo "state: ветки state ещё нет — начинаем с пустого состояния"
      echo none > "$MARK"
      exit 0
    elif [ "$rc" -ne 0 ]; then
      echo "state: не удалось связаться с репозиторием (код $rc) — останавливаюсь" >&2
      exit 1
    fi
    git fetch -q --depth=1 "$REMOTE" state
    sha=$(git rev-parse FETCH_HEAD)
    git show FETCH_HEAD:state.json > "$JSON.tmp"
    seen=$(check_json "$JSON.tmp")
    events=0
    if git cat-file -e FETCH_HEAD:events.db 2>/dev/null; then
      git show FETCH_HEAD:events.db > "$DB.tmp"
      events=$(check_db "$DB.tmp")
      mv "$DB.tmp" "$DB"
    fi
    mv "$JSON.tmp" "$JSON"
    echo "$sha" > "$MARK"
    echo "state: загружен коммит ${sha:0:7}, просмотрено: $seen, событий в базе: $events"
    ;;
  push)
    if [ ! -f "$MARK" ]; then
      echo "state: pull не выполнен или не удался — сохранять нельзя" >&2
      exit 1
    fi
    if [ ! -f "$JSON" ]; then echo "state: нечего сохранять"; exit 0; fi
    seen=$(check_json "$JSON")
    tmp=$(mktemp -d)
    cp "$JSON" "$tmp/state.json"
    events=0
    if [ -f "$DB" ]; then
      events=$(check_db "$DB")
      cp "$DB" "$tmp/events.db"
    fi
    expected=$(cat "$MARK")
    [ "$expected" = none ] && expected=""
    printf 'Служебная ветка: память бота Tokyo Events (state.json — что уже видели; events.db — база событий). Пишет только GitHub Actions.\n' > "$tmp/README.md"
    git -C "$tmp" init -q -b state
    git -C "$tmp" add -A
    git -C "$tmp" -c user.name="tokyo-events-bot" -c user.email="tokyo-events-bot@users.noreply.github.com" \
      commit -q -m "state: run ${GITHUB_RUN_ID:-local}"
    for attempt in 1 2 3; do
      if git -C "$tmp" push -q "--force-with-lease=state:$expected" "$REMOTE" state; then
        echo "state: сохранено (просмотрено: $seen, событий в базе: $events)"
        exit 0
      fi
      echo "state: push не удался (попытка $attempt)" >&2
      sleep $((attempt * 5))
    done
    echo "state: не удалось сохранить состояние" >&2
    exit 1
    ;;
  *)
    echo "использование: $0 pull|push" >&2
    exit 2
    ;;
esac
