# Stock Scout

A small-cap radar and self-tuning stock picker. It scans every US-listed
stock, filters down to the cheap-but-liquid pond ($1–$15, $50M–$2B market
cap, real volume), scores each name with a transparent model, paper-trades
a $1,000 bankroll, and **grades its own picks a week later so the model
teaches itself** what's actually working. A news radar pulls SEC EDGAR
filings and the PR wires — the primary sources that social media
repackages hours later.

## Quick start

```
pip install -r requirements.txt
python scout.py scan          # first scan (few minutes), creates 5 paper picks
python scout.py web           # dashboard at http://localhost:5050
```

While the dashboard is running, news refreshes every 5 minutes and picks
grade themselves automatically when their week is up.

## On your iPhone

The dashboard is a PWA — it installs to your home screen like a real app,
no App Store needed:

1. Start the dashboard on the PC: `python scout.py web`. It prints an
   iPhone URL like `http://192.168.1.23:5050`.
2. The first time, Windows will ask to allow Python through the firewall —
   click **Allow** (private networks). If the phone still can't connect,
   run this once in an *admin* PowerShell:
   `netsh advfirewall firewall add rule name="Stock Scout" dir=in action=allow protocol=TCP localport=5050`
3. On the iPhone (same Wi-Fi), open that URL in Safari, tap **Share →
   Add to Home Screen**. Done — fullscreen app, candlestick icon.

The PC has to be on and running `scout.py web` for the app to load.
To use it away from home, install [Tailscale](https://tailscale.com) (free)
on both the PC and the iPhone and use the PC's Tailscale IP instead —
that's a private encrypted tunnel, never exposed to the open internet.

## Push alerts to your phone

Lock-screen notifications when something actually happens — a hot filing
on a pond ticker, new picks after a scan, picks getting graded:

1. Install the free **ntfy** app from the App Store.
2. In the app: **+ → Subscribe to topic** → enter the exact value of
   `NTFY_TOPIC` from `config.py`.
3. Test it: `python scout.py alert-test` — your phone should buzz.

Alerts fire while `scout.py web` is running (the background thread checks
news every 5 minutes). The topic name works like a password — anyone who
knows it can read your alerts, so don't post it anywhere. Rotate it by
generating a new random one in `config.py` and re-subscribing. Set it to
`""` to turn pushes off. Tune sensitivity with `ALERT_MIN_SCORE`.

## How the "self-teaching" works (no hand-waving)

1. Every scan computes 8 features per stock: 5-day and 20-day momentum,
   volume spike, position in the recent range, volatility, gap, dollar
   volume (liquidity), and a **news catalyst score**.
2. A logistic-regression model turns those into a probability score.
   Its starting weights are hand-set trader priors.
3. The top-scored names become paper picks. Seven days later each pick is
   graded: +5% or better = WIN, anything else = MISS.
4. Each graded pick nudges every weight toward whatever predicted the
   outcome (online SGD). You can watch the weights drift on the dashboard.

It is honest machine learning, but it is *slow* learning: the model's
opinion means very little until it has graded **50+ picks** (roughly 2–3
months of daily scans). Until then you're mostly trading the priors.

## The news radar

- **SEC EDGAR live 8-K feed** — material events, filed by law, timestamped
  the second they hit. Nothing on the internet is earlier than this.
- **GlobeNewswire + PR Newswire** — where the press releases actually drop.

Headlines are keyword-scored: FDA approvals, mergers, contract awards and
uplistings score positive; **offerings, going-concern language, reverse
splits and investigations score negative** — dilution is the #1 killer of
small-cap longs and most influencers never mention it. The `pond` badge
means the ticker passes your universe filters.

## House rules (30 years of scar tissue, condensed)

- **$200 max per position.** Five slots. When they're full, you wait.
- A stock under $1 is not "cheap", it's a company begging for delisting.
- If the headline says **"offering"**, close the tab. You are the exit
  liquidity.
- Volume spike *with* a real filing = signal. Volume spike with only a
  Discord/TikTok mention = you're late and you're the product.
- Paper trade until the model has 50 graded picks and a win rate you'd
  bet a steak dinner on. Then, if you go live, expect to lose the $1,000
  as tuition. Anyone who promises otherwise is selling something.

**This tool is for research and education. Nothing it outputs is financial
advice.**

## Files

| File | What it does |
|---|---|
| `config.py` | every knob: filters, bankroll, horizons, learning rate |
| `screener.py` | universe fetch, features, scoring, auto-picks |
| `learner.py` | the online-learning model + pick grading |
| `news.py` | EDGAR + wire feeds, catalyst keyword scoring |
| `app.py` | Flask dashboard |
| `scout.py` | CLI entry point |
| `data/` | SQLite DB + caches (auto-created, git-ignored) |

## Roadmap ideas

- Form 4 insider-buying clusters (the single best small-cap signal there is)
- Short-interest / days-to-cover feature
- Backtest harness so the model can learn from years of history, not weeks
- Windows toast / email alert when a pond ticker files a hot 8-K
