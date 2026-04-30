"""System-wide constants. No magic numbers anywhere else in the codebase."""

# ---------------------------------------------------------------------------
# UNIVERSE FILTERS
# ---------------------------------------------------------------------------
FLOAT_MAX = 10_000_000          # 10M shares max float
FLOAT_MICRO = 2_000_000         # under 2M = extreme caution
PRICE_MIN = 1.00                # minimum price for S1
PRICE_MAX = 20.00               # maximum price for S1
AVG_DAILY_VOLUME_MIN = 500_000  # minimum 5-day avg volume

# ---------------------------------------------------------------------------
# SIGNAL THRESHOLDS
# ---------------------------------------------------------------------------
CATALYST_SCORE_MINIMUM = 60
CONFIDENCE_THRESHOLD_S1 = 65
CONFIDENCE_THRESHOLD_S2 = 70
CONFIDENCE_THRESHOLD_S1_WITH_S2_OPEN = 75  # raises when 3+ S2 positions open
LIQUIDITY_SCORE_MINIMUM = 40
LIQUIDITY_SCORE_FULL_SIZE = 60
LIQUIDITY_SCORE_EXCELLENT = 80
MIN_RR_RATIO = 3.0

# ---------------------------------------------------------------------------
# RISK PARAMETERS
# ---------------------------------------------------------------------------
RISK_PCT_S1 = 0.01              # 1% per S1 trade
RISK_PCT_S2_MIN = 0.01          # 1% minimum per S2 position
RISK_PCT_S2_MAX = 0.015         # 1.5% maximum per S2 position
MAX_DAILY_LOSS_PCT = 0.02       # 2% combined daily max
MAX_DAILY_LOSS_S1 = 0.01        # 1% S1 sub-limit
MAX_DAILY_LOSS_S2 = 0.015       # 1.5% S2 sub-limit (mark to market)
MAX_POSITION_PCT = 0.20         # 20% max single position
MAX_POSITION_PCT_SUB2 = 0.10    # 10% max for stocks under $2
S2_MAX_CONCURRENT = 4
S2_MAX_EXPOSURE_PCT = 0.40      # 40% max total swing exposure
COMBINED_EXPOSURE_MAX = 0.55    # 55% max S1 + S2 combined
CASH_BUFFER_MIN = 0.45          # 45% always in cash
CONSECUTIVE_LOSS_THRESHOLD = 3  # losses before size reduction
CONSECUTIVE_LOSS_SIZE_REDUCTION = 0.50

# ---------------------------------------------------------------------------
# STRATEGY 1 ENTRY
# ---------------------------------------------------------------------------
S1_VOLUME_MULTIPLIER = 5.0      # 5x prior 20-session avg 15-min volume
S1_SPREAD_MAX_PCT = 0.01        # 1% max spread
S1_ENTRY_WINDOW_MINUTES = 14    # countdown timer duration

# ---------------------------------------------------------------------------
# STRATEGY 2 TIME STOPS (days)
# ---------------------------------------------------------------------------
S2_TIME_STOP_CATEGORY_A = 14
S2_TIME_STOP_CATEGORY_B = 21
S2_TIME_STOP_CATEGORY_C = 7
S2_TIME_STOP_CATEGORY_D = 0     # exits day of catalyst event

# ---------------------------------------------------------------------------
# STRATEGY 1 EXIT LADDER
# ---------------------------------------------------------------------------
S1_TARGET_1_R = 2.0                  # first target at 2R
S1_TARGET_1_SELL_PCT = 0.50          # sell 50% at target 1
S1_TARGET_2_R = 3.0
S1_TIME_STOP_NO_MOVEMENT_MINS = 30   # exit 50% if not at 1R in 30 mins
S1_TIME_STOP_FULL_EXIT_MINS = 60     # full exit if not at 1R in 60 mins

# ---------------------------------------------------------------------------
# MARKET HOURS (ET)
# ---------------------------------------------------------------------------
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
PREMARKET_BRIEF_TIME = "06:15"
AFTERMARKET_BRIEF_TIME = "16:30"
EDGAR_WATCH_START = "04:00"
EDGAR_WATCH_END = "20:00"
S1_ENTRY_CUTOFF = "15:45"            # no new S1 entries after this
S1_HIGH_PROB_WINDOW_END = "11:30"    # best window closes
AFTERNOON_ENTRY_CUTOFF = "14:00"     # no new entries unless catalyst <30 mins old

# ---------------------------------------------------------------------------
# MARKET REGIME (VIX thresholds)
# ---------------------------------------------------------------------------
VIX_REDUCE_THRESHOLD = 25.0
VIX_SUPPRESS_THRESHOLD = 30.0
SPY_GAP_CIRCUIT_BREAKER = -0.015     # -1.5% SPY gap triggers manual override

# ---------------------------------------------------------------------------
# BROKER
# ---------------------------------------------------------------------------
SPREAD_LIMIT_ABSOLUTE = 0.01         # 1% spread hard limit
BRACKET_ORDER_TIMEOUT_SECONDS = 300

# ---------------------------------------------------------------------------
# PDT
# ---------------------------------------------------------------------------
PDT_ACCOUNT_THRESHOLD = 25_000
PDT_MAX_DAY_TRADES = 3
PDT_ROLLING_DAYS = 5

# ---------------------------------------------------------------------------
# EDGAR / SEC INGESTION
# ---------------------------------------------------------------------------
EDGAR_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
EDGAR_COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
EDGAR_POLL_INTERVAL_MINUTES = 5
EDGAR_POLL_HOUR_START_ET = 4    # 04:00 ET — pre-market window opens
EDGAR_POLL_HOUR_END_ET = 20     # 20:00 ET — after-hours window closes
EDGAR_HTTP_TIMEOUT_SECONDS = 15
EDGAR_HTTP_RETRY = 3
EDGAR_RSS_FETCH_COUNT = 100     # max items per poll, per form type

# Universe seeding stays wider than the tradeable float cap so we keep
# borderline companies in view if they cross the threshold later.
EDGAR_UNIVERSE_FLOAT_MAX = 15_000_000
EDGAR_UNIVERSE_TARGET_SIZE = 5000
# "Nasdaq" includes Capital Market, Global Market, and Global Select tiers
# in SEC's master JSON — we accept all three at seed time and rely on the
# float_updater to narrow to genuine micro-caps (float ≤ FLOAT_MAX).
EDGAR_SMALL_EXCHANGES = (
    "OTC", "Pink", "OTCBB",
    "NYSE MKT", "NYSE American",
    "Nasdaq",
)

# Forms to monitor on every poll, ordered by priority.
EDGAR_PRIORITY_FORMS = (
    "8-K",       # event-driven catalyst (most relevant in our window)
    "S-1",       # initial registration
    "S-3",       # shelf registration (dilution overhang signal)
    "4",         # insider transactions (Form 4)
    "DEF 14A",   # proxy — reverse-split votes live here
    "SC 13G",    # passive 5%+ ownership disclosures
    "NT 10-K",   # late annual filing — distress signal
    "NT 10-Q",   # late quarterly filing — distress signal
)
# Items 8.01 (other events) and 2.02 (results of operations) are the
# catalyst-relevant slots inside an 8-K. The rest are Phase-1 additions:
#   5.03 — amendments to articles (reverse splits, share-class changes)
#   3.02 — unregistered equity sales (private placements / dilution)
#   1.01 — material agreement entry (IR firm hires, underwriter contracts)
#   7.01 — Reg FD disclosure (investor presentations, conference invites)
EDGAR_8K_PRIORITY_ITEMS = (
    "8.01", "2.02", "5.03", "3.02", "1.01", "7.01",
)

# ---------------------------------------------------------------------------
# POLYGON.IO (rebranded as Massive.com Oct 2025; SDK + key unchanged)
# ---------------------------------------------------------------------------
# Starter plan = 5 req/min. Real-time tier (required before Phase 2)
# is unlimited. The throttler reads this constant so a tier upgrade only
# requires changing one number.
POLYGON_REQUESTS_PER_MINUTE = 5
POLYGON_HTTP_TIMEOUT_SECONDS = 30
POLYGON_FLOAT_BATCH_PROGRESS_INTERVAL = 50  # report every N tickers in update_floats

# ---------------------------------------------------------------------------
# EXPONENTIAL WEIGHTING (months)
# ---------------------------------------------------------------------------
WEIGHT_0_6_MONTHS = 3.0
WEIGHT_6_12_MONTHS = 2.0
WEIGHT_12_24_MONTHS = 1.5
WEIGHT_24_36_MONTHS = 1.0
