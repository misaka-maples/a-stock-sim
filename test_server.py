"""Isolated REST API tests: market data and notification providers are mocked."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import requests
import daily_momentum
import notifier
import server
import sim_trader
import snapshot_manager
from daily_momentum import MomentumRotationProStrategy
from sim_trader import SimAccount, MarketFeed, MomentumRotationStrategy
from test_daily_momentum import StubFeed
from trade_calendar import BEIJING

NOW = dt.datetime(2026, 9, 22, 10, tzinfo=BEIJING)


class TestServerAPI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ctx = server.ServerContext()
        self.stack.enter_context(patch.object(server, "ctx", self.ctx))
        # Keep production authentication enabled; every route test logs in
        # with private credentials generated only inside its temporary tree.
        manager = server.auth_manager
        for key, value in dict(enabled=True, private_dir=self.root / "auth" / "private",
                               credentials_path=self.root / "auth" / "private" / "api_credentials.json",
                               config_path=self.root / "auth" / "private" / "api_auth.json",
                               _config=None, _sessions={}, _failures={}).items():
            self.stack.enter_context(patch.object(manager, key, value))
        manager.initialize()
        class ClockDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
        proxy = {key: getattr(dt, key) for key in dir(dt) if not key.startswith("__")}
        proxy["datetime"] = ClockDateTime
        for module in (server, sim_trader):
            self.stack.enter_context(patch.object(module, "datetime", SimpleNamespace(**proxy)))
        self.stack.enter_context(patch.object(snapshot_manager, "get_now", return_value=NOW))
        paths = dict(SIM_ACCOUNT_PATH=self.root / "account.json", STRATEGY_CONFIG_PATH=self.root / "strategy.json",
                     NOTIFICATION_CONFIG_PATH=self.root / "notification.json", SNAPSHOTS_DIR=self.root / "snapshots",
                     INDEX_FILE=self.root / "snapshots" / "index.json")
        for key, value in paths.items():
            self.stack.enter_context(patch.object(snapshot_manager, key, value))
        for key in ("STRATEGY_CONFIG_PATH", "SNAPSHOTS_DIR"):
            self.stack.enter_context(patch.object(server, key, paths[key]))
        self.stack.enter_context(patch.object(notifier, "NOTIFICATION_CONFIG_PATH", paths["NOTIFICATION_CONFIG_PATH"]))
        self.stack.enter_context(patch.object(notifier.NotificationManager, "_instance", None))
        self.stack.enter_context(patch.object(snapshot_manager.SnapshotManager, "_instance", None))
        self.stack.enter_context(patch.object(snapshot_manager.SnapshotManager, "_restore_callbacks", [server.on_snapshot_restored]))
        self.stack.enter_context(patch.object(daily_momentum, "HistoricalDataFeed", return_value=StubFeed()))
        # All provider methods remain mocked for the entire test, including
        # async dispatch: no API action can send an external notification.
        for provider, method in ((notifier.WxPusherClient, "send_message"),
                                 (notifier.WxPusherClient, "send_simple_push"),
                                 (notifier.ServerChanClient, "send_message")):
            self.stack.enter_context(patch.object(provider, method, return_value={"success": True, "message": "mock"}))
        self.stack.enter_context(patch.object(notifier.NotificationManager, "async_dispatch", return_value=None))
        self.stack.enter_context(patch.object(notifier.NotificationManager, "dispatch_message",
            return_value=[{"success": True, "channel": "mock", "message": "OK"}]))
        self.stack.enter_context(patch.object(notifier.WxPusherClient, "create_qrcode", return_value={
            "success": True, "data": {"url": "https://example.test/qr", "code": "ticket_mock"}}))
        self.stack.enter_context(patch.object(notifier.WxPusherClient, "query_scan_status", return_value={
            "success": True, "scanned": True, "uid": "UID_mock_scanned"}))
        self.network = self.stack.enter_context(patch.object(requests.Session, "request",
            side_effect=AssertionError("API unit tests must not use the network")))
        self.ctx.account = SimAccount(initial_cash=500000., strict_t1=False, save_path=str(paths["SIM_ACCOUNT_PATH"]))
        self.ctx.symbols = ["sh600519", "sz002594", "sz000001"]
        self.catalog_path = self.root / "stock_universe.json"
        self.catalog_path.write_text(json.dumps({"stocks": [
            {"symbol": "sh600519", "name": "贵州茅台"},
            {"symbol": "sz002594", "name": "比亚迪"},
            {"symbol": "sz000001", "name": "平安银行"},
            {"symbol": "sh600036", "name": "招商银行"},
            {"symbol": "sz300001", "name": "创业测试银行"},
            {"symbol": "sz600036", "name": "错误交易所"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.stack.enter_context(patch.object(sim_trader.StockUniverse, "UNIVERSE_FILE", self.catalog_path))
        self.ctx.strategy = MomentumRotationProStrategy(self.ctx.account, self.ctx.symbols, feed=StubFeed(), industry_map={})
        self.quotes = {s: dict(symbol=s, code=s[-6:], name="测试股票", current=10., pre_close=10.,
                               open=10., change=0., change_pct=0., time=NOW.strftime("%Y%m%d%H%M%S"),
                               volume_lots=10000, fresh=True, bids=[dict(price=9.99, volume=100)],
                               asks=[dict(price=10.01, volume=100)]) for s in self.ctx.symbols}
        def fetch(symbols, **kwargs):
            normalized = [MarketFeed.normalize_symbol(s) for s in symbols]
            return {s: copy.deepcopy(self.quotes[s]) for s in normalized if s in self.quotes}
        self.fetch = self.stack.enter_context(patch.object(MarketFeed, "fetch_quotes", side_effect=fetch))
        self.ctx.last_quotes = copy.deepcopy(self.quotes)
        # Avoid launching the polling worker; requests still exercise FastAPI.
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.get("/api/summary").status_code, 401)
        password = json.loads(manager.credentials_path.read_text())["password"]
        logged_in = self.client.post("/api/auth/login", json={"password": password})
        self.assertEqual(logged_in.status_code, 200)
        self.assertTrue(self.client.get("/api/auth/status").json()["authenticated"])

    def tearDown(self):
        self.network.assert_not_called()
        if notifier.NotificationManager._instance is not None:
            notifier.NotificationManager._instance.executor.shutdown(wait=True)

    def order(self, side="BUY", symbol="sz002594", shares=200, price=0):
        return self.client.post("/api/trade/order", json=dict(symbol=symbol, side=side,
                                shares=shares, price=price, reason="offline API test"))

    def test_index_page(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("A股", result.text)

    def test_get_summary(self):
        result = self.client.get("/api/summary")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["initial_cash"], 500000.)
        self.assertEqual(result.json()["total_equity"], result.json()["cash"])

    def test_get_status(self):
        result = self.client.get("/api/status")
        self.assertEqual(result.status_code, 200)
        self.assertIn("market_status", result.json())
        self.assertIn("strategy_active", result.json())

    def test_get_history_status(self):
        result = self.client.get("/api/history/status")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertIn("data_quality", data)
        self.assertIn("unavailable_symbols", data)
        self.assertIn("quotes_summary", data)

    def test_history_refresh(self):
        result = self.client.post("/api/history/refresh")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertTrue(data.get("success"))
        thread = getattr(self.ctx.strategy, "_history_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def test_strategy_toggle(self):
        for active in (False, True):
            result = self.client.post("/api/strategy/toggle", json={"active": active})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["strategy_active"], active)

    def test_manual_trade_order(self):
        buy = self.order()
        self.assertEqual(buy.status_code, 200)
        self.assertTrue(buy.json()["success"], buy.json())
        self.assertEqual(buy.json()["trade"]["price"], 10.01)
        self.assertEqual(buy.json()["trade"]["time"], "2026-09-22 10:00:00")
        self.assertTrue(any(p["symbol"] == "sz002594" for p in self.client.get("/api/positions").json()))
        sold = self.order(side="SELL")
        self.assertTrue(sold.json()["success"], sold.json())
        self.assertEqual(sold.json()["trade"]["price"], 9.99)
        self.assertNotIn("sz002594", self.ctx.account.positions)
        self.assertAlmostEqual(self.ctx.account.cash, 500000. - 4. - sum(t["fees"] for t in self.ctx.account.trades))

    def test_manual_trade_rejects_non_main_board(self):
        for symbol in ("sz301520", "sh688001"):
            result = self.order(symbol=symbol, shares=100, price=30)
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()["success"])
            self.assertIn("不属于沪深主板", result.json()["reason"])
        # Unsupported boards require their own rules; preserve old positions
        # rather than fabricating fills with the main-board-only model.
        self.ctx.account.positions["sz301520"] = dict(symbol="sz301520", name="历史仓位", total_shares=300,
            available_shares=300, cost_price=25., last_price=32., market_value=9600., unrealized_pnl=2100.,
            pnl_pct=28., buy_lots=[])
        self.assertFalse(self.order(side="SELL", symbol="sz301520", shares=300, price=32).json()["success"])
        self.assertIn("sz301520", self.ctx.account.positions)

    def test_universe_and_search(self):
        result = self.client.get("/api/universe")
        self.assertEqual(result.status_code, 200)
        self.assertIn("主板", result.json()["board_type"])
        found = self.client.get("/api/stock/search?q=000001")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()[0]["code"], "000001")

    def test_search_catalog_names_partial_and_unquoted_metadata(self):
        # Provider names in the running quote cache can override the directory.
        self.ctx.last_quotes = {}
        result = self.client.get("/api/stock/search", params={"q": "银行"})
        self.assertEqual(result.status_code, 200)
        records = {row["symbol"]: row for row in result.json()}
        self.assertEqual(set(records), {"sz000001", "sh600036"})
        self.assertFalse(records["sh600036"]["quote_available"])
        self.assertFalse(records["sh600036"]["fresh"])
        self.assertEqual(self.client.get("/api/stock/search", params={"q": "60003"}).json()[0]["symbol"], "sh600036")

    def test_search_validation_does_not_probe_unknown_or_nonmain_codes(self):
        self.fetch.reset_mock()
        for query in ("http://bad.test", "sz300001", "sz600036", "999999"):
            self.assertEqual(self.client.get("/api/stock/search", params={"q": query}).status_code, 404)
        for query in ("", "x" * 41):
            self.assertEqual(self.client.get("/api/stock/search", params={"q": query}).status_code, 422)
        self.assertEqual(self.client.get("/api/stock/search", params={"q": "  "}).status_code, 400)
        self.fetch.assert_not_called()
        for symbol in ("sz600036", "sz300001", "60003", "http://a"):
            self.assertIn(self.client.get("/api/stock/quote", params={"symbol": symbol}).status_code, (400, 422))
        self.fetch.assert_not_called()

    def test_quote_fetch_is_outside_lock_and_read_preserves_state(self):
        before = copy.deepcopy((self.ctx.account.positions, self.ctx.account.trades, self.ctx.symbols, self.ctx.last_quotes))
        def unlocked(symbols):
            self.assertFalse(self.ctx.lock._is_owned())
            return {s: copy.deepcopy(self.quotes[s]) for s in symbols if s in self.quotes}
        with patch.object(MarketFeed, "fetch_quotes", side_effect=unlocked):
            response = self.client.get("/api/stock/quote", params={"symbol": "000001"})
            search = self.client.get("/api/stock/search", params={"q": "600519"})
        self.assertEqual(search.status_code, 200)
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["fresh"])
        self.assertTrue(data["can_buy"])
        self.assertFalse(data["can_sell"])
        self.assertEqual(data["quote"]["asks"][0]["price"], 10.01)
        self.assertEqual(before, (self.ctx.account.positions, self.ctx.account.trades, self.ctx.symbols, self.ctx.last_quotes))

    def test_quote_invalid_stale_and_unavailable_fail_closed(self):
        for update in ({"current": float("nan")}, {"asks": [{"price": float("inf"), "volume": 1}]},
                       {"symbol": "sh600519"}):
            with self.subTest(update=update):
                quote = dict(self.quotes["sz000001"], **update)
                with patch.object(MarketFeed, "fetch_quotes", return_value={"sz000001": quote}):
                    data = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
                self.assertFalse(data["quote_available"])
                self.assertFalse(data["can_buy"])
        for stamp in ((NOW - dt.timedelta(seconds=121)), (NOW + dt.timedelta(seconds=1)), (NOW - dt.timedelta(days=1))):
            quote = dict(self.quotes["sz000001"], time=stamp.strftime("%Y%m%d%H%M%S"), fresh=True)
            with patch.object(MarketFeed, "fetch_quotes", return_value={"sz000001": quote}):
                data = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
            self.assertTrue(data["quote_available"])
            self.assertFalse(data["fresh"])
            self.assertFalse(data["can_buy"])
        with patch.object(MarketFeed, "fetch_quotes", side_effect=RuntimeError("provider down")):
            data = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
        self.assertIsNone(data["quote"])
        self.assertEqual(data["quote_status"], "unavailable")

    def test_quote_t1_preview_is_copy_and_requires_sell_book(self):
        self.ctx.account.strict_t1 = True
        self.ctx.account.positions["sz000001"] = dict(total_shares=300, available_shares=0,
            buy_lots=[dict(date="2026-09-21", shares=200), dict(date="2026-09-22", shares=100)])
        result = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
        self.assertEqual(result["position"]["available_shares"], 200)
        self.assertTrue(result["can_sell"])
        self.assertEqual(self.ctx.account.positions["sz000001"]["available_shares"], 0)
        self.quotes["sz000001"]["bids"] = []
        self.assertFalse(self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()["can_sell"])
        self.quotes["sz000001"]["is_st"] = True
        self.assertFalse(self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()["can_buy"])

    def test_stock_read_routes_require_auth_and_session_for_trade_flags(self):
        anonymous = TestClient(server.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get("/api/stock/search", params={"q": "000001"}).status_code, 401)
        self.assertEqual(anonymous.get("/api/stock/quote", params={"symbol": "000001"}).status_code, 401)
        with patch.object(MarketFeed, "is_trading_time", return_value=False):
            data = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
        self.assertTrue(data["quote_available"])
        self.assertFalse(data["can_buy"])
        self.assertFalse(data["can_sell"])

    def test_stock_quote_declared_stale_suspended_and_invalid_book_disable_orders(self):
        for update in ({"fresh": False}, {"suspended": True}, {"volume_lots": 0},
                       {"asks": [{"price": 50, "volume": 100}]},
                       {"asks": [{"price": 10.01, "volume": -1}]}):
            with self.subTest(update=update):
                quote = dict(self.quotes["sz000001"], **update)
                with patch.object(MarketFeed, "fetch_quotes", return_value={"sz000001": quote}):
                    data = self.client.get("/api/stock/quote", params={"symbol": "000001"}).json()
                self.assertFalse(data["can_buy"])
        quote = dict(self.quotes["sz000001"], asks=[{"price":"10.01", "volume":"100"}])
        with patch.object(MarketFeed, "fetch_quotes", return_value={"sz000001": quote}):
            data = self.client.get("/api/stock/quote", params={"symbol":"000001"}).json()
        self.assertTrue(data["can_buy"])
        self.assertEqual(data["quote"]["asks"][0]["price"],10.01)

    def test_search_exact_priority_and_bounded_provider_batch(self):
        stocks = [{"symbol": "sh600%03d" % i, "name": "银行%d" % i} for i in range(20)]
        self.catalog_path.write_text(json.dumps({"stocks": stocks}), encoding="utf-8")
        self.ctx.last_quotes = {}
        self.fetch.reset_mock()
        result = self.client.get("/api/stock/search", params={"q": "600"})
        self.assertEqual(len(result.json()), 10)
        self.assertEqual(len(self.fetch.call_args.args[0]), 10)
        self.assertEqual(self.client.get("/api/stock/search", params={"q": "600519"}).json()[0]["symbol"], "sh600519")

    def test_history_retry_countdown_is_live_and_does_not_mutate_snapshot(self):
        import time
        self.ctx.strategy.data_status['next_failed_retry_seconds']=900
        self.ctx.strategy._next_failed_retry=time.monotonic()+120
        result=self.client.get('/api/status').json()['data_quality']
        self.assertTrue(118<=result['next_failed_retry_seconds']<=120)
        self.assertEqual(self.ctx.strategy.data_status['next_failed_retry_seconds'],900)

    def test_get_performance(self):
        result = self.client.get("/api/performance")
        self.assertEqual(result.status_code, 200)
        for field in ("summary", "equity_curve", "symbol_stats", "daily_stats"):
            self.assertIn(field, result.json())
        for field in ("today_pnl", "realized_pnl", "win_rate", "profit_loss_ratio"):
            self.assertIn(field, result.json()["summary"])

    def test_backtest_endpoints(self):
        config = self.client.get("/api/backtest/config")
        self.assertEqual(config.status_code, 200)
        self.assertIn("strategies", config.json())
        self.assertIn("pools", config.json())
        report = dict(summary=dict(initial_cash=100000., final_equity=99990.),
                      equity_curve=[dict(date="2026-09-15", equity=99990.)], trades=[])
        with patch.object(server, "run_backtest", return_value=report) as run:
            result = self.client.post("/api/backtest/run", json=dict(start_date="2026-08-01", end_date="2026-09-15",
                initial_cash=100000., strategy_name="Momentum_Rotation_Pro", symbols=["sh600237"],
                stop_loss=-7., risk_per_trade=.005, max_sector_weight=.25))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["success"], result.json())
        self.assertEqual(result.json()["data"]["summary"], report["summary"])
        self.assertEqual(run.call_args.kwargs["symbols"], ["sh600237"])
        self.assertEqual(run.call_args.kwargs["config"]["stop_loss_pct"], -7.)
        self.assertEqual(run.call_args.kwargs["config"]["risk_per_trade"], .005)

    def test_backtest_current_snapshot_captures_complete_config_pool_and_industry_outside_lock(self):
        config = server.MomentumConfig(max_positions=8, max_stock_weight=.17, stop_loss_pct=-8,
            momentum_w20=1.4, momentum_w60=.7, alpha_high=.91, require_positive_candle=False,
            rebalance_every=7, rank_buffer=4, min_holding_days=9, cooldown_days=2, participation_rate=.025)
        self.ctx.strategy.config = config
        self.ctx.strategy.industry_map = {"sh600519": {"industry": "食品"}, "sz002594": "汽车"}
        self.ctx.strategy.data_status["industry_data"] = {"source": "fixture", "warning": "not_point_in_time"}
        expected_symbols = list(self.ctx.symbols)
        before_account = self.ctx.account.save_path.read_text() if isinstance(self.ctx.account.save_path, Path) else Path(self.ctx.account.save_path).read_text()

        def research_only(**kwargs):
            self.assertFalse(self.ctx.lock._is_owned(), "historical I/O must run outside the server lock")
            self.assertEqual(kwargs["config"], server.asdict(config))
            self.assertEqual(kwargs["symbols"], expected_symbols)
            self.assertNotIn("take_profit_trigger_pct", kwargs)
            # A concurrent runtime edit cannot change the captured research input.
            self.ctx.symbols.append("sh600036")
            self.ctx.strategy.industry_map["sh600519"]["industry"] = "已修改"
            self.assertEqual(kwargs["symbols"], expected_symbols)
            self.assertEqual(kwargs["industry_map"]["sh600519"]["industry"], "食品")
            return dict(summary={"symbol_count": len(kwargs["symbols"])}, parameters=kwargs["config"])

        with patch.object(server, "run_backtest", side_effect=research_only) as run:
            result = self.client.post("/api/backtest/run", json=dict(
                strategy_name="Momentum_Rotation_Pro", pool_type="current_watchlist", use_current_config=True,
                take_profit=99, stop_loss=-1)).json()
        self.assertTrue(result["success"], result)
        context = result["data"]["request_context"]
        self.assertEqual(context["source_params"], server.asdict(config))
        self.assertEqual(context["pool_size"], 3)
        self.assertEqual(context["industry_coverage"], dict(mapped=2, total=3, ratio=2/3,
            main_board_mapped=2, main_board_total=3, main_board_ratio=2/3))
        self.assertEqual(context["industry_source"]["source"], "fixture")
        self.assertEqual(context["ignored_params"], ["take_profit", "stop_loss"])
        self.assertEqual(Path(self.ctx.account.save_path).read_text(), before_account)
        run.assert_called_once()

    def test_backtest_presets_remain_explicitly_distinct_from_current_watchlist(self):
        self.ctx.strategy.config = server.MomentumConfig(max_positions=8)
        with patch.object(server, "run_backtest", return_value=dict(summary={})) as run:
            result = self.client.post("/api/backtest/run", json=dict(
                strategy_name="Momentum_Rotation_Pro", pool_type="core_active", use_current_config=True)).json()
        self.assertTrue(result["success"])
        self.assertEqual(len(run.call_args.kwargs["symbols"]), 20)
        self.assertEqual(run.call_args.kwargs["config"]["max_positions"], 8)
        context = result["data"]["request_context"]
        self.assertEqual(context["main_board_count"], 16)
        self.assertEqual(context["industry_coverage"]["mapped"], 0)
        self.assertTrue(any("未知行业" in note for note in context["model_limitations"]))
        self.ctx.strategy.industry_map = {s: "测试行业" for s in server.PRESET_POOLS["core_active"]
                                         if server.StockUniverse.is_main_board(s)}
        with patch.object(server, "run_backtest", return_value=dict(summary={})):
            mapped = self.client.post("/api/backtest/run", json=dict(
                pool_type="core_active", use_current_config=True)).json()["data"]["request_context"]
        self.assertEqual(mapped["industry_coverage"], dict(mapped=16, total=20, ratio=.8,
            main_board_mapped=16, main_board_total=16, main_board_ratio=1.))
        self.assertFalse(any("未知行业" in note for note in mapped["model_limitations"]))

    def test_backtest_snapshot_rejects_mismatched_strategy_and_ambiguous_inputs(self):
        cases = [dict(strategy_name="Momentum_Rotation", use_current_config=True),
                 dict(use_current_config=True, parameters={"max_positions": 3}),
                 dict(pool_type="current_watchlist", symbols=["sh600519"]),
                 dict(symbols=[])]
        with patch.object(server, "run_backtest") as run:
            for request in cases:
                with self.subTest(request=request):
                    result = self.client.post("/api/backtest/run", json=request).json()
                    self.assertFalse(result["success"], result)
            run.assert_not_called()

    def test_backtest_complete_independent_parameters_and_ignored_legacy_knobs(self):
        config = server.asdict(server.MomentumConfig(max_positions=5, volatility_target=.19,
            momentum_w20=1.3, alpha_high=.9, rebalance_every=8, require_positive_candle=False))
        with patch.object(server, "run_backtest", return_value=dict(summary={})) as run:
            result = self.client.post("/api/backtest/run", json=dict(parameters=config,
                take_profit=20, max_holding_days=1)).json()
        self.assertTrue(result["success"])
        self.assertEqual(run.call_args.kwargs["config"], config)
        self.assertEqual(result["data"]["request_context"]["ignored_params"], ["max_holding_days", "take_profit"])
        self.assertNotIn("max_holding_days", run.call_args.kwargs)

    def test_backtest_get_post_use_same_default_momentum_rules(self):
        with patch.object(server, "run_backtest", return_value=dict(summary={})) as run:
            self.client.post("/api/backtest/run", json={})
            post_arguments = copy.deepcopy(run.call_args.kwargs)
            self.client.get("/api/backtest/run")
            self.assertEqual(run.call_args.kwargs, post_arguments)
            self.assertEqual(post_arguments["config"]["max_positions"], 6)

    def test_status_candidate_preview_is_separate_from_history_coverage_and_does_not_trade(self):
        self.ctx.strategy.data_status = dict(status="partial", loaded=2, requested=3, unavailable=1)
        before_state = copy.deepcopy(self.ctx.account.strategy_state)
        with patch.object(self.ctx.strategy, "preview_candidates", create=True,
                          return_value=[dict(symbol="sh600519")]) as preview, \
             patch.object(self.ctx.strategy, "on_tick") as on_tick:
            result = self.client.get("/api/status").json()
        self.assertEqual(result["radar_count"], 1)
        self.assertEqual(result["data_quality"]["loaded"], 2)
        self.assertEqual(result["data_quality"]["requested"], 3)
        self.assertEqual(self.ctx.account.strategy_state, before_state)
        preview.assert_called_once()
        on_tick.assert_not_called()

    def test_strategy_list(self):
        result = self.client.get("/api/strategy/list")
        self.assertEqual(result.status_code, 200)
        strategies = result.json()["strategies"]
        self.assertTrue({"Momentum_Rotation", "Minervini_SEPA", "TurtleBreakout"} <= {s["id"] for s in strategies})
        for strategy in strategies:
            self.assertEqual(strategy["performance_status"], "unverified")
            self.assertEqual(strategy["one_year_return"], "待验证")
            self.assertFalse(strategy["recommended"])

    def test_strategy_switch(self):
        for sid, cls, label in (("Momentum_Rotation", MomentumRotationStrategy, "基础日线动量"),
                                ("Momentum_Rotation_Pro", MomentumRotationProStrategy, "日线动量增强")):
            result = self.client.post("/api/strategy/switch", json={"strategy_id": sid})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()["success"])
            self.assertIn(label, result.json()["strategy_name"])
            self.assertIsInstance(self.ctx.strategy, cls)
            self.assertEqual(self.client.get("/api/status").json()["strategy_id"], sid)
        self.assertEqual(self.client.post("/api/strategy/switch", json={"strategy_id": "invalid"}).status_code, 400)

    def test_notification_endpoints(self):
        config = self.client.get("/api/notification/config")
        self.assertEqual(config.status_code, 200)
        self.assertIn("enabled", config.json())
        update = self.client.post("/api/notification/config", json=dict(enabled=True, provider="both",
            wxpusher_app_token="AT_mock", wxpusher_uids=["UID_mock"], serverchan_sendkey="SCT_mock"))
        self.assertTrue(update.json()["success"])
        self.assertTrue(update.json()["config"]["enabled"])
        qr = self.client.post("/api/notification/wxpusher/qrcode", json={"app_token": "AT_mock"})
        self.assertEqual(qr.json()["data"]["code"], "ticket_mock")
        scan = self.client.get("/api/notification/wxpusher/scan_status?code=ticket_mock").json()
        self.assertIn("UID_mock_scanned", scan["bound_uids"])
        callback = self.client.post("/api/notification/wxpusher/callback",
            json=dict(action="app_subscribe", data=dict(uid="UID_webhook_mock")))
        self.assertEqual(callback.json()["code"], 1000)
        self.assertTrue(self.client.post("/api/notification/test").json()["success"])

    def test_purge_non_main_api(self):
        self.ctx.account.positions["sz301611"] = dict(symbol="sz301611", name="历史非主板", total_shares=300,
            cost_price=90., last_price=98., market_value=29400., unrealized_pnl=2400.)
        self.ctx.account.trades.append(dict(symbol="sz301611", side="BUY", shares=300, price=90., amount=27000., fees=7.))
        result = self.client.post("/api/account/purge_non_main")
        self.assertTrue(result.json()["success"])
        self.assertNotIn("sz301611", self.ctx.account.positions)
        self.assertIn("sz301611", result.json()["removed_positions"])

    def test_snapshot_api(self):
        self.ctx.account.save()
        created = self.client.post("/api/snapshot/create", json=dict(tag="manual", description="offline test"))
        self.assertTrue(created.json()["success"], created.json())
        sid = created.json()["snapshot"]["meta"]["snapshot_id"]
        listed = self.client.get("/api/snapshot/list").json()
        self.assertTrue(any(s["snapshot_id"] == sid for s in listed["snapshots"]))
        downloaded = self.client.get("/api/snapshot/download", params={"id": sid})
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["content-type"], "application/gzip")
        restored = self.client.post("/api/snapshot/restore", json=dict(snapshot_id=sid, backup_current=True))
        self.assertTrue(restored.json()["success"], restored.json())
        uploaded = self.client.post("/api/snapshot/upload", content=downloaded.content,
            headers={"x-filename": sid+".tar.gz", "content-type": "application/gzip"})
        self.assertTrue(uploaded.json()["success"], uploaded.json())
        self.assertTrue(self.client.delete("/api/snapshot/delete", params={"id": sid}).json()["success"])

    def test_limit_and_expired_quote_rejections(self):
        self.assertFalse(self.order(price=10.).json()["success"])
        self.assertFalse(self.ctx.account.trades)
        self.quotes["sz002594"]["time"] = "20260921100000"
        old = self.order(price=20.)
        self.assertFalse(old.json()["success"])
        self.assertIn("过期", old.json()["reason"])
        self.assertFalse(self.ctx.account.trades)

    def test_risk_parameters_persist_and_invalid_stop_is_atomic(self):
        update = self.client.post("/api/strategy/config", json=dict(risk_per_trade=.005,
            max_sector_weight=.25, cooldown_days=4, participation_rate=.02, stop_loss=-7.))
        self.assertEqual(update.status_code, 200)
        stored = json.loads(server.STRATEGY_CONFIG_PATH.read_text())
        for key, expected in dict(risk_per_trade=.005, max_sector_weight=.25, cooldown_days=4,
                                  participation_rate=.02, stop_loss_pct=-7.).items():
            self.assertEqual(stored["parameters"][key], expected)
        before = server.STRATEGY_CONFIG_PATH.read_text()
        rejected = self.client.post("/api/strategy/config", json={"stop_loss": 2.})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(server.STRATEGY_CONFIG_PATH.read_text(), before)
        self.assertEqual(self.ctx.strategy.stop_loss_pct, -7.)

    def test_entry_guards_persist_strictly_without_changing_other_parameters(self):
        original = server.asdict(self.ctx.strategy.config)
        safeguards = dict(max_entry_gap_down_pct=2.5, max_entry_gap_up_pct=4,
                          max_entry_intraday_drop_pct=0, entry_require_ma20=False)
        update = self.client.post("/api/strategy/config", json=safeguards)
        self.assertEqual(update.status_code, 200, update.text)
        stored = json.loads(server.STRATEGY_CONFIG_PATH.read_text())["parameters"]
        for key, value in original.items():
            self.assertEqual(stored[key], safeguards.get(key, value))
        listed = self.client.get("/api/strategy/list").json()
        self.assertEqual(listed["current_parameters"], stored)
        before = server.STRATEGY_CONFIG_PATH.read_text()
        for invalid in ({"max_entry_gap_down_pct": True}, {"max_entry_gap_up_pct": "5"},
                        {"max_entry_intraday_drop_pct": -1}, {"max_entry_gap_down_pct": 21},
                        {"entry_require_ma20": "false"}, {"entry_require_ma20": 1}):
            with self.subTest(invalid=invalid):
                rejected = self.client.post("/api/strategy/config", json=invalid)
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertEqual(server.STRATEGY_CONFIG_PATH.read_text(), before)

    def test_backtest_accepts_explicit_entry_guards_and_current_snapshot_ignores_overrides(self):
        with patch.object(server, "run_backtest", return_value={"summary": {}}) as run:
            guards = dict(max_entry_gap_down_pct=2, max_entry_gap_up_pct=4,
                          max_entry_intraday_drop_pct=0, entry_require_ma20=False)
            result = self.client.post("/api/backtest/run", json=guards)
            self.assertEqual(result.status_code, 200, result.text)
            for key, value in guards.items():
                self.assertEqual(run.call_args.kwargs["config"][key], value)
            result = self.client.post("/api/backtest/run", json=dict(use_current_config=True, **guards))
            self.assertEqual(result.status_code, 200, result.text)
            for key in guards:
                self.assertEqual(run.call_args.kwargs["config"][key], getattr(self.ctx.strategy.config, key))

    def test_editing_active_strategy_guard_preserves_unmentioned_risk_parameters(self):
        self.ctx.strategy.config = server.MomentumConfig(max_positions=8, max_stock_weight=.17,
            risk_per_trade=.005, max_sector_weight=.25, volatility_target=.19, cooldown_days=7)
        before = server.asdict(self.ctx.strategy.config)
        response = self.client.post("/api/strategy/switch", json=dict(
            strategy_id=self.ctx.strategy.name, max_entry_gap_down_pct=2))
        self.assertEqual(response.status_code, 200, response.text)
        after = server.asdict(self.ctx.strategy.config)
        self.assertEqual(after, {**before, "max_entry_gap_down_pct": 2})

    def test_snapshot_restore_rebinds_strategy_state_and_preserves_risk_capacity(self):
        self.ctx.strategy.state["cooldown"] = {"sh600519": "2026-09-18"}
        self.ctx.account.risk_history = dict(peak_equity=600000., max_drawdown=100000., max_drawdown_pct=20.)
        self.ctx.account._quote_consumption = {("2026-09-22", "snapshot", "BUY"): 100}
        self.ctx.account._daily_consumption = {("2026-09-22", "sh600519", "BUY"): 100}
        self.ctx.account.save()
        created = self.client.post("/api/snapshot/create", json={"tag": "manual"}).json()
        self.assertTrue(created["success"], created)
        sid = created["snapshot"]["meta"]["snapshot_id"]
        self.ctx.account.strategy_state, self.ctx.account.risk_history = {}, {}
        self.ctx.account._quote_consumption, self.ctx.account._daily_consumption = {}, {}
        self.ctx.account.save()
        restored = self.client.post("/api/snapshot/restore", json=dict(snapshot_id=sid, backup_current=True)).json()
        self.assertTrue(restored["success"], restored)
        self.assertIs(self.ctx.strategy.state, self.ctx.account.strategy_state[self.ctx.strategy.name])
        self.assertEqual(self.ctx.strategy.state["cooldown"], {"sh600519": "2026-09-18"})
        self.assertEqual(self.ctx.account.risk_history["max_drawdown"], 100000.)
        self.assertEqual(self.ctx.account._quote_consumption[("2026-09-22", "snapshot", "BUY")], 100)
        self.assertEqual(self.ctx.account._daily_consumption[("2026-09-22", "sh600519", "BUY")], 100)

    def test_snapshot_restore_uses_saved_strategy_parameters_and_enabled_state(self):
        for strategy_key in ("active_strategy", "current_strategy"):
            with self.subTest(strategy_key=strategy_key):
                switched = self.client.post("/api/strategy/switch", json=dict(
                    strategy_id="Momentum_Rotation", max_positions=5, risk_per_trade=.005,
                    max_sector_weight=.25, stop_loss=-8.))
                self.assertTrue(switched.json()["success"], switched.json())
                self.assertEqual(self.client.post("/api/strategy/toggle", json={"active": False}).status_code, 200)
                self.ctx.strategy.state["cooldown"] = {"sh600519": "2026-09-18"}
                self.ctx.account.save()
                saved = json.loads(server.STRATEGY_CONFIG_PATH.read_text())
                if strategy_key == "current_strategy":
                    saved["current_strategy"] = saved.pop("active_strategy")
                    server.STRATEGY_CONFIG_PATH.write_text(json.dumps(saved))
                created = self.client.post("/api/snapshot/create", json={"tag": strategy_key}).json()
                self.assertTrue(created["success"], created)
                sid = created["snapshot"]["meta"]["snapshot_id"]
                self.assertEqual(created["snapshot"]["strategy_state"], saved)

                # The running strategy deliberately differs from the saved
                # strategy, parameters and enabled flag at restoration time.
                changed = self.client.post("/api/strategy/switch", json=dict(
                    strategy_id="Momentum_Rotation_Pro", max_positions=2, risk_per_trade=.01))
                self.assertTrue(changed.json()["success"])
                self.client.post("/api/strategy/toggle", json={"active": True})
                self.ctx.engine = SimpleNamespace(account=self.ctx.account, strategy=self.ctx.strategy)
                old_strategy = self.ctx.strategy
                restored = self.client.post("/api/snapshot/restore", json=dict(
                    snapshot_id=sid, backup_current=True)).json()
                self.assertTrue(restored["success"], restored)

                status = self.client.get("/api/status").json()
                self.assertEqual(status["strategy_id"], "Momentum_Rotation")
                self.assertFalse(status["strategy_active"])
                self.assertEqual(status["strategy_parameters"], saved["parameters"])
                self.assertEqual(status["strategy_parameters"]["max_positions"], 5)
                self.assertEqual(status["strategy_parameters"]["risk_per_trade"], .005)
                self.assertEqual(json.loads(server.STRATEGY_CONFIG_PATH.read_text()), saved)
                self.assertIsInstance(self.ctx.strategy, MomentumRotationStrategy)
                self.assertIsNot(self.ctx.strategy, old_strategy)
                self.assertIs(self.ctx.strategy.state, self.ctx.account.strategy_state["Momentum_Rotation"])
                self.assertEqual(self.ctx.strategy.state["cooldown"], {"sh600519": "2026-09-18"})
                self.assertIs(self.ctx.engine.strategy, self.ctx.strategy)
                self.assertIs(self.ctx.engine.account, self.ctx.account)


    def test_restore_and_upload_hold_lock_before_manager_writes_and_allow_reentrant_callback(self):
        called = []

        def guarded_restore(snapshot_input, backup_current=True):
            # Check before the first write; checking only the callback cannot
            # detect a background save overwriting the freshly restored file.
            self.assertTrue(self.ctx.lock._is_owned())
            called.append(snapshot_input)
            server.on_snapshot_restored({"strategy_state": {
                "active_strategy": "Momentum_Rotation_Pro", "parameters": {},
                "strategy_active": False}})
            self.assertTrue(self.ctx.lock._is_owned())
            return {"success": True, "message": "guarded fake restore"}

        with patch.object(snapshot_manager.SnapshotManager, "restore_snapshot", side_effect=guarded_restore):
            restored = self.client.post("/api/snapshot/restore", json={"snapshot_id": "guarded"})
            self.assertTrue(restored.json()["success"], restored.json())
            uploaded = self.client.post("/api/snapshot/upload", content=b'{"account_state": {"summary": {}}}',
                                        headers={"content-type": "application/json", "x-filename": "guarded.json"})
            self.assertTrue(uploaded.json()["success"], uploaded.json())
        self.assertEqual(len(called), 2)
        self.assertFalse(self.ctx.strategy_active)

    def test_worker_checks_quote_freshness_after_provider_io(self):
        current, observed, prepared_at = [NOW], [], []
        symbol = "sz002594"

        class RequestClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

        proxy = {key: getattr(dt, key) for key in dir(dt) if not key.startswith("__")}
        proxy["datetime"] = RequestClock
        self.ctx.running, self.ctx.strategy_active = True, True
        self.ctx.strategy = SimpleNamespace(
            latest_candidates=[{"symbol": symbol}],
            prepare=lambda now: prepared_at.append(now),
            on_tick=lambda quotes: observed.append(copy.deepcopy(quotes)))

        def fetch_after_one_second(symbols):
            quote = copy.deepcopy(self.quotes[symbol])
            quote["time"] = (NOW+dt.timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
            current[0] = NOW+dt.timedelta(seconds=2)
            self.ctx.running = False  # The one in-flight iteration must still finish.
            return {symbol: quote}

        with patch.object(server, "datetime", SimpleNamespace(**proxy)), \
             patch.object(MarketFeed, "fetch_quotes", side_effect=fetch_after_one_second), \
             patch.object(server.time, "sleep", return_value=None):
            server.background_worker()
        self.assertEqual(prepared_at, [NOW])
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][symbol]["fresh"])
        self.assertEqual(observed[0][symbol]["time"], "20260922100001")


    def test_position_risk_and_quote_time_are_read_only(self):
        self.assertTrue(self.order().json()["success"])
        before = copy.deepcopy((self.ctx.account.positions, self.ctx.account.strategy_state, self.ctx.account.trades))
        disk = Path(self.ctx.account.save_path).read_bytes()
        row = self.client.get("/api/positions").json()[0]
        self.assertEqual(row["risk"]["stop_loss_mode"], "intraday")
        self.assertAlmostEqual(row["risk"]["stop_price"], row["cost_basis_cents"]/100/row["total_shares"]*.94)
        self.assertEqual(row["quote_status"], "fresh")
        self.assertEqual(row["quote_time"], NOW.isoformat())
        self.assertEqual(before, (self.ctx.account.positions, self.ctx.account.strategy_state, self.ctx.account.trades))
        self.assertEqual(disk, Path(self.ctx.account.save_path).read_bytes())
        self.ctx.last_quotes["sz002594"].update(time="20260921150000", fresh=False)
        stale = self.client.get("/api/positions").json()[0]
        self.assertEqual(stale["quote_status"], "stale")
        self.assertEqual(stale["risk"]["status"], "awaiting_quote")

    def test_paused_strategy_position_does_not_imply_automatic_execution(self):
        self.assertTrue(self.order().json()["success"])
        self.ctx.strategy_active = False
        row = self.client.get("/api/positions").json()[0]
        self.assertEqual(row["risk"]["status"], "paused")
        self.assertIn("不会自动下单", row["risk"]["blocked_reason"])
        self.ctx.strategy_active = True
        self.ctx.account._persistence_error = "simulated disk full"
        row = self.client.get("/api/positions").json()[0]
        self.assertEqual(row["risk"]["status"], "unavailable")
        self.assertIn("持久化异常", row["risk"]["blocked_reason"])

    def test_stop_loss_mode_validation_and_roundtrip(self):
        bad = self.client.post("/api/strategy/config", json={"stop_loss_mode": "unknown"})
        self.assertEqual(bad.status_code, 422)
        response = self.client.post("/api/strategy/config", json={"stop_loss_mode": "daily_close"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ctx.strategy.config.stop_loss_mode, "daily_close")
        self.assertIn("收盘确认止损", self.client.get("/api/status").json()["signal_schedule"])


if __name__ == "__main__":
    unittest.main()
