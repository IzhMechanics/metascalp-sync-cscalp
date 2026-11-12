# MetaScalp settings sync

Переносит значения из файлов `XDSD.<MARKET>.<TICKER>_Settings.xml` (папка `MVS`)
в таблицу `OrderBookSettings` базы SQLite (`MetaScalp.db`).
Зависимости: только стандартная библиотека Python (tkinter, sqlite3, xml.etree).

## Файлы пути
Скопировать в отдельную папку
C:\Users\пользователь\AppData\Roaming\MetaScalp файл MetaScalp.db
C:\Program Files (x86)\FSR Launcher\SubApps\CS\Data\ папка MVS

## Соответствия (baza.txt)
Формат, одна строка на ключ:

```
"TradeAmount1":1.0, = <First_WorkAmount Value="11" />
"AmountBarFilledAt":15000.0, = <DOM/FilledAt Value="20000" />   # секция необязательна
```

- слева — ключ JSON в `SettingsSet` (его тип подсказывает тип значения);
- справа — элемент XML, откуда берётся значение;
- имена элементов могут повторяться (`FilledAt` в `DOM` и в `CLUSTER_PANEL`) —
  тогда берётся то же по порядку вхождение, что и в `baza.txt`,
  либо явно `<СЕКЦИЯ/Элемент>`;
- строки `#` — комментарии.

## Рынки
`TQBR -> MarketId 58`, `FUT -> MarketId 59`, `CETS -> MarketId 60`.
Другие сегменты (например `MTQR`) пропускаются с пометкой в логе.
Файлы `.tmp` используются, если нет `.xml` для того же тикера.

## Логика
- **тикер есть в базе** (`Ticker` + `MarketId`) — обновляются только ключи,
  которые уже есть в `SettingsSet` этой строки (структура не меняется);
- **тикера нет** — новая строка создаётся на основе шаблона `SettingsSet`
  самого частого набора ключей этого `MarketId` (119 ключей), затем
  применяются значения из XML; `ApiKey`/`ExchangeId` берутся из строки того же рынка;
- перед применением делается резервная копия базы.

## Запуск
```bash
uv run metascalp_sync.py                                   # GUI
uv run metascalp_sync.py --no-gui --dry-run                # проверка без записи
uv run metascalp_sync.py --no-gui --db MetaScalp.db --mvs MVS --map baza.txt
uv run metascalp_sync.py --no-gui --ticker SBER            # только один тикер
uv run metascalp_sync.py --no-gui --no-backup --no-template
uv run metascalp_sync.py --no-gui --dry-run --log run.log
```

GUI: пути к базе / папке / файлу соответствий, фильтр тикера,
чекбоксы (dry-run, резервная копия, добавлять отсутствующие, использовать шаблон ключей),
кнопки «Проверить», «Применить», окно лога.

## Проверка
`verify.py --db MetaScalp.db --orig MetaScalp.db` — сверяет значения 20 ключей
из `baza.txt` с XML для всех строк рынков 58/59/60 и число ключей (119).
