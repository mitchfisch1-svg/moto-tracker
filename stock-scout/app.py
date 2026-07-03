"""Web dashboard.  python scout.py web  ->  http://localhost:5050

Background thread re-pulls the news feeds every NEWS_REFRESH_MIN minutes
and grades any picks that hit their horizon. Scans run from the button
(they take a few minutes -- they hit Yahoo for ~hundreds of tickers).
"""
import datetime as dt
import json
import threading
import time
import traceback

from flask import Flask, redirect, render_template_string

import config
import db
import learner
import news
import screener

app = Flask(__name__)
JOBS = {}
STATUS = {"msg": "idle"}


def _run_job(name, fn):
    def wrap():
        STATUS["msg"] = f"{name} running..."
        try:
            fn()
            STATUS["msg"] = f"{name} finished {dt.datetime.now():%H:%M:%S}"
        except Exception as e:
            STATUS["msg"] = f"{name} failed: {e}"
            traceback.print_exc()
    if name in JOBS and JOBS[name].is_alive():
        return
    JOBS[name] = threading.Thread(target=wrap, daemon=True)
    JOBS[name].start()


def _background():
    while True:
        try:
            news.refresh_news(db.connect())
            learner.resolve_open_picks(db.connect())
        except Exception:
            traceback.print_exc()
        time.sleep(config.NEWS_REFRESH_MIN * 60)


def _mcap(v):
    if not v:
        return "?"
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


@app.post("/run/<job>")
def run_job(job):
    if job == "scan":
        _run_job("scan", lambda: screener.run_scan())
    elif job == "news":
        _run_job("news", lambda: news.refresh_news(db.connect()))
    elif job == "resolve":
        _run_job("resolve", lambda: learner.resolve_open_picks(db.connect()))
    return redirect("/")


@app.get("/")
def home():
    conn = db.connect()
    stats = db.pick_stats(conn)

    open_rows = conn.execute(
        "SELECT * FROM picks WHERE status='open' ORDER BY picked_at DESC").fetchall()
    live = screener.fetch_last_closes([p["ticker"] for p in open_rows])
    open_picks = []
    for p in open_rows:
        now_px = live.get(p["ticker"])
        pl = (now_px / p["entry_price"] - 1) * 100 if now_px else None
        open_picks.append({"ticker": p["ticker"], "entry": p["entry_price"],
                           "now": now_px, "pl": pl, "prob": p["prob"],
                           "picked": (p["picked_at"] or "")[:10],
                           "resolve": (p["resolve_after"] or "")[:10]})
    unreal = sum((pk["pl"] or 0) / 100 * config.POSITION_SIZE for pk in open_picks)

    scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    cands = []
    if scan:
        for c in conn.execute("SELECT * FROM candidates WHERE scan_id=? "
                              "ORDER BY rank LIMIT 15", (scan["id"],)).fetchall():
            f = json.loads(c["features"])
            cands.append({"rank": c["rank"], "ticker": c["ticker"],
                          "name": c["name"][:34], "sector": c["sector"][:20],
                          "price": c["price"], "mcap": _mcap(c["mcap"]),
                          "score": c["score"], "mom5": f["raw"]["mom5"],
                          "volx": f["raw"]["vol_spike"] + 1,
                          "cat": f["x"].get("catalyst", 0)})

    headlines = []
    for n in conn.execute(
            "SELECT n.*, u.ticker AS pond FROM news n "
            "LEFT JOIN universe u ON u.ticker = n.ticker "
            "ORDER BY n.id DESC LIMIT 25").fetchall():
        headlines.append({"src": n["source"], "ticker": n["ticker"],
                          "pond": n["pond"], "headline": n["headline"],
                          "url": n["url"], "score": n["score"],
                          "when": (n["published"] or n["fetched_at"] or "")[:16]})

    w = learner.get_weights(conn)
    weights = [{"name": k, "value": v, "pct": min(abs(v) / 2 * 100, 100)}
               for k, v in w.items()]
    n_updates = conn.execute("SELECT COUNT(*) c FROM weights").fetchone()["c"] - 1

    resolved = [{"ticker": p["ticker"], "ret": p["ret_pct"], "label": p["label"],
                 "when": (p["resolved_at"] or "")[:10]}
                for p in conn.execute("SELECT * FROM picks WHERE status='resolved' "
                                      "ORDER BY resolved_at DESC LIMIT 10").fetchall()]

    return render_template_string(
        PAGE, status=STATUS["msg"], stats=stats, open_picks=open_picks,
        unreal=unreal, cands=cands, headlines=headlines, weights=weights,
        n_updates=max(n_updates, 0), resolved=resolved, cfg=config,
        scan_when=(scan["ran_at"][:16].replace("T", " ") if scan else "never"))


@app.get("/news")
def all_news():
    conn = db.connect()
    headlines = []
    for n in conn.execute(
            "SELECT n.*, u.ticker AS pond FROM news n "
            "LEFT JOIN universe u ON u.ticker = n.ticker "
            "ORDER BY n.id DESC LIMIT 200").fetchall():
        headlines.append({"src": n["source"], "ticker": n["ticker"],
                          "pond": n["pond"], "headline": n["headline"],
                          "url": n["url"], "score": n["score"],
                          "when": (n["published"] or n["fetched_at"] or "")[:16]})
    return render_template_string(NEWS_PAGE, headlines=headlines)


CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;
--dim:#8b949e;--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
body{background:var(--bg);color:var(--text);
font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:18px 26px}
h1{font-size:20px;letter-spacing:2px;margin:0}
h2{font-size:12px;color:var(--dim);text-transform:uppercase;
letter-spacing:1px;margin:0 0 10px}
.panel{background:var(--panel);border:1px solid var(--border);
border-radius:8px;padding:14px 16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);text-align:left;font-weight:600;padding:4px 8px;
border-bottom:1px solid var(--border)}
td{padding:5px 8px;border-bottom:1px solid #21262d}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
.card{background:var(--panel);border:1px solid var(--border);
border-radius:8px;padding:10px 16px;min-width:105px}
.card .v{font-size:19px;font-weight:700}
.card .k{font-size:11px;color:var(--dim);text-transform:uppercase}
.pos{color:var(--green)}.neg{color:var(--red)}
a{color:var(--blue);text-decoration:none}
.btn{background:#21262d;border:1px solid var(--border);color:var(--text);
padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px}
.btn:hover{border-color:var(--blue)}
.badge{background:#1f6feb33;color:var(--blue);border-radius:4px;
padding:1px 6px;font-size:11px}
.bar{height:8px;border-radius:4px;display:inline-block;vertical-align:middle}
.dim{color:var(--dim)}.status{color:var(--amber);font-size:12px;margin-top:4px}
footer{color:var(--dim);font-size:11px;margin-top:18px;line-height:1.6}
.panel{overflow-x:auto}
@media(max-width:700px){
 body{padding:10px 12px}
 .grid{grid-template-columns:1fr}
 h1{font-size:16px;letter-spacing:1px}
 table{font-size:12px}
 td,th{padding:4px 5px;white-space:nowrap}
 .cards{gap:8px}
 .card{min-width:72px;padding:8px 10px;flex:1}
 .card .v{font-size:16px}
 .btn{padding:8px 10px;font-size:12px}
}
"""

# PWA head: lets iPhone "Add to Home Screen" install this as a fullscreen app.
HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Stock Scout">
"""

PAGE = """<!doctype html><html><head>""" + HEAD + """
<meta http-equiv="refresh" content="180"><title>Stock Scout</title>
<style>""" + CSS + """</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
 <h1>STOCK SCOUT <span class="dim" style="font-size:12px;letter-spacing:0">
   small-cap radar + self-tuning picker</span></h1>
 <div>
  <form style="display:inline" method="post" action="/run/scan"><button class="btn">&#9654; Run scan</button></form>
  <form style="display:inline" method="post" action="/run/news"><button class="btn">&#128240; Fetch news</button></form>
  <form style="display:inline" method="post" action="/run/resolve"><button class="btn">&#9878; Grade picks</button></form>
 </div>
</div>
<div class="status">status: {{status}} &nbsp;|&nbsp; last scan: {{scan_when}} &nbsp;|&nbsp; news auto-refreshes every {{cfg.NEWS_REFRESH_MIN}} min while this is running</div>

<div class="cards">
 <div class="card"><div class="v">${{'%.0f' % cfg.BANKROLL}}</div><div class="k">paper bankroll</div></div>
 <div class="card"><div class="v">{{stats.open}}/{{cfg.MAX_POSITIONS}}</div><div class="k">open picks</div></div>
 <div class="card"><div class="v {{'pos' if unreal>=0 else 'neg'}}">{{'%+.0f' % unreal}}$</div><div class="k">unrealized</div></div>
 <div class="card"><div class="v">{{stats.resolved}}</div><div class="k">graded picks</div></div>
 <div class="card"><div class="v">{{'%.0f' % stats.win_rate}}%</div><div class="k">win rate</div></div>
 <div class="card"><div class="v {{'pos' if stats.avg_ret>=0 else 'neg'}}">{{'%+.1f' % stats.avg_ret}}%</div><div class="k">avg return</div></div>
 <div class="card"><div class="v">{{n_updates}}</div><div class="k">model updates</div></div>
</div>

<div class="grid">
 <div class="panel"><h2>Open paper picks (${{'%.0f' % cfg.POSITION_SIZE}} each)</h2>
  {% if open_picks %}<table>
  <tr><th>Ticker</th><th>Entry</th><th>Now</th><th>P/L</th><th>Model p</th><th>Picked</th><th>Grades on</th></tr>
  {% for p in open_picks %}<tr>
   <td><b>{{p.ticker}}</b></td><td>${{'%.2f' % p.entry}}</td>
   <td>{{'$%.2f' % p.now if p.now else '?'}}</td>
   <td class="{{'pos' if p.pl is not none and p.pl>=0 else 'neg'}}">{{'%+.1f%%' % p.pl if p.pl is not none else '--'}}</td>
   <td>{{'%.2f' % p.prob}}</td><td class="dim">{{p.picked}}</td><td class="dim">{{p.resolve}}</td>
  </tr>{% endfor %}</table>
  {% else %}<div class="dim">No open picks yet -- hit "Run scan".</div>{% endif %}
 </div>

 <div class="panel"><h2>Latest scan -- top candidates</h2>
  {% if cands %}<table>
  <tr><th>#</th><th>Ticker</th><th>Price</th><th>MCap</th><th>Score</th><th>5d</th><th>Vol</th><th>News</th></tr>
  {% for c in cands %}<tr>
   <td class="dim">{{c.rank}}</td>
   <td><b>{{c.ticker}}</b> <span class="dim" style="font-size:11px">{{c.sector}}</span></td>
   <td>${{'%.2f' % c.price}}</td><td class="dim">{{c.mcap}}</td>
   <td><b>{{'%.2f' % c.score}}</b></td>
   <td class="{{'pos' if c.mom5>=0 else 'neg'}}">{{'%+.0f%%' % c.mom5}}</td>
   <td>{{'%.1fx' % c.volx}}</td>
   <td class="{{'pos' if c.cat>0 else ('neg' if c.cat<0 else 'dim')}}">{{'%.1f' % c.cat}}</td>
  </tr>{% endfor %}</table>
  {% else %}<div class="dim">No scan yet -- hit "Run scan" (takes a few minutes).</div>{% endif %}
 </div>
</div>

<div class="panel"><h2>News radar -- SEC filings + PR wires, before social media
  <a href="/news" style="float:right;text-transform:none">all news &rarr;</a></h2>
 {% if headlines %}<table>
 <tr><th style="width:110px">When</th><th style="width:90px">Source</th><th style="width:90px">Ticker</th><th style="width:50px">Score</th><th>Headline</th></tr>
 {% for n in headlines %}<tr>
  <td class="dim">{{n.when}}</td><td class="dim">{{n.src}}</td>
  <td>{% if n.ticker %}<b>{{n.ticker}}</b>{% if n.pond %} <span class="badge">pond</span>{% endif %}{% else %}<span class="dim">--</span>{% endif %}</td>
  <td class="{{'pos' if n.score>0 else ('neg' if n.score<0 else 'dim')}}">{{'%+.1f' % n.score}}</td>
  <td><a href="{{n.url}}" target="_blank">{{n.headline}}</a></td>
 </tr>{% endfor %}</table>
 {% else %}<div class="dim">Nothing yet -- hit "Fetch news".</div>{% endif %}
</div>

<div class="grid">
 <div class="panel"><h2>Model brain -- current feature weights</h2>
  <table>{% for w in weights %}<tr>
   <td style="width:110px">{{w.name}}</td>
   <td style="width:70px" class="{{'pos' if w.value>=0 else 'neg'}}">{{'%+.2f' % w.value}}</td>
   <td><span class="bar" style="width:{{w.pct}}%;background:{{'var(--green)' if w.value>=0 else 'var(--red)'}}"></span></td>
  </tr>{% endfor %}</table>
  <div class="dim" style="font-size:11px;margin-top:8px">
   Positive = the model has learned this trait predicts a +{{'%.0f' % (cfg.TARGET_RET*100)}}% pop
   within {{cfg.HORIZON_DAYS}} days. Weights move every time a pick is graded.</div>
 </div>

 <div class="panel"><h2>Recently graded picks</h2>
  {% if resolved %}<table>
  <tr><th>Ticker</th><th>Return</th><th>Verdict</th><th>Graded</th></tr>
  {% for p in resolved %}<tr>
   <td><b>{{p.ticker}}</b></td>
   <td class="{{'pos' if p.ret>=0 else 'neg'}}">{{'%+.1f%%' % p.ret}}</td>
   <td>{{'WIN' if p.label else 'MISS'}}</td><td class="dim">{{p.when}}</td>
  </tr>{% endfor %}</table>
  {% else %}<div class="dim">Nothing graded yet. Picks grade themselves
   {{cfg.HORIZON_DAYS}} days after entry -- that's when the model learns.</div>{% endif %}
 </div>
</div>

<footer><b>Paper trading only.</b> This is a research and learning tool, not financial
advice. Small caps can drop 50% on one filing; position sizing and stop losses are on
you. The model's opinion means little until it has graded 50+ picks.</footer>
</body></html>"""

NEWS_PAGE = """<!doctype html><html><head>""" + HEAD + """
<meta http-equiv="refresh" content="120"><title>Stock Scout -- News</title>
<style>""" + CSS + """</style></head><body>
<h1>NEWS RADAR <a href="/" style="font-size:13px;letter-spacing:0">&larr; dashboard</a></h1>
<div class="panel" style="margin-top:14px"><table>
<tr><th style="width:110px">When</th><th style="width:90px">Source</th><th style="width:90px">Ticker</th><th style="width:50px">Score</th><th>Headline</th></tr>
{% for n in headlines %}<tr>
 <td class="dim">{{n.when}}</td><td class="dim">{{n.src}}</td>
 <td>{% if n.ticker %}<b>{{n.ticker}}</b>{% if n.pond %} <span class="badge">pond</span>{% endif %}{% else %}<span class="dim">--</span>{% endif %}</td>
 <td class="{{'pos' if n.score>0 else ('neg' if n.score<0 else 'dim')}}">{{'%+.1f' % n.score}}</td>
 <td><a href="{{n.url}}" target="_blank">{{n.headline}}</a></td>
</tr>{% endfor %}</table></div>
</body></html>"""


def serve(port=None):
    threading.Thread(target=_background, daemon=True).start()
    port = port or config.WEB_PORT
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic sent; just picks the LAN interface
        lan_ip = s.getsockname()[0]
        s.close()
        print(f"On this PC:      http://localhost:{port}")
        print(f"On your iPhone:  http://{lan_ip}:{port}  (same Wi-Fi)")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    serve()
