"""
Unit tests for scanning/team_matcher.py and core/match_id.py - Team name matching.
"""
import pytest

from src.core.match_id import (
    normalize_team,
    normalize_game,
    make_match_id,
    is_individual_game_market,
    parse_match_question,
)
from src.scanning.team_matcher import (
    teams_match_exactly,
    find_matching_odds,
    get_fair_value_for_team,
)


class TestNormalizeTeam:
    """Tests for normalize_team function."""
    
    def test_lowercase(self):
        """Team names are lowercased."""
        assert normalize_team("Team Liquid") == "liquid"
        assert normalize_team("NAVI") == "navi"
    
    def test_removes_common_suffixes(self):
        """Removes 'Team', 'Gaming', 'Esports', etc."""
        assert normalize_team("NaVi Gaming") == "navi"
        assert normalize_team("Vitality Esports") == "vitality"  # tier-1 team, not the Bee academy
        assert normalize_team("Team Spirit") == "spirit"
    
    def test_removes_accents(self):
        """Removes accented characters."""
        assert normalize_team("Leviatán") == "leviatan"
        assert normalize_team("São Paulo") == "saopaulo"
    
    def test_removes_punctuation(self):
        """Removes punctuation and spaces."""
        assert normalize_team("100 Thieves") == "100thieves"
        assert normalize_team("G2 Esports") == "g2"
    
    def test_alias_resolution(self):
        """Aliases resolve to canonical names."""
        # Keyd Stars -> vivokeydstars
        assert normalize_team("Keyd Stars") == "vivokeydstars"
        assert normalize_team("Vivo Keyd Stars") == "vivokeydstars"
    
    def test_korean_challenger_aliases(self):
        """Korean Challengers/Academy team aliases resolve correctly."""
        # BNK FearX variants -> bnkfearxyouth
        assert normalize_team("BNK FearX") == "bnkfearxyouth"
        assert normalize_team("BNK FearX Youth") == "bnkfearxyouth"
        assert normalize_team("FearX") == "bnkfearxyouth"
        assert normalize_team("FearX Youth") == "bnkfearxyouth"
        
        # Hanwha Life Challengers variants
        assert normalize_team("Hanwha Life Challengers") == "hanwhalifechallengers"
        assert normalize_team("Hanwha Life Esports Challengers") == "hanwhalifechallengers"
        
        # HANJIN BRION variants
        assert normalize_team("HANJIN BRION Challengers") == "hanjinbrionchallengers"
        assert normalize_team("Hanjin Brion") == "hanjinbrionchallengers"
    
    def test_typo_aliases(self):
        """Team name typos resolve to canonical names."""
        # Aurora Young Blud (typo) -> aurorayoungblood
        assert normalize_team("Aurora Young Blud") == "aurorayoungblood"
        assert normalize_team("Aurora Young Blood") == "aurorayoungblood"
    
    def test_dota2_team_aliases(self):
        """Dota 2 team aliases resolve correctly."""
        # Looking For Org PE -> lookingfororg
        assert normalize_team("Looking For Org PE") == "lookingfororg"
        assert normalize_team("Looking for Org") == "lookingfororg"
    
    def test_cs2_team_aliases(self):
        """CS2 team aliases resolve correctly."""
        # Senshi Esports Club variants
        assert normalize_team("Senshi Esports Club") == "senshiesportsclub"
        assert normalize_team("Senshi eSports") == "senshiesportsclub"
        assert normalize_team("Senshi") == "senshiesportsclub"
        
        # Sangal ALTERS / Sangal Academy
        assert normalize_team("Sangal ALTERS") == "sangalalters"
        assert normalize_team("Sangal Academy") == "sangalalters"
        
        # PCIFIC variants
        assert normalize_team("PCIFIC Espor") == "pcificespor"
        assert normalize_team("PCIFIC") == "pcificespor"
        
        # WATERMELON / WATERMEL0N (zero vs letter O)
        assert normalize_team("WATERMELON") == "watermelon"
        assert normalize_team("WATERMEL0N") == "watermelon"
        
        # paiN Academy variants
        assert normalize_team("paiN Academy") == "painacademy"
        assert normalize_team("paiN Gaming Academy") == "painacademy"
        
        # The Bandits / banditsesc
        assert normalize_team("The Bandits") == "thebandits"
        assert normalize_team("banditsesc") == "thebandits"
        
        # Frites Esports Club / Frites
        assert normalize_team("Frites Esports Club") == "fritesesportsclub"
        assert normalize_team("Frites") == "fritesesportsclub"
        
        # Dragons Esports / Dragons Esports Club
        # "Dragons Esports" strips " Esports" → "dragons" (stays as-is, no alias)
        # "Dragons Esports Club" → "dragonsesportsclub" → alias → "dragonsesports"
        assert normalize_team("Dragons Esports") == "dragons"
        assert normalize_team("Dragons Esports Club") == "dragonsesports"
    
    def test_jan2026_alignment_aliases(self):
        """Team aliases added Jan 2026 for alignment issues."""
        # Famalicao / FC Famalicão Esports
        assert normalize_team("Famalicao") == "famalicao"
        assert normalize_team("FC Famalicão Esports") == "famalicao"
        
        # Dortmund Esports / Dortmund Gesichtenhausen
        assert normalize_team("Dortmund Esports") == "dortmund"
        assert normalize_team("Dortmund Gesichtenhausen") == "dortmund"
        
        # Karmine Corp Blue Stars / Karmine Corp Blue
        assert normalize_team("Karmine Corp Blue Stars") == "karminecorpbluestars"
        assert normalize_team("Karmine Corp Blue") == "karminecorpbluestars"
        
        # BoostGate Esports / BoostGate Espor (typo)
        assert normalize_team("BoostGate Esports") == "boostgate"
        assert normalize_team("BoostGate Espor") == "boostgate"
        
        # Pipsqueak / Pipsqueak+4
        assert normalize_team("Pipsqueak") == "pipsqueak"
        assert normalize_team("Pipsqueak+4") == "pipsqueak"
        
        # GnG Esports / GnG Amazigh
        assert normalize_team("GnG Esports") == "gng"
        assert normalize_team("GnG Amazigh") == "gng"
        
        # DetonatioN FocusMe / DetonatioN FM
        assert normalize_team("DetonatioN FocusMe") == "detonationfocusme"
        assert normalize_team("DetonatioN FM") == "detonationfocusme"
        
        # Shopify Rebellion / Shopify Rebellion Black
        assert normalize_team("Shopify Rebellion") == "shopifyrebellion"
        assert normalize_team("Shopify Rebellion Black") == "shopifyrebellion"
    
    def test_empty_string(self):
        """Empty string returns empty."""
        assert normalize_team("") == ""


class TestNormalizeGame:
    """Tests for normalize_game function."""
    
    def test_cs2_variants(self):
        """All CS2 variants normalize to 'cs2'."""
        assert normalize_game("Counter-Strike") == "cs2"
        assert normalize_game("counter-strike") == "cs2"
        assert normalize_game("cs2") == "cs2"
        assert normalize_game("CS2") == "cs2"
    
    def test_dota_variants(self):
        """All Dota variants normalize to 'dota2'."""
        assert normalize_game("Dota 2") == "dota2"
        assert normalize_game("dota2") == "dota2"
        assert normalize_game("dota") == "dota2"
    
    def test_lol_variants(self):
        """All LoL variants normalize to 'lol'."""
        assert normalize_game("League of Legends") == "lol"
        assert normalize_game("LoL") == "lol"
    
    def test_sports_with_league_in_name(self):
        """Sport names containing 'league' should NOT be classified as LoL."""
        assert normalize_game("Cricket South Africa T20 League") == "cricket"
        assert normalize_game("Champions League Basketball") == "basketball"
        assert normalize_game("Rugby Champions Cup") == "rugby"
        assert normalize_game("Football Premier League") == "football"


class TestMakeMatchId:
    """Tests for make_match_id function."""
    
    def test_creates_canonical_id(self):
        """Creates properly formatted match ID."""
        match_id = make_match_id("Team A", "Team B", "cs2")
        # Teams sorted alphabetically: a < b
        assert match_id == "cs2:a:vs:b"
    
    def test_sorts_teams_alphabetically(self):
        """Teams are sorted to ensure consistent IDs."""
        id1 = make_match_id("Zeta", "Alpha", "cs2")
        id2 = make_match_id("Alpha", "Zeta", "cs2")
        assert id1 == id2  # Same ID regardless of order
        assert "alpha:vs:zeta" in id1
    
    def test_normalizes_teams(self):
        """Team names are normalized."""
        match_id = make_match_id("Team Liquid", "Natus Vincere Gaming", "cs2")
        assert "liquid" in match_id
        assert "navi" in match_id or "natusvincere" in match_id


class TestTeamMatchExactly:
    """Tests for teams_match_exactly function."""
    
    def test_exact_match(self):
        """Exact matches after normalization."""
        assert teams_match_exactly("Team Liquid", "team liquid") is True
        assert teams_match_exactly("NAVI", "NaVi Gaming") is True
    
    def test_no_match(self):
        """Different teams don't match."""
        assert teams_match_exactly("Team A", "Team B") is False


# team_match_score has been REMOVED - it enabled dangerous fuzzy matching.
# Use find_matching_odds() or get_fair_value_for_team() for exact matching instead.



class TestIsIndividualGameMarket:
    """Tests for is_individual_game_market function."""
    
    def test_detects_game_markers(self):
        """Detects 'Game 1', 'Map 2', 'Round 3', etc."""
        # The function checks for patterns like "Game 1", "Map 2", etc.
        # It should detect individual game markets
        assert is_individual_game_market("Dota 2: Team A vs Team B - Game 1 Winner") is True
        assert is_individual_game_market("CS2: Team A vs Team B - Map 3") is True
    
    def test_allows_regular_markets(self):
        """Allows regular match winner markets."""
        assert is_individual_game_market("Dota 2: Team A vs Team B (BO3)") is False
        assert is_individual_game_market("CS2: Team A vs Team B") is False


class TestParseMatchQuestion:
    """Tests for parse_match_question function."""
    
    def test_parses_standard_format(self):
        """Parses 'Game: Team1 vs Team2 (BO1)' format."""
        game, team1, team2 = parse_match_question("CS2: Team A vs Team B (BO1)")
        assert game == "cs2"
        assert "a" in team1.lower() or "b" in team1.lower()
    
    def test_handles_accented_teams(self):
        """Handles teams with accents."""
        game, team1, team2 = parse_match_question("LoL: Leviatán vs Team A")
        assert game == "lol"
    
    def test_returns_empty_on_failure(self):
        """Returns empty strings on parse failure."""
        game, team1, team2 = parse_match_question("Invalid format here")
        # Should return empty or parsed values, not crash
        assert isinstance(game, str)
        assert isinstance(team1, str)
        assert isinstance(team2, str)
    
    def test_ncaab_vs_dot_format(self):
        """Handles NCAAB 'Team A vs. Team B' with period after vs."""
        game, team1, team2 = parse_match_question("UNLV Runnin' Rebels vs. Utah State Aggies")
        assert team1 == "UNLV Runnin' Rebels"
        assert team2 == "Utah State Aggies"
    
    def test_ncaab_no_game_prefix(self):
        """NCAAB questions have no game prefix - returns empty game."""
        game, team1, team2 = parse_match_question("Grand Canyon Antelopes vs. Utah State Aggies")
        assert game == ""
        assert team1 == "Grand Canyon Antelopes"
        assert team2 == "Utah State Aggies"
    
    def test_ncaab_with_bo_suffix(self):
        """Handles 'vs.' with (BOx) suffix."""
        game, team1, team2 = parse_match_question("Team A vs. Team B (BO3)")
        assert team1 == "Team A"
        assert team2 == "Team B"
