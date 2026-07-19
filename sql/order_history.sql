CREATE TABLE order_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Identification
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    condition_id TEXT,
    match_id TEXT,
    team TEXT,
    
    -- Lifecycle
    placed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cancelled_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    
    -- Status
    final_status TEXT NOT NULL,  -- FILLED, CANCELLED, EXPIRED
    
    -- Price tracking
    initial_price DECIMAL(10,4) NOT NULL,
    final_price DECIMAL(10,4),
    adjustment_count INTEGER DEFAULT 0,
    price_history JSONB,  -- [{"price": 0.30, "at": "...", "reason": "outbid"}]
    
    -- Cancel context
    cancel_reason TEXT,  -- EDGE_LOST, OUTBID, MATCH_LIVE, MANUAL, FILLED
    best_bid_at_cancel DECIMAL(10,4),
    fair_value_at_cancel DECIMAL(10,4),
    
    -- Initial context
    fair_value_at_place DECIMAL(10,4),
    edge_at_place DECIMAL(10,4),
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_order_history_match ON order_history(match_id);
CREATE INDEX idx_order_history_status ON order_history(final_status);
CREATE INDEX idx_order_history_placed ON order_history(placed_at);

-- Retention policy: Keep 90 days
-- (Run periodically: DELETE FROM order_history WHERE placed_at < NOW() - INTERVAL '90 days')