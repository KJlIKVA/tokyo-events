#!/usr/bin/env bash
# Состояние (state/state.json) живёт в ветке `state`: один коммит без истории, чтобы файл не раздувал репозиторий.
#
#   scripts/state.sh pull — скачать state/state.json из ветки state
#   scripts/state.sh push — сохранить state/state.json в ветку state
#
# Защиты: pull отличает «ветки ещё нет» от сетевой ошибки (при ошибке — стоп, никакого пустого состояния:
# иначе бот забыл бы всё отправленное и прислал повторно); push — только после успешного pull,
# только корректный JSON и только если ветку никто не изменил за это время (--force-with-lease).
set -euo pipefail
FILE=state/state.json
MARK=state/.pulled   # SHA скачанного коммита или «none», если ветки не было
REMOTE="${STATE_REMOTE:-https://x-access-token:${GITHUB_TOKEN:?нужен GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY:?нужен GITHUB_REPOSITORY}.git}"

check_json() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("version")==1, "не та версия"; print(len(d.get("seen",{})))' "$1"
}

case "${1:-}" in
  pull)
    mkdir -p state
    rm -f "$MARK" "$FILE.tmp"
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
    git show FETCH_HEAD:state.json > "$FILE.tmp"
    n=$(check_json "$FILE.tmp")
    mv "$FILE.tmp" "$FILE"
    echo "$sha" > "$MARK"
    echo "state: загружен коммит ${sha:0:7}, просмотрено: $n"
    ;;
  push)
    if [ ! -f "$MARK" ]; then
      echo "state: pull не выполнен или не удался — сохранять нельзя" >&2
      exit 1
    fi
    if [ ! -f "$FILE" ]; then echo "state: нечего сохранять"; exit 0; fi
    n=$(check_json "$FILE")
    expected=$(cat "$MARK")
    [ "$expected" = none ] && expected=""
    tmp=$(mktemp -d)
    cp "$FILE" "$tmp/state.json"
    printf 'Служебная ветка: память бота Tokyo Events (что уже видели и отправили). Пишет только GitHub Actions.\n' > "$tmp/README.md"
    git -C "$tmp" init -q -b state
    git -C "$tmp" add -A
    git -C "$tmp" -c user.name="tokyo-events-bot" -c user.email="tokyo-events-bot@users.noreply.github.com" \
      commit -q -m "state: run ${GITHUB_RUN_ID:-local}"
    for attempt in 1 2 3; do
      if git -C "$tmp" push -q "--force-with-lease=state:$expected" "$REMOTE" state; then
        echo "state: сохранено (просмотрено: $n)"
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
