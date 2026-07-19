CREATE TABLE arbitrages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Match context
    match_id TEXT,
    condition_id TEXT,
    game TEXT,
    team1 TEXT,
    team2 TEXT,
    
    -- Entry leg
    entry_order_id TEXT REFERENCES filled_orders(order_id),
    entry_team TEXT NOT NULL,
    entry_price DECIMAL(10,4) NOT NULL,
    entry_shares DECIMAL(10,4) NOT NULL,
    entry_cost DECIMAL(10,2),
    entry_filled_at TIMESTAMPTZ,
    
    -- Hedge leg
    hedge_order_id TEXT REFERENCES filled_orders(order_id),
    hedge_team TEXT NOT NULL,
    hedge_price DECIMAL(10,4) NOT NULL,
    hedge_shares DECIMAL(10,4) NOT NULL,
    hedge_cost DECIMAL(10,2),
    hedge_filled_at TIMESTAMPTZ,
    
    -- Profit calculation
    total_cost DECIMAL(10,2),
    locked_profit DECIMAL(10,2),  -- guaranteed profit at fill
    locked_profit_pct DECIMAL(10,4),
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_arbitrages_match ON arbitrages(match_id);
CREATE INDEX idx_arbitrages_created ON arbitrages(created_at);