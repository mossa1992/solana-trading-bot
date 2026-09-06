import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class BotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "min_liquidity": 5000,
            "min_h1_volume": 5000,
            "max_age_hours": 48,
            "max_top_holder_pct": 40,
            "signal_threshold": 65,
            "ai_enabled": False,
        }
        self.pair = {
            "url": "https://dexscreener.com/solana/pair",
            "dexId": "pumpfun",
            "baseToken": {"address": "MINT", "name": "Test", "symbol": "TEST"},
            "quoteToken": {"address": bot.SOLANA_NATIVE, "symbol": "SOL"},
            "priceUsd": "0.001",
            "liquidity": {"usd": 12000},
            "volume": {"h1": 9000, "h24": 20000},
            "priceChange": {"m5": 2, "h1": 15, "h24": 40},
            "txns": {"h1": {"buys": 80, "sells": 40}},
            "fdv": 100000,
            "marketCap": 100000,
            "pairCreatedAt": bot.now_ms() - 2 * 3600000,
            "info": {"socials": [{"type": "twitter"}], "websites": []},
        }
        self.report = {
            "mint": "MINT",
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "topHolders": [
                {"owner": "WALLET_A", "pct": 12},
                {"owner": "WALLET_B", "pct": 8},
                {"owner": "WALLET_C", "pct": 7},
            ],
            "risks": [],
            "rugged": False,
            "lpLockedPct": 90,
            "totalHolders": 100,
            "knownAccounts": {},
        }

    def test_safe_candidate_passes_gate(self):
        candidate = bot.normalize_candidate({"tokenAddress": "MINT", "description": "x"}, self.pair, self.report, self.cfg)
        self.assertTrue(candidate["hard_gate_passed"])
        self.assertGreater(candidate["raw_score"], 0)
        self.assertEqual(candidate["buys_h1"], 80)

    def test_critical_risk_blocks_candidate(self):
        report = json.loads(json.dumps(self.report))
        report["risks"] = [{"name": "critical", "level": "critical"}]
        candidate = bot.normalize_candidate({"tokenAddress": "MINT"}, self.pair, report, self.cfg)
        self.assertFalse(candidate["hard_gate_passed"])
        self.assertTrue(any("عالياً" in reason or "حرجاً" in reason for reason in candidate["gate_reasons"]))

    def test_mint_authority_blocks_candidate(self):
        report = json.loads(json.dumps(self.report))
        report["token"]["mintAuthority"] = "AUTH"
        candidate = bot.normalize_candidate({"tokenAddress": "MINT"}, self.pair, report, self.cfg)
        self.assertFalse(candidate["hard_gate_passed"])

    def test_low_liquidity_blocks_candidate(self):
        pair = json.loads(json.dumps(self.pair))
        pair["liquidity"]["usd"] = 10
        candidate = bot.normalize_candidate({"tokenAddress": "MINT"}, pair, self.report, self.cfg)
        self.assertFalse(candidate["hard_gate_passed"])

    def test_format_contains_address_and_disclaimer(self):
        candidate = bot.normalize_candidate({"tokenAddress": "MINT"}, self.pair, self.report, self.cfg)
        text = bot.format_signal(candidate, bot.ai_assess(candidate, self.cfg))
        self.assertIn("MINT", text)
        self.assertIn("لا ينفذ هذا البوت صفقات", text)


if __name__ == "__main__":
    unittest.main()

class TelegramControlTests(unittest.TestCase):
    def test_unauthorized_update_is_ignored(self):
        update = {"message": {"chat": {"id": "999"}, "from": {"id": "999"}, "text": "/status"}}
        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "6631432245"}, clear=False), patch("bot.telegram_api") as api:
            bot.handle_command(update)
            api.assert_not_called()

    def test_help_command_sends_response_to_admin(self):
        update = {"message": {"chat": {"id": "6631432245"}, "from": {"id": "6631432245"}, "text": "/help"}}
        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "6631432245"}, clear=False), patch("bot.telegram_api") as api:
            bot.handle_command(update)
            self.assertTrue(api.called)
            payload = api.call_args.args[1]
            self.assertEqual(payload["chat_id"], "6631432245")
            self.assertIn("/status", payload["text"])

    def test_set_rejects_unsafe_value(self):
        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "6631432245"}, clear=False):
            self.assertIn("غير صالحة", bot.persist_setting("signal_threshold", "999"))


if __name__ == "__main__":
    unittest.main()


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "risk_per_trade": 0.03,
            "max_position_pct": 0.25,
            "max_positions": 4,
            "fee_bps": 30,
            "entry_slippage_bps": 50,
            "exit_slippage_bps": 100,
            "max_hold_hours": 12,
            "min_liquidity_factor": 0.5,
            "take_profit_pct": 0.10,
            "stop_loss_pct": 0.05,
            "entry_sol": 1.0,
            "unit_sol_usd": 150.0,
            "sol_usd": 150.0,
        }
        self.candidate = {
            "mint": "PAPER_MINT",
            "symbol": "PAPER",
            "name": "Paper Token",
            "price_usd": 1.0,
            "liquidity_usd": 100000,
            "hard_gate_passed": True,
            "raw_score": 80,
            "assessment": {"action": "SIGNAL"},
            "buys_h1": 80,
            "sells_h1": 40,
        }

    def test_open_accounts_for_fee_and_slippage(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                event = bot.paper_open(state, self.candidate, self.cfg, "2026-08-24T00:00:00+00:00")
                self.assertIsNotNone(event)
                self.assertLess(state["cash"], 200.0)
                self.assertIn("PAPER_MINT", state["positions"])
                self.assertGreater(state["fees_paid"], 0.0)

    def test_stop_loss_realizes_loss_without_negative_cash(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                bot.paper_open(state, self.candidate, self.cfg, "2026-08-24T00:00:00+00:00")
                event = bot.paper_close(state, "PAPER_MINT", 0.80, 1.0, "STOP_LOSS", self.cfg, "2026-08-24T01:00:00+00:00")
                self.assertIsNotNone(event)
                self.assertLess(event["pnl"], 0.0)
                self.assertGreaterEqual(state["cash"], 0.0)
                self.assertNotIn("PAPER_MINT", state["positions"])

    def test_profitable_close_reinvests_into_cash_balance(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                bot.paper_open(state, self.candidate, self.cfg, "2026-08-24T00:00:00+00:00")
                event = bot.paper_close(state, "PAPER_MINT", 1.10, 1.0, "TAKE_PROFIT_10PCT", self.cfg, "2026-08-24T01:00:00+00:00")
                self.assertGreater(event["pnl"], 0.0)
                self.assertGreater(state["cash"], 200.0)

    def test_stop_does_not_reopen_same_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "paper_state.json"
            ledger_path = Path(temp) / "ledger.jsonl"
            with patch.object(bot, "PAPER_STATE_FILE", state_path), patch.object(bot, "PAPER_LEDGER_FILE", ledger_path):
                bot.save_paper_state(bot.paper_default_state())
                bot.paper_process_results([self.candidate], 65, 5000)
                stopped = dict(self.candidate)
                stopped["price_usd"] = 0.80
                events = bot.paper_process_results([stopped], 65, 5000)
                self.assertTrue(any(event.get("reason") == "STOP_LOSS" for event in events))
                state = bot.load_paper_state()
                self.assertEqual(state["positions"], {})


class PaperVisualTests(unittest.TestCase):
    def test_close_message_has_green_and_red_markers(self):
        profit = bot.format_paper_event({"type": "close", "symbol": "WIN", "reason": "TAKE_PROFIT_2", "price": 2, "pnl": 5.0, "pnl_pct": 25.0, "fee": 0.1})
        loss = bot.format_paper_event({"type": "close", "symbol": "LOSS", "reason": "STOP_LOSS", "price": 0.8, "pnl": -5.0, "pnl_pct": -20.0, "fee": 0.1})
        self.assertIn("🟩", profit)
        self.assertIn("🟥", loss)

    def test_open_position_status_changes_marker_by_pnl(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_STATE_FILE", Path(temp) / "paper_state.json"), patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                bot.paper_open(state, {"mint": "M", "symbol": "OPEN", "name": "Open", "price_usd": 1.0, "buys_h1": 80, "sells_h1": 40}, self._cfg(), "2026-08-24T00:00:00+00:00")
                state["positions"]["M"]["last_price"] = 1.2
                bot.save_paper_state(state)
                self.assertIn("🟩", bot.paper_status())
                state["positions"]["M"]["last_price"] = 0.7
                bot.save_paper_state(state)
                self.assertIn("🟥", bot.paper_status())

    @staticmethod
    def _cfg():
        return {"risk_per_trade": 0.03, "max_position_pct": 0.25, "max_positions": 4, "fee_bps": 30, "entry_slippage_bps": 50, "exit_slippage_bps": 100, "max_hold_hours": 12, "min_liquidity_factor": 0.5, "take_profit_pct": 0.10, "stop_loss_pct": 0.05, "entry_sol": 1.0, "unit_sol_usd": 150.0, "sol_usd": 150.0}


class StopCommandTests(unittest.TestCase):
    def test_stop_command_sets_shutdown_event(self):
        bot.STOP_EVENT.clear()
        update = {"message": {"chat": {"id": "6631432245"}, "from": {"id": "6631432245"}, "text": "/stop"}}
        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "6631432245"}, clear=False), patch("bot.telegram_api"):
            bot.handle_command(update)
        self.assertTrue(bot.STOP_EVENT.is_set())
        bot.STOP_EVENT.clear()


class MomentumRuleTests(unittest.TestCase):
    def test_sell_dominant_candidate_does_not_open(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                candidate = {"mint": "SELL", "symbol": "SELL", "name": "Sell", "price_usd": 1.0, "buys_h1": 20, "sells_h1": 80}
                self.assertIsNone(bot.paper_open(state, candidate, self._cfg(), "2026-08-24T00:00:00+00:00"))

    def test_one_sol_and_five_ten_levels(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "PAPER_LEDGER_FILE", Path(temp) / "ledger.jsonl"):
                state = bot.paper_default_state()
                candidate = {"mint": "MOM", "symbol": "MOM", "name": "Momentum", "price_usd": 2.0, "buys_h1": 90, "sells_h1": 10}
                event = bot.paper_open(state, candidate, self._cfg(), "2026-08-24T00:00:00+00:00")
                self.assertIsNotNone(event)
                pos = state["positions"]["MOM"]
                self.assertEqual(pos["entry_sol"], 1.0)
                self.assertAlmostEqual(pos["stop_price"], 1.90, places=6)
                self.assertAlmostEqual(pos["take_profit_price"], 2.20, places=6)

    @staticmethod
    def _cfg():
        return {"risk_per_trade": 0.03, "max_position_pct": 1.0, "max_positions": 2, "fee_bps": 30, "entry_slippage_bps": 50, "exit_slippage_bps": 100, "max_hold_hours": 12, "min_liquidity_factor": 0.5, "take_profit_pct": 0.10, "stop_loss_pct": 0.05, "entry_sol": 1.0, "unit_sol_usd": 150.0, "sol_usd": 100.0}
