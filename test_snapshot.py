#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试：系统快照与整机离线迁移管理器 (test_snapshot.py)
"""

import json
import shutil
import tempfile
import tarfile
from pathlib import Path
import pytest

from snapshot_manager import SnapshotManager, BASE_DIR, SNAPSHOTS_DIR, SIM_ACCOUNT_PATH, STRATEGY_CONFIG_PATH, NOTIFICATION_CONFIG_PATH


@pytest.fixture
def temp_snapshot_env(monkeypatch, tmp_path):
    """设置临时隔离的快照与账户环境"""
    test_snapshots_dir = tmp_path / "snapshots"
    test_account_file = tmp_path / "sim_account.json"
    test_strategy_file = tmp_path / "strategy_config.json"
    test_notif_file = tmp_path / "notification_config.json"
    test_index_file = test_snapshots_dir / "index.json"

    monkeypatch.setattr("snapshot_manager.SNAPSHOTS_DIR", test_snapshots_dir)
    monkeypatch.setattr("snapshot_manager.SIM_ACCOUNT_PATH", test_account_file)
    monkeypatch.setattr("snapshot_manager.STRATEGY_CONFIG_PATH", test_strategy_file)
    monkeypatch.setattr("snapshot_manager.NOTIFICATION_CONFIG_PATH", test_notif_file)
    monkeypatch.setattr("snapshot_manager.INDEX_FILE", test_index_file)

    # 初始化测试数据
    account_data = {
        "updated_at": "2026-09-23T09:30:00+08:00",
        "summary": {
            "initial_cash": 100000.0,
            "cash": 80000.0,
            "market_value": 22000.0,
            "total_equity": 102000.0,
            "total_pnl": 2000.0,
            "total_pnl_pct": 2.0,
            "today_pnl": 1500.0,
            "today_pnl_pct": 1.5,
            "win_rate": 66.7,
            "profit_loss_ratio": 2.5
        },
        "positions": {
            "sh600000": {
                "name": "浦发银行",
                "total_shares": 2000,
                "available_shares": 2000,
                "cost_price": 10.0,
                "last_price": 11.0,
                "market_value": 22000.0,
                "unrealized_pnl": 2000.0,
                "pnl_pct": 10.0
            }
        },
        "trades": [
            {
                "time": "2026-09-23 09:35:00",
                "symbol": "sh600000",
                "name": "浦发银行",
                "side": "BUY",
                "price": 10.0,
                "shares": 2000,
                "fees": 10.0,
                "reason": "突破均线买入"
            },
            {
                "time": "2026-09-20 14:00:00",
                "symbol": "sh600519",
                "name": "贵州茅台",
                "side": "SELL",
                "price": 1800.0,
                "shares": 100,
                "fees": 18.0,
                "reason": "止盈"
            }
        ],
        "orders": []
    }
    with open(test_account_file, "w", encoding="utf-8") as f:
        json.dump(account_data, f)

    strat_data = {
        "current_strategy": "Momentum_Rotation",
        "max_weight": 0.33,
        "max_positions": 3,
        "stop_loss": -6.0
    }
    with open(test_strategy_file, "w", encoding="utf-8") as f:
        json.dump(strat_data, f)

    notif_data = {
        "enabled": True,
        "provider": "wxpusher",
        "wxpusher_app_token": "AT_test123",
        "wxpusher_uids": "UID_test456"
    }
    with open(test_notif_file, "w", encoding="utf-8") as f:
        json.dump(notif_data, f)

    # 重新初始化单例
    SnapshotManager._instance = None
    mgr = SnapshotManager()
    mgr._init_paths()

    return {
        "mgr": mgr,
        "tmp_path": tmp_path,
        "account_file": test_account_file,
        "strategy_file": test_strategy_file,
        "notif_file": test_notif_file,
        "snapshots_dir": test_snapshots_dir
    }


def test_create_and_list_snapshot(temp_snapshot_env):
    mgr: SnapshotManager = temp_snapshot_env["mgr"]
    snap = mgr.create_snapshot(tag="manual", description="测试即时快照")

    assert snap is not None
    assert snap["meta"]["tag"] == "manual"
    assert snap["summary"]["total_equity"] == 102000.0
    assert snap["summary"]["position_count"] == 1
    assert snap["strategy_state"]["current_strategy"] == "Momentum_Rotation"
    assert snap["notification_state"]["wxpusher_app_token"] == "AT_test123"

    # 验证当日成交提取 (仅提取 2026-09-23 的 1 笔)
    assert len(snap["daily_detail"]["today_trades"]) == 1
    assert snap["daily_detail"]["today_trades"][0]["symbol"] == "sh600000"

    # 列表检验
    snaps = mgr.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0]["snapshot_id"] == snap["meta"]["snapshot_id"]
    assert snaps[0]["total_equity"] == 102000.0


def test_update_daily_snapshot(temp_snapshot_env):
    mgr: SnapshotManager = temp_snapshot_env["mgr"]
    snap = mgr.update_daily_snapshot(date_str="2026-09-23")

    assert snap["meta"]["snapshot_id"] == "daily_2026-09-23"
    assert snap["meta"]["tag"] == "daily"
    assert "2026-09-23" in snap["meta"]["description"]

    # 再次更新同一天快照（增量更新不重复新增条目）
    mgr.update_daily_snapshot(date_str="2026-09-23")
    snaps = mgr.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0]["snapshot_id"] == "daily_2026-09-23"


def test_export_and_restore_archive(temp_snapshot_env):
    mgr: SnapshotManager = temp_snapshot_env["mgr"]
    snap = mgr.create_snapshot(tag="manual", description="打包测试快照")
    sid = snap["meta"]["snapshot_id"]

    # 1. 导出为 .tar.gz
    archive_path = mgr.export_archive(sid)
    assert archive_path.exists()
    assert archive_path.stat().st_size > 0

    # 验证 tar 包内容
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "snapshot.json" in names
        assert "sim_account.json" in names
        assert "strategy_config.json" in names
        assert "notification_config.json" in names
        assert "README.txt" in names

    # 2. 模拟系统数据被清空/修改
    with open(temp_snapshot_env["account_file"], "w", encoding="utf-8") as f:
        json.dump({"summary": {"total_equity": 0.0}, "positions": {}, "trades": []}, f)

    # 3. 从 .tar.gz 恢复
    restore_called = []
    def on_restore_cb(data):
        restore_called.append(data)

    SnapshotManager.register_on_restore_callback(on_restore_cb)

    res = mgr.restore_snapshot(archive_path, backup_current=True)
    assert res["success"] is True
    assert res["total_equity"] == 102000.0
    assert res["position_count"] == 1
    assert len(restore_called) == 1

    # 验证账户文件已原子恢复
    with open(temp_snapshot_env["account_file"], "r", encoding="utf-8") as f:
        restored_account = json.load(f)
    assert restored_account["summary"]["total_equity"] == 102000.0
    assert "sh600000" in restored_account["positions"]

    # 验证产生了 pre_restore 安全备份
    snaps = mgr.list_snapshots()
    pre_restores = [s for s in snaps if s["tag"] == "pre_restore"]
    assert len(pre_restores) == 1


def test_delete_snapshot(temp_snapshot_env):
    mgr: SnapshotManager = temp_snapshot_env["mgr"]
    snap = mgr.create_snapshot(tag="manual", description="待删除快照")
    sid = snap["meta"]["snapshot_id"]

    assert mgr.get_snapshot(sid) is not None
    deleted = mgr.delete_snapshot(sid)
    assert deleted is True
    assert mgr.get_snapshot(sid) is None

    snaps = mgr.list_snapshots()
    assert not any(s["snapshot_id"] == sid for s in snaps)

