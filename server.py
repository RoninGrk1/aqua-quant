#!/usr/bin/env python3
"""Aqua-Quant — paper terminal on real free-API candles.

Market data: Yahoo Finance chart endpoint (no key). No synthetic prices.
Wallet: paper ledger only. This is not a broker and cannot move bank money.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "aqua_quant.sqlite"
STATIC = ROOT / "static"
HOST = os.environ.get("AQUA_HOST", "0.0.0.0")
PORT = int(os.environ.get("AQUA_PORT", "8765"))
SESSION_SECONDS = 4 * 3600 + 30 * 60
RISK_PCT = 0.0005  # 0.05%
WIN_GATE = 0.60
SCAN_SECONDS = 40
UA = "Mozilla/5.0 (compatible; AquaQuant/1.0; paper-terminal)"

UNIVERSE = [
    {"symbol": "^GSPC", "name": "S&P 500", "kind": "index"},
    {"symbol": "^DJI", "name": "Dow Jones", "kind": "index"},
    {"symbol": "^IXIC", "name": "Nasdaq Composite", "kind": "index"},
    {"symbol": "^RUT", "name": "Russell 2000", "kind": "index"},
    {"symbol": "^FTSE", "name": "FTSE 100", "kind": "index"},
    {"symbol": "^GDAXI", "name": "DAX", "kind": "index"},
    {"symbol": "^N225", "name": "Nikkei 225", "kind": "index"},
    {"symbol": "^HSI", "name": "Hang Seng", "kind": "index"},
    {"symbol": "AAPL", "name": "Apple", "kind": "stock"},
    {"symbol": "MSFT", "name": "Microsoft", "kind": "stock"},
    {"symbol": "NVDA", "name": "NVIDIA", "kind": "stock"},
    {"symbol": "AMZN", "name": "Amazon", "kind": "stock"},
    {"symbol": "GOOGL", "name": "Alphabet", "kind": "stock"},
    {"symbol": "META", "name": "Meta", "kind": "stock"},
    {"symbol": "TSLA", "name": "Tesla", "kind": "stock"},
    {"symbol": "JPM", "name": "JPMorgan", "kind": "stock"},
    {"symbol": "XOM", "name": "Exxon Mobil", "kind": "stock"},
    {"symbol": "UNH", "name": "UnitedHealth", "kind": "stock"},
]

DB_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
PRICE_CACHE: dict[str, dict] = {}
BAR_CACHE: dict[str, dict] = {}


def utc_now() -> int:
    return int(time.time())


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        conn = connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                started INTEGER NOT NULL,
                ends INTEGER NOT NULL,
                status TEXT NOT NULL,
                start_equity REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                run_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry REAL NOT NULL,
                stop REAL NOT NULL,
                target REAL NOT NULL,
                exit_px REAL,
                pnl REAL,
                status TEXT NOT NULL,
                opened INTEGER NOT NULL,
                closed INTEGER,
                risk_cash REAL NOT NULL,
                p_win REAL NOT NULL,
                reason TEXT,
                agents_json TEXT
            );
            """
        )
        conn.commit()
        conn.close()


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=18) as resp:
        return json.loads(resp.read().decode())


def yahoo_chart(symbol: str, interval: str, range_: str) -> dict:
    key = f"{symbol}|{interval}|{range_}"
    now = time.time()
    with CACHE_LOCK:
        hit = BAR_CACHE.get(key)
        if hit and now - hit["t"] < 25:
            return hit["data"]
    q = urllib.parse.urlencode({"interval": interval, "range": range_, "includePrePost": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
    raw = fetch_json(url)
    result = (raw.get("chart") or {}).get("result") or []
    if not result:
        err = (raw.get("chart") or {}).get("error") or {}
        raise RuntimeError(err.get("description") or f"No chart for {symbol}")
    block = result[0]
    meta = block.get("meta") or {}
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    stamps = block.get("timestamp") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    bars = []
    for i, ts in enumerate(stamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        bars.append(
            {
                "t": int(ts),
                "o": float(opens[i] or c),
                "h": float(highs[i] or c),
                "l": float(lows[i] or c),
                "c": float(c),
                "v": float(vols[i] or 0),
            }
        )
    data = {
        "symbol": meta.get("symbol") or symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "currency": meta.get("currency") or "USD",
        "exchange": meta.get("exchangeName") or "",
        "price": float(meta.get("regularMarketPrice") or (bars[-1]["c"] if bars else 0)),
        "prev": float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0),
        "bars": bars,
        "asof": utc_now(),
        "source": "Yahoo Finance chart API",
    }
    with CACHE_LOCK:
        BAR_CACHE[key] = {"t": now, "data": data}
        PRICE_CACHE[symbol] = {"price": data["price"], "prev": data["prev"], "t": now, "currency": data["currency"]}
    return data


def resample_4h(bars_1h: list[dict]) -> list[dict]:
    buckets: dict[int, dict] = {}
    order: list[int] = []
    for bar in bars_1h:
        key = bar["t"] // (4 * 3600)
        if key not in buckets:
            buckets[key] = {"t": bar["t"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}
            order.append(key)
        else:
            b = buckets[key]
            b["h"] = max(b["h"], bar["h"])
            b["l"] = min(b["l"], bar["l"])
            b["c"] = bar["c"]
            b["v"] += bar["v"]
            b["t"] = bar["t"]
    return [buckets[k] for k in order]


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - (100 / (1 + rs))


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-n:]
    return sum(window) / len(window)


def macd_hist(values: list[float]) -> float | None:
    if len(values) < 35:
        return None
    e12 = ema(values, 12)
    e26 = ema(values, 26)
    line = [a - b for a, b in zip(e12, e26)]
    signal = ema(line, 9)
    return line[-1] - signal[-1]


def agent(name: str, seat: str, bias: str, score: float, note: str) -> dict:
    return {
        "name": name,
        "seat": seat,
        "bias": bias,
        "score": round(max(0.0, min(1.0, score)), 3),
        "note": note,
    }


def analyse(symbol: str) -> dict:
    d30 = yahoo_chart(symbol, "30m", "10d")
    d1h = yahoo_chart(symbol, "60m", "60d")
    bars = d30["bars"]
    h4 = resample_4h(d1h["bars"])
    if len(bars) < 40 or len(h4) < 20:
        raise RuntimeError(f"Not enough real bars for {symbol}")
    closes = [b["c"] for b in bars]
    h4c = [b["c"] for b in h4]
    price = d30["price"] or closes[-1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    h_e20 = ema(h4c, 20)
    h_e8 = ema(h4c, 8)
    rsi14 = rsi(closes, 14)
    rsi_h = rsi(h4c, 14)
    hist = macd_hist(closes)
    a = atr(bars, 14)
    a_h = atr(h4, 14)
    vols = [b["v"] for b in bars if b["v"] > 0]
    vol_now = vols[-1] if vols else 0
    vol_avg = sum(vols[-21:-1]) / max(1, len(vols[-21:-1])) if len(vols) > 2 else 0
    window = bars[-20:]
    hi = max(b["h"] for b in window[:-1])
    lo = min(b["l"] for b in window[:-1])
    mid = sum(closes[-20:]) / 20
    sd = (sum((c - mid) ** 2 for c in closes[-20:]) / 20) ** 0.5
    upper = mid + 2 * sd
    lower = mid - 2 * sd
    last = bars[-1]

    agents = []
    # 1 Tide — 4h trend
    if h4c[-1] > h_e20[-1] and h_e8[-1] > h_e20[-1]:
        agents.append(agent("Tide", "4h trend", "long", 0.78, f"4h close {h4c[-1]:.2f} above EMA20 {h_e20[-1]:.2f}"))
    elif h4c[-1] < h_e20[-1] and h_e8[-1] < h_e20[-1]:
        agents.append(agent("Tide", "4h trend", "short", 0.78, f"4h close {h4c[-1]:.2f} below EMA20 {h_e20[-1]:.2f}"))
    else:
        agents.append(agent("Tide", "4h trend", "flat", 0.4, "4h EMAs mixed — no trend seat"))
    # 2 Current — 30m alignment
    if closes[-1] > e20[-1] > e50[-1]:
        agents.append(agent("Current", "30m alignment", "long", 0.74, f"30m stack up EMA20 {e20[-1]:.2f} / EMA50 {e50[-1]:.2f}"))
    elif closes[-1] < e20[-1] < e50[-1]:
        agents.append(agent("Current", "30m alignment", "short", 0.74, f"30m stack down EMA20 {e20[-1]:.2f} / EMA50 {e50[-1]:.2f}"))
    else:
        agents.append(agent("Current", "30m alignment", "flat", 0.35, "30m EMAs not stacked"))
    # 3 Pulse — RSI
    if rsi14 is None:
        agents.append(agent("Pulse", "RSI", "flat", 0.2, "RSI unavailable"))
    elif 52 <= rsi14 <= 72:
        agents.append(agent("Pulse", "RSI", "long", 0.66, f"30m RSI {rsi14:.1f} in long impulse band"))
    elif 28 <= rsi14 <= 48:
        agents.append(agent("Pulse", "RSI", "short", 0.66, f"30m RSI {rsi14:.1f} in short impulse band"))
    else:
        agents.append(agent("Pulse", "RSI", "flat", 0.3, f"30m RSI {rsi14:.1f} outside impulse band"))
    # 4 Drift — MACD
    if hist is None:
        agents.append(agent("Drift", "MACD", "flat", 0.2, "MACD unavailable"))
    elif hist > 0:
        agents.append(agent("Drift", "MACD", "long", 0.64, f"30m MACD hist {hist:.4f} positive"))
    else:
        agents.append(agent("Drift", "MACD", "short", 0.64, f"30m MACD hist {hist:.4f} negative"))
    # 5 Swell — Bollinger location
    if price > upper:
        agents.append(agent("Swell", "Bollinger", "short", 0.55, f"Price above upper band {upper:.2f} — stretched"))
    elif price < lower:
        agents.append(agent("Swell", "Bollinger", "long", 0.55, f"Price below lower band {lower:.2f} — stretched"))
    elif price >= mid:
        agents.append(agent("Swell", "Bollinger", "long", 0.58, f"Price above 20-bar mid {mid:.2f}"))
    else:
        agents.append(agent("Swell", "Bollinger", "short", 0.58, f"Price below 20-bar mid {mid:.2f}"))
    # 6 Depth — ATR regime
    if a and price:
        atr_pct = a / price
        if 0.0015 <= atr_pct <= 0.02:
            agents.append(agent("Depth", "ATR regime", "long" if closes[-1] > e20[-1] else "short", 0.6, f"ATR {a:.2f} ({atr_pct*100:.2f}% of price) tradable"))
        else:
            agents.append(agent("Depth", "ATR regime", "flat", 0.25, f"ATR {a:.2f} ({atr_pct*100:.2f}%) outside tradable band"))
    else:
        agents.append(agent("Depth", "ATR regime", "flat", 0.2, "ATR unavailable"))
    # 7 Flow — volume
    if vol_avg <= 0:
        agents.append(agent("Flow", "Volume", "flat", 0.3, "Volume missing on this feed — seat abstains"))
    elif vol_now >= vol_avg * 1.05:
        bias = "long" if closes[-1] >= closes[-2] else "short"
        agents.append(agent("Flow", "Volume", bias, 0.67, f"Last 30m volume {vol_now:.0f} vs avg {vol_avg:.0f}"))
    else:
        agents.append(agent("Flow", "Volume", "flat", 0.34, f"Volume {vol_now:.0f} below average {vol_avg:.0f}"))
    # 8 Shelf — 20-bar break
    if last["c"] > hi:
        agents.append(agent("Shelf", "Breakout", "long", 0.8, f"30m close through 20-bar high {hi:.2f}"))
    elif last["c"] < lo:
        agents.append(agent("Shelf", "Breakout", "short", 0.8, f"30m close through 20-bar low {lo:.2f}"))
    else:
        agents.append(agent("Shelf", "Breakout", "flat", 0.32, f"Inside range {lo:.2f}–{hi:.2f}"))
    # 9 Revert — only if 4h RSI stretched and 30m turns
    if rsi_h is not None and rsi_h >= 72 and rsi14 is not None and rsi14 < 60:
        agents.append(agent("Revert", "Mean revert", "short", 0.62, f"4h RSI {rsi_h:.1f} stretched, 30m RSI {rsi14:.1f} cooling"))
    elif rsi_h is not None and rsi_h <= 28 and rsi14 is not None and rsi14 > 40:
        agents.append(agent("Revert", "Mean revert", "long", 0.62, f"4h RSI {rsi_h:.1f} stretched, 30m RSI {rsi14:.1f} lifting"))
    else:
        agents.append(agent("Revert", "Mean revert", "flat", 0.28, "No stretch/turn pair"))
    # 10 Lock — filled after direction vote
    longs = [a for a in agents if a["bias"] == "long"]
    shorts = [a for a in agents if a["bias"] == "short"]
    if len(longs) > len(shorts):
        side = "long"
        aligned = longs
    elif len(shorts) > len(longs):
        side = "short"
        aligned = shorts
    else:
        side = "flat"
        aligned = []
    conf = (sum(a["score"] for a in aligned) / len(aligned)) if aligned else 0
    # Confluence score used as the gate. Not a promised future win rate.
    p_win = 0.38 + 0.055 * max(0, len(aligned) - 3) + 0.12 * (conf - 0.5)
    p_win = max(0.0, min(0.86, p_win))
    h4_bias = agents[0]["bias"]
    m30_bias = agents[1]["bias"]
    rr_ok = a is not None and a > 0
    lock_ok = (
        side in ("long", "short")
        and len(aligned) >= 6
        and p_win >= WIN_GATE
        and h4_bias == side
        and m30_bias == side
        and rr_ok
    )
    lock_note = (
        f"Aligned {len(aligned)}/9 · confluence {p_win*100:.1f}% · 4h {h4_bias} · 30m {m30_bias}"
    )
    if lock_ok:
        agents.append(agent("Lock", "Risk gate", side, p_win, lock_note + " — cleared"))
    else:
        agents.append(agent("Lock", "Risk gate", "flat", p_win, lock_note + " — blocked"))
    stop_dist = (1.2 * a) if a else None
    return {
        "symbol": d30["symbol"],
        "name": d30["name"],
        "currency": d30["currency"],
        "exchange": d30["exchange"],
        "price": price,
        "prev": d30["prev"],
        "asof": d30["asof"],
        "source": d30["source"],
        "change_pct": ((price - d30["prev"]) / d30["prev"] * 100) if d30["prev"] else None,
        "atr": a,
        "atr_4h": a_h,
        "rsi": rsi14,
        "rsi_4h": rsi_h,
        "side": side if lock_ok else "flat",
        "raw_side": side,
        "p_win": round(p_win, 4),
        "aligned": len(aligned),
        "cleared": lock_ok,
        "stop_dist": stop_dist,
        "agents": agents,
        "bars_30m": bars[-80:],
        "bars_4h": h4[-60:],
    }


def quote_map() -> list[dict]:
    rows = []
    for item in UNIVERSE:
        try:
            data = yahoo_chart(item["symbol"], "30m", "5d")
            px = data["price"]
            prev = data["prev"]
            rows.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "price": px,
                    "prev": prev,
                    "change_pct": ((px - prev) / prev * 100) if prev else None,
                    "currency": data["currency"],
                    "asof": data["asof"],
                    "source": data["source"],
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 — surface feed errors, never invent a price
            rows.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "price": None,
                    "prev": None,
                    "change_pct": None,
                    "currency": None,
                    "asof": utc_now(),
                    "source": "Yahoo Finance chart API",
                    "error": str(exc),
                }
            )
    return rows


def cash_of(conn: sqlite3.Connection, user_id: int) -> float:
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM ledger WHERE user_id = ?", (user_id,)).fetchone()
    return float(row["c"])


def open_trades(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trades WHERE user_id = ? AND status = 'open' ORDER BY opened DESC",
        (user_id,),
    ).fetchall()


def reserved_margin(conn: sqlite3.Connection, user_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(ABS(qty) * entry), 0) AS m FROM trades WHERE user_id = ? AND status = 'open'",
        (user_id,),
    ).fetchone()
    return float(row["m"])


def mark_trade(trade: sqlite3.Row, price: float) -> float:
    if trade["side"] == "long":
        return (price - trade["entry"]) * trade["qty"]
    return (trade["entry"] - price) * trade["qty"]


def latest_price(symbol: str) -> float | None:
    with CACHE_LOCK:
        hit = PRICE_CACHE.get(symbol)
        if hit and time.time() - hit["t"] < 20:
            return hit["price"]
    try:
        return yahoo_chart(symbol, "30m", "5d")["price"]
    except Exception:
        return None


def active_run(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE user_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def user_state(conn: sqlite3.Connection, user: sqlite3.Row) -> dict:
    uid = user["id"]
    cash = cash_of(conn, uid)
    opens = open_trades(conn, uid)
    unreal = 0.0
    positions = []
    for tr in opens:
        px = latest_price(tr["symbol"])
        upnl = mark_trade(tr, px) if px else None
        if upnl is not None:
            unreal += upnl
        positions.append(
            {
                "id": tr["id"],
                "symbol": tr["symbol"],
                "side": tr["side"],
                "qty": tr["qty"],
                "entry": tr["entry"],
                "stop": tr["stop"],
                "target": tr["target"],
                "price": px,
                "upnl": upnl,
                "p_win": tr["p_win"],
                "opened": iso(tr["opened"]),
                "risk_cash": tr["risk_cash"],
            }
        )
    equity = cash + unreal
    run = active_run(conn, uid)
    now = utc_now()
    session = None
    if run:
        if now >= run["ends"]:
            conn.execute("UPDATE runs SET status = 'ended' WHERE id = ?", (run["id"],))
            conn.commit()
        else:
            session = {
                "id": run["id"],
                "started": iso(run["started"]),
                "ends": iso(run["ends"]),
                "remaining": run["ends"] - now,
                "start_equity": run["start_equity"],
                "status": "running",
            }
    closed = conn.execute(
        "SELECT * FROM trades WHERE user_id = ? AND status = 'closed' ORDER BY closed DESC LIMIT 40",
        (uid,),
    ).fetchall()
    ledger = conn.execute(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 30",
        (uid,),
    ).fetchall()
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    return {
        "user": {"id": uid, "email": user["email"], "username": user["username"]},
        "wallet": {
            "cash": cash,
            "unrealized": unreal,
            "equity": equity,
            "reserved": reserved_margin(conn, uid),
            "withdrawable": max(0.0, cash - reserved_margin(conn, uid)),
            "currency": "USD",
            "mode": "paper",
        },
        "session": session,
        "positions": positions,
        "trades": [
            {
                "id": t["id"],
                "symbol": t["symbol"],
                "side": t["side"],
                "qty": t["qty"],
                "entry": t["entry"],
                "exit": t["exit_px"],
                "pnl": t["pnl"],
                "p_win": t["p_win"],
                "opened": iso(t["opened"]),
                "closed": iso(t["closed"]),
                "reason": t["reason"],
            }
            for t in closed
        ],
        "stats": {
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "realized": sum(t["pnl"] or 0 for t in closed),
        },
        "ledger": [
            {"id": r["id"], "kind": r["kind"], "amount": r["amount"], "note": r["note"], "ts": iso(r["ts"])}
            for r in ledger
        ],
    }


def close_trade(conn: sqlite3.Connection, trade: sqlite3.Row, price: float, reason: str) -> None:
    pnl = mark_trade(trade, price)
    conn.execute(
        "UPDATE trades SET status = 'closed', exit_px = ?, pnl = ?, closed = ?, reason = ? WHERE id = ?",
        (price, pnl, utc_now(), reason, trade["id"]),
    )
    conn.execute(
        "INSERT INTO ledger (user_id, kind, amount, note, ts) VALUES (?, 'pnl', ?, ?, ?)",
        (trade["user_id"], pnl, f"{trade['symbol']} {trade['side']} {reason}", utc_now()),
    )


def manage_open(conn: sqlite3.Connection, user_id: int) -> None:
    for tr in open_trades(conn, user_id):
        try:
            data = yahoo_chart(tr["symbol"], "30m", "5d")
        except Exception:
            continue
        px = data["price"]
        last = data["bars"][-1] if data["bars"] else None
        hi = last["h"] if last else px
        lo = last["l"] if last else px
        if tr["side"] == "long":
            if lo <= tr["stop"]:
                close_trade(conn, tr, tr["stop"], "stop")
            elif hi >= tr["target"]:
                close_trade(conn, tr, tr["target"], "target")
        else:
            if hi >= tr["stop"]:
                close_trade(conn, tr, tr["stop"], "stop")
            elif lo <= tr["target"]:
                close_trade(conn, tr, tr["target"], "target")


def maybe_open(conn: sqlite3.Connection, user_id: int, run_id: int) -> None:
    opens = open_trades(conn, user_id)
    if len(opens) >= 3:
        return
    held = {t["symbol"] for t in opens}
    cash = cash_of(conn, user_id)
    unreal = 0.0
    for tr in opens:
        px = latest_price(tr["symbol"])
        if px:
            unreal += mark_trade(tr, px)
    equity = cash + unreal
    if equity < 1000:
        return
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run and equity <= run["start_equity"] * 0.99:
        return
    candidates = []
    for item in UNIVERSE:
        if item["symbol"] in held:
            continue
        try:
            view = analyse(item["symbol"])
        except Exception:
            continue
        if view["cleared"] and view["stop_dist"]:
            candidates.append(view)
    if not candidates:
        return
    candidates.sort(key=lambda v: (v["aligned"], v["p_win"]), reverse=True)
    view = candidates[0]
    risk_cash = equity * RISK_PCT
    stop_dist = view["stop_dist"]
    qty = risk_cash / stop_dist
    notional = qty * view["price"]
    free = cash - reserved_margin(conn, user_id)
    if notional > free * 0.25 or notional < 1:
        return
    if view["side"] == "long":
        stop = view["price"] - stop_dist
        target = view["price"] + stop_dist * 1.8
    else:
        stop = view["price"] + stop_dist
        target = view["price"] - stop_dist * 1.8
    conn.execute(
        """
        INSERT INTO trades (
            user_id, run_id, symbol, side, qty, entry, stop, target, status,
            opened, risk_cash, p_win, reason, agents_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            run_id,
            view["symbol"],
            view["side"],
            qty,
            view["price"],
            stop,
            target,
            utc_now(),
            risk_cash,
            view["p_win"],
            f"Desk cleared {view['aligned']}/9 at {view['p_win']*100:.1f}% confluence",
            json.dumps(view["agents"]),
        ),
    )


def scanner_loop() -> None:
    while True:
        time.sleep(SCAN_SECONDS)
        try:
            with DB_LOCK:
                conn = connect()
                runs = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
                now = utc_now()
                for run in runs:
                    if now >= run["ends"]:
                        conn.execute("UPDATE runs SET status = 'ended' WHERE id = ?", (run["id"],))
                        continue
                    manage_open(conn, run["user_id"])
                    maybe_open(conn, run["user_id"], run["id"])
                conn.commit()
                conn.close()
        except Exception:
            continue


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("aq_session="):
                return part.split("=", 1)[1]
        return None

    def _user(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        token = self._cookie_token()
        if not token:
            return None
        row = conn.execute(
            """
            SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires > ?
            """,
            (token, utc_now()),
        ).fetchone()
        return row

    def _send(self, code: int, payload: dict | None = None, body: bytes | None = None, content: str = "application/json", extra: dict | None = None) -> None:
        data = body if body is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = (STATIC / "index.html").read_bytes()
            self._send(200, body=data, content="text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "name": "Aqua-Quant", "data": "Yahoo Finance chart API"})
            return
        if path == "/api/market":
            self._send(200, {"quotes": quote_map(), "source": "Yahoo Finance chart API"})
            return
        if path.startswith("/api/analyse/"):
            symbol = urllib.parse.unquote(path.split("/api/analyse/", 1)[1])
            try:
                self._send(200, analyse(symbol))
            except Exception as exc:  # noqa: BLE001
                self._send(502, {"error": str(exc), "symbol": symbol})
            return
        if path == "/api/state":
            with DB_LOCK:
                conn = connect()
                user = self._user(conn)
                if not user:
                    conn.close()
                    self._send(401, {"error": "Sign in required"})
                    return
                state = user_state(conn, user)
                conn.close()
            self._send(200, state)
            return
        self._send(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception:
            self._send(400, {"error": "Bad JSON"})
            return
        if path == "/api/register":
            email = (body.get("email") or "").strip().lower()
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if "@" not in email or len(username) < 3 or len(password) < 8:
                self._send(400, {"error": "Email, username (3+), and password (8+) are required"})
                return
            salt = secrets.token_hex(16)
            digest = hash_password(password, salt)
            with DB_LOCK:
                conn = connect()
                try:
                    conn.execute(
                        "INSERT INTO users (email, username, password_hash, salt, created) VALUES (?, ?, ?, ?, ?)",
                        (email, username, digest, salt, utc_now()),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    conn.close()
                    self._send(409, {"error": "Email or username already registered"})
                    return
                conn.close()
            self._send(200, {"ok": True})
            return
        if path == "/api/login":
            email = (body.get("email") or "").strip().lower()
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            with DB_LOCK:
                conn = connect()
                user = conn.execute(
                    "SELECT * FROM users WHERE email = ? AND username = ?",
                    (email, username),
                ).fetchone()
                if not user or hash_password(password, user["salt"]) != user["password_hash"]:
                    conn.close()
                    self._send(401, {"error": "Email, username, and password do not match"})
                    return
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO sessions (token, user_id, expires) VALUES (?, ?, ?)",
                    (token, user["id"], utc_now() + 7 * 86400),
                )
                conn.commit()
                conn.close()
            self._send(
                200,
                {"ok": True, "username": username},
                extra={"Set-Cookie": f"aq_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=604800"},
            )
            return
        if path == "/api/logout":
            token = self._cookie_token()
            with DB_LOCK:
                conn = connect()
                if token:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                    conn.commit()
                conn.close()
            self._send(200, {"ok": True}, extra={"Set-Cookie": "aq_session=; HttpOnly; Path=/; Max-Age=0"})
            return
        with DB_LOCK:
            conn = connect()
            user = self._user(conn)
            if not user:
                conn.close()
                self._send(401, {"error": "Sign in required"})
                return
            uid = user["id"]
            if path == "/api/wallet/deposit":
                amount = float(body.get("amount") or 0)
                if amount <= 0 or amount > 5_000_000:
                    conn.close()
                    self._send(400, {"error": "Deposit must be between 0 and 5,000,000 paper USD"})
                    return
                conn.execute(
                    "INSERT INTO ledger (user_id, kind, amount, note, ts) VALUES (?, 'deposit', ?, ?, ?)",
                    (uid, amount, "Paper deposit", utc_now()),
                )
                conn.commit()
                state = user_state(conn, user)
                conn.close()
                self._send(200, state)
                return
            if path == "/api/wallet/withdraw":
                amount = float(body.get("amount") or 0)
                free = cash_of(conn, uid) - reserved_margin(conn, uid)
                if amount <= 0 or amount > free + 1e-9:
                    conn.close()
                    self._send(400, {"error": f"Withdrawable paper cash is {free:.2f}"})
                    return
                conn.execute(
                    "INSERT INTO ledger (user_id, kind, amount, note, ts) VALUES (?, 'withdraw', ?, ?, ?)",
                    (uid, -amount, "Paper withdrawal", utc_now()),
                )
                conn.commit()
                state = user_state(conn, user)
                conn.close()
                self._send(200, state)
                return
            if path == "/api/session/start":
                existing = active_run(conn, uid)
                if existing and utc_now() < existing["ends"]:
                    conn.close()
                    self._send(409, {"error": "A 4h 30m session is already running"})
                    return
                manage_open(conn, uid)
                cash = cash_of(conn, uid)
                if cash < 1000:
                    conn.close()
                    self._send(400, {"error": "Deposit at least 1,000 paper USD before arming the desk"})
                    return
                now = utc_now()
                conn.execute(
                    "INSERT INTO runs (user_id, started, ends, status, start_equity) VALUES (?, ?, ?, 'running', ?)",
                    (uid, now, now + SESSION_SECONDS, cash),
                )
                conn.commit()
                state = user_state(conn, user)
                conn.close()
                self._send(200, state)
                return
            if path == "/api/session/stop":
                conn.execute("UPDATE runs SET status = 'stopped' WHERE user_id = ? AND status = 'running'", (uid,))
                conn.commit()
                state = user_state(conn, user)
                conn.close()
                self._send(200, state)
                return
            conn.close()
        self._send(404, {"error": "Not found"})


def main() -> None:
    init_db()
    threading.Thread(target=scanner_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Aqua-Quant listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
