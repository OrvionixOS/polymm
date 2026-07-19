-- sports_odds_v2: Multi-market odds from the-odds-api.com
--
-- Supports all market types: h2h, spreads, totals
-- One row per match × market_type × line × source × sport

CREATE TABLE IF NOT EXISTS sports_odds_v2 (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  
  -- Match identification
  match_id TEXT NOT NULL,           -- '{sport}:{sorted_team1}:vs:{sorted_team2}'
  source TEXT NOT NULL DEFAULT 'the-odds-api',
  sport TEXT NOT NULL,              -- 'soccer_epl', 'icehockey_nhl', etc.
  team1 TEXT NOT NULL,
  team2 TEXT NOT NULL,
  
  -- Market identification
  market_type TEXT NOT NULL,        -- 'h2h', 'spreads', 'totals'
  line DECIMAL NOT NULL DEFAULT 0,  -- 0 for h2h, 2.5 for totals, 1.5 for spreads
  
  -- Outcomes (names differ by market type)
  outcome1_name TEXT NOT NULL,      -- Team name, 'Over', 'Yes'
  outcome2_name TEXT NOT NULL,      -- Team name, 'Under', 'No'
  outcome_draw_name TEXT,           -- 'Draw' for 3-way h2h, NULL otherwise
  
  -- Median odds across bookmakers (decimal format)
  odds1 DECIMAL NOT NULL,
  odds2 DECIMAL NOT NULL,
  odds_draw DECIMAL,
  
  -- Fair probabilities (vig-removed, 0-100 scale)
  fair_prob1 DECIMAL NOT NULL,
  fair_prob2 DECIMAL NOT NULL,
  fair_prob_draw DECIMAL,
  
  -- Metadata
  event_id TEXT,                    -- the-odds-api event ID
  commence_time TIMESTAMPTZ,        -- Match start time
  bookmaker_count INT DEFAULT 1,
  is_live BOOLEAN DEFAULT false,
  team1_spread_point DECIMAL,       -- Team1's signed spread point (+1.5 or -1.5), NULL for non-spreads
  scraped_at TIMESTAMPTZ DEFAULT now(),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Unique: one row per match + market + line + source + sport
-- sport is included so same teams in different leagues are stored separately
CREATE UNIQUE INDEX IF NOT EXISTS idx_sports_odds_v2_unique
  ON sports_odds_v2(match_id, market_type, line, source, sport);

-- Query indexes
CREATE INDEX IF NOT EXISTS idx_sports_odds_v2_sport ON sports_odds_v2(sport);
CREATE INDEX IF NOT EXISTS idx_sports_odds_v2_market ON sports_odds_v2(market_type);
CREATE INDEX IF NOT EXISTS idx_sports_odds_v2_scraped ON sports_odds_v2(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_sports_odds_v2_match ON sports_odds_v2(match_id);
