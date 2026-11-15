#!/usr/bin/env python3
"""scrapekit engine — registry-driven extraction with selector fallbacks and export.

This is the client half. It speaks one JSON request per line to a browser
worker listening on a unix socket (SCRAPEKIT_SOCK). The worker is not part of
this repository.

The registry (sites.yaml) is the contract: per site a URL, a wait selector, a
minimum row count for the canary, and one or more extractors. The first
extractor that yields rows wins, which is what makes selector drift survivable.

    scrapekit.py --list
    scrapekit.py --site hackernews --out /tmp/hn.csv
    scrapekit.py --all --out /tmp/all.xlsx
    scrapekit.py --canary            # exit 1 if any site drops below min_rows
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import re
import socket
import sys
import time
from pathlib import Path

import yaml  # noqa: E402

try:  # optional: XLSX export
    from openpyxl import Workbook  # noqa: E402
except Exception:  # noqa: BLE001
    Workbook = None  # type: ignore[assignment]

SOCK = Path(os.environ.get("SCRAPEKIT_SOCK", "/tmp/scrapekit.sock"))
REGISTRY = Path(os.environ.get("SCRAPEKIT_SITES", "sites.example.yaml"))
PRICE = re.compile(r"([\d.,]+)")
DATE_HINTS = re.compile(r"ago|heute|gestern|tag|stunde|minute|hour|day|minute", re.I)


def cmd(message: dict, timeout: float = 120) -> dict:
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(timeout)
    try:
        conn.connect(str(SOCK))
    except OSError as exc:
        conn.close()
        return {"ok": False, "error": f"unreachable: {SOCK} ({exc.strerror or exc})"}
    try:
        conn.sendall((json.dumps(message) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        conn.close()
    return json.loads(data.decode())


def load_registry(path: Path = REGISTRY) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    return data.get("sites") or []


def scroll(pid: int, rounds: int) -> None:
    for _ in range(max(0, rounds)):
        cmd({"cmd": "eval", "id": pid,
             "expr": "() => { window.scrollBy(0, Math.round(innerHeight * 0.9)); return true; }"})
        time.sleep(1.2)


def scroll_until(pid: int, count_expr: str, rounds: int, pause: float = 0.9) -> int:
    """Scroll until the record count stops growing (stall detection, no fixed sleep)."""
    last, stalls = -1, 0
    for _ in range(max(1, rounds)):
        cmd({"cmd": "eval", "id": pid,
             "expr": "() => { window.scrollBy(0, Math.round(innerHeight * 0.9)); return true; }"})
        time.sleep(pause)
        count = cmd({"cmd": "eval", "id": pid, "expr": count_expr}).get("result")
        count = count if isinstance(count, int) else 0
        if count == last:
            stalls += 1
            if stalls >= 2:
                return count
        else:
            stalls = 0
        last = count
    return max(last, 0)


def open_only(site: dict) -> tuple[int | None, str]:
    opened = cmd({"cmd": "open", "url": site["url"], "wait": "commit"})
    return opened.get("id"), str(opened.get("error") or "")


def prepare_site(site: dict, pid: int) -> None:
    if site.get("ui_search"):
        ui = site["ui_search"]
        # commit returns before the box exists; without this wait the search is
        # silently skipped and the site yields zero rows.
        cmd({"cmd": "wait", "id": pid, "for": "selector", "selector": ui["input"],
             "state": "visible", "timeout": 20000})
        box = cmd({"cmd": "center", "id": pid, "selector": ui["input"]})
        if box.get("ok"):
            cmd({"cmd": "click", "id": pid, "x": box["x"], "y": box["y"]})
            cmd({"cmd": "type", "id": pid, "text": ui["query"], "delay": 30})
            cmd({"cmd": "key", "id": pid, "key": "Enter"})
            if ui.get("until_url"):
                cmd({"cmd": "wait", "id": pid, "for": "url", "pattern": ui["until_url"],
                     "timeout": 20000})
            time.sleep(3)
    cmd({"cmd": "wait", "id": pid, "for": "selector", "selector": site["wait"],
         "state": "attached", "timeout": 25000})
    if site.get("ready_js"):
        cmd({"cmd": "wait", "id": pid, "for": "js", "expr": site["ready_js"], "timeout": 25000})
    if site.get("delay"):
        time.sleep(float(site["delay"]))
    if site.get("scroll_until"):
        scroll_until(pid, site["scroll_until"], int(site.get("scroll") or 6))
    elif site.get("scroll"):
        scroll(pid, int(site["scroll"]))


def _complete_score(rows: list[dict]) -> tuple[int, float]:
    """Rank by complete rows, then by field fill. A 3-row junk result must lose."""
    if not rows:
        return 0, 0.0
    keys = sorted({key for row in rows for key in row})
    complete = sum(1 for row in rows if keys and all(str(row.get(k, "")).strip() for k in keys))
    filled = sum(1 for row in rows for k in keys if str(row.get(k, "")).strip())
    total = max(1, len(rows) * len(keys))
    return complete, filled / total


def extract(site: dict, pid: int) -> tuple[list[dict], str]:
    """Try extractors in order, keep the best by complete rows, then fill.

    Tries at most two more expressions after the first one that returns rows, so
    a fallback cannot turn a 1 s site into a 25 s stall.
    """
    expressions = site.get("js") or []
    if isinstance(expressions, str):
        expressions = [expressions]
    best: tuple[list[dict], str] | None = None
    best_score = (-1, -1.0)
    attempts = 0
    for index, expression in enumerate(expressions):
        if best is not None and attempts >= 2:
            break
        result = cmd({"cmd": "eval", "id": pid, "expr": expression}, timeout=150)
        data = result.get("result")
        attempts += 1
        if not isinstance(data, list):
            continue
        rows = [r for r in data if isinstance(r, dict) and any(str(v).strip() for v in r.values())]
        score = _complete_score(rows)
        if score > best_score:
            best_score, best = score, (rows, f"js[{index}]")
        if best_score[0] > 0 and best_score[1] >= 0.98:
            break
    if best and best[0]:
        return best[0], best[1]
    if site.get("human"):
        solved = cmd({"cmd": "solve", "id": pid,
                      "timeout_s": float(site.get("human_timeout_s") or 300)},
                     timeout=float(site.get("human_timeout_s") or 300) + 60)
        if solved.get("solved"):
            for index, expression in enumerate(expressions):
                data = cmd({"cmd": "eval", "id": pid, "expr": expression}, timeout=150).get("result")
                if isinstance(data, list):
                    rows = [r for r in data if isinstance(r, dict) and any(str(v).strip() for v in r.values())]
                    if rows:
                        return rows, f"js[{index}]+human"
        else:
            return [], "challenge_unsolved"
    return [], "none"


def extract_pages(site: dict, pid: int) -> tuple[list[dict], str, bool]:
    until_empty = site.get("paginate") == "until_empty"
    pages = int(site.get("max_pages") or 10) if until_empty else int(site.get("pages") or 1)
    out: list[dict] = []
    source = "none"
    blocked = False
    for index in range(1, pages + 1):
        if index > 1:
            if site.get("page_url"):
                target = str(site["page_url"]).format(page=index)
                pattern = f"page/{index}" if "/page/" in target else f"page={index}"
            else:
                sep = "&" if "?" in site["url"] else "?"
                target = f"{site['url']}{sep}page={index}"
                pattern = f"page={index}"
            nav = cmd({"cmd": "goto", "id": pid, "url": target, "wait": "commit"})
            if not nav.get("ok"):
                # A blocked page 2 is not selector drift: page 1 rows are real,
                # the rest never arrived.
                blocked = nav.get("error") in ("rate_limited", "bidi_displaced")
                break
            # Wait for the URL, not just the selector: the old page still has the
            # selector attached, so extracting too early returns page 1 again.
            cmd({"cmd": "wait", "id": pid, "for": "url", "pattern": pattern,
                 "timeout": 15000})
            cmd({"cmd": "wait", "id": pid, "for": "selector", "selector": site["wait"],
                 "state": "attached", "timeout": 20000})
            time.sleep(2)
            if site.get("scroll"):
                scroll(pid, int(site["scroll"]))
        rows, source = extract(site, pid)
        if until_empty and not rows:
            break
        out.extend(rows)
    return out, source, blocked


def parse_amount(text: str) -> float | None:
    """Parse a price like 1.234,56 / 1,234.56 / 51.77 / 140,08 / 1.234."""
    match = PRICE.search(text)
    if not match:
        return None
    raw = match.group(1)
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[-1]
        raw = raw.replace(",", ".") if len(tail) <= 2 else raw.replace(",", "")
    elif "." in raw:
        tail = raw.rsplit(".", 1)[-1]
        # 1.000 is a thousand; 0.001 and 51.777 are decimals.
        if tail == "000" and 1 <= len(raw.rsplit(".", 1)[0]) <= 3:
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def normalize(rows: list[dict]) -> list[dict]:
    cleaned = []
    for row in rows:
        item = {}
        for key, value in row.items():
            text = str(value).strip()
            text = re.sub(r"\s+", " ", text)
            if key in ("age", "posted", "date") and text:
                months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                          "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
                          "december": 12}
                relative = re.match(r"(\d+)\s*(minute|min|hour|h|day|d|week|w|month|mo)s?\s*ago", text, re.I)
                german = re.match(r"vor\s*(\d+)\s*(minute|stunde|tag|woche|monat)", text, re.I)
                if relative:
                    unit = relative.group(2).lower()[0]
                    item[key] = f"{relative.group(1)}{unit}"
                    continue
                if german:
                    unit = {"m": "m", "s": "h", "t": "d", "w": "w"}[german.group(2).lower()[0]]
                    item[key] = f"{german.group(1)}{unit}"
                    continue
                if re.fullmatch(r"(yesterday|gestern)", text, re.I):
                    item[key] = "1d"
                    continue
            if key in ("price", "viewers") and text:
                number = parse_amount(text)
                if number is not None:
                    kind = " SEK" if "SEK" in text.upper() else (" EUR" if "EUR" in text.upper() or "€" in text else "")
                    item[key] = f"{number:g}{kind}"
                    continue
            item[key] = text
        cleaned.append(item)
    return cleaned


def dedupe(rows: list[dict], key: str | None) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        marker = str(row.get(key or "", "")).strip().lower()
        if marker and marker in seen:
            continue
        if marker:
            seen.add(marker)
        out.append(row)
    return out


def run_batch(sites: list[dict], parallel: int) -> list[dict]:
    """Open `parallel` pages with commit, then prepare/extract them in order.

    Network loads overlap because the pages exist; commands stay serial on the
    worker thread, which is the honest limit of one browser.
    """
    results = []
    for start in range(0, len(sites), parallel):
        chunk = sites[start:start + parallel]
        opened = []
        for site in chunk:
            pid, error = open_only(site)
            opened.append((site, pid))
            if pid is None:
                blocked = "rate_limited" in error or "burned" in error
                state = "blocked" if blocked else ("unreachable" if "unreachable" in error else "low")
                results.append({"name": site["name"], "rows": 0, "source": error or "open_failed",
                                "state": state, "seconds": 0,
                                "records": [], "min_rows": int(site.get("min_rows") or 1)})
        for site, pid in opened:
            if pid is None:
                continue
            started = time.time()
            try:
                prepare_site(site, pid)
                if site.get("pages") or site.get("paginate"):
                    rows, source, blocked = extract_pages(site, pid)
                else:
                    rows, source = extract(site, pid)
                    blocked = source == "challenge_unsolved"
            finally:
                cmd({"cmd": "close", "id": pid})
            rows = dedupe(normalize(rows), site.get("key"))
            min_rows = int(site.get("min_rows") or 1)
            state = "blocked" if blocked else ("ok" if len(rows) >= min_rows else "low")
            results.append({"name": site["name"], "rows": len(rows), "source": source,
                            "state": state, "seconds": round(time.time() - started, 1),
                            "records": rows, "min_rows": min_rows})
    return results


BLOCK_MARKERS = ("error page", "sorry", "you are unable to access", "attention required",
                 "access denied", "rate limit", "too many requests", "captcha")


def shortfall_state(pid: int) -> str:
    """Tell drift apart from a block: a block is not a selector problem."""
    try:
        probe = cmd({"cmd": "eval", "id": pid,
                     "expr": "({t: document.title || '', x: (document.body ? document.body.innerText : '').slice(0, 400)})"})
        text = f"{probe.get('result', {}).get('t', '')} {probe.get('result', {}).get('x', '')}".lower()
    except Exception:  # noqa: BLE001
        return "low"
    return "blocked" if any(marker in text for marker in BLOCK_MARKERS) else "low"


def run_site(site: dict, keep_browser: bool = False) -> dict:
    started = time.time()
    pid, error = open_only(site)
    if pid is None:
        blocked = "rate_limited" in error or "burned" in error
        state = "blocked" if blocked else ("unreachable" if "unreachable" in error else "low")
        return {"name": site["name"], "rows": 0, "source": error or "open_failed",
                "state": state, "seconds": 0,
                "records": [], "min_rows": int(site.get("min_rows") or 1)}
    prepare_site(site, pid)
    try:
        if site.get("pages") or site.get("paginate"):
            rows, source, blocked = extract_pages(site, pid)
        else:
            rows, source = extract(site, pid)
            blocked = source == "challenge_unsolved"
    finally:
        if not keep_browser:
            cmd({"cmd": "close", "id": pid})
    rows = dedupe(normalize(rows), site.get("key"))
    min_rows = int(site.get("min_rows") or 1)
    state = "blocked" if blocked else ("ok" if len(rows) >= min_rows else shortfall_state(pid))
    return {"name": site["name"], "rows": len(rows), "source": source, "state": state,
            "seconds": round(time.time() - started, 1), "records": rows, "min_rows": min_rows}


def export(rows: list[dict], path: Path) -> None:
    if path.suffix.lower() == ".xlsx" and Workbook is None:
        raise SystemExit("xlsx export needs openpyxl: pip install openpyxl (or write .csv)")
    if path.suffix.lower() == ".xlsx":
        wb = Workbook()
        ws = wb.active
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        ws.append(keys)
        for row in rows:
            ws.append([row.get(k, "") for k in keys])
        for col, key in enumerate(keys, start=1):
            ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = min(60, max(12, len(key) + 10))
        wb.save(path)
        return
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--canary", action="store_true", help="exit 1 if a site drops below min_rows")
    parser.add_argument("--out", help="CSV or XLSX output path")
    parser.add_argument("--registry", help="path to an alternative sites.yaml")
    parser.add_argument("--json", action="store_true", help="print records as JSON")
    parser.add_argument("--parallel", type=int, default=1, help="pages loaded at once (1-4)")
    parser.add_argument("--report", help="write a JSON run report to this path")
    args = parser.parse_args()

    sites = load_registry(Path(args.registry) if args.registry else REGISTRY)
    if args.list:
        for site in sites:
            print(f"{site['name']:14} min_rows={site.get('min_rows')} pages={site.get('pages', 1)} "
                  f"scroll={site.get('scroll', 0)}")
        return 0

    selected = sites if args.all or not args.site else [s for s in sites if s["name"] in args.site]
    if not selected:
        print("no matching site", file=sys.stderr)
        return 2

    started_all = time.time()
    batch = run_batch(selected, args.parallel) if args.parallel > 1 else [run_site(s) for s in selected]
    results, rows = [], []
    for result in batch:
        results.append({k: v for k, v in result.items() if k != "records"})
        rows.extend(result["records"])
        state = result.get("state") or ("ok" if result["rows"] >= result["min_rows"] else "low")
        state = {"ok": "ok", "low": "LOW", "blocked": "BLOCK", "unreachable": "DOWN"}.get(state, state)
        print(f"{state:3} {result['name']:14} rows={result['rows']:<3} "
              f"source={result['source']:<8} {result['seconds']:>5}s", flush=True)
    print(f"wall {time.time() - started_all:.1f}s (parallel={args.parallel})")

    print(f"\n{sum(r['rows'] for r in results)} records from {len(results)} sites")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    if args.out:
        export(rows, Path(args.out))
        print(f"written {args.out}")
    if args.report:
        Path(args.report).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.canary:
        drift = [r for r in results if r["state"] == "low"]
        down = [r for r in results if r["state"] == "unreachable"]
        blocked = [r for r in results if r["state"] == "blocked"]
        for row in drift:
            print(f"CANARY FAIL {row['name']}: {row['rows']} < {row['min_rows']} (drift)", file=sys.stderr)
        for row in down:
            print(f"CANARY FAIL {row['name']}: worker unreachable ({row['source']})", file=sys.stderr)
        for row in blocked:
            print(f"CANARY WARN {row['name']}: blocked / rate limited, not drift", file=sys.stderr)
        return 1 if (drift or down) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
