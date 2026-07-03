"""Central knobs for Stock Scout. Change a number, restart, done."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# --- Bankroll / paper trading ---
BANKROLL = 1000.0                          # what you're playing with
MAX_POSITIONS = 5                          # never more than 5 names at once
POSITION_SIZE = BANKROLL / MAX_POSITIONS   # $200 a slot. Boring math is what saves accounts.

# --- Universe filters (the pond we fish in) ---
PRICE_MIN = 1.00            # below $1 is lottery-ticket land + delisting risk
PRICE_MAX = 15.00           # "cheap" but still investable
MCAP_MIN = 50e6             # under $50M market cap is shell-company territory
MCAP_MAX = 2e9              # over $2B and you're back in boring blue-chip land
MIN_SHARE_VOLUME = 300_000  # if nobody trades it, you can't get out of it

# --- Picks & the learning loop ---
PICK_TOP_N = 5              # picks created per scan (capped by MAX_POSITIONS open)
HORIZON_DAYS = 7            # calendar days until a pick gets graded (~5 trading days)
TARGET_RET = 0.05           # +5% inside the horizon counts as a "win" the model learns from
LEARNING_RATE = 0.05        # how hard each graded pick nudges the model's weights

# --- News radar ---
NEWS_REFRESH_MIN = 5        # dashboard background thread re-pulls feeds this often
NEWS_LOOKBACK_H = 72        # news this recent counts toward a ticker's catalyst score
SEC_USER_AGENT = "StockScout/0.1 (mitchfisch1@gmail.com)"  # SEC requires a contact UA

WEB_PORT = 5050

# --- Phone push alerts (ntfy) ---
# Install the free "ntfy" app on your iPhone and subscribe to this topic.
# The topic name is the only secret -- anyone who knows it can read your
# alerts, so keep it random. Set to "" to disable pushes entirely.
NTFY_TOPIC = "stock-scout-1a0a80395740"
ALERT_MIN_SCORE = 0.5       # |news score| needed to buzz your phone (pond tickers only)
ALERT_MAX_PER_REFRESH = 5   # spam guard per news refresh
