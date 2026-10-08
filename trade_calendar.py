#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股开盘交易日与法定节假日交易日历管理器 (A-Share Trading Calendar)

核心规则：
1. 周末（周六、周日）全天休市。A股证券交易所永远不在周末开盘（即使国务院安排周末调休补班，股市亦不交易）。
2. 中国法定节假日（元旦、春节、清明、劳动节、端午、中秋、国庆）全天休市。
3. 支持 2024~2030 年精确官方节假日放假安排及未覆盖年份自动通用兜底。
4. 支持从 data/holidays.json 加载用户自定义节假日或特殊交易日配置。
5. 交易时段判断：9:30-11:30 早盘交易，13:00-14:57 午盘交易，14:57-15:00 尾盘竞价。
"""

import json
import datetime
from pathlib import Path
from typing import Optional, Union, Dict, Set, Tuple

BEIJING = datetime.timezone(datetime.timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
HOLIDAYS_CONFIG_FILE = BASE_DIR / "data" / "holidays.json"

# 精确官方休市节假日安排 (起始日, 结束日, 节日名称)
OFFICIAL_HOLIDAY_RANGES: Tuple[Tuple[str, str, str], ...] = (
    # 2024 年
    ("2024-01-01", "2024-01-01", "元旦"),
    ("2024-02-09", "2024-02-18", "春节"),
    ("2024-04-04", "2024-04-06", "清明节"),
    ("2024-05-01", "2024-05-05", "劳动节"),
    ("2024-06-10", "2024-06-10", "端午节"),
    ("2024-09-15", "2024-09-17", "中秋节"),
    ("2024-10-01", "2024-10-07", "国庆节"),
    # 2025 年
    ("2025-01-01", "2025-01-01", "元旦"),
    ("2025-01-28", "2025-02-04", "春节"),
    ("2025-04-04", "2025-04-06", "清明节"),
    ("2025-05-01", "2025-05-05", "劳动节"),
    ("2025-05-31", "2025-06-02", "端午节"),
    ("2025-10-01", "2025-10-08", "国庆节与中秋节"),
    # 2026 年 (当前系统年度)
    ("2026-01-01", "2026-01-03", "元旦"),
    ("2026-02-15", "2026-02-23", "春节"),
    ("2026-04-04", "2026-04-06", "清明节"),
    ("2026-05-01", "2026-05-05", "劳动节"),
    ("2026-06-19", "2026-06-21", "端午节"),
    ("2026-09-25", "2026-09-27", "中秋节"),
    ("2026-10-01", "2026-10-07", "国庆节"),
    # 2027 年
    ("2027-01-01", "2027-01-03", "元旦"),
    ("2027-02-05", "2027-02-13", "春节"),
    ("2027-04-03", "2027-04-05", "清明节"),
    ("2027-05-01", "2027-05-05", "劳动节"),
    ("2027-06-09", "2027-06-11", "端午节"),
    ("2027-09-15", "2027-09-17", "中秋节"),
    ("2027-10-01", "2027-10-07", "国庆节"),
    # 2028 年
    ("2028-01-01", "2028-01-03", "元旦"),
    ("2028-01-25", "2028-02-02", "春节"),
    ("2028-04-03", "2028-04-05", "清明节"),
    ("2028-05-01", "2028-05-05", "劳动节"),
    ("2028-05-27", "2028-05-29", "端午节"),
    ("2028-10-01", "2028-10-07", "国庆节与中秋节"),
    # 2029 年
    ("2029-01-01", "2029-01-03", "元旦"),
    ("2029-02-12", "2029-02-20", "春节"),
    ("2029-04-04", "2029-04-06", "清明节"),
    ("2029-05-01", "2029-05-05", "劳动节"),
    ("2029-06-15", "2029-06-17", "端午节"),
    ("2029-09-21", "2029-09-23", "中秋节"),
    ("2029-10-01", "2029-10-07", "国庆节"),
    # 2030 年
    ("2030-01-01", "2030-01-03", "元旦"),
    ("2030-02-01", "2030-02-09", "春节"),
    ("2030-04-04", "2030-04-06", "清明节"),
    ("2030-05-01", "2030-05-05", "劳动节"),
    ("2030-06-04", "2030-06-06", "端午节"),
    ("2030-09-11", "2030-09-13", "中秋节"),
    ("2030-10-01", "2030-10-07", "国庆节"),
)

# 通用固定节日兜底 (针对未显式配置的未来年份)
GENERIC_HOLIDAY_RANGES: Tuple[Tuple[str, str, str], ...] = (
    ("01-01", "01-03", "元旦"),
    ("05-01", "05-05", "劳动节"),
    ("10-01", "10-07", "国庆节"),
)


def _build_holiday_dict() -> Dict[str, str]:
    """展开所有节假日区间为具体日期字典 {YYYY-MM-DD: 节日名称}"""
    h_dict: Dict[str, str] = {}
    for start_s, end_s, name in OFFICIAL_HOLIDAY_RANGES:
        start_d = datetime.date.fromisoformat(start_s)
        end_d = datetime.date.fromisoformat(end_s)
        curr = start_d
        while curr <= end_d:
            h_dict[curr.isoformat()] = f"{name}假期"
            curr += datetime.timedelta(days=1)
    return h_dict


_OFFICIAL_HOLIDAY_MAP: Dict[str, str] = _build_holiday_dict()


def _load_custom_config() -> Tuple[Set[str], Set[str]]:
    """加载用户自定义额外节假日与特殊交易日"""
    custom_holidays: Set[str] = set()
    custom_trading_days: Set[str] = set()
    if HOLIDAYS_CONFIG_FILE.exists():
        try:
            with open(HOLIDAYS_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                custom_holidays = set(data.get("holidays", []))
                custom_trading_days = set(data.get("trading_days", []))
        except Exception:
            pass
    return custom_holidays, custom_trading_days


def to_date(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> datetime.date:
    """将多种时间输入统一转换为 datetime.date (默认返回当前北京时间日期)"""
    if dt is None:
        return datetime.datetime.now(BEIJING).date()
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            return dt.date()
        return dt.astimezone(BEIJING).date()
    if isinstance(dt, datetime.date):
        return dt
    if isinstance(dt, str):
        # 兼容 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS
        return datetime.date.fromisoformat(dt.strip().split(" ")[0])
    raise TypeError(f"无法将类型 {type(dt)} 转换为 datetime.date")


def get_holiday_reason(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> Optional[str]:
    """获取指定日期的休市原因，若为正常开盘日则返回 None"""
    d = to_date(dt)
    d_str = d.isoformat()

    # 1. 自定义交易日补丁（优先强制开盘）
    custom_holidays, custom_trading_days = _load_custom_config()
    if d_str in custom_trading_days:
        return None

    # 2. 周末（周六、周日）永远休市
    if d.weekday() >= 5:
        day_name = "周六" if d.weekday() == 5 else "周日"
        return f"周末休市（{day_name}）"

    # 3. 自定义节假日覆盖
    if d_str in custom_holidays:
        return "自定义休市节假日"

    # 4. 官方已排定休市表
    if d_str in _OFFICIAL_HOLIDAY_MAP:
        return _OFFICIAL_HOLIDAY_MAP[d_str]

    # 5. 未覆盖年份通用固定节日兜底
    md = d.strftime("%m-%d")
    for start_md, end_md, name in GENERIC_HOLIDAY_RANGES:
        if start_md <= md <= end_md:
            return f"{name}假期"

    return None


def is_trading_day(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> bool:
    """
    判断指定日期是否为A股开盘交易日：
    - 周末（周六、周日）返回 False
    - 法定节假日（十一国庆、春节等）返回 False
    - 正常开市工作日返回 True
    """
    return get_holiday_reason(dt) is None


def get_market_status(now: Optional[datetime.datetime] = None) -> str:
    """
    获取当前时点的市场状态，精确识别休市、集合竞价、连续交易等状态
    """
    if now is None:
        now = datetime.datetime.now(BEIJING)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)

    # 检查是否为开盘日
    holiday_reason = get_holiday_reason(now)
    if holiday_reason is not None:
        if now.weekday() >= 5:
            return "休市中（周末）"
        return f"休市中（{holiday_reason}）"

    # 当日为交易日，判断具体交易时段
    t = now.time()
    if t < datetime.time(9, 15):
        return "未开盘（盘前）"
    elif datetime.time(9, 15) <= t < datetime.time(9, 25):
        return "开盘集合竞价"
    elif datetime.time(9, 25) <= t < datetime.time(9, 30):
        return "盘前准备期"
    elif datetime.time(9, 30) <= t <= datetime.time(11, 30):
        return "早盘交易中"
    elif datetime.time(11, 30) < t < datetime.time(13, 0):
        return "午间休市"
    elif datetime.time(13, 0) <= t < datetime.time(14, 57):
        return "午盘交易中"
    elif datetime.time(14, 57) <= t <= datetime.time(15, 0):
        return "尾盘集合竞价"
    else:
        return "已收盘"


def is_trading_time(now: Optional[datetime.datetime] = None) -> bool:
    """
    判断当前是否处于连续交易时段（9:30-11:30 或 13:00-14:57）
    在非交易日或周末直接返回 False
    """
    status = get_market_status(now)
    return "交易中" in status


def get_next_trading_day(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> datetime.date:
    """获取下一个开盘交易日"""
    curr = to_date(dt) + datetime.timedelta(days=1)
    while not is_trading_day(curr):
        curr += datetime.timedelta(days=1)
    return curr


def get_previous_trading_day(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> datetime.date:
    """获取上一个开盘交易日"""
    curr = to_date(dt) - datetime.timedelta(days=1)
    while not is_trading_day(curr):
        curr -= datetime.timedelta(days=1)
    return curr
