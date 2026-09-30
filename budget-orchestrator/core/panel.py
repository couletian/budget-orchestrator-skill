# -*- coding: utf-8 -*-
"""可视化面板服务：HTTP + SSE 实时推送 + 控制接口。

路由
  GET  /                面板单页（web/index.html）
  GET  /api/state       当前状态快照（JSON）
  GET  /api/stream      SSE 实时流（默认 1s 推送一次）
  GET  /api/pricing     价格表快照
  GET  /api/export      导出全量 JSON（审计/归档）
  POST /api/control     控制动作：pause/resume/stop/add_budget/set_budget/shutdown_panel/...
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .budget import BudgetManager
from .ledger import Ledger
from . import pricing

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def make_server(ledger: Ledger, host: str = "127.0.0.1", port: int = 8787):
    bm = BudgetManager(ledger)
    index_path = os.path.join(WEB_DIR, "index.html")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "BOC/1.0"

        def log_message(self, *a):  # 静音
            pass

        # ── 工具 ─────────────────────────────────────────────────────
        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        # ── GET ──────────────────────────────────────────────────────
        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                try:
                    with open(index_path, "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"panel html missing", "text/plain; charset=utf-8")
                return
            if p == "/api/state":
                self._json(bm.snapshot())
                return
            if p == "/api/pricing":
                self._json(pricing.price_snapshot())
                return
            if p == "/api/check":
                self._json(bm.check())
                return
            if p == "/api/export":
                snap = bm.snapshot(series_minutes=1440, events_limit=1000)
                snap["exported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._json(snap)
                return
            if p == "/api/stream":
                self._stream()
                return
            self._json({"error": "not found"}, 404)

        # ── SSE ──────────────────────────────────────────────────────
        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    snap = bm.snapshot()
                    payload = json.dumps(snap, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        # ── POST ─────────────────────────────────────────────────────
        def do_POST(self):
            p = self.path.split("?")[0]
            if p != "/api/control":
                self._json({"error": "not found"}, 404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            action = (body.get("action") or "").strip()
            kwargs = {k: v for k, v in body.items() if k != "action"}
            if action == "shutdown_panel":
                res = self._shutdown_panel()
            elif action in ("task_add", "task_update", "task_delete", "task_clear"):
                res = self._task_action(action, kwargs)
            else:
                res = bm.control(action, **kwargs)
            self._json(res)

        def _shutdown_panel(self):
            """手动彻底关闭面板：先落标记再退出进程。

            标记 `panel.disabled` 会让看守进程停止重拉（尊重用户意志）；
            之后执行 `bo.py daemon start` / `bo.py respawn` 会清除标记并正常唤起。
            """
            data_dir = os.path.dirname(ledger.path)
            marker = os.path.join(data_dir, "panel.disabled")
            try:
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            except OSError:
                pass
            try:
                ledger.add_event("panel", "面板被手动彻底关闭（可用 daemon start / respawn 重新唤起）",
                                 level="warn")
            except Exception:
                pass
            # 先让响应发出去、日志落盘，再退出进程
            threading.Timer(0.6, lambda: os._exit(0)).start()
            return {"ok": True, "shutdown": True, "marker": marker}

        def _task_action(self, action, kw):
            if action == "task_add" or action == "task_update":
                tid = kw.get("id")
                if not tid:
                    return {"ok": False, "error": "id required"}
                existing = ledger.get_task(tid) or {}
                ledger.upsert_task({
                    "id": tid,
                    "parent": kw.get("parent", existing.get("parent") or "root"),
                    "name": kw.get("name", existing.get("name") or tid),
                    "kind": kw.get("kind", existing.get("kind") or "task"),
                    "budget_tokens": kw.get("budget_tokens", existing.get("budget_tokens")),
                    "budget_cny": kw.get("budget_cny", existing.get("budget_cny")),
                    "status": kw.get("status", existing.get("status") or "pending"),
                    "progress": kw.get("progress", existing.get("progress") or 0),
                    "note": kw.get("note", existing.get("note")),
                })
                ledger.add_event("task_" + action, f"{tid} {kw.get('name','')}")
                return {"ok": True}
            if action == "task_delete":
                ledger.delete_task(kw.get("id"))
                return {"ok": True}
            if action == "task_clear":
                for t in ledger.list_tasks():
                    if t["kind"] != "root":
                        ledger.delete_task(t["id"])
                return {"ok": True}
            return {"ok": False, "error": "unknown"}

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv


def serve(ledger: Ledger, host="127.0.0.1", port=8787, block=True):
    srv = make_server(ledger, host, port)
    if block:
        srv.serve_forever()
    else:
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="boc-panel")
        t.start()
    return srv
