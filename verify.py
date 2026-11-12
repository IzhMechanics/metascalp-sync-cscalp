#!/usr/bin/env python3
"""
verify.py — независимая (stdlib-only) проверка результата metascalp_sync.py.

Проверяет, что в OrderBookSettings:
  1) у каждой строки, соответствующей тикеру из папки MVS, значения 20 ключей
     из baza.txt равны значениям из соответствующего XML;
  2) число ключей в SettingsSet == EXPECT_KEYS (по умолчанию 119).

Запуск:
    python verify.py --db test_apply.db --mvs MVS --map baza.txt
    python verify.py --db test_apply.db --sample 5   # показать детали по 5 вставленным тикерам
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

EXPECT_KEYS = 119

FILE_RE = re.compile(r"^XDSD\.([A-Z]+)\.(.+?)_Settings\.(xml|tmp)$", re.IGNORECASE)
RULE_RE = re.compile(
    r"""^\s*["']?(?P<key>[A-Za-z0-9_]+)["']?\s*:\s*(?P<ref>[^=]*?)\s*=\s*
        <\s*(?P<elem>[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)\b(?P<attrs>[^>]*)""",
    re.IGNORECASE | re.VERBOSE,
)
ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
MARKET_BY_SEGMENT = {"TQBR": 58, "FUT": 59, "CETS": 60}


# ------------------------------------------------------------------ rules ----
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


def parse_rules(path: Path):
    rules, seen = [], {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip().lstrip("{[").rstrip("}]")
        if not s or s.startswith("#"):
            continue
        m = RULE_RE.match(s)
        if not m:
            continue
        elem = m.group("elem")
        section = None
        if "/" in elem:
            section, elem = elem.split("/", 1)
        attr = "Value"
        for a, _v in ATTR_RE.findall(m.group("attrs")):
            if a.lower() == "value":
                attr = a
                break
        occ = seen.get(elem, 0)
        seen[elem] = occ + 1
        rules.append((m.group("key"), elem, section, occ, attr, parse_ref(m.group("ref"))))
    return rules


# --------------------------------------------------------------- xml values ----
def xml_values(path: Path, rules) -> dict:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    nodes, stack = [], [(root, "")]
    while stack:
        el, parent = stack.pop(0)
        nodes.append((el.tag, parent, dict(el.attrib)))
        stack.extend((ch, el.tag) for ch in el)
    out = {}
    for key, name, section, occ, attr, ref in rules:
        cands = [n for n in nodes if n[0] == name and (section is None or n[1] == section)]
        if not cands:
            continue
        raw = cands[min(occ, len(cands) - 1)][2].get(attr)
        if raw is None:
            continue
        out[key] = convert(raw, ref)
    return out


def collect_files(folder: Path):
    best = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        m = FILE_RE.match(p.name)
        if not m:
            continue
        market, ticker = m.group(1).upper(), m.group(2).strip()
        ticker = re.sub(r"\s*\(\d+\)$", "", ticker)
        key = (market, ticker)
        if key not in best or (best[key].suffix.lower() == ".tmp" and p.suffix.lower() == ".xml"):
            best[key] = p
    return [(p, t, m) for (m, t), p in sorted(best.items())]


# ----------------------------------------------------------------- check -----
def eq(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def verify(db: Path, mvs: Path, mapping: Path, sample: int, verbose: bool, orig: Path | None):
    rules = parse_rules(mapping)
    keys = [r[0] for r in rules]
    files = collect_files(mvs)

    conn = sqlite3.connect(db)
    cur = conn.cursor()
    orig_keys: set[tuple[int, str]] | None = None
    if orig and orig.exists():
        oc = sqlite3.connect(orig)
        orig_keys = {(mid, (t or "").strip().lower())
                    for mid, t in oc.execute("SELECT MarketId, Ticker FROM OrderBookSettings")}
        before = oc.execute("SELECT COUNT(*) FROM OrderBookSettings").fetchone()[0]
        oc.close()
        print(f"Строк до (в {orig.name}): {before}")
    rows = cur.execute("SELECT Id, MarketId, Ticker, SettingsSet FROM OrderBookSettings").fetchall()
    total = len(rows)

    by_ticker = {}
    for rid, mid, ticker, raw in rows:
        by_ticker.setdefault((mid, (ticker or "").strip().lower()), []).append((rid, raw))

    mismatches, key_count_bad, missing_rows, unparsed = [], [], [], 0
    checked_rows = 0
    inserted_examples = []

    for path, ticker, segment in files:
        mid = MARKET_BY_SEGMENT.get(segment)
        if mid is None:
            continue
        expected = xml_values(path, rules)
        found = by_ticker.get((mid, ticker.lower()))
        if not found:
            missing_rows.append(f"{ticker} (MarketId={mid}) — строки в базе не найдены")
            continue
        for rid, raw in found:
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                unparsed += 1
                mismatches.append(f"id={rid} {ticker}: SettingsSet не JSON")
                continue
            checked_rows += 1
            if len(data) != EXPECT_KEYS:
                key_count_bad.append((mid, rid, ticker, len(data),
                                     orig_keys is not None and (mid, ticker.lower()) not in orig_keys))
            for k in keys:
                if k not in data:
                    mismatches.append(f"id={rid} {ticker}: ключ {k} отсутствует")
                elif k in expected and not eq(data[k], expected[k]):
                    mismatches.append(f"id={rid} {ticker}: {k} = {data[k]!r}, XML = {expected[k]!r}")
            if orig_keys is not None and (mid, ticker.lower()) not in orig_keys:
                vals_ok = all(k in data and eq(data[k], expected[k]) for k in keys)
                inserted_examples.append((rid, ticker, mid, len(data), vals_ok))

    conn.close()

    print(f"База: {db}")
    print(f"Строк в OrderBookSettings: {total}")
    print(f"Правил в {mapping.name}: {len(keys)} ({', '.join(keys)})")
    print(f"Файлов XML/TMP (уникальных тикеров): {len(files)}; строк проверено: {checked_rows}")
    # отдельная проверка числа ключей у всех строк рынков 58/59/60
    scope_seen = {rid for path, ticker, segment in files
                  for mid in [MARKET_BY_SEGMENT.get(segment)] if mid
                  for rid, _raw in by_ticker.get((mid, ticker.lower()), [])}
    for rid, mid, ticker, raw in rows:
        if mid in MARKET_BY_SEGMENT.values() and rid not in scope_seen:
            try:
                n = len(json.loads(raw) if raw else {})
            except Exception:
                unparsed += 1
                continue
            if n != EXPECT_KEYS:
                key_count_bad.append((mid, rid, ticker, n,
                                     orig_keys is not None and (mid, (ticker or "").strip().lower()) not in orig_keys))

    in_scope = [x for x in key_count_bad if x[0] in MARKET_BY_SEGMENT.values()]
    out_scope = [x for x in key_count_bad if x[0] not in MARKET_BY_SEGMENT.values()]
    new_bad = [x for x in in_scope if x[4]]
    old_bad = [x for x in in_scope if not x[4]]
    print(f"Строк с числом ключей != {EXPECT_KEYS}: всего {len(key_count_bad)} "
          f"(вставленных: {len(new_bad)}, ранее существовавших: {len(old_bad)}, "
          f"прочие рынки: {len(out_scope)})")
    for mid, rid, ticker, n, _new in (new_bad + old_bad + out_scope)[:40]:
        print(f"  id={rid} {ticker} MarketId={mid}: {n} ключей")
    print(f"Тикеров без строки в базе: {len(missing_rows)}")
    if missing_rows:
        print("  " + "; ".join(missing_rows[:20]))
    print(f"Расхождений значений: {len(mismatches)}")
    for m in mismatches[:40]:
        print("  " + m)

    if verbose and inserted_examples:
        print(f"Примеры вставленных тикеров (первые {sample}):")
        for rid, ticker, mid, n, ok in inserted_examples[:sample]:
            print(f"  id={rid} {ticker} MarketId={mid} keys={n} values_ok={ok}")

    if not mismatches and not new_bad and not missing_rows and not unparsed:
        if old_bad or out_scope:
            print(f"OK (примечание: {len(old_bad)} ранее существовавших строк рынков 58/59/60 "
                  f"и {len(out_scope)} строк других рынков имеют другое число ключей)")
        else:
            print("OK")
        return 0
    print(f"НЕ OK: {len(mismatches) + len(key_count_bad) + len(missing_rows) + unparsed} проблем")
    return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Verify metascalp_sync result")
    ap.add_argument("--db", default=str(here / "test_apply.db"))
    ap.add_argument("--mvs", default=str(here / "MVS"))
    ap.add_argument("--map", default=str(here / "baza.txt"))
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--orig", default=str(here / "MetaScalp.db"), help="исходная база (для счёта 'до')")
    args = ap.parse_args()
    return verify(Path(args.db), Path(args.mvs), Path(args.map), args.sample, args.verbose, Path(args.orig))


if __name__ == "__main__":
    sys.exit(main())
