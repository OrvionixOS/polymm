"""
Configuration for both prematch and live bots.

Shared config values and bot-specific overrides.
"""

# Shared configuration
SHARED_CONFIG = {
    # 10 shares per order. Lowered from 100 to match the Rust bot's
    # default 2026-06-12 — we want both bots placing identical orders
    # while we validate the Rust rewrite live (parity doc Area 3 / Gap 3).
    # At prices ≥ $0.10, 10 shares clears Polymarket's $1 minimum order;
    # below that, callers should bump shares dynamically.
    "default_shares": 10,
    # min_shares is read by no production code today — kept aligned with
    # default_shares for clarity if it's ever wired up.
    "min_shares": 10,
    "min_profit": 0.07,            # 7% minimum profit for hedge
    "stale_threshold": 600,        # 5 minutes - cancel orders if odds older than this

    # Per-sport share overrides. All sports use 10 shares — Rust uses
    # a flat 10 default with no per-sport map, so this map is currently
    # a no-op but kept here in case we ever want to bump specific sports.
    "sport_shares": {
        "football": 10,
        "basketball": 10,
        "hockey": 10,
        "rugby": 10,
        "cricket": 10,
        "ufc": 10,
    },
}

# Prematch bot configuration
CONFIG = {
    **SHARED_CONFIG,
    # Entry criteria
    "min_edge": 0.07,              # 7% minimum edge

    "min_sources": 1,              # Minimum bookmakers for fair value
    
    # Timing
    "signal_scan_interval": 15,    # Seconds between scans
    "monitor_interval": 5,         # Seconds between position checks
    "odds_refresh_interval": 10,   # Seconds between odds fetches from Supabase
    
    # Outbid thresholds
    "outbid_threshold": 0.005,     # 0.5¢ minimum to consider outbid
    "fair_change_threshold": 0.03, # 3% change triggers re-evaluation
    "adjust_increment": 0.01,      # 1¢ bid increment when adjusting
    
    # Rugby draw hedging (3-way sports)
    "rugby_draw_max_prices": {     # Max price based on fair_prob_draw
        5.0: 0.03,  # 5% fair prob -> max 3¢
        4.0: 0.02,  # 4% fair prob -> max 2¢
        0.0: 0.01,  # <=3% fair prob -> max 1¢
    },
    "draw_min_edge": 0.015,        # 1.5% minimum edge for draw orders
}

# Live bot configuration - faster and tighter
LIVE_CONFIG = {
    **SHARED_CONFIG,
    "live_shares": 10,             # Smaller shares for live (experimental)
    "min_edge": 0.08,              # 8% minimum edge (8c spread)
    "min_spread": 0.08,            # 8c minimum market spread (bid gap)

    "min_sources": 1,              # Single source OK for live (speed matters)
    "signal_scan_interval": 5,     # 5 seconds between scans
    "monitor_interval": 2,         # 2 seconds between position checks
    "state_sync_interval": 10,     # 10 seconds state re-sync
    "book_validation_interval": 30, # 30 seconds book validation
    "odds_max_age": 30,            # Max age of odds in seconds
    "stale_threshold": 30,         # Cancel orders if odds older than this
}

# Spread bot configuration - weather temperature markets
SPREAD_CONFIG = {
    **SHARED_CONFIG,
    "min_spread": 0.15,             # 15c minimum spread to enter
    "min_edge": 0.05,               # 5c min edge (cost-based, for outbid guard)
    "min_profit": 0.10,             # 10c minimum expected profit (after fees)
    "default_shares": 10,           # Shares per order (same as other bots)
    "signal_scan_interval": 10,     # 10 seconds between scans
    "monitor_interval": 5,          # 5 seconds between position checks
    "state_sync_interval": 30,      # 30 seconds state re-sync
    "book_validation_interval": 60, # 60 seconds book validation
    
    # Bid increment for outbidding
    "bid_increment": 0.001,           # 0.1c increment (Polymarket supports 0.001)
    
    # Spread exit thresholds
    "spread_cancel_threshold": 0.10, # Cancel if spread drops below 10c
    
    # Cities to monitor
    "cities": [
        "seattle", "dallas", "london", "nyc", "seoul",
        "buenos-aires", "miami", "toronto", "ankara",
        "chicago", "wellington", "atlanta",
    ],
    
    # Deadline protection
    "deadline_stock_hours": 3,          # Cancel stock orders this many hours before market close
    "weather_cutoff_utc": {             # Cancel weather orders at 1 PM local (peak temp window)
        "nyc": 18, "atlanta": 18, "miami": 18, "toronto": 18,
        "chicago": 19, "dallas": 19,
        "seattle": 21,
        "london": 13,
        "seoul": 4,
        "ankara": 10,
        "buenos-aires": 16,
        "wellington": 0,
    },
}
