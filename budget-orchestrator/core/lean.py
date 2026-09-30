# -*- coding: utf-8 -*-
"""精简审计（ponytail 融合层）。

把 ponytail 的 **YAGNI 阶梯**与**输出纪律**，从"行为习惯"变成"可测量的预算守卫"：
账本已经知道每次消耗发生在**哪个工具、哪个目标、哪次请求**，据此就能指出
"这里本可以更省"——而且给出的是**实测事实**，不是感觉。

━━ 诚实边界（沿用 ponytail-gain 的硬规则）━━
绝不输出"你省了 N token"这类数字：**没写出来的版本不存在，没有基线可减**。
本模块只报三类可数的事实：
  1. 同一目标被重复操作了几次（返工）；
  2. 这些操作关联了多少真实消耗（归因，不是"浪费量"）；
  3. 哪几次请求异常重（上下文膨胀 / 输出冗长嫌疑）。
建议部分标明它对应 YAGNI 阶梯的第几级，由执行方自行决定是否采纳。
"""
from __future__ import annotations

# ── ponytail 的 YAGNI 阶梯（停在第一个成立的级别）─────────────────────────────
LADDER = [
    {"rung": 1, "ask": "这一步是否真的需要存在？", "do": "推测性需求直接跳过，一句话说明理由"},
    {"rung": 2, "ask": "本仓库/本会话里已经有现成的？", "do": "先找再写；重复实现是最常见的浪费"},
    {"rung": 3, "ask": "标准库能做？", "do": "用标准库，不写等价的自研实现"},
    {"rung": 4, "ask": "平台原生能力覆盖？", "do": "用原生特性替代第三方依赖"},
    {"rung": 5, "ask": "已安装的依赖能解决？", "do": "用现有依赖，不为几行代码新增依赖"},
    {"rung": 6, "ask": "能一行搞定？", "do": "一行"},
    {"rung": 7, "ask": "才写最少可运行代码", "do": "最短可行 diff 即完成"},
]

# ── lean 档位：按预算已用比例自动升级强度（对应 ponytail 的 lite/full/ultra）──
LEAN_LEVELS = [
    {"key": "normal", "lo": 0,  "hi": 60,  "name": "常规",
     "rules": ["保持预算意识，正常推进"]},
    {"key": "lite", "lo": 60, "hi": 80, "name": "轻瘦身",
     "rules": ["继续做，但每步附一句更省的做法供选择",
               "能复用现有代码就不新写"]},
    {"key": "full", "lo": 80, "hi": 95, "name": "强制阶梯",
     "rules": ["执行 YAGNI 阶梯：需求→已有→标准库→原生→已装依赖→一行→最少代码",
               "删优于加；不写单实现抽象、不为将来留脚手架",
               "输出纪律：代码优先，解释不超过三行",
               "非关键子任务换更小模型或直接砍掉"]},
    {"key": "ultra", "lo": 95, "hi": 10 ** 9, "name": "极端瘦身",
     "rules": ["只交主干，其余当场砍掉并说明",
               "先给最省的一行方案，再质疑需求本身是否成立",
               "任何新增依赖、新增文件、新增抽象都需要明确理由"]},
]

# 无论预算多紧都不能省的东西（ponytail 的 When NOT to be lazy）
NEVER_CUT = [
    "信任边界的输入校验",
    "防止数据丢失的错误处理",
    "安全措施",
    "无障碍基础",
    "用户明确要求的内容",
    "一处可运行的检查（非平凡逻辑必须留一个最小可跑验证）",
]

# 工具 → YAGNI 阶梯的联想（用于把"高消耗工具"翻译成"该爬哪一级"）
TOOL_TO_RUNG = {
    "Write": 2,   # 先看有没有现成的可复用
    "Edit": 7,    # 反复小改 = 动手前没想清楚，属于实现层返工
    "Read": 2,
    "Bash": 4,    # 平台原生能力往往能替掉一堆手工步骤
    "Grep": 2,
    "WebFetch": 5,
    "WebSearch": 5,
}


def lean_level(pct: float) -> dict:
    """已用百分比 → lean 档位。"""
    p = max(0.0, float(pct or 0))
    for lv in LEAN_LEVELS:
        if lv["lo"] <= p < lv["hi"]:
            return lv
    return LEAN_LEVELS[-1]


def _fmt_tk(n) -> str:
    n = float(n or 0)
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.2f}万"
    return str(int(n))


def audit(lg, since: float | None = None) -> dict:
    """产出精简审计报告。lg 为 Ledger 实例。"""
    if since is None:
        since = float(lg.get_meta("since_ts") or 0)
    io = lg.io_ratio(since)
    tools = lg.by_tool(since)
    repeats = lg.repeat_targets(since, tools=("Read", "Write", "Edit", "Bash"),
                               min_count=2, limit=20)
    top_in = lg.top_requests(since, "input_tokens", 10)
    top_out = lg.top_requests(since, "output_tokens", 10)
    total_tk = sum(t["tt"] for t in tools)
    total_div = total_tk or 1

    findings = []

    # ① 返工：同一目标被反复改（实测次数）
    for r in repeats:
        if r["tool"] == "Write" and r["n"] >= 2:
            findings.append({
                "kind": "rewrite", "severity": "high",
                "rung": TOOL_TO_RUNG["Write"],
                "evidence": f"{r['target'][:70]} 被整体重写 {r['n']} 次，"
                            f"关联消耗 {_fmt_tk(r['tt'])} token",
                "advice": "推倒重来通常意味着动手前没定清目标；先读目标文件与相邻实现再落笔",
            })
        elif r["n"] >= 4:
            findings.append({
                "kind": "rework", "severity": "high",
                "rung": TOOL_TO_RUNG.get(r["tool"], 7),
                "evidence": f"{r['target'][:70]} 被 {r['tool']} 操作 {r['n']} 次，"
                            f"关联消耗 {_fmt_tk(r['tt'])} token",
                "advice": "连续小步修改说明需求没想透；先读完再一次性改完",
            })
        elif r["n"] >= 3:
            findings.append({
                "kind": "rework", "severity": "medium",
                "rung": TOOL_TO_RUNG.get(r["tool"], 7),
                "evidence": f"{r['target'][:70]} 被 {r['tool']} 操作 {r['n']} 次，"
                            f"关联消耗 {_fmt_tk(r['tt'])} token",
                "advice": "考虑一次读全/改全，减少重复往返",
            })

    # ② 输出冗长嫌疑：输出/输入比偏高
    if io["out_per_in"] >= 30 and io["requests"] >= 5:
        findings.append({
            "kind": "chattiness", "severity": "medium", "rung": 7,
            "evidence": f"输出/输入 = {io['out_per_in']:.1f}%（输出 {_fmt_tk(io['output'])} token，"
                        f"{io['requests']} 次请求）",
            "advice": "输出纪律：代码优先，解释不超过三行；解释比代码长就删解释",
        })

    # ③ 上下文膨胀：单次请求输入异常大
    big = [r for r in top_in if (r["input_tokens"] or 0) >= 150000]
    if big:
        b = big[0]
        findings.append({
            "kind": "context_bloat", "severity": "medium", "rung": 2,
            "evidence": f"最大单次输入 {_fmt_tk(b['input_tokens'])} token"
                        + (f"（{b['tool']}：{b['target'][:40]}）" if b.get("tool") else ""),
            "advice": "改用 Grep 定位后只读相关片段；长文件分段处理，避免整份塞进上下文",
        })

    # ④ 工具热点：单一工具吃掉大半预算
    for t in tools[:3]:
        share = t["tt"] / total_div
        if share >= 0.45 and t["n"] <= max(6, int(io["requests"] * 0.25)):
            findings.append({
                "kind": "tool_hotspot", "severity": "low",
                "rung": TOOL_TO_RUNG.get(t["tool"], 6),
                "evidence": f"{t['tool']} 占 {share*100:.0f}% 消耗（{_fmt_tk(t['tt'])} token / {t['n']} 次）",
                "advice": "单次重、次数少的工具调用最值得优化：合并批次、减小单次载荷",
            })
            break

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), -f.get("rung", 0)))

    return {
        "since": since,
        "window_tokens": total_tk,
        "io": io,
        "tools": tools,
        "repeats": repeats,
        "top_input": top_in,
        "top_output": top_out,
        "findings": findings,
        "ladder": LADDER,
        "never_cut": NEVER_CUT,
        "honesty": "只报可数的实测事实（重复次数 / 关联消耗 / 异常大请求），"
                   "不估算『省下的』token —— 未写出的版本不存在，没有基线可减。",
    }
