"""RugCheck rules. A token is bought only when every check passes."""

from __future__ import annotations

from bot.config import Config

# LP burns and the system program. These are not "a person holding the supply".
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

_POOL_KEYS = (
    "pubkey",
    "mintA",
    "mintB",
    "mintLP",
    "mintAAccount",
    "mintBAccount",
    "mintLPAccount",
    "liquidityAAccount",
    "liquidityBAccount",
)


def evaluate_report(report: dict, cfg: Config) -> str | None:
    """Return a Polish skip reason, or None when the report is acceptable."""
    if report.get("rugged") is True:
        return "RugCheck oznacza ten token jako rug"

    try:
        score = float(report.get("score_normalised"))
    except (TypeError, ValueError):
        return "brak oceny RugCheck"
    if score > cfg.max_score_normalised:
        return (
            f"za wysokie ryzyko RugCheck ({score:.0f}, "
            f"limit {cfg.max_score_normalised:.0f})"
        )

    token = report.get("token") or {}
    if token.get("mintAuthority") or report.get("mintAuthority"):
        return "mint authority nie jest cofnięte (ktoś może dodrukować token)"
    if token.get("freezeAuthority") or report.get("freezeAuthority"):
        return "freeze authority nie jest cofnięte (ktoś może zamrozić portfele)"

    fee = report.get("transferFee") or {}
    try:
        fee_pct = float(fee.get("pct") or 0)
    except (TypeError, ValueError):
        fee_pct = 0.0
    if fee_pct > cfg.max_transfer_fee_pct:
        return f"za wysoka opłata przy przelewie ({fee_pct:.1f}%)"

    extensions = report.get("token_extensions") or {}
    if isinstance(extensions, dict):
        if extensions.get("nonTransferable"):
            return "tokenu nie da się przelać"
        if extensions.get("permanentDelegate"):
            return "ktoś ma stałe prawo zabrać token (permanent delegate)"

    pools = _pool_addresses(report)
    worst = 0.0
    for holder in report.get("topHolders") or []:
        if not isinstance(holder, dict):
            continue
        address = str(holder.get("address") or "")
        owner = str(holder.get("owner") or "")
        if address in pools or owner in pools or address in BURN_ADDRESSES or owner in BURN_ADDRESSES:
            continue
        try:
            pct = float(holder.get("pct") or 0)
        except (TypeError, ValueError):
            continue
        worst = max(worst, pct)
    if worst > cfg.max_top_holder_pct:
        return (
            f"jeden portfel ma za dużo tokenu ({worst:.1f}%, "
            f"limit {cfg.max_top_holder_pct:.0f}%)"
        )

    locked = _primary_lp_locked_pct(report)
    if locked is None:
        return "brak danych, czy płynność jest zablokowana albo spalona"
    if locked < cfg.min_lp_locked_pct:
        return (
            f"płynność nie jest zablokowana ({locked:.0f}%, "
            f"minimum {cfg.min_lp_locked_pct:.0f}%)"
        )
    return None


def _pool_addresses(report: dict) -> set[str]:
    found: set[str] = set()
    for market in report.get("markets") or []:
        if not isinstance(market, dict):
            continue
        for key in _POOL_KEYS:
            value = market.get(key)
            if isinstance(value, str) and len(value) > 30:
                found.add(value)
        lp = market.get("lp")
        if isinstance(lp, dict):
            for key in ("lpMint", "baseMint", "quoteMint"):
                value = lp.get(key)
                if isinstance(value, str) and value:
                    found.add(value)
    return found


def _primary_lp_locked_pct(report: dict) -> float | None:
    """Lock percent of the deepest pool, not of a tiny side pool."""
    best_pct = None
    best_usd = -1.0
    for market in report.get("markets") or []:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp")
        if not isinstance(lp, dict) or lp.get("lpLockedPct") is None:
            continue
        try:
            usd = float(lp.get("baseUSD") or 0) + float(lp.get("quoteUSD") or 0)
            pct = float(lp.get("lpLockedPct"))
        except (TypeError, ValueError):
            continue
        if usd > best_usd:
            best_usd = usd
            best_pct = pct
    return best_pct
