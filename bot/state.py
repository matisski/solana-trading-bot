"""Positions, daily PnL and the CSV trade log."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRADE_FIELDS = [
    "czas_utc",
    "tryb",
    "strona",
    "mint",
    "symbol",
    "ilosc_tokenow",
    "cena_usd",
    "wartosc_usdc",
    "zysk_usdc",
    "powod",
    "podpis",
    "solscan",
]


class StateError(Exception):
    pass


def empty_state() -> dict:
    return {
        "positions": [],
        "cooldowns": {},
        "daily": {
            "date_utc": "",
            "live_realized_pnl_usd": 0.0,
            "paper_realized_pnl_usd": 0.0,
        },
        "last_run_at": None,
    }


class State:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls(path, empty_state())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"state/state.json jest uszkodzony: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError("state/state.json ma zły format.")
        data.setdefault("positions", [])
        data.setdefault("cooldowns", {})
        daily = data.setdefault("daily", {})
        daily.setdefault("date_utc", "")
        daily.setdefault("live_realized_pnl_usd", 0.0)
        daily.setdefault("paper_realized_pnl_usd", 0.0)
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")

    def roll_day(self, today: str) -> None:
        if self.data["daily"].get("date_utc") != today:
            self.data["daily"] = {
                "date_utc": today,
                "live_realized_pnl_usd": 0.0,
                "paper_realized_pnl_usd": 0.0,
            }

    def realized(self, mode: str) -> float:
        key = _pnl_key(mode)
        return float(self.data["daily"].get(key) or 0)

    def add_realized(self, mode: str, pnl: float) -> None:
        key = _pnl_key(mode)
        current = float(self.data["daily"].get(key) or 0)
        self.data["daily"][key] = round(current + pnl, 4)

    def positions_for(self, mode: str) -> list[dict]:
        return [pos for pos in self.data["positions"] if pos.get("mode") == mode]

    def holds(self, mint: str) -> bool:
        return any(pos.get("mint") == mint for pos in self.data["positions"])

    def remove_position(self, mint: str, mode: str) -> None:
        self.data["positions"] = [
            pos
            for pos in self.data["positions"]
            if not (pos.get("mint") == mint and pos.get("mode") == mode)
        ]

    def on_cooldown(self, mint: str, now: datetime, minutes: float) -> bool:
        raw = (self.data.get("cooldowns") or {}).get(mint)
        closed = _parse_time(raw)
        if closed is None:
            return False
        return now - closed < timedelta(minutes=minutes)

    def prune_cooldowns(self, now: datetime, minutes: float) -> None:
        kept = {}
        for mint, raw in (self.data.get("cooldowns") or {}).items():
            closed = _parse_time(raw)
            if closed is not None and now - closed < timedelta(minutes=minutes):
                kept[mint] = raw
        self.data["cooldowns"] = kept

    def mark_cooldown(self, mint: str, now: datetime) -> None:
        self.data.setdefault("cooldowns", {})[mint] = now.isoformat()


def append_trade(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = (not path.exists()) or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS, lineterminator="\n")
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in TRADE_FIELDS})


def _pnl_key(mode: str) -> str:
    return "live_realized_pnl_usd" if mode == "live" else "paper_realized_pnl_usd"


def _parse_time(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
