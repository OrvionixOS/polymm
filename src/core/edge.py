"""
One place that decides whether a trade has edge, net of fees.

Before this, `edge = fair - entry_price` was written inline at several sites
in the scanners and `min_edge` was read in a dozen modules, with no fee term
anywhere in `src/`. On the international Polymarket that was harmless —
trading fees are zero there — so the omission cost nothing and stayed
invisible. It stops being harmless the moment the venue charges anything:
every edge is then overstated by exactly the fee, and the bot trades
positions it believes are profitable and are not.

`config.py` still carries `"min_profit": 0.10  # 10c minimum expected profit
(after fees)`. Nothing subtracted a fee, so that comment described an
intention rather than the code.

The design rule here is the one that matters most: **an unmodelled fee is
never treated as zero.** A venue whose schedule has not been established
returns FEE_UNKNOWN and no trade clears, rather than silently reusing
Polymarket's zero. Guessing a schedule would produce confident numbers that
are wrong in the direction that looks like profit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


# Prices move in whole cents, so binary floating point is the only thing that
# can put a comparison a hair off. 0.62 - 0.55 evaluates to 0.06999999999999995,
# which is below a 0.07 threshold and would reject a trade that exactly clears
# it — reporting, absurdly, a 0.00c shortfall. This tolerance is orders of
# magnitude below one cent, so it absorbs the representation error without
# moving any real decision.
EPSILON = 1e-9


class EdgeVerdict(Enum):
    """Why a candidate trade is or is not worth taking."""

    CLEARS = "clears"
    BELOW_THRESHOLD = "below_threshold"
    NEGATIVE_AFTER_FEES = "negative_after_fees"
    FEE_UNKNOWN = "fee_unknown"
    NO_FAIR_VALUE = "no_fair_value"
    INVALID_PRICE = "invalid_price"


@runtime_checkable
class FeeModel(Protocol):
    """A venue's trading fee.

    `total_fee` returns dollars for the whole order, not per contract,
    because at least one venue rounds the order total up to the next cent —
    a per-contract abstraction would misprice small orders.
    """

    name: str

    def total_fee(self, price: float, contracts: float, *, is_maker: bool) -> Optional[float]:
        """Fee in dollars, or None if this venue's schedule is not established."""
        ...


@dataclass(frozen=True)
class ZeroFeeModel:
    """A venue that charges nothing to trade.

    True of the international Polymarket CLOB. Stated explicitly rather than
    assumed, so that choosing it is a decision in the code someone can find.
    """

    name: str = "zero"

    def total_fee(self, price: float, contracts: float, *, is_maker: bool) -> Optional[float]:
        return 0.0


@dataclass(frozen=True)
class UnknownFeeModel:
    """A venue whose fee schedule has not been established.

    Deliberately not zero. Every decision made with this model returns
    FEE_UNKNOWN, so an unverified venue cannot quietly inherit Polymarket's
    free trading and start booking phantom edge.
    """

    name: str = "unknown"

    def total_fee(self, price: float, contracts: float, *, is_maker: bool) -> Optional[float]:
        return None


@dataclass(frozen=True)
class KalshiFeeModel:
    """Kalshi: 0.07 x contracts x P x (1-P), rounded up to the next cent.

    Makers pay nothing on a standard series. The taker curve is parabolic and
    peaks at a 50c price — 1.75c per contract, a quarter of a 7c edge — so
    where a trade sits on the price range matters as much as its size.

    `maker_multiplier` and `series_multiplier` exist because Kalshi varies
    both by series; the defaults describe a standard series.
    """

    name: str = "kalshi"
    rate: float = 0.07
    series_multiplier: float = 1.0
    maker_multiplier: float = 0.0

    def total_fee(self, price: float, contracts: float, *, is_maker: bool) -> Optional[float]:
        if contracts <= 0:
            return 0.0
        multiplier = self.maker_multiplier if is_maker else self.series_multiplier
        if multiplier == 0:
            return 0.0
        raw = self.rate * multiplier * contracts * price * (1 - price)
        # Rounded up to the next cent on the order total. The inner round()
        # is load-bearing: 0.07 * 100 * 0.5 * 0.5 evaluates to
        # 1.7500000000000002, and ceiling that directly bills 1.76 instead of
        # 1.75 — the ceiling turns a 1e-16 representation error into a full
        # cent of fee on every mid-priced order.
        return math.ceil(round(raw * 100, 9)) / 100


@dataclass(frozen=True)
class ProportionalFeeModel:
    """A flat proportional fee, for a venue that charges one.

    `rate` is a fraction of notional. This exists so a schedule can be
    supplied once it is known, instead of someone reaching for ZeroFeeModel
    because it is the only concrete option to hand.
    """

    rate: float
    name: str = "proportional"
    maker_rate: Optional[float] = None

    def total_fee(self, price: float, contracts: float, *, is_maker: bool) -> Optional[float]:
        if contracts <= 0:
            return 0.0
        rate = self.maker_rate if (is_maker and self.maker_rate is not None) else self.rate
        return rate * contracts * price


@dataclass(frozen=True)
class EdgeDecision:
    """The full arithmetic, not just a boolean.

    Every field is kept so a rejection can be explained after the fact —
    "below threshold by 0.4c" and "negative once fees are paid" are different
    problems with different fixes, and a bare False hides which happened.
    """

    verdict: EdgeVerdict
    threshold: float
    gross_edge: Optional[float] = None
    fee_per_contract: Optional[float] = None
    net_edge: Optional[float] = None
    fee_model: str = ""
    detail: str = ""

    @property
    def clears(self) -> bool:
        return self.verdict is EdgeVerdict.CLEARS

    def __str__(self) -> str:
        if self.gross_edge is None:
            return f"{self.verdict.value}: {self.detail}"
        net = "n/a" if self.net_edge is None else f"{self.net_edge * 100:.2f}c"
        fee = "n/a" if self.fee_per_contract is None else f"{self.fee_per_contract * 100:.2f}c"
        return (
            f"{self.verdict.value}: gross {self.gross_edge * 100:.2f}c "
            f"- fee {fee} = net {net} vs threshold {self.threshold * 100:.2f}c "
            f"[{self.fee_model}]"
        )


def evaluate_edge(
    fair_value: Optional[float],
    entry_price: Optional[float],
    *,
    threshold: float,
    fees: FeeModel,
    contracts: float = 1.0,
    is_maker: bool = True,
    legs: int = 1,
) -> EdgeDecision:
    """Decide whether a candidate trade clears `threshold` net of fees.

    `fair_value` and `entry_price` are probabilities in dollars (0-1), the
    convention the scanners already use: a 55c price is 0.55.

    `is_maker` defaults to True because this bot posts inside the spread
    (`spread_bot` quotes at best_bid + 1c) rather than crossing. That default
    is generous, so a taking path must say so explicitly.

    `legs` covers a hedged position paying a fee on entry and again on the
    hedge. Fees are charged per leg, so a round trip is `legs=2`.
    """
    if fair_value is None:
        return EdgeDecision(
            EdgeVerdict.NO_FAIR_VALUE, threshold, fee_model=fees.name,
            detail="no fair value for this market; nothing to compare the price against",
        )
    if entry_price is None or not (0 < entry_price < 1):
        return EdgeDecision(
            EdgeVerdict.INVALID_PRICE, threshold, fee_model=fees.name,
            detail=f"entry price {entry_price!r} outside (0, 1)",
        )
    if not (0 < fair_value < 1):
        return EdgeDecision(
            EdgeVerdict.NO_FAIR_VALUE, threshold, fee_model=fees.name,
            detail=f"fair value {fair_value!r} outside (0, 1)",
        )
    if legs < 1:
        raise ValueError(f"legs must be at least 1, got {legs}")

    gross_edge = fair_value - entry_price

    total = fees.total_fee(entry_price, contracts, is_maker=is_maker)
    if total is None:
        return EdgeDecision(
            EdgeVerdict.FEE_UNKNOWN, threshold, gross_edge=gross_edge,
            fee_model=fees.name,
            detail=(
                f"{fees.name} fee schedule is not established; refusing to treat "
                "an unmodelled fee as zero"
            ),
        )

    per_contract = (total / contracts if contracts > 0 else 0.0) * legs
    net_edge = gross_edge - per_contract

    if net_edge <= -EPSILON:
        verdict = EdgeVerdict.NEGATIVE_AFTER_FEES
        detail = f"fees consume the whole {gross_edge * 100:.2f}c gross edge"
    elif net_edge < threshold - EPSILON:
        verdict = EdgeVerdict.BELOW_THRESHOLD
        detail = f"short of threshold by {(threshold - net_edge) * 100:.2f}c"
    else:
        verdict = EdgeVerdict.CLEARS
        detail = ""

    return EdgeDecision(
        verdict=verdict, threshold=threshold, gross_edge=gross_edge,
        fee_per_contract=per_contract, net_edge=net_edge,
        fee_model=fees.name, detail=detail,
    )


def fee_free_threshold(
    threshold: float,
    price: float,
    *,
    fees: FeeModel,
    is_maker: bool = True,
    legs: int = 1,
) -> Optional[float]:
    """The gross edge needed to clear `threshold` net, at this price.

    Useful for scanning: a scanner that filters on gross edge can raise its
    bar to this instead of discovering the shortfall one trade at a time.
    Returns None when the venue's fees are not established.
    """
    total = fees.total_fee(price, 1.0, is_maker=is_maker)
    if total is None:
        return None
    return threshold + total * legs
