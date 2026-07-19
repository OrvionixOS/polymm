-- Live esports odds table
-- Separate from prematch odds for faster updates and different schema

CREATE TABLE IF NOT EXISTS esports_odds_live (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id TEXT NOT NULL,
    source TEXT NOT NULL,  -- source_g, source_l, source_b, etc.
    game TEXT NOT NULL,    -- cs2, dota2, lol
    
    -- Teams
    team1 TEXT NOT NULL,
    team2 TEXT NOT NULL,
    
    -- Odds
    odds1 DECIMAL(6,3),
    odds2 DECIMAL(6,3),
    fair_prob1 DECIMAL(5,2),
    fair_prob2 DECIMAL(5,2),
    
    -- Live-specific fields
    map_score1 INT DEFAULT 0,    -- Maps won by team1 (e.g., 1 in 1-0)
    map_score2 INT DEFAULT 0,    -- Maps won by team2
    round_score1 INT DEFAULT 0,  -- Rounds in current map for team1
    round_score2 INT DEFAULT 0,  -- Rounds in current map for team2
    current_map TEXT,            -- Current map name (e.g., "Mirage", "Nuke")
    match_format TEXT,           -- BO1, BO3, BO5
    tournament TEXT,             -- Tournament name
    
    -- Timestamps
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Unique constraint: one record per match per source (for UPSERT)
    UNIQUE(match_id, source)
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_live_odds_scraped ON esports_odds_live(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_odds_match ON esports_odds_live(match_id);
CREATE INDEX IF NOT EXISTS idx_live_odds_game ON esports_odds_live(game);
CREATE INDEX IF NOT EXISTS idx_live_odds_source ON esports_odds_live(source);

-- Positions table for shared tracking across bots
CREATE TABLE IF NOT EXISTS positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id TEXT UNIQUE NOT NULL,  -- e.g., "POS-0001"
    bot_type TEXT NOT NULL,            -- 'prematch' or 'live'
    
    -- Match info
    match_id TEXT NOT NULL,
    game TEXT NOT NULL,
    team1 TEXT NOT NULL,
    team2 TEXT NOT NULL,
    
    -- Entry order
    entry_team TEXT NOT NULL,
    entry_token_id TEXT NOT NULL,
    entry_order_id TEXT,
    entry_price DECIMAL(6,3),
    entry_shares DECIMAL(10,2),
    entry_fair_value DECIMAL(5,2),
    entry_filled_at TIMESTAMPTZ,
    
    -- Hedge order
    hedge_team TEXT,
    hedge_token_id TEXT,
    hedge_order_id TEXT,
    hedge_price DECIMAL(6,3),
    hedge_shares DECIMAL(10,2),
    hedge_filled_at TIMESTAMPTZ,
    
    -- Position state
    state TEXT DEFAULT 'pending',  -- pending, open, filled, hedging, hedged, settled, cancelled
    locked_profit DECIMAL(10,2),
    profit_percent DECIMAL(5,2),
    
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);

-- Indexes for positions
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
CREATE INDEX IF NOT EXISTS idx_positions_bot ON positions(bot_type);
CREATE INDEX IF NOT EXISTS idx_positions_match ON positions(match_id);
CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(entry_token_id);

-- Function to auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for positions
DROP TRIGGER IF EXISTS update_positions_updated_at ON positions;
CREATE TRIGGER update_positions_updated_at
    BEFORE UPDATE ON positions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Comment
COMMENT ON TABLE esports_odds_live IS 'Live esports odds - updated every 5-10 seconds during matches';
COMMENT ON TABLE positions IS 'Shared position tracking across prematch and live bots';
