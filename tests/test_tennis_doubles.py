"""Tennis DOUBLES pair matching (match_id canonicalization).

A doubles pair is named differently by every book — Polymarket "Ruehl/Veldheer"
(surname/surname), source_d "Tim Ruehl/Mick Veldheer" (First Last), source_i
"Ruehl X / Veldheer Y" (Surname Initial), and the two players appear in different
order across books. These tests prove all formats collapse to ONE match_id
(so odds aggregate + the PM market matches), singles are untouched, and a doubles
key can never collide with a singles surname (the poisoning-safety the old
"skip doubles" guards used to provide).
"""
from src.core.match_id import (
    make_match_id,
    canonical_pair,
    _extract_surname,
)


def test_cross_source_same_match_id():
    # Ruehl/Veldheer vs Bondioli/Caniato — PM vs source_d, intra-pair order
    # differs (PM Bondioli/Caniato = BC Caniato/Bondioli).
    pm = make_match_id("Ruehl/Veldheer", "Bondioli/Caniato", "tennis")
    bc = make_match_id(
        "Tim Ruehl/Mick Veldheer",
        "Carlo Alberto Caniato/Federico Bondioli",
        "tennis",
    )
    assert pm == bc == "tennis:bondioli+caniato:vs:ruehl+veldheer"

    # PM vs source_i: initials + compound surname (Cavalle-Reimers).
    assert make_match_id("Grabher/Kraus", "Cavalle-Reimers/Pigossi", "tennis") == make_match_id(
        "Grabher J / Kraus S", "Cavalle-Reimers Y / Pigossi L", "tennis"
    )
    # PM vs source_i comma format ("Surname, First").
    assert make_match_id("Charaeva/Kulambayeva", "Garcia-Perez/Ibragimova", "tennis") == make_match_id(
        "Charaeva, Alina/Kulambayeva, Zhibek", "Garcia-Perez G / Ibragimova A", "tennis"
    )


def test_order_independent():
    a = make_match_id("Ruehl/Veldheer", "Bondioli/Caniato", "tennis")
    b = make_match_id("Veldheer/Ruehl", "Caniato/Bondioli", "tennis")  # both pairs swapped
    c = make_match_id("Bondioli/Caniato", "Ruehl/Veldheer", "tennis")  # sides swapped
    assert a == b == c


def test_singles_unaffected():
    # No '/', so the doubles path never fires — singles behave exactly as before.
    assert make_match_id("Jannik Sinner", "Carlos Alcaraz", "tennis") == make_match_id(
        "Sinner, Jannik", "Alcaraz, Carlos", "tennis"
    )
    assert "+" not in make_match_id("Jannik Sinner", "Carlos Alcaraz", "tennis")


def test_poisoning_safe():
    # A doubles key ("surname+surname") can never equal a singles surname, so a
    # doubles odds row can't false-match a singles market of the same name.
    dbl = make_match_id("Markovski/Srbljak", "Foo/Bar", "tennis")
    sgl = make_match_id("Markovski", "Srbljak", "tennis")
    assert "+" in dbl and "+" not in sgl
    assert dbl != sgl


def test_doubles_path_is_tennis_gated():
    # A '/' in a NON-tennis name keeps the old mentions-truncation behavior.
    assert "+" not in make_match_id("Iran / Nuclear", "Foo", "politics")


def test_surname_extraction():
    assert _extract_surname("Veldheer") == "veldheer"          # PM surname only
    assert _extract_surname("Mick Veldheer") == "veldheer"     # BC First Last
    assert _extract_surname("Herbert P-H") == "herbert"        # source_i Surname Initial
    assert _extract_surname("Cavalle-Reimers Y") == "cavallereimers"  # compound
    assert _extract_surname("Charaeva, Alina") == "charaeva"   # comma


def test_surname_first_order_disambiguated():
    # source_j lists players surname-first, inconsistently within a pair. Known
    # first names are stripped so the surname is found regardless of order —
    # including the Asian given-name case.
    assert _extract_surname("Gille Sander") == "gille"    # Surname First (Western)
    assert _extract_surname("Sander Gille") == "gille"    # First Last (same result)
    assert _extract_surname("Chan Hao-Ching") == "chan"   # Surname First (Asian given name)
    assert _extract_surname("Pierre-Hugues Herbert") == "herbert"  # hyphenated first name
    # The formerly-broken source_j pair now produces the reference key.
    assert make_match_id("Gille Sander/Sem Verbeek", "Dylan Dietrich/Dominic Stricker", "tennis") == \
        make_match_id("Gille/Verbeek", "Dietrich/Stricker", "tennis")


def test_canonical_pair_rejects_non_pairs():
    assert canonical_pair("Just One Name") is None        # no '/'
    assert canonical_pair("a/b/c") is None                # 3 players, not a clean pair
    assert canonical_pair("Ruehl/Veldheer") == "ruehl+veldheer"
