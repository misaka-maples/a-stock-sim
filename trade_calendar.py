#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股开盘交易日与法定节假日交易日历管理器 (A-Share Trading Calendar)

核心规则：
1. 周末（周六、周日）全天休市。A股证券交易所永远不在周末开盘（即使国务院安排周末调休补班，股市亦不交易）。
2. 中国法定节假日（元旦、春节、清明、劳动节、端午、中秋、国庆）全天休市。
3. 2023~2026 年采用交易所公布的休市安排；更早未覆盖年份仅提供未核验的固定节日估算。
   尚未配置官方安排的未来年份停止交易，不把预测假期当作官方日历。
4. 支持从 data/holidays.json 加载用户自定义节假日或特殊工作日配置，周末不可强制开盘。
   导入核验后的完整年度安排：{"calendar_years": [2027], "holidays": ["2027-MM-DD", ...]}。
   calendar_years 表示该年度全部工作日休市日期已录入；未声明时只能逐日覆盖，不能推定整年可交易。
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
# 来源保留在代码中，方便逐年核对；元旦跨年区间只列当前年度部分。
OFFICIAL_HOLIDAY_SOURCES = {
    2023: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml",
    2024: "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml",
    2025: "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20241223_10767110.shtml",
    2026: "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
}
OFFICIAL_HOLIDAY_RANGES: Tuple[Tuple[str, str, str], ...] = (
    # 2023 年
    ("2023-01-01", "2023-01-02", "元旦"),
    ("2023-01-21", "2023-01-27", "春节"),
    ("2023-04-05", "2023-04-05", "清明节"),
    ("2023-04-29", "2023-05-03", "劳动节"),
    ("2023-06-22", "2023-06-24", "端午节"),
    ("2023-09-29", "2023-10-06", "中秋节与国庆节"),
    # 2024 年
    ("2024-01-01", "2024-01-01", "元旦"),
    ("2024-02-09", "2024-02-17", "春节"),
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
)

# 历史未覆盖年份的固定节日估算，不能用于声称精确的历史回测。
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
_OFFICIAL_YEARS = frozenset(OFFICIAL_HOLIDAY_SOURCES)
_LATEST_OFFICIAL_YEAR = max(_OFFICIAL_YEARS)


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


def _load_custom_years() -> Set[int]:
    """用户明确声明已核验并录入完整休市表的年度，不从零散日期推断覆盖。"""
    try:
        with open(HOLIDAYS_CONFIG_FILE, "r", encoding="utf-8") as f:
            years = json.load(f).get("calendar_years", [])
        if not isinstance(years, list):
            return set()
        return {year for year in years if type(year) is int and 1 <= year <= 9999}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


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

    # 1. 周末永远休市；调休补班和自定义配置都不能改变此规则。
    if d.weekday() >= 5:
        day_name = "周六" if d.weekday() == 5 else "周日"
        return f"周末休市（{day_name}）"

    # 2. 自定义交易日只允许覆盖工作日。
    custom_holidays, custom_trading_days = _load_custom_config()
    if d_str in custom_trading_days:
        return None

    # 3. 自定义节假日覆盖
    if d_str in custom_holidays:
        return "自定义休市节假日"

    # 4. 官方已排定休市表
    if d_str in _OFFICIAL_HOLIDAY_MAP:
        return _OFFICIAL_HOLIDAY_MAP[d_str]

    # 5. 已核验年度的普通工作日不可再套用通用假期范围。
    if d.year in _OFFICIAL_YEARS:
        return None

    if d.year in _load_custom_years():
        return None

    # 6. 未公布/未录入的未来年度不猜测开市日期，避免错误放行订单。
    if d.year > _LATEST_OFFICIAL_YEAR:
        return f"年度交易日历未配置（{d.year}）"

    # 7. 更早未覆盖年份仅提供明确标注的通用估算。
    md = d.strftime("%m-%d")
    for start_md, end_md, name in GENERIC_HOLIDAY_RANGES:
        if start_md <= md <= end_md:
            return f"估算休市（{name}，年度日历未核验）"

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
    else:
        now = now.astimezone(BEIJING)

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
    """获取下一个开盘日；未配置日历等原因导致一年内找不到时抛出 ValueError。"""
    return _adjacent_trading_day(dt, 1)


def get_previous_trading_day(dt: Optional[Union[datetime.date, datetime.datetime, str]] = None) -> datetime.date:
    """获取上一个开盘日；未配置日历等原因导致一年内找不到时抛出 ValueError。"""
    return _adjacent_trading_day(dt, -1)


def _adjacent_trading_day(dt, direction: int) -> datetime.date:
    curr = to_date(dt)
    step = datetime.timedelta(days=direction)
    for _ in range(366):
        curr += step
        if is_trading_day(curr):
            return curr
    raise ValueError(f"一年内未找到已配置交易日，请更新年度交易日历：{to_date(dt).isoformat()}")
