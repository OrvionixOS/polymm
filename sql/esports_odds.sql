-- Esports Odds Table for Polybot
-- Run this in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS esports_odds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  match_id TEXT NOT NULL,
  source TEXT NOT NULL,           -- 'crossbet' or 'egamersworld'
  game TEXT NOT NULL,             -- 'cs2' or 'dota2'
  team1 TEXT NOT NULL,
  team2 TEXT NOT NULL,
  odds1 DECIMAL,
  odds2 DECIMAL,
  fair_prob1 DECIMAL,             -- vig-free probability
  fair_prob2 DECIMAL,
  fair_odds1 DECIMAL,             -- vig-free odds
  fair_odds2 DECIMAL,
  avg_vig DECIMAL,                -- average vig percentage
  tournament TEXT,
  format TEXT,                    -- 'Bo3', 'Bo5', etc.
  is_live BOOLEAN DEFAULT false,
  match_time TEXT,                -- scheduled time if not live
  scraped_at TIMESTAMPTZ DEFAULT now(),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Unique constraint for upsert (one record per match per source)
CREATE UNIQUE INDEX IF NOT EXISTS idx_esports_odds_unique 
  ON esports_odds(match_id, source);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_esports_odds_game ON esports_odds(game);
CREATE INDEX IF NOT EXISTS idx_esports_odds_live ON esports_odds(is_live);
CREATE INDEX IF NOT EXISTS idx_esports_odds_source ON esports_odds(source);
CREATE INDEX IF NOT EXISTS idx_esports_odds_scraped ON esports_odds(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_esports_odds_match ON esports_odds(match_id);

-- RLS policies (allow service role full access)
ALTER TABLE esports_odds ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role has full access" ON esports_odds
  FOR ALL USING (true) WITH CHECK (true);
