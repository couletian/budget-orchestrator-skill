# -*- coding: utf-8 -*-
"""规划校验（plan）：动手之前先确认「这个预算装不装得下这些子任务」。

回答三个问题
  1. 子任务额度合计是否超出总预算？
  2. 主干额度占比是否 ≤ 70%（SKILL.md 的建议线）？
  3. 若必须压缩，按优先级该牺牲谁、牺牲多少？

为什么必须在开工前做
  协议里写着「不能只等预算不够了才开始思考如何缩减任务，而是从一开始就做足够
  科学合理地分析来规划如何在预算范围内高质量完成任务」。本模块把这句话变成一次
  可执行的校验：降级（autopilot 的 adapt）应当是**兑现预案**，而不是现场拍脑袋。

与其它模块的关系
  · priority / 主干阈值与 autopilot 完全一致（ESSENTIAL_PRIO = 4）
  · 主干占比上限与 SKILL.md 的建议值一致（ESSENTIAL_SHARE_MAX = 0.70）
  · 输出的 sacrifice 顺序 = autopilot 建议跳过任务时的**依据**
"""
from __future__ import annotations

ESSENTIAL_PRIO = 4          # >= 此值视为主干（与 SKILL.md / autopilot 一致）
ESSENTIAL_SHARE_MAX = 0.70  # 主干额度合计占总额度的建议上限


def _f(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _prio(t) -> int:
    return int(t.get("priority") or 3)


def _subtasks(lg) -> list:
    """真正的子任务（排除 root 容器）。"""
    out = []
    for t in lg.list_tasks():
        if t.get("id") == "root" or t.get("kind") == "root":
            continue
        out.append(t)
    return out


def audit(lg) -> dict:
    """做一次规划校验。纯读操作。"""
    meta = lg.all_meta()
    total_cny = _f(meta.get("budget_cny", 0))
    total_tk = _f(meta.get("budget_tokens", 0))
    has_budget = total_cny > 0 or total_tk > 0

    tasks = _subtasks(lg)
    # 已实际发生的消耗（与规划额度分开看）。
    # 与预算同口径：只算统计起点之后的请求，否则会把上一轮任务的历史算进来。
    try:
        used = lg.totals(since=_f(meta.get("since_ts", 0)))
    except Exception:
        used = {"cost": 0.0, "tt": 0}

    plan_cny = sum(_f(t.get("budget_cny")) for t in tasks)
    plan_tk = sum(_f(t.get("budget_tokens")) for t in tasks)
    ess = [t for t in tasks if _prio(t) >= ESSENTIAL_PRIO]
    opt = [t for t in tasks if _prio(t) < ESSENTIAL_PRIO]
    ess_cny = sum(_f(t.get("budget_cny")) for t in ess)
    opt_cny = plan_cny - ess_cny
    ess_tk = sum(_f(t.get("budget_tokens")) for t in ess)

    unbudgeted = [t["id"] for t in tasks
                  if not _f(t.get("budget_cny")) and not _f(t.get("budget_tokens"))]

    def share(part, whole):
        return (part / whole) if whole > 0 else 0.0

    # ── 判定：哪个维度设了就用哪个 ──────────────────────────────
    over_cny = total_cny > 0 and plan_cny > total_cny + 1e-9
    over_tk = total_tk > 0 and plan_tk > total_tk + 1e-9
    over = over_cny or over_tk

    share_cny = share(ess_cny, total_cny) if total_cny > 0 else 0.0
    share_tk = share(ess_tk, total_tk) if total_tk > 0 else 0.0
    ess_share = max(share_cny, share_tk)
    fat_essential = ess_share > ESSENTIAL_SHARE_MAX + 1e-9

    if not has_budget:
        verdict = "nobudget"
    elif over:
        verdict = "over"
    elif fat_essential or unbudgeted:
        verdict = "tight"
    else:
        verdict = "ok"

    # ── 牺牲预案：按 (优先级升序, 额度降序) 排 ──────────────────
    # 优先级越低越先牺牲；同优先级里额度大的先砍（砍一刀省得多）
    order = sorted(opt, key=lambda t: (_prio(t), -_f(t.get("budget_cny"))))
    sacrifice = []
    if over:
        need_cny = max(0.0, plan_cny - total_cny) if over_cny else 0.0
        need_tk = max(0.0, plan_tk - total_tk) if over_tk else 0.0
        left_cny, left_tk = need_cny, need_tk
        for t in order:
            c, k = _f(t.get("budget_cny")), _f(t.get("budget_tokens"))
            if left_cny <= 1e-9 and left_tk <= 1e-9:
                break
            take_c = min(c, left_cny) if left_cny > 0 else 0.0
            take_k = min(k, left_tk) if left_tk > 0 else 0.0
            full = (c > 0 and take_c >= c - 1e-9) or (k > 0 and take_k >= k - 1e-9)
            sacrifice.append({
                "id": t["id"], "name": t.get("name") or t["id"], "priority": _prio(t),
                "cny": c, "cut_cny": take_c, "cut_tokens": take_k,
                "mode": "砍掉" if full else "减半或部分",
            })
            left_cny -= take_c
            left_tk -= take_k
        shortfall = {"cny": round(max(0.0, left_cny), 6), "tokens": round(max(0.0, left_tk), 2)}
    else:
        # 未超额时也给出「可牺牲清单」，让预案提前可见
        for t in order:
            sacrifice.append({
                "id": t["id"], "name": t.get("name") or t["id"], "priority": _prio(t),
                "cny": _f(t.get("budget_cny")), "cut_cny": 0.0, "cut_tokens": 0.0,
                "mode": "备选",
            })
        shortfall = {"cny": 0.0, "tokens": 0.0}

    # ── 体检结论 ────────────────────────────────────────────
    findings = []
    if not has_budget:
        findings.append({"level": "warn", "text": "尚未设置预算额度，无法校验规划"})
    if over:
        if over_cny:
            findings.append({"level": "error",
                             "text": f"子任务额度合计 ¥{plan_cny:.4f} 超出总额度 ¥{total_cny:.4f}"
                                     f"（超 {plan_cny - total_cny:.4f}）"})
        if over_tk:
            findings.append({"level": "error",
                             "text": f"子任务 token 合计 {plan_tk:,.0f} 超出总额度 {total_tk:,.0f}"
                                     f"（超 {plan_tk - total_tk:,.0f}）"})
    else:
        findings.append({"level": "ok",
                         "text": f"子任务额度合计 ¥{plan_cny:.4f}，在总额度 ¥{total_cny:.4f} 之内"
                                 if total_cny > 0 else "子任务额度合计未超额"})
    if total_cny > 0:
        lv = "warn" if fat_essential else "ok"
        findings.append({"level": lv,
                         "text": f"主干额度占比 {ess_share * 100:.1f}%"
                                 f"（建议 ≤ {ESSENTIAL_SHARE_MAX * 100:.0f}%）"})
    if unbudgeted:
        findings.append({"level": "warn",
                         "text": f"{len(unbudgeted)} 个子任务未标额度：{'、'.join(unbudgeted)}"
                                 f" —— 未标额度就无法参与取舍计算"})

    gap_cny = max(0.0, plan_cny - total_cny) if total_cny > 0 else 0.0
    gap_tk = max(0.0, plan_tk - total_tk) if total_tk > 0 else 0.0
    covered = shortfall["cny"] <= 1e-9 and shortfall["tokens"] <= 1e-9
    if verdict == "over":
        _a = f"规划超出额度"
        if gap_cny > 1e-9:
            _a += f" ¥{gap_cny:.4f}"
        if covered:
            _a += "，按下方顺序牺牲即可覆盖。"
        else:
            _a += (f"，且按下方顺序砍完仍缺 ¥{shortfall['cny']:.4f}"
                   f" / {shortfall['tokens']:,.0f} token —— 需追加预算或缩减主干。")
    else:
        _a = ""
    advice = {
        "ok": "规划合理，可以开工。",
        "tight": "规划偏紧：主干占比过高或存在未标额度的任务，建议先调整再开工。",
        "over": _a,
        "nobudget": "先设置预算额度（budget --cny / --tokens），再做规划校验。",
    }[verdict]

    return {
        "verdict": verdict,
        "has_budget": has_budget,
        "total": {"tokens": total_tk, "cny": total_cny},
        "planned": {"tokens": plan_tk, "cny": plan_cny,
                    "share_cny": share(plan_cny, total_cny),
                    "share_tokens": share(plan_tk, total_tk)},
        "essential": {"ids": [t["id"] for t in ess], "cny": ess_cny, "tokens": ess_tk,
                      "share": ess_share, "share_max": ESSENTIAL_SHARE_MAX},
        "optional": {"ids": [t["id"] for t in opt], "cny": opt_cny},
        "unbudgeted": unbudgeted,
        "sacrifice": sacrifice,
        "shortfall": shortfall,
        "findings": findings,
        "advice": advice,
        "used": {"cny": _f(used.get("cost")), "tokens": _f(used.get("tt"))},
        "count": len(tasks),
    }


def plan_text(r: dict) -> str:
    """渲染成人能读的校验报告。"""
    L = []
    bar = "─" * 62
    L.append(bar)
    L.append(f"  规划校验　子任务 {r['count']} 个")
    L.append(bar)
    if r["has_budget"]:
        L.append(f"  总预算            {('¥%.4f' % r['total']['cny']) if r['total']['cny'] else '未设'}"
                 f"    {('%.0f token' % r['total']['tokens']) if r['total']['tokens'] else 'token 未设'}")
    L.append(f"  子任务额度合计     ¥{r['planned']['cny']:.4f}"
             + (f"   占总额度 {r['planned']['share_cny'] * 100:.1f}%" if r['total']['cny'] else ""))
    L.append(f"  主干额度          ¥{r['essential']['cny']:.4f}"
             + (f"   占比 {r['essential']['share'] * 100:.1f}%"
                f"  {'✓' if r['essential']['share'] <= r['essential']['share_max'] else '⚠ 超过建议上限 70%'}"
                if r['total']['cny'] else ""))
    L.append(f"  非主干额度         ¥{r['optional']['cny']:.4f}")
    L.append(f"  已实际消耗         ¥{r['used']['cny']:.4f} / {r['used']['tokens']:,.0f} token")
    if r["unbudgeted"]:
        L.append(f"  未标额度           {len(r['unbudgeted'])} 个：{'、'.join(r['unbudgeted'])}")
    L.append(bar)
    if r["sacrifice"]:
        head = "取舍预案（预算不够时按此顺序牺牲）" if r["verdict"] != "over" else "建议牺牲顺序（补足缺口）"
        L.append(f"  {head}：")
        for i, s in enumerate(r["sacrifice"], 1):
            if r["verdict"] == "over" and s["cut_cny"] > 1e-9:
                extra = f"　→ 砍 ¥{s['cut_cny']:.4f}（{s['mode']}）"
            elif r["verdict"] == "over":
                extra = f"　→ 砍 {s['cut_tokens']:,.0f} token"
            else:
                extra = f"　¥{s['cny']:.4f}　可牺牲"
            L.append(f"    {i}. {s['id']:<6} P{s['priority']}  {s['name'][:22]:<24} {extra}")
        if r["verdict"] == "over" and (r["shortfall"]["cny"] > 1e-9 or r["shortfall"]["tokens"] > 1e-9):
            L.append(f"    ⚠ 砍完仍缺 ¥{r['shortfall']['cny']:.4f} / {r['shortfall']['tokens']:,.0f} token"
                     f" —— 需追加预算或缩减主干范围")
    else:
        L.append("  取舍预案：当前没有非主干子任务可牺牲（全部是主干）")
    L.append(bar)
    mark = {"ok": "✓", "tight": "⚠", "over": "✗", "nobudget": "·"}[r["verdict"]]
    L.append(f"  {mark} 结论：{r['advice']}")
    for f in r["findings"]:
        sym = {"ok": "·", "warn": "!", "error": "×"}.get(f["level"], "·")
        L.append(f"    {sym} {f['text']}")
    L.append(bar)
    return "\n".join(L)


def next_action(r: dict) -> str:
    """给 agent 的一句话行动指令。"""
    v = r["verdict"]
    if v == "ok":
        return "规划通过，按计划开工；执行中按 autopilot 指令推进。"
    if v == "tight":
        return ("规划偏紧 —— 先把未标额度的任务补上，或降低主干占比（砍/降级非主干），"
                "再开工；不要带着未知额度上路。")
    if v == "over":
        return ("规划超额 —— **先按给出的顺序牺牲，或向用户确认取舍后再开工**；"
                "不要先做再说。")
    return "先设置预算额度，再做规划校验。"
