# Garmin-TN-MCP (`garmin-raw`)

Минимальный **сырьевой** доступ к Garmin Connect для тренировочного анализа.
Один бэкенд (`garminconnect` 0.3.x) обслуживает два фронтенда:

- **MCP-сервер** (`garmin-raw-mcp`) — живой доступ к данным прямо в чате Claude Desktop.
- **One-shot экспорт** (`garmin-raw-export`) — выгрузка периода в JSON для тиража
  (атлет/коуч заливает файл в чат).

Принцип: только сырьё (пульс/каденс/мощность/шаг/высота по кругам, посекундные
потоки, комментарий-лактат). **Никаких** VO2max / training-effect / device-оценок
порога — они по методологии отвергаются.

## Установка

Требуется Python 3.10+ и [`uv`](https://astral.sh/uv).

```bash
git clone https://github.com/asilenin/Garmin-TN-MCP.git
cd Garmin-TN-MCP
uv sync
```

### 1. Авторизация (один раз)

```bash
uv run garmin-raw-auth
```

Введёшь email, пароль и MFA-код. Токены лягут в `~/.garminconnect` (формат 0.3.x).
Дальше логин не нужен — сервер и экспорт работают по токенам (и не ловят 429 от
повторных входов).

### 2. Подключить MCP к Claude Desktop — одной командой

```bash
uv run garmin-raw-install
```

Команда сама находит `uv` и путь к этой папке и **аккуратно дописывает** сервер
`garmin-raw` в `claude_desktop_config.json` — не затирая остальное (preferences и пр.),
с бэкапом. Кроссплатформенно (macOS/Windows/Linux). Путь можно задать явно:

```bash
uv run garmin-raw-install /полный/путь/к/Garmin-TN-MCP
```

Затем **полный перезапуск Claude Desktop** (Cmd+Q на macOS) — появятся 6 тулзов.

<details>
<summary>Ручная альтернатива (если не хочешь скрипт)</summary>

Добавь в `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`), подставив пути:

```json
{
  "mcpServers": {
    "garmin-raw": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["--directory", "/полный/путь/к/Garmin-TN-MCP", "run", "garmin-raw-mcp"]
    }
  }
}
```
</details>

## Удаление

```bash
uv run garmin-raw-uninstall      # убирает garmin-raw из конфига (с бэкапом), остальное не трогает
```

Перезапусти Claude Desktop. Токены при этом не удаляются — снести их вручную при
необходимости:

```bash
rm -rf ~/.garminconnect           # сохранённые токены Garmin
```

Полностью убрать установку:

```bash
cd .. && rm -rf Garmin-TN-MCP     # сам репозиторий
```

## Экспорт для тиража

```bash
# весь период
uv run garmin-raw-export --start 2026-06-01 --end 2026-06-21

# одна активность + посекундные потоки
uv run garmin-raw-export --start 2026-06-20 --end 2026-06-21 \
    --activity 23321211303 --streams
```

Получишь `garmin_export.json` — залей в чат.

## Тулзы (одинаковы в MCP и экспорте)

| Тул | Отдаёт |
|---|---|
| `list_activities(start, end, sport)` | сырые сводки за период (1 запрос) |
| `get_activity_laps(id)` | пульс/каденс/мощность/шаг/высота **по кругу** (lapDTOs) |
| `get_activity_streams(id)` | посекундные потоки (HR, каденс, высота, уклон, мощность, шаг, дыхание) |
| `get_activity_comment(id)` | комментарий активности + распарсенный лактат (`LA:x.x`) |
| `get_wellness(date)` | сон, HRV, RHR, стресс, Body Battery |
| `get_personal_records()` | PR по дистанциям |

## Лактат

Вносится в **комментарий активности** в Garmin Connect строкой вида `LA:6.1`
(можно с контекстом: `LA:6.6 @rep12`). `get_activity_comment` достаёт поле
`description` и парсит все значения в `lactate_mmol`. Комментарий тянется лениво —
только для активностей, реально идущих в анализ, чтобы не удваивать число запросов.

## Замечания

- **PR Garmin — это авто-детект самого быстрого сплита**, а не протокольные времена;
  они могут быть быстрее официальных. Для маркеров формы держи протокольные времена,
  а garmin-PR используй как ориентир.
- **Wellness/PR** зовутся через перебор кандидатов-методов: если в твоей версии
  `garminconnect` метод назван иначе, тул вернёт `_error`, не роняя весь ответ.
- **PII** (имя/ID владельца) вырезается из выгрузок регистронезависимо — гигиена для тиража.
- Если MCP молча перестал отвечать — почти всегда это протухшие токены: прогони
  `garmin-raw-auth` заново. One-shot экспорт — устойчивый фолбэк на этот случай.
