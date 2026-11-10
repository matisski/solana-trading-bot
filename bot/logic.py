"""Exit rules and fill math. No network calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExitDecision:
    action: str  # "hold" or "sell"
    sell_fraction: float
    reason: str


def decide_exit(
    entry: float,
    peak: float,
    price: float,
    took_partial: bool,
    take_profit_multiple: float,
    take_profit_sell_fraction: float,
    stop_loss_pct: float,
    trailing_stop_pct: float,
    stop_basis: float | None = None,
) -> ExitDecision:
    """One decision per run. Hard stop first, then take-profit, then trail.

    ``stop_basis`` is the executable price just after the buy (a sell quote).
    The buy itself is a worse price than that, so measuring the stop from the
    raw entry makes the spread look like a loss and trips the stop early.
    Take-profit stays on the price actually paid.
    """
    if entry <= 0 or price <= 0:
        return ExitDecision("hold", 0.0, "")
    basis = stop_basis if stop_basis is not None and stop_basis > 0 else entry
    if price <= basis * (1 - stop_loss_pct / 100):
        return ExitDecision("sell", 1.0, "stop_loss")
    if not took_partial and price >= entry * take_profit_multiple:
        fraction = 1.0 if take_profit_sell_fraction >= 0.999 else take_profit_sell_fraction
        return ExitDecision("sell", fraction, "take_profit")
    if peak > entry and price <= peak * (1 - trailing_stop_pct / 100):
        return ExitDecision("sell", 1.0, "trailing_stop")
    return ExitDecision("hold", 0.0, "")


def sell_raw_amount(amount_raw: int, fraction: float) -> int:
    if amount_raw <= 1 or fraction >= 0.999:
        return amount_raw
    raw = int(amount_raw * fraction)
    if raw <= 0 or raw >= amount_raw:
        return amount_raw
    return raw


def split_cost(cost_remaining: float, amount_raw: int, sell_raw: int) -> tuple[float, float]:
    if amount_raw <= 0:
        return cost_remaining, 0.0
    sold = cost_remaining * (sell_raw / amount_raw)
    return sold, cost_remaining - sold


def impact_percent(price_impact_pct) -> float:
    """Jupiter sends priceImpactPct as a fraction: 0.05 means 5%."""
    if price_impact_pct in (None, ""):
        return 0.0
    return abs(float(price_impact_pct)) * 100.0


def price_from_usdc(usdc_value: float, token_raw: int, decimals: int) -> float:
    ui = token_raw / (10**decimals)
    if ui <= 0:
        return 0.0
    return usdc_value / ui


def token_ui(token_raw: int, decimals: int) -> float:
    return token_raw / (10**decimals)
