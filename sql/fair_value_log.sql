CREATE TABLE fair_value_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Match identification
    match_id TEXT NOT NULL,
    team1 TEXT NOT NULL,
    team2 TEXT NOT NULL,
    game TEXT,
    
    -- Aggregated fair values
    fair_prob1 DECIMAL(10,4),
    fair_prob2 DECIMAL(10,4),
    source_count INTEGER,

    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_fair_value_log_match ON fair_value_log(match_id);
CREATE INDEX idx_fair_value_log_recorded ON fair_value_log(recorded_at);

-- Retention policy: Keep 30 days