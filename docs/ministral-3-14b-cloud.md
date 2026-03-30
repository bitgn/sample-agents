# ministral-3:14b-cloud — PAC1 Benchmark Results

> Дата: 2026-03-29
> Модель: `ministral-3:14b-cloud` (Ollama local backend)
> Бенчмарк: `bitgn/pac1-dev` (22 задачи)
> Результат: **100.00%** (22/22) — после FIX-111

---

## Конфигурация

```
backend:      ollama (anthropic=✗, openrouter=✗, ollama=✓)
classifier    = ministral-3:14b-cloud
default       = ministral-3:14b-cloud
think         = ministral-3:14b-cloud
longContext   = ministral-3:14b-cloud
TASK_TIMEOUT_S = 900
```

Агент: `pac1-py/agent/` (FIX-108 + FIX-109 + FIX-111 применены)

---

## Итоговая статистика

```
ИТОГО      100.00%  1550.2s     489,258     53,588     53 tok/s
СРЕДНЕЕ               70.5s      22,239      2,435
```

---

## Результаты по задачам

| Задача | Оценка | Время   | Шаги | Вход(tok) | Выход(tok) | ток/с | Тип         |
|--------|--------|---------|------|-----------|------------|-------|-------------|
| t01    | 1.00   |  97.4s  |  13  |  52,350   |  4,679     |  65   | longContext |
| t02    | 1.00   |  33.4s  |   3  |  10,853   |  1,564     |  84   | default     |
| t03    | 1.00   | 130.5s  |   9  |  40,887   |  6,617     |  65   | think       |
| t04    | 1.00   |  25.1s  |   2  |   7,028   |    534     |  73   | default     |
| t05    | 1.00   |  16.7s  |   1  |   3,491   |    195     |  78   | default     |
| t06    | 1.00   |  27.4s  |   1  |   3,498   |    447     |  53   | default     |
| t07    | 1.00   |  38.2s  |   3  |  11,105   |  1,110     |  57   | default     |
| t08    | 1.00   |  33.1s  |   1  |   3,480   |    198     |  80   | default     |
| t09    | 1.00   |  31.6s  |   1  |   3,540   |    347     |  47   | default     |
| t10    | 1.00   |  40.2s  |   5  |  17,425   |  1,253     |  63   | default     |
| t11    | 1.00   |  82.4s  |   4  |  13,118   |  3,543     |  60   | default     |
| t12    | 1.00   |  22.2s  |   2  |   7,489   |    305     |  64   | default     |
| t13    | 1.00   |  54.2s  |   7  |  30,115   |  2,113     |  69   | default     |
| t14    | 1.00   |  97.2s  |  13  |  59,614   |  4,950     |  68   | default     |
| t15    | 1.00   |  22.8s  |   1  |   3,674   |    225     |  66   | default     |
| t16    | 1.00   | 451.0s  |  21  |  96,507   |  8,880     |  22   | think       |
| t17    | 1.00   | 120.0s  |   8  |  32,359   |  7,997     |  94   | default     |
| t18    | 1.00   |  33.1s  |   4  |  15,472   |  1,485     |  99   | default     |
| t19    | 1.00   |  50.4s  |   8  |  33,213   |  2,308     |  98   | default     |
| t20    | 1.00   |  39.6s  |   5  |  19,789   |  1,568     |  77   | default     |
| t21    | 1.00   |  28.7s  |   3  |   8,714   |    511     |  82   | default     |
| t22    | 1.00   |  48.7s  |   4  |  15,537   |  2,759     |  95   | default     |

---

## История прогонов

| Прогон | Дата       | Результат  | Фиксы       | Примечание |
|--------|------------|------------|-------------|------------|
| v1     | 2026-03-29 | **95.45%** | до FIX-111  | t03 провал: модель "забыла" completed steps после compaction |
| v2     | 2026-03-29 | **100.00%**| +FIX-111    | t03 исправлен: done_operations + server ledger |

---

## Наблюдения

### FIX-111 — root cause t03

**Провал v1:** t03 (capture + distill + delete inbox) — 11 шагов, финал `OUTCOME_NONE_CLARIFICATION`.

Последовательность сбоя:
- step 3: `WRITTEN: /01_capture/influential/...` ✅
- step 5: `WRITTEN: /02_distill/cards/...` ✅
- step 8: `WRITTEN: /02_distill/threads/...` ✅
- step 9: `DELETED: /00_inbox/...` ✅ ← log compaction убрала steps 3,5,8 из контекста
- step 10: модель попыталась перечитать уже удалённый inbox файл → NOT_FOUND → паника → `OUTCOME_NONE_CLARIFICATION`

**Исправление v2:** FIX-111 добавил `done_operations` поле в схему и server-side ledger в `preserve_prefix`. В step 8 модель явно несёт `"done_operations":["WRITTEN:/01_capture/...", "WRITTEN:/02_distill/cards/...", "WRITTEN:/02_distill/threads/..."]`, на step 9 уверенно делает delete и сразу `OUTCOME_OK` (9 шагов вместо 11).

### t16 — тяжёлая think-задача

451s при 22 tok/s — модель использует глубокий reasoning (21 шаг, 96k входных токенов). Задача всё же пройдена. Это аналогично поведению minimax-m2.7:cloud (645s на t16).

### Classifier failures

Несколько задач: `[FIX-80][Ollama] Empty after all retries — returning None` при классификации → падение на regex-fallback (FIX-108: 1 попытка вместо 3). Задачи при этом выполнены корректно — fallback работает надёжно.

### Сравнение с параллельным прогоном (2026-03-28)

| Прогон         | Результат   | t03    | Время  |
|----------------|-------------|--------|--------|
| Параллельный   | **90.91%**  | ❌     | ~n/a   |
| Одиночный v1   | **95.45%**  | ❌     | 2335s  |
| Одиночный v2   | **100.00%** | ✅     | 1550s  |

Параллельный прогон показал 90.91% из-за TIMEOUT на t01/t03 при разделении GPU. Одиночный v1 — t03 провал из-за context loss. Одиночный v2 — 100% с FIX-111.
