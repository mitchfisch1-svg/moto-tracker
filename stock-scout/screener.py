"""The scanner: builds the small-cap pond, pulls price history, computes
features, and lets the model rank everything.

Universe comes from Nasdaq's public screener endpoint (every US-listed
stock with price/mcap/volume in ONE request -- no API key). Price history
comes from Yahoo via yfinance, in chunks so we don't get throttled.
"""
import datetime as dt
import json
import math
import time

import requests
import yfinance as yf

import config
import db
from learner import get_weights, predict

UNIVERSE_CACHE = config.DATA_DIR / "universe.json"
BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def _num(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def fetch_universe(force=False):
    """All ~7000 US-listed stocks, cached for 12h."""
    config.DATA_DIR.mkdir(exist_ok=True)
    if UNIVERSE_CACHE.exists() and not force:
        if time.time() - UNIVERSE_CACHE.stat().st_mtime < 12 * 3600:
            return json.loads(UNIVERSE_CACHE.read_text())
    r = requests.get("https://api.nasdaq.com/api/screener/stocks",
                     params={"tableonly": "true", "limit": "25", "download": "true"},
                     headers=BROWSER_UA, timeout=60)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    UNIVERSE_CACHE.write_text(json.dumps(rows))
    return rows


def build_shortlist(conn, rows):
    """Apply the config filters and refresh the universe table."""
    short = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or any(ch in sym for ch in "^./ "):  # prefs, units, warrants
            continue
        price = _num(r.get("lastsale"))
        mcap = _num(r.get("marketCap"))
        vol = _num(r.get("volume"))
        if price is None or not mcap:
            continue
        if not (config.PRICE_MIN <= price <= config.PRICE_MAX):
            continue
        if not (config.MCAP_MIN <= mcap <= config.MCAP_MAX):
            continue
        if (vol or 0) < config.MIN_SHARE_VOLUME:
            continue
        short.append({"ticker": sym, "name": r.get("name") or "",
                      "sector": r.get("sector") or "?", "price": price,
                      "mcap": mcap, "volume": vol})
    short.sort(key=lambda s: -(s["price"] * s["volume"]))  # most liquid first
    conn.execute("DELETE FROM universe")
    conn.executemany(
        "INSERT INTO universe (ticker,name,sector,price,mcap,updated_at) VALUES (?,?,?,?,?,?)",
        [(s["ticker"], s["name"], s["sector"], s["price"], s["mcap"], db.now())
         for s in short])
    conn.commit()
    return short


def _frame_for(df, ticker):
    """Pull one ticker's OHLCV frame out of a (possibly multi-ticker) download."""
    try:
        sub = df[ticker]
        if "Close" in sub.columns:
            return sub
    except Exception:
        pass
    try:
        if "Close" in df.columns:
            return df
    except Exception:
        pass
    return None


def fetch_history(tickers, chunk=100):
    hist = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            df = yf.download(part, period="4mo", interval="1d", group_by="ticker",
                             auto_adjust=True, threads=True, progress=False)
        except Exception as e:
            print(f"  chunk {i // chunk + 1} failed: {e}")
            continue
        for t in part:
            sub = _frame_for(df, t)
            if sub is not None and sub["Close"].dropna().shape[0]:
                hist[t] = sub
        print(f"  history {min(i + chunk, len(tickers))}/{len(tickers)}...")
        time.sleep(1)  # be polite, avoid throttling
    return hist


def fetch_last_closes(tickers):
    """Latest close for each ticker. Used for grading picks and live P/L."""
    out = {}
    if not tickers:
        return out
    try:
        df = yf.download(list(tickers), period="5d", interval="1d",
                         group_by="ticker", auto_adjust=True, threads=True,
                         progress=False)
    except Exception:
        return out
    for t in tickers:
        sub = _frame_for(df, t)
        try:
            out[t] = float(sub["Close"].dropna().iloc[-1])
        except Exception:
            continue
    return out


def compute_features(sub):
    """Turn raw OHLCV history into the model's squashed [-1,1] features."""
    closes = sub["Close"].dropna()
    if len(closes) < 25:
        return None, None
    vols = sub["Volume"].reindex(closes.index).fillna(0)
    opens = sub["Open"].reindex(closes.index)
    c = float(closes.iloc[-1])
    prev_c = float(closes.iloc[-2])
    mom5 = c / float(closes.iloc[-6]) - 1
    mom20 = c / float(closes.iloc[-21]) - 1
    avg_vol = float(vols.iloc[-21:-1].mean()) or 1.0
    spike = float(vols.iloc[-1]) / avg_vol - 1
    lo, hi = float(closes.min()), float(closes.max())
    range_pos = 0.0 if hi == lo else (c - lo) / (hi - lo) * 2 - 1
    std20 = float(closes.pct_change().iloc[-20:].std() or 0)
    o = float(opens.iloc[-1])
    gap = (o / prev_c - 1) if not math.isnan(o) else 0.0
    dollar_vol = c * (float(vols.iloc[-21:].mean()) or 1.0)

    raw = {"price": c, "mom5": mom5 * 100, "mom20": mom20 * 100,
           "vol_spike": spike, "gap": gap * 100, "dollar_vol": dollar_vol}
    x = {
        "bias": 1.0,
        "mom5": math.tanh(mom5 / 0.10),
        "mom20": math.tanh(mom20 / 0.25),
        "vol_spike": math.tanh(spike / 2.0),
        "range_pos": range_pos,
        "volatility": math.tanh(std20 / 0.05),
        "gap": math.tanh(gap / 0.05),
        "dollar_vol": max(-1.0, min(1.0, (math.log10(max(dollar_vol, 1)) - 6.0) / 1.5)),
    }
    return x, raw


def auto_pick(conn, cands):
    """Turn the top-ranked candidates into open paper picks (bankroll-capped)."""
    picked = []
    for cnd in cands:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM picks WHERE status='open'").fetchone()["c"]
        if open_n >= config.MAX_POSITIONS:
            break
        dup = conn.execute(
            "SELECT 1 FROM picks WHERE status='open' AND ticker=?",
            (cnd["ticker"],)).fetchone()
        if dup:
            continue
        resolve_after = (dt.datetime.now(dt.timezone.utc)
                         + dt.timedelta(days=config.HORIZON_DAYS)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO picks (ticker,picked_at,entry_price,horizon_days,"
            "resolve_after,features,prob) VALUES (?,?,?,?,?,?,?)",
            (cnd["ticker"], db.now(), cnd["price"], config.HORIZON_DAYS,
             resolve_after, json.dumps(cnd["x"]), cnd["prob"]))
        picked.append(f"{cnd['ticker']} @ ${cnd['price']:.2f} (p={cnd['prob']:.2f})")
    conn.commit()
    return picked


def run_scan(limit=None, force_universe=False):
    conn = db.connect()
    rows = fetch_universe(force_universe)
    short = build_shortlist(conn, rows)
    if limit:
        short = short[:limit]
    print(f"Universe: {len(rows)} listed | pond after filters: {len(short)}")
    hist = fetch_history([s["ticker"] for s in short])
    print(f"Usable history for {len(hist)} tickers")

    w = get_weights(conn)
    cands = []
    for s in short:
        sub = hist.get(s["ticker"])
        if sub is None:
            continue
        x, raw = compute_features(sub)
        if x is None:
            continue
        x["catalyst"] = db.catalyst_for(conn, s["ticker"])
        cands.append({**s, "x": x, "raw": raw, "price": raw["price"],
                      "prob": predict(w, x)})
    cands.sort(key=lambda cnd: -cnd["prob"])

    cur = conn.execute("INSERT INTO scans (ran_at, universe_n, shortlist_n) VALUES (?,?,?)",
                       (db.now(), len(rows), len(short)))
    scan_id = cur.lastrowid
    for i, cnd in enumerate(cands[:40], 1):
        conn.execute(
            "INSERT INTO candidates (scan_id,ticker,name,sector,price,mcap,"
            "features,score,rank) VALUES (?,?,?,?,?,?,?,?,?)",
            (scan_id, cnd["ticker"], cnd["name"], cnd["sector"], cnd["price"],
             cnd["mcap"], json.dumps({"x": cnd["x"], "raw": cnd["raw"]}),
             cnd["prob"], i))
    conn.commit()

    print(f"\nTOP {min(15, len(cands))} -- scan #{scan_id}")
    print(f"{'#':>2} {'TICKER':<6} {'PRICE':>8} {'SCORE':>6} {'5D%':>7} "
          f"{'VOLx':>6} {'CAT':>5}  SECTOR")
    for i, cnd in enumerate(cands[:15], 1):
        print(f"{i:>2} {cnd['ticker']:<6} {cnd['price']:>8.2f} {cnd['prob']:>6.2f} "
              f"{cnd['raw']['mom5']:>6.1f}% {cnd['raw']['vol_spike'] + 1:>5.1f}x "
              f"{cnd['x']['catalyst']:>5.2f}  {cnd['sector'][:24]}")
    picked = auto_pick(conn, cands)
    if picked:
        print("\nNew paper picks: " + ", ".join(picked))
        import alerts
        alerts.push_picks(picked)
    return cands
