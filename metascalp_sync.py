#!/usr/bin/env python3
"""
MetaScalp settings sync: из XML-файлов (папка MVS) значения переносятся
в таблицу OrderBookSettings базы MetaScalp.db.

Формат карты (baza.txt):
    "TradeAmount1":1.0, = <First_WorkAmount Value="11" />
    # комментарий
    "AmountBarFilledAt":15000.0, = <DOM/FilledAt Value="20000" />

Левая часть — ключ в JSON SettingsSet (тип значения подсказывает тип).
Правая часть — элемент XML, откуда берётся значение.
Можно указать секцию: <DOM/FilledAt .../> (если имя элемента повторяется).

Запуск:
    uv run metascalp_sync.py                      # GUI
    uv run metascalp_sync.py --db MetaScalp.db --mvs MVS --map baza.txt --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

MARKET_BY_SEGMENT = {"TQBR": 58, "FUT": 59, "CETS": 60}
MARKETS = (58, 59, 60)
FILE_RE = re.compile(r"^XDSD\.([A-Z]+)\.(.+?)_Settings\.(xml|tmp)$", re.IGNORECASE)
RULE_RE = re.compile(
    r"""^\s*["']?(?P<key>[A-Za-z0-9_]+)["']?\s*:\s*(?P<ref>[^=]*?)\s*=\s*
        <\s*(?P<elem>[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)\b(?P<attrs>[^>]*)""",
    re.IGNORECASE | re.VERBOSE,
)
ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


# ---------------------------------------------------------------- logging ----
class GuiLogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink  # callable(str)

    def emit(self, record):
        try:
            self.sink(self.format(record))
        except Exception:
            pass


def setup_logging(log_file: Path | None) -> logging.Logger:
    logger = logging.getLogger("metascalp_sync")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ------------------------------------------------------------- mapping rules ----
@dataclass
class Rule:
    key: str
    element: str
    section: str | None
    occurrence: int          # порядковый вхождение имени элемента в файле (0-based)
    ref_value: object
    attr: str = "Value"

    @property
    def name(self) -> str:
        return self.element


def parse_ref(text: str):
    text = text.strip().rstrip(",")
    if not text:
        return None
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
        return text


def parse_rules(path: Path) -> list[Rule]:
    rules: list[Rule] = []
    seen_names: dict[str, int] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip().lstrip("{[").rstrip("}]")
        if not s or s.startswith("#"):
            continue
        m = RULE_RE.match(s)
        if not m:
            logging.getLogger("metascalp_sync").warning(
                "baza.txt:%d — не распознана строка: %s", lineno, s
            )
            continue
        key = m.group("key")
        ref = parse_ref(m.group("ref"))
        elem = m.group("elem")
        section = None
        if "/" in elem:
            section, elem = elem.split("/", 1)
        attrs = ATTR_RE.findall(m.group("attrs"))
        attr = "Value"
        for a, _v in attrs:
            if a.lower() == "value":
                attr = a
                break
        occ = seen_names.get(elem, 0)
        seen_names[elem] = occ + 1
        rules.append(Rule(key=key, element=elem, section=section, occurrence=occ, ref_value=ref, attr=attr))
    return rules


# --------------------------------------------------------------- xml values ----
def xml_values(path: Path, rules: list[Rule]) -> dict[str, object]:
    """Возвращает {json_key: значение} из XML-файла настроек."""
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        logging.getLogger("metascalp_sync").warning("Не удалось прочитать %s: %s", path, exc)
        return {}

    # список (имя, секция=имя родителя, атрибуты) всех элементов
    nodes: list[tuple[str, str, dict[str, str]]] = []
    stack = [(root, "")]
    while stack:
        el, parent = stack.pop(0)
        nodes.append((el.tag, parent, dict(el.attrib)))
        for ch in list(el):
            stack.append((ch, el.tag))

    out: dict[str, object] = {}
    for r in rules:
        if r.section:
            candidates = [n for n in nodes if n[0] == r.name and n[1] == r.section]
        else:
            candidates = [n for n in nodes if n[0] == r.name]
        if not candidates:
            continue
        idx = min(r.occurrence, len(candidates) - 1)
        raw = candidates[idx][2].get(r.attr)
        if raw is None:
            continue
        out[r.key] = convert(raw, r.ref_value)
    return out


def convert(raw: str, ref):
    raw = raw.strip()
    if isinstance(ref, bool) or raw.lower() in ("true", "false"):
        return raw.lower() in ("true", "1", "yes", "y")
    if re.fullmatch(r"-?\d+", raw):
        val = int(raw)
        return float(val) if isinstance(ref, float) else val
    try:
        val = float(raw)
        return int(val) if val.is_integer() and isinstance(ref, int) else val
    except ValueError:
        return raw


# ------------------------------------------------------------ file parsing ----
def ticker_from_filename(name: str) -> tuple[str, str] | None:
    m = FILE_RE.match(name)
    if not m:
        return None
    market = m.group(1).upper()
    ticker = m.group(2).strip()
    ticker = re.sub(r"\s*\(\d+\)$", "", ticker)  # " (2)" от копий
    return market, ticker


def collect_files(folder: Path) -> list[tuple[Path, str, str]]:
    """[(path, ticker, market_segment)], предпочитая .xml вместо .tmp для одного тикера."""
    best: dict[tuple[str, str], Path] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        parsed = ticker_from_filename(p.name)
        if not parsed:
            logging.getLogger("metascalp_sync").info("Пропущен файл (не формат): %s", p.name)
            continue
        market, ticker = parsed
        key = (market, ticker)
        ext = p.suffix.lower()
        if key not in best or (best[key].suffix.lower() == ".tmp" and ext == ".xml"):
            best[key] = p
    return [(p, t, m) for (m, t), p in sorted(best.items())]


# --------------------------------------------------------------- database ------
def market_id(segment: str) -> int | None:
    return MARKET_BY_SEGMENT.get(segment.upper())


def parse_settings(raw) -> dict | None:
    """SettingsSet -> dict, либо None если это не JSON-объект."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def build_templates(cur: sqlite3.Cursor, markets: tuple[int, ...] = MARKETS):
    """Шаблоны SettingsSet по MarketId.

    Для каждого рынка берётся набор ключей самой частой строки и её значения.
    Если для рынка строк нет — строка Ticker='Default' (Id=1), иначе —
    самый распространённый набор ключей по всей базе.
    Возвращает (templates, union_keys, info_lines).
    """
    logger = logging.getLogger("metascalp_sync")
    rows: list[tuple[int, int, str, dict]] = []
    unparsed = 0
    for rid, mid, ticker, raw in cur.execute(
        "SELECT Id, MarketId, Ticker, SettingsSet FROM OrderBookSettings"
    ).fetchall():
        obj = parse_settings(raw)
        if obj is None:
            unparsed += 1
            continue
        rows.append((rid, mid, ticker, obj))
    if unparsed:
        logger.warning("Строк с непарсимым SettingsSet: %d", unparsed)

    union_keys: set[str] = set()
    for _rid, _mid, _ticker, obj in rows:
        union_keys.update(obj.keys())

    default_row = next((r for r in rows if (r[2] or "").strip().lower() == "default"), None)
    if default_row is None:
        default_row = next((r for r in rows if r[0] == 1), None)

    global_counter = Counter(frozenset(r[3].keys()) for r in rows)
    global_keys, global_count = global_counter.most_common(1)[0] if global_counter else (frozenset(), 0)
    global_row = next((r for r in rows if frozenset(r[3].keys()) == global_keys), None)

    templates: dict[int, dict] = {}
    info: list[str] = []
    wanted = sorted(set(markets) | {m for m in MARKET_BY_SEGMENT.values()})
    for mid in wanted:
        sub = [r for r in rows if r[1] == mid]
        counter = Counter(frozenset(r[3].keys()) for r in sub)
        if counter:
            keys, count = counter.most_common(1)[0]
            rep = next(r for r in sub if frozenset(r[3].keys()) == keys)
            source = f"самая частая строка рынка (id={rep[0]}, ticker={rep[2]}, {count} строк с этим набором)"
        elif default_row is not None:
            rep = default_row
            source = f"строка Default (id={default_row[0]})"
        elif global_row is not None:
            rep = global_row
            source = f"самый распространённый набор ключей по базе (id={global_row[0]}, {global_count} строк)"
        else:
            rep = None
            source = "нет данных"
        templates[mid] = dict(rep[3]) if rep else {}
        info.append(f"MarketId={mid}: шаблон {len(templates[mid])} ключей — {source}")
    for line in info:
        logger.info("Шаблон: %s", line)
    logger.info("Объединённый набор ключей базы: %d", len(union_keys))
    return templates, union_keys, info


def find_row(cur: sqlite3.Connection, ticker: str, market_id: int):
    row = cur.execute(
        "SELECT Id, SettingsSet FROM OrderBookSettings WHERE Ticker = ? AND MarketId = ?",
        (ticker, market_id),
    ).fetchone()
    if row:
        return [row]
    return cur.execute(
        "SELECT Id, SettingsSet FROM OrderBookSettings WHERE Ticker = ? COLLATE NOCASE AND MarketId = ?",
        (ticker, market_id),
    ).fetchall()


def backup_db(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db.with_name(f"{db.stem}_backup_{stamp}.db")
    shutil.copy2(db, dst)
    return dst


def apply(db_path: Path, folder: Path, rules: list[Rule], dry_run: bool,
          ticker_filter: str | None, use_template: bool = True,
          allow_insert: bool = True) -> dict[str, int]:
    logger = logging.getLogger("metascalp_sync")
    files = collect_files(folder)
    if ticker_filter:
        files = [f for f in files if ticker_filter.lower() in f[1].lower()]
    logger.info("Найдено файлов: %d", len(files))

    stats = {"updated": 0, "inserted": 0, "skipped": 0, "no_change": 0, "unknown_market": 0,
             "values": 0, "skipped_keys": 0, "added_keys": 0, "template_rows": 0}
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        api_row = cur.execute(
            "SELECT ApiKey, ExchangeId FROM OrderBookSettings WHERE ExchangeId IS NOT NULL LIMIT 1"
        ).fetchone()
        default_api, default_exchange = api_row if api_row else (None, None)
        key_templates, union_keys, _info = build_templates(cur)
        skipped_examples: list[str] = []

        for path, ticker, segment in files:
            mid = market_id(segment)
            if mid is None:
                logger.info("Неизвестный рынок %s для %s — пропущен", segment, path.name)
                stats["unknown_market"] += 1
                continue

            values = xml_values(path, rules)
            if not values:
                logger.warning("%s: ни одного значения не извлечено", path.name)
                stats["skipped"] += 1
                continue
            stats["values"] += len(values)

            rows = find_row(cur, ticker, mid)
            if rows:
                for rid, settings in rows:
                    try:
                        data = json.loads(settings) if settings else {}
                    except Exception:
                        logger.warning("Row %d: SettingsSet не является JSON — перезаписываем", rid)
                        data = {}
                    if use_template:
                        missing = [k for k in values if k not in data]
                        if missing:
                            stats["skipped_keys"] += len(missing)
                            stats["template_rows"] += 1
                            if len(skipped_examples) < 3:
                                skipped_examples.append(
                                    f"{ticker} (id={rid}): {', '.join(missing)}"
                                )
                            logger.info("%s id=%s — пропущено: ключ отсутствует в строке: %s",
                                        ticker, rid, ", ".join(missing))
                        changed = {k: v for k, v in values.items() if k in data and data[k] != v}
                    else:
                        changed = {k: v for k, v in values.items() if data.get(k) != v}
                    if not changed:
                        stats["no_change"] += 1
                        logger.info("%s (%s): без изменений (id=%s)", ticker, segment, rid)
                        continue
                    data.update(changed)
                    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                    if dry_run:
                        logger.info("[dry-run] %s id=%s: %s", ticker, rid,
                                    ", ".join(f"{k}={v!r}" for k, v in changed.items()))
                    else:
                        cur.execute("UPDATE OrderBookSettings SET SettingsSet = ? WHERE Id = ?", (payload, rid))
                        logger.info("UPDATE %s id=%s: %s", ticker, rid,
                                    ", ".join(f"{k}={v!r}" for k, v in changed.items()))
                    stats["updated"] += 1
            elif not allow_insert:
                logger.info("%s (market=%s): тикера нет в базе — вставка запрещена", ticker, mid)
                stats["no_insert"] = stats.get("no_insert", 0) + 1
            else:
                api, exchange = default_api, default_exchange
                same_market = cur.execute(
                    "SELECT ApiKey, ExchangeId FROM OrderBookSettings WHERE MarketId = ? LIMIT 1", (mid,)
                ).fetchone()
                if same_market:
                    api, exchange = same_market
                data = dict(key_templates.get(mid, {})) if use_template else {}
                for k, v in values.items():
                    if k in data:
                        data[k] = v
                    elif k in union_keys:
                        stats["skipped_keys"] += 1
                        if len(skipped_examples) < 3:
                            skipped_examples.append(f"{ticker} (новый, market={mid}): {k}")
                        logger.info("%s — пропущен ключ %s: его нет в шаблоне MarketId=%s, "
                                    "но он есть в других наборах базы", ticker, k, mid)
                    else:
                        data[k] = v
                        stats["added_keys"] += 1
                payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                if dry_run:
                    logger.info("[dry-run] INSERT %s market=%s ExchangeId=%s "
                                "(шаблон %d + из XML %d = %d ключей)",
                                ticker, mid, exchange, len(key_templates.get(mid, {})),
                                len(values), len(data))
                else:
                    cur.execute(
                        "INSERT INTO OrderBookSettings (ApiKey, ExchangeId, IsDefault, MarketId, SettingsSet, Ticker) "
                        "VALUES (?, ?, 0, ?, ?, ?)",
                        (api, exchange, mid, payload, ticker),
                    )
                    logger.info("INSERT %s market=%s (id=%s, %d ключей)",
                                ticker, mid, cur.lastrowid, len(data))
                stats["inserted"] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    if skipped_examples:
        logger.info("Примеры пропущенных ключей: %s", "; ".join(skipped_examples))
    return stats


# ------------------------------------------------------------------- GUI ----
def run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import ttk

    logger = setup_logging(Path(args.log) if args.log else None)
    win = tk.Tk()
    win.title("MetaScalp settings sync")
    win.geometry("900x620")

    frm = ttk.Frame(win, padding=10)
    frm.pack(fill="both")

    def field(label, default, width=60):
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label).pack(side="left")
        var = tk.StringVar(value=default)
        ent = ttk.Entry(row, textvariable=var, width=width)
        ent.pack(side="left", fill="x", padx=6)
        return var

    db_var = field("База (MetaScalp.db):", args.db)
    mvs_var = field("Папка с тикерами (MVS):", args.mvs)
    map_var = field("Файл соответствий (baza.txt):", args.map)
    filt_var = field("Фильтр тикера (необязательно):", "")

    opts = ttk.Frame(frm)
    opts.pack(fill="x", pady=6)
    dry_var = tk.BooleanVar(value=args.dry_run)
    backup_var = tk.BooleanVar(value=True)
    insert_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts, text="Только проверка (dry-run)", variable=dry_var).pack(side="left")
    ttk.Checkbutton(opts, text="Резервная копия базы", variable=backup_var).pack(side="left")
    ttk.Checkbutton(opts, text="Добавлять отсутствующие тикеры", variable=insert_var).pack(side="left")
    template_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts, text="Использовать шаблон ключей", variable=template_var).pack(side="left")

    log = tk.Text(win, height=25, font=("Consolas", 9))
    log.pack(fill="both", padx=10, pady=10)

    def append(text: str):
        log.insert("end", text + "\n")
        log.see("end")

    logger.addHandler(GuiLogHandler(append))

    def run():
        db = Path(db_var.get())
        mvs = Path(mvs_var.get())
        mapping = Path(map_var.get())
        if not db.exists():
            append(f"ERROR: база не найдена: {db}")
            return
        if not mvs.is_dir():
            append(f"ERROR: папка не найдена: {mvs}")
            return
        if not mapping.exists():
            append(f"ERROR: файл соответствий не найден: {mapping}")
            return
        rules = parse_rules(mapping)
        append(f"Правил соответствий: {len(rules)}")
        if backup_var.get() and not dry_var.get():
            b = backup_db(db)
            append(f"Резервная копия: {b}")
        stats = apply(db, mvs, rules, dry_var.get(), filt_var.get().strip() or None,
                      use_template=template_var.get(), allow_insert=insert_var.get())
        append(f"Готово: обновлено {stats['updated']}, добавлено {stats['inserted']}, "
              f"без изменений {stats['no_change']}, пропущено {stats['skipped']}, "
              f"неизвестный рынок {stats['unknown_market']}, значений {stats['values']}, "
              f"пропущено ключей {stats['skipped_keys']}, добавлено новых ключей {stats['added_keys']}")

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=8)
    ttk.Button(btns, text="Проверить (dry-run)", command=lambda: (dry_var.set(True), run())).pack(side="left")
    ttk.Button(btns, text="Применить", command=lambda: (dry_var.set(False), run())).pack(side="left")
    ttk.Button(btns, text="Очистить лог", command=lambda: log.delete("1.0", "end")).pack(side="left")

    win.mainloop()
    return 0


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Sync MetaScalp OrderBookSettings from MVS XML files")
    ap.add_argument("--db", default=str(here / "MetaScalp.db"))
    ap.add_argument("--mvs", default=str(here / "MVS"))
    ap.add_argument("--map", default=str(here / "baza.txt"))
    ap.add_argument("--ticker", default=None, help="фильтр по тикеру")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-template", action="store_true",
                    help="не использовать шаблоны ключей по MarketId")
    ap.add_argument("--no-insert", action="store_true", help="не добавлять отсутствующие тикеры")
    ap.add_argument("--log", default=None, help="файл лога")
    ap.add_argument("--no-gui", action="store_true")
    args = ap.parse_args()

    if args.no_gui:
        logger = setup_logging(Path(args.log) if args.log else None)
        rules = parse_rules(Path(args.map))
        logger.info("Правил соответствий: %d", len(rules))
        if not args.dry_run and not args.no_backup:
            logger.info("Резервная копия: %s", backup_db(Path(args.db)))
        stats = apply(Path(args.db), Path(args.mvs), rules, args.dry_run, args.ticker,
                      use_template=not args.no_template, allow_insert=not args.no_insert)
        logger.info("Итог: %s", stats)
        return 0

    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
