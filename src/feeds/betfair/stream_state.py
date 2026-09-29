"""
State machine over Betfair Exchange Stream API market-change messages.

Pure: no sockets, no clock, no Supabase. Feed it `mcm` messages, ask it for
market books. That keeps the fiddly part — delta application — testable
without credentials, which matters because the free Delayed App Key and the
paid Live App Key deliver the *same* message format at different conflation
rates, so everything here is verified before anyone pays for anything.

The format has three properties that bite if you treat it as a snapshot feed:

  1. `atb`/`atl` are DELTAS, not snapshots. A price level with size 0 means
     "this level is gone", not "there is zero money here". Dropping that
     distinction leaves phantom liquidity in the book forever.
  2. `img: true` on a MarketChange means REPLACE, not merge. Merging an image
     into stale state silently resurrects removed price levels.
  3. Runner names are NOT in the stream. `RunnerDefinition` carries only
     `id` (selectionId) and `sortPriority`, so names must come from
     `listMarketCatalogue` out of band.

Two ladder encodings exist depending on what the subscription asked for:
`atb`/`atl` are (price, size) pairs for the full ladder, `batb`/`batl` are
(level, price, size) triples for the best few. Both are supported; a market
that carries both prefers the full ladder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

# Betfair's price ladder runs 1.01 to 1000. A price outside that is either a
# LINE/RANGE market (different semantics entirely) or corrupt.
MIN_PRICE = 1.01
MAX_PRICE = 1000.0


@dataclass
class RunnerBook:
    """One selection's side of the book.

    Ladders are kept keyed so deltas can be applied and removals honoured:
    `_full_*` by price (from atb/atl), `_level_*` by level index
    (from batb/batl).
    """

    selection_id: int
    status: Optional[str] = None
    sort_priority: Optional[int] = None
    last_traded_price: Optional[float] = None
    _full_back: dict[float, float] = field(default_factory=dict)
    _full_lay: dict[float, float] = field(default_factory=dict)
    _level_back: dict[int, tuple[float, float]] = field(default_factory=dict)
    _level_lay: dict[int, tuple[float, float]] = field(default_factory=dict)

    def clear_prices(self) -> None:
        self._full_back.clear()
        self._full_lay.clear()
        self._level_back.clear()
        self._level_lay.clear()

    # ── Delta application ──────────────────────────────────────────────

    def apply_full(self, ladder: str, entries: Iterable[Sequence[Any]]) -> None:
        """Apply an atb/atl delta: (price, size) pairs, size 0 removes."""
        target = self._full_back if ladder == "back" else self._full_lay
        for entry in entries:
            if len(entry) < 2:
                continue
            price, size = _as_float(entry[0]), _as_float(entry[1])
            if price is None or size is None:
                continue
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size

    def apply_level(self, ladder: str, entries: Iterable[Sequence[Any]]) -> None:
        """Apply a batb/batl delta: (level, price, size) triples.

        A triple with size 0 clears that level. Betfair also sends price 0
        with size 0 for a cleared level, so the price is not trusted there.
        """
        target = self._level_back if ladder == "back" else self._level_lay
        for entry in entries:
            if len(entry) < 3:
                continue
            level = _as_int(entry[0])
            price, size = _as_float(entry[1]), _as_float(entry[2])
            if level is None or price is None or size is None:
                continue
            if size == 0:
                target.pop(level, None)
            else:
                target[level] = (price, size)

    # ── Best prices ────────────────────────────────────────────────────
    #
    # "Best available to back" is the HIGHEST price you can back at (best
    # payout); "best available to lay" is the LOWEST price you can lay at.
    # On a two-sided book best_back < best_lay, and the true probability sits
    # between their reciprocals.

    def best_back(self) -> Optional[float]:
        tradeable = [p for p in self._full_back if _tradeable(p)]
        if tradeable:
            return max(tradeable)
        # Falls through when the full ladder holds nothing tradeable — a
        # ladder of out-of-range prices must not mask level data, and must
        # not raise on an empty selection either.
        return self._best_from_levels(self._level_back)

    def best_lay(self) -> Optional[float]:
        tradeable = [p for p in self._full_lay if _tradeable(p)]
        if tradeable:
            return min(tradeable)
        return self._best_from_levels(self._level_lay)

    @staticmethod
    def _best_from_levels(levels: Mapping[int, tuple[float, float]]) -> Optional[float]:
        """Level 0 is best, so take the lowest populated level index."""
        candidates = [
            (level, price) for level, (price, _) in levels.items() if _tradeable(price)
        ]
        if not candidates:
            return None
        return min(candidates)[1]

    @property
    def is_two_sided(self) -> bool:
        return self.best_back() is not None and self.best_lay() is not None


@dataclass
class MarketBook:
    """One market's definition plus its runners' books."""

    market_id: str
    definition: dict[str, Any] = field(default_factory=dict)
    runners: dict[int, RunnerBook] = field(default_factory=dict)
    publish_time_ms: Optional[int] = None
    conflate_ms: Optional[int] = None

    @property
    def market_type(self) -> Optional[str]:
        return self.definition.get("marketType")

    @property
    def event_id(self) -> Optional[str]:
        return self.definition.get("eventId")

    @property
    def status(self) -> Optional[str]:
        return self.definition.get("status")

    @property
    def in_play(self) -> bool:
        return bool(self.definition.get("inPlay", False))

    @property
    def is_open(self) -> bool:
        """Only an OPEN market is quotable.

        SUSPENDED means prices are frozen mid-event and CLOSED means settled;
        emitting either as a live observation would present a stale or final
        price as a current one.
        """
        return self.status == "OPEN"

    def active_runners(self) -> tuple[RunnerBook, ...]:
        """Runners still in contention, in Betfair's own display order.

        REMOVED runners (a withdrawn horse, a cancelled selection) must not
        enter a probability normalization: their prices linger but their
        outcome can no longer happen.
        """
        active = [
            r for r in self.runners.values()
            if r.status is None or r.status == "ACTIVE"
        ]
        return tuple(sorted(active, key=_runner_sort_key))

    def runner_ids_missing_prices(self) -> tuple[int, ...]:
        return tuple(
            sorted(r.selection_id for r in self.active_runners() if not r.is_two_sided)
        )


class StreamState:
    """Accumulates market state from a sequence of `mcm` messages."""

    def __init__(self) -> None:
        self.markets: dict[str, MarketBook] = {}
        self.last_publish_time_ms: Optional[int] = None
        self.last_conflate_ms: Optional[int] = None
        self.last_clk: Optional[str] = None
        self.initial_clk: Optional[str] = None
        self.stream_status: Optional[int] = None

    # ── Ingest ─────────────────────────────────────────────────────────

    def apply(self, message: Mapping[str, Any]) -> tuple[str, ...]:
        """Apply one stream message; return ids of markets whose state changed.

        Non-`mcm` operations (connection, status, order changes) and
        heartbeats carry no market data and return ().
        """
        if message.get("op") != "mcm":
            return ()

        # clk/initialClk are the resume tokens. They are tracked even on a
        # heartbeat, because a reconnect without the latest clk replays from
        # the wrong point.
        if message.get("initialClk") is not None:
            self.initial_clk = message["initialClk"]
        if message.get("clk") is not None:
            self.last_clk = message["clk"]
        if message.get("status") is not None:
            self.stream_status = _as_int(message["status"])

        publish_time = _as_int(message.get("pt"))
        if publish_time is not None:
            self.last_publish_time_ms = publish_time
        conflate = _as_int(message.get("conflateMs"))
        if conflate is not None:
            self.last_conflate_ms = conflate

        if message.get("ct") == "HEARTBEAT":
            return ()

        changed: list[str] = []
        for change in message.get("mc") or ():
            market_id = change.get("id")
            if not market_id:
                continue
            self._apply_market_change(str(market_id), change, publish_time, conflate)
            changed.append(str(market_id))
        return tuple(changed)

    def _apply_market_change(
        self,
        market_id: str,
        change: Mapping[str, Any],
        publish_time: Optional[int],
        conflate: Optional[int],
    ) -> None:
        book = self.markets.get(market_id)
        if book is None:
            book = MarketBook(market_id=market_id)
            self.markets[market_id] = book

        # An image replaces prices outright. Merging one in would leave
        # removed price levels alive.
        if change.get("img"):
            for runner in book.runners.values():
                runner.clear_prices()

        definition = change.get("marketDefinition")
        if isinstance(definition, Mapping):
            book.definition = dict(definition)
            self._apply_runner_definitions(book, definition.get("runners") or ())

        for runner_change in change.get("rc") or ():
            self._apply_runner_change(book, runner_change)

        if publish_time is not None:
            book.publish_time_ms = publish_time
        if conflate is not None:
            book.conflate_ms = conflate

    @staticmethod
    def _apply_runner_definitions(
        book: MarketBook, definitions: Iterable[Mapping[str, Any]]
    ) -> None:
        for definition in definitions:
            selection_id = _as_int(definition.get("id"))
            if selection_id is None:
                continue
            runner = book.runners.get(selection_id)
            if runner is None:
                runner = RunnerBook(selection_id=selection_id)
                book.runners[selection_id] = runner
            if definition.get("status") is not None:
                runner.status = str(definition["status"])
            sort_priority = _as_int(definition.get("sortPriority"))
            if sort_priority is not None:
                runner.sort_priority = sort_priority

    @staticmethod
    def _apply_runner_change(book: MarketBook, change: Mapping[str, Any]) -> None:
        selection_id = _as_int(change.get("id"))
        if selection_id is None:
            return
        runner = book.runners.get(selection_id)
        if runner is None:
            runner = RunnerBook(selection_id=selection_id)
            book.runners[selection_id] = runner

        if change.get("atb"):
            runner.apply_full("back", change["atb"])
        if change.get("atl"):
            runner.apply_full("lay", change["atl"])
        if change.get("batb"):
            runner.apply_level("back", change["batb"])
        if change.get("batl"):
            runner.apply_level("lay", change["batl"])

        ltp = _as_float(change.get("ltp"))
        if ltp is not None:
            runner.last_traded_price = ltp


# ── Helpers ────────────────────────────────────────────────────────────────

def _tradeable(price: float) -> bool:
    return MIN_PRICE <= price <= MAX_PRICE


def _runner_sort_key(runner: RunnerBook) -> tuple[int, int]:
    """Betfair's display order, with unknown priorities sorted last.

    Order matters: it is how home and away are told apart in a MATCH_ODDS
    market, since the stream carries no names.
    """
    if runner.sort_priority is None:
        return (1, runner.selection_id)
    return (0, runner.sort_priority)


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
