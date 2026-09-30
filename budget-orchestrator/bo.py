# -*- coding: utf-8 -*-
"""Budget Orchestrator Console (BOC) —— 统一命令行入口。

用法速览
  bo.py init                                  初始化账本
  bo.py run  [--tokens N] [--cny M]           前台运行（监听 + 面板）
  bo.py daemon start|stop|status              后台运行（监听 + 面板）
  bo.py status [--json]                       查看预算/用量状态
  bo.py check  [--json]                       轻量判定（送 agent / hook）
  bo.py budget --tokens N --cny M             设置预算
  bo.py budget --add-tokens N --add-cny M     追加预算
  bo.py control pause|resume|stop             控制
  bo.py mode observe|enforce                  监督模式 / 强制执行模式
  bo.py task add|use|done|list|rm|rm-all      任务树
  bo.py task --cny M --priority N add T1 --name "..."   子任务额度与优先级（额度旗标写在动作词之前）
  bo.py reset [--keep-budget]                 账本归零（停 daemon + 备份 ledger.db 三件套）
  bo.py report --out FILE                     导出报告
  bo.py url                                   打印面板地址
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import signal
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core.budget import BudgetManager     # noqa: E402
from core.ledger import Ledger            # noqa: E402
from core.panel import serve              # noqa: E402
from core.watcher import Watcher          # noqa: E402
from core import pricing                  # noqa: E402
from core import lean                     # noqa: E402
from core import autopilot                # noqa: E402
from core import plan                     # noqa: E402

HOME = os.path.expanduser("~")
DEFAULT_DATA = os.path.join(HOME, ".workbuddy", "budget-orchestrator")
DEFAULT_PROJECTS = os.path.join(HOME, ".workbuddy", "projects")


def workspace_slug(path: str) -> str:
    s = os.path.abspath(path)
    s = s.replace(":", "").replace("\\", "-").replace("/", "-")
    return s[0].lower() + s[1:] if s else s


def fmt_tk(n) -> str:
    n = float(n or 0)
    if n >= 1e8:
        return f"{n/1e8:.2f} 亿"
    if n >= 1e4:
        return f"{n/1e4:.2f} 万"
    return str(int(n))


def fmt_cny(n) -> str:
    n = float(n or 0)
    if n >= 1e4:
        return f"¥{n/1e4:.2f} 万"
    return f"¥{n:.4f}"


def _paths(args):
    data = os.path.abspath(args.data_dir or DEFAULT_DATA)
    return data, os.path.join(data, "ledger.db")


def _ledger(args) -> Ledger:
    data, db = _paths(args)
    os.makedirs(data, exist_ok=True)
    return Ledger(db)


def _resolve_since(lg, since_arg):
    """统计起点解析。

    since_arg: None 沿用已存起点（无则取当前时刻）；'all' 全量回填；'now' 从此刻起；数字=近 N 秒。
    注意 '0'（all）是**有效值**，不能按“空值”重置。
    """
    if since_arg is not None:
        s = str(since_arg).strip().lower()
        if s == "all":
            val = 0.0
        elif s == "now":
            val = time.time()
        else:
            val = time.time() - float(s)
        lg.set_meta("since_ts", val)
        return val
    v = lg.get_meta("since_ts", None)
    if v is None or str(v).strip() == "":
        val = time.time()
        lg.set_meta("since_ts", val)
        return val
    return float(v)


def _resolve_workspace(args):
    if getattr(args, "all", False):
        return None
    ws = getattr(args, "workspace", None)
    if not ws or ws == "auto":
        return workspace_slug(os.getcwd())
    return ws


# ── 命令实现 ────────────────────────────────────────────────────────────────
def cmd_init(args):
    lg = _ledger(args)
    lg.set_meta("workspace", _resolve_workspace(args) or "*")
    print(f"账本已就绪: {lg.path}")
    print(f"工作区: {lg.get_meta('workspace')}")
    return 0


def cmd_status(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    snap = bm.snapshot()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    b = snap["budget"]
    u = snap["used"]
    print("─" * 62)
    print(f"  状态 {snap['state']}   等级 {snap['level']}   模式 {snap['mode']}")
    print(f"  工作区 {snap['workspace']}")
    print("─" * 62)
    print(f"  已用 Token   {fmt_tk(u['tokens']):>14}   请求 {u['requests']}")
    print(f"  已用金额     {fmt_cny(u['cny']):>14}")
    pr = snap.get("pricing") or {}
    if not pr.get("all_priced", True):
        names = "、".join(x["model"] for x in pr["unpriced"])
        print(f"  ⚠ 牌价未知   {names}")
        print(f"    └ 上述模型的 ¥{pr['est_cny']:.4f} 是按兜底档**假设**的，不是实际计费；"
              f"补价见 core/pricing.py 的 PRICE")
    if b["tokens"] > 0:
        print(f"  预算 Token   {fmt_tk(b['tokens']):>14}   剩余 {fmt_tk(snap['remaining']['tokens'])}"
              f"   已用 {snap['pct']['tokens']:.1f}%")
    if b["cny"] > 0:
        print(f"  预算金额     {fmt_cny(b['cny']):>14}   剩余 {fmt_cny(snap['remaining']['cny'])}"
              f"   已用 {snap['pct']['cny']:.1f}%")
    r = snap["rate"]
    if r["tokens_per_min"]:
        eta = ""
        if r["eta_min"] is not None:
            eta = f"   预计 {r['eta_min']:.0f} 分钟后触顶"
        print(f"  速率         {fmt_tk(r['tokens_per_min'])}/分钟  {fmt_cny(r['cny_per_min'])}/分钟{eta}")
    if snap["models"]:
        print("─" * 62)
        print("  按模型：")
        for m in snap["models"][:8]:
            print(f"    {m['model']:<26} {fmt_tk(m['tt']):>12}  {fmt_cny(m['cost']):>12}  ×{m['n']}")
    if len(snap["tasks"]) > 1:
        print("─" * 62)
        print("  任务树：")
        for t in snap["tasks"]:
            pct = f"{t['pct']:.1f}%" if t.get("pct") is not None else "-"
            print(f"    [{(t.get('status') or '?'):<9}] {t['id']:<12} {t.get('name',''):<20}"
                  f" {fmt_tk(t.get('used_tokens',0)):>12}  预算 {pct}")
    if snap["degrade_hint"]["needed"]:
        print("─" * 62)
        print("  降级建议：")
        for s in snap["degrade_hint"]["steps"]:
            print(f"    · {s['action']:<22} {s['why']}")
    w = snap.get("watch") or {}
    if w:
        lag = w.get("lag_bytes", 0)
        # lag=0 → 日志已读到底，面板没变化是宿主还没落盘（不是本工具滞后）
        verdict = "已读到日志末尾" if lag <= 0 else f"尚有 {fmt_tk(lag)} 字节待读"
        last = (time.strftime('%H:%M:%S', time.localtime(w["newest_mtime"]))
                if w.get("newest_mtime") else "无")
        print("─" * 62)
        print(f"  监听          {w.get('files', 0)} 个日志文件   {verdict}")
        print(f"  日志最后写入   {last}   （面板没变化时先看这里）")
    print("─" * 62)
    return 0


def cmd_check(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    res = bm.check()
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"verdict={res['verdict']}  state={res['state']}  level={res['level']}  "
              f"pct={res['pct']['overall']:.1f}%")
        if res.get("misaligned"):
            p = res["pct"]
            b = res.get("binding", "tokens")
            me = "token" if b == "tokens" else "金额"
            other = "金额" if b == "tokens" else "token"
            ov = p["cny"] if b == "tokens" else p["tokens"]
            print(f"  [口径提示] 顶爆预算的是 {me} 口径（另一口径 {other} 仅 {ov:.1f}%）—— "
                  f"通常是 {me} 上限标定过低，而不是真的超支。"
                  f"用 `bo.py budget --tokens N --cny M` 把两个上限校准到同一口径再判断。")
        if res["degrade_hint"]["needed"]:
            print("  建议：" + ", ".join(s["action"] for s in res["degrade_hint"]["steps"]))
    # 退出码：0=GO 3=DEGRADE 4=STOP（供 hook / shell 判定）
    return {"GO": 0, "DEGRADE": 3, "STOP": 4}.get(res["verdict"], 0)


def cmd_budget(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    if args.add_tokens is not None or args.add_cny is not None:
        bm.add_budget(args.add_tokens, args.add_cny)
    else:
        bm.set_budget(args.tokens, args.cny)
        # 设置预算 = 开启一个「新账期」：统计起点推到此刻。
        # 否则上一轮任务遗留的 since_ts 会把历史消耗算进本次预算 ——
        # 实测踩过：新任务一开工就显示「已用 ¥0.32 / 521 万 token」（全是历史）。
        if not getattr(args, "keep_since", False):
            lg.set_meta("since_ts", time.time())
            print("统计起点已重置为此刻（设置预算 = 新账期；如需保留原起点请加 --keep-since）")
    if args.soft is not None or args.hard is not None:
        bm.control("set_threshold", soft_pct=args.soft, hard_pct=args.hard)
    c = bm.config()
    print(f"预算 tokens={fmt_tk(c['budget_tokens'])} cny={fmt_cny(c['budget_cny'])} "
          f"soft={c['soft_pct']}% hard={c['hard_pct']}%")
    return 0


def cmd_control(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    # 允许 "action" 传入 name 或位置参数
    action = args.action
    res = bm.control(action, reason=args.reason)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_mode(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    res = bm.control("set_mode", mode=args.mode)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _warn_duplicate_flags(raw, flags=("--cny", "--tokens", "--priority", "--name")):
    """argparse 对重复旗标是「后者静默覆盖前者」——这里把它显式喊出来。

    实测：`task --cny 0.045 add T1 --cny 0.02` 最终只记 0.02，前面的值无声消失。
    """
    dups = []
    for f in flags:
        n = sum(1 for a in raw if a == f or a.startswith(f + "="))
        if n > 1:
            dups.append((f, n))
    for f, n in dups:
        print(f"[warn] {f} 出现 {n} 次，只有最后一个生效（argparse 后者覆盖前者）", file=sys.stderr)
    return dups


def cmd_task(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    _warn_duplicate_flags(getattr(args, "_argv_raw", []))
    if args.task_action == "add":
        lg.upsert_task({
            "id": args.id, "parent": args.parent, "name": args.name or args.id,
            "budget_tokens": args.tokens, "budget_cny": args.cny,
            "status": "pending", "progress": 0,
            "priority": args.priority if args.priority is not None else 3,
        })
        lg.add_event("task_add", f"{args.id} {args.name or ''}")
        print(f"已添加任务 {args.id}（优先级 {args.priority if args.priority is not None else 3}）")
        if args.cny is None and args.tokens is None:
            # 没记额度 → plan 会把它列进 unbudgeted，预算不够时无法参与取舍计算。
            # 这是实测中被踩过的坑，所以必须显式提醒，而不是安静通过。
            print("[warn] 未给 --cny / --tokens：该子任务记不上额度，"
                  "plan 会报「未标额度」且无法参与取舍计算。", file=sys.stderr)
            print("       补记写法： bo.py task --cny 0.045 add %s --name \"...\"" % (args.id or "T1"),
                  file=sys.stderr)
        else:
            print("       额度：" + " ".join(
                x for x in (f"--cny {args.cny}" if args.cny is not None else "",
                            f"--tokens {args.tokens}" if args.tokens is not None else "") if x))
    elif args.task_action == "prio":
        t = lg.get_task(args.id)
        if not t:
            print(f"任务不存在: {args.id}")
            return 1
        t["priority"] = args.priority if args.priority is not None else t.get("priority", 3)
        lg.upsert_task(t)
        lg.add_event("task_prio", f"{args.id} → 优先级 {t['priority']}")
        print(f"任务 {args.id} 优先级 → {t['priority']}")
    elif args.task_action == "use":
        bm.control("set_active_task", task_id=args.id)
        print(f"活跃任务 → {args.id}")
    elif args.task_action == "done":
        t = lg.get_task(args.id) or {}
        lg.upsert_task({**t, "id": args.id, "status": "done", "progress": 1.0})
        print(f"任务 {args.id} 标记完成")
    elif args.task_action == "list":
        for t in lg.list_tasks():
            agg = lg.totals(t["id"])
            star = "★主干" if int(t.get("priority") or 3) >= 4 else f"P{t.get('priority', 3)}"
            print(f"[{t.get('status',''):<9}] {t['id']:<14} {t.get('name',''):<24} "
                  f"{star:<8} {fmt_tk(agg['tt']):>10}  {fmt_cny(agg['cost']):>10}")
    elif args.task_action == "rm":
        lg.delete_task(args.id)
        print(f"已删除 {args.id}")
    elif args.task_action == "rm-all":
        n = 0
        for t in lg.list_tasks():
            if t["kind"] != "root":
                lg.delete_task(t["id"])
                n += 1
        print(f"已清空 {n} 个任务")
    return 0


def cmd_lean(args):
    """精简审计：把 ponytail 的 YAGNI 阶梯与输出纪律，对着真实消耗给出建议。"""
    lg = _ledger(args)
    bm = BudgetManager(lg)
    rep = lean.audit(lg)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    snap = bm.snapshot(series_minutes=5, events_limit=1)
    lv = snap["lean"]
    io = rep["io"]
    print("─" * 66)
    print(f"  瘦身档位：{lv['name']}（{lv['level']}）   预算已用 {snap['pct']['overall']:.1f}%")
    print(f"  窗口消耗：{fmt_tk(rep['window_tokens'])} token   "
          f"输入 {fmt_tk(io['input'])} / 输出 {fmt_tk(io['output'])} "
          f"（输出占输入 {io['out_per_in']:.1f}%）")
    print("─" * 66)
    if lv["rules"]:
        print("  当前纪律：")
        for r in lv["rules"]:
            print(f"    · {r}")

    fs = rep["findings"]
    print("─" * 66)
    if not fs:
        print("  未发现明显可压缩项。")
    else:
        print(f"  可压缩项（{len(fs)} 条，按优先级）：")
        tag = {"high": "高", "medium": "中", "low": "低"}
        for f in fs:
            print(f"\n    [{tag.get(f['severity'],'?')}] 阶梯第 {f['rung']} 级 · {f['kind']}")
            print(f"        证据：{f['evidence']}")
            print(f"        建议：{f['advice']}")

    tools = rep["tools"][:6]
    if tools:
        mx = max(t["tt"] for t in tools) or 1
        print("─" * 66)
        print("  工具消耗分布：")
        for t in tools:
            bar = "█" * max(1, int(t["tt"] / mx * 18))
            print(f"    {t['tool']:<14} {bar:<18} {fmt_tk(t['tt']):>10}  ×{t['n']}")

    print("─" * 66)
    print("  YAGNI 阶梯（停在第一个成立的级别）：")
    for l in rep["ladder"]:
        print(f"    {l['rung']}. {l['ask']}  → {l['do']}")
    print("─" * 66)
    print("  无论多紧都不能省：")
    for n in rep["never_cut"]:
        print(f"    ! {n}")
    print("─" * 66)
    print(f"  口径：{rep['honesty']}")
    return 0


def cmd_plan(args):
    """规划校验：开工前确认「预算装不装得下这些子任务」。

    退出码：0=规划合理　3=偏紧（主干占比过高 / 有任务未标额度）　4=超额
    """
    lg = _ledger(args)
    r = plan.audit(lg)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(plan.plan_text(r))
        print(f"  → {plan.next_action(r)}")
    return {"ok": 0, "tight": 3, "over": 4, "nobudget": 3}.get(r["verdict"], 0)


def cmd_autopilot(args):
    """自主协调指令：AI 读它自行决定如何调整任务，不必停下来等人。

    退出码：0=proceed 继续　3=adapt 降级后继续　4=escalate 才需要人工
    """
    lg = _ledger(args)
    bm = BudgetManager(lg)
    d = bm.autopilot()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(autopilot.autopilot_text(d))
    return {"proceed": 0, "adapt": 3, "escalate": 4}.get(d["mode"], 0)


def cmd_report(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    snap = bm.snapshot(series_minutes=1440, events_limit=500)
    out = os.path.abspath(args.out or "budget-report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=2)
    print(f"报告已写出: {out}")
    return 0


def cmd_url(args):
    data, _ = _paths(args)
    port = _read_port(data) or args.port
    print(f"http://127.0.0.1:{port}/")
    return 0


def cmd_reset(args):
    """账本归零：先停守护进程，再把 ledger.db 三件套一起备份改名，然后重新起算。

    为什么必须连 `-shm` / `-wal` 一起搬
      SQLite 处于 WAL 模式，最新提交可能还在 `-wal` 里没落进 `ledger.db`。
      只搬主库会得到一本"半旧"的账，`-shm`/`-wal` 遗留还会让新库读到陈旧索引。
      三件套必须同进同出 —— 这就是"改名之后才敢说从零起算"的原因。

    为什么必须先停 daemon
      守护进程还在写时改名，Windows 会直接 PermissionError。
      这里选择**报错并中止**，而不是忽略失败继续——宁可不动手，也不留一本半搬的账。

    不想动文件的话，更轻的做法是 `run --since now`：
    只把统计起点推到此刻，历史仍在库里可查。
    """
    data, db = _paths(args)
    os.makedirs(data, exist_ok=True)

    # 0) 先停看守进程（respawn）：它会周期性地把面板重新拉起来，且自己也持有账本句柄。
    #    不先停掉，后面改名 ledger.db 必报 PermissionError（WinError 32）。
    wpidf = _watch_pid_file(data)
    wpid = _read_pid(wpidf)
    if wpid and _alive(wpidf):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(wpid), "/T", "/F"], capture_output=True)
            else:
                os.kill(wpid, signal.SIGTERM)
            time.sleep(0.5)
            print(f"已停止看守进程 pid={wpid}（respawn）")
        except Exception as e:
            print(f"[warn] 停止看守进程失败: {e}")
    try:
        os.remove(wpidf)
    except OSError:
        pass

    # 1) 停守护进程，避免改名时它还在写
    pidf = _pid_file(data)
    pid = _read_pid(pidf)
    if pid and _alive(pidf):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            time.sleep(0.8)
            print(f"已停止 daemon pid={pid}")
        except Exception as e:
            print(f"[warn] 停止 daemon 失败: {e}")
    elif pid:
        print("daemon 未在运行（pid 文件陈旧，已清理）")
    try:
        os.remove(pidf)
    except OSError:
        pass

    # 2) 可选：把预算与任务树带到新账本
    carry = None
    if getattr(args, "keep_budget", False) and os.path.exists(db):
        try:
            old = Ledger(db)
            c = BudgetManager(old).config()
            carry = {
                "budget_tokens": c["budget_tokens"], "budget_cny": c["budget_cny"],
                "soft_pct": c["soft_pct"], "hard_pct": c["hard_pct"],
                "reserve_pct": c["reserve_pct"], "mode": c["mode"],
                "tasks": [t for t in old.list_tasks() if t.get("kind") != "root"],
            }
            old.close()
            print(f"将保留预算与 {len(carry['tasks'])} 个子任务")
        except Exception as e:
            print(f"[warn] 读取旧预算失败，改为只重置账本: {e}", file=sys.stderr)

    # 3) 三件套一起改名到 backup/
    #    命名保持 .db 后缀，方便回滚时一眼认出、直接改回 ledger.db
    #
    #    改名之前先强制收一次垃圾：sqlite3.Connection 参与引用环，仅靠引用计数
    #    回收不掉（见 core/ledger.py:close 的说明）。CLI 每次是新进程所以很少中招，
    #    但**同一进程内二次调用**（测试用 in-process main()、宿主内嵌调用）时，
    #    前一次命令留下的连接会一直占着 ledger.db，os.replace 必报
    #    `WinError 32 另一个程序正在使用此文件`，reset 直接返回 1 中止。
    #    实测：加这一行前 4 个 reset 测试稳定失败，加了以后全绿。
    gc.collect()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(data, "backup")
    os.makedirs(bdir, exist_ok=True)
    ext_suffix = {"": ".db", "-shm": ".db-shm", "-wal": ".db-wal"}
    moved = []
    for ext in ("", "-shm", "-wal"):
        src = db + ext
        if not os.path.exists(src):
            continue
        dst = os.path.join(bdir, f"ledger-{stamp}{ext_suffix[ext]}")
        # 刚被 taskkill 的守护进程句柄可能还没被系统回收，给几次机会
        last = None
        for attempt in range(4):
            try:
                os.replace(src, dst)
                last = None
                break
            except OSError as e:
                last = e
                time.sleep(0.3)
        if last is not None:
            print(f"[error] 备份失败，已中止（未做任何删改）: {src} → {last}", file=sys.stderr)
            print("        请确认没有其他进程占用账本：", file=sys.stderr)
            print(f"          · 停止看守进程（respawn 启动的）：看 {_watch_pid_file(data)}，"
                  f"或直接从宿主取消那个后台任务", file=sys.stderr)
            print("          · 停止守护进程：bo.py daemon stop", file=sys.stderr)
            print("        然后重试本命令（本命令已在开头自动尝试停掉这两者）。", file=sys.stderr)
            return 1
        moved.append(dst)

    if not moved:
        print("没有找到账本文件，直接新建。")
    else:
        for dst in moved:
            print(f"已备份: {os.path.basename(dst)}")

    # 4) 建新账本，起算点设在此刻
    lg = Ledger(db)
    bm = BudgetManager(lg)
    lg.set_meta("since_ts", time.time())
    lg.set_meta("last_level", "ok")
    if carry:
        lg.set_meta("budget_tokens", carry["budget_tokens"])
        lg.set_meta("budget_cny", carry["budget_cny"])
        lg.set_meta("soft_pct", carry["soft_pct"])
        lg.set_meta("hard_pct", carry["hard_pct"])
        lg.set_meta("reserve_pct", carry["reserve_pct"])
        lg.set_meta("mode", carry["mode"])
        for t in carry["tasks"]:
            lg.upsert_task({
                "id": t["id"], "parent": t.get("parent") or "root", "name": t.get("name"),
                "kind": t.get("kind") or "task", "budget_tokens": t.get("budget_tokens"),
                "budget_cny": t.get("budget_cny"), "status": t.get("status") or "pending",
                "progress": t.get("progress") or 0, "note": t.get("note"),
                "priority": t.get("priority"),
            })
    _set_panel_disabled(data, False)   # 重置后应可正常唤起面板
    lg.add_event("reset", f"账本归零（备份于 {bdir}）", level="warn")

    snap = bm.snapshot(series_minutes=5, events_limit=1)
    print(f"新账本: {db}")
    print(f"备份目录: {bdir}")
    print(f"当前用量: {fmt_tk(snap['used']['tokens'])} token / {fmt_cny(snap['used']['cny'])}"
          f" / {snap['used']['requests']} 次请求（应为 0）")
    # 主动关连接：WAL 归并、句柄释放，此后文件可被安全移动/删除
    lg.close()
    print("下一步: bo.py daemon start   （或 respawn 以带看守进程）")
    return 0


# ── 运行（watcher + panel）──────────────────────────────────────────────────
def _port_file(data):
    return os.path.join(data, "panel.port")


def _read_port(data):
    try:
        with open(_port_file(data)) as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _port_open(port, host="127.0.0.1"):
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _pid_file(data):
    return os.path.join(data, "daemon.pid")


def _watch_pid_file(data):
    """看守进程（respawn）的 pid 文件。

    存在的意义：reset 必须先把看守进程停掉 —— 它既会周期性重拉面板，
    自己又持有账本句柄；不先停就会出现「taskkill 完面板立刻被拉起来，
    接着改名 ledger.db 报 WinError 32」。（实测踩过。）
    """
    return os.path.join(data, "watch.pid")


def _recorded_workspace(data):
    """读账本里守护进程记录的工作区 slug。读不到返回 None。"""
    try:
        lg = Ledger(os.path.join(data, "ledger.db"))
        v = lg.get_meta("workspace", None)
        lg.close()
        if v and str(v).strip() not in ("", "*", "None"):
            return str(v)
    except Exception:
        return None
    return None


def _disabled_file(data):
    """手动关闭面板的标记。存在即表示「用户主动关的，看守进程不要重拉」。"""
    return os.path.join(data, "panel.disabled")


def _panel_disabled(data):
    return os.path.exists(_disabled_file(data))


def _set_panel_disabled(data, flag=True):
    f = _disabled_file(data)
    try:
        if flag:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            os.remove(f)
    except OSError:
        pass


def _do_run(args):
    lg = _ledger(args)
    bm = BudgetManager(lg)
    ws = _resolve_workspace(args)
    projects = os.path.abspath(args.projects or DEFAULT_PROJECTS)
    if not os.path.isdir(projects):
        print(f"[warn] projects 目录不存在: {projects}", file=sys.stderr)

    data_dir = os.path.dirname(lg.path)
    # 手动关闭标记还在 → 尊重用户意志，不启动面板（看守进程据此不再重拉）
    if _panel_disabled(data_dir) and not getattr(args, "force", False):
        print(f"[BOC] 面板已被手动关闭（{_disabled_file(data_dir)}）")
        print("[BOC] 重新唤起请执行:  bo.py daemon start  或  bo.py respawn")
        return 0
    # 显式启动 → 清除标记，保证「手动关闭后仍可正常唤起」
    _set_panel_disabled(data_dir, False)

    lg.set_meta("workspace", ws or "*")
    if args.tokens is not None or args.cny is not None:
        bm.set_budget(args.tokens, args.cny)
    if getattr(args, "soft", None) is not None or getattr(args, "hard", None) is not None:
        bm.control("set_threshold", soft_pct=getattr(args, "soft", None),
                   hard_pct=getattr(args, "hard", None))
    since_ts = _resolve_since(lg, args.since)

    def on_batch(rows):
        bm.evaluate()

    w = Watcher(lg, projects, workspace=ws, since_ts=since_ts,
                interval=args.interval, on_batch=on_batch, backfill=(args.since == "all"))
    lg.add_event("start", f"监听启动 workspace={ws or '*'} since={time.strftime('%H:%M:%S', time.localtime(since_ts))}")

    srv = serve(lg, host=args.host, port=args.port, block=False)
    actual_port = srv.server_address[1]
    with open(_port_file(os.path.dirname(lg.path)), "w") as fh:
        fh.write(str(actual_port))
    lg.add_event("panel", f"面板启动 http://127.0.0.1:{actual_port}/")

    print(f"[BOC] 面板: http://127.0.0.1:{actual_port}/")
    print(f"[BOC] 账本: {lg.path}")
    print(f"[BOC] 监听: {projects}" + (f" / {ws}" if ws else " / 全部工作区"))
    print(f"[BOC] 统计起点: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since_ts))}")
    print(f"[BOC] >>> 看板地址 http://127.0.0.1:{actual_port}/ <<<")
    sys.stdout.flush()
    if getattr(args, "open", False):
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{actual_port}/")
        except Exception:
            pass

    w.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[BOC] 收工")
    finally:
        w.stop()
    return 0


def cmd_run(args):
    return _do_run(args)


# ── daemon ─────────────────────────────────────────────────────────────────
def cmd_daemon(args):
    data, _ = _paths(args)
    os.makedirs(data, exist_ok=True)
    pidf, logf = _pid_file(data), os.path.join(data, "daemon.log")

    if args.daemon_action == "start":
        _set_panel_disabled(data, False)   # 显式唤起 → 清标记
        if _alive(pidf):
            print(f"daemon 已在运行 pid={_read_pid(pidf)}")
            return 0
        cmd = [sys.executable, os.path.join(HERE, "bo.py"), "run",
               "--data-dir", data, "--projects", os.path.abspath(args.projects or DEFAULT_PROJECTS),
               "--workspace", args.workspace, "--port", str(args.port), "--host", args.host]
        if args.tokens is not None:
            cmd += ["--tokens", str(args.tokens)]
        if args.cny is not None:
            cmd += ["--cny", str(args.cny)]
        if args.since:
            cmd += ["--since", args.since]
        if args.soft is not None:
            cmd += ["--soft", str(args.soft)]
        if args.hard is not None:
            cmd += ["--hard", str(args.hard)]
        if getattr(args, "open", False):
            cmd += ["--open"]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(logf, "ab") as lf:
            p = subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                 creationflags=flags, close_fds=True)
        with open(pidf, "w") as fh:
            fh.write(str(p.pid))
        # 轮询等待面板端口出现，确认守护真正起来了再返回
        port = None
        for _ in range(20):
            time.sleep(0.4)
            port = _read_port(data)
            if port and _port_open(port):
                break
            if p.poll() is not None:      # 子进程已退出
                print(f"daemon 启动失败（退出码 {p.returncode}），请查看日志: {logf}")
                return 1
        if port and _port_open(port):
            print(f"daemon 已启动 pid={p.pid}")
            print(f"面板: http://127.0.0.1:{port}/")
        else:
            print(f"daemon 进程已启动 pid={p.pid}，但面板端口暂未就绪，稍后再试或查看日志: {logf}")
        print(f"日志: {logf}")
        return 0

    if args.daemon_action == "stop":
        pid = _read_pid(pidf)
        if not pid:
            print("daemon 未运行")
            return 0
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"已停止 pid={pid}")
        except Exception as e:
            print(f"停止失败: {e}")
        try:
            os.remove(pidf)
        except OSError:
            pass
        return 0

    # status
    if _alive(pidf):
        port = _read_port(data)
        print(f"daemon 运行中 pid={_read_pid(pidf)}" + (f"  面板 http://127.0.0.1:{port}/" if port else ""))
        return 0
    if _panel_disabled(data):
        print("daemon 未运行（面板已被手动关闭；用 `daemon start` 或 `respawn` 可重新唤起）")
        return 1
    print("daemon 未运行")
    return 1


def _pick_free_port(preferred, host="127.0.0.1"):
    """优先用 preferred；被占用则顺序向后找一个可绑定的端口。

    用于「上次的后台任务被手动取消、但端口仍被旧进程占着」的情形，
    保证看守进程每次都能成功重新建立，而不是启动即失败。
    """
    import socket as _s
    for p in range(int(preferred), int(preferred) + 40):
        s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        try:
            s.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return int(preferred)


def cmd_respawn(args):
    """看守进程：在前台持续运行，发现面板端口掉线就重新拉起。

    用于沙箱/CI 这类「父进程一退出就回收所有子进程」的环境 —— 那里
    `daemon start` 拉起的脱离进程会被立刻杀掉，导致面板时有时无。
    把本命令本身交给宿主的长驻后台任务托管，面板即可稳定存活。

    **可重复调用**：本命令是幂等的 —— 每次重新使用技能时再跑一次即可，
    它会（1）清除可能残留的「手动关闭」标记；（2）自动避开被占用的端口；
    （3）接管并守护面板。不需要先清理任何旧状态。
    """
    data, _ = _paths(args)
    os.makedirs(data, exist_ok=True)
    interval = max(1.0, float(getattr(args, "interval", 3.0) or 3.0))
    # 记录自己的 pid：reset / daemon stop 需要先停掉看守进程（否则面板会被立刻重拉、
    # 且账本句柄一直占着，改名会 WinError 32）
    wpidf = _watch_pid_file(data)
    with open(wpidf, "w") as fh:
        fh.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(wpidf) and os.remove(wpidf))

    # 工作区解析：子进程是用 cwd=HERE（技能目录）启动的，原样把 "auto" 传下去会被解析成
    # **技能目录**，于是面板显示「工作区=技能目录 → 监听 0 个日志文件」，本会话消耗全部漏统计。
    # （实测踩过。）这里在**调用者上下文**里解析，并把结果显式传给子进程。
    ws = getattr(args, "workspace", "auto")
    if getattr(args, "all", False):
        ws = None
    elif not ws or ws == "auto":
        rec = _recorded_workspace(data)
        if rec:
            ws = rec
            print(f"[BOC] 沿用账本中已记录的工作区：{ws}"
                  f"（切换请显式传 --workspace <slug> 或 --all）", flush=True)
        else:
            ws = workspace_slug(os.getcwd())
    print(f"[BOC] 监听工作区：{ws if ws else 'ALL（全部工作区）'}", flush=True)

    # 显式唤起 → 清除「手动关闭」标记，保证一次性可恢复（幂等，无标记也不报错）
    _set_panel_disabled(data, False)
    port = int(args.port)
    # 若面板已在别的进程上跑着（例如上次任务没被取消），沿用那个端口，避免起重复实例
    live = _read_port(data)
    if live and _port_open(live):
        port = int(live)
    else:
        port = _pick_free_port(port, args.host)
    print(f"[BOC] 看守进程启动 pid={os.getpid()} 端口={port} 间隔={interval}s", flush=True)
    print(f"[BOC] 建议交给宿主长驻后台任务托管；面板 http://127.0.0.1:{port}/", flush=True)

    def _spawn():
        cmd = [sys.executable, os.path.join(HERE, "bo.py"), "run",
               "--data-dir", data,
               "--projects", os.path.abspath(args.projects or DEFAULT_PROJECTS),
               "--port", str(port), "--host", args.host,
               "--force"]
        if ws:
            cmd += ["--workspace", ws]
        else:
            cmd += ["--all"]
        if args.tokens is not None:
            cmd += ["--tokens", str(args.tokens)]
        if args.cny is not None:
            cmd += ["--cny", str(args.cny)]
        if args.since:
            cmd += ["--since", args.since]
        if args.soft is not None:
            cmd += ["--soft", str(args.soft)]
        if args.hard is not None:
            cmd += ["--hard", str(args.hard)]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        logf = os.path.join(data, "daemon.log")
        with open(logf, "ab") as lf:
            return subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                    creationflags=flags, close_fds=True, cwd=HERE)

    child = None
    fails = 0
    adopted = _port_open(port)   # 启动时端口已通 → 多半是本工具的面板，先接管不重启
    try:
        while True:
            # 用户在面板里点了「彻底关闭」→ 停手，不再重拉；
            # 再次 `daemon start` / `respawn`（会清掉标记）即可正常唤起。
            if _panel_disabled(data):
                if not getattr(_panel_disabled, "_logged", False):
                    print("[BOC] 检测到面板被手动关闭标记，看守进程停止重拉（等待重新唤起）", flush=True)
                    _panel_disabled._logged = True
                time.sleep(interval)
                continue
            _panel_disabled._logged = False
            up = _port_open(port)
            if up:
                # 端口已有服务：若正是本工具面板则接管（不重复起实例）
                if child is None and adopted:
                    print(f"[BOC] 端口 {port} 已有面板在跑，接管守护（不重复启动）", flush=True)
                    adopted = False
                fails = 0
            else:
                if child is not None and child.poll() is None:
                    try:
                        child.terminate()
                    except Exception:
                        pass
                child = _spawn()
                with open(_pid_file(data), "w") as fh:
                    fh.write(str(child.pid))
                print(f"[BOC] 面板已拉起 pid={child.pid} http://127.0.0.1:{port}/", flush=True)
                fails = 0
                # 起完等它监听；失败则累计，连续多次才认为端口被外人占用
                for _ in range(10):
                    time.sleep(0.4)
                    if _port_open(port) or child.poll() is not None:
                        break
                if not _port_open(port):
                    fails += 1
                    if fails >= 3:
                        newport = _pick_free_port(port + 1, args.host)
                        if newport != port:
                            print(f"[BOC] 端口 {port} 起不来（可能被占用），改绑 {newport}", flush=True)
                            port = newport
                        fails = 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[BOC] 看守进程退出", flush=True)
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except Exception:
                pass
        return 0


def _read_pid(pidf):
    try:
        with open(pidf) as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _alive(pidf):
    pid = _read_pid(pidf)
    if not pid:
        return False
    try:
        if os.name == "nt":
            # tasklist 输出为本地代码页(GBK)，必须按 GBK 解码，否则解码异常
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, encoding="gbk", errors="ignore")
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def cmd_watch(args):
    lg = _ledger(args)
    projects = os.path.abspath(args.projects or DEFAULT_PROJECTS)
    ws = _resolve_workspace(args)
    since_ts = _resolve_since(lg, args.since)
    w = Watcher(lg, projects, workspace=ws, since_ts=since_ts, interval=args.interval,
                backfill=(args.since == "all"),
                on_batch=lambda rows: BudgetManager(lg).evaluate())
    print(f"[BOC] 仅监听模式，Ctrl+C 退出。起点 {time.strftime('%H:%M:%S', time.localtime(since_ts))}")
    try:
        while True:
            n = w.scan_once()
            if n:
                print(f"  +{n} 条请求")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("收工")
    return 0


# ── 参数 ───────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(prog="bo", description="预算编排器 (Budget Orchestrator Console)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=None, help=f"数据目录（默认 {DEFAULT_DATA}）")
    common.add_argument("--projects", default=None, help="WorkBuddy projects 目录")
    common.add_argument("--workspace", default="auto", help="工作区 slug 或 auto（默认 auto）")
    common.add_argument("--all", action="store_true", help="统计全部工作区")
    common.add_argument("--json", action="store_true", help="以 JSON 输出")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common], help="初始化账本")
    sp = sub.add_parser("status", parents=[common], help="查看状态")
    sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("check", parents=[common], help="轻量判定")
    sp.set_defaults(func=cmd_check)
    sp = sub.add_parser("url", parents=[common], help="打印面板地址")
    sp.add_argument("--port", type=int, default=8787)
    sp.set_defaults(func=cmd_url)

    sp = sub.add_parser("reset", parents=[common],
                        help="账本归零：停 daemon + 备份 ledger.db 三件套后重新起算")
    sp.add_argument("--keep-budget", action="store_true",
                    help="保留当前预算设置与任务树（只清历史用量）")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("run", parents=[common], help="前台运行（监听+面板）")
    sp.add_argument("--tokens", type=float, default=None)
    sp.add_argument("--cny", type=float, default=None)
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--interval", type=float, default=1.5)
    sp.add_argument("--since", default=None, help="'all' 或秒数（如 600 表示近 10 分钟）")
    sp.add_argument("--soft", type=float, default=None, help="软阈值百分比（默认 70）")
    sp.add_argument("--hard", type=float, default=None, help="硬阈值百分比（默认 100）")
    sp.add_argument("--open", action="store_true", help="启动后尝试用系统浏览器打开看板")
    sp.add_argument("--force", action="store_true", help="忽略「面板已手动关闭」标记，强制启动")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("watch", parents=[common], help="仅监听（前台）")
    sp.add_argument("--interval", type=float, default=1.5)
    sp.add_argument("--since", default=None)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("daemon", parents=[common], help="后台运行")
    sp.add_argument("daemon_action", choices=["start", "stop", "status"])
    sp.add_argument("--tokens", type=float, default=None)
    sp.add_argument("--cny", type=float, default=None)
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--since", default=None)
    sp.add_argument("--soft", type=float, default=None)
    sp.add_argument("--hard", type=float, default=None)
    sp.add_argument("--open", action="store_true", help="启动后尝试用系统浏览器打开看板")
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("respawn", parents=[common],
                        help="看守进程：面板掉线自动重拉（沙箱/CI 环境用）")
    sp.add_argument("--tokens", type=float, default=None)
    sp.add_argument("--cny", type=float, default=None)
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--since", default=None)
    sp.add_argument("--soft", type=float, default=None)
    sp.add_argument("--hard", type=float, default=None)
    sp.add_argument("--interval", type=float, default=3.0)
    sp.set_defaults(func=cmd_respawn)

    sp = sub.add_parser("budget", parents=[common], help="设置/追加预算")
    sp.add_argument("--tokens", type=float, default=None)
    sp.add_argument("--cny", type=float, default=None)
    sp.add_argument("--add-tokens", type=float, default=None)
    sp.add_argument("--add-cny", type=float, default=None)
    sp.add_argument("--keep-since", action="store_true",
                    help="设置预算时保留原统计起点（默认：设置预算即开启新账期，起点推到此刻）")
    sp.add_argument("--soft", type=float, default=None)
    sp.add_argument("--hard", type=float, default=None)
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("control", parents=[common], help="暂停/恢复/停止")
    sp.add_argument("action", choices=["pause", "resume", "stop"])
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_control)

    sp = sub.add_parser("mode", parents=[common], help="auto|observe|enforce")
    sp.add_argument("mode", choices=["auto", "observe", "enforce"])
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser("task", parents=[common], help="任务树")
    sp.add_argument("task_action", choices=["add", "use", "done", "list", "rm", "rm-all", "prio"])
    sp.add_argument("id", nargs="?", default=None)
    sp.add_argument("--name", default=None)
    sp.add_argument("--parent", default="root")
    sp.add_argument("--tokens", type=float, default=None)
    sp.add_argument("--cny", type=float, default=None)
    sp.add_argument("--priority", type=int, default=None, help="1-5，≥4 视为主干（默认 3）")
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("report", parents=[common], help="导出报告")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("lean", parents=[common], help="精简审计（ponytail 融合：YAGNI 阶梯 + 输出纪律）")
    sp.set_defaults(func=cmd_lean)

    sp = sub.add_parser("autopilot", parents=[common],
                        help="自主协调指令：AI 据此自行调整任务（0=继续 3=降级后继续 4=需人工）")
    sp.set_defaults(func=cmd_autopilot)

    sp = sub.add_parser("plan", parents=[common],
                        help="规划校验：额度是否装得下子任务 + 取舍预案（0=合理 3=偏紧 4=超额）")
    sp.set_defaults(func=cmd_plan)
    return p


def main(argv=None):
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw)
    # 原始 argv 透传下去，供「重复旗标静默覆盖」这类检查使用
    args._argv_raw = raw
    if args.cmd == "init":
        return cmd_init(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
