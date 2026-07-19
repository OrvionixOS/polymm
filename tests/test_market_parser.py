"""Tests for src/core/market_parser.py — shared Polymarket title parsing."""
import pytest
from src.core.market_parser import (
    classify_market_type, extract_line, extract_line_label,
    match_odds_to_poly_market,
    SPREAD, TOTALS, OU, BTTS, EXACT_SCORE, GOALSCORER, MONEYLINE, HALFTIME,
    SET_HANDICAP, SET_WINNER, TOTAL_SETS, GO_THE_DISTANCE, ROUNDS_OU,
    WIN_BY_KO, TOSS_WINNER, TOSS_MATCH_DOUBLE, COMPLETED_MATCH,
    TOP_BATTER, MOST_SIXES, MENTIONS, TEMPERATURE, DIRECTION, OTHER,
    MAP_WINNER,
)


# ========== classify_market_type ==========

class TestClassifyMarketType:
    """Test market type classification from Polymarket titles."""

    # --- Spreads ---
    @pytest.mark.parametrize("title,expected", [
        ("Spread: Seattle Redhawks (-7.5)", SPREAD),
        ("Spread: Miami (OH) RedHawks (-10.5)", SPREAD),
        ("Spread: Tigres de la UANL (-2.5)", SPREAD),
        ("Spread: Oilers (-1.5)", SPREAD),
        ("AUG -2.5", SPREAD),        # bare spread token
        ("KOE +2.5", SPREAD),         # bare spread token
        ("DUKE -9.5", SPREAD),
    ])
    def test_spread(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Totals / O/U ---
    @pytest.mark.parametrize("title,expected", [
        ("O 148.5", TOTALS),
        ("U 139.5", TOTALS),
        ("Over 2.5", OU),
        ("Under 23.5", OU),
        ("Over", OU),
        ("Under", OU),
        ("Gardner-Webb vs. Charleston Southern: O/U 158.5", TOTALS),
        ("Flyers vs. Rangers: O/U 5.5", TOTALS),
        ("Olympique de Marseille vs. Olympique Lyonnais: O/U 1.5", TOTALS),
    ])
    def test_totals(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Tennis ---
    @pytest.mark.parametrize("title,expected", [
        ("Set Handicap: Tauson (-1.5) vs Linette (+1.5)", SET_HANDICAP),
        ("Set Handicap: Medvedev (-1.5) vs Brooksby (+1.5)", SET_HANDICAP),
        ("Set 1 Winner: Tabilo vs Barrios", SET_WINNER),
        ("Set 1 Winner: Bucsa vs Stakusic", SET_WINNER),
        ("Rublev vs. Royer: Total Sets O/U 2.5", TOTAL_SETS),
        ("Galarneau vs. Gojo: Total Sets O/U 2.5", TOTAL_SETS),
        ("Set 1 Games O/U 8.5", TOTALS),  # Set games → Totals
        ("Match O/U 21.5", TOTALS),
    ])
    def test_tennis(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Esports per-map winner (child_moneyline) ---
    @pytest.mark.parametrize("title,expected", [
        ("LoL: Anyone's Legend vs Weibo Gaming - Game 1 Winner", MAP_WINNER),
        ("Valorant: TEC Esports vs All Gamers - Map 1 Winner", MAP_WINNER),
        ("Game 2 Winner", MAP_WINNER),
        ("Map 3 Winner", MAP_WINNER),
        # series moneyline (no game/map #) must stay MONEYLINE, not MAP_WINNER
        ("Dota 2: Shizageddon vs Nemiga Gaming", MONEYLINE),
        ("LoL: Anyone's Legend vs Weibo Gaming (BO3)", MONEYLINE),
    ])
    def test_esports_map_winner(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Football ---
    @pytest.mark.parametrize("title,expected", [
        ("Samsunspor vs. Gaziantep FK: Both Teams to Score", BTTS),
        ("CF América vs. Tigres: Both Teams to Score", BTTS),
        ("Exact Score: Eintracht Frankfurt 2 - 0 SC Freiburg?", EXACT_SCORE),
        ("Exact Score: AS Roma 3 - 3 Juventus FC?", EXACT_SCORE),
        ("Benjamin Sesko: Anytime Goalscorer", GOALSCORER),
        ("Ryan Rodin: Anytime Goalscorer", GOALSCORER),
        ("Draw", MONEYLINE),
    ])
    def test_football(self, title, expected):
        assert classify_market_type(title, "football") == expected

    # --- UFC ---
    @pytest.mark.parametrize("title,expected", [
        ("Fight to Go the Distance?", GO_THE_DISTANCE),
        ("O/U 1.5 Rounds", ROUNDS_OU),
        ("O/U 2.5 Rounds", ROUNDS_OU),
        ("Will Cristian Quiñonez win by KO or TKO?", WIN_BY_KO),
        ("Will the fight be won by KO or TKO?", WIN_BY_KO),
        ('Will the announcers say "Eye poke" during Strickland vs. Hernandez?', MENTIONS),
    ])
    def test_ufc(self, title, expected):
        assert classify_market_type(title, "ufc") == expected

    # --- Cricket ---
    @pytest.mark.parametrize("title,expected", [
        ("ODI Series: Australia vs India - Who wins the toss?", TOSS_WINNER),
        ("T20 World Cup: Sri Lanka vs England - Toss Match Double Sri Lanka Winner", TOSS_MATCH_DOUBLE),
        ("T20 World Cup: India vs West Indies - Completed match?", COMPLETED_MATCH),
        ("Australia vs India - Team Top Batter Australia?", TOP_BATTER),
        ("T20 World Cup: Sri Lanka vs New Zealand - Most Sixes Sri Lanka Winner", MOST_SIXES),
    ])
    def test_cricket(self, title, expected):
        assert classify_market_type(title, "cricket") == expected

    # --- Moneyline ---
    @pytest.mark.parametrize("title,expected", [
        ("California Golden Bears vs. Syracuse Orange", MONEYLINE),
        ("KHL: Torpedo vs. Ak Bars Kazan", MONEYLINE),
        ("Will Leinster win?", MONEYLINE),
        ("Will National Bank of Egypt Club win on 2026-02-25?", MONEYLINE),
    ])
    def test_moneyline(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Halftime / 1H Moneyline ---
    @pytest.mark.parametrize("title,expected", [
        ("Kings: 1H Moneyline", HALFTIME),
        ("Pelicans: 1H Moneyline", HALFTIME),
        ("Halftime Result", HALFTIME),
        ("LA Lakers vs Nuggets: Halftime Result", HALFTIME),
    ])
    def test_halftime(self, title, expected):
        assert classify_market_type(title) == expected

    # --- Non-sport ---
    def test_temperature(self):
        assert classify_market_type("NYC Temperature Above 45°F?") == TEMPERATURE

    def test_direction(self):
        assert classify_market_type("Will AAPL go up or down?") == DIRECTION

    def test_empty(self):
        assert classify_market_type("") == OTHER


# ========== extract_line ==========

class TestExtractLine:
    """Test line extraction from Polymarket titles."""

    # --- Spreads ---
    def test_spread_parentheses(self):
        assert extract_line("Spread: Team (-2.5)", SPREAD) == -2.5

    def test_spread_parentheses_positive(self):
        assert extract_line("Spread: Team (+7.5)", SPREAD) == 7.5

    def test_spread_bare(self):
        assert extract_line("DUKE -9.5", SPREAD) == -9.5

    def test_spread_bare_positive(self):
        assert extract_line("NCST +9.5", SPREAD) == 9.5

    # --- Set Handicap ---
    def test_set_handicap(self):
        assert extract_line("Set Handicap: Tauson (-1.5) vs Linette (+1.5)", SET_HANDICAP) == -1.5

    # --- Map Winner (line = map/game number) ---
    def test_map_winner_game(self):
        assert extract_line("LoL: A vs B - Game 1 Winner", MAP_WINNER) == 1.0

    def test_map_winner_map(self):
        assert extract_line("Valorant: A vs B - Map 3 Winner", MAP_WINNER) == 3.0

    # --- Totals ---
    def test_totals_short(self):
        assert extract_line("O 148.5", TOTALS) == 148.5

    def test_totals_under_short(self):
        assert extract_line("U 139.5", TOTALS) == 139.5

    def test_totals_over_word(self):
        assert extract_line("Over 2.5", OU) == 2.5

    def test_totals_under_word(self):
        assert extract_line("Under 23.5", OU) == 23.5

    def test_totals_event_level(self):
        assert extract_line("Duke vs NCST: O/U 148.5", TOTALS) == 148.5

    def test_total_sets(self):
        assert extract_line("Rublev vs. Royer: Total Sets O/U 2.5", TOTAL_SETS) == 2.5

    def test_set_games(self):
        assert extract_line("Set 1 Games O/U 8.5", TOTALS) == 8.5

    def test_match_ou(self):
        assert extract_line("Match O/U 21.5", TOTALS) == 21.5

    # --- Rounds O/U ---
    def test_rounds_ou(self):
        assert extract_line("O/U 2.5 Rounds", ROUNDS_OU) == 2.5

    def test_rounds_ou_singular(self):
        assert extract_line("O/U 1.5 Round", ROUNDS_OU) == 1.5

    # --- Over/Under fallback to event_title ---
    def test_bare_over_with_event_title(self):
        assert extract_line("Over", OU, event_title="Duke vs NCST: O/U 148.5") == 148.5

    def test_bare_under_with_event_title(self):
        assert extract_line("Under", OU, event_title="O/U 2.5 Goals") == 2.5

    # --- Non-line-bearing types ---
    def test_moneyline_returns_none(self):
        assert extract_line("Real Madrid vs Barcelona", MONEYLINE) is None

    def test_btts_returns_none(self):
        assert extract_line("Both Teams to Score", BTTS) is None


# ========== extract_line_label ==========

class TestExtractLineLabel:
    """Test line label formatting for dashboard display."""

    def test_spread_negative(self):
        assert extract_line_label("Spread: Team (-2.5)", SPREAD) == "-2.5"

    def test_spread_positive(self):
        assert extract_line_label("Spread: Team (+7.5)", SPREAD) == "+7.5"

    def test_totals_over(self):
        assert extract_line_label("O 148.5", TOTALS, outcome="Over") == "O 148.5"

    def test_totals_under(self):
        assert extract_line_label("U 139.5", TOTALS, outcome="Under") == "U 139.5"

    def test_total_sets_over(self):
        label = extract_line_label("Total Sets O/U 2.5", TOTAL_SETS, outcome="Over 2.5")
        assert label == "O 2.5"

    def test_rounds_ou(self):
        label = extract_line_label("O/U 2.5 Rounds", ROUNDS_OU, outcome="Over")
        assert label == "O 2.5 Rds"

    def test_non_line_bearing_returns_empty(self):
        assert extract_line_label("Real Madrid vs Barcelona", MONEYLINE) == ""


# ========== match_odds_to_poly_market ==========

class TestMatchOddsToPolyMarket:
    """Test cross-matching between odds-api markets and Polymarket markets."""

    # --- h2h ---
    def test_h2h_to_moneyline(self):
        assert match_odds_to_poly_market(
            "h2h", None, "Real Madrid vs Barcelona", "football"
        )

    def test_h2h_does_not_match_spread(self):
        assert not match_odds_to_poly_market(
            "h2h", None, "Spread: Team (-2.5)", "football"
        )

    # --- map_winner (esports; line = map #) ---
    def test_map_winner_matching(self):
        assert match_odds_to_poly_market(
            "map_winner", 1.0, "LoL: A vs B - Game 1 Winner", "lol"
        )

    def test_map_winner_wrong_map(self):
        assert not match_odds_to_poly_market(
            "map_winner", 2.0, "LoL: A vs B - Game 1 Winner", "lol"
        )

    def test_map_winner_does_not_match_series_moneyline(self):
        assert not match_odds_to_poly_market(
            "map_winner", 1.0, "Dota 2: A vs B", "dota2"
        )

    # --- spreads ---
    def test_spreads_matching_line(self):
        assert match_odds_to_poly_market(
            "spreads", -2.5, "Spread: Team (-2.5)", "football"
        )

    def test_spreads_matching_opposite_line(self):
        # Odds line is -2.5, poly shows +2.5 (other team's perspective) → still matches on abs
        assert match_odds_to_poly_market(
            "spreads", -2.5, "Spread: Team (+2.5)", "football"
        )

    def test_spreads_wrong_line(self):
        assert not match_odds_to_poly_market(
            "spreads", -1.5, "Spread: Team (-2.5)", "football"
        )

    def test_spreads_to_set_handicap(self):
        assert match_odds_to_poly_market(
            "spreads", -1.5, "Set Handicap: Tauson (-1.5) vs Linette (+1.5)", "tennis"
        )

    # --- totals ---
    def test_totals_matching_line(self):
        assert match_odds_to_poly_market(
            "totals", 2.5, "Real Madrid vs Barcelona: O/U 2.5", "football"
        )

    def test_totals_wrong_line(self):
        assert not match_odds_to_poly_market(
            "totals", 3.5, "Real Madrid vs Barcelona: O/U 2.5", "football"
        )

    def test_totals_to_total_sets(self):
        assert match_odds_to_poly_market(
            "totals", 2.5, "Rublev vs. Royer: Total Sets O/U 2.5", "tennis"
        )

    def test_totals_to_rounds_ou(self):
        assert match_odds_to_poly_market(
            "totals", 2.5, "O/U 2.5 Rounds", "ufc"
        )

    def test_totals_short_form(self):
        assert match_odds_to_poly_market(
            "totals", 148.5, "O 148.5", "ncaab"
        )

    # --- btts ---
    def test_btts_match(self):
        assert match_odds_to_poly_market(
            "btts", None, "Both Teams to Score", "football"
        )

    def test_btts_does_not_match_moneyline(self):
        assert not match_odds_to_poly_market(
            "btts", None, "Real Madrid vs Barcelona", "football"
        )

    # --- wrong type ---
    def test_unknown_odds_type(self):
        assert not match_odds_to_poly_market(
            "corners", None, "Real Madrid vs Barcelona", "football"
        )

    # --- h2h_h1 (halftime) ---
    def test_h2h_h1_to_halftime(self):
        assert match_odds_to_poly_market(
            "h2h_h1", None, "Kings: 1H Moneyline", "ncaab"
        )

    def test_h2h_h1_does_not_match_moneyline(self):
        assert not match_odds_to_poly_market(
            "h2h_h1", None, "Real Madrid vs Barcelona", "football"
        )

    def test_h2h_h1_halftime_result(self):
        assert match_odds_to_poly_market(
            "h2h_h1", None, "Halftime Result", "football"
        )


# ========== _clean_team_name ==========

class TestCleanTeamName:
    """Test team name cleaning for hydration."""

    def test_strip_1h_moneyline(self):
        from src.state.order_state import _clean_team_name
        assert _clean_team_name("Kings: 1H Moneyline") == "Kings"

    def test_strip_spread(self):
        from src.state.order_state import _clean_team_name
        assert _clean_team_name("Kings: Spread -1.5") == "Kings"

    def test_strip_ou(self):
        from src.state.order_state import _clean_team_name
        assert _clean_team_name("Kings: O/U 3.5") == "Kings"

    def test_no_strip_plain_name(self):
        from src.state.order_state import _clean_team_name
        assert _clean_team_name("Kings") == "Kings"
