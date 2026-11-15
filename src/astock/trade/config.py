# src/astock/trade/config.py
# -*- coding: utf-8 -*-
"""
A-Stock Environment and Path Configuration
Explicitly isolates test databases and audit logs from production simulation accounts.
"""

import os
from pathlib import Path


def get_env() -> str:
    return os.getenv("ASTOCK_ENV", "production").lower()


def is_production() -> bool:
    return get_env() == "production"


def is_test() -> bool:
    return get_env() == "test"


def get_db_path() -> Path:
    env_db = os.getenv("ASTOCK_DB_PATH")
    if env_db:
        return Path(env_db)
    if is_test():
        return Path("data/test_trading_v3.sqlite")
    return Path("data/trading_v3.sqlite")


def get_audit_path() -> Path:
    env_audit = os.getenv("ASTOCK_AUDIT_PATH")
    if env_audit:
        return Path(env_audit)
    if is_test():
        return Path("data/audit/test_daily_acceptance_records.json")
    return Path("data/audit/daily_acceptance_records.json")
