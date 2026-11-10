"""Load desk.yaml and decide whether this run is paper or live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# Official Circle USDC on Solana. The string the owner pasted
# (…M2qcJmM7wPEpkCSXzE5vbtCCzSs6) is not an account on mainnet.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

TRUE_VALUES = {"1", "true", "yes", "y", "tak", "on"}


class ConfigError(Exception):
    """desk.yaml is missing or has a value the bot will not trade with."""


@dataclass
class Config:
    usdc_mint: str
    usdc_decimals: int
    position_size_usd: float
    max_open_positions: int
    max_new_buys_per_run: int
    daily_loss_limit_usd: float
    cooldown_minutes: float
    take_profit_multiple: float
    take_profit_sell_fraction: float
    stop_loss_pct: float
    trailing_stop_pct: float
    sol_fee_reserve: float
    priority_fee_max_lamports: int
    min_pair_age_minutes: float
    min_liquidity_usd: float
    min_volume_h1_usd: float
    max_sell_to_buy_ratio_h1: float
    min_buys_h1: int
    min_price_change_h1_pct: float
    max_price_change_h1_pct: float
    max_m5_drop_pct: float
    max_sell_to_buy_ratio_m5: float
    allowed_quote_mints: list[str]
    max_score_normalised: float
    max_top_holder_pct: float
    min_lp_locked_pct: float
    max_transfer_fee_pct: float
    slippage_bps: int
    max_price_impact_pct: float
    min_roundtrip_pct: float
    max_full_checks_per_run: int
    request_timeout_seconds: float
    confirm_timeout_seconds: float
    jupiter_quote_url: str
    jupiter_swap_url: str
    rpc_fallback: str

    @property
    def position_size_raw(self) -> int:
        return int(round(self.position_size_usd * (10**self.usdc_decimals)))


def trading_mode(environ: dict[str, str] | None = None) -> str:
    """Live when a private key is present, unless DRY_RUN is true."""
    env = os.environ if environ is None else environ
    dry = str(env.get("DRY_RUN", "")).strip().lower()
    if dry in TRUE_VALUES:
        return "paper"
    if not str(env.get("SOLANA_PRIVATE_KEY", "")).strip():
        return "paper"
    return "live"


def paper_reason(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    dry = str(env.get("DRY_RUN", "")).strip().lower()
    if dry in TRUE_VALUES:
        return (
            "Zmienna DRY_RUN jest ustawiona na true. "
            "Bot nic nie wysyła na Solanę."
        )
    return (
        "Brak sekretu SOLANA_PRIVATE_KEY. "
        "Bot działa próbnie i nic nie wysyła."
    )


_DEFAULTS = {
    "platnosc": {"mint": USDC_MINT, "decimals": 6},
    "pozycja": {
        "wielkosc_usd": 3.0,
        "max_otwartych": 3,
        "max_nowych_na_uruchomienie": 1,
        "limit_straty_dziennej_usd": 10.0,
        "pauza_po_sprzedazy_minuty": 360,
    },
    "wyjscia": {
        "take_profit_razy": 1.3,
        "take_profit_sprzedaj_czesc": 0.5,
        "stop_loss_procent": 8.0,
        "trailing_stop_procent": 12.0,
    },
    "oplaty": {"rezerwa_sol": 0.005, "max_priority_fee_lamports": 200_000},
    "filtry": {
        "min_wiek_pary_minuty": 45,
        "min_plynnosc_usd": 50_000,
        "min_obrot_h1_usd": 30_000,
        "max_stosunek_sprzedaz_kupno_h1": 1.5,
        "min_kupna_h1": 25,
        "min_zmiana_h1_procent": 0,
        "max_zmiana_h1_procent": 100,
        "max_spadek_m5_procent": 5,
        "max_stosunek_sprzedaz_kupno_m5": 1.8,
    },
    "pary_quote": [SOL_MINT, USDC_MINT, USDT_MINT],
    "rugcheck": {
        "max_wynik": 30,
        "max_udzial_portfela_procent": 15,
        "min_lp_zablokowane_procent": 90,
        "max_oplata_transferu_procent": 1,
    },
    "jupiter": {
        "slippage_bps": 300,
        "max_wplyw_ceny_procent": 1.5,
        "min_odzysk_przy_sprzedazy_procent": 97,
        "quote_url": "https://lite-api.jup.ag/swap/v1/quote",
        "swap_url": "https://lite-api.jup.ag/swap/v1/swap",
    },
    "skan": {
        "max_pelnych_sprawdzen": 6,
        "timeout_sekundy": 20,
        "potwierdzenie_sekundy": 45,
    },
    "rpc": {"publiczny": "https://api.mainnet-beta.solana.com"},
}


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"Nie ma pliku {path.name}.")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"desk.yaml jest uszkodzony: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("desk.yaml musi być zestawem pól, nie listą.")
    data = _merge(_DEFAULTS, loaded)
    cfg = Config(
        usdc_mint=str(_dig(data, "platnosc", "mint")).strip(),
        usdc_decimals=_as_int(_dig(data, "platnosc", "decimals"), "platnosc.decimals"),
        position_size_usd=_as_float(_dig(data, "pozycja", "wielkosc_usd"), "pozycja.wielkosc_usd"),
        max_open_positions=_as_int(_dig(data, "pozycja", "max_otwartych"), "pozycja.max_otwartych"),
        max_new_buys_per_run=_as_int(
            _dig(data, "pozycja", "max_nowych_na_uruchomienie"),
            "pozycja.max_nowych_na_uruchomienie",
        ),
        daily_loss_limit_usd=_as_float(
            _dig(data, "pozycja", "limit_straty_dziennej_usd"),
            "pozycja.limit_straty_dziennej_usd",
        ),
        cooldown_minutes=_as_float(
            _dig(data, "pozycja", "pauza_po_sprzedazy_minuty"),
            "pozycja.pauza_po_sprzedazy_minuty",
        ),
        take_profit_multiple=_as_float(
            _dig(data, "wyjscia", "take_profit_razy"), "wyjscia.take_profit_razy"
        ),
        take_profit_sell_fraction=_as_float(
            _dig(data, "wyjscia", "take_profit_sprzedaj_czesc"),
            "wyjscia.take_profit_sprzedaj_czesc",
        ),
        stop_loss_pct=_as_float(
            _dig(data, "wyjscia", "stop_loss_procent"), "wyjscia.stop_loss_procent"
        ),
        trailing_stop_pct=_as_float(
            _dig(data, "wyjscia", "trailing_stop_procent"),
            "wyjscia.trailing_stop_procent",
        ),
        sol_fee_reserve=_as_float(_dig(data, "oplaty", "rezerwa_sol"), "oplaty.rezerwa_sol"),
        priority_fee_max_lamports=_as_int(
            _dig(data, "oplaty", "max_priority_fee_lamports"),
            "oplaty.max_priority_fee_lamports",
        ),
        min_pair_age_minutes=_as_float(
            _dig(data, "filtry", "min_wiek_pary_minuty"), "filtry.min_wiek_pary_minuty"
        ),
        min_liquidity_usd=_as_float(
            _dig(data, "filtry", "min_plynnosc_usd"), "filtry.min_plynnosc_usd"
        ),
        min_volume_h1_usd=_as_float(
            _dig(data, "filtry", "min_obrot_h1_usd"), "filtry.min_obrot_h1_usd"
        ),
        max_sell_to_buy_ratio_h1=_as_float(
            _dig(data, "filtry", "max_stosunek_sprzedaz_kupno_h1"),
            "filtry.max_stosunek_sprzedaz_kupno_h1",
        ),
        min_buys_h1=_as_int(_dig(data, "filtry", "min_kupna_h1"), "filtry.min_kupna_h1"),
        min_price_change_h1_pct=_as_float(
            _dig(data, "filtry", "min_zmiana_h1_procent"), "filtry.min_zmiana_h1_procent"
        ),
        max_price_change_h1_pct=_as_float(
            _dig(data, "filtry", "max_zmiana_h1_procent"), "filtry.max_zmiana_h1_procent"
        ),
        max_m5_drop_pct=_as_float(
            _dig(data, "filtry", "max_spadek_m5_procent"), "filtry.max_spadek_m5_procent"
        ),
        max_sell_to_buy_ratio_m5=_as_float(
            _dig(data, "filtry", "max_stosunek_sprzedaz_kupno_m5"),
            "filtry.max_stosunek_sprzedaz_kupno_m5",
        ),
        allowed_quote_mints=[str(item).strip() for item in _dig(data, "pary_quote")],
        max_score_normalised=_as_float(_dig(data, "rugcheck", "max_wynik"), "rugcheck.max_wynik"),
        max_top_holder_pct=_as_float(
            _dig(data, "rugcheck", "max_udzial_portfela_procent"),
            "rugcheck.max_udzial_portfela_procent",
        ),
        min_lp_locked_pct=_as_float(
            _dig(data, "rugcheck", "min_lp_zablokowane_procent"),
            "rugcheck.min_lp_zablokowane_procent",
        ),
        max_transfer_fee_pct=_as_float(
            _dig(data, "rugcheck", "max_oplata_transferu_procent"),
            "rugcheck.max_oplata_transferu_procent",
        ),
        slippage_bps=_as_int(_dig(data, "jupiter", "slippage_bps"), "jupiter.slippage_bps"),
        max_price_impact_pct=_as_float(
            _dig(data, "jupiter", "max_wplyw_ceny_procent"),
            "jupiter.max_wplyw_ceny_procent",
        ),
        min_roundtrip_pct=_as_float(
            _dig(data, "jupiter", "min_odzysk_przy_sprzedazy_procent"),
            "jupiter.min_odzysk_przy_sprzedazy_procent",
        ),
        jupiter_quote_url=str(_dig(data, "jupiter", "quote_url")).strip(),
        jupiter_swap_url=str(_dig(data, "jupiter", "swap_url")).strip(),
        max_full_checks_per_run=_as_int(
            _dig(data, "skan", "max_pelnych_sprawdzen"), "skan.max_pelnych_sprawdzen"
        ),
        request_timeout_seconds=_as_float(
            _dig(data, "skan", "timeout_sekundy"), "skan.timeout_sekundy"
        ),
        confirm_timeout_seconds=_as_float(
            _dig(data, "skan", "potwierdzenie_sekundy"), "skan.potwierdzenie_sekundy"
        ),
        rpc_fallback=str(_dig(data, "rpc", "publiczny")).strip(),
    )
    _validate(cfg)
    return cfg


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dig(data: dict, *keys: str):
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ConfigError(f"W pliku desk.yaml brakuje pola {'.'.join(keys)}.")
        current = current[key]
    return current


def _as_float(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Pole {label} musi być liczbą.")
    return float(value)


def _as_int(value, label: str) -> int:
    number = _as_float(value, label)
    if number != int(number):
        raise ConfigError(f"Pole {label} musi być liczbą całkowitą.")
    return int(number)


def _validate(cfg: Config) -> None:
    if cfg.usdc_mint != USDC_MINT:
        raise ConfigError(
            "platnosc.mint musi być oficjalnym USDC na Solanie "
            f"({USDC_MINT}). To jest zwykłe USDC z Phantom. "
            "Nie wpisuj tu innego adresu."
        )
    if not 0 < cfg.usdc_decimals <= 12:
        raise ConfigError("platnosc.decimals jest poza zakresem.")
    if not 0 < cfg.position_size_usd <= 25:
        raise ConfigError(
            "Wielkość pozycji musi być większa od 0 i nie większa niż 25 USD. "
            "Zostaw 5, jeśli nie chcesz większego ryzyka."
        )
    if not 1 <= cfg.max_open_positions <= 10:
        raise ConfigError("max_otwartych musi być od 1 do 10.")
    if cfg.max_new_buys_per_run < 1:
        raise ConfigError("max_nowych_na_uruchomienie musi być co najmniej 1.")
    if cfg.daily_loss_limit_usd <= 0:
        raise ConfigError("limit_straty_dziennej_usd musi być większy od 0.")
    if cfg.cooldown_minutes < 0:
        raise ConfigError("pauza_po_sprzedazy_minuty nie może być ujemna.")
    if cfg.take_profit_multiple < 1.1:
        raise ConfigError("take_profit_razy musi być co najmniej 1.1.")
    if not 0 < cfg.take_profit_sell_fraction <= 1:
        raise ConfigError("take_profit_sprzedaj_czesc musi być między 0 a 1.")
    if not 0 < cfg.stop_loss_pct < 90:
        raise ConfigError("stop_loss_procent musi być między 0 a 90.")
    if not 0 < cfg.trailing_stop_pct < 90:
        raise ConfigError("trailing_stop_procent musi być między 0 a 90.")
    if cfg.sol_fee_reserve <= 0:
        raise ConfigError("rezerwa_sol musi być większa od 0.")
    if not 0 <= cfg.priority_fee_max_lamports <= 5_000_000:
        raise ConfigError("max_priority_fee_lamports musi być od 0 do 5000000.")
    if cfg.min_pair_age_minutes < 0:
        raise ConfigError("min_wiek_pary_minuty nie może być ujemny.")
    if not 1 <= cfg.slippage_bps <= 1500:
        raise ConfigError("slippage_bps musi być od 1 do 1500 (1500 = 15%).")
    if not 0 < cfg.max_price_impact_pct <= 25:
        raise ConfigError("max_wplyw_ceny_procent musi być od 0 do 25.")
    if not 0 < cfg.min_roundtrip_pct <= 100:
        raise ConfigError("min_odzysk_przy_sprzedazy_procent musi być od 0 do 100.")
    if cfg.usdc_mint not in cfg.allowed_quote_mints:
        raise ConfigError("pary_quote musi zawierać adres USDC.")
    if not cfg.jupiter_quote_url.startswith("https://") or not cfg.jupiter_swap_url.startswith("https://"):
        raise ConfigError("Adresy Jupiter muszą zaczynać się od https://.")
    if not cfg.rpc_fallback.startswith("https://"):
        raise ConfigError("Adres RPC musi zaczynać się od https://.")
