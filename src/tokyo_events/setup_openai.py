"""Свой ключ OpenAI для проекта: `uv run tokyo-events setup-openai`.

Спрашивает ключ (ввод скрыт), проверяет его пробным вызовом gpt-5.6-terra, сохраняет в .env
(файл не попадает в git) и, если у репозитория есть GitHub, обновляет секрет OPENAI_API_KEY.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from typing import Any

from .config import ALLOWED_MODELS
from .setup_telegram import github_repo, set_github_secret, update_env

MODEL = ALLOWED_MODELS[0]

STEPS = f"""\
Свой ключ OpenAI для Tokyo Events

1) platform.openai.com → Settings → Projects → Create project, название «Tokyo Events».
   Отдельный проект — отдельный счёт расходов и свой лимит, не смешивается с другими ключами.
2) В проекте «Tokyo Events»:
   • Limits — поставьте месячный бюджет (например, $40) и оповещение;
     если есть список разрешённых моделей — оставьте только {MODEL}.
   • API keys → Create new secret key → проект «Tokyo Events».
     Права (Permissions): Restricted — достаточно доступа к Responses API (Write),
     остальное можно выключить. Названия пунктов в кабинете могут немного отличаться.
3) Скопируйте ключ (sk-…) — OpenAI показывает его один раз — и вставьте ниже.
"""


def _client(key: str) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=key, timeout=60, max_retries=1)


def check_key(key: str) -> str | None:
    """Пробный вызов модели. None — всё хорошо, иначе понятная причина."""
    import openai

    try:
        resp = _client(key).responses.create(model=MODEL, input="Ответь одним словом: готово",
                                             reasoning={"effort": "low"}, max_output_tokens=200, store=False)
    except openai.AuthenticationError:
        return "OpenAI не принял ключ — проверьте, что скопировали его целиком."
    except openai.PermissionDeniedError as e:
        return (f"У ключа нет доступа к {MODEL} или к Responses API — проверьте права ключа "
                f"и разрешённые модели проекта. ({str(e)[:150]})")
    except openai.NotFoundError:
        return f"Модель {MODEL} недоступна этому проекту/организации."
    except openai.RateLimitError as e:
        if "quota" in str(e).lower():
            return "На счёте нет средств или исчерпан лимит проекта — пополните баланс в Billing."
        return "OpenAI просит подождать (слишком много запросов) — попробуйте через минуту."
    except openai.OpenAIError as e:
        return f"Не удалось проверить ключ: {type(e).__name__}"
    if resp.status not in ("completed", "incomplete"):
        return f"Неожиданный ответ модели: {resp.status}"
    return None


def main(env_path: Path = Path(".env")) -> int:
    print(STEPS)
    for _ in range(3):
        key = getpass.getpass("Ключ OpenAI (ввод не отображается): ").strip()
        if not key.startswith("sk-") or len(key) < 40:
            print("  Не похоже на ключ OpenAI (начинается с sk-…). Попробуйте ещё раз.")
            continue
        print(f"  Проверяю ключ пробным вызовом {MODEL} (стоит доли цента)…")
        problem = check_key(key)
        if problem:
            print(f"  ✗ {problem}")
            continue
        break
    else:
        print("\nКлюч не сохранён. Запустите команду снова, когда будет готов.")
        return 1

    print(f"  ✓ Ключ работает, {MODEL} доступна")
    update_env(env_path, {"OPENAI_API_KEY": key})
    print(f"\n✓ Сохранено в {env_path.resolve()} (в git не попадает; прежний ключ в этом файле заменён)")

    if github_repo():
        answer = input("Обновить секрет OPENAI_API_KEY в GitHub, чтобы бот на Actions работал с новым ключом? [Y/n]: ")
        if answer.strip().lower() in ("", "y", "д", "да", "yes"):
            ok = set_github_secret("OPENAI_API_KEY", key)
            print("✓ GitHub Secrets обновлены — со следующего запуска бот работает на новом ключе" if ok
                  else "✗ Не получилось — проверьте `gh auth status`")
    return 0
