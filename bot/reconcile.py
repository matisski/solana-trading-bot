"""Match state.json to the tokens actually sitting in the wallet."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from bot.config import SOL_MINT, USDC_MINT

# Below this many dollars the balance is dust: a manual sell or a leftover
# account, not an open trade. The bot must not try to swap it.
MIN_POSITION_USD = 0.5
# A buy we just sent can take a moment to show up. Don't call it "sold" yet.
FRESH_MINUTES = 3


def read_trade_rows(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def is_meaningful(raw: int, decimals: int, value_usd: float | None) -> bool:
    if raw <= 0:
        return False
    if value_usd is None:
        ui = raw / (10**decimals) if decimals else float(raw)
        return ui >= 1
    return value_usd >= MIN_POSITION_USD


def is_fresh(opened_at: str | None, now: datetime, minutes: int = FRESH_MINUTES) -> bool:
    if not opened_at:
        return False
    try:
        opened = datetime.fromisoformat(opened_at)
    except ValueError:
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return now - opened < timedelta(minutes=minutes)


def estimate_entry(rows: list[dict], mint: str, ui_amount: float) -> dict:
    """Best cost basis we can rebuild from the trade log for what's left."""
    buys = []
    for row in rows:
        if (row.get("mint") or "") != mint:
            continue
        side = (row.get("strona") or "").upper()
        if side != "KUPNO":
            continue
        try:
            ui = float(row.get("ilosc_tokenow") or 0)
            cost = float(row.get("wartosc_usdc") or 0)
            price = float(row.get("cena_usd") or 0)
        except ValueError:
            continue
        if ui <= 0:
            continue
        buys.append(
            {
                "ui": ui,
                "cost": cost,
                "price": price or (cost / ui),
                "symbol": row.get("symbol") or "?",
                "time": row.get("czas_utc") or "",
            }
        )
    if not buys or ui_amount <= 0:
        return {"price": 0.0, "cost": 0.0, "symbol": "?", "opened_at": ""}
    for buy in reversed(buys):
        if abs(buy["ui"] - ui_amount) / ui_amount <= 0.05:
            price = buy["price"]
            return {
                "price": price,
                "cost": price * ui_amount,
                "symbol": buy["symbol"],
                "opened_at": buy["time"],
            }
    spent = sum(item["cost"] for item in buys)
    received = sum(item["ui"] for item in buys)
    price = spent / received if received else buys[-1]["price"]
    return {
        "price": price,
        "cost": price * ui_amount,
        "symbol": buys[-1]["symbol"],
        "opened_at": buys[-1]["time"],
    }


def usdc_in_for_exit(
    transactions: list[dict],
    owner: str,
    mint: str,
    known_sigs: set[str],
    opened_at: str | None = None,
) -> float | None:
    """USDC received for this mint after the position opened, ignoring bot sells already logged.

    ``transactions`` must be newest first. Older lots of the same token are not
    counted again.
    """
    opened_ts = _opened_timestamp(opened_at)
    total = 0.0
    found = False
    for tx in transactions:
        signature = tx.get("signature") or ""
        if signature in known_sigs:
            continue
        block_time = tx.get("blockTime")
        if opened_ts is not None and isinstance(block_time, (int, float)) and block_time < opened_ts:
            break
        mint_delta, usdc_delta = _owner_deltas(tx.get("meta") or {}, owner, mint)
        if mint_delta < 0 and usdc_delta > 0:
            total += usdc_delta
            found = True
    return total if found else None


def _opened_timestamp(opened_at: str | None) -> float | None:
    if not opened_at:
        return None
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return opened.timestamp()


def _owner_deltas(meta: dict, owner: str, mint: str) -> tuple[float, float]:
    mint_delta = 0.0
    usdc_delta = 0.0
    pre = {(b.get("accountIndex"), b.get("mint")): b for b in meta.get("preTokenBalances") or []}
    post = {(b.get("accountIndex"), b.get("mint")): b for b in meta.get("postTokenBalances") or []}
    for key in set(pre) | set(post):
        before = pre.get(key) or {}
        after = post.get(key) or {}
        who = (after or before).get("owner")
        if who != owner:
            continue
        token = (after or before).get("mint")
        pre_ui = float(((before.get("uiTokenAmount") or {}).get("uiAmount")) or 0)
        post_ui = float(((after.get("uiTokenAmount") or {}).get("uiAmount")) or 0)
        delta = post_ui - pre_ui
        if token == mint:
            mint_delta += delta
        elif token == USDC_MINT:
            usdc_delta += delta
    return mint_delta, usdc_delta


def union_state(left: dict, right: dict) -> dict:
    """Keep every mint from both saves. Same mint: keep the larger balance."""
    if (left.get("last_run_at") or "") >= (right.get("last_run_at") or ""):
        base, other = left, right
    else:
        base, other = right, left
    by_mint: dict[str, dict] = {}
    for pos in list(base.get("positions") or []) + list(other.get("positions") or []):
        mint = pos.get("mint")
        if not mint:
            continue
        current = by_mint.get(mint)
        if current is None or int(pos.get("amount_raw") or 0) > int(current.get("amount_raw") or 0):
            by_mint[mint] = pos
    merged = json.loads(json.dumps(base))
    merged["positions"] = list(by_mint.values())
    cooldowns = dict(other.get("cooldowns") or {})
    cooldowns.update(base.get("cooldowns") or {})
    merged["cooldowns"] = cooldowns
    return merged


def union_csv(left: str, right: str) -> str:
    rows = []
    seen = set()
    header = None
    for text in (left, right):
        reader = csv.DictReader(StringIO(text))
        if reader.fieldnames and header is None:
            header = list(reader.fieldnames)
        for row in reader:
            key = (
                row.get("czas_utc"),
                row.get("strona"),
                row.get("mint"),
                row.get("podpis"),
                row.get("wartosc_usdc"),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    if not header:
        return left or right
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def split_conflict(text: str) -> tuple[str, str] | None:
    if "<<<<<<<" not in text:
        return None
    ours: list[str] = []
    theirs: list[str] = []
    mode = ""
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<<"):
            mode = "ours"
            continue
        if line.startswith("======="):
            mode = "theirs"
            continue
        if line.startswith(">>>>>>>"):
            mode = ""
            continue
        if mode == "ours":
            ours.append(line)
        elif mode == "theirs":
            theirs.append(line)
        else:
            ours.append(line)
            theirs.append(line)
    return "".join(ours), "".join(theirs)


def ignored_mint(mint: str, usdc_mint: str) -> bool:
    return mint in {usdc_mint, SOL_MINT}
