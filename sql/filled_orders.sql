CREATE TABLE filled_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Polymarket identifiers (consistent across restarts)
    order_id TEXT NOT NULL UNIQUE,
    token_id TEXT NOT NULL,
    condition_id TEXT,  -- Polymarket condition ID for cross-referencing
    
    -- Match context
    match_id TEXT,
    game TEXT,
    team1 TEXT,
    team2 TEXT,
    market_type TEXT,  -- esports, esports_live, weather, stock, rugby
    
    -- Order details
    side TEXT NOT NULL,  -- 'entry' or 'hedge'
    team TEXT NOT NULL,  -- which team we bet on
    price DECIMAL(10,4) NOT NULL,
    shares DECIMAL(10,4) NOT NULL,
    cost DECIMAL(10,2) GENERATED ALWAYS AS (price * shares) STORED,
    
    -- Timing
    placed_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    time_to_fill_seconds INTEGER,
    
    -- Fair value context at fill
    fair_value_at_fill DECIMAL(10,4),
    edge_at_fill DECIMAL(10,4),  -- fair_value - price
    spread_at_fill DECIMAL(10,4),  -- bid-ask spread (best_ask - best_bid) on filled token at fill time
    raw_odds JSONB,  -- {"source_b": {"odds": 1.55, "prob": 64.5}, "source_l": {...}}
    trading_deadline TIMESTAMPTZ,  -- UTC timestamp when we stop trading this market
    
    -- Linking
    arb_id UUID REFERENCES arbitrages(id),  -- if part of an arb
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_filled_orders_match ON filled_orders(match_id);
CREATE INDEX idx_filled_orders_filled_at ON filled_orders(filled_at);
CREATE INDEX idx_filled_orders_market_type ON filled_orders(market_type);