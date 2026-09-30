# -*- coding: utf-8 -*-
"""预算策略与控制面。

职责
- 汇总账本 → 生成面板/CLI 可直接消费的状态快照。
- 按软/硬阈值判定告警等级，在 enforce 模式下"喊停"（置 paused）。
- 提供控制动作：暂停、恢复、停止、设/加预算、切换活跃任务、切模型。
- 给出预算不足时的**降级阶梯**建议（供监督 agent 参考，不强制）。

诚实边界：本模块无法拦截宿主自身的 LLM 调用。所谓"硬控制"是通过
① 置 paused 状态 + ② 面板/MCP 的 check 接口返回 STOP + ③ 可选 hook 拦截工具调用
三者组合实现，属"确定性规则的强制 + 人的最终决定"，而非内核级拦截。
"""
from __future__ import annotations

import time
from typing import Optional

from .ledger import Ledger
from . import pricing
from . import lean as lean_mod
from . import autopilot as autopilot_mod

# 降级阶梯：第一条来自 ponytail——**先别写/别多做**，比任何"省着做"都更省。
DEGRADE_LADDER = [
    ("yagni_reuse", "先爬 YAGNI 阶梯：这一步是否必要？能否复用已有实现 / 标准库 / 平台原生能力？"),
    ("trim_output", "执行输出纪律：代码优先、解释≤3行；删掉没人要的抽象、样板与预留脚手架"),
    ("compress_context", "压缩上下文，减少重复输入与重复读取"),
    ("switch_small_model", "把非关键子任务切到更小更便宜的模型"),
    ("reduce_review_rounds", "减少评审/辩论轮次"),
    ("skip_noncritical", "跳过非关键子任务，保住主干交付"),
    ("request_approval", "请求用户追加预算或批准超支"),
]


def _f(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


class BudgetManager:
    def __init__(self, ledger: Ledger):
        self.lg = ledger

    # ── 配置读写 ───────────────────────────────────────────────────────────
    def config(self) -> dict:
        m = self.lg.all_meta()
        return {
            "budget_tokens": _f(m.get("budget_tokens", 0)),
            "budget_cny": _f(m.get("budget_cny", 0)),
            "soft_pct": _f(m.get("soft_pct", 70)),
            "hard_pct": _f(m.get("hard_pct", 100)),
            "reserve_pct": _f(m.get("reserve_pct", 15)),
            "state": m.get("state", "running"),
            "mode": m.get("mode", "auto"),
            "active_task": m.get("active_task", "root"),
            "workspace": m.get("workspace", ""),
            "since_ts": _f(m.get("since_ts", 0)),
            "currency": m.get("currency", "CNY"),
        }

    def set_budget(self, tokens: Optional[float] = None, cny: Optional[float] = None):
        if tokens is not None:
            self.lg.set_meta("budget_tokens", tokens)
        if cny is not None:
            self.lg.set_meta("budget_cny", cny)
        self.lg.add_event("budget_set", f"预算更新 tokens={tokens} cny={cny}")

    def add_budget(self, tokens: Optional[float] = None, cny: Optional[float] = None):
        cur = self.config()
        nt = cur["budget_tokens"] + (tokens or 0)
        nc = cur["budget_cny"] + (cny or 0)
        self.lg.set_meta("budget_tokens", nt)
        self.lg.set_meta("budget_cny", nc)
        self.lg.add_event("budget_add", f"追加预算 tokens+{tokens} cny+{cny} → {nt}/{nc}")
        if cur["state"] == "overpaused" or cur["state"] == "paused":
            self.lg.set_meta("state", "running")
            self.lg.add_event("resume", "追加预算后自动恢复", level="warn")

    # ── 速率与预测 ─────────────────────────────────────────────────────────
    def rate(self, window_sec: float = 300.0) -> dict:
        since = time.time() - window_sec
        rows = self.lg.by_hour(since=since)
        if not rows:
            rows = self.lg.by_hour(since=self.config()["since_ts"])
        if not rows:
            return {"tokens_per_min": 0.0, "cny_per_min": 0.0, "eta_min": None, "basis": "none"}
        t0 = min(r["ts"] for r in rows)
        t1 = max(r["ts"] for r in rows)
        span = max(60.0, t1 - t0)
        tk = sum(r["total_tokens"] for r in rows)
        cy = sum(r["cost_cny"] for r in rows)
        tpm = tk / (span / 60.0)
        cpm = cy / (span / 60.0)
        cfg = self.config()
        used = self.lg.totals(since=cfg["since_ts"])
        eta = None
        if cfg["budget_tokens"] > 0 and tpm > 0:
            eta = max(0.0, (cfg["budget_tokens"] - used["tt"]) / tpm)
        return {"tokens_per_min": tpm, "cny_per_min": cpm, "eta_min": eta,
                "basis": "window" if len(rows) > 1 else "single"}

    # ── 快照 ──────────────────────────────────────────────────────────────
    def snapshot(self, series_minutes: int = 60, events_limit: int = 60) -> dict:
        cfg = self.config()
        # 预算按任务计 → "已用"只算统计起点之后的请求，历史不参与（见 Ledger.totals）
        used = self.lg.totals(since=cfg["since_ts"])
        tk_budget = cfg["budget_tokens"]
        cy_budget = cfg["budget_cny"]
        pct_tk = (used["tt"] / tk_budget * 100.0) if tk_budget > 0 else 0.0
        pct_cy = (used["cost"] / cy_budget * 100.0) if cy_budget > 0 else 0.0
        overall = max(pct_tk, pct_cy)
        # 到底是谁把预算顶爆了？两个上限同时生效时，这个信息决定该校准哪一个。
        # （实测踩过：token 上限拍脑袋设小 → 金额才用 23% 就报 STOP。）
        binding = "tokens" if pct_tk >= pct_cy else "cny"
        both_set = tk_budget > 0 and cy_budget > 0
        misaligned = bool(both_set and overall >= cfg["hard_pct"]
                          and min(pct_tk, pct_cy) < cfg["soft_pct"])
        if overall >= cfg["hard_pct"]:
            level = "over"
        elif overall >= cfg["soft_pct"]:
            level = "warn"
        else:
            level = "ok"

        # 时间序列（按分钟）
        series: dict = {}
        now = time.time()
        for r in self.lg.by_hour(since=now - series_minutes * 60):
            k = int(r["ts"] // 60) * 60
            s = series.setdefault(k, {"tk": 0, "cy": 0.0})
            s["tk"] += r["total_tokens"]
            s["cy"] += r["cost_cny"]
        series_list = [{"t": k, "tk": v["tk"], "cy": v["cy"]}
                       for k, v in sorted(series.items())]

        tasks = self.lg.list_tasks()
        for t in tasks:
            agg = self.lg.totals(t["id"], since=cfg["since_ts"])
            t["used_tokens"] = agg["tt"]
            t["used_cny"] = agg["cost"]
            t["used_requests"] = agg["n"]
            if t.get("budget_tokens"):
                t["pct"] = round(agg["tt"] / t["budget_tokens"] * 100.0, 2)
            else:
                t["pct"] = None

        # 有效额度：扣除预留给协调/监督/评审 agent 的份额。
        # 注意账本统计的是**整个会话含所有子代理**的真实消耗，因此"工具自身及监督开销"
        # 天然被计入总额；reserve_pct 只是建议额外留出的安全余量。
        reserve = max(0.0, min(90.0, cfg["reserve_pct"])) / 100.0
        eff_tk = tk_budget * (1 - reserve) if tk_budget > 0 else None
        eff_cny = cy_budget * (1 - reserve) if cy_budget > 0 else None

        # lean 档位：预算越紧，瘦身纪律越强（对应 ponytail 的 lite/full/ultra）
        lv = lean_mod.lean_level(overall)

        models = self.lg.by_model(since=cfg["since_ts"])
        # 牌价未知的模型（匿名/新上线）：其金额按兜底档**假设**而来，必须标出来，
        # 否则工具会把假设值显示成精确到 6 位小数的确定金额 —— 那是误导。
        unpriced = [{"model": m["model"], "n": m["n"], "tt": m["tt"], "cost": m["cost"]}
                    for m in models if not pricing.is_priced(m["model"])]

        snap = {
            "now": now,
            "state": cfg["state"],
            "mode": cfg["mode"],
            "workspace": cfg["workspace"],
            "currency": cfg["currency"],
            "since_ts": cfg["since_ts"],
            "budget": {"tokens": tk_budget, "cny": cy_budget,
                       "soft_pct": cfg["soft_pct"], "hard_pct": cfg["hard_pct"],
                       "reserve_pct": cfg["reserve_pct"]},
            "used": {"tokens": used["tt"], "cny": used["cost"], "input": used["it"],
                     "output": used["ot"], "cache_hit": used["ch"],
                     "requests": used["n"]},
            "remaining": {"tokens": max(0.0, tk_budget - used["tt"]) if tk_budget > 0 else None,
                          "cny": max(0.0, cy_budget - used["cost"]) if cy_budget > 0 else None},
            "effective": {"tokens": eff_tk, "cny": eff_cny,
                          "note": "扣除 reserve_pct 预留后的建议可用额度"},
            "pct": {"tokens": round(pct_tk, 2), "cny": round(pct_cy, 2),
                    "overall": round(overall, 2)},
            "level": level,
            "binding": binding,
            "misaligned": misaligned,
            "rate": self.rate(),
            "models": models,
            "pricing": {
                "all_priced": not unpriced,
                "unpriced": unpriced,
                "est_cny": round(sum(u["cost"] for u in unpriced), 6),
                "note": "unpriced 中的模型金额由兜底档假设得出，非实际计费；"
                        "补牌价见 core/pricing.py 的 PRICE",
            },
            "agents": self.lg.by_agent(since=cfg["since_ts"]),
            "tasks": tasks,
            "recent": self.lg.recent(40),
            "events": self.lg.events(events_limit),
            "series": series_list,
            # 监听健康度：lag_bytes=0 表示日志已读到底；
            # 此时面板若无变化，是宿主还没落盘，而非本工具采集滞后。
            "watch": self.lg.cursor_lag(),
            "degrade_hint": self.degrade_hint(level, overall),
            "lean": {"level": lv["key"], "name": lv["name"], "rules": lv["rules"],
                     "never_cut": lean_mod.NEVER_CUT},
            "lean_audit": (lambda r: {"findings": r["findings"][:6], "io": r["io"],
                                      "tools": r["tools"][:8], "honesty": r["honesty"]})(
                self.lean_report()),
        }
        # 自主协调指令：AI 读它来自行决定如何调整，不必等人
        snap["directive"] = autopilot_mod.directive(snap, cfg, tasks, self.lean_report())
        return snap

    def autopilot(self) -> dict:
        """当前应执行的自主决策指令。"""
        return self.snapshot()["directive"]

    def lean_report(self, ttl: float = 30.0) -> dict:
        """精简审计（带缓存，避免 SSE 每次重算）。"""
        now = time.time()
        c = getattr(self, "_lean_cache", None)
        if c and now - c[0] < ttl:
            return c[1]
        rep = lean_mod.audit(self.lg)
        self._lean_cache = (now, rep)
        return rep

    def degrade_hint(self, level: str, pct: float) -> dict:
        if level == "ok":
            return {"needed": False, "steps": []}
        idx = 0
        if pct >= 100:
            idx = 2
        elif pct >= 85:
            idx = 1
        steps = DEGRADE_LADDER[idx:]
        return {"needed": True, "steps": [{"action": a, "why": b} for a, b in steps]}

    # ── 阈值评估（由 watcher 回调触发）──────────────────────────────────────
    def evaluate(self) -> dict:
        snap = self.snapshot(series_minutes=5, events_limit=1)
        level = snap["level"]
        cfg = self.config()
        last = self.lg.get_meta("last_level", "ok")
        if level != last:
            if level == "warn":
                self.lg.add_event(
                    "threshold_soft",
                    f"已达软阈值 {snap['pct']['overall']}%（tokens {snap['pct']['tokens']}% / "
                    f"金额 {snap['pct']['cny']}%），建议开始降级", level="warn",
                    data=snap["degrade_hint"])
            elif level == "over":
                self.lg.add_event(
                    "threshold_hard",
                    f"已达硬阈值 {snap['pct']['overall']}%，停止新增消耗", level="error",
                    data=snap["degrade_hint"])
                if cfg["mode"] == "enforce":
                    # 仅在 enforce 模式硬停（等人）。auto 模式不打断，由 AI 自主降级。
                    if cfg["state"] == "running":
                        self.lg.set_meta("state", "paused")
                        self.lg.add_event("auto_pause", "enforce 模式：预算触顶，已自动暂停", level="error")
                elif cfg["mode"] == "auto":
                    d = self.autopilot()
                    self.lg.add_event(
                        "autopilot_adapt",
                        f"自主协调：预算触顶，AI 接管降级（{len(d.get('actions', []))} 项动作）",
                        level="warn", data={"mode": d["mode"], "essential": d.get("essential_pending")})
            else:
                self.lg.add_event("threshold_ok", "用量回到阈值内", level="info")
            self.lg.set_meta("last_level", level)
        return {"level": level, "pct": snap["pct"], "hint": snap["degrade_hint"]}

    # ── 控制动作 ───────────────────────────────────────────────────────────
    def control(self, action: str, **kw) -> dict:
        if action == "pause":
            self.lg.set_meta("state", "paused")
            self.lg.add_event("pause", kw.get("reason") or "用户/监督方暂停", level="warn")
        elif action == "resume":
            self.lg.set_meta("state", "running")
            self.lg.add_event("resume", "恢复运行", level="info")
        elif action == "stop":
            self.lg.set_meta("state", "stopped")
            self.lg.add_event("stop", kw.get("reason") or "任务终止", level="error")
        elif action == "set_budget":
            self.set_budget(kw.get("tokens"), kw.get("cny"))
        elif action == "add_budget":
            self.add_budget(kw.get("tokens"), kw.get("cny"))
        elif action == "set_mode":
            self.lg.set_meta("mode", kw.get("mode", "auto"))
            self.lg.add_event("mode", f"模式 → {kw.get('mode')}")
        elif action == "set_threshold":
            if kw.get("soft_pct") is not None:
                self.lg.set_meta("soft_pct", kw["soft_pct"])
            if kw.get("hard_pct") is not None:
                self.lg.set_meta("hard_pct", kw["hard_pct"])
            self.lg.add_event("threshold", f"阈值 → soft {kw.get('soft_pct')} / hard {kw.get('hard_pct')}")
        elif action == "set_active_task":
            self.lg.set_meta("active_task", kw.get("task_id", "root"))
            self.lg.add_event("active_task", f"活跃任务 → {kw.get('task_id')}")
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
        return {"ok": True, "state": self.lg.get_meta("state"), "config": self.config()}

    def check(self) -> dict:
        """给 agent / hook 调用的轻量判定接口。"""
        snap = self.snapshot(series_minutes=5, events_limit=1)
        cfg = self.config()
        verdict = "GO"
        if cfg["state"] in ("paused", "stopped"):
            verdict = "STOP"
        elif snap["level"] == "over":
            verdict = "STOP"
        elif snap["level"] == "warn":
            verdict = "DEGRADE"
        return {
            "verdict": verdict,
            "state": cfg["state"],
            "level": snap["level"],
            "lean": snap["lean"],
            "pct": snap["pct"],
            # 到底是哪个口径顶爆的：两个上限同时生效时，这决定该校准哪一个
            "binding": snap.get("binding"),
            "misaligned": snap.get("misaligned"),
            "remaining_tokens": snap["remaining"]["tokens"],
            "remaining_cny": snap["remaining"]["cny"],
            "degrade_hint": snap["degrade_hint"],
        }
