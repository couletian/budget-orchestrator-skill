# -*- coding: utf-8 -*-
"""预算账本：SQLite 持久化。

设计要点
- WAL 模式：watcher 持续写入的同时面板可以并发读取。
- requests.uid 去重：同一请求被重复扫描只计一次（uid 优先取 providerData.messageId）。
- files 表保存每个 jsonl 的读取游标（增量 tail），重启后不丢进度。
- meta 表保存预算配置、控制状态（暂停/降级）等运行时状态。
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS requests(
  uid            TEXT PRIMARY KEY,
  ts             REAL NOT NULL,
  session_id     TEXT,
  agent          TEXT,
  src_file       TEXT,
  model          TEXT,
  input_tokens   INTEGER DEFAULT 0,
  output_tokens  INTEGER DEFAULT 0,
  cache_hit      INTEGER DEFAULT 0,
  cache_write    INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0,
  total_tokens   INTEGER DEFAULT 0,
  is_peak        INTEGER DEFAULT 0,
  cost_cny       REAL DEFAULT 0,
  task_id        TEXT,
  tool           TEXT,
  target         TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_ts   ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_sess ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_req_task ON requests(task_id);

CREATE TABLE IF NOT EXISTS files(
  path       TEXT PRIMARY KEY,
  offset     INTEGER DEFAULT 0,
  size       INTEGER DEFAULT 0,
  mtime      REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks(
  id            TEXT PRIMARY KEY,
  parent        TEXT,
  name          TEXT,
  kind          TEXT DEFAULT 'task',
  budget_tokens INTEGER,
  budget_cny    REAL,
  status        TEXT DEFAULT 'pending',
  progress      REAL DEFAULT 0,
  note          TEXT,
  priority      INTEGER DEFAULT 3,
  created_at    REAL,
  updated_at    REAL
);

CREATE TABLE IF NOT EXISTS events(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL,
  level   TEXT,
  kind    TEXT,
  message TEXT,
  data    TEXT
);

CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

DEFAULT_META = {
    "budget_tokens": "0",       # 0 = 未设限
    "budget_cny": "0",
    "soft_pct": "70",           # 软阈值（警告）
    "hard_pct": "100",          # 硬阈值（喊停）
    "reserve_pct": "15",        # 预留给监督/协调的份额
    "state": "running",         # running | paused | stopped
    "mode": "auto",             # auto(自主协调) | observe(只观测) | enforce(超预算硬停等人)
    "active_task": "root",
    "workspace": "",
    "currency": "CNY",
    "created_at": "",
}


class Ledger:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.readonly = False
        self._lock = threading.RLock()
        self._con: Optional[sqlite3.Connection] = None
        self._init()

    # ── 基础 ──────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False：watcher 线程写、面板线程读，共用一条连接，
        # 用下面的 RLock 串行化（sqlite3 默认禁止跨线程）。
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    @contextlib.contextmanager
    def conn(self):
        """复用一条**常驻连接**，成功提交、异常回滚、由 close() 显式关闭。

        为什么常驻而不是每次开关
          实测本机 Windows：每次新建连接会让 WAL 反复建立/checkpoint/删除，
          单次操作要 91–125 ms；复用常驻连接只要 6 ms（约 15–20 倍差距）。
          账本处于面板 SSE 与 watcher 的高频路径上，这个差距直接影响实时性。

        为什么要显式 close
          sqlite3.Connection 会参与**引用环**，仅靠引用计数回收不掉 ——
          不 gc 时 `os.replace(ledger.db, ...)` 必报
          `WinError 32 另一个程序正在使用此文件`（实测：手动 gc.collect() 后才成功）。
          表现为 `reset` 备份改名失败。close() 会真正关掉连接，句柄立刻还给系统。
        """
        with self._lock:
            if self._con is None:
                self._con = self._connect()
            try:
                yield self._con
                self._con.commit()
            except Exception:
                try:
                    self._con.rollback()
                except Exception:
                    pass
                raise

    def close(self):
        """归并 WAL 并**真正关闭**连接，释放文件句柄。

        测试、以及需要删除/改名账本文件的场景（Windows 会拒绝操作被占用的文件）
        都要调用它。关闭后 WAL 被清空，句柄释放，`ledger.db` 可安全移动；
        之后再调用其他方法会自动重开连接，不会因关闭而不可用。
        """
        with self._lock:
            if self._con is None:
                return
            try:
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._con.commit()
            except Exception:
                pass
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None

    def _drop_conn(self):
        """丢弃常驻连接（只读降级/出错时用），不抛异常。"""
        try:
            if self._con is not None:
                self._con.close()
        except Exception:
            pass
        self._con = None

    def _init(self):
        # 初始化写操作全部容错：账本可能位于只读路径（如受限沙盒），
        # 此时应降级为"只读可用"而不是直接抛异常——观测能力不该因写不进而丧失。
        self.readonly = False
        try:
            with self.conn() as c:
                c.executescript(SCHEMA)
                # 轻量迁移：老库补上后加的列（已存在则忽略）
                for tbl, col, typ in (("requests", "tool", "TEXT"), ("requests", "target", "TEXT"),
                                      ("tasks", "priority", "INTEGER")):
                    try:
                        c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError:
                        pass
        except sqlite3.OperationalError:
            self.readonly = True
            self._drop_conn()      # 别让只读路径上的连接一直占着句柄
            return
        try:
            for k, v in DEFAULT_META.items():
                if k == "created_at":
                    v = time.strftime("%Y-%m-%d %H:%M:%S")
                self.set_meta_if_absent(k, str(v))
            # 确保根任务存在
            if not self.get_task("root"):
                self.upsert_task({"id": "root", "parent": None, "name": "总任务", "kind": "root"})
        except sqlite3.OperationalError:
            self.readonly = True
            self._drop_conn()

    # ── meta ──────────────────────────────────────────────────────────────
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any):
        with self.conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def set_meta_if_absent(self, key: str, value: Any):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    def all_meta(self) -> dict:
        with self.conn() as c:
            return {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM meta")}

    # ── requests ──────────────────────────────────────────────────────────
    def add_request(self, rec: dict) -> bool:
        """插入一条请求记录；重复 uid 返回 False。"""
        cols = ("uid", "ts", "session_id", "agent", "src_file", "model", "input_tokens",
                "output_tokens", "cache_hit", "cache_write", "reasoning_tokens",
                "total_tokens", "is_peak", "cost_cny", "task_id", "tool", "target")
        vals = tuple(rec.get(k) for k in cols)
        with self.conn() as c:
            cur = c.execute(
                f"INSERT OR IGNORE INTO requests({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", vals)
            return cur.rowcount > 0

    def totals(self, task_id: Optional[str] = None, since: Optional[float] = None) -> dict:
        """汇总用量。

        since 是**预算口径**的一部分：预算按任务计，所以"已用"必须只统计
        统计起点（meta.since_ts）之后的请求。否则上一轮任务的历史请求会继续
        累加进来，面板就会出现"已用 4090%"这种数字。
        传 0 / None 表示不设起点（全量，供 `task list` 这类"终身用量"视图使用）。
        """
        q = ("SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) it, "
             "COALESCE(SUM(output_tokens),0) ot, COALESCE(SUM(cache_hit),0) ch, "
             "COALESCE(SUM(total_tokens),0) tt, COALESCE(SUM(cost_cny),0) cost "
             "FROM requests")
        where, args = [], []
        if task_id:
            where.append("task_id=?")
            args.append(task_id)
        if since:
            where.append("ts>=?")
            args.append(float(since))
        if where:
            q += " WHERE " + " AND ".join(where)
        with self.conn() as c:
            r = c.execute(q, tuple(args)).fetchone()
        return dict(r)

    def by_model(self, since: float = 0) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT model, COUNT(*) n, COALESCE(SUM(total_tokens),0) tt, "
                "COALESCE(SUM(cost_cny),0) cost FROM requests WHERE ts>=? "
                "GROUP BY model ORDER BY tt DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def by_session(self, since: float = 0) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT session_id, COUNT(*) n, COALESCE(SUM(total_tokens),0) tt, "
                "COALESCE(SUM(cost_cny),0) cost, MIN(ts) t0, MAX(ts) t1 "
                "FROM requests WHERE ts>=? GROUP BY session_id ORDER BY tt DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def by_agent(self, since: float = 0) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT COALESCE(agent,'main') agent, COUNT(*) n, "
                "COALESCE(SUM(total_tokens),0) tt, COALESCE(SUM(cost_cny),0) cost "
                "FROM requests WHERE ts>=? GROUP BY agent ORDER BY tt DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def by_hour(self, since: float = 0) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT ts, total_tokens, cost_cny, model, is_peak FROM requests "
                "WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 40) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT ts, model, input_tokens, output_tokens, cache_hit, total_tokens, "
                "cost_cny, is_peak, agent, task_id FROM requests ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── 游标 ──────────────────────────────────────────────────────────────
    def file_offset(self, path: str) -> int:
        with self.conn() as c:
            r = c.execute("SELECT offset FROM files WHERE path=?", (path,)).fetchone()
        return int(r["offset"]) if r else 0

    def file_cursor(self, path: str) -> tuple:
        """返回 (offset, size, mtime)，用于检测截断/轮转。"""
        with self.conn() as c:
            r = c.execute("SELECT offset,size,mtime FROM files WHERE path=?", (path,)).fetchone()
        return (int(r["offset"]), int(r["size"]), float(r["mtime"])) if r else (0, 0, 0.0)

    def set_file_cursor(self, path: str, offset: int, size: int, mtime: float):
        with self.conn() as c:
            c.execute(
                "INSERT INTO files(path,offset,size,mtime,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,size=excluded.size,"
                "mtime=excluded.mtime,updated_at=excluded.updated_at",
                (path, offset, size, mtime, time.time()))

    def cursor_lag(self) -> dict:
        """监听健康度：已读游标距文件真实末尾还差多少字节。

        用途：回答"看板是不是卡住了"。lag=0 说明监听已把日志读完，
        此时面板没变化只可能是**宿主还没把新行落盘**（日志按消息粒度追加，
        不是连续的），而不是本工具采集滞后；lag 持续很大才是采集问题。
        """
        rows = []
        total = 0
        for r in self.conn_files():
            p, off, size, mt = r["path"], int(r["offset"] or 0), int(r["size"] or 0), float(r["mtime"] or 0)
            try:
                real = os.path.getsize(p)
            except OSError:
                real = -1
            lag = (real - off) if real >= 0 else 0
            total += max(0, lag)
            rows.append({"path": p, "offset": off, "size": size,
                         "real_size": real, "lag": lag, "mtime": mt,
                         "exists": real >= 0})
        rows.sort(key=lambda x: -x["lag"])
        newest = max([r["mtime"] for r in rows], default=0.0)
        return {"files": len(rows), "lag_bytes": total,
                "newest_mtime": newest, "detail": rows[:5]}

    def conn_files(self) -> list:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT path,offset,size,mtime FROM files")]

    # ── tasks ─────────────────────────────────────────────────────────────
    def upsert_task(self, t: dict):
        now = time.time()
        prio = t.get("priority")
        with self.conn() as c:
            c.execute(
                "INSERT INTO tasks(id,parent,name,kind,budget_tokens,budget_cny,status,"
                "progress,note,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET parent=excluded.parent,name=excluded.name,"
                "kind=excluded.kind,budget_tokens=excluded.budget_tokens,"
                "budget_cny=excluded.budget_cny,status=excluded.status,"
                "progress=excluded.progress,note=excluded.note,"
                "priority=COALESCE(excluded.priority,tasks.priority),"
                "updated_at=excluded.updated_at",
                (t.get("id"), t.get("parent"), t.get("name"), t.get("kind", "task"),
                 t.get("budget_tokens"), t.get("budget_cny"), t.get("status", "pending"),
                 t.get("progress", 0), t.get("note"), 3 if prio is None else prio, now, now))

    def get_task(self, tid: str) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def list_tasks(self) -> list:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def delete_task(self, tid: str):
        with self.conn() as c:
            c.execute("DELETE FROM tasks WHERE id=? AND kind!='root'", (tid,))
            c.execute("UPDATE requests SET task_id='root' WHERE task_id=?", (tid,))

    # ── events ────────────────────────────────────────────────────────────
    def add_event(self, kind: str, message: str, level: str = "info", data: Any = None):
        with self.conn() as c:
            c.execute("INSERT INTO events(ts,level,kind,message,data) VALUES(?,?,?,?,?)",
                      (time.time(), level, kind, message,
                       json.dumps(data, ensure_ascii=False) if data is not None else None))

    def events(self, limit: int = 200) -> list:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── 工具维度（供精简审计）──────────────────────────────────────────────
    def by_tool(self, since: float = 0, limit: int = 30) -> list:
        with self.conn() as c:
            rows = c.execute(
                "SELECT COALESCE(tool,'(未标注)') tool, COUNT(*) n, "
                "COALESCE(SUM(total_tokens),0) tt, COALESCE(SUM(input_tokens),0) it, "
                "COALESCE(SUM(output_tokens),0) ot, COALESCE(SUM(cost_cny),0) cost "
                "FROM requests WHERE ts>=? GROUP BY tool ORDER BY tt DESC LIMIT ?",
                (since, limit)).fetchall()
        return [dict(r) for r in rows]

    def repeat_targets(self, since: float = 0, tools=("Read", "Write", "Edit"),
                       min_count: int = 2, limit: int = 20) -> list:
        """同一目标被同一工具重复操作的次数与关联消耗（实测事实，非估算节省量）。"""
        ph = ",".join("?" * len(tools))
        with self.conn() as c:
            rows = c.execute(
                f"SELECT tool, target, COUNT(*) n, COALESCE(SUM(total_tokens),0) tt, "
                f"COALESCE(SUM(input_tokens),0) it, MIN(ts) t0, MAX(ts) t1 "
                f"FROM requests WHERE ts>=? AND target IS NOT NULL AND target<>'' "
                f"AND tool IN ({ph}) GROUP BY tool, target HAVING COUNT(*)>=? "
                f"ORDER BY n DESC, tt DESC LIMIT ?",
                (since, *tools, min_count, limit)).fetchall()
        return [dict(r) for r in rows]

    def top_requests(self, since: float = 0, by: str = "input_tokens", limit: int = 15) -> list:
        col = by if by in ("input_tokens", "output_tokens", "total_tokens", "cost_cny") else "input_tokens"
        with self.conn() as c:
            rows = c.execute(
                f"SELECT ts, model, tool, target, input_tokens, output_tokens, "
                f"total_tokens, cost_cny FROM requests WHERE ts>=? "
                f"ORDER BY {col} DESC LIMIT ?", (since, limit)).fetchall()
        return [dict(r) for r in rows]

    def io_ratio(self, since: float = 0) -> dict:
        with self.conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(input_tokens),0) it, COALESCE(SUM(output_tokens),0) ot, "
                "COALESCE(SUM(reasoning_tokens),0) rt, COUNT(*) n FROM requests WHERE ts>=?",
                (since,)).fetchone()
        it, ot = r["it"], r["ot"]
        return {"input": it, "output": ot, "reasoning": r["rt"], "requests": r["n"],
                "out_per_in": (ot / it * 100.0) if it else 0.0}
