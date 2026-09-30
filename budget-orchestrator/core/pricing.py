# -*- coding: utf-8 -*-
"""计价内核。

价格表与峰谷规则**直接复用 token-dashboard skill**
（~/.workbuddy/skills/token-dashboard/scripts/gen_dashboard.py）的 PRICE / PEAK_WINDOWS /
HOL_RANGES / MAKEUP 口径，保证本工具与看板的金额可对齐、可交叉验证。

单位：元 / 百万 token。
每项结构：{"P": [输入, 输出, 缓存命中], "O": [输入, 输出, 缓存命中], "CW": 缓存写入倍率}
  P = 高峰价；O = 空闲价（= P × 50%）；CW = 缓存写入倍率（Claude 系 1.25，其余 1）
匹配规则：按【最长前缀】命中，更具体的键优先于更短的通用键。
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

# ── 模型名归一 ────────────────────────────────────────────────────────────────
MODEL_MERGE = {"glm-5.2-x": "glm-5.2"}
MODEL_ALIAS = {
    "deepseek-flash": "deepseek-v4.1-flash",
}
_RE_DEPLOY = re.compile(r"-expires-on-\d+$", re.I)


def norm_model(name: str) -> str:
    if not name:
        return name or "unknown"
    n = _RE_DEPLOY.sub("", name)
    n = MODEL_ALIAS.get(n, n)
    return MODEL_MERGE.get(n, n)


# ── 价格表（与 token-dashboard 保持一致；用户可自行修改）────────────────────────
PRICE = {
    # ── DeepSeek 官方牌价 ──
    "deepseek-v4.1-flash": {"P": [2, 8, 0.04], "O": [1, 4, 0.02], "CW": 1},
    "deepseek-flash": {"P": [2, 8, 0.04], "O": [1, 4, 0.02], "CW": 1},
    "deepseek-v4-pro": {"P": [9, 27, 0.30], "O": [4.5, 13.5, 0.15], "CW": 1},
    "deepseek-v4-flash": {"P": [2, 8, 0.04], "O": [1, 4, 0.02], "CW": 1},
    "deepseek": {"P": [2, 8, 0.04], "O": [1, 4, 0.02], "CW": 1},
    # ── 其他厂商 ──
    "hy4": {"P": [4, 16, 0.8], "O": [4, 16, 0.8], "CW": 1},
    "hy3": {"P": [4, 16, 0.8], "O": [4, 16, 0.8], "CW": 1},
    "hy": {"P": [4, 16, 0.8], "O": [4, 16, 0.8], "CW": 1},
    "glm-5.3-flash": {"P": [1, 4, 0.2], "O": [1, 4, 0.2], "CW": 1},
    "glm-5.3": {"P": [2, 8, 0.4], "O": [2, 8, 0.4], "CW": 1},
    "glm-5.2-x": {"P": [1.2, 6, 0.24], "O": [1.2, 6, 0.24], "CW": 1},
    "glm-5.2": {"P": [0.8, 3.2, 0.16], "O": [0.8, 3.2, 0.16], "CW": 1},
    "glm": {"P": [1, 4, 0.2], "O": [1, 4, 0.2], "CW": 1},
    "kimi": {"P": [1.5, 6, 0.3], "O": [1.5, 6, 0.3], "CW": 1},
    "claude-opus-5": {"P": [150, 750, 15], "O": [150, 750, 15], "CW": 1.25},
    "claude-opus-4-8": {"P": [110, 550, 11], "O": [110, 550, 11], "CW": 1.25},
    "claude": {"P": [110, 550, 11], "O": [110, 550, 11], "CW": 1.25},
    "qwen": {"P": [2.4, 9.6, 0.48], "O": [2.4, 9.6, 0.48], "CW": 1},
    "mimo": {"P": [2, 8, 0.4], "O": [2, 8, 0.4], "CW": 1},
    "minimax": {"P": [1, 5, 0.2], "O": [1, 5, 0.2], "CW": 1},
    "m3": {"P": [1, 5, 0.2], "O": [1, 5, 0.2], "CW": 1},
}
PRICE_DEF = {"P": [2, 8, 0.4], "O": [2, 8, 0.4], "CW": 1}
_PKEYS = sorted(PRICE.keys(), key=len, reverse=True)


def match_key(name: str) -> Optional[str]:
    """最长前缀匹配命中的价格表键；**未命中返回 None**（表示该模型牌价未知）。"""
    n = (name or "").lower()
    for k in _PKEYS:
        if n.startswith(k):
            return k
    return None


def is_priced(name: str) -> bool:
    """该模型是否有明确牌价。False ⇒ 其金额是按兜底档**假设**出来的，不是实际计费。

    背景：匿名/新上线模型常不在表内（实测 `stealth/space-bunny-alpha` 一个键都不命中）。
    若不区分，工具会把假设值显示成精确到 6 位小数的确定金额 —— 那是误导。
    """
    return match_key(name) is not None


def price_of(name: str) -> dict:
    """最长前缀匹配；未命中回落到默认档。未知模型的金额请配合 is_priced() 解读。"""
    k = match_key(name)
    return PRICE[k] if k else PRICE_DEF


# ── 峰谷规则 ──────────────────────────────────────────────────────────────────
PEAK_WINDOWS = [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]
PEAK_ON_MAKEUP = False
# 2026 年法定节假日（国办发明电〔2025〕7号）；跨年使用请追加年份区间
HOL_RANGES = [
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
]
MAKEUP = {
    "2026-01-04", "2026-02-14", "2026-02-28",
    "2026-05-09", "2026-09-20", "2026-10-10",
}


def _fymd(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


HOLIDAYS = set()
for _a, _b in HOL_RANGES:
    _d0 = datetime.date.fromisoformat(_a)
    _d1 = datetime.date.fromisoformat(_b)
    _cur = _d0
    while _cur <= _d1:
        HOLIDAYS.add(_fymd(_cur))
        _cur += datetime.timedelta(days=1)


def is_peak(ts_sec: float) -> bool:
    """按请求发生时刻判定高峰/空闲（本地时间 = 北京时间）。"""
    d = datetime.datetime.fromtimestamp(ts_sec)
    ds = _fymd(d.date())
    if ds in HOLIDAYS:
        return False
    dow = d.weekday()  # 0=周一
    workday = (0 <= dow <= 4) or (PEAK_ON_MAKEUP and ds in MAKEUP)
    if not workday:
        return False
    m = d.hour * 60 + d.minute
    return any(a <= m < b for a, b in PEAK_WINDOWS)


def cost_cny(model: str, input_tokens: int, output_tokens: int,
             cache_hit: int = 0, cache_write: int = 0, ts_sec: float = 0.0) -> float:
    """按单条请求的实际缓存命中计价，返回人民币金额。"""
    pp = price_of(model)
    pk = pp["P"] if is_peak(ts_sec) else pp["O"]
    cht = max(0, min(int(cache_hit or 0), int(input_tokens or 0)))
    cwt = max(0, min(int(cache_write or 0), int(input_tokens or 0) - cht))
    rest = int(input_tokens or 0) - cht - cwt
    return (cht * pk[2] + cwt * pp["CW"] * pk[0] + rest * pk[0] + int(output_tokens or 0) * pk[1]) / 1e6


def price_snapshot() -> dict:
    """导出价格表快照（供面板展示 / 审计）。"""
    return {
        "unit": "CNY per 1M tokens",
        "peak_windows": [[a // 60, a % 60, b // 60, b % 60] for a, b in PEAK_WINDOWS],
        "holidays": sorted(HOLIDAYS),
        "price": PRICE,
        "default": PRICE_DEF,
        "default_note": "未命中任何牌价键时使用；该档金额是**假设值**，不是实际计费",
    }
