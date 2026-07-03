"""Push notifications to your iPhone via ntfy (https://ntfy.sh).

No account, no API key: the topic name IS the password. Anyone who
guesses it can read your alerts, so it's a long random string. Install
the free ntfy app on the phone and subscribe to config.NTFY_TOPIC.
"""
import requests

import config


def enabled():
    return bool(config.NTFY_TOPIC)


def send(title, message, priority="default", tags=None, click=None):
    if not enabled():
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    try:
        r = requests.post(f"https://ntfy.sh/{config.NTFY_TOPIC}",
                          data=message.encode("utf-8"), headers=headers,
                          timeout=15)
        return r.ok
    except Exception as e:
        print(f"  ntfy push failed: {e}")
        return False


def push_news(items):
    """items: hot news on pond tickers, already filtered and sorted."""
    for it in items[:config.ALERT_MAX_PER_REFRESH]:
        good = it["score"] > 0
        send(f"{it['ticker']} catalyst {it['score']:+.1f}",
             it["headline"],
             priority="high" if abs(it["score"]) >= 0.8 else "default",
             tags=["chart_with_upwards_trend"] if good else ["rotating_light"],
             click=it.get("url") or None)


def push_picks(picked):
    if picked:
        send(f"Scan done: {len(picked)} new paper pick(s)",
             "\n".join(picked), tags=["mag"])


def push_graded(results):
    """results: list of (ticker, ret_pct, label) from resolve_open_picks."""
    if not results:
        return
    wins = sum(1 for _, _, label in results if label)
    lines = [f"{t} {ret:+.1f}% {'WIN' if label else 'MISS'}"
             for t, ret, label in results]
    send(f"Graded {len(results)} pick(s): {wins} win(s)",
         "\n".join(lines), tags=["scales"])
