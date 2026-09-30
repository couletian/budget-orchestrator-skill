# 操作记录（Operations Log）

> 用途：记录本工具在开发与安装过程中，对 WorkBuddy 底层文件的**每一次**访问与改动，
> 保证任何操作都可追溯、可回滚。
>
> 原则：**只读来源绝不写入；新增目录不覆盖既有文件；修改既有文件前先备份。**

---

## A. 只读访问（未产生任何修改）

开发期间为摸清数据来源与平台能力，读取过以下位置。**全部为只读**（`ls` / `cat` / 读取 JSON /
`grep` / `stat`），未写入、未移动、未删除任何文件：

| 路径 | 目的 |
|---|---|
| `~/.workbuddy/skills/token-dashboard/**` | 复用其价格表与解析口径（**本工具的上游依据**） |
| `~/.workbuddy/projects/**/*.jsonl` | 采集 usage 的数据源（**只读 tail**） |
| `~/.workbuddy/settings.json` | 查看结构与 hook 配置是否存在 |
| `~/.workbuddy/usage-log.json` | 确认其仅为 skill 使用记录、非 token 数据 |
| `~/.workbuddy/mcp.json` | 查看既有 MCP server |
| `<WorkBuddy安装目录>/resources/app.asar.unpacked/cli/**` | 确认 CLI 参数与端口冲突根因 |
| `~/.workbuddy/logs/`、`~/.codebuddy/` | 诊断子进程挂死原因 |

数据来源声明：本工具对 `~/.workbuddy/projects/**/*.jsonl` **只做读取**（以只读方式打开、
按字节游标 seek + read），从不写入、追加、重命名或删除。

---

## B. 新增文件（不覆盖任何既有内容）

| 新增路径 | 说明 | 回滚方式 |
|---|---|---|
| `<工作区>/budget-orchestrator/` | 项目源码目录（开发期） | 直接删除该目录 |
| `~/.workbuddy/skills/budget-orchestrator/` | 安装后的 skill 目录 | 删除该目录 |
| `~/.workbuddy/budget-orchestrator/` | 运行时数据（`ledger.db` + `-shm` + `-wal`、`panel.port`、`daemon.pid`、`daemon.log`、`backup/`） | 删除该目录 |

以上均为**新建目录**，安装前会先检查目标是否已存在；若已存在则**不覆盖**，改为提示用户。

---

## C. 修改既有文件

| 路径 | 操作 | 备份 | 回滚 |
|---|---|---|---|
| `~/.workbuddy/mcp.json` | 新增 `mcpServers.budget-orchestrator` 一项（**仅追加一个键，其余内容原样保留**） | 备份为 `~/.workbuddy/mcp.json.bak-boc-<日期>` | 用备份文件覆盖回去，或手工删掉该键 |

写入方式：读取原文件 → 解析 JSON → 在 `mcpServers` 下新增一个键 → 整体写回（保留原有键值）。
**不改动、不重排、不删除任何既有配置。**

### C-2. 本 skill 自身文件的后续更新

源码工作副本位于工作区 `budget-orchestrator/`，安装副本位于
`~/.workbuddy/skills/budget-orchestrator/`，两者内容一致。
更新方式为 `shutil.copytree(src, dst, dirs_exist_ok=True)` **覆盖文件**，
不删除目录、不触碰 `~/.workbuddy` 下任何其他文件。

安装副本始终可由工作区源码完整重建，因此无需单独备份；
若需回到旧版本，用工作区的 git/副本覆盖回去即可。

### C-3. 运行时数据目录的清理

`~/.workbuddy/budget-orchestrator/ledger.db*` 为本工具**自建**的账本文件，
可安全删除（会重新生成，仅丢失历史统计）。

**账本归零的正确姿势**（推荐用内建命令，它已内建下面两条纪律）：

```bash
bo.py daemon stop          # ① 必须先停守护进程
bo.py reset                # ② 停 daemon + 备份三件套 + 重新起算（一步到位）
bo.py reset --keep-budget  # 只清历史用量，保留预算设置与任务树
```

手工做也可以，但**必须连同 `-shm` / `-wal` 一起备份改名**：

```bash
cd ~/.workbuddy/budget-orchestrator
S=$(date +%Y%m%d-%H%M%S); mkdir -p backup
[ -f ledger.db     ] && mv ledger.db     "backup/ledger-$S.db"
[ -f ledger.db-shm ] && mv ledger.db-shm "backup/ledger-$S.db-shm"
[ -f ledger.db-wal ] && mv ledger.db-wal "backup/ledger-$S.db-wal"
```

| 操作 | 影响 | 恢复 |
|---|---|---|
| 只搬走 `ledger.db`（留下 `-wal`/`-shm`） | **危险**：SQLite 为 WAL 模式，最新提交可能还在 `-wal` 里 | 把三件套一起放回原位 |
| **回滚时只放回主库、不处理残留 `-wal`** | **数据丢失**：SQLite 会把新账本的 `-wal` 当成自己的日志重放，空账本的页覆盖掉刚放回的旧数据（已由 `test_reset_preserves_rollback_data` 固定） | 回滚前先删掉现存的 `ledger.db-wal` / `ledger.db-shm`，再整体放回 |
| 三件套一起备份改名（`ledger.db` + `-shm` + `-wal`） | 从零起算，历史保留在 `backup/` 可回查 | 把 `backup/` 里的三件套改回原名 |
| **不停 daemon 就改名** | Windows 下守护进程持有句柄 → `PermissionError`（`WinError 32`） | `bo.py daemon stop` 后重试（`bo.py reset` 会自动先停） |
| 删除 `ledger.db*` | 丢失历史用量统计 | 无法恢复历史；重新 `run` 后从当前时刻继续记账 |
| 删除 `panel.port` / `daemon.pid` / `daemon.log` | 无实质影响 | 下次启动自动重建 |

> 更轻的归零方式：`bo.py run --since now` 只把**统计起点**推到此刻，
> 历史仍在库里可查。预算与"已用"都按该起点统计，因此百分比立刻归零。
>
> `bo.py reset` 的产物命名：`backup/ledger-<YYYYmmdd-HHMMSS>.db[-shm|-wal]`；
> 它会先停 daemon、改名失败即报错中止（不静默半搬）、收工时主动关闭连接释放句柄。

---

## D. 安全边界

- 面板默认只绑定 `127.0.0.1`（本机可访问），不对外网暴露。
- MCP server 走本地 stdio，不联网、不外发数据。
- 本工具不含遥测；`ledger.db` 全在本机。
- 面板的"导出 JSON"由用户主动点击，不会自动上传。

---

## E. 卸载步骤

```bash
<python> <skill>/bo.py daemon stop          # 1) 停守护
rm -rf ~/.workbuddy/skills/budget-orchestrator   # 2) 删 skill
rm -rf ~/.workbuddy/budget-orchestrator          # 3) 删运行时数据
# 4) 从 ~/.workbuddy/mcp.json 中移除 budget-orchestrator 键（或用备份覆盖）
```

卸载**不影响** WorkBuddy 本体，也不影响 `~/.workbuddy/projects` 下的原始日志。
