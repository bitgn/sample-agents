# Test Agent Benchmark Runner

## 1. Запуск бенчмарка

Запусти команду:

```
cd sandbox/py && uv run python main.py
```

Дождись завершения всех задач. Сохрани полный stdout — он нужен для анализа.

## 2. Анализ результатов

Для каждой задачи (t01–t07) определи из stdout:

- **Score**: 0.00 или 1.00
- **Steps**: сколько шагов потребовалось
- **Outcome**: краткое описание (1 строка) — что агент сделал и почему получил такой скор

### Failure Analysis

Для задач со score 0.00 определи root cause из категорий:
- `shallow-exploration` — не обошёл поддиректории, остановился на верхнем уровне
- `pattern-mismatch` — неправильный формат/именование файла (расширение, префикс, нумерация)
- `skipped-agents-md` — не прочитал AGENTS.MD, ответил из общих знаний
- `wrong-path` — нашёл инструкции, но записал файл не в ту директорию
- `premature-finish` — завершился раньше, чем исследовал достаточно
- `other` — с пояснением

### Strengths / Weaknesses

Выдели 3–5 сильных и 3–5 слабых сторон агента на основе всех задач.

## 3. Определи модель

Прочитай `MODEL_ID` из `sandbox/py/main.py`. Используй его для имени файла, заменив `/` на `-` и убрав спецсимволы.

## 4. Сохрани отчёт

Сохрани результаты в `docs/<model_name>.md` по шаблону ниже. Если файл уже существует — перезапиши его.

```markdown
# <MODEL_ID> - Benchmark Results

## Run Info

| Parameter        | Value                          |
|------------------|--------------------------------|
| Model            | <MODEL_ID>                     |
| Agent            | agent.py (SGR Micro-Steps)     |
| Provider         | OpenRouter / Ollama            |
| Benchmark        | bitgn/sandbox                  |
| Tasks            | <количество задач>             |
| Date             | <YYYY-MM-DD>                   |
| Final Score      | **<score>%**                   |

## Task Results

| Task | Description | Score | Steps | Root Cause | Outcome |
|------|-------------|-------|-------|------------|---------|
| t01  | ...         | 0.00  | N     | category   | ...     |
| ...  | ...         | ...   | ...   | —          | ...     |

## Failure Analysis

### Root Causes

1. ...

### Strengths

- ...

### Weaknesses

- ...

### Pattern Summary

- N/7 tasks: model read AGENTS.MD
- N/7 tasks: loops or parse failures
- N/7 tasks: scored 1.00
- Key gap: ...

## Comparison Table

> Собери данные из ВСЕХ существующих файлов в docs/*.md и объедини в одну таблицу.

| Model | Agent | Date | t01 | t02 | t03 | t04 | t05 | t06 | t07 | Final |
|-------|-------|------|-----|-----|-----|-----|-----|-----|-----|-------|
| ...   | ...   | ...  | ... | ... | ... | ... | ... | ... | ... | ...   |
```

## 5. Финальная проверка

- Убедись, что Comparison Table содержит строки из ВСЕХ предыдущих прогонов (прочитай `docs/*.md`)
- Убедись, что Final Score совпадает с выводом `FINAL: XX.XX%` из stdout
- Убедись, что количество задач в таблице совпадает с количеством задач в stdout
