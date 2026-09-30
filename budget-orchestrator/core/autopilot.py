# -*- coding: utf-8 -*-
"""自主协调引擎（autopilot）。

设计目标：让 AI **自主**在预算内把任务做到最好，而不是每超一点就停下来等人。

本模块把「预算状态 + 任务树 + 浪费审计」翻译成一份**可直接执行的行动指令**：
AI 读到它就能自己降级、自己取舍、自己继续推进，全程不需要人工逐次批准。

三种决策
  proceed  —— 预算充裕，正常推进，无需任何调整
  adapt    —— 触发降级：执行 actions 后**继续**（AI 自主，不打断）
  escalate —— 降级阶梯已用尽（典型情况：预算连主干都做不完），才请人追加

人的位置（始终保留，但不再是主路径）
  · 随时 pause / stop
  · 随时改预算、改任务优先级
  · 只在 escalate 时被请求追加额度

动作类型（都可被 AI 直接执行，不需二次解释）
  behavior  行为约束：AI 照做即可            —— 压缩、输出纪律、减少评审
  model     模型切换：给下次子代理指定模型     —— 换更省的档
  task      任务取舍：跳过 / 降级 / 收敛范围   —— 保住主干
  output    输出限制：限制篇幅与轮次
  escalate  请人追加预算
"""
from __future__ import annotations

CHEAP_MODEL = "deepseek-v4.1-flash"   # 已知最省的可用档，可在 meta 里覆盖

# YAGNI 阶梯：决策永远从"要不要做"开始，最后才谈"怎么写"
RUNG = {
    0: "是否已做完",
    1: "这一步是否必要",
    2: "是否已有现成实现",
    3: "标准库/原生是否够",
    4: "能否合并且少调用工具",
    5: "能否换更省的模型",
    6: "能否缩小范围",
    7: "才写最少可执行代码",
}


def _act(aid, atype, rung, instruction, **params):
    d = {"id": aid, "type": atype, "rung": rung, "rung_ask": RUNG.get(rung, ""),
         "instruction": instruction}
    if params:
        d["params"] = params
    return d


def _fmt(n):
    n = float(n or 0)
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(int(n))


def directive(snap, cfg, tasks, audit=None) -> dict:
    """产出当期决策指令。纯函数，便于测试。"""
    pct = float(snap.get("pct", {}).get("overall", 0) or 0)
    soft = float(cfg.get("soft_pct", 70) or 70)
    hard = float(cfg.get("hard_pct", 100) or 100)
    has_budget = (float(cfg.get("budget_cny", 0) or 0) > 0) or \
                 (float(cfg.get("budget_tokens", 0) or 0) > 0)

    # 只考虑真正的子任务（root 是容器，不是可取舍的对象）
    subs = [t for t in tasks if t.get("kind") != "root" and t.get("id") != "root"]
    pending_essential = [t for t in subs
                         if int(t.get("priority") or 3) >= 4 and t.get("status") != "done"]
    pending_other = [t for t in subs
                     if int(t.get("priority") or 3) < 4 and t.get("status") != "done"]

    base = {
        "mode": "proceed", "level": snap.get("level", "ok"), "pct": round(pct, 2),
        "budget_tokens": cfg.get("budget_tokens", 0), "budget_cny": cfg.get("budget_cny", 0),
        "remaining_cny": (snap.get("remaining") or {}).get("cny"),
        "remaining_tokens": (snap.get("remaining") or {}).get("tokens"),
        "essential_pending": [t["id"] for t in pending_essential],
        "optional_pending": [t["id"] for t in pending_other],
    }

    if not has_budget or pct < soft:
        base["actions"] = []
        base["note"] = "预算充裕，按计划推进；无需调整。"
        base["next_check"] = "每完成一个子任务后重新调用本指令（boc_autopilot）"
        return base

    # ── 分档：越紧，动作越激进 ─────────────────────────────────────────────
    mid = (soft + hard) / 2.0
    if pct >= hard:
        band = 3
    elif pct >= mid:
        band = 2
    else:
        band = 1

    acts = [
        _act("yagni_first", "behavior", 1,
             "每一步先问『这一步是否真的必要』：能跳过就跳过并一句话说明；"
             "能复用本会话/本仓库已有实现就不要新写。"),
        _act("trim_output", "output", 7,
             "输出纪律：代码与结论优先，解释不超过三行；不写没人要的抽象与样板。"),
    ]

    if band >= 2:
        acts.append(_act("compress_context", "behavior", 4,
                         "压缩上下文：只读相关片段（用 Grep 定位后再读），"
                         "长文件分段处理，避免整份塞进上下文；合并同类工具调用。"))
        acts.append(_act("switch_cheap_model", "model", 5,
                         f"把非关键子任务切到更省的模型（{CHEAP_MODEL}）；"
                         "先看当前主用模型是否已经是它，是则跳过本条。",
                         model=CHEAP_MODEL))
        if audit and audit.get("findings"):
            top = audit["findings"][0]
            acts.append(_act("fix_waste", "behavior", 2,
                             f"优先消除审计指出的浪费：{top['evidence']} → {top['advice']}"))

    if band >= 3:
        acts.append(_act("cut_rounds", "behavior", 6,
                         "减少评审/验证轮次：一次做对优于反复返工；"
                         "只在主干交付物上做校验。"))
        if pending_other:
            acts.append(_act("skip_optional", "task", 6,
                             "暂停或跳过非主干子任务，把剩余额度全部留给主干交付物。",
                             skip_task_ids=[t["id"] for t in pending_other],
                             keep_task_ids=[t["id"] for t in pending_essential]))

    # ── 只有"预算已耗尽且主干未完成"才请人 ─────────────────────────────────
    mode, escalate = "adapt", None
    if pct >= 100:
        if not pending_essential:
            base["note"] = ("预算已超，但主干交付物已完成 —— 停止新增消耗，直接收尾交付，"
                            "不要再做任何优化或补充。")
        else:
            rem = (snap.get("remaining") or {}).get("cny")
            rem_tk = (snap.get("remaining") or {}).get("tokens")
            exhausted = ((rem is not None and rem <= 0) or
                         (rem_tk is not None and rem_tk <= 0))
            if exhausted:
                mode = "escalate"
                escalate = {
                    "reason": f"预算已用尽（已用 {pct:.0f}%），但主干子任务尚未完成",
                    "essential_pending": [t["id"] for t in pending_essential],
                    "need_hint_cny": round(max(0.0, pct / 100.0 * float(cfg.get("budget_cny") or 0)
                                               - float(cfg.get("budget_cny") or 0)) or 0.001, 4),
                    "ask": "请追加预算，或授权砍掉主干范围；在此之前我将只做最低成本的保住动作。",
                }
                acts.append(_act("essential_only", "task", 6,
                                 "只推进主干子任务，其余一律冻结。",
                                 keep_task_ids=[t["id"] for t in pending_essential]))
                acts.append(_act("request_approval", "escalate", 6,
                                 escalate["ask"]))
            else:
                base["note"] = ("预算已超但仍有余额，只推进主干，其余全部冻结；"
                                "每次动作后重新自检。")

    base["mode"] = mode
    base["actions"] = acts
    if escalate:
        base["escalate"] = escalate
    base.setdefault("note", {
        1: "接近软阈值：开始爬 YAGNI 阶梯，成本意识优先。",
        2: "已过半程预算：执行全部降级动作，但**继续推进，不要停**。",
        3: "已近硬阈值：激进瘦身，保主干。",
    }[band])
    base["next_check"] = ("每完成一个子任务、或每 2-3 次工具调用后重新调用 boc_autopilot；"
                          "人工可随时 pause/stop/改预算覆盖本决策。")
    return base


def autopilot_text(d: dict) -> str:
    """把指令渲染成 AI 一眼能读的文本。"""
    tag = {"proceed": "▶ 继续推进", "adapt": "⚙ 降级后继续（自主）", "escalate": "⚠ 需要人工"}
    out = [f"{tag.get(d['mode'], d['mode'])}　预算已用 {d['pct']:.1f}%（等级 {d['level']}）"]
    if d.get("remaining_cny") is not None:
        out.append(f"剩余 ¥{d['remaining_cny']:.4f}"
                   + (f" / {_fmt(d['remaining_tokens'])} token" if d.get("remaining_tokens") is not None else ""))
    out.append(f"说明：{d['note']}")
    if d.get("essential_pending"):
        out.append("主干待办：" + "、".join(d["essential_pending"]))
    if d.get("optional_pending"):
        out.append("可选待办：" + "、".join(d["optional_pending"]))
    if d["actions"]:
        out.append("\n必须执行的动作：")
        for i, a in enumerate(d["actions"], 1):
            p = ""
            if a["type"] == "model":
                p = f"（→ {a['params']['model']}）"
            elif a["type"] == "task" and a["params"].get("skip_task_ids"):
                p = f"（跳过 {', '.join(a['params']['skip_task_ids'])}）"
            out.append(f"  {i}. [{a['type']}·阶梯{a['rung']}] {a['instruction']}{p}")
    if d.get("escalate"):
        e = d["escalate"]
        out.append(f"\n需要人工：{e['reason']}")
        out.append(f"  {e['ask']}")
    out.append(f"\n下次自检：{d['next_check']}")
    return "\n".join(out)
