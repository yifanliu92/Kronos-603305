#!/usr/bin/env python3
"""
Kronos — Universal A-share simulation CLI
Usage (one-liner):  python3 kronos.py 603305 simulate
Usage (REPL):       python3 kronos.py
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
STOCKS_DIR = BASE_DIR / "stocks"
CACHE_PATH = BASE_DIR / ".stock_name_cache.json"

# ── Source ticker (template) ───────────────────────────────────────────────────
SRC_CODE = "603305"
SRC_NAME = "旭升集团"

HELP_TEXT = """
Kronos — A-share simulation CLI
────────────────────────────────
Commands:
  <code> simulate    Run position simulation       (e.g. 603305 simulate)
  <code> shadow      Run shadow strategy
  <code> review      Post-close review             (e.g. 603305 review)
  <code> winrate     Win rate report
  <code> status      Show current simulated position
  <code> reset       Reset simulated state
  <codes> add to watchlist
                     Bulk-add tickers (no simulate run needed)
                     e.g. 603305, 688582, 688127 add to watchlist
  watchlist add <codes>
                     Same, command-first form
  stocks             List all tracked stocks
  dashboard          Cross-ticker summary (position, price, win rate, signal)
  dashboard html     Same, written to dashboard.html (charts incl. signals)
  help               Show this message
  exit / quit        Exit

Examples:
  Kronos> 603305 simulate
  Kronos> 000001 simulate
  Kronos> 603305 review
  Kronos> 688582, 688127, 688608 add to watchlist
  Kronos> stocks
  Kronos> dashboard html
"""

# ── Stock name resolution ──────────────────────────────────────────────────────

def _load_name_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_name_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fetch_name_tencent(code: str) -> str | None:
    """
    Tencent qt.gtimg.cn quote feed — primary name source. Response is GBK-
    encoded and tilde-delimited, e.g. v_sz000001="51~平安银行~000001~...";
    Field index 1 (0-based) is the stock name. Used as primary because
    EastMoney's f58 field comes back empty for SZ-listed tickers.
    """
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        text = raw.decode("gbk", errors="ignore")
        fields = text.split('"')[1].split("~")
        name = fields[1].strip()
        return name or None
    except Exception:
        return None


def _fetch_name_eastmoney(code: str) -> str | None:
    """EastMoney quote API fallback. Field f58 = stock name."""
    secid_prefix = "1" if code.startswith("6") else "0"
    url = (
        f"https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid_prefix}.{code}&fields=f58"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        name = data.get("data", {}).get("f58")
        return name or None
    except Exception:
        return None


def fetch_stock_name(code: str) -> str:
    """
    Fetch stock name, Tencent gtimg.cn primary with EastMoney as fallback.
    Falls back to the code itself if both requests fail.
    Result is cached locally so repeated calls don't hit the network.
    """
    cache = _load_name_cache()
    if code in cache:
        return cache[code]

    name = _fetch_name_tencent(code) or _fetch_name_eastmoney(code) or code
    cache[code] = name
    _save_name_cache(cache)
    return name


def stock_name(code: str) -> str:
    """Return stock name, using cache + live API lookup."""
    if code == SRC_CODE:
        return SRC_NAME
    return fetch_stock_name(code)


# ── Workspace setup ────────────────────────────────────────────────────────────

def _patch(text: str, code: str, name: str, stock_dir: Path, exchange: str) -> str:
    """Apply all text substitutions needed to port a script to a new ticker."""
    # Protect any literal BASE_DIR path with a sentinel BEFORE the SRC_CODE
    # replacement below. BASE_DIR's own path (e.g. ".../Kronos-Multi-Stock")
    # contains SRC_CODE as a substring, so replacing SRC_CODE first corrupts
    # it (".../Kronos-Multi-Stock" -> ".../Kronos-000001"); the later BASE_DIR ->
    # stock_dir replace then finds no match and silently leaves a broken
    # hardcoded path pointing outside the per-stock workspace (this is what
    # broke calc_winrate_<code>.py's OUT_DIR for 000001).
    sentinel = "\x00KRONOS_BASE_DIR\x00"
    text = text.replace(str(BASE_DIR), sentinel)
    text = text.replace(SRC_CODE, code)
    text = text.replace(SRC_NAME, name)
    text = text.replace(sentinel, str(stock_dir))
    if exchange == "SZ":
        text = text.replace('"SH"', '"SZ"').replace("'SH'", "'SZ'")
        text = text.replace(f"secid=1.{code}", f"secid=0.{code}")
        # EastMoney secid is often built as `secid = f"1.{symbol}"` rather than
        # a literal "secid=1.<code>" string — catch that construction too
        # (signal_router_603305.py does this; the line above misses it).
        text = re.sub(r'secid\s*=\s*f(["\'])1\.\{(\w+)\}\1', r'secid = f\g<1>0.{\2}\g<1>', text)
        # Tencent qt.gtimg.cn uses a sh/sz market prefix in the URL path
        # (e.g. f"...q=sh{symbol}") — separate from the "SH"/"SZ" field values
        # handled above.
        text = re.sub(r'(q=)sh(\{\w+\})', r'\1sz\2', text)
    return text


def workspace_dir(code: str) -> Path:
    """
    Root-level dir for the source ticker (its scripts/state already live
    there) — per-ticker subdir under stocks/ for everything else. Without
    this, the source ticker would get re-scaffolded into stocks/<SRC_CODE>/
    as a second, diverging copy of itself with its own independent state.
    """
    return BASE_DIR if code == SRC_CODE else STOCKS_DIR / code


def ensure_stock_workspace(code: str) -> Path:
    """
    First call: scaffold a per-stock workspace by copying and patching
    all template scripts from the SRC_CODE originals.
    Subsequent calls: return the existing directory immediately.
    SRC_CODE itself is never scaffolded — its scripts/state already live
    at BASE_DIR, and scaffolding it would create a duplicate, diverging
    copy of the real tracked position.
    """
    if code == SRC_CODE:
        return BASE_DIR

    stock_dir = STOCKS_DIR / code
    if stock_dir.exists():
        return stock_dir

    name = stock_name(code)
    exchange = "SH" if code.startswith("6") else "SZ"
    print(f"[Kronos] Setting up workspace for {code} {name} ({exchange})...")

    # Create directory structure
    for sub in ("sim_logs_daily", "strategy_compare_reports", "scripts"):
        (stock_dir / sub).mkdir(parents=True, exist_ok=True)

    # Core scripts to copy & patch
    script_map = {
        f"simulate_position_{SRC_CODE}.py":        f"simulate_position_{code}.py",
        f"simulate_position_{SRC_CODE}_shadow.py": f"simulate_position_{code}_shadow.py",
        f"sim_review_{SRC_CODE}.py":               f"sim_review_{code}.py",
        f"signal_router_{SRC_CODE}.py":            f"signal_router_{code}.py",
        f"auto_report_guard_{SRC_CODE}.py":        f"auto_report_guard_{code}.py",
        "short_cost_calculator.py":                "short_cost_calculator.py",
    }
    for src_name, dst_name in script_map.items():
        src = BASE_DIR / src_name
        if not src.exists():
            continue
        text = _patch(src.read_text(encoding="utf-8"), code, name, stock_dir, exchange)
        (stock_dir / dst_name).write_text(text, encoding="utf-8")

    # scripts/ subdirectory — needs BOTH content patching AND filename renaming
    # (e.g. lot_ledger_603305.py -> lot_ledger_000001.py), otherwise cross-imports
    # inside the patched content (which now reference the new module name) fail
    # to resolve because the file on disk still has the old SRC_CODE name.
    scripts_src = BASE_DIR / "scripts"
    if scripts_src.exists():
        shutil.copytree(scripts_src, stock_dir / "scripts", dirs_exist_ok=True)
        for py in list((stock_dir / "scripts").glob("*.py")):
            try:
                text = _patch(py.read_text(encoding="utf-8"), code, name, stock_dir, exchange)
                dst = py.with_name(py.name.replace(SRC_CODE, code)) if SRC_CODE in py.name else py
                dst.write_text(text, encoding="utf-8")
                if dst != py:
                    py.unlink()
            except Exception:
                pass

    # JSON configs — also patch exchange_code field
    json_map = {
        f"simulate_rules_{SRC_CODE}.json": f"simulate_rules_{code}.json",
        f"signal_rules_{SRC_CODE}.json":   f"signal_rules_{code}.json",
        f"sim_costs_{SRC_CODE}.json":      f"sim_costs_{code}.json",
    }
    for src_name, dst_name in json_map.items():
        src = BASE_DIR / src_name
        if not src.exists():
            continue
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            if "exchange_code" in data:
                data["exchange_code"] = exchange
            (stock_dir / dst_name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            shutil.copy2(src, stock_dir / dst_name)

    print(f"[Kronos] Workspace ready → {stock_dir}")
    return stock_dir


def tracked_codes() -> list[str]:
    """All tracked ticker codes, SRC_CODE first (its state lives at BASE_DIR,
    not under stocks/, so it isn't picked up by the STOCKS_DIR scan below)."""
    codes = [SRC_CODE]
    if STOCKS_DIR.exists():
        codes += sorted(d.name for d in STOCKS_DIR.iterdir() if d.is_dir() and d.name != SRC_CODE)
    return codes


def load_trade_records(code: str) -> list[dict]:
    """All historical signal/trade events for a ticker, oldest first. Each
    line of sim_trades_<code>.jsonl is one simulate run (price, signal,
    position_from/to, performance, cost) — this is the time series the
    dashboard charts are built from."""
    path = workspace_dir(code) / f"sim_trades_{code}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    records.sort(key=lambda r: r.get("ts", ""))
    return records


def latest_winrate_report(code: str) -> dict | None:
    """Most recently generated winrate_<code>_*.json report, or None."""
    reports_dir = workspace_dir(code) / "strategy_compare_reports"
    if not reports_dir.exists():
        return None
    files = sorted(reports_dir.glob(f"winrate_{code}_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


# ── Script runner ──────────────────────────────────────────────────────────────

def run_script(script: Path, stock_dir: Path) -> None:
    if not script.exists():
        print(f"[Kronos] Script not found: {script.name}")
        return
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(stock_dir) + os.pathsep +
        str(BASE_DIR)  + os.pathsep +
        env.get("PYTHONPATH", "")
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(stock_dir),
        env=env,
    )
    if result.returncode != 0:
        print(f"[Kronos] Exited with code {result.returncode}")


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_simulate(code: str) -> None:
    d = ensure_stock_workspace(code)
    run_script(d / f"simulate_position_{code}.py", d)

def cmd_shadow(code: str) -> None:
    d = ensure_stock_workspace(code)
    run_script(d / f"simulate_position_{code}_shadow.py", d)

def cmd_review(code: str) -> None:
    d = ensure_stock_workspace(code)
    run_script(d / f"sim_review_{code}.py", d)

def cmd_winrate(code: str) -> None:
    d = ensure_stock_workspace(code)
    run_script(d / "scripts" / f"calc_winrate_{code}.py", d)

def cmd_status(code: str) -> None:
    state_path = workspace_dir(code) / f"sim_state_{code}.json"
    if not state_path.exists():
        print(f"[Kronos] No state for {code}. Run '{code} simulate' first.")
        return
    try:
        s = json.loads(state_path.read_text(encoding="utf-8"))
        pos     = s.get("position_pct", 0)
        updated = s.get("updated_at", "—")
        avg     = s.get("avg_entry_price")
        cost    = float(s.get("cumulative_cost", 0.0))
        side    = "空仓" if pos < 0 else ("多仓" if pos > 0 else "空仓(0%)")
        name    = stock_name(code)
        print(f"\n── {code} {name} {'─' * max(1, 30 - len(name))}")
        print(f"  仓位:     {pos:+d}% ({side})")
        print(f"  更新时间: {updated}")
        if avg:
            print(f"  持仓均价: {float(avg):.2f}")
        print(f"  累计成本: {cost:.2f} 元\n")
    except Exception as e:
        print(f"[Kronos] Error reading state: {e}")

def cmd_reset(code: str) -> None:
    state_path = workspace_dir(code) / f"sim_state_{code}.json"
    if not state_path.exists():
        print(f"[Kronos] Nothing to reset for {code}.")
        return
    confirm = input(f"Reset state for {code} {stock_name(code)}? [y/N] ").strip().lower()
    if confirm == "y":
        state_path.unlink()
        print(f"[Kronos] State reset for {code}.")
    else:
        print("[Kronos] Cancelled.")

def cmd_stocks() -> None:
    codes = tracked_codes()

    if len(codes) == 1 and not (BASE_DIR / f"sim_state_{SRC_CODE}.json").exists():
        print("[Kronos] No stocks tracked yet. Run '<code> simulate' to start.")
        return

    print(f"\n── Tracked Stocks {'─' * 28}")
    for code in codes:
        d = workspace_dir(code)
        state_path = d / f"sim_state_{code}.json"
        name = stock_name(code)
        if state_path.exists():
            try:
                s = json.loads(state_path.read_text(encoding="utf-8"))
                pos     = s.get("position_pct", 0)
                updated = s.get("updated_at", "—")
                print(f"  {code}  {name:<10}  {pos:+4d}%   {updated}")
            except Exception:
                print(f"  {code}  {name}")
        else:
            print(f"  {code}  {name:<10}  (未初始化)")
    print()


def cmd_add_watchlist(codes_raw: str) -> None:
    """
    Bulk-add tickers to the tracked-stocks workspace, e.g.
    '603305, 688582, 688127 add to watchlist'. Just scaffolds each
    ticker's workspace (the same setup the first simulate/shadow/etc.
    run would trigger) without running a simulation, so they show up
    in 'stocks' and 'dashboard' immediately.
    """
    tokens = [c for c in re.split(r"[,\s]+", codes_raw.strip()) if c]
    if not tokens:
        print("[Kronos] No stock codes given. Example: 603305, 000001 add to watchlist")
        return

    added, already, invalid = [], [], []
    for code in tokens:
        if not (code.isdigit() and len(code) == 6):
            invalid.append(code)
            continue
        was_tracked = code == SRC_CODE or (STOCKS_DIR / code).exists()
        ensure_stock_workspace(code)
        (already if was_tracked else added).append(code)

    print()
    if added:
        print(f"[Kronos] Added to watchlist: {', '.join(f'{c} {stock_name(c)}' for c in added)}")
    if already:
        print(f"[Kronos] Already tracked: {', '.join(f'{c} {stock_name(c)}' for c in already)}")
    if invalid:
        print(f"[Kronos] Skipped invalid codes (need 6 digits): {', '.join(invalid)}")
    print()


# ── Dashboard ──────────────────────────────────────────────────────────────────

def _dashboard_rows() -> list[dict]:
    """Gather per-ticker state, trade history, and latest win-rate report —
    shared by both the CLI table and the HTML export."""
    rows = []
    for code in tracked_codes():
        d = workspace_dir(code)
        state_path = d / f"sim_state_{code}.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        rows.append({
            "code": code,
            "name": stock_name(code),
            "state": state,
            "trades": load_trade_records(code),
            "winrate": latest_winrate_report(code),
        })
    return rows


def _print_dashboard_cli(rows: list[dict]) -> None:
    print(f"\n══ Kronos Multi-Stock Dashboard {'═' * 28}")
    for r in rows:
        code, name, state, trades = r["code"], r["name"], r["state"], r["trades"]
        pos     = state.get("position_pct", 0)
        price   = state.get("last_price")
        cost    = float(state.get("cumulative_cost", 0.0))
        updated = state.get("updated_at", "—")
        side    = "多仓" if pos > 0 else ("空仓" if pos < 0 else "无仓位")
        last    = trades[-1] if trades else {}

        wr_main = ((r["winrate"] or {}).get("main") or {}).get("direction_next_tick") or {}
        wr_pct, wr_n = wr_main.get("winrate"), wr_main.get("samples", 0)
        wr_str  = f"{wr_pct:.1%}" if isinstance(wr_pct, (int, float)) else "—"

        print(f"\n  {code}  {name}")
        print(f"    仓位: {pos:+d}% ({side})   现价: {price if price is not None else '—'}   累计成本: {cost:.2f} 元")
        print(f"    最新信号: {last.get('signal', '—')}   动作: {last.get('action', '—')}   更新: {updated}")
        print(f"    胜率(下一条方向): {wr_str}  (样本数: {wr_n})   历史记录: {len(trades)} 条")
    print()


def _svg_price_chart(trades: list[dict], width: int = 720, height: int = 200) -> str:
    """Line chart of price over time; each point is colored by the signal
    fired at that record (green = long, red = short, gray = flat/neutral)."""
    pad_l, pad_r, pad_t, pad_b = 50, 16, 16, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    priced = [t for t in trades if isinstance(t.get("price"), (int, float))]
    if not priced:
        return (f'<svg viewBox="0 0 {width} {height}" class="kchart">'
                 f'<text x="{width/2}" y="{height/2}" class="kchart-empty" '
                 f'text-anchor="middle">暂无数据 / No data yet</text></svg>')

    prices = [t["price"] for t in priced]
    lo, hi = min(prices), max(prices)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    n = len(priced)

    def x_at(i: int) -> float:
        return pad_l if n == 1 else pad_l + plot_w * i / (n - 1)

    def y_at(p: float) -> float:
        return pad_t + plot_h * (1 - (p - lo) / span)

    pts, dots = [], []
    for i, t in enumerate(priced):
        x, y = x_at(i), y_at(t["price"])
        pts.append(f"{x:.1f},{y:.1f}")
        pos_to = t.get("position_to") or 0
        color = "#1a9850" if pos_to > 0 else ("#d73027" if pos_to < 0 else "#999")
        title = html.escape(f"{t.get('ts', '')}\n{t.get('signal', '')} | {t.get('action', '')}\nprice {t['price']}")
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
                     f'stroke="#fff" stroke-width="1"><title>{title}</title></circle>')

    line = (f'<polyline points="{" ".join(pts)}" fill="none" stroke="#4477aa" stroke-width="1.5" />'
            if len(pts) > 1 else "")
    first_ts = html.escape(str(priced[0].get("ts", ""))[:10])
    last_ts  = html.escape(str(priced[-1].get("ts", ""))[:10])

    return f'''<svg viewBox="0 0 {width} {height}" class="kchart">
  <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" class="axis"/>
  <line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="axis"/>
  <text x="{pad_l - 6}" y="{pad_t + 4}" class="kchart-label" text-anchor="end">{hi:.2f}</text>
  <text x="{pad_l - 6}" y="{height - pad_b}" class="kchart-label" text-anchor="end">{lo:.2f}</text>
  <text x="{pad_l}" y="{height - 8}" class="kchart-label">{first_ts}</text>
  <text x="{width - pad_r}" y="{height - 8}" class="kchart-label" text-anchor="end">{last_ts}</text>
  {line}
  {''.join(dots)}
</svg>'''


def _svg_position_strip(trades: list[dict], width: int = 720, height: int = 56) -> str:
    """Step/bar strip of position_pct (-100%..+100%) over time — the
    'simulation result' shape beneath the price+signal chart above."""
    pad_l, pad_r, pad_t, pad_b = 50, 16, 8, 16
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    if not trades:
        return ""
    n = len(trades)

    def x_at(i: int) -> float:
        return pad_l if n == 1 else pad_l + plot_w * i / (n - 1)

    zero_y = pad_t + plot_h / 2

    def y_at(pos: float) -> float:
        return pad_t + plot_h * (1 - (pos + 100) / 200)

    bars = []
    for i, t in enumerate(trades):
        pos = t.get("position_to") or 0
        x = x_at(i)
        top, h = min(zero_y, y_at(pos)), abs(y_at(pos) - zero_y)
        color = "#1a9850" if pos > 0 else ("#d73027" if pos < 0 else "#ccc")
        bars.append(f'<rect x="{x - 3:.1f}" y="{top:.1f}" width="6" height="{max(h, 1):.1f}" '
                     f'fill="{color}"><title>{pos:+d}%</title></rect>')

    return f'''<svg viewBox="0 0 {width} {height}" class="kchart kchart-strip">
  <line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" class="axis-zero"/>
  <text x="{pad_l - 6}" y="{pad_t + 8}" class="kchart-label" text-anchor="end">+100%</text>
  <text x="{pad_l - 6}" y="{height - pad_b}" class="kchart-label" text-anchor="end">-100%</text>
  {''.join(bars)}
</svg>'''


_DASHBOARD_CSS = """
  body { font-family: -apple-system, "PingFang SC", Helvetica, Arial, sans-serif;
         background:#f6f7f9; color:#222; margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .meta { color:#777; font-size:12px; margin-bottom:20px; }
  table { border-collapse:collapse; width:100%; background:#fff; border-radius:8px;
          overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:28px; }
  th, td { padding:8px 12px; text-align:left; font-size:13px; border-bottom:1px solid #eee; }
  th { background:#fafafa; color:#666; font-weight:600; }
  td.long { color:#1a9850; font-weight:600; }
  td.short { color:#d73027; font-weight:600; }
  td.flat { color:#888; }
  section.ticker { background:#fff; border-radius:8px; padding:16px 20px;
                   margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  section.ticker h2 { font-size:15px; margin:0 0 8px; }
  section.ticker h2 .name { color:#666; font-weight:400; margin-left:6px; }
  .pos { float:right; font-size:13px; padding:2px 8px; border-radius:4px; }
  .pos.long { background:#e6f4ea; color:#1a9850; }
  .pos.short { background:#fbe7e6; color:#d73027; }
  .pos.flat { background:#eee; color:#888; }
  svg.kchart { width:100%; height:auto; display:block; }
  .axis, .axis-zero { stroke:#ccc; stroke-width:1; }
  .kchart-label { font-size:10px; fill:#888; }
  .kchart-empty { font-size:12px; fill:#aaa; }
"""


def _write_dashboard_html(rows: list[dict]) -> None:
    summary_rows, sections = [], []
    for r in rows:
        code, name, state, trades = r["code"], r["name"], r["state"], r["trades"]
        pos     = state.get("position_pct", 0)
        price   = state.get("last_price")
        cost    = float(state.get("cumulative_cost", 0.0))
        updated = html.escape(str(state.get("updated_at", "—")))
        side       = "多仓" if pos > 0 else ("空仓" if pos < 0 else "无仓位")
        side_class = "long" if pos > 0 else ("short" if pos < 0 else "flat")

        wr_main = ((r["winrate"] or {}).get("main") or {}).get("direction_next_tick") or {}
        wr_pct, wr_n = wr_main.get("winrate"), wr_main.get("samples", 0)
        wr_str  = f"{wr_pct:.1%}" if isinstance(wr_pct, (int, float)) else "—"

        summary_rows.append(
            f'<tr><td>{html.escape(code)}</td><td>{html.escape(name)}</td>'
            f'<td class="{side_class}">{pos:+d}% ({side})</td>'
            f'<td>{price if price is not None else "—"}</td>'
            f'<td>{cost:.2f}</td><td>{wr_str} ({wr_n})</td>'
            f'<td>{len(trades)}</td><td>{updated}</td></tr>'
        )
        sections.append(f'''
    <section class="ticker">
      <h2>{html.escape(code)} <span class="name">{html.escape(name)}</span>
        <span class="pos {side_class}">{pos:+d}% {side}</span></h2>
      <div>{_svg_price_chart(trades)}</div>
      <div>{_svg_position_strip(trades)}</div>
    </section>''')

    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Kronos Multi-Stock Dashboard</title>
<style>{_DASHBOARD_CSS}</style>
</head>
<body>
  <h1>Kronos Multi-Stock Dashboard</h1>
  <div class="meta">Generated {ts_now} · {len(rows)} tickers tracked</div>
  <table>
    <thead><tr><th>Code</th><th>Name</th><th>Position</th><th>Last Price</th>
      <th>Cum. Cost</th><th>Win Rate (n)</th><th>Records</th><th>Updated</th></tr></thead>
    <tbody>{''.join(summary_rows)}</tbody>
  </table>
  {''.join(sections)}
</body>
</html>"""
    out_path = BASE_DIR / "dashboard.html"
    out_path.write_text(doc, encoding="utf-8")
    print(f"[Kronos] Dashboard written → {out_path}")


def cmd_dashboard(as_html: bool = False) -> None:
    rows = _dashboard_rows()
    if len(rows) == 1 and not rows[0]["state"]:
        print("[Kronos] No stocks tracked yet. Run '<code> simulate' to start.")
        return
    if as_html:
        _write_dashboard_html(rows)
    else:
        _print_dashboard_cli(rows)


# ── Dispatcher ─────────────────────────────────────────────────────────────────

COMMANDS: dict[str, object] = {
    "simulate": cmd_simulate,  "模拟": cmd_simulate,
    "shadow":   cmd_shadow,    "影子": cmd_shadow,
    "review":   cmd_review,    "复盘": cmd_review,
    "winrate":  cmd_winrate,   "胜率": cmd_winrate,
    "status":   cmd_status,    "状态": cmd_status,
    "reset":    cmd_reset,     "重置": cmd_reset,
}


_WATCHLIST_SUFFIX = "add to watchlist"


def dispatch(raw: str) -> bool:
    """Parse one command string. Returns False to signal exit."""
    stripped = raw.strip()
    if not stripped:
        return True

    # Bulk watchlist add, codes-first phrasing:
    # "603305, 688582, 688127 add to watchlist"
    normalized = re.sub(r"\s+", " ", stripped)
    if normalized.lower().endswith(_WATCHLIST_SUFFIX):
        cmd_add_watchlist(normalized[: -len(_WATCHLIST_SUFFIX)])
        return True

    parts = normalized.split()
    if not parts:
        return True

    head = parts[0].lower()

    if head in ("exit", "quit", "q", "退出"):
        return False
    if head in ("help", "h", "帮助"):
        print(HELP_TEXT)
        return True
    if head == "stocks":
        cmd_stocks()
        return True
    if head in ("dashboard", "看板"):
        as_html = len(parts) > 1 and parts[1].lower() in ("html", "网页")
        cmd_dashboard(as_html)
        return True
    # Bulk watchlist add, command-first phrasing: "watchlist add 603305 688582"
    if head == "watchlist" and len(parts) > 1 and parts[1].lower() == "add":
        cmd_add_watchlist(" ".join(parts[2:]))
        return True

    # Expect: <6-digit code> <command>
    code = parts[0]
    if not (code.isdigit() and len(code) == 6):
        print(f"[Kronos] Expected a 6-digit stock code, got '{code}'. Type 'help'.")
        return True

    if len(parts) == 1:
        cmd_status(code)
        return True

    cmd = parts[1].lower()
    if cmd in COMMANDS:
        COMMANDS[cmd](code)
    else:
        print(f"[Kronos] Unknown command '{cmd}'. Type 'help' for commands.")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1:
        dispatch(" ".join(sys.argv[1:]))
        return

    print("Kronos — A-share simulation CLI  (type 'help' for commands)")
    while True:
        try:
            raw = input("Kronos> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not dispatch(raw):
            break


if __name__ == "__main__":
    main()
