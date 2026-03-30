# pac1-py Agent — Applied Fixes

> Дата: 2026-03-24
> Агент: `pac1-py/agent/` (PAC1 benchmark, PCM runtime)
> Результат: **100% на bitgn/pac1-dev** (anthropic/claude-haiku-4.5, qwen/qwen3.5-9b)

---

## Применённые фиксы

### loop.py

| ID | Строки | Описание |
|----|--------|---------|
| **FIX-27** | 100–140 | Retry-loop (4 попытки, 4s sleep) на transient-ошибки: `503`, `502`, `NoneType`, `overloaded`, `unavailable`, `server error` от OpenRouter/провайдеров |
| **FIX-qwen** | 98, 105–120 | `use_json_object=True` в cfg → `response_format={"type":"json_object"}` вместо Pydantic structured output. Нужен для qwen: structured-режим вызывает token-blowout (10000+ токенов на вывод схемы) |
| **JSON-correction-retry** | 142–158 | После FIX-qwen: если `model_validate_json` провалился — инжектирует correction-hint в лог, делает ещё 1 попытку, затем убирает hint (успех или нет) |
| **FIX-63** | 184–195 | Auto-list родительской директории перед первым `delete` из неё. Предотвращает удаление "вслепую" без знания содержимого папки |
| **DELETED/WRITTEN feedback** | 207–212 | После `delete`/`write`/`mkdir` — вместо сырого proto-JSON возвращает `DELETED: <path>` / `WRITTEN: <path>` / `CREATED DIR: <path>`. Предотвращает повторные удаления после log-компакции (модель "забывает" что уже сделала) |
| **Log compaction** | 47–69, 92 | Скользящее окно: `preserve_prefix` (system + task + prephase) никогда не сжимается; хвост — последние 5 пар assistant/tool; старые пары заменяются кратким summary из last-5 assistant-сообщений |
| **max_steps=30** | 82 | Лимит 30 шагов (не 20) — PAC1-задачи требуют больше шагов (list + read + find + write) |

### prephase.py

| ID | Строки | Описание |
|----|--------|---------|
| **Discovery-first prephase** | 33–101 | До main loop: `tree /` + чтение `AGENTS.MD` (кандидаты: `/AGENTS.MD`, `/AGENTS.md`, `/02_distill/AGENTS.md`). Результат инжектируется в контекст как `preserve_prefix` — никогда не компактируется. Агент получает полную карту vault до первого шага |

### main.py / MODEL_CONFIGS

| ID | Строки | Описание |
|----|--------|---------|
| **MODEL_CONFIGS** | 15–18 | `qwen/qwen3.5-9b`: `max_completion_tokens=4000`, `use_json_object=True`. `anthropic/claude-haiku-4.5`: пустой конфиг (structured output работает нативно) |
| **Итоговая статистика** | 83–95 | Таблица в stdout по завершению: task_id, score, elapsed, проблемы — для сбора логов по CLAUDE.md |

---

## Архитектурные решения (не нумерованные фиксы)

### Discovery-first промпт (prompt.py)

Системный промпт содержит **ноль хардкодных путей vault**. Вся информация о папках поступает из:
1. AGENTS.MD (pre-loaded в prephase)
2. Дерева vault (pre-loaded в prephase)
3. `list`/`find`/`search` вызовов в процессе выполнения задачи

Ключевые правила промпта:
- Каждый путь должен прийти из `list`/`find`/`tree` результата — не конструировать из памяти
- Шаблонные файлы (`_*` или помеченные в AGENTS.MD) — никогда не удалять
- "Keep the diff focused": выполнить все явно запрошенные операции, затем сразу `report_completion`
- Перед записью производного файла — list целевой директории для проверки существования
- Вместо `ask_clarification` — `report_completion` с `OUTCOME_NONE_CLARIFICATION`

### VaultContext — заменён неявным подходом

`VaultContext` (`models.py:10–39`) определён, но **не используется нигде в коде** — мёртвый код.

Вместо структурированного извлечения контекста из AGENTS.MD агент использует:
- **Неявный подход**: полный текст AGENTS.MD + tree инжектируется в контекст LLM как есть
- LLM самостоятельно интерпретирует содержимое AGENTS.MD и определяет роли папок
- Никакого программного парсинга AGENTS.MD нет — только prompt-инструкции

Это работает для claude и qwen-9b, но менее надёжно для слабых моделей.

---

## Ограничения OpenRouter / JSON

### Structured output (Pydantic parse mode)
- `client.beta.chat.completions.parse(response_format=NextStep, ...)` работает только если провайдер поддерживает structured output
- OpenRouter передаёт это провайдеру — **не все провайдеры поддерживают**
- qwen-модели через OpenRouter/Together: structured output вызывает **token-blowout** (модель начинает выводить JSON Schema вместо ответа)
- Решение: `use_json_object=True` → `response_format={"type":"json_object"}` + ручной `model_validate_json`

### json_object режим
- Гарантирует валидный JSON, **но не гарантирует соответствие схеме**
- Поля могут отсутствовать или иметь неверный тип → `ValidationError` → JSON-correction-retry
- Провайдеры **могут игнорировать** `max_completion_tokens` (задокументировано в MEMORY.md)

### Transient-ошибки (FIX-27)
- OpenRouter провайдеры (Venice/Together) имеют **503/502 storms** в часы пик
- `NoneType` ошибки — модель вернула пустой ответ
- Решение: retry 4 раза с 4s sleep, после чего abort

### Итог по json_object vs structured
| Режим | Claude | qwen-9b | qwen-4b/2b |
|-------|--------|---------|------------|
| structured (Pydantic) | ✅ работает | ❌ token-blowout | ❌ token-blowout |
| json_object | ✅ работает | ✅ работает | ✅ работает (с retry) |

---

## FIX-111 — done_operations: server-side ledger + YAML fallback

> Дата: 2026-03-29 | Причина: ministral-3:14b-cloud t03 провал из-за context loss после log compaction

### Проблема

Log compaction (`_compact_log`, `max_tool_pairs=5`) убирает ранние шаги из контекста. Старые пары заменяются summary из assistant-сообщений (намерения), но **user-сообщения с подтверждениями `WRITTEN:`/`DELETED:` не извлекались**. После компакции модель теряла track выполненных операций и пыталась повторно прочитать уже удалённый файл.

Конкретный сбой (t03, ministral-3:14b-cloud v1):
- step 3: `WRITTEN: /01_capture/influential/...` ✅ → через 6 шагов ушло в компакцию
- step 9: `DELETED: /00_inbox/...` ✅
- step 10: модель «не знает» что уже писала → пробует прочитать inbox файл → NOT_FOUND → паника → `OUTCOME_NONE_CLARIFICATION`

### Решение (три слоя)

#### 1. `done_operations` поле в NextStep схеме (`models.py`)

```python
done_operations: List[str] = Field(
    default_factory=list,
    description="Accumulated list of ALL confirmed write/delete/move operations completed so far in this task ..."
)
```

Модель сама несёт накапливаемый список подтверждённых операций в каждом ответе. Structured output (Pydantic/JSON schema) гарантирует наличие поля.

#### 2. Server-side ledger в `preserve_prefix` (`loop.py`)

```python
_done_ops: list[str] = []
_ledger_msg: dict | None = None
```

После каждой успешной write/delete/move/mkdir:
- `_done_ops.append(f"WRITTEN: {path}")` и т.д.
- Создаётся/обновляется `_ledger_msg` и кладётся в `preserve_prefix` (никогда не компактируется)
- Мутация словаря — один элемент в `preserve_prefix` всегда актуален

Это **авторитетный источник** — не зависит от того, правильно ли модель аккумулирует `done_operations`.

FIX-111 injection: если модель вернула `done_operations=[]` при `_done_ops` непустом — заменяем:
```python
if _done_ops and not job.done_operations:
    job = job.model_copy(update={"done_operations": list(_done_ops)})
```

#### 3. Улучшенная компакция (`_compact_log`)

Теперь извлекает `WRITTEN:`/`DELETED:`/`MOVED:`/`CREATED DIR:` из user-сообщений в компактируемой части:
```
Confirmed ops (already done, do NOT redo):
  WRITTEN: /01_capture/influential/...
  WRITTEN: /02_distill/cards/...
```

#### 4. YAML fallback в `_extract_json_from_text`

Для моделей, которые выводят YAML вместо JSON при отсутствии strict JSON schema mode:
```python
try:
    import yaml
    parsed_yaml = yaml.safe_load(stripped)
    if isinstance(parsed_yaml, dict) and any(k in parsed_yaml for k in ("current_state", "function", "tool")):
        return parsed_yaml
except Exception:
    pass
```

### Файлы изменены

| Файл | Изменение |
|------|-----------|
| `agent/models.py` | `done_operations: List[str]` добавлено в `NextStep` |
| `agent/prompt.py` | "ALL 5 FIELDS REQUIRED", пример JSON обновлён, правило для `done_operations` |
| `agent/loop.py` | `_done_ops` + `_ledger_msg` (server ledger), улучшенная `_compact_log`, FIX-111 injection, YAML fallback, JSON retry hint обновлён до 5 полей |
| `pac1-py/CLAUDE.md` | Fix counter → FIX-112 |

### Результат

| Прогон | Модель | Результат | Время |
|--------|--------|-----------|-------|
| v1 (до FIX-111) | ministral-3:14b-cloud | **95.45%** | 2335s |
| v2 (после FIX-111) | ministral-3:14b-cloud | **100.00%** | 1550s |

t03: 11 шагов (провал) → 9 шагов (успех). Время −34%.

---

## Что не применено / мёртвый код

| Элемент | Файл | Статус |
|---------|------|--------|
| `VaultContext` | `models.py:10–39` | Определён, нигде не используется |
| Все sandbox-фиксы (Fix-21–62b) | — | Отсутствуют — их заменяет discovery-first архитектура |
