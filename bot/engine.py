"""One GitHub run: exits first, then buys. May repeat for a few minutes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.clients import DexScreener, Http, Jupiter, RugCheck
from bot.config import SOL_MINT, USDT_MINT, Config, ConfigError, load_config, paper_reason, trading_mode
from bot.logic import (
    decide_exit,
    impact_percent,
    price_from_usdc,
    sell_raw_amount,
    split_cost,
    token_ui,
)
from bot.report import Report, redact
from bot.safety import evaluate_report
from bot.screen import best_pair, screen_pair
from bot.solana import RpcError, SolanaRpc, WalletError, load_keypair
from bot.state import State, StateError, append_trade

REASON_PL = {
    "stop_loss": "twardy stop-loss",
    "take_profit": "cel zysku (sprzedaż części)",
    "trailing_stop": "trailing stop od szczytu",
}

ROOT = Path(__file__).resolve().parent.parent
CYCLE_INTERVAL_SECONDS = 30
SCAN_EVERY_SECONDS = 90


@dataclass
class Candidate:
    mint: str
    symbol: str
    pair_address: str
    decimals: int
    buy_quote: dict
    entry_mark_price: float


def run(root: Path | None = None) -> int:
    root = root or ROOT
    secret = os.environ.get("SOLANA_PRIVATE_KEY", "")
    try:
        cfg = load_config(root / "desk.yaml")
        state = State.load(root / "state" / "state.json")
    except (ConfigError, StateError) as exc:
        _say(str(exc), secret)
        return 1

    mode = trading_mode()
    report = Report()
    report.mode = mode
    report.loss_limit = cfg.daily_loss_limit_usd
    report.max_open = cfg.max_open_positions
    report.sol_reserve = cfg.sol_fee_reserve
    report.usdc_mint = cfg.usdc_mint
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state.roll_day(today)
    state.prune_cooldowns(now, cfg.cooldown_minutes)
    report.day = today

    http = Http(cfg.request_timeout_seconds)
    jupiter = Jupiter(http, cfg.jupiter_quote_url, cfg.jupiter_swap_url)
    dex = DexScreener(http)
    rugs = RugCheck(http)

    keypair = None
    rpc = None
    owner = ""

    if mode == "live":
        try:
            keypair = load_keypair(secret)
        except WalletError as exc:
            report.notes.append(str(exc))
            report.realized = state.realized(mode)
            report.open_count = len(state.positions_for(mode))
            _finish(root, state, report, secret, now)
            return 1
        owner = str(keypair.pubkey())
        report.pubkey = owner
        rpc_url = os.environ.get("RPC_URL", "").strip() or cfg.rpc_fallback
        rpc = SolanaRpc(rpc_url, cfg.request_timeout_seconds)
    else:
        report.paper_reason = paper_reason()

    loop_seconds = parse_loop_seconds(os.environ.get("LOOP_SECONDS"))
    started = time.monotonic()
    last_scan_at = -SCAN_EVERY_SECONDS
    cycle_no = 0
    # Hard cap so a clock mistake cannot spin. 4.5 min at 30 s is about 10 cycles.
    while cycle_no < 20:
        cycle_no += 1
        report.cycles = cycle_no
        now = datetime.now(timezone.utc)
        try:
            scanned = _one_cycle(
                root=root,
                cfg=cfg,
                state=state,
                mode=mode,
                report=report,
                secret=secret,
                http_clients=(jupiter, dex, rugs),
                rpc=rpc,
                keypair=keypair,
                owner=owner,
                now=now,
                cycle_no=cycle_no,
                allow_scan=(time.monotonic() - last_scan_at) >= SCAN_EVERY_SECONDS,
            )
        except Exception as exc:
            report.warnings.append(f"Sprawdzenie {cycle_no} przerwane ({type(exc).__name__}).")
            _say(f"Sprawdzenie {cycle_no} nie doszło do końca: {type(exc).__name__}", secret)
            scanned = False
            state.data["last_run_at"] = now.isoformat()
            state.save()
            _write_report_file(root, report, secret)
        if scanned:
            last_scan_at = time.monotonic()
        pause = seconds_until_next_cycle(
            loop_seconds, started, time.monotonic(), CYCLE_INTERVAL_SECONDS
        )
        if pause is None:
            break
        _say(f"Następne sprawdzenie cen za {int(pause)} s.", secret)
        time.sleep(pause)
    _finish(root, state, report, secret, datetime.now(timezone.utc))
    return 0


def parse_loop_seconds(raw: str | None) -> int:
    """0 means one cycle. Anything else is how long the whole run may last."""
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        return 0
    return value if value > 0 else 0


def seconds_until_next_cycle(loop_seconds: int, started: float, now: float, interval: int) -> float | None:
    """Sleep until the next 30 s mark. None means this run should stop."""
    if loop_seconds <= 0 or interval <= 0:
        return None
    elapsed = now - started
    if elapsed >= loop_seconds:
        return None
    next_start = (int(elapsed // interval) + 1) * interval
    if next_start >= loop_seconds:
        return None
    return max(0.0, started + next_start - now)


def _one_cycle(
    root,
    cfg,
    state,
    mode,
    report,
    secret,
    http_clients,
    rpc,
    keypair,
    owner,
    now,
    cycle_no,
    allow_scan,
) -> bool:
    """Exits first, then at most one buy. Returns True when a buy scan ran."""
    jupiter, dex, rugs = http_clients
    state.roll_day(now.strftime("%Y-%m-%d"))
    state.prune_cooldowns(now, cfg.cooldown_minutes)
    report.day = now.strftime("%Y-%m-%d")
    report.positions.clear()

    blocked_sol = False
    usdc_raw = 0
    wallet_unreadable = False
    if mode == "live" and rpc is not None:
        try:
            sol = rpc.sol_balance(owner)
            usdc_raw = rpc.token_raw(owner, cfg.usdc_mint)
        except RpcError as exc:
            wallet_unreadable = True
            report.notes.append(
                "Nie udało się odczytać portfela, więc w tym sprawdzeniu nic nie sprzedaję "
                f"i nic nie kupuję. Szczegół: {exc}"
            )
        else:
            report.sol = sol
            report.usdc = usdc_raw / (10**cfg.usdc_decimals)
            if sol < cfg.sol_fee_reserve:
                blocked_sol = True
                report.notes.append(
                    f"Za mało SOL na opłaty sieci. Jest {sol:.4f} SOL, a minimum to "
                    f"{cfg.sol_fee_reserve:.4f} SOL. Wpłać około 0.02–0.03 SOL na portfel bota. "
                    "Bot nie wysyła transakcji, dopóki SOL nie wróci powyżej minimum."
                )

    if wallet_unreadable:
        state.data["last_run_at"] = now.isoformat()
        state.save()
        _write_report_file(root, report, secret)
        return False

    _say(f"Sprawdzenie {cycle_no}: pilnuję otwartych pozycji...", secret)
    for pos in list(state.positions_for(mode)):
        try:
            _manage_position(
                pos, cfg, state, mode, jupiter, rpc, keypair, owner, blocked_sol, report, now
            )
        except Exception as exc:
            symbol = pos.get("symbol") or "?"
            report.warnings.append(f"{symbol}: błąd przy pozycji ({type(exc).__name__})")

    report.realized = state.realized(mode)
    report.open_count = len(state.positions_for(mode))
    loss_hit = state.realized(mode) <= -cfg.daily_loss_limit_usd
    if loss_hit:
        report.notes.append(
            f"Dzienny limit straty ({cfg.daily_loss_limit_usd:.0f} USD) jest osiągnięty. "
            "Do następnego dnia UTC nie kupuję nowych tokenów. Otwarte pozycje dalej pilnuję."
        )

    slots = cfg.max_open_positions - len(state.positions_for(mode))
    usdc_short = mode == "live" and usdc_raw < cfg.position_size_raw
    can_buy = slots > 0 and not loss_hit and not blocked_sol and not usdc_short
    if usdc_short and slots > 0 and not loss_hit and not blocked_sol:
        have = usdc_raw / (10**cfg.usdc_decimals)
        report.notes.append(
            f"Za mało USDC na nowe kupno. Jest {have:.2f} USDC, "
            f"a jedna pozycja kosztuje {cfg.position_size_usd:.2f} USDC."
        )
    if slots <= 0 and not loss_hit and not blocked_sol:
        report.notes.append("Jest już maksymalna liczba otwartych pozycji. Nowych nie kupuję.")

    scanned = False
    if can_buy and allow_scan:
        scanned = True
        _say("Szukam tokenów...", secret)
        try:
            _look_for_buys(
                cfg, state, mode, dex, rugs, jupiter, rpc, keypair, owner, report, now
            )
        except Exception as exc:
            report.warnings.append(f"Skan przerwany ({type(exc).__name__}).")
    elif can_buy:
        _say("Tym razem tylko ceny otwartych pozycji. Nowe szukanie za chwilę.", secret)

    report.realized = state.realized(mode)
    report.open_count = len(state.positions_for(mode))
    if not report.positions:
        for pos in state.positions_for(mode):
            _remember_position(report, pos, float(pos.get("peak_price_usd") or pos.get("entry_price_usd") or 0))
    state.data["last_run_at"] = now.isoformat()
    state.save()
    _write_report_file(root, report, secret)
    return scanned


def _manage_position(pos, cfg, state, mode, jupiter, rpc, keypair, owner, blocked_sol, report, now):
    symbol = pos.get("symbol") or "?"
    mint = pos.get("mint") or ""
    if not mint:
        return

    if mode == "live" and rpc is not None and pos.get("pending"):
        outcome = _settle_pending(pos, cfg, state, rpc, report, now)
        if outcome == "drop":
            return
        if outcome == "wait":
            _remember_position(report, pos, float(pos.get("peak_price_usd") or 0))
            return

    if mode == "live" and rpc is not None and owner:
        try:
            on_chain = rpc.token_raw(owner, mint)
        except RpcError as exc:
            report.warnings.append(f"{symbol}: nie odczytałem salda ({exc})")
            on_chain = None
        if on_chain == 0:
            state.remove_position(mint, mode)
            state.save()
            report.notes.append(
                f"{symbol}: na portfelu nie ma już tego tokenu. Zdejmuję go z listy pozycji."
            )
            return
        if on_chain is not None and 0 < on_chain < int(pos["amount_raw"]):
            old = int(pos["amount_raw"])
            pos["cost_usd_remaining"] = round(float(pos["cost_usd_remaining"]) * (on_chain / old), 6)
            pos["amount_raw"] = str(on_chain)

    amount_raw = int(pos["amount_raw"])
    quote, quote_error = jupiter.quote(mint, cfg.usdc_mint, amount_raw, cfg.slippage_bps)
    if not quote:
        report.warnings.append(f"{symbol}: brak trasy sprzedaży na Jupiter ({quote_error}). Zostawiam.")
        _remember_position(report, pos, float(pos.get("peak_price_usd") or 0))
        return

    proceeds = int(quote["outAmount"]) / (10**cfg.usdc_decimals)
    price = price_from_usdc(proceeds, amount_raw, int(pos["decimals"]))
    peak = max(float(pos.get("peak_price_usd") or 0), price)
    pos["peak_price_usd"] = peak
    mark = float(pos.get("entry_mark_price") or 0)
    decision = decide_exit(
        float(pos["entry_price_usd"]),
        peak,
        price,
        bool(pos.get("took_partial_tp")),
        cfg.take_profit_multiple,
        cfg.take_profit_sell_fraction,
        cfg.stop_loss_pct,
        cfg.trailing_stop_pct,
        stop_basis=mark or None,
    )
    if decision.action != "sell":
        _remember_position(report, pos, price)
        return

    if mode == "live" and blocked_sol:
        report.notes.append(
            f"{symbol}: trzeba sprzedać ({REASON_PL.get(decision.reason, decision.reason)}), "
            "ale SOL na opłaty jest za mało. Nic nie wysyłam."
        )
        _remember_position(report, pos, price)
        return

    sell_raw = sell_raw_amount(amount_raw, decision.sell_fraction)
    sell_quote = quote
    if sell_raw != amount_raw:
        sell_quote, sell_error = jupiter.quote(mint, cfg.usdc_mint, sell_raw, cfg.slippage_bps)
        if not sell_quote:
            report.warnings.append(f"{symbol}: nie udało się wycenić częściowej sprzedaży ({sell_error}).")
            _remember_position(report, pos, price)
            return

    _execute_sell(
        pos, cfg, state, mode, jupiter, rpc, keypair, owner, sell_quote, sell_raw, decision.reason, report, now
    )


def _execute_sell(pos, cfg, state, mode, jupiter, rpc, keypair, owner, sell_quote, sell_raw, reason, report, now):
    symbol = pos.get("symbol") or "?"
    mint = pos["mint"]
    if sell_quote.get("inputMint") != mint or sell_quote.get("outputMint") != cfg.usdc_mint:
        report.warnings.append(f"{symbol}: wycena sprzedaży była na złą parę. Nic nie wysyłam.")
        return
    proceeds = int(sell_quote["outAmount"]) / (10**cfg.usdc_decimals)
    signature = ""
    if mode == "live":
        try:
            sol_now = rpc.sol_balance(owner)
        except RpcError as exc:
            report.warnings.append(f"{symbol}: nie sprawdziłem SOL przed sprzedażą ({exc}).")
            return
        if sol_now < cfg.sol_fee_reserve:
            report.notes.append(
                f"{symbol}: sprzedaż wstrzymana, bo SOL spadło poniżej {cfg.sol_fee_reserve:.4f}."
            )
            return
        transaction, tx_error = jupiter.swap_transaction(
            sell_quote, owner, cfg.priority_fee_max_lamports
        )
        if not transaction:
            report.warnings.append(f"{symbol}: nie zbudowałem sprzedaży ({tx_error}).")
            return
        try:
            signature = rpc.send_swap(keypair, transaction)
        except RpcError as exc:
            report.warnings.append(f"{symbol}: sprzedaż odrzucona ({exc}).")
            return
        status = rpc.confirm(signature, cfg.confirm_timeout_seconds)
        link = _solscan(signature)
        if status == "failed":
            report.warnings.append(f"{symbol}: sprzedaż nie weszła na sieć. {link}")
            return
        if status == "timeout":
            pos["pending"] = {
                "side": "sell",
                "signature": signature,
                "sent_at": now.isoformat(),
                "sell_raw": str(sell_raw),
                "proceeds_usd": proceeds,
                "reason": reason,
            }
            state.save()
            report.notes.append(
                f"{symbol}: sprzedaż wysłana, czekam na potwierdzenie. Nie wysyłam drugiej. {link}"
            )
            _remember_position(report, pos, float(pos.get("peak_price_usd") or 0))
            return

    _commit_sell(pos, cfg, state, mode, sell_raw, proceeds, reason, signature, report, now)


def _commit_sell(pos, cfg, state, mode, sell_raw, proceeds, reason, signature, report, now):
    mint = pos["mint"]
    symbol = pos.get("symbol") or "?"
    amount_raw = int(pos["amount_raw"])
    sold_cost, left_cost = split_cost(float(pos["cost_usd_remaining"]), amount_raw, sell_raw)
    pnl = proceeds - sold_cost
    state.add_realized(mode, pnl)
    fill_price = price_from_usdc(proceeds, sell_raw, int(pos["decimals"]))
    link = _solscan(signature) if signature else ""
    append_trade(
        _trades_path(state),
        {
            "czas_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tryb": "prawdziwy" if mode == "live" else "próbny",
            "strona": "SPRZEDAZ",
            "mint": mint,
            "symbol": _clean_symbol(symbol),
            "ilosc_tokenow": f"{token_ui(sell_raw, int(pos['decimals'])):.8f}",
            "cena_usd": f"{fill_price:.10f}",
            "wartosc_usdc": f"{proceeds:.4f}",
            "zysk_usdc": f"{pnl:.4f}",
            "powod": REASON_PL.get(reason, reason),
            "podpis": signature,
            "solscan": link,
        },
    )
    label = REASON_PL.get(reason, reason)
    where = link or "tryb próbny, bez transakcji"
    report.sells.append(
        f"{symbol} `{mint}` — {label}, {proceeds:.2f} USDC, wynik {pnl:+.2f} USDC. {where}"
    )
    pos.pop("pending", None)
    if sell_raw >= amount_raw:
        state.remove_position(mint, mode)
        state.mark_cooldown(mint, now)
    else:
        pos["amount_raw"] = str(amount_raw - sell_raw)
        pos["cost_usd_remaining"] = round(max(left_cost, 0.0), 6)
        if reason == "take_profit":
            pos["took_partial_tp"] = True
        _remember_position(report, pos, fill_price)
    state.save()


def _settle_pending(pos, cfg, state, rpc, report, now) -> str:
    pending = pos.get("pending") or {}
    signature = pending.get("signature") or ""
    symbol = pos.get("symbol") or "?"
    status = "timeout"
    if signature:
        try:
            status = rpc.confirm(signature, min(20, cfg.confirm_timeout_seconds))
        except RpcError:
            status = "timeout"
    age = _age_minutes(pending.get("sent_at"), now)
    link = _solscan(signature) if signature else ""

    if pending.get("side") == "sell" and status == "confirmed":
        _commit_sell(
            pos,
            cfg,
            state,
            "live",
            int(pending.get("sell_raw") or pos["amount_raw"]),
            float(pending.get("proceeds_usd") or 0),
            pending.get("reason") or "sell",
            signature,
            report,
            now,
        )
        return "drop"
    if pending.get("side") == "buy" and status == "confirmed":
        pos.pop("pending", None)
        state.save()
        report.notes.append(f"{symbol}: kupno potwierdzone. {link}")
        return "ready"
    if status == "failed" or age >= 30:
        pos.pop("pending", None)
        if pending.get("side") == "buy":
            state.remove_position(pos.get("mint"), "live")
            state.save()
            report.notes.append(f"{symbol}: kupno nie weszło na sieć. Zdejmuję pozycję. {link}")
            return "drop"
        state.save()
        report.notes.append(f"{symbol}: poprzednia sprzedaż nie weszła. Sprawdzę warunek jeszcze raz. {link}")
        return "ready"
    report.notes.append(f"{symbol}: czekam na potwierdzenie transakcji. Nie wysyłam drugiej. {link}")
    return "wait"


def _look_for_buys(cfg, state, mode, dex, rugs, jupiter, rpc, keypair, owner, report, now):
    report.skips.clear()
    mints, warnings = dex.trending_mints()
    report.warnings.extend(warnings)
    report.discovered = len(mints)
    if not mints:
        report.warnings.append("DexScreener nie zwrócił tokenów Solana.")
        return
    pairs, pair_warnings = dex.pairs_for(mints)
    report.warnings.extend(pair_warnings)
    allowed = set(cfg.allowed_quote_mints)
    now_ms = int(now.timestamp() * 1000)
    candidates = []
    screened_out = 0
    screen_examples: list[str] = []
    for mint in mints:
        if mint in {cfg.usdc_mint, SOL_MINT, USDT_MINT}:
            continue
        if state.holds(mint) or state.on_cooldown(mint, now, cfg.cooldown_minutes):
            continue
        pair = best_pair(pairs, mint, allowed)
        if pair is None:
            screened_out += 1
            continue
        reason = screen_pair(pair, cfg, now_ms)
        if reason:
            screened_out += 1
            if len(screen_examples) < 5:
                symbol = (pair.get("baseToken") or {}).get("symbol") or mint[:4]
                screen_examples.append(f"{_clean_symbol(symbol)} (`{mint}`): {reason}")
            continue
        candidates.append((mint, pair))
    report.screened_out = screened_out

    buys_done = 0
    checks = 0
    for mint, pair in candidates:
        if buys_done >= cfg.max_new_buys_per_run:
            break
        if len(state.positions_for(mode)) >= cfg.max_open_positions:
            break
        if checks >= cfg.max_full_checks_per_run:
            break
        checks += 1
        symbol = _clean_symbol((pair.get("baseToken") or {}).get("symbol") or "?")
        _say(f"Sprawdzam {symbol} {mint}", os.environ.get("SOLANA_PRIVATE_KEY", ""))
        candidate, reason = _full_check(mint, symbol, pair, cfg, rugs, jupiter)
        if candidate is None:
            report.skips.append(f"{symbol} (`{mint}`): {reason}")
            continue
        error = _execute_buy(candidate, cfg, state, mode, jupiter, rpc, keypair, owner, report, now)
        if error:
            report.skips.append(f"{symbol} (`{mint}`): {error}")
            continue
        buys_done += 1
    report.skips.extend(screen_examples)


def _full_check(mint, symbol, pair, cfg, rugs, jupiter) -> tuple[Candidate | None, str]:
    del symbol
    report, err = rugs.report(mint)
    if not report:
        return None, f"RugCheck niedostępny ({err})"
    bad = evaluate_report(report, cfg)
    if bad:
        return None, bad
    raw_decimals = (report.get("token") or {}).get("decimals")
    if isinstance(raw_decimals, bool) or not isinstance(raw_decimals, (int, float)):
        return None, "brak liczby miejsc po przecinku"
    decimals = int(raw_decimals)
    if not 0 <= decimals <= 12:
        return None, "brak liczby miejsc po przecinku"
    buy_quote, buy_error = jupiter.quote(cfg.usdc_mint, mint, cfg.position_size_raw, cfg.slippage_bps)
    if not buy_quote:
        return None, f"brak trasy kupna ({buy_error})"
    buy_impact = impact_percent(buy_quote.get("priceImpactPct"))
    if buy_impact > cfg.max_price_impact_pct:
        return None, f"za duży wpływ na cenę przy kupnie ({buy_impact:.1f}%)"
    out_raw = int(buy_quote["outAmount"])
    if out_raw <= 0:
        return None, "kupno zwraca 0 tokenów"
    sell_quote, sell_error = jupiter.quote(mint, cfg.usdc_mint, out_raw, cfg.slippage_bps)
    if not sell_quote:
        return None, f"brak trasy sprzedaży ({sell_error})"
    sell_impact = impact_percent(sell_quote.get("priceImpactPct"))
    if sell_impact > cfg.max_price_impact_pct:
        return None, f"za duży wpływ na cenę przy sprzedaży ({sell_impact:.1f}%)"
    back = int(sell_quote["outAmount"]) / (10**cfg.usdc_decimals)
    recovery = back / cfg.position_size_usd * 100
    if recovery < cfg.min_roundtrip_pct:
        return None, f"test sprzedaży zwraca tylko {recovery:.0f}% USDC"
    entry_mark_price = price_from_usdc(back, out_raw, decimals)
    return (
        Candidate(
            mint=mint,
            symbol=_clean_symbol((pair.get("baseToken") or {}).get("symbol") or "?"),
            pair_address=str(pair.get("pairAddress") or ""),
            decimals=decimals,
            buy_quote=buy_quote,
            entry_mark_price=entry_mark_price,
        ),
        "",
    )


def _execute_buy(candidate, cfg, state, mode, jupiter, rpc, keypair, owner, report, now) -> str:
    quote = candidate.buy_quote
    if quote.get("inputMint") != cfg.usdc_mint or quote.get("outputMint") != candidate.mint:
        return "wycena kupna była na złą parę"
    out_raw = int(quote["outAmount"])
    cost = cfg.position_size_usd
    entry = price_from_usdc(cost, out_raw, candidate.decimals)
    signature = ""
    pending = None
    if mode == "live":
        try:
            sol_now = rpc.sol_balance(owner)
            usdc_now = rpc.token_raw(owner, cfg.usdc_mint)
        except RpcError as exc:
            return f"nie sprawdziłem salda przed kupnem ({exc})"
        if sol_now < cfg.sol_fee_reserve:
            report.notes.append(
                f"Za mało SOL na opłaty ({sol_now:.4f}). Pomijam kupno {candidate.symbol}."
            )
            return "za mało SOL na opłaty"
        if usdc_now < cfg.position_size_raw:
            have = usdc_now / (10**cfg.usdc_decimals)
            return f"za mało USDC ({have:.2f})"
        transaction, tx_error = jupiter.swap_transaction(
            quote, owner, cfg.priority_fee_max_lamports
        )
        if not transaction:
            return tx_error or "nie zbudowałem transakcji"
        try:
            signature = rpc.send_swap(keypair, transaction)
        except RpcError as exc:
            return f"transakcja odrzucona ({exc})"
        status = rpc.confirm(signature, cfg.confirm_timeout_seconds)
        link = _solscan(signature)
        if status == "failed":
            return f"kupno nie weszło na sieć: {link}"
        if status == "timeout":
            pending = {"side": "buy", "signature": signature, "sent_at": now.isoformat()}
            report.notes.append(
                f"{candidate.symbol}: kupno wysłane, czekam na potwierdzenie. {link}"
            )

    position = {
        "mint": candidate.mint,
        "symbol": candidate.symbol,
        "decimals": candidate.decimals,
        "amount_raw": str(out_raw),
        "entry_price_usd": entry,
        "entry_mark_price": candidate.entry_mark_price,
        "cost_usd_remaining": round(cost, 4),
        "peak_price_usd": candidate.entry_mark_price or entry,
        "took_partial_tp": False,
        "opened_at": now.isoformat(),
        "mode": mode,
        "pair_address": candidate.pair_address,
        "entry_tx": signature,
    }
    if pending:
        position["pending"] = pending
    state.data["positions"].append(position)
    link = _solscan(signature) if signature else ""
    append_trade(
        _trades_path(state),
        {
            "czas_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tryb": "prawdziwy" if mode == "live" else "próbny",
            "strona": "KUPNO",
            "mint": candidate.mint,
            "symbol": candidate.symbol,
            "ilosc_tokenow": f"{token_ui(out_raw, candidate.decimals):.8f}",
            "cena_usd": f"{entry:.10f}",
            "wartosc_usdc": f"{cost:.4f}",
            "zysk_usdc": "",
            "powod": "wejście",
            "podpis": signature,
            "solscan": link,
        },
    )
    state.save()
    where = link or "tryb próbny, bez transakcji"
    report.buys.append(
        f"{candidate.symbol} `{candidate.mint}` za {cost:.2f} USDC. {where}"
    )
    _remember_position(report, position, entry)
    return ""


def _remember_position(report: Report, pos: dict, price: float) -> None:
    entry = float(pos.get("entry_price_usd") or 0)
    change = ((price / entry) - 1) * 100 if entry and price else 0
    decimals = int(pos.get("decimals") or 0)
    ui = token_ui(int(pos.get("amount_raw") or 0), decimals)
    value = price * ui if price else 0
    report.positions.append(
        f"{pos.get('symbol') or '?'} `{pos.get('mint')}` — "
        f"wejście {entry:.8g}, teraz {price:.8g} ({change:+.1f}%), "
        f"wartość około {value:.2f} USDC, szczyt {float(pos.get('peak_price_usd') or 0):.8g}"
    )


def _write_report_file(root: Path, report: Report, secret: str) -> str:
    text = redact(report.render(), secret)
    report_path = root / "state" / "ostatni_raport.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    return text


def _finish(root: Path, state: State, report: Report, secret: str, now: datetime) -> None:
    state.data["last_run_at"] = now.isoformat()
    state.save()
    text = _write_report_file(root, report, secret)
    _say(text, secret)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)


def _trades_path(state: State) -> Path:
    return state.path.parent / "trades.csv"


def _solscan(signature: str) -> str:
    if not signature:
        return ""
    return f"https://solscan.io/tx/{signature}"


def _clean_symbol(symbol: str) -> str:
    cleaned = (symbol or "?").replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:32] or "?"


def _age_minutes(raw, now: datetime) -> float:
    if not raw or not isinstance(raw, str):
        return 0.0
    try:
        sent = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return max((now - sent) / timedelta(minutes=1), 0.0)


def _say(message: str, secret: str) -> None:
    print(redact(message, secret), flush=True)
