"""Runtime feature flags shared by the scrapers and (conceptually) the bot.

Kept tiny + dependency-free so any scraper can import it."""
import os


def include_tennis_doubles() -> bool:
    """Collect tennis DOUBLES odds. OFF by default — opt-in via
    ``TRADE_TENNIS_DOUBLES=1``, matching the Rust scanner's TRADE_TENNIS_DOUBLES
    discovery gate so odds collection and trading turn on together. Doubles pairs
    canonicalize to a surname-set match_id (see match_id.canonical_pair) that can
    never collide with a singles surname, so collecting them is safe."""
    return os.getenv("TRADE_TENNIS_DOUBLES", "").strip().lower() in ("1", "true")
