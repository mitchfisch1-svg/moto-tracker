"""The self-teaching part: an online logistic-regression scorer.

Every pick Stock Scout makes gets graded HORIZON_DAYS later: did it gain
TARGET_RET or not? Each graded pick nudges the feature weights toward
whatever actually worked (classic online SGD). No black box -- you can
watch every weight move on the dashboard and see WHY it likes a stock.

It starts from hand-set priors (volume spike + a real catalyst beats
everything; illiquidity and dilution kill) and drifts toward whatever
the market is currently paying for.
"""
import json
import math

import config
import db

FEATURES = ["bias", "mom5", "mom20", "vol_spike", "range_pos",
            "volatility", "gap", "dollar_vol", "catalyst"]

# Starting opinions. The learner overwrites these with experience.
PRIORS = {
    "bias": -1.2,        # default skepticism: most setups fail
    "mom5": 0.4,         # 5-day momentum
    "mom20": 0.2,        # 20-day trend
    "vol_spike": 0.6,    # unusual volume = someone knows something
    "range_pos": 0.2,    # near highs beats knife-catching near lows
    "volatility": -0.3,  # chop for chop's sake is a tax
    "gap": 0.2,          # gapping up on news
    "dollar_vol": 0.3,   # liquidity: you need a door to exit through
    "catalyst": 0.8,     # real news is the whole ballgame
}


def sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def predict(weights, feats):
    return sigmoid(sum(weights[f] * feats.get(f, 0.0) for f in FEATURES))


def get_weights(conn):
    row = conn.execute("SELECT weights FROM weights ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return json.loads(row["weights"])
    save_weights(conn, dict(PRIORS), "initial priors")
    return dict(PRIORS)


def save_weights(conn, w, note=""):
    conn.execute("INSERT INTO weights (updated_at, weights, note) VALUES (?,?,?)",
                 (db.now(), json.dumps(w), note))
    conn.commit()


def learn_from(conn, graded):
    """graded: list of (feature_dict, label). One SGD step per graded pick."""
    w = get_weights(conn)
    for feats, label in graded:
        p = predict(w, feats)
        for f in FEATURES:
            w[f] += config.LEARNING_RATE * (label - p) * feats.get(f, 0.0)
    save_weights(conn, w, f"learned from {len(graded)} graded pick(s)")
    return w


def resolve_open_picks(conn):
    """Grade every open pick past its resolve date, then update the model."""
    from screener import fetch_last_closes  # runtime import avoids a cycle
    due = conn.execute(
        "SELECT * FROM picks WHERE status='open' AND resolve_after <= ?",
        (db.now(),)).fetchall()
    if not due:
        return []
    closes = fetch_last_closes(sorted({r["ticker"] for r in due}))
    graded, results = [], []
    for r in due:
        close = closes.get(r["ticker"])
        if close is None:
            continue  # halted/delisted: stays open, deal with it by hand
        ret = close / r["entry_price"] - 1.0
        label = 1 if ret >= config.TARGET_RET else 0
        conn.execute(
            "UPDATE picks SET status='resolved', exit_price=?, ret_pct=?, "
            "label=?, resolved_at=? WHERE id=?",
            (close, ret * 100, label, db.now(), r["id"]))
        graded.append((json.loads(r["features"]), label))
        results.append((r["ticker"], ret * 100, label))
    conn.commit()
    if graded:
        learn_from(conn, graded)
        import alerts
        alerts.push_graded(results)
    return results
