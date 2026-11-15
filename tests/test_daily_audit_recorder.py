# tests/test_daily_audit_recorder.py
# -*- coding: utf-8 -*-
import os
import tempfile
import pytest
from pathlib import Path
from astock.trade.daily_audit_recorder import DailyAuditRecorder
from astock.trade.runtime import TradingRuntime

def test_daily_audit_recorder_workflow():
    with tempfile.TemporaryDirectory() as tmp_dir:
        json_path = Path(tmp_dir) / "daily_acceptance_records.json"
        recorder = DailyAuditRecorder(log_path=json_path)

        # 初始状态
        status = recorder.get_status()
        assert status["recorded_trading_days"] == 0
        assert status["is_completed"] is False
        assert "IN_PROGRESS" in status["acceptance_status"]

        # 记录 3 天
        for i in range(1, 4):
            day_str = f"2026-11-{15+i:02d}"
            recorder.record_day({
                "trade_date": day_str,
                "market_sync_completed_at": "16:35:10",
                "market_regime": "STRONG_RISK_ON",
                "balanced_account": {"equity": 100000.0, "cash": 80000.0, "positions_count": 2},
                "aggressive_account": {"equity": 100000.0, "cash": 60000.0, "positions_count": 2},
                "strategy_status": "ACTIVE",
                "orders_generated": 2,
                "orders_executed": 2,
                "orders_rejected": [],
                "sqlite_reconciliation": "MATCH",
                "systemd_restarts": 0,
                "peak_memory_mb": 420.5,
                "error_summary": "None"
            })

        status_3 = recorder.get_status()
        assert status_3["recorded_trading_days"] == 3
        assert status_3["is_completed"] is False
        assert "3/5" in status_3["acceptance_status"]

        # 记录第 4 天与第 5 天
        for i in range(4, 6):
            day_str = f"2026-11-{15+i:02d}"
            recorder.record_day({
                "trade_date": day_str,
                "market_sync_completed_at": "16:35:10",
                "market_regime": "RISK_ON",
                "balanced_account": {"equity": 102000.0, "cash": 75000.0, "positions_count": 3},
                "aggressive_account": {"equity": 105000.0, "cash": 50000.0, "positions_count": 3},
                "strategy_status": "ACTIVE",
                "orders_generated": 1,
                "orders_executed": 1,
                "orders_rejected": [],
                "sqlite_reconciliation": "MATCH",
                "systemd_restarts": 0,
                "peak_memory_mb": 435.0,
                "error_summary": "None"
            })

        status_5 = recorder.get_status()
        assert status_5["recorded_trading_days"] == 5
        assert status_5["is_completed"] is True
        assert status_5["acceptance_status"] == "COMPLETED"


def test_capture_from_trading_runtime():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test.sqlite")
        json_path = Path(tmp_dir) / "daily_acceptance.json"
        
        runtime = TradingRuntime(db_path=db_path, mode="ACTIVE", initial_capital=100000.0)
        recorder = DailyAuditRecorder(log_path=json_path)

        res = recorder.capture_from_runtime(
            runtime=runtime,
            trade_date="2026-11-16",
            market_regime="STRONG_RISK_ON",
            sync_completed_time="16:32:00",
            orders_generated=3,
            orders_executed=3,
            orders_rejected=[]
        )

        assert res["recorded_trading_days"] == 1
        assert res["is_completed"] is False
        assert res["latest_record"]["sqlite_reconciliation"] == "MATCH (Discrepancy: 0.00)"
