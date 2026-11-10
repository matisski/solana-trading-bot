"""Rules that must hold before the bot is allowed to trade."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from bot.config import USDC_MINT, ConfigError, load_config, trading_mode
from bot.engine import parse_loop_seconds, seconds_until_next_cycle
from bot.reconcile import estimate_entry, is_meaningful, split_conflict, union_csv, union_state
from bot.logic import decide_exit, impact_percent, sell_raw_amount, split_cost
from bot.report import redact
from bot.safety import evaluate_report
from bot.screen import screen_pair
from bot.solana import WalletError, load_keypair
from bot.state import State

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "desk.yaml")


class ModeTests(unittest.TestCase):
    def test_paper_without_key(self):
        self.assertEqual(trading_mode({}), "paper")
        self.assertEqual(trading_mode({"DRY_RUN": "false"}), "paper")

    def test_live_when_key_present(self):
        self.assertEqual(trading_mode({"SOLANA_PRIVATE_KEY": "abc"}), "live")

    def test_dry_run_overrides_key(self):
        env = {"SOLANA_PRIVATE_KEY": "abc", "DRY_RUN": "true"}
        self.assertEqual(trading_mode(env), "paper")
        self.assertEqual(trading_mode({"SOLANA_PRIVATE_KEY": "abc", "DRY_RUN": " TAK "}), "paper")


class ExitTests(unittest.TestCase):
    def decide(self, entry, peak, price, took_partial=False):
        return decide_exit(
            entry,
            peak,
            price,
            took_partial,
            CFG.take_profit_multiple,
            CFG.take_profit_sell_fraction,
            CFG.stop_loss_pct,
            CFG.trailing_stop_pct,
        )

    def test_hard_stop(self):
        decision = self.decide(1.0, 1.0, 0.92)
        self.assertEqual(decision.reason, "stop_loss")
        self.assertEqual(decision.sell_fraction, 1.0)

    def test_small_dip_holds(self):
        self.assertEqual(self.decide(1.0, 1.0, 0.93).action, "hold")

    def test_buy_spread_alone_does_not_trip_stop(self):
        # Paid 1.00, but a sell quote right after the fill is 0.97.
        # 0.91 is already -9% versus cash, and only about -6% versus that quote.
        decision = decide_exit(1.0, 1.0, 0.91, False, 2.0, 0.5, 8.0, 10.0, stop_basis=0.97)
        self.assertEqual(decision.action, "hold")
        tripped = decide_exit(1.0, 1.0, 0.97 * 0.92, False, 2.0, 0.5, 8.0, 10.0, stop_basis=0.97)
        self.assertEqual(tripped.reason, "stop_loss")

    def test_take_profit_sells_half(self):
        decision = self.decide(1.0, 2.0, 2.0)
        self.assertEqual(decision.reason, "take_profit")
        self.assertEqual(decision.sell_fraction, 0.5)

    def test_no_second_partial(self):
        self.assertEqual(self.decide(1.0, 2.2, 2.1, took_partial=True).action, "hold")

    def test_trailing_after_peak(self):
        decision = self.decide(1.0, 1.5, 1.35)
        self.assertEqual(decision.reason, "trailing_stop")

    def test_trail_not_armed_at_entry(self):
        self.assertEqual(self.decide(1.0, 1.0, 0.95).action, "hold")

    def test_half_amount_and_cost(self):
        self.assertEqual(sell_raw_amount(100, 0.5), 50)
        sold, left = split_cost(5.0, 100, 50)
        self.assertAlmostEqual(sold, 2.5)
        self.assertAlmostEqual(left, 2.5)

    def test_impact_is_a_fraction(self):
        self.assertAlmostEqual(impact_percent("0.05"), 5.0)


class SafetyTests(unittest.TestCase):
    def test_young_pair_is_skipped(self):
        now_ms = 1_000_000_000_000
        pair = self._pair(created=now_ms - 10 * 60_000, change=20)
        reason = screen_pair(pair, CFG, now_ms)
        self.assertIn("za młoda", reason)

    def test_good_pair_passes_screen(self):
        now_ms = 1_000_000_000_000
        pair = self._pair(created=now_ms - 120 * 60_000, change=20)
        self.assertIsNone(screen_pair(pair, CFG, now_ms))

    def test_lopsided_sells_are_skipped(self):
        now_ms = 1_000_000_000_000
        pair = self._pair(created=now_ms - 120 * 60_000, change=20, buys=100, sells=400)
        self.assertIn("sprzedaży", screen_pair(pair, CFG, now_ms))

    def test_five_minute_dump_is_skipped(self):
        now_ms = 1_000_000_000_000
        pair = self._pair(created=now_ms - 120 * 60_000, change=20)
        pair["priceChange"]["m5"] = -12
        self.assertIn("5 minutach", screen_pair(pair, CFG, now_ms))

    def test_clean_report_passes(self):
        self.assertIsNone(evaluate_report(self._report(), CFG))

    def test_mint_authority_blocks(self):
        report = self._report()
        report["token"]["mintAuthority"] = "SomeAuthority111111111111111111111111111"
        self.assertIn("mint authority", evaluate_report(report, CFG))

    def test_pool_holder_is_not_a_person(self):
        report = self._report()
        report["topHolders"] = [
            {"address": "PoolVault11111111111111111111111111111111", "owner": "person", "pct": 40},
            {"address": "Wallet1111111111111111111111111111111111", "owner": "person", "pct": 4},
        ]
        report["markets"][0]["mintAAccount"] = "PoolVault11111111111111111111111111111111"
        self.assertIsNone(evaluate_report(report, CFG))

    def test_large_holder_blocks(self):
        report = self._report()
        report["topHolders"] = [
            {"address": "Wallet1111111111111111111111111111111111", "owner": "person", "pct": 30}
        ]
        self.assertIn("za dużo", evaluate_report(report, CFG))

    def test_unlocked_main_pool_blocks(self):
        report = self._report()
        report["markets"][0]["lp"]["lpLockedPct"] = 0
        self.assertIn("nie jest zablokowana", evaluate_report(report, CFG))

    def _pair(self, created, change, buys=100, sells=80):
        return {
            "chainId": "solana",
            "baseToken": {"address": "Mint", "symbol": "TEST"},
            "quoteToken": {"address": USDC_MINT},
            "liquidity": {"usd": 50_000},
            "pairCreatedAt": created,
            "txns": {"h1": {"buys": buys, "sells": sells}},
            "volume": {"h1": 40_000},
            "priceChange": {"h1": change},
        }

    def _report(self):
        return {
            "rugged": False,
            "score_normalised": 5,
            "token": {"mintAuthority": None, "freezeAuthority": None, "decimals": 6},
            "mintAuthority": None,
            "freezeAuthority": None,
            "transferFee": {"pct": 0},
            "token_extensions": {},
            "topHolders": [
                {"address": "Wallet1111111111111111111111111111111111", "owner": "o", "pct": 4}
            ],
            "markets": [
                {
                    "pubkey": "Pool111111111111111111111111111111111111",
                    "lp": {"baseUSD": 20_000, "quoteUSD": 20_000, "lpLockedPct": 100},
                }
            ],
        }


class WalletTests(unittest.TestCase):
    def test_phantom_style_key_roundtrip(self):
        import base58
        from solders.keypair import Keypair

        keypair = Keypair()
        secret = base58.b58encode(bytes(keypair)).decode()
        loaded = load_keypair(secret + "\n")
        self.assertEqual(str(loaded.pubkey()), str(keypair.pubkey()))

    def test_seed_phrase_is_rejected_without_echo(self):
        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
        with self.assertRaises(WalletError) as caught:
            load_keypair(words)
        self.assertNotIn("alpha", str(caught.exception))
        self.assertNotIn(words, str(caught.exception))

    def test_secret_is_redacted(self):
        secret = "5" * 40
        self.assertNotIn(secret, redact(f"oops {secret}", secret))


class StateTests(unittest.TestCase):
    def test_new_utc_day_resets_pnl(self):
        path = ROOT / "state" / "state.json"
        state = State(path, json.loads(path.read_text(encoding="utf-8")))
        state.data["daily"] = {
            "date_utc": "2020-01-01",
            "live_realized_pnl_usd": -10,
            "paper_realized_pnl_usd": -4,
        }
        state.roll_day("2026-11-10")
        self.assertEqual(state.realized("live"), 0)
        self.assertEqual(state.realized("paper"), 0)

    def test_cooldown(self):
        path = ROOT / "state" / "state.json"
        state = State(path, {"positions": [], "cooldowns": {}, "daily": {}})
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        state.mark_cooldown("Mint", now - timedelta(minutes=10))
        self.assertTrue(state.on_cooldown("Mint", now, 360))
        self.assertFalse(state.on_cooldown("Mint", now, 5))


class RepoTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(CFG.usdc_mint, USDC_MINT)
        self.assertEqual(CFG.position_size_usd, 5)
        self.assertEqual(CFG.position_size_raw, 5_000_000)
        self.assertEqual(CFG.max_open_positions, 3)
        self.assertEqual(CFG.daily_loss_limit_usd, 10)
        self.assertEqual(CFG.take_profit_multiple, 2)
        self.assertEqual(CFG.take_profit_sell_fraction, 0.5)
        self.assertEqual(CFG.stop_loss_pct, 8)
        self.assertEqual(CFG.trailing_stop_pct, 10)
        self.assertEqual(CFG.min_pair_age_minutes, 45)
        self.assertEqual(CFG.min_liquidity_usd, 50_000)
        self.assertEqual(CFG.min_volume_h1_usd, 30_000)
        self.assertEqual(CFG.max_price_impact_pct, 1.5)
        self.assertEqual(CFG.min_roundtrip_pct, 97)
        self.assertEqual(CFG.sol_fee_reserve, 0.005)
        self.assertEqual(CFG.slippage_bps, 300)

    def test_wrong_usdc_mint_is_rejected(self):
        text = (ROOT / "desk.yaml").read_text(encoding="utf-8")
        bad = text.replace(USDC_MINT, "EPjFWdd5AufqSSqeM2qcJmM7wPEpkCSXzE5vbtCCzSs6")
        path = ROOT / "desk-bad.yaml"
        path.write_text(bad, encoding="utf-8")
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_workflow_is_valid_and_scheduled(self):
        path = ROOT / ".github" / "workflows" / "solana-bot.yml"
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        trigger = data.get(True, data.get("on"))
        self.assertIn("workflow_dispatch", trigger)
        self.assertEqual(trigger["schedule"][0]["cron"], "*/5 * * * *")
        self.assertEqual(data["permissions"]["contents"], "write")
        self.assertEqual(data["concurrency"]["group"], "solana-memecoin-bot")
        self.assertFalse(data["concurrency"]["cancel-in-progress"])
        self.assertIn("python -u main.py", text)
        self.assertIn("SOLANA_PRIVATE_KEY", text)
        self.assertIn("DRY_RUN", text)
        self.assertIn("RPC_URL", text)
        self.assertIn("LOOP_SECONDS", text)
        self.assertIn("github.event.repository.private == false && '270' || '0'", text)
        self.assertIn("timeout-minutes: 8", text)
        self.assertIn("git pull --rebase", text)
        self.assertIn("bot.merge_state", text)
        self.assertNotIn("\n  run: |", text)
        self.assertNotIn("while True", (ROOT / "main.py").read_text(encoding="utf-8"))
        self.assertNotIn("while True", (ROOT / "bot" / "engine.py").read_text(encoding="utf-8"))


class ReconcileTests(unittest.TestCase):
    def test_dust_is_not_a_position_and_a_real_bag_is(self):
        self.assertFalse(is_meaningful(0, 6, 0.0))
        self.assertFalse(is_meaningful(33435, 5, 0.01))
        self.assertTrue(is_meaningful(602_815_105, 6, 5.0))
        self.assertTrue(is_meaningful(15_229_744_841, 6, None))

    def test_entry_uses_the_buy_that_matches_the_balance(self):
        rows = [
            {"strona": "KUPNO", "mint": "GATO", "symbol": "GATO", "ilosc_tokenow": "600", "wartosc_usdc": "5", "cena_usd": "0.0083", "czas_utc": "t1"},
            {"strona": "KUPNO", "mint": "GATO", "symbol": "GATO", "ilosc_tokenow": "602", "wartosc_usdc": "5", "cena_usd": "0.0083", "czas_utc": "t2"},
            {"strona": "KUPNO", "mint": "GATO", "symbol": "GATO", "ilosc_tokenow": "603", "wartosc_usdc": "5", "cena_usd": "0.00829", "czas_utc": "t3"},
        ]
        guess = estimate_entry(rows, "GATO", 603)
        self.assertEqual(guess["opened_at"], "t3")
        self.assertAlmostEqual(guess["cost"], 0.00829 * 603, places=4)

    def test_union_keeps_both_tokens_and_the_larger_balance(self):
        left = {
            "last_run_at": "2026-11-10T18:58:00+00:00",
            "positions": [
                {"mint": "GATO", "amount_raw": "600", "symbol": "GATO"},
                {"mint": "FORG", "amount_raw": "40000", "symbol": "forg"},
            ],
            "cooldowns": {},
            "daily": {},
        }
        right = {
            "last_run_at": "2026-11-10T19:10:00+00:00",
            "positions": [{"mint": "GATO", "amount_raw": "602", "symbol": "GATO"}],
            "cooldowns": {"GATO": "2026-11-10T19:10:00+00:00"},
            "daily": {},
        }
        merged = union_state(left, right)
        amounts = {pos["mint"]: pos["amount_raw"] for pos in merged["positions"]}
        self.assertEqual(amounts["GATO"], "602")
        self.assertEqual(amounts["FORG"], "40000")
        self.assertIn("GATO", merged["cooldowns"])

    def test_conflict_csv_keeps_every_row(self):
        left = "czas_utc,strona,mint,podpis,wartosc_usdc\n18:36,KUPNO,GATO,sig1,5\n"
        right = "czas_utc,strona,mint,podpis,wartosc_usdc\n18:36,KUPNO,GATO,sig1,5\n19:10,KUPNO,GATO,sig2,5\n"
        text = union_csv(left, right)
        self.assertEqual(text.count("KUPNO"), 2)
        conflict = "<<<<<<< HEAD\n" + left + "=======\n" + right + ">>>>>>> bot\n"
        sides = split_conflict(conflict)
        self.assertIn("sig1", sides[0])
        self.assertIn("sig2", sides[1])


class LoopTests(unittest.TestCase):
    def test_missing_or_zero_is_a_single_pass(self):
        self.assertEqual(parse_loop_seconds(None), 0)
        self.assertEqual(parse_loop_seconds(""), 0)
        self.assertEqual(parse_loop_seconds("0"), 0)
        self.assertEqual(parse_loop_seconds("nope"), 0)
        self.assertIsNone(seconds_until_next_cycle(0, 0.0, 1.0, 30))

    def test_public_window_checks_every_30s_and_stops_before_five_minutes(self):
        starts = [0.0]
        now = 0.0
        while len(starts) < 20:
            pause = seconds_until_next_cycle(270, 0.0, now, 30)
            if pause is None:
                break
            now += pause
            starts.append(now)
        self.assertEqual(starts, [0, 30, 60, 90, 120, 150, 180, 210, 240])
        self.assertLess(starts[-1] + 30, 300)
        scan_times = [t for t in starts if t % 90 == 0]
        self.assertEqual(scan_times, [0, 90, 180])


if __name__ == "__main__":
    unittest.main()
