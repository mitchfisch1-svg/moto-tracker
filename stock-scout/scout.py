"""Stock Scout command line.

  python scout.py scan [--limit N] [--fresh]   scan the market, create picks
  python scout.py news                         pull SEC filings + PR wires now
  python scout.py resolve                      grade due picks, update the model
  python scout.py status                       weights, stats, open picks
  python scout.py web [--port P]               launch the dashboard
"""
import argparse
import json

import config
import db


def cmd_status():
    import learner
    conn = db.connect()
    s = db.pick_stats(conn)
    print(f"Bankroll ${config.BANKROLL:.0f} | open {s['open']}/{config.MAX_POSITIONS} "
          f"| graded {s['resolved']} | win rate {s['win_rate']:.0f}% "
          f"| avg return {s['avg_ret']:+.1f}%")
    print("\nModel weights:")
    for k, v in learner.get_weights(conn).items():
        bar = "#" * int(abs(v) * 10)
        print(f"  {k:<11} {v:+6.2f}  {bar}")
    rows = conn.execute(
        "SELECT * FROM picks WHERE status='open' ORDER BY picked_at DESC").fetchall()
    if rows:
        print("\nOpen picks:")
        for r in rows:
            print(f"  {r['ticker']:<6} entry ${r['entry_price']:.2f}  "
                  f"p={r['prob']:.2f}  grades after {r['resolve_after'][:10]}")


def main():
    ap = argparse.ArgumentParser(description="Stock Scout")
    sub = ap.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("scan", help="scan the market, create paper picks")
    p_scan.add_argument("--limit", type=int, default=None,
                        help="only scan the N most liquid names (quick test)")
    p_scan.add_argument("--fresh", action="store_true",
                        help="force re-download of the stock universe")
    sub.add_parser("news", help="pull news feeds now")
    sub.add_parser("resolve", help="grade due picks, update the model")
    sub.add_parser("status", help="show weights, stats, open picks")
    sub.add_parser("alert-test", help="send a test push to your phone")
    p_web = sub.add_parser("web", help="launch the dashboard")
    p_web.add_argument("--port", type=int, default=config.WEB_PORT)
    args = ap.parse_args()

    if args.cmd == "scan":
        import screener
        screener.run_scan(limit=args.limit, force_universe=args.fresh)
    elif args.cmd == "news":
        import news
        news.refresh_news()
    elif args.cmd == "resolve":
        import learner
        results = learner.resolve_open_picks(db.connect())
        if not results:
            print("No picks due for grading.")
        for t, ret, label in results:
            print(f"  {t:<6} {ret:+.1f}%  -> {'WIN' if label else 'MISS'}")
    elif args.cmd == "alert-test":
        import alerts
        ok = alerts.send("Stock Scout test",
                         "If you can read this on your phone, alerts work.",
                         tags=["tada"])
        print("Sent -- check your phone." if ok
              else "Failed -- check NTFY_TOPIC in config.py and your internet.")
    elif args.cmd == "web":
        from app import serve
        print(f"Dashboard -> http://localhost:{args.port}")
        serve(args.port)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
