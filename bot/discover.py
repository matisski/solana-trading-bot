"""Find several hundred Solana mints from free public lists.

Buy filters stay elsewhere. This module only collects addresses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from bot.config import SOL_MINT, USDC_MINT, USDT_MINT

# Stables, SOL, and wrapped or staked majors. Memecoins are everything else.
EXCLUDED_MINTS = {
    SOL_MINT,
    USDC_MINT,
    USDT_MINT,
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",  # USD1
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",  # soBTC
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",  # JLP
}

DEX_SEARCHES = (
    "SOL",
    "USDC",
    "pump",
    "meme",
    "pepe",
    "dog",
    "cat",
    "ai",
    "bonk",
    "raydium",
    "trump",
    "fart",
    "inu",
    "wif",
    "popcat",
    "penguin",
)

# GeckoTerminal allows about 30 calls a minute. Stay under that.
GECKO_GAP_SECONDS = 3.0
GECKO_VOLUME_PAGES = 8
GECKO_NEW_PAGES = 4
MAX_MINTS = 500
CACHE_SECONDS = 90


@dataclass
class Discovery:
    mints: list[str] = field(default_factory=list)
    counts: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class MintSearch:
    def __init__(self, http):
        self.http = http
        self._cache: tuple[float, Discovery] | None = None
        self._next_gecko = 0.0

    def discover(self) -> Discovery:
        if self._cache is not None:
            saved_at, cached = self._cache
            if time.monotonic() - saved_at < CACHE_SECONDS:
                return cached
        found = Discovery()
        ordered: list[str] = []
        seen: set[str] = set()
        self._dex_lists(found, ordered, seen)
        self._dex_search(found, ordered, seen)
        self._gecko(found, ordered, seen)
        found.mints = ordered
        self._cache = (time.monotonic(), found)
        return found

    def _add(self, ordered: list[str], seen: set[str], mint: str | None) -> bool:
        if not mint or mint in EXCLUDED_MINTS or mint in seen:
            return False
        if len(ordered) >= MAX_MINTS:
            return False
        seen.add(mint)
        ordered.append(mint)
        return True

    def _dex_lists(self, found: Discovery, ordered: list[str], seen: set[str]) -> None:
        lists = (
            ("DexScreener boosty top", "https://api.dexscreener.com/token-boosts/top/v1"),
            ("DexScreener boosty najnowsze", "https://api.dexscreener.com/token-boosts/latest/v1"),
            ("DexScreener profile", "https://api.dexscreener.com/token-profiles/latest/v1"),
        )
        for label, url in lists:
            data, err = self.http.get_json(url)
            added = 0
            if err or not isinstance(data, list):
                found.warnings.append(f"{label}: {err or 'zła odpowiedź'}")
            else:
                for item in data:
                    if not isinstance(item, dict) or item.get("chainId") != "solana":
                        continue
                    if self._add(ordered, seen, item.get("tokenAddress")):
                        added += 1
            found.counts.append((label, added))

    def _dex_search(self, found: Discovery, ordered: list[str], seen: set[str]) -> None:
        added = 0
        for query in DEX_SEARCHES:
            if len(ordered) >= MAX_MINTS:
                break
            data, err = self.http.get_json(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": query},
            )
            if err or not isinstance(data, dict):
                found.warnings.append(f"DexScreener szukaj {query}: {err or 'zła odpowiedź'}")
                continue
            for pair in data.get("pairs") or []:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                base = (pair.get("baseToken") or {}).get("address")
                quote = (pair.get("quoteToken") or {}).get("address")
                if self._add(ordered, seen, _memecoin(base, quote)):
                    added += 1
            time.sleep(0.15)
        found.counts.append(("DexScreener szukaj", added))

    def _gecko(self, found: Discovery, ordered: list[str], seen: set[str]) -> None:
        jobs = [("GeckoTerminal popularne", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", 1)]
        jobs.append(
            (
                "GeckoTerminal wolumen",
                "https://api.geckoterminal.com/api/v2/networks/solana/pools",
                GECKO_VOLUME_PAGES,
            )
        )
        jobs.append(
            (
                "GeckoTerminal nowe pule",
                "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
                GECKO_NEW_PAGES,
            )
        )
        for label, url, pages in jobs:
            added = 0
            for page in range(1, pages + 1):
                if len(ordered) >= MAX_MINTS:
                    break
                self._wait_gecko()
                params = {"page": page} if "trending" not in url else None
                if "pools" in url and "new_pools" not in url and "trending" not in url:
                    params = {"page": page, "sort": "h24_volume_usd_desc"}
                data, err = self.http.get_json(url, params=params, retry_429=False)
                if err == "HTTP 429":
                    self._next_gecko = time.monotonic() + 10
                    self._wait_gecko()
                    data, err = self.http.get_json(url, params=params, retry_429=False)
                if err or not isinstance(data, dict):
                    found.warnings.append(f"{label}: {err or 'zła odpowiedź'}")
                    break
                rows = data.get("data") or []
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    if self._add(ordered, seen, _gecko_mint(row)):
                        added += 1
                if len(rows) < 20:
                    break
            found.counts.append((label, added))

    def _wait_gecko(self) -> None:
        delay = self._next_gecko - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next_gecko = time.monotonic() + GECKO_GAP_SECONDS


def _memecoin(base: str | None, quote: str | None) -> str | None:
    if base and base not in EXCLUDED_MINTS:
        return base
    if quote and quote not in EXCLUDED_MINTS:
        return quote
    return None


def _gecko_mint(row: dict) -> str | None:
    if not isinstance(row, dict):
        return None
    rel = row.get("relationships") or {}
    base = _gecko_id((rel.get("base_token") or {}).get("data") or {})
    quote = _gecko_id((rel.get("quote_token") or {}).get("data") or {})
    return _memecoin(base, quote)


def _gecko_id(data: dict) -> str | None:
    raw = data.get("id") if isinstance(data, dict) else None
    if not isinstance(raw, str) or "_" not in raw:
        return None
    prefix, mint = raw.split("_", 1)
    if prefix != "solana" or not mint:
        return None
    return mint
