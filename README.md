# Budget Orchestrator

给任务设一个 token / 金额预算，让ai**在这个预算内尽量做得好**，并且全程看得见、随时能打断。

---

## 1. 它是什么

一套**旁路实时预算监督系统**，由四部分组成：

```
~/.workbuddy/projects/**/*.jsonl          (WorkBuddy 实时写入的真实 usage，只读)
            │
            ▼
   ┌─────────────────┐     ┌──────────────┐     ┌─────────────────┐
   │ Watcher 监听器   │ ──► │ Ledger 账本   │ ──► │ Budget 策略      │
   │ 增量 tail + 去重 │     │ SQLite(WAL)  │     │ 阈值/预测/降级    │
   └─────────────────┘     └──────────────┘     └────────┬────────┘
                                                          │
                        ┌─────────────────────────────────┼──────────────┐
                        ▼                                 ▼              ▼
                  CLI (bo.py)                     HTTP+SSE 面板      MCP Server
                  给 agent / 脚本调用              给人看 / 点         给 agent 直接调
```

- **采集**：`core/watcher.py` 增量读取每个 jsonl 的新增字节，解析 `providerData.rawUsage`，
  按 `messageId` 去重。文件被轮转/截断会自动回退。半行不消费。
- **计价**：`core/pricing.py` **逐行移植 token-dashboard** 的价格表与峰谷规则
  （高峰＝工作日 9–12/14–18；空闲＝高峰价 ×50%；Claude 系缓存写入 ×1.25）。
- **账本**：`core/ledger.py`，SQLite + WAL，watcher 写、面板读互不阻塞。
- **策略**：`core/budget.py`，软/硬阈值、速率与预计触顶、降级阶梯（YAGNI 优先）、暂停状态机。
- **瘦身**：`core/lean.py`，把 ponytail 的 YAGNI 阶梯与输出纪律变成可测量项（见 §5）。
- **交互**：`core/panel.py` + `web/index.html`，SSE 每秒推送；`mcp_server.py` 暴露 9 个工具。

## 2. 前期验证结果

| 验证项 | 方法 | 结果 |
|---|---|---|
| 解析口径与 token-dashboard 一致 | 同一工作区，对比 token / 请求数 / 输入 / 输出 / 缓存命中 | **逐项完全相等** |
| 增量与去重 | 重复扫描不重复计数；追加只读新增 | 通过 |
| 半行容错 | 写入到一半的行不被消费，补全后正常入库 | 通过 |
| 子代理归属 | `<ws>/<sid>/subagents/agent-*.jsonl` → `sub:xxxx` | 通过 |
| 峰谷计价 | 工作日/午休/夜间/周末/法定假日 6 组用例 | 通过 |
| 缓存写入倍率 | Claude 系 ×1.25 | 通过 |
| 阈值与拦截 | ok→warn→over→STOP；enforce 自动暂停；追加后自动恢复 | 通过 |
| 任务隔离 | 子任务预算独立计量 | 通过 |
| 精简审计 | 返工 / 整体重写 / 上下文膨胀 / 输出冗长 / 工具热点 检出 | 通过 |
| 诚实边界 | 审计输出不含"省了/节省"类无基线措辞（专项测试守护） | 通过 |

`python tests/test_boc.py` → **23 tests OK**（纯标准库，无需依赖）。

## 3. 快速开始

```bash
PY="C:/Users/Miracle R/.workbuddy/binaries/python/versions/3.11.9/python.exe"
SK="<skill目录>/bo.py"

"$PY" "$SK" daemon start --tokens 3000000 --cny 20 --open   # 启动（后台）+ 拉起看板
      # 加了 --open 会尝试用系统浏览器打开；不加则手动访问打印出的地址
      # 用 skill 时：启动后必须把看板交给用户（present_files 打开，或首行报出端口）
"$PY" "$SK" status                                   # 查看
"$PY" "$SK" url                                      # 面板地址
"$PY" "$SK" daemon stop                              # 停止
```

## 4. 与 ponytail 的融合

`ponytail` 是套"逼你把代码写少"的编程技能：**YAGNI 阶梯**（先问要不要做，再谈怎么写）+
**输出纪律**（代码优先、解释≤3 行）+ 三档强度（lite/full/ultra）+ `ponytail:` 债务标注。

它和省 token 的关系是**供给侧**的：同一个任务用更少消耗做完。本工具是**需求侧**的：
计量、告警、喊停。两者互补，融合点有四处：

| 融合点 | 做法 |
|---|---|
| **降级第一动作** | 原降级阶梯第一条是"压缩上下文"，现改为**先爬 YAGNI 阶梯**——"别做/别重写"永远比"省着做"更省 |
| **档位自动切换** | 预算已用比例 → 自动映射到 normal / lite / full / ultra，`check`、`status`、面板同步显示当期纪律 |
| **浪费可测量** | `core/lean.py` 从账本指出返工、上下文膨胀、输出冗长、工具热点，每条标明对应阶梯第几级 |
| **纪律写进协议** | SKILL.md 的瘦身纪律章节作为 agent 的硬约束；面板加"瘦身审计"卡片实时可见 |

**为什么"浪费可测量"是本融合最实在的一块**：jsonl 里同一条记录**既是 LLM 请求（带 usage）
又是工具调用（带 `name` 与 `argumentsDisplayText`）**，所以把消耗归因到"哪个工具、哪个目标"
是**零额外 IO** 的。实测效果（用本工具审计它自己的开发过程）：

```
[高] 阶梯第 7 级 · rework
    证据：budget-orchestrator\bo.py 被 Edit 操作 4 次，关联消耗 82.59万 token
    建议：连续小步修改说明需求没想透；先读完再一次性改完

[中] 阶梯第 2 级 · context_bloat
    证据：最大单次输入 24.36万 token（Bash：cd "D:/WYX 2/workbuddy-working space/202）
    建议：改用 Grep 定位后只读相关片段；长文件分段处理
```

**沿用 ponytail-gain 的诚实边界**：`lean` **绝不输出"你省了 N token"** ——
没写出来的版本不存在，没有基线可减。它只报可数的事实（重复次数、关联消耗、异常大请求）。
这条纪律有单元测试守着（`test_never_fabricates_savings`），防止日后被"优化"掉。

**没有采纳的**：`ponytail:` 债务注释的自动收集（那是代码仓库层面的账，
与 token 预算无关，按 YAGNI 跳过）；也不做"省下的 token 折算成钱"，理由同上。

## 5. 目录结构

```
budget-orchestrator/
  SKILL.md              skill 定义 + agent 协作协议
  bo.py                 统一 CLI（init/run/watch/daemon/status/check/budget/control/mode/task/report/url）
  mcp_server.py         MCP stdio server（8 个工具）
  core/
    pricing.py          价格表 + 峰谷规则 + 模型名归一（移植自 token-dashboard）
    ledger.py           SQLite 账本
    watcher.py          增量监听器
    budget.py           预算策略与控制面（降级阶梯以 YAGNI 开头）
    lean.py             精简审计（ponytail 融合：YAGNI 阶梯 + 输出纪律）
    panel.py            HTTP + SSE 面板服务
  web/index.html        面板单页（无外部依赖，深/浅色）
  tests/test_boc.py     单元测试
  docs/OPERATIONS.md    对 WorkBuddy 底层文件的操作记录与回滚说明
```

## 6. AI 自主协调为主（默认模式）

**设计立场**：预算约束应该由 AI 自己执行，而不是每超一点就停下来等人批准。

```
budget 用量 ─► autopilot 引擎 ─► 可执行动作清单 ─► AI 自行执行 ─► 继续推进
                     │
                     └─► 仅当"降级阶梯用尽且主干未完成"才 ─► escalate 请人
```

三种决策（`bo.py autopilot` 的退出码 / `boc_autopilot` 的返回）：

| mode | 退出码 | 含义 | AI 的动作 |
|---|---|---|---|
| `proceed` | 0 | 预算充裕 | 正常推进 |
| `adapt` | 3 | 需要降级 | **执行 actions 后继续**，不打断 |
| `escalate` | 4 | 阶梯用尽 | 此时才请人追加预算 |

`adapt` 产出的动作是结构化的、可直接执行的，且每条标明对应 YAGNI 阶梯级别：

```json
{"id": "skip_optional", "type": "task", "rung": 6,
 "instruction": "暂停或跳过非主干子任务，把剩余额度全部留给主干交付物。",
 "params": {"skip_task_ids": ["T2"], "keep_task_ids": ["T1"]}}
```

**取舍的依据是任务优先级与额度**：

```bash
bo.py task --cny 0.045 --priority 5 add T1 --name "核心功能"   # 额度/优先级旗标写在动作词之前
```

不标优先级，工具就无法判断该砍谁；**不标额度，`plan` 会把它列为 `unbudgeted`
并判定 `tight`** —— 预算不够时同样无从取舍。两者都要给。

> 旗标重复会被 argparse 静默覆盖（后者生效），且额度**必须**有值 —— 这两点都实测踩过，
> 见 SKILL.md 的「子任务优先级与额度」一节。

**人工的位置**（保留，但不再是主路径）：随时 `pause/stop`、`budget --add-cny` 追加、
`task prio` 改重要性；`mode enforce` 可切回"超预算就硬停等人"的保守模式。

**模式对照**：`auto`（默认·自主协调） / `observe`（只观测不干预） / `enforce`（硬停等人）。

## 7. 已知限制

- 无法拦截 LLM 调用，只能事后秒级计量（见 4.3）。
- 金额为牌价粗估，非实际账单-必须在使用前手动更新计价表
- 面板需守护进程存活；`daemon stop` 后面板不可访问（数据仍在 `ledger.db`）。
- 峰谷规则内置 2026 年节假日表，跨年需在 `core/pricing.py` 追加。
- 预算按任务计：只统计 `since_ts` 之后的请求。跨任务复用同一账本时，
  想重新起算用 `bo.py run --since now`（保留历史），想彻底归零用 `bo.py reset`。
