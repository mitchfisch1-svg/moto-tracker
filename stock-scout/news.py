"""News radar: pulls from the sources the TikTok pumpers read hours
before they post.

Sources (all free, no API keys):
  - SEC EDGAR live 8-K feed  -> material corporate events, required by law,
    published the moment they're filed
  - GlobeNewswire / PR Newswire -> where company press releases actually drop

Every headline is keyword-scored: real catalysts (FDA, merger, contract,
uplisting) push a ticker's score up; the account-killers (offering,
going concern, reverse split) push it down. The net score feeds the
model's 'catalyst' feature and lights up the dashboard.
"""
import json
import re
import time

import feedparser
import requests

import config
import db

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

WIRE_FEEDS = [
    ("GlobeNewswire",
     "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
     "GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PRNewswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
]
EDGAR_8K = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type=8-K&company=&dateb=&owner=include&count=100&output=atom")
CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
CIK_CACHE = config.DATA_DIR / "cik_tickers.json"

# keyword -> weight. Simple substring match, lowercase.
# ("offering" also catches marketing fluff like "offering solutions" --
#  acceptable noise; dilution headlines matter too much to miss.)
CATALYSTS = {
    # the good stuff
    "fda approval": 1.0, "fda clearance": 0.9, "breakthrough designation": 0.8,
    "fast track": 0.6, "phase 3": 0.7, "phase 2": 0.5,
    "merger": 0.8, "acquisition": 0.7, "takeover": 0.8, "buyout": 0.8,
    "strategic alternatives": 0.6,
    "contract award": 0.7, "awarded": 0.5, "purchase order": 0.5,
    "partnership": 0.5, "collaboration": 0.4,
    "uplist": 0.7, "nasdaq listing": 0.6,
    "buyback": 0.5, "repurchase": 0.4, "special dividend": 0.6,
    "record revenue": 0.6, "raises guidance": 0.7, "raised guidance": 0.7,
    "beats estimates": 0.5, "insider buying": 0.6,
    # the stuff that vaporizes accounts
    "offering": -0.7, "dilution": -0.7, "reverse split": -0.6,
    "reverse stock split": -0.6, "going concern": -0.9, "delisting": -0.8,
    "deficiency": -0.5, "investigation": -0.7, "class action": -0.6,
    "subpoena": -0.7, "bankruptcy": -1.0, "chapter 11": -1.0,
    "restatement": -0.7, "halted": -0.6,
}

# 8-K item codes -> what they usually mean for the stock
EDGAR_ITEMS = {
    "1.01": ("material agreement", 0.5),
    "1.03": ("bankruptcy", -1.0),
    "2.01": ("deal completed", 0.6),
    "2.02": ("earnings out", 0.3),
    "3.01": ("delisting notice", -0.7),
    "3.02": ("unregistered share sales", -0.4),
    "5.02": ("exec departure/appointment", -0.2),
    "7.01": ("Reg FD disclosure", 0.2),
    "8.01": ("other event", 0.2),
}

TICKER_RE = re.compile(
    r"\((?:NASDAQ|NYSE(?:\s+American)?|AMEX|CBOE|OTC(?:QB|QX)?)\s*[:\-]\s*"
    r"([A-Za-z]{1,5})\)", re.I)


def score_text(text):
    t = text.lower()
    hits, score = [], 0.0
    for kw, wgt in CATALYSTS.items():
        if kw in t:
            hits.append(kw)
            score += wgt
    return max(-1.0, min(1.0, score)), hits


def cik_ticker_map():
    """SEC's official CIK -> ticker mapping, cached for a week."""
    config.DATA_DIR.mkdir(exist_ok=True)
    if CIK_CACHE.exists() and time.time() - CIK_CACHE.stat().st_mtime < 7 * 86400:
        raw = json.loads(CIK_CACHE.read_text())
    else:
        r = requests.get(CIK_MAP_URL,
                         headers={"User-Agent": config.SEC_USER_AGENT}, timeout=60)
        r.raise_for_status()
        raw = r.json()
        CIK_CACHE.write_text(json.dumps(raw))
    return {str(v["cik_str"]).zfill(10): v["ticker"] for v in raw.values()}


def _insert(conn, published, source, ticker, headline, url, score, hits):
    cur = conn.execute(
        "INSERT OR IGNORE INTO news (fetched_at,published,source,ticker,"
        "headline,url,score,hits) VALUES (?,?,?,?,?,?,?,?)",
        (db.now(), published or "", source, ticker, headline.strip()[:300],
         url, score, json.dumps(hits)))
    return cur.rowcount


def fetch_edgar(conn):
    """Live 8-K filings. This is as early as public information gets."""
    try:
        cikmap = cik_ticker_map()
        r = requests.get(EDGAR_8K, headers={"User-Agent": config.SEC_USER_AGENT},
                         timeout=30)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  EDGAR feed failed: {e}")
        return 0
    added, new_items = 0, []
    for e in feed.entries:
        title = e.get("title", "")
        summary = e.get("summary", "")
        m = re.search(r"\((\d{10})\)", title)
        ticker = cikmap.get(m.group(1)) if m else None
        score, hits = score_text(title + " " + summary)
        for code, (label, wgt) in EDGAR_ITEMS.items():
            if re.search(rf"item\s+{re.escape(code)}", summary, re.I):
                score += wgt
                hits.append(f"8-K item {code}: {label}")
        score = max(-1.0, min(1.0, score))
        company = title.split(" - ", 1)[-1] if " - " in title else title
        headline = f"8-K filed: {company}"
        n = _insert(conn, e.get("updated", ""), "SEC 8-K", ticker,
                    headline, e.get("link", ""), score, hits)
        if n:
            new_items.append({"ticker": ticker, "headline": headline,
                              "url": e.get("link", ""), "score": score})
        added += n
    conn.commit()
    return added, new_items


def fetch_wires(conn):
    """PR wires. Keep anything with a ticker symbol or a meaningful score."""
    added, new_items = 0, []
    for source, url in WIRE_FEEDS:
        try:
            feed = feedparser.parse(url, agent=BROWSER_UA)
        except Exception as e:
            print(f"  {source} failed: {e}")
            continue
        for e in feed.entries:
            title = e.get("title", "")
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:800]
            m = TICKER_RE.search(title + " " + summary)
            ticker = m.group(1).upper() if m else None
            score, hits = score_text(title + " " + summary)
            if ticker is None and abs(score) < 0.3:
                continue  # untickered fluff
            n = _insert(conn, e.get("published", ""), source, ticker,
                        title, e.get("link", ""), score, hits)
            if n:
                new_items.append({"ticker": ticker, "headline": title,
                                  "url": e.get("link", ""), "score": score})
            added += n
    conn.commit()
    return added, new_items


def refresh_news(conn=None):
    import alerts
    conn = conn or db.connect()
    a1, items1 = fetch_edgar(conn)
    a2, items2 = fetch_wires(conn)
    added = a1 + a2
    print(f"News refresh: {added} new item(s)")
    # buzz the phone for hot headlines on tickers in our pond
    if alerts.enabled():
        hot = [it for it in items1 + items2
               if it["ticker"] and abs(it["score"]) >= config.ALERT_MIN_SCORE
               and conn.execute("SELECT 1 FROM universe WHERE ticker=?",
                                (it["ticker"],)).fetchone()]
        if hot:
            alerts.push_news(sorted(hot, key=lambda it: -abs(it["score"])))
    hot = conn.execute(
        "SELECT * FROM news WHERE ticker IS NOT NULL ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for h in hot:
        flag = "+" if h["score"] > 0 else ("-" if h["score"] < 0 else " ")
        print(f"  [{flag}{abs(h['score']):.1f}] {h['ticker'] or '----':<5} "
              f"{h['headline'][:90]}")
    return added
