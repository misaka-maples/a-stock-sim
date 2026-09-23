#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股量化模拟交易系统 - 系统快照与离线整机迁移管理器 (SnapshotManager)
功能特性：
1. 全状态快照生成：包含资产权益、持仓、历史成交流水、当日交易详情、策略配置与微信推送配置
2. 每日自动增量/全量记录：每日收盘定格归档为 daily_YYYY-MM-DD 快照，盘中实时记录今日交易流水
3. 离线整机迁移与恢复：一键导出为 .tar.gz 独立离线包，在新机上一键解包恢复并热重载
4. 安全保障：恢复前自动生成 pre_restore 安全快照，杜绝数据覆盖丢失
5. CLI 命令行支持：snapshot_manager.py create / list / export / restore
"""

import os
import sys
import json
import shutil
import tarfile
import logging
import platform
import datetime
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Callable

logger = logging.getLogger("snapshot_manager")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOTS_DIR = BASE_DIR / "data" / "snapshots"
SIM_ACCOUNT_PATH = BASE_DIR / "sim_account.json"
STRATEGY_CONFIG_PATH = BASE_DIR / "data" / "strategy_config.json"
NOTIFICATION_CONFIG_PATH = BASE_DIR / "data" / "notification_config.json"
INDEX_FILE = SNAPSHOTS_DIR / "index.json"

BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def get_now() -> datetime.datetime:
    return datetime.datetime.now(BEIJING)


class SnapshotManager:
    """系统快照与整机离线迁移管理单例"""

    _instance: Optional["SnapshotManager"] = None
    _restore_callbacks: List[Callable[[Dict[str, Any]], None]] = []

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_paths()
        return cls._instance

    def _init_paths(self):
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        if not INDEX_FILE.exists():
            self._save_index([])

    @classmethod
    def register_on_restore_callback(cls, callback: Callable[[Dict[str, Any]], None]):
        """注册快照恢复后的内存热重载回调"""
        if callback not in cls._restore_callbacks:
            cls._restore_callbacks.append(callback)

    def _load_index(self) -> List[Dict[str, Any]]:
        try:
            if INDEX_FILE.exists():
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"读取快照索引失败，重新初始化: {e}")
        return []

    def _save_index(self, index: List[Dict[str, Any]]):
        temp_file = INDEX_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        temp_file.replace(INDEX_FILE)

    def _read_json_safe(self, path: Path) -> Dict[str, Any]:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取文件 {path} 异常: {e}")
        return {}

    def _filter_today_trades(self, all_trades: List[Dict[str, Any]], date_str: str) -> List[Dict[str, Any]]:
        """从全部成交流水中提取指定日期的成交记录"""
        today_trades = []
        for t in all_trades:
            t_time = str(t.get("time", ""))
            # 兼容 "2026-09-23 09:30:00" 或 "2026-09-23T..."
            if t_time.startswith(date_str):
                today_trades.append(t)
        return today_trades

    def create_snapshot(
        self,
        tag: str = "manual",
        description: str = "",
        account_data: Optional[Dict[str, Any]] = None,
        snapshot_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        创建系统全量快照
        :param tag: 快照类型: 'manual' (手动) / 'daily' (每日收盘) / 'pre_restore' (恢复前备份)
        :param description: 自定义描述
        :param account_data: 可选内存中的账户数据，若无则从 sim_account.json 读取
        :param snapshot_id: 可选自定义快照ID
        :return: 生成的快照完整字典
        """
        now = get_now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%Y%m%d_%H%M%S")

        if not snapshot_id:
            if tag == "daily":
                snapshot_id = f"daily_{date_str}"
            else:
                snapshot_id = f"{tag}_{time_str}"

        # 1. 抓取账户全量数据
        if account_data is None:
            account_data = self._read_json_safe(SIM_ACCOUNT_PATH)

        summary = account_data.get("summary", {})
        positions = account_data.get("positions", {})
        trades = account_data.get("trades", [])
        orders = account_data.get("orders", [])

        # 提取当日成交明细
        today_trades = self._filter_today_trades(trades, date_str)

        # 2. 抓取策略配置
        strategy_state = self._read_json_safe(STRATEGY_CONFIG_PATH)

        # 3. 抓取微信推送配置 (敏感 Token 完整保存以支持离线整机迁移)
        notification_state = self._read_json_safe(NOTIFICATION_CONFIG_PATH)

        if not description:
            if tag == "daily":
                description = f"每日收盘自动归档快照 ({date_str})"
            elif tag == "pre_restore":
                description = f"系统恢复前的自动备份快照 ({time_str})"
            else:
                description = f"控制台手动创建快照 ({now.strftime('%Y-%m-%d %H:%M:%S')})"

        # 4. 构建快照自包含包
        snapshot: Dict[str, Any] = {
            "meta": {
                "snapshot_id": snapshot_id,
                "created_at": now.isoformat(),
                "tag": tag,
                "description": description,
                "version": "1.0",
                "system_info": {
                    "hostname": platform.node(),
                    "platform": platform.platform(),
                    "python": sys.version.split()[0]
                }
            },
            "summary": {
                "date": date_str,
                "total_equity": summary.get("total_equity", 0.0),
                "cash": summary.get("cash", 0.0),
                "market_value": summary.get("market_value", 0.0),
                "total_pnl": summary.get("total_pnl", 0.0),
                "total_pnl_pct": summary.get("total_pnl_pct", 0.0),
                "today_pnl": summary.get("today_pnl", 0.0),
                "today_pnl_pct": summary.get("today_pnl_pct", 0.0),
                "position_count": len(positions),
                "today_trade_count": len(today_trades),
                "total_trade_count": len(trades),
                "win_rate": summary.get("win_rate", 0.0),
                "profit_loss_ratio": summary.get("profit_loss_ratio", 0.0)
            },
            "account_state": {
                "updated_at": account_data.get("updated_at", now.isoformat()),
                "summary": summary,
                "positions": positions,
                "trades": trades,
                "orders": orders
            },
            "daily_detail": {
                "date": date_str,
                "today_trades": today_trades,
                "today_pnl": summary.get("today_pnl", 0.0),
                "today_pnl_pct": summary.get("today_pnl_pct", 0.0),
                "positions_snapshot": [
                    {
                        "symbol": sym,
                        "name": p.get("name", sym),
                        "shares": p.get("total_shares", 0),
                        "cost": p.get("cost_price", 0.0),
                        "last_price": p.get("last_price", 0.0),
                        "market_value": p.get("market_value", 0.0),
                        "pnl": p.get("unrealized_pnl", 0.0),
                        "pnl_pct": p.get("pnl_pct", 0.0)
                    }
                    for sym, p in positions.items()
                ]
            },
            "strategy_state": strategy_state,
            "notification_state": notification_state
        }

        # 5. 持久化存储到磁盘
        snapshot_file = SNAPSHOTS_DIR / f"{snapshot_id}.json"
        temp_file = snapshot_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        temp_file.replace(snapshot_file)

        # 6. 更新并排序快照索引
        item_meta = {
            "snapshot_id": snapshot_id,
            "filename": f"{snapshot_id}.json",
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": date_str,
            "tag": tag,
            "description": description,
            "total_equity": summary.get("total_equity", 0.0),
            "cash": summary.get("cash", 0.0),
            "market_value": summary.get("market_value", 0.0),
            "today_pnl": summary.get("today_pnl", 0.0),
            "total_pnl": summary.get("total_pnl", 0.0),
            "position_count": len(positions),
            "today_trades": len(today_trades),
            "total_trades": len(trades),
            "file_size": snapshot_file.stat().st_size
        }

        index = self._load_index()
        # 若已有同 ID 快照（如今日增量更新），先剔除旧记录
        index = [i for i in index if i.get("snapshot_id") != snapshot_id]
        index.insert(0, item_meta)
        self._save_index(index)

        logger.info(f"系统快照 [{snapshot_id}] 已生成 ({description})")
        return snapshot

    def update_daily_snapshot(
        self,
        date_str: Optional[str] = None,
        account_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        每日自动记录/增量更新当日快照
        :param date_str: 日期字符串 YYYY-MM-DD，缺省为今日
        :param account_data: 内存中的最新账户数据
        :return: 快照数据
        """
        if not date_str:
            date_str = get_now().strftime("%Y-%m-%d")

        snapshot_id = f"daily_{date_str}"
        desc = f"每日收盘自动快照 ({date_str})"
        return self.create_snapshot(
            tag="daily",
            description=desc,
            account_data=account_data,
            snapshot_id=snapshot_id
        )

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """获取所有快照列表（按时间倒序）"""
        index = self._load_index()
        # 校验证实文件存在性
        valid_index = []
        modified = False
        for item in index:
            sid = item.get("snapshot_id")
            fpath = SNAPSHOTS_DIR / f"{sid}.json"
            if fpath.exists():
                item["file_size"] = fpath.stat().st_size
                valid_index.append(item)
            else:
                modified = True

        if modified:
            self._save_index(valid_index)
        return valid_index

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """读取指定快照数据"""
        fpath = SNAPSHOTS_DIR / f"{snapshot_id}.json"
        if not fpath.exists():
            return None
        return self._read_json_safe(fpath)

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除指定快照及其归档文件"""
        json_file = SNAPSHOTS_DIR / f"{snapshot_id}.json"
        tar_file = SNAPSHOTS_DIR / f"{snapshot_id}.tar.gz"

        deleted = False
        if json_file.exists():
            json_file.unlink()
            deleted = True
        if tar_file.exists():
            tar_file.unlink()

        index = self._load_index()
        new_index = [i for i in index if i.get("snapshot_id") != snapshot_id]
        self._save_index(new_index)
        return deleted

    def export_archive(self, snapshot_id: str, output_path: Optional[Path] = None) -> Path:
        """
        将快照打包导出为标准的 .tar.gz 便携独立离线迁移压缩包
        包含：
        - snapshot.json: 完整快照结构体
        - sim_account.json: 账户直接覆盖文件
        - strategy_config.json: 策略配置
        - notification_config.json: 微信推送配置
        - README.txt: 离线迁移操作指南
        """
        snapshot = self.get_snapshot(snapshot_id)
        if not snapshot:
            raise FileNotFoundError(f"未找到快照: {snapshot_id}")

        if output_path is None:
            output_path = SNAPSHOTS_DIR / f"{snapshot_id}.tar.gz"

        # 创建临时打包目录
        temp_pack_dir = SNAPSHOTS_DIR / f"temp_pack_{snapshot_id}"
        temp_pack_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. snapshot.json
            with open(temp_pack_dir / "snapshot.json", "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)

            # 2. sim_account.json
            with open(temp_pack_dir / "sim_account.json", "w", encoding="utf-8") as f:
                json.dump(snapshot.get("account_state", {}), f, ensure_ascii=False, indent=2)

            # 3. strategy_config.json
            with open(temp_pack_dir / "strategy_config.json", "w", encoding="utf-8") as f:
                json.dump(snapshot.get("strategy_state", {}), f, ensure_ascii=False, indent=2)

            # 4. notification_config.json
            with open(temp_pack_dir / "notification_config.json", "w", encoding="utf-8") as f:
                json.dump(snapshot.get("notification_state", {}), f, ensure_ascii=False, indent=2)

            # 5. README.txt 指南
            readme_text = f"""================================================================================
A股量化模拟交易控制台 - 离线整机迁移快照包
================================================================================
快照标识: {snapshot_id}
生成时间: {snapshot.get('meta', {}).get('created_at')}
快照说明: {snapshot.get('meta', {}).get('description')}
总资产权益: ¥{snapshot.get('summary', {}).get('total_equity', 0.0):,.2f}
可用现金: ¥{snapshot.get('summary', {}).get('cash', 0.0):,.2f}
持仓数量: {snapshot.get('summary', {}).get('position_count', 0)} 只
历史交易: {snapshot.get('summary', {}).get('total_trade_count', 0)} 笔

【在新机上恢复运行的步骤】:
方法一 (推荐): 在新机启动 Web 控制台后，点击顶部【📷 系统快照】->【📤 上传恢复快照文件】，
              直接上传本 .tar.gz 压缩包即可完成秒级热恢复！

方法二 (命令行):
   1. 将本压缩包复制到新机器的项目根目录下。
   2. 执行恢复命令:
      python3 snapshot_manager.py restore {output_path.name}
   3. 启动实盘交易引擎:
      python3 server.py --daemon
================================================================================
"""
            with open(temp_pack_dir / "README.txt", "w", encoding="utf-8") as f:
                f.write(readme_text)

            # 6. 压制为 .tar.gz
            with tarfile.open(output_path, "w:gz") as tar:
                for item in temp_pack_dir.iterdir():
                    tar.add(item, arcname=item.name)

            logger.info(f"离线快照迁移包已生成: {output_path}")
            return output_path
        finally:
            if temp_pack_dir.exists():
                shutil.rmtree(temp_pack_dir, ignore_errors=True)

    def restore_snapshot(
        self,
        snapshot_input: Union[str, Dict[str, Any], Path],
        backup_current: bool = True
    ) -> Dict[str, Any]:
        """
        恢复系统状态 (支持快照ID、快照字典、.json 文件或 .tar.gz 压缩包)
        :param snapshot_input: 快照ID、快照字典或文件路径
        :param backup_current: 恢复前是否先备份当前运行状态
        :return: 恢复结果摘要
        """
        # 1. 解析快照字典数据
        snapshot_data: Optional[Dict[str, Any]] = None

        if isinstance(snapshot_input, dict):
            snapshot_data = snapshot_input
        elif isinstance(snapshot_input, (str, Path)):
            path_or_id = Path(snapshot_input)
            if path_or_id.exists():
                # 判断是 .tar.gz 还是 .json
                if path_or_id.name.endswith(".tar.gz") or path_or_id.name.endswith(".tgz"):
                    return self.import_archive_and_restore(path_or_id, backup_current=backup_current)
                else:
                    snapshot_data = self._read_json_safe(path_or_id)
            else:
                # 尝试当作已存在的快照 ID 读取
                snapshot_data = self.get_snapshot(str(snapshot_input))

        if not snapshot_data:
            raise ValueError(f"无法识别或加载指定的快照: {snapshot_input}")

        account_state = snapshot_data.get("account_state")
        if not account_state:
            raise ValueError("快照数据不合法，缺少 account_state 字段")

        # 2. 安全保障：恢复前自动备份当前运行状态
        if backup_current and SIM_ACCOUNT_PATH.exists():
            try:
                self.create_snapshot(
                    tag="pre_restore",
                    description=f"恢复快照 [{snapshot_data.get('meta', {}).get('snapshot_id', 'unknown')}] 前的自动安全备份"
                )
            except Exception as e:
                logger.warning(f"创建 pre_restore 安全备份失败: {e}")

        # 3. 恢复 sim_account.json
        temp_account = SIM_ACCOUNT_PATH.with_suffix(".tmp")
        with open(temp_account, "w", encoding="utf-8") as f:
            json.dump(account_state, f, ensure_ascii=False, indent=2)
        temp_account.replace(SIM_ACCOUNT_PATH)

        # 4. 恢复 strategy_config.json
        strategy_state = snapshot_data.get("strategy_state")
        if strategy_state:
            STRATEGY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_strat = STRATEGY_CONFIG_PATH.with_suffix(".tmp")
            with open(temp_strat, "w", encoding="utf-8") as f:
                json.dump(strategy_state, f, ensure_ascii=False, indent=2)
            temp_strat.replace(STRATEGY_CONFIG_PATH)

        # 5. 恢复 notification_config.json
        notification_state = snapshot_data.get("notification_state")
        if notification_state:
            NOTIFICATION_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_notif = NOTIFICATION_CONFIG_PATH.with_suffix(".tmp")
            with open(temp_notif, "w", encoding="utf-8") as f:
                json.dump(notification_state, f, ensure_ascii=False, indent=2)
            temp_notif.replace(NOTIFICATION_CONFIG_PATH)

        # 6. 如果当前快照不在快照库中，则存入库并更新索引
        sid = snapshot_data.get("meta", {}).get("snapshot_id")
        if sid and not (SNAPSHOTS_DIR / f"{sid}.json").exists():
            with open(SNAPSHOTS_DIR / f"{sid}.json", "w", encoding="utf-8") as f:
                json.dump(snapshot_data, f, ensure_ascii=False, indent=2)
            # 更新索引
            summary = account_state.get("summary", {})
            positions = account_data = account_state.get("positions", {})
            trades = account_state.get("trades", [])
            item_meta = {
                "snapshot_id": sid,
                "filename": f"{sid}.json",
                "created_at": snapshot_data.get("meta", {}).get("created_at", get_now().strftime("%Y-%m-%d %H:%M:%S")),
                "date": snapshot_data.get("summary", {}).get("date", get_now().strftime("%Y-%m-%d")),
                "tag": snapshot_data.get("meta", {}).get("tag", "imported"),
                "description": snapshot_data.get("meta", {}).get("description", "导入的离线快照"),
                "total_equity": summary.get("total_equity", 0.0),
                "cash": summary.get("cash", 0.0),
                "market_value": summary.get("market_value", 0.0),
                "today_pnl": summary.get("today_pnl", 0.0),
                "total_pnl": summary.get("total_pnl", 0.0),
                "position_count": len(positions),
                "today_trades": len(snapshot_data.get("daily_detail", {}).get("today_trades", [])),
                "total_trades": len(trades),
                "file_size": (SNAPSHOTS_DIR / f"{sid}.json").stat().st_size
            }
            index = self._load_index()
            index = [i for i in index if i.get("snapshot_id") != sid]
            index.insert(0, item_meta)
            self._save_index(index)

        # 7. 触发内存热重载回调 (让 server.py 正在运行的进程无感接管)
        for cb in self._restore_callbacks:
            try:
                cb(snapshot_data)
            except Exception as e:
                logger.error(f"执行快照热恢复回调异常: {e}")

        summary = account_state.get("summary", {})
        pos_cnt = len(account_state.get("positions", {}))
        tr_cnt = len(account_state.get("trades", []))
        msg = f"系统快照 [{sid}] 恢复成功！恢复总资产 ¥{summary.get('total_equity', 0.0):,.2f}，在持 {pos_cnt} 只股票，已载入 {tr_cnt} 笔历史交易。"
        logger.info(msg)

        return {
            "success": True,
            "message": msg,
            "snapshot_id": sid,
            "total_equity": summary.get("total_equity", 0.0),
            "position_count": pos_cnt,
            "trade_count": tr_cnt
        }

    def import_archive_and_restore(self, archive_path: Path, backup_current: bool = True) -> Dict[str, Any]:
        """从 .tar.gz 压缩包解包并恢复"""
        if not archive_path.exists():
            raise FileNotFoundError(f"归档包不存在: {archive_path}")

        temp_extract_dir = SNAPSHOTS_DIR / f"temp_extract_{archive_path.stem}"
        temp_extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(temp_extract_dir, filter="data")
                else:
                    tar.extractall(temp_extract_dir)

            snapshot_file = temp_extract_dir / "snapshot.json"
            if snapshot_file.exists():
                snapshot_data = self._read_json_safe(snapshot_file)
                return self.restore_snapshot(snapshot_data, backup_current=backup_current)
            else:
                # 兼容直接解压出来的单独文件
                account_file = temp_extract_dir / "sim_account.json"
                if account_file.exists():
                    account_state = self._read_json_safe(account_file)
                    strat_state = self._read_json_safe(temp_extract_dir / "strategy_config.json")
                    notif_state = self._read_json_safe(temp_extract_dir / "notification_config.json")
                    synthetic_snapshot = {
                        "meta": {
                            "snapshot_id": f"imported_{get_now().strftime('%Y%m%d_%H%M%S')}",
                            "created_at": get_now().isoformat(),
                            "tag": "imported",
                            "description": f"从归档文件 {archive_path.name} 导入恢复"
                        },
                        "account_state": account_state,
                        "strategy_state": strat_state,
                        "notification_state": notif_state,
                        "summary": account_state.get("summary", {})
                    }
                    return self.restore_snapshot(synthetic_snapshot, backup_current=backup_current)
                raise ValueError("压缩包内缺少有效的快照数据 (未找到 snapshot.json 或 sim_account.json)")
        finally:
            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)


# =============================================================================
# CLI 命令行交互入口
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="A股模拟交易系统快照与整机离线迁移工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # 1. create
    create_p = subparsers.add_parser("create", help="创建即时系统快照")
    create_p.add_argument("-t", "--tag", default="manual", help="快照类型 (manual, daily 等)")
    create_p.add_argument("-d", "--desc", default="", help="快照说明描述")

    # 2. list
    subparsers.add_parser("list", help="查看所有历史系统快照")

    # 3. export
    export_p = subparsers.add_parser("export", help="导出快照为 .tar.gz 独立迁移包")
    export_p.add_argument("snapshot_id", help="快照 ID (如 daily_2026-09-23)")
    export_p.add_argument("-o", "--output", help="输出压缩包路径 (默认在 data/snapshots/ 下)")

    # 4. restore
    restore_p = subparsers.add_parser("restore", help="从快照 ID 或 .tar.gz 压缩包恢复系统")
    restore_p.add_argument("target", help="快照 ID 或 .tar.gz / .json 文件路径")
    restore_p.add_argument("--no-backup", action="store_true", help="恢复前跳过安全备份")

    args = parser.parse_args()
    mgr = SnapshotManager()

    if args.command == "create":
        snap = mgr.create_snapshot(tag=args.tag, description=args.desc)
        print(f"✅ 快照创建成功: {snap['meta']['snapshot_id']}")
        print(f"   总资产: ¥{snap['summary']['total_equity']:,.2f} | 持仓: {snap['summary']['position_count']} 只 | 今日交易: {snap['summary']['today_trade_count']} 笔")

    elif args.command == "list":
        snaps = mgr.list_snapshots()
        if not snaps:
            print("📭 当前暂无快照记录。")
            return
        print(f"{'快照ID':<26} {'日期时间':<20} {'类型':<10} {'总权益(¥)':<14} {'持仓':<6} {'今日笔数':<8} {'说明'}")
        print("-" * 105)
        for s in snaps:
            print(f"{s['snapshot_id']:<26} {s['created_at']:<20} {s['tag']:<10} {s['total_equity']:<14,.2f} {s['position_count']:<6} {s['today_trades']:<8} {s.get('description','')}")

    elif args.command == "export":
        out = Path(args.output) if args.output else None
        res_path = mgr.export_archive(args.snapshot_id, output_path=out)
        print(f"📦 离线快照迁移包导出成功: {res_path}")

    elif args.command == "restore":
        res = mgr.restore_snapshot(args.target, backup_current=not args.no_backup)
        print(f"🔄 {res['message']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
