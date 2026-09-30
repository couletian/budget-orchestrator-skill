#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Budget Orchestrator MCP Server (stdio)。

以 MCP 标准（换行分隔的 JSON-RPC 2.0）暴露预算控制面，供 WorkBuddy agent 直接调用。
注意：本进程的 stdout 是协议通道，任何调试输出必须走 stderr。

注册（~/.workbuddy/mcp.json）：
  "budget-orchestrator": {
    "command": "<python.exe>",
    "args": ["<skill目录>/mcp_server.py"],
    "env": { "BOC_DATA_DIR": "<数据目录>" }
  }
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core.ledger import Ledger          # noqa: E402
from core.budget import BudgetManager   # noqa: E402
from core import lean                   # noqa: E402
from core import autopilot              # noqa: E402
from core import plan                    # noqa: E402

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("BOC_DATA_DIR") or os.path.join(HOME, ".workbuddy", "budget-orchestrator")
DB = os.path.join(DATA_DIR, "ledger.db")
PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "boc_status",
        "description": "查看当前预算与用量状态：已用/剩余 token 与金额、百分比、按模型与执行体分布、"
                       "消耗速率与预计触顶时间、任务树、最近事件。",
        "inputSchema": {"type": "object", "properties": {
            "detail": {"type": "boolean", "description": "true 返回完整 JSON，false 只返回摘要"}},
            "required": []},
    },
    {
        "name": "boc_plan",
        "description": "【开工前必做】规划校验：确认「这个预算装不装得下这些子任务」。"
                       "返回①子任务额度合计是否超总额度 ②主干额度占比是否 ≤70% "
                       "③未标额度的任务 ④**按优先级排序的取舍预案**（预算不够时该牺牲谁、砍多少）。"
                       "先声明子任务、并用 AskUserQuestion 与用户确认优先级，再调用本工具。",
        "inputSchema": {"type": "object", "properties": {
            "json": {"type": "boolean", "description": "true 返回结构化 JSON"}}, "required": []},
    },
    {
        "name": "boc_autopilot",
        "description": "【首选，用于自主协调】返回当期应执行的调整指令：继续推进 / 降级后继续 / 需人工。"
                       "指令含可执行动作（爬 YAGNI 阶梯、压缩上下文、换更省的模型、跳过非关键子任务、"
                       "减少评审轮次等），AI 据此**自行调整任务并继续**，无需人工逐次批准。"
                       "建议每完成一个子任务、或每 2-3 次工具调用后调用一次。",
        "inputSchema": {"type": "object", "properties": {
            "json": {"type": "boolean", "description": "true 返回结构化 JSON"}}, "required": []},
    },
    {
        "name": "boc_check",
        "description": "轻量预算判定，返回 GO / DEGRADE / STOP 三态，供 agent 在每一步前自检。"
                       "STOP = 已暂停或超预算，应立即停止新增消耗；DEGRADE = 接近上限，应执行降级。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "boc_set_budget",
        "description": "设定任务的 token 预算与/或金额预算（人民币），并可设置软/硬阈值百分比。",
        "inputSchema": {"type": "object", "properties": {
            "tokens": {"type": "number", "description": "token 预算总额"},
            "cny": {"type": "number", "description": "金额预算（元）"},
            "soft_pct": {"type": "number", "description": "软阈值百分比，默认 70"},
            "hard_pct": {"type": "number", "description": "硬阈值百分比，默认 100"}},
            "required": []},
    },
    {
        "name": "boc_add_budget",
        "description": "在现有预算上追加 token/金额额度（用户批准超支后使用），追加后若处于暂停态会自动恢复。",
        "inputSchema": {"type": "object", "properties": {
            "tokens": {"type": "number"}, "cny": {"type": "number"}}, "required": []},
    },
    {
        "name": "boc_control",
        "description": "任务中断控制：pause 暂停 / resume 恢复 / stop 终止。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pause", "resume", "stop"]},
            "reason": {"type": "string"}}, "required": ["action"]},
    },
    {
        "name": "boc_mode",
        "description": "切换运行模式。observe=只监督告警；enforce=触顶时自动暂停。",
        "inputSchema": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["observe", "enforce"]}}, "required": ["mode"]},
    },
    {
        "name": "boc_task",
        "description": "管理任务树与子任务预算：add 新增 / use 设为活跃（后续消耗归入该任务）/ "
                       "done 完成 / list 列表 / rm 删除 / prio 改优先级。"
                       "注意：add 时**必须**带 cny（或 tokens）——漏给额度的子任务会进 plan 的 "
                       "unbudgeted 清单并判定 tight，预算不够时无法参与取舍计算。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "use", "done", "list", "rm", "prio"]},
            "id": {"type": "string"}, "name": {"type": "string"},
            "parent": {"type": "string", "description": "父任务 id，默认 root"},
            "tokens": {"type": "number"}, "cny": {"type": "number",
                                                  "description": "该子任务的金额额度，强烈建议都填上"},
            "priority": {"type": "number",
                         "description": "1-5，≥4 视为主干（预算告急时优先保住）；默认 3"}},
            "required": ["action"]},
    },
    {
        "name": "boc_lean",
        "description": "精简审计（融合 ponytail 的 YAGNI 阶梯与输出纪律）：对真实消耗给出可压缩项——"
                       "返工/重复操作、上下文膨胀、输出冗长、工具热点，并给出当前瘦身档位与纪律。"
                       "只报可数的实测事实，不虚构『省下的』token。预算吃紧或需要降耗时调用。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "boc_panel",
        "description": "返回可视化面板地址（实时展示任务进度、token 与金额消耗、手动打断入口）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def _ledger():
    os.makedirs(DATA_DIR, exist_ok=True)
    return Ledger(DB)


def _fmt_tk(n):
    n = float(n or 0)
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.2f}万"
    return str(int(n))


def _fmt_cny(n):
    n = float(n or 0)
    return f"¥{n/1e4:.2f}万" if n >= 1e4 else f"¥{n:.4f}"


def call_tool(name: str, args: dict) -> str:
    lg = _ledger()
    bm = BudgetManager(lg)

    if name == "boc_status":
        snap = bm.snapshot()
        if args.get("detail"):
            return json.dumps(snap, ensure_ascii=False, indent=2)
        b, u = snap["budget"], snap["used"]
        lines = [
            f"状态: {snap['state']}  等级: {snap['level']}  模式: {snap['mode']}",
            f"已用: {_fmt_tk(u['tokens'])} token / {_fmt_cny(u['cny'])}  ({u['requests']} 次请求)",
            f"预算: {_fmt_tk(b['tokens']) if b['tokens'] else '未设'} token / "
            f"{_fmt_cny(b['cny']) if b['cny'] else '未设'}",
            f"已用占比: {snap['pct']['overall']:.1f}%（token {snap['pct']['tokens']:.1f}% / 金额 {snap['pct']['cny']:.1f}%）",
        ]
        r = snap["rate"]
        if r["tokens_per_min"]:
            eta = f"，预计 {r['eta_min']:.0f} 分钟后触顶" if r["eta_min"] is not None else ""
            lines.append(f"速率: {_fmt_tk(r['tokens_per_min'])} token/分钟{eta}")
        if snap["models"]:
            lines.append("按模型: " + "; ".join(
                f"{m['model']} {_fmt_tk(m['tt'])}/{_fmt_cny(m['cost'])}" for m in snap["models"][:6]))
        if len(snap["tasks"]) > 1:
            lines.append("任务树: " + "; ".join(
                f"{t['id']}({t.get('status')}) {_fmt_tk(t.get('used_tokens',0))}" for t in snap["tasks"]))
        if snap["degrade_hint"]["needed"]:
            lines.append("降级建议: " + ", ".join(s["action"] for s in snap["degrade_hint"]["steps"]))
        return "\n".join(lines)

    if name == "boc_plan":
        r = plan.audit(lg)
        if args.get("json"):
            return json.dumps(r, ensure_ascii=False, indent=2)
        return plan.plan_text(r) + "\n  → " + plan.next_action(r)

    if name == "boc_autopilot":
        d = bm.autopilot()
        return json.dumps(d, ensure_ascii=False, indent=2) if args.get("json") \
            else autopilot.autopilot_text(d)

    if name == "boc_check":
        return json.dumps(bm.check(), ensure_ascii=False, indent=2)

    if name == "boc_set_budget":
        bm.set_budget(args.get("tokens"), args.get("cny"))
        if args.get("soft_pct") is not None or args.get("hard_pct") is not None:
            bm.control("set_threshold", soft_pct=args.get("soft_pct"), hard_pct=args.get("hard_pct"))
        c = bm.config()
        return f"预算已设为 {_fmt_tk(c['budget_tokens'])} token / {_fmt_cny(c['budget_cny'])}，" \
               f"软 {c['soft_pct']}% 硬 {c['hard_pct']}%"

    if name == "boc_add_budget":
        bm.add_budget(args.get("tokens"), args.get("cny"))
        c = bm.config()
        return f"已追加。当前预算 {_fmt_tk(c['budget_tokens'])} token / {_fmt_cny(c['budget_cny'])}，" \
               f"状态 {c['state']}"

    if name == "boc_control":
        res = bm.control(args.get("action", ""), reason=args.get("reason"))
        return json.dumps(res, ensure_ascii=False)

    if name == "boc_mode":
        return json.dumps(bm.control("set_mode", mode=args.get("mode")), ensure_ascii=False)

    if name == "boc_task":
        act = args.get("action")
        if act == "add":
            lg.upsert_task({"id": args["id"], "parent": args.get("parent", "root"),
                            "name": args.get("name", args["id"]),
                            "budget_tokens": args.get("tokens"), "budget_cny": args.get("cny"),
                            "priority": args.get("priority", 3), "status": "pending"})
            lg.add_event("task_add", f"{args['id']} {args.get('name','')}")
            return f"已添加任务 {args['id']}（优先级 {args.get('priority', 3)}）"
        if act == "prio":
            t = lg.get_task(args["id"]) or {}
            t["priority"] = args.get("priority", t.get("priority", 3))
            lg.upsert_task(t)
            return f"任务 {args['id']} 优先级 → {t['priority']}"
        if act == "use":
            bm.control("set_active_task", task_id=args["id"])
            return f"活跃任务 → {args['id']}"
        if act == "done":
            t = lg.get_task(args["id"]) or {}
            lg.upsert_task({**t, "id": args["id"], "status": "done", "progress": 1.0})
            return f"任务 {args['id']} 已完成"
        if act == "rm":
            lg.delete_task(args["id"])
            return f"已删除 {args['id']}"
        rows = []
        for t in lg.list_tasks():
            agg = lg.totals(t["id"])
            rows.append(f"[{t.get('status')}] {t['id']} {t.get('name','')} "
                        f"{_fmt_tk(agg['tt'])} {_fmt_cny(agg['cost'])}")
        return "\n".join(rows) or "（无任务）"

    if name == "boc_lean":
        rep = lean.audit(lg)
        snap = bm.snapshot(series_minutes=5, events_limit=1)
        lv = snap["lean"]
        out = [f"瘦身档位: {lv['name']}（{lv['level']}）  预算已用 {snap['pct']['overall']:.1f}%",
               "当前纪律: " + "；".join(lv["rules"])]
        fs = rep["findings"]
        if fs:
            out.append(f"可压缩项 {len(fs)} 条：")
            for f in fs:
                out.append(f"  [{f['severity']}] 阶梯第{f['rung']}级 · {f['kind']}")
                out.append(f"      证据: {f['evidence']}")
                out.append(f"      建议: {f['advice']}")
        else:
            out.append("未发现明显可压缩项。")
        out.append("无论多紧都不能省: " + "、".join(rep["never_cut"]))
        out.append("口径: " + rep["honesty"])
        return "\n".join(out)

    if name == "boc_panel":
        port = None
        try:
            with open(os.path.join(DATA_DIR, "panel.port")) as fh:
                port = fh.read().strip()
        except Exception:
            pass
        if port:
            return f"面板地址: http://127.0.0.1:{port}/  （若未启动，运行: python bo.py daemon start）"
        return "面板未启动。运行: python bo.py daemon start --tokens <预算> 后访问输出中的地址。"

    raise ValueError(f"unknown tool: {name}")


# ── JSON-RPC 循环 ────────────────────────────────────────────────────────────
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        mid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "budget-orchestrator", "version": "1.0.0"}}})
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                nm = params.get("name")
                a = params.get("arguments") or {}
                try:
                    txt = call_tool(nm, a)
                    send({"jsonrpc": "2.0", "id": mid,
                          "result": {"content": [{"type": "text", "text": txt}], "isError": False}})
                except Exception as e:
                    send({"jsonrpc": "2.0", "id": mid,
                          "result": {"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                if mid is not None:
                    send({"jsonrpc": "2.0", "id": mid,
                          "error": {"code": -32601, "message": f"method not found: {method}"}})
        except Exception as e:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
