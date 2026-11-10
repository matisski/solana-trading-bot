"""Cheap DexScreener filters. RugCheck and Jupiter run only after these pass."""

from __future__ import annotations

from bot.config import Config


def best_pair(pairs: list[dict], mint: str, allowed_quotes: set[str]) -> dict | None:
    best = None
    best_liq = -1.0
    for pair in pairs:
        if pair.get("chainId") not in (None, "solana"):
            continue
        base = (pair.get("baseToken") or {}).get("address")
        quote = (pair.get("quoteToken") or {}).get("address")
        if base != mint or quote not in allowed_quotes:
            continue
        try:
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        if liq > best_liq:
            best = pair
            best_liq = liq
    return best


def screen_pair(pair: dict, cfg: Config, now_ms: int) -> str | None:
    """Return a Polish skip reason, or None when the pair is allowed."""
    try:
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError):
        liq = 0.0
    if liq < cfg.min_liquidity_usd:
        return f"za mała płynność ({liq:.0f} USD)"

    created = pair.get("pairCreatedAt")
    if not created:
        return "brak wieku pary"
    try:
        age_min = (now_ms - int(created)) / 60_000
    except (TypeError, ValueError):
        return "brak wieku pary"
    if age_min < cfg.min_pair_age_minutes:
        return (
            f"para za młoda ({max(age_min, 0):.0f} min, "
            f"minimum {cfg.min_pair_age_minutes:.0f})"
        )

    hour = (pair.get("txns") or {}).get("h1") or {}
    try:
        buys = int(hour.get("buys") or 0)
        sells = int(hour.get("sells") or 0)
    except (TypeError, ValueError):
        return "brak danych o transakcjach"
    if buys < cfg.min_buys_h1:
        return f"za mało kupna w ostatniej godzinie ({buys})"
    ratio = sells / max(buys, 1)
    if ratio > cfg.max_sell_to_buy_ratio_h1:
        return f"za dużo sprzedaży wobec kupna ({ratio:.2f})"

    try:
        volume = float((pair.get("volume") or {}).get("h1") or 0)
    except (TypeError, ValueError):
        volume = 0.0
    if volume < cfg.min_volume_h1_usd:
        return f"za mały obrót godzinowy ({volume:.0f} USD)"

    change = (pair.get("priceChange") or {}).get("h1")
    if change is None:
        return "brak zmiany ceny"
    try:
        change_pct = float(change)
    except (TypeError, ValueError):
        return "brak zmiany ceny"
    if change_pct < cfg.min_price_change_h1_pct:
        return f"słaby ruch w górę ({change_pct:.1f}% w godzinę)"
    if change_pct > cfg.max_price_change_h1_pct:
        return f"już zbyt mocno urosło ({change_pct:.0f}% w godzinę)"
    return None
