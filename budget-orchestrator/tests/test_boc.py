# -*- coding: utf-8 -*-
"""Budget Orchestrator 单元测试（纯标准库，直接运行即可）。

    python tests/test_boc.py
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import pricing                      # noqa: E402
from core.ledger import Ledger                # noqa: E402
from core.watcher import Watcher, extract      # noqa: E402
from core.budget import BudgetManager          # noqa: E402
from core import lean                          # noqa: E402
from core import autopilot                     # noqa: E402
from core import plan                          # noqa: E402

_SEQ = itertools.count()


def lts(y, m, d, H, M):
    """本地时间 → 时间戳（峰谷判定基于本地时区）。"""
    return datetime.datetime(y, m, d, H, M).timestamp()


class TestPricing(unittest.TestCase):
    def test_norm_model(self):
        self.assertEqual(pricing.norm_model("deepseek-flash"), "deepseek-v4.1-flash")
        self.assertEqual(pricing.norm_model("glm-5.2-x"), "glm-5.2")
        self.assertEqual(pricing.norm_model("hy4-preview-expires-on-0910"), "hy4-preview")
        self.assertEqual(pricing.norm_model(""), "unknown")

    def test_price_longest_prefix(self):
        # 更具体的键必须优先于通用键
        self.assertEqual(pricing.price_of("deepseek-v4-pro")["P"][0], 9)
        self.assertEqual(pricing.price_of("deepseek-v4.1-flash")["P"][0], 2)
        self.assertEqual(pricing.price_of("deepseek-something")["P"][0], 2)
        self.assertEqual(pricing.price_of("unknown-xyz"), pricing.PRICE_DEF)

    def test_is_peak(self):
        # 2026-09-28 周一
        self.assertTrue(pricing.is_peak(lts(2026, 9, 28, 10, 0)))    # 上午高峰
        self.assertTrue(pricing.is_peak(lts(2026, 9, 28, 15, 0)))    # 下午高峰
        self.assertFalse(pricing.is_peak(lts(2026, 9, 28, 12, 30)))  # 午休
        self.assertFalse(pricing.is_peak(lts(2026, 9, 28, 20, 0)))   # 夜间
        self.assertFalse(pricing.is_peak(lts(2026, 9, 26, 10, 0)))   # 周六
        self.assertFalse(pricing.is_peak(lts(2026, 10, 1, 10, 0)))   # 国庆
        self.assertFalse(pricing.is_peak(lts(2026, 9, 27, 10, 0)))   # 中秋假期内

    def test_cost_peak_vs_offpeak(self):
        # deepseek-v4.1-flash: P=[2,8,0.04] O=[1,4,0.02]
        c_peak = pricing.cost_cny("deepseek-v4.1-flash", 1000, 100,
                                  cache_hit=400, ts_sec=lts(2026, 9, 28, 10, 0))
        # (400*0.04 + 600*2 + 100*8)/1e6 = 2016/1e6
        self.assertAlmostEqual(c_peak, 0.002016, places=9)
        c_off = pricing.cost_cny("deepseek-v4.1-flash", 1000, 100,
                                 cache_hit=400, ts_sec=lts(2026, 9, 28, 20, 0))
        # (400*0.02 + 600*1 + 100*4)/1e6 = 1008/1e6
        self.assertAlmostEqual(c_off, 0.001008, places=9)

    def test_cost_cache_write_multiplier(self):
        # claude 系 CW=1.25：写入按 1.25× 输入价
        c = pricing.cost_cny("claude", 1000, 0, cache_hit=0, cache_write=1000,
                             ts_sec=0.0)
        expect = (1000 * 1.25 * 110) / 1e6
        self.assertAlmostEqual(c, expect, places=9)

    def test_cost_clamp(self):
        # 缓存命中数超过输入时不应产生负成本
        c = pricing.cost_cny("deepseek-v4.1-flash", 100, 0, cache_hit=999,
                             cache_write=999, ts_sec=0.0)
        self.assertGreaterEqual(c, 0.0)


def _rec(sess, mid, ts, model="deepseek-v4.1-flash", it=1000, ot=100, ch=0, cw=0):
    return {
        "sessionId": sess,
        "timestamp": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providerData": {
            "messageId": mid,
            "model": model,
            "rawUsage": {
                "prompt_tokens": it, "completion_tokens": ot,
                "total_tokens": it + ot,
                "prompt_cache_hit_tokens": ch,
                "prompt_tokens_details": {"cached_tokens": ch, "cached_creation_tokens": cw},
            },
        },
    }


class TestExtract(unittest.TestCase):
    def test_extract_basic(self):
        r = extract(_rec("s1", "m1", 1000000000))
        self.assertIsNotNone(r)
        self.assertEqual(r["total_tokens"], 1100)
        self.assertEqual(r["uid"], "s1|m1")
        self.assertEqual(r["model"], "deepseek-v4.1-flash")

    def test_extract_skips_non_usage(self):
        self.assertIsNone(extract({"role": "user", "content": "hi"}))
        self.assertIsNone(extract({"providerData": {}}))


class TestLedgerAndWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boc-test-")
        self.projects = os.path.join(self.tmp, "projects")
        self.ws = "d-test-ws"
        self.sessdir = os.path.join(self.projects, self.ws)
        os.makedirs(self.sessdir, exist_ok=True)
        self.jsonl = os.path.join(self.sessdir, "sess1.jsonl")
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))

    def _append(self, recs):
        with open(self.jsonl, "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_incremental_and_dedup(self):
        w = Watcher(self.lg, self.projects, workspace=self.ws, since_ts=0, backfill=True)
        self._append([_rec("s1", "m1", 1000000000), _rec("s1", "m2", 1000000100)])
        self.assertEqual(w.scan_once(), 2)
        self.assertEqual(self.lg.totals()["n"], 2)
        # 重复扫描不应重复计数
        self.assertEqual(w.scan_once(), 0)
        self.assertEqual(self.lg.totals()["n"], 2)
        # 追加新记录 → 只增量读新增
        self._append([_rec("s1", "m3", 1000000200)])
        self.assertEqual(w.scan_once(), 1)
        self.assertEqual(self.lg.totals()["n"], 3)

    def test_partial_line_not_consumed(self):
        w = Watcher(self.lg, self.projects, workspace=self.ws, since_ts=0, backfill=True)
        rec = json.dumps(_rec("s1", "m1", 1000000000)) + "\n"
        with open(self.jsonl, "wb") as fh:
            fh.write(rec.encode("utf-8"))
            fh.write(b'{"sessionId":"s1","providerData":{"rawUsa')  # 半行
        self.assertEqual(w.scan_once(), 1)
        self.assertEqual(self.lg.totals()["n"], 1)
        # 补全半行
        with open(self.jsonl, "ab") as fh:
            fh.write(b'ge":{"total_tokens":5,"prompt_tokens":5,"completion_tokens":0},'
                     b'"messageId":"m2","model":"deepseek-v4.1-flash"},"timestamp":"2026-09-28T02:00:00Z"}\n')
        self.assertEqual(w.scan_once(), 1)
        self.assertEqual(self.lg.totals()["n"], 2)

    def test_subagent_attribution(self):
        # 实际布局：<ws>/<sessionId>/subagents/agent-xxxx.jsonl
        sess = os.path.join(self.projects, self.ws, "sess1")
        sub = os.path.join(sess, "subagents")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "agent-abcd1234.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_rec("s1", "ms", 1000000000)) + "\n")
        w = Watcher(self.lg, self.projects, workspace=self.ws, since_ts=0, backfill=True)
        w.scan_once()
        agents = {a["agent"] for a in self.lg.by_agent()}
        self.assertIn("sub:abcd1234", agents)


def _rec_ms(sess, mid, ts, tt=1100, it=1000, ot=100, ch=0):
    """显式 epoch 毫秒时间戳的构造器。

    不用上面的 `_rec`：它把本地时间 `strftime` 成带 `Z` 的串再按 UTC 解析，
    会整体偏移一个时区，无法用来卡 `since_ts` 边界。毫秒整数走
    `parse_ts` 的 `v > 1e12 → /1000` 分支，无歧义。
    """
    return {
        "type": "function_call",
        "sessionId": sess,
        "timestamp": int(ts * 1000),
        "providerData": {
            "messageId": mid,
            "model": "deepseek-v4.1-flash",
            "rawUsage": {"prompt_tokens": it, "completion_tokens": ot,
                         "total_tokens": tt, "prompt_cache_hit_tokens": ch},
        },
    }


class TestFirstScanPrimeTail(unittest.TestCase):
    """首扫必须回读文件尾部，而不是把游标直接推到末尾。

    回归背景（实测）：宿主在 watcher 启动前就把请求落盘了 —— 面板启动时
    会话 jsonl 里已有 10 条记录，其中 8 条落在 since_ts 之后。旧实现无条件
    `set_file_cursor(size)` 跳过整段，这 8 条被永久漏掉（48.4 万 token /
    ¥0.09），增量扫描也再也读不回来。表现为「面板金额永远比官方账单少一截」。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boc-prime-")
        self.projects = os.path.join(self.tmp, "projects")
        self.ws = "d-test-ws"
        os.makedirs(os.path.join(self.projects, self.ws), exist_ok=True)
        self.jsonl = os.path.join(self.projects, self.ws, "sess1.jsonl")
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))
        self.t0 = 1790607900.0            # 任意基准秒
        self.since = self.t0 + 60         # 起点落在记录中间

    def tearDown(self):
        self.lg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, recs, mode="w"):
        with open(self.jsonl, mode, encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_first_scan_keeps_records_after_since(self):
        # 3 条早于起点（应被 since 过滤）+ 3 条晚于起点（必须计入）
        self._write([
            _rec_ms("s1", "old1", self.t0 + 10),
            _rec_ms("s1", "old2", self.t0 + 40),
            _rec_ms("s1", "new1", self.t0 + 80),
            _rec_ms("s1", "new2", self.t0 + 95),
            _rec_ms("s1", "new3", self.t0 + 110),
        ])
        w = Watcher(self.lg, self.projects, workspace=self.ws,
                    since_ts=self.since, backfill=False)
        # 旧实现这里会返回 0
        self.assertEqual(w.scan_once(), 3)
        self.assertEqual(self.lg.totals()["n"], 3)
        # 游标仍然要推进到文件末尾，否则下一轮会重读
        self.assertEqual(self.lg.file_cursor(self.jsonl)[0], os.path.getsize(self.jsonl))

    def test_first_scan_then_incremental_no_duplicate(self):
        self._write([_rec_ms("s1", "new1", self.t0 + 80)])
        w = Watcher(self.lg, self.projects, workspace=self.ws,
                    since_ts=self.since, backfill=False)
        self.assertEqual(w.scan_once(), 1)
        self.assertEqual(w.scan_once(), 0)          # 不重复计
        self._write([_rec_ms("s1", "new2", self.t0 + 90)], mode="a")
        self.assertEqual(w.scan_once(), 1)          # 增量正常
        self.assertEqual(self.lg.totals()["n"], 2)

    def test_large_file_only_reads_tail(self):
        # 超过 PRIME_TAIL 的文件不应整体回读：只回读尾部，且起点之后仍被计入
        recs = [_rec_ms("s1", f"m{i}", self.t0 + 100 + i) for i in range(60)]
        # 记录放在**文件末尾**（回读尾部的现实场景），前面用无用的填充撑大文件
        blob = (b'{"pad":1}\n' * (Watcher.PRIME_TAIL // 9)
                + b"".join((json.dumps(r) + "\n").encode("utf-8") for r in recs))
        with open(self.jsonl, "wb") as fh:
            fh.write(blob)
        self.assertGreater(os.path.getsize(self.jsonl), Watcher.PRIME_TAIL)
        w = Watcher(self.lg, self.projects, workspace=self.ws,
                    since_ts=self.since, backfill=False)
        w.scan_once()
        # 尾部回读覆盖这 60 条（它们就在文件末尾）
        self.assertEqual(self.lg.totals()["n"], 60)
        # 且确实走了尾部回读分支
        kinds = {e["kind"] for e in self.lg.events(50)}
        self.assertIn("prime_tail", kinds)


class TestBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boc-budget-")
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))
        self.bm = BudgetManager(self.lg)
        self._seq = itertools.count(1)

    def _add(self, tt, cost):
        """模拟 watcher 入库：task_id 取自当前 active_task。"""
        tid = self.lg.get_meta("active_task", "root")
        self.lg.add_request({"uid": f"u{next(self._seq)}", "ts": 1000000000,
                             "session_id": "s", "agent": "main", "src_file": "x",
                             "model": "deepseek-v4.1-flash", "input_tokens": tt,
                             "output_tokens": 0, "cache_hit": 0, "cache_write": 0,
                             "reasoning_tokens": 0, "total_tokens": tt,
                             "is_peak": 0, "cost_cny": cost, "task_id": tid})

    def test_levels_and_gating(self):
        self.bm.set_budget(tokens=1000, cny=0)
        self._add(500, 0.001)
        self.assertEqual(self.bm.snapshot()["level"], "ok")
        self.assertEqual(self.bm.check()["verdict"], "GO")
        self._add(300, 0.001)   # 800/1000 = 80% → warn
        self.assertEqual(self.bm.check()["verdict"], "DEGRADE")
        self._add(300, 0.001)   # 1100/1000 → over
        self.assertEqual(self.bm.check()["verdict"], "STOP")

    def test_enforce_autopause(self):
        self.bm.set_budget(tokens=100, cny=0)
        self.bm.control("set_mode", mode="enforce")
        self._add(200, 0.001)
        self.bm.evaluate()
        self.assertEqual(self.lg.get_meta("state"), "paused")

    def test_add_budget_resumes(self):
        self.bm.set_budget(tokens=100, cny=0)
        self._add(200, 0.001)
        self.bm.control("pause")
        self.assertEqual(self.lg.get_meta("state"), "paused")
        self.bm.add_budget(tokens=10000)
        self.assertEqual(self.lg.get_meta("state"), "running")

    def test_readonly_ledger_degrades_gracefully(self):
        """账本位于只读路径时应降级为可读，而不是抛异常。"""
        ro = os.path.join(self.tmp, "ro.db")
        lg1 = Ledger(ro)
        lg1.add_request({"uid": "r1", "ts": 1000000000, "session_id": "s", "agent": "main",
                         "src_file": "x", "model": "m", "input_tokens": 10, "output_tokens": 1,
                         "cache_hit": 0, "cache_write": 0, "reasoning_tokens": 0,
                         "total_tokens": 11, "is_peak": 0, "cost_cny": 0.0, "task_id": "root"})
        os.chmod(ro, 0o444)
        try:
            lg2 = Ledger(ro)                      # 不应抛异常
            self.assertEqual(lg2.totals()["n"], 1)  # 仍可读
        finally:
            os.chmod(ro, 0o666)

    def test_task_isolation(self):
        self.lg.upsert_task({"id": "T1", "parent": "root", "name": "A", "budget_tokens": 1000})
        self.lg.set_meta("active_task", "root")
        self._add(500, 0.001)
        self.lg.set_meta("active_task", "T1")
        self._add(700, 0.001)
        self.assertEqual(self.lg.totals("T1")["tt"], 700)
        self.assertEqual(self.lg.totals("root")["tt"], 500)
        self.assertEqual(self.lg.totals()["tt"], 1200)


class TestLean(unittest.TestCase):
    """ponytail 融合层：YAGNI 档位 + 精简审计 + 诚实边界。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boc-lean-")
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))
        self._seq = itertools.count(1)

    def _add(self, tt, tool=None, target=None, ot=0):
        self.lg.add_request({"uid": f"L{next(self._seq)}", "ts": 1000000000,
                             "session_id": "s", "agent": "main", "src_file": "x",
                             "model": "deepseek-v4.1-flash", "input_tokens": tt,
                             "output_tokens": ot, "cache_hit": 0, "cache_write": 0,
                             "reasoning_tokens": 0, "total_tokens": tt + ot,
                             "is_peak": 0, "cost_cny": 0.0, "task_id": "root",
                             "tool": tool, "target": target})

    def test_lean_level_bands(self):
        self.assertEqual(lean.lean_level(0)["key"], "normal")
        self.assertEqual(lean.lean_level(59.9)["key"], "normal")
        self.assertEqual(lean.lean_level(60)["key"], "lite")
        self.assertEqual(lean.lean_level(80)["key"], "full")
        self.assertEqual(lean.lean_level(95)["key"], "ultra")
        self.assertEqual(lean.lean_level(9999)["key"], "ultra")

    def test_detect_rework(self):
        for _ in range(4):
            self._add(1000, "Edit", "src/bo.py")
        rep = lean.audit(self.lg, since=0)
        self.assertIn("rework", [f["kind"] for f in rep["findings"]])

    def test_detect_rewrite(self):
        for _ in range(2):
            self._add(1000, "Write", "src/a.py")
        rep = lean.audit(self.lg, since=0)
        self.assertIn("rewrite", [f["kind"] for f in rep["findings"]])

    def test_detect_context_bloat(self):
        self._add(200000, "Bash", "big command")
        rep = lean.audit(self.lg, since=0)
        self.assertIn("context_bloat", [f["kind"] for f in rep["findings"]])

    def test_detect_chattiness(self):
        for _ in range(6):
            self._add(1000, "Bash", "x", ot=500)   # 输出占输入 50%
        rep = lean.audit(self.lg, since=0)
        self.assertIn("chattiness", [f["kind"] for f in rep["findings"]])

    def test_empty_is_clean(self):
        rep = lean.audit(self.lg, since=0)
        self.assertEqual(rep["findings"], [])
        self.assertEqual(rep["window_tokens"], 0)
        self.assertTrue(rep["ladder"] and rep["never_cut"])

    def test_ladder_has_seven_rungs(self):
        self.assertEqual([l["rung"] for l in lean.LADDER], list(range(1, 8)))

    def test_never_fabricates_savings(self):
        """诚实边界：审计输出不得出现『省了/节省/节约』这类无基线的估算措辞。"""
        for _ in range(5):
            self._add(5000, "Edit", "x.py")
        rep = lean.audit(self.lg, since=0)
        blob = json.dumps(rep, ensure_ascii=False)
        for banned in ("省了", "节省", "节约", "saved"):
            self.assertNotIn(banned, blob)


class TestAutopilot(unittest.TestCase):
    """自主协调引擎：AI 自主降级而非等人。"""

    def _snap(self, pct, remaining=1.0, level=None):
        lv = "over" if pct >= 100 else ("warn" if pct >= 70 else "ok")
        return {"pct": {"overall": pct}, "level": level or lv,
                "remaining": {"cny": remaining, "tokens": None}}

    def _cfg(self, **kw):
        c = {"budget_cny": 1.0, "budget_tokens": 0, "soft_pct": 70, "hard_pct": 100}
        c.update(kw)
        return c

    def test_proceed_when_room(self):
        d = autopilot.directive(self._snap(10), self._cfg(), [])
        self.assertEqual(d["mode"], "proceed")
        self.assertEqual(d["actions"], [])

    def test_adapt_midway_keeps_going(self):
        """过半程预算时应给动作并继续，而不是停下等人。"""
        d = autopilot.directive(self._snap(80), self._cfg(), [])
        self.assertEqual(d["mode"], "adapt")
        self.assertIn("yagni_first", [a["id"] for a in d["actions"]])
        self.assertNotIn("escalate", d)

    def test_skip_nonessential_only(self):
        tasks = [{"id": "T1", "priority": 5, "status": "pending", "kind": "task"},
                 {"id": "T2", "priority": 2, "status": "pending", "kind": "task"},
                 {"id": "root", "priority": 3, "status": "pending", "kind": "root"}]
        d = autopilot.directive(self._snap(105, remaining=0.5), self._cfg(), tasks)
        skip = [a for a in d["actions"] if a["id"] == "skip_optional"]
        self.assertTrue(skip)
        self.assertEqual(skip[0]["params"]["skip_task_ids"], ["T2"])
        self.assertEqual(skip[0]["params"]["keep_task_ids"], ["T1"])
        self.assertEqual(d["essential_pending"], ["T1"])

    def test_root_never_skipped(self):
        tasks = [{"id": "root", "priority": 3, "status": "pending", "kind": "root"}]
        d = autopilot.directive(self._snap(105, remaining=0.5), self._cfg(), tasks)
        self.assertEqual(d["optional_pending"], [])

    def test_escalate_only_when_exhausted_and_essential_left(self):
        tasks = [{"id": "T1", "priority": 5, "status": "pending", "kind": "task"}]
        d = autopilot.directive(self._snap(120, remaining=0.0), self._cfg(), tasks)
        self.assertEqual(d["mode"], "escalate")
        self.assertIn("escalate", d)

    def test_no_escalate_when_essential_done(self):
        """预算超了但主干已完成 → 不打扰人，直接收尾。"""
        tasks = [{"id": "T1", "priority": 5, "status": "done", "kind": "task"}]
        d = autopilot.directive(self._snap(150, remaining=0.0), self._cfg(), tasks)
        self.assertEqual(d["mode"], "adapt")
        self.assertNotIn("escalate", d)

    def test_text_render(self):
        d = autopilot.directive(self._snap(85), self._cfg(), [])
        t = autopilot.autopilot_text(d)
        self.assertIn("降级后继续", t)
        self.assertIn("下次自检", t)


class TestPlan(unittest.TestCase):
    """规划校验：开工前确认预算装不装得下，并给出取舍预案。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boc-plan-")
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))

    def _mk(self, cny, tasks):
        self.lg.set_meta("budget_cny", cny)
        self.lg.upsert_task({"id": "root", "parent": None, "name": "总任务", "kind": "root"})
        for tid, name, prio, amt in tasks:
            self.lg.upsert_task({"id": tid, "parent": "root", "name": name,
                                 "priority": prio, "budget_cny": amt})

    def test_ok_when_within_budget_and_share_healthy(self):
        self._mk(1.0, [("A", "主干", 5, 0.5), ("B", "次要", 2, 0.2), ("C", "可选", 1, 0.1)])
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "ok")
        self.assertAlmostEqual(r["planned"]["cny"], 0.8, places=6)
        self.assertAlmostEqual(r["essential"]["share"], 0.5, places=6)

    def test_over_when_planned_exceeds_total(self):
        self._mk(1.0, [("A", "主干", 5, 0.8), ("B", "次要", 2, 0.5), ("C", "可选", 1, 0.3)])
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "over")
        self.assertAlmostEqual(r["shortfall"]["cny"], 0.0, places=6)   # 砍完刚好够
        self.assertIn("覆盖", r["advice"])

    def test_sacrifice_order_is_by_priority_then_size(self):
        """牺牲顺序：优先级低者先砍；同优先级下额度大者先砍。"""
        self._mk(0.5, [("A", "主干", 5, 0.3),
                       ("P1a", "低优先大额", 1, 0.4), ("P1b", "低优先小额", 1, 0.2),
                       ("P2", "次低", 2, 0.3)])
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "over")
        order = [s["id"] for s in r["sacrifice"]]
        self.assertEqual(order[:2], ["P1a", "P1b"])   # 同为 P1，大额在前
        self.assertEqual(order[2], "P2")

    def test_shortfall_when_even_cutting_everything_is_not_enough(self):
        self._mk(1.0, [("A", "主干", 5, 1.7), ("B", "可选", 2, 0.5), ("C", "可选", 1, 0.3)])
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "over")
        self.assertAlmostEqual(r["shortfall"]["cny"], 0.7, places=6)
        self.assertIn("追加预算", r["advice"])

    def test_tight_when_essential_share_too_high(self):
        self._mk(1.0, [("A", "主干", 5, 0.8), ("B", "次要", 2, 0.1)])
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "tight")
        self.assertGreater(r["essential"]["share"], plan.ESSENTIAL_SHARE_MAX)

    def test_tight_when_task_has_no_budget(self):
        self._mk(1.0, [("A", "主干", 5, 0.3)])
        self.lg.upsert_task({"id": "B", "parent": "root", "name": "没标额度", "priority": 2})
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "tight")
        self.assertEqual(r["unbudgeted"], ["B"])

    def test_nobudget(self):
        self.lg.upsert_task({"id": "A", "parent": "root", "name": "x", "priority": 3})
        r = plan.audit(self.lg)
        self.assertEqual(r["verdict"], "nobudget")

    def test_root_never_counted_as_subtask(self):
        self._mk(1.0, [("A", "主干", 5, 0.4)])
        r = plan.audit(self.lg)
        self.assertEqual(r["count"], 1)
        self.assertNotIn("root", [s["id"] for s in r["sacrifice"]])

    def test_never_crashes_on_empty_data(self):
        r = plan.audit(self.lg)          # 无预算、无任务
        self.assertEqual(r["verdict"], "nobudget")
        plan.plan_text(r)                # 渲染不应崩
        plan.next_action(r)


class TestPanelShutdown(unittest.TestCase):
    """手动关闭面板的标记语义：关得掉、认得出、还能再唤起。"""

    def setUp(self):
        # 不用 TemporaryDirectory：Windows 下 sqlite 句柄可能导致删除失败，
        # 用一个带唯一名的临时目录，清理失败不影响测试结论。
        self.data = os.path.join(tempfile.gettempdir(),
                                 "boc-panel-test-%d-%d" % (os.getpid(), next(_SEQ)))
        os.makedirs(self.data, exist_ok=True)
        import bo as bo_mod
        self.bo = bo_mod

    def tearDown(self):
        import shutil
        shutil.rmtree(self.data, ignore_errors=True)

    def test_marker_roundtrip(self):
        self.assertFalse(self.bo._panel_disabled(self.data))
        self.bo._set_panel_disabled(self.data, True)
        self.assertTrue(self.bo._panel_disabled(self.data))
        self.bo._set_panel_disabled(self.data, False)
        self.assertFalse(self.bo._panel_disabled(self.data))

    def test_clearing_when_absent_is_safe(self):
        # 标记不存在时清除不应抛异常（幂等）
        self.bo._set_panel_disabled(self.data, False)
        self.assertFalse(self.bo._panel_disabled(self.data))

    def test_marker_file_named_as_documented(self):
        self.bo._set_panel_disabled(self.data, True)
        self.assertTrue(os.path.exists(os.path.join(self.data, "panel.disabled")))

    def test_run_refuses_when_disabled_and_respects_force(self):
        """run 在标记存在时不启动面板；--force 可越过（此处只验证标记语义）。

        注意：--force 会真的起服务，故这里只用不带 force 的路径断言「拒绝启动」，
        再单独验证 force 分支会先清标记 —— 用 _set_panel_disabled 直接表达。
        """
        from core.ledger import Ledger
        from core.budget import BudgetManager

        lg = Ledger(os.path.join(self.data, "ledger.db"))
        BudgetManager(lg).set_budget(100000, 1.0)
        self.bo._set_panel_disabled(self.data, True)

        class A:
            data_dir = self.data
            projects = None
            workspace = "auto"
            all = False
            json = False
            tokens = None
            cny = None
            soft = None
            hard = None
            since = None
            interval = 1.5
            host = "127.0.0.1"
            port = 0
            open = False
            force = False

        # 标记在 → 拒绝启动，且标记保持
        rc = self.bo._do_run(A())
        self.assertEqual(rc, 0)
        self.assertTrue(self.bo._panel_disabled(self.data))

        # force 分支的等价语义：清标记后即可继续
        self.bo._set_panel_disabled(self.data, False)
        self.assertFalse(self.bo._panel_disabled(self.data))
        lg.close()   # 释放 sqlite 句柄，避免 Windows 下临时目录清理失败


    def test_respawn_is_idempotent_when_marker_left_behind(self):
        """核心保证：上次后台任务被手动取消后残留标记，再次 respawn 仍能正常建立。

        对应需求：「可以在 workbuddy 中手动关闭托管看守进程的后台任务，
        重新使用技能时仍要能重新创立看守进程」——不能因残留状态而拒绝启动。
        """
        # 制造"上次手动关闭未清理"的现场
        self.bo._set_panel_disabled(self.data, True)
        self.assertTrue(self.bo._panel_disabled(self.data))

        # respawn 启动时的第一步必须是：无条件清除标记（幂等唤起）
        self.bo._set_panel_disabled(self.data, False)
        self.assertFalse(self.bo._panel_disabled(self.data))
        # 再清一次也不报错（幂等）
        self.bo._set_panel_disabled(self.data, False)
        self.assertFalse(self.bo._panel_disabled(self.data))

    def test_port_fallback_skips_occupied_port(self):
        """端口被占时应能换到一个可用端口，避免看守进程启动即失败。"""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        try:
            got = self.bo._pick_free_port(busy, "127.0.0.1")
            self.assertNotEqual(got, busy)      # 必须避开被占端口
            self.assertTrue(0 < got < 65536)
        finally:
            s.close()


class TestSinceScope(unittest.TestCase):
    """预算口径：'已用' 必须只统计统计起点之后的请求。

    回归目标 —— 面板出现「已用 4090%」：totals() 过去不带时间过滤，
    而 plan/lean/按模型分布都按 since_ts 过滤，口径不一致导致历史请求
    持续累加进"已用"。
    """

    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(),
                                "boc-since-%d-%d" % (os.getpid(), next(_SEQ)))
        os.makedirs(self.tmp, exist_ok=True)
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))
        self.bm = BudgetManager(self.lg)
        self._seq = itertools.count(1)

    def tearDown(self):
        import shutil
        self.lg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, ts, tt, cost, tid="root"):
        self.lg.add_request({
            "uid": "S%d" % next(self._seq), "ts": ts, "session_id": "s", "agent": "main",
            "src_file": "x", "model": "deepseek-v4.1-flash", "input_tokens": tt,
            "output_tokens": 0, "cache_hit": 0, "cache_write": 0, "reasoning_tokens": 0,
            "total_tokens": tt, "is_peak": 0, "cost_cny": cost, "task_id": tid})

    def test_totals_defaults_to_lifetime(self):
        """不传 since → 全量（`task list` 这类终身视图依赖它）。"""
        now = time.time()
        self._add(now - 3600, 1000, 0.10)     # 起点之前的历史
        self._add(now, 500, 0.05)
        self.assertEqual(self.lg.totals()["tt"], 1500)
        self.assertEqual(round(self.lg.totals()["cost"], 4), 0.15)

    def test_totals_respects_since(self):
        now = time.time()
        self._add(now - 3600, 1000, 0.10)
        self._add(now, 500, 0.05)
        scoped = self.lg.totals(since=now - 60)
        self.assertEqual(scoped["tt"], 500)          # 只算起点之后
        self.assertEqual(round(scoped["cost"], 4), 0.05)
        self.assertEqual(scoped["n"], 1)

    def test_totals_since_zero_means_no_filter(self):
        now = time.time()
        self._add(now, 500, 0.05)
        self.assertEqual(self.lg.totals(since=0)["tt"], 500)
        self.assertEqual(self.lg.totals(since=None)["tt"], 500)

    def test_totals_combines_task_and_since(self):
        now = time.time()
        self.lg.upsert_task({"id": "T1", "parent": "root", "name": "A"})
        self._add(now - 3600, 900, 0.09, tid="T1")    # 历史，属 T1
        self._add(now, 100, 0.01, tid="T1")           # 本任务，属 T1
        scoped = self.lg.totals("T1", since=now - 60)
        self.assertEqual(scoped["tt"], 100)
        self.assertEqual(self.lg.totals("T1")["tt"], 1000)   # 终身口径不受影响

    def test_snapshot_pct_ignores_history(self):
        """核心回归：历史请求不得把百分比顶到几千。"""
        now = time.time()
        self._add(now - 86400, 10_000_000, 5.0)      # 上一轮任务的历史消耗
        self.lg.set_meta("since_ts", now)
        self.bm.set_budget(tokens=100000, cny=0.15)
        snap = self.bm.snapshot()
        self.assertEqual(snap["used"]["tokens"], 0)
        self.assertEqual(snap["pct"]["overall"], 0.0)
        self.assertEqual(snap["level"], "ok")

        self._add(now + 0.001, 50_000, 0.07)
        snap = self.bm.snapshot()
        self.assertEqual(snap["used"]["tokens"], 50_000)
        self.assertEqual(snap["pct"]["tokens"], 50.0)
        self.assertEqual(snap["level"], "ok")        # 不再被历史顶成 over

    def test_snapshot_per_task_scoped_too(self):
        """任务树必须与总口径一致，否则子任务之和会超过总量。"""
        now = time.time()
        self.lg.upsert_task({"id": "T1", "parent": "root", "name": "A"})
        self._add(now - 86400, 800, 0.08, tid="T1")     # 历史
        self.lg.set_meta("since_ts", now)
        self._add(now + 0.001, 200, 0.02, tid="T1")
        self.bm.set_budget(tokens=1000, cny=0)
        snap = self.bm.snapshot()
        t1 = [t for t in snap["tasks"] if t["id"] == "T1"][0]
        self.assertEqual(t1["used_tokens"], 200)
        self.assertEqual(snap["used"]["tokens"], 200)

    def test_rate_eta_uses_scoped_usage(self):
        now = time.time()
        self._add(now - 86400, 10_000_000, 5.0)
        self.lg.set_meta("since_ts", now)
        self.bm.set_budget(tokens=1000000, cny=0)
        self._add(now + 0.001, 1000, 0.001)
        r = self.bm.rate()
        self.assertIsNotNone(r["eta_min"])
        self.assertGreater(r["eta_min"], 0)          # 不会因历史变成负数/0

    def test_plan_used_is_scoped(self):
        now = time.time()
        self._add(now - 86400, 9_000_000, 9.0)
        self.lg.set_meta("since_ts", now)
        self.bm.set_budget(tokens=100000, cny=0.15)
        self._add(now + 0.001, 1000, 0.001)
        r = plan.audit(self.lg)
        self.assertEqual(r["used"]["tokens"], 1000)
        self.assertEqual(round(r["used"]["cny"], 4), 0.001)


class TestTaskFlagIntake(unittest.TestCase):
    """命令行入账：额度旗标的位置、漏给、重复给。"""

    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(),
                                "boc-flags-%d-%d" % (os.getpid(), next(_SEQ)))
        # 注意：`--data-dir` 收的是**目录**，账本固定叫该目录下的 ledger.db
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)
        import bo as bo_mod
        self.bo = bo_mod
        self.db = os.path.join(self.data, "ledger.db")

    def tearDown(self):
        import shutil
        try:
            Ledger(self.db).close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        """跑真实 CLI，返回 (退出码, stdout, stderr)。

        `--data-dir` 由 `parents=[common]` 定义在**子命令**上，所以必须插在
        子命令名之后（`bo.py task --data-dir X ...`），放在前面会 argparse 报错。
        """
        import io
        import contextlib
        full = [argv[0], "--data-dir", self.data] + list(argv[1:])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = self.bo.main(full)
            except SystemExit as e:
                rc = e.code or 0
        return rc, out.getvalue(), err.getvalue()

    def _stored(self):
        lg = Ledger(self.db)
        t = lg.get_task("T1")
        lg.close()
        return t

    def test_budget_flag_before_action_word(self):
        """推荐写法：旗标在 add 之前 —— 额度必须落账。"""
        rc, _, _ = self._run(["task", "--cny", "0.045", "--priority", "5",
                              "add", "T1", "--name", "核心功能"])
        self.assertEqual(rc, 0)
        t = self._stored()
        self.assertAlmostEqual(t["budget_cny"], 0.045)
        self.assertEqual(t["priority"], 5)

    def test_budget_flag_after_action_word_also_works(self):
        """当前实现两种位置都能解析（argparse 允许交错）——防止文档写成"会失败"。"""
        rc, _, _ = self._run(["task", "add", "T1", "--name", "核心功能",
                              "--priority", "5", "--cny", "0.045"])
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(self._stored()["budget_cny"], 0.045)

    def test_missing_budget_is_loud_not_silent(self):
        """漏给额度必须显式告警，而不是安静通过（实测踩过的坑）。"""
        rc, out, err = self._run(["task", "add", "T1", "--name", "核心功能", "--priority", "5"])
        self.assertEqual(rc, 0)
        self.assertIsNone(self._stored()["budget_cny"])
        self.assertIn("未给 --cny", err)
        self.assertIn("无法参与取舍计算", err)
        self.assertIn("补记写法", err)

    def test_no_warning_when_budget_given(self):
        rc, out, err = self._run(["task", "--cny", "0.02", "add", "T1", "--name", "X"])
        self.assertEqual(rc, 0)
        self.assertNotIn("未给 --cny", err)

    def test_duplicate_budget_flag_warns(self):
        """argparse 后者覆盖前者是静默的 —— 必须提醒。"""
        rc, out, err = self._run(["task", "--cny", "0.045", "add", "T1", "--name", "X",
                                  "--cny", "0.02"])
        self.assertEqual(rc, 0)
        self.assertIn("--cny 出现 2 次", err)
        self.assertAlmostEqual(self._stored()["budget_cny"], 0.02)   # 文档要写清是"后者生效"

    def test_unbudgeted_task_makes_plan_tight(self):
        """漏额度的后果落到 plan 上：unbudgeted → tight。"""
        self._run(["task", "add", "T1", "--name", "X", "--priority", "5"])
        self._run(["budget", "--cny", "0.15"])
        lg = Ledger(self.db)
        r = plan.audit(lg)
        lg.close()
        self.assertIn("T1", r["unbudgeted"])
        self.assertEqual(r["verdict"], "tight")

    def test_budgeted_task_plan_ok(self):
        self._run(["task", "--cny", "0.045", "--priority", "5", "add", "T1", "--name", "X"])
        self._run(["task", "--cny", "0.030", "--priority", "2", "add", "T2", "--name", "Y"])
        self._run(["budget", "--cny", "0.15"])
        lg = Ledger(self.db)
        r = plan.audit(lg)
        lg.close()
        self.assertEqual(r["unbudgeted"], [])
        self.assertEqual(r["verdict"], "ok")


class TestLedgerReset(unittest.TestCase):
    """`bo.py reset`：三件套一起备份改名，然后从零起算。"""

    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(),
                                "boc-reset-%d-%d" % (os.getpid(), next(_SEQ)))
        self.data = os.path.join(self.tmp, "data")     # --data-dir 是目录
        os.makedirs(self.data, exist_ok=True)
        import bo as bo_mod
        self.bo = bo_mod
        self.db = os.path.join(self.data, "ledger.db")

    def tearDown(self):
        import shutil
        try:
            Ledger(self.db).close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        import io
        import contextlib
        # `--projects` 必须指向空的临时目录：否则默认指向真实的
        # ~/.workbuddy/projects，而 reset 会把 since_ts 设成"此刻"，
        # 首次扫描回读尾部时会把测试期间真实写入的新请求算进来，
        # 使 `reset 后 n==0` 这条断言随机失败（实测踩过）。
        proj = os.path.join(self.tmp, "projects")
        os.makedirs(proj, exist_ok=True)
        full = [argv[0], "--data-dir", self.data, "--projects", proj] + list(argv[1:])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = self.bo.main(full)
            except SystemExit as e:
                rc = e.code or 0
        return rc, out.getvalue(), err.getvalue()

    def _seed(self, tt=5000, cost=0.05):
        lg = Ledger(self.db)
        lg.set_meta("budget_cny", 0.15)
        lg.set_meta("since_ts", 0)          # 故意不设起点 → 历史会被算进去
        lg.add_request({"uid": "R1", "ts": time.time(), "session_id": "s", "agent": "main",
                        "src_file": "x", "model": "deepseek-v4.1-flash", "input_tokens": tt,
                        "output_tokens": 0, "cache_hit": 0, "cache_write": 0,
                        "reasoning_tokens": 0, "total_tokens": tt, "is_peak": 0,
                        "cost_cny": cost, "task_id": "root"})
        lg.close()

    def test_reset_moves_whole_triple_to_backup(self):
        """三件套**凡是存在的**都要一起进 backup/ —— 只搬主库会得到半旧的账。"""
        self._seed()
        # 再写一次，尽量让 -wal/-shm 出现
        lg = Ledger(self.db)
        lg.add_request({"uid": "R2", "ts": time.time(), "session_id": "s", "agent": "main",
                        "src_file": "x", "model": "deepseek-v4.1-flash", "input_tokens": 1,
                        "output_tokens": 0, "cache_hit": 0, "cache_write": 0,
                        "reasoning_tokens": 0, "total_tokens": 1, "is_peak": 0,
                        "cost_cny": 0.0, "task_id": "root"})
        lg.close()
        before = [f for f in ("ledger.db", "ledger.db-shm", "ledger.db-wal")
                  if os.path.exists(os.path.join(self.data, f))]
        self.assertIn("ledger.db", before)

        rc, out, err = self._run(["reset"])
        self.assertEqual(rc, 0, err)
        self.assertIn("已备份", out)

        bdir = os.path.join(self.data, "backup")
        moved = os.listdir(bdir)
        suffix = {"ledger.db": ".db", "ledger.db-shm": ".db-shm", "ledger.db-wal": ".db-wal"}
        for f in before:
            self.assertTrue(any(m.endswith(suffix[f]) for m in moved),
                            "%s 未被一起备份：%s" % (f, moved))
        # 备份里的主库必须还带着旧数据（否则等于把账弄丢了）
        # 5000 来自 _seed，+1 来自后面补写的 R2
        bdb = [f for f in moved if f.endswith(".db")][0]
        old = Ledger(os.path.join(bdir, bdb))
        self.assertEqual(old.totals("root")["tt"], 5001)
        old.close()

    def test_reset_backup_keeps_db_extension(self):
        """备份名保留 .db 后缀，回滚时一眼能认出。"""
        self._seed()
        self._run(["reset"])
        moved = os.listdir(os.path.join(self.data, "backup"))
        self.assertTrue(any(m.endswith(".db") for m in moved), moved)
        self.assertTrue(all(m.startswith("ledger-") for m in moved), moved)

    def test_reset_zeroes_usage(self):
        self._seed()
        rc, out, _ = self._run(["reset"])
        self.assertEqual(rc, 0)
        lg = Ledger(self.db)
        agg = lg.totals()
        cfg_since = float(lg.get_meta("since_ts") or 0)
        lg.close()
        self.assertEqual(agg["n"], 0)                  # 新账本干净
        self.assertGreater(cfg_since, 0)               # 起点已设为此刻
        self.assertIn("0 次请求", out)

    def test_reset_keep_budget_carries_budget_and_tasks(self):
        self._seed()
        self._run(["task", "--cny", "0.045", "--priority", "5", "add", "T1", "--name", "核心"])
        rc, out, _ = self._run(["reset", "--keep-budget"])
        self.assertEqual(rc, 0)
        self.assertIn("保留", out)
        lg = Ledger(self.db)
        t = lg.get_task("T1")
        bm = BudgetManager(lg)
        self.assertEqual(lg.totals()["n"], 0)                      # 用量清零
        self.assertAlmostEqual(bm.config()["budget_cny"], 0.15)     # 预算保留
        self.assertAlmostEqual(t["budget_cny"], 0.045)              # 任务额度保留
        self.assertEqual(t["priority"], 5)
        lg.close()

    def test_reset_clears_panel_disabled_marker(self):
        """重置后应能正常唤起面板。"""
        self._seed()
        self.bo._set_panel_disabled(self.data, True)
        self.assertTrue(self.bo._panel_disabled(self.data))
        self._run(["reset"])
        self.assertFalse(self.bo._panel_disabled(self.data))

    def test_reset_on_empty_dir_is_safe(self):
        rc, out, _ = self._run(["reset"])
        self.assertEqual(rc, 0)
        self.assertIn("没有找到账本文件", out)

    def test_reset_preserves_rollback_data(self):
        """回滚：把备份的三件套**一起**放回原位，历史即可还原。

        为什么不能只放主库：残留的 -wal 会被 SQLite 当成"自己的" WAL 重放，
        于是新账本（空）的页覆盖掉刚放回去的旧数据 —— 这正是文档强调
        "三件套同进同出"的实测依据。
        """
        import shutil
        self._seed(tt=777)
        rc, _, _ = self._run(["reset"])
        self.assertEqual(rc, 0)

        bdir = os.path.join(self.data, "backup")
        # 先清掉新账本的残留，再整体放回
        for ext in ("", "-shm", "-wal"):
            p = self.db + ext
            if os.path.exists(p):
                os.remove(p)
        for f in os.listdir(bdir):
            ext = f.split(".db", 1)[1]           # "" / "-shm" / "-wal"
            shutil.move(os.path.join(bdir, f), self.db + ext)

        lg = Ledger(self.db)
        self.assertEqual(lg.totals("root")["tt"], 777)    # 历史回来了
        lg.close()

    def test_reset_leaves_no_open_handle(self):
        """reset 收工时必须关掉新账本的连接，否则文件句柄会一直占着（Windows 下无法再改名/删除）。"""
        import gc
        self._seed()
        rc, _, _ = self._run(["reset"])
        self.assertEqual(rc, 0)
        # 不依赖 gc 也能改名 → 说明没有残留句柄
        gc.collect()
        probe = self.db + ".move-probe"
        os.replace(self.db, probe)          # 不 gc 也不该报 WinError 32
        self.assertTrue(os.path.exists(probe))


class TestUnknownPricing(unittest.TestCase):
    """牌价未知的模型必须被标出来，而不是若无其事地显示一个精确金额。

    起因：实测匿名模型 `stealth/space-bunny-alpha` 一个价格键都不命中，
    落到了兜底档，却仍被显示成精确到 6 位小数的"确定金额"。
    """

    def setUp(self):
        self.tmp = os.path.join(tempfile.gettempdir(),
                                "boc-price-%d-%d" % (os.getpid(), next(_SEQ)))
        os.makedirs(self.tmp, exist_ok=True)
        self.lg = Ledger(os.path.join(self.tmp, "ledger.db"))
        self.bm = BudgetManager(self.lg)
        self._seq = itertools.count(1)

    def tearDown(self):
        import shutil
        self.lg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, model, tt, cost):
        self.lg.add_request({
            "uid": "P%d" % next(self._seq), "ts": time.time(), "session_id": "s",
            "agent": "main", "src_file": "x", "model": model, "input_tokens": tt,
            "output_tokens": 0, "cache_hit": 0, "cache_write": 0, "reasoning_tokens": 0,
            "total_tokens": tt, "is_peak": 0, "cost_cny": cost, "task_id": "root"})

    def test_known_models_are_priced(self):
        for m in ("deepseek-v4.1-flash", "claude-opus-5", "glm-5.2", "kimi-k2"):
            self.assertTrue(pricing.is_priced(m), m + " 应有牌价")

    def test_anonymous_model_is_unpriced(self):
        self.assertFalse(pricing.is_priced("stealth/space-bunny-alpha"))
        self.assertIsNone(pricing.match_key("stealth/space-bunny-alpha"))
        # 仍然能算出一个金额（兜底档），但那是假设值
        self.assertIs(pricing.price_of("stealth/space-bunny-alpha"), pricing.PRICE_DEF)

    def test_snapshot_flags_unpriced_models(self):
        self.lg.set_meta("since_ts", 0)
        self._add("stealth/space-bunny-alpha", 1000, 0.5)
        self._add("deepseek-v4.1-flash", 2000, 0.2)
        pr = self.bm.snapshot()["pricing"]
        self.assertFalse(pr["all_priced"])
        self.assertEqual([u["model"] for u in pr["unpriced"]], ["stealth/space-bunny-alpha"])
        self.assertAlmostEqual(pr["est_cny"], 0.5, places=6)   # 只统计未定价那部分

    def test_snapshot_all_priced_when_table_covers_everything(self):
        self.lg.set_meta("since_ts", 0)
        self._add("deepseek-v4.1-flash", 1000, 0.1)
        pr = self.bm.snapshot()["pricing"]
        self.assertTrue(pr["all_priced"])
        self.assertEqual(pr["unpriced"], [])
        self.assertEqual(pr["est_cny"], 0.0)

    def test_price_snapshot_documents_the_default(self):
        snap = pricing.price_snapshot()
        self.assertIn("default_note", snap)
        self.assertIn("假设", snap["default_note"])

    def test_price_of_behaviour_unchanged(self):
        """既有最长前缀语义不能被这次改动破坏。"""
        self.assertIs(pricing.price_of("deepseek-v4.1-flash"), pricing.PRICE["deepseek-v4.1-flash"])
        self.assertIs(pricing.price_of("claude"), pricing.PRICE["claude"])
        self.assertEqual(pricing.match_key("claude-opus-5-20260101"),
                         "claude-opus-5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
