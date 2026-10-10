# 契约：已记住的放行（面板 ↔ 分级插件）

主人对管家说「以后都允许」时，分级插件 `guanjia-guard` 往线上分级表写一条放行规则；管家面板（语音进程控制口 `127.0.0.1:18795`）负责列出和撤销。两边只通过这个文件交接，不互相调用。

## 文件

- 路径：`C:\Users\inksn\.openclaw\workspace\guanjia\levels.json`（插件每次工具调用都重新读，改了立即生效，不用重启网关）。
- 编码 UTF-8，写入时不带 BOM；读取时要容忍 BOM（主人可能用记事本另存过）。
- 只动顶层 `overrides` 数组，其它键（`_说明`、`defaultLevel`、`tools`、`rules` …）原样保留。
- 排版：顶层每键一行，`tools` 每个工具一行，`rules`、`overrides` 每条一行。插件的 `formatLevelsJson` 和 Python 的 `format_levels` 输出逐字节相同。排版不影响含义，只是让主人手看文件时不乱。

## 一条「已记住」的放行

```json
{ "id": "rem-1a2b3c4d5e6f", "source": "remembered", "createdAt": "2026-10-10T13:00:00.000Z", "expiresAt": "9999-12-31T00:00:00.000Z", "scope": "global", "tool": "exec", "param": "command", "pattern": "^\\s*shutdown\\s+[\\\\/]s\\s+[\\\\/]t\\s+3600\\s*$", "level": "L1", "summary": "exec: shutdown /s /t 3600", "reason": "主人口头说以后都允许" }
```

| 字段 | 含义 |
|---|---|
| `id` | `rem-` + 12 位十六进制（由 tool/param/pattern 算出，同一件事记两次是同一个 id，插件不会重复加） |
| `source` | 固定 `remembered`。**面板只许删这种**；没有 `source` 或别的值是主人手写的，面板只读不删 |
| `createdAt` | 记住的时间（ISO，UTC） |
| `expiresAt` | 固定 `9999-12-31T00:00:00.000Z`，表示一直有效直到撤销 |
| `tool` / `param` / `pattern` | 只记 `exec` 的 `command`。一般是只匹配这条命令本身的正则（空白多少、斜杠方向不算差别）；结束进程按程序名记，`summary` 写成「结束进程：程序名」 |
| `level` | 固定 `L1`（直接做） |
| `summary` | 给人看的一行，面板列表就显示它 |

读密钥文件、改管家自己的规则这两类，插件不会写成「已记住」（每次都问）。

## 写入规则（插件和面板都遵守）

1. 读最新的文件；读不出来（缺失、JSON 坏了）就**不写**，返回错误——不能拿默认表或空表覆盖主人手改过的文件。
2. 只改 `overrides`。
3. 写到同目录临时文件（插件用 `levels.json.<pid>.<时间>.tmp`，面板用 `levels.json.panel-<pid>-<时间>.tmp`，不会撞名），再原子替换（Node `fs.renameSync`、Python `os.replace`）。Windows 上对方恰好开着文件时替换可能报权限错误，重试最多 5 次、每次间隔 50 毫秒。
4. 两边同时写的概率极低（都是人说话/点按钮的速度）；万一撞上，后写的一方覆盖前一方的 `overrides` 改动，结果是"刚记住的又没了"或"刚撤销的又回来"，再说一次或再点一次即可。不加文件锁。

## 面板接口（由面板计划实现在 `control.py`，鉴权沿用 P2.1：同源 Origin + 令牌）

| 方法 | 路径 | 请求 | 回应 |
|---|---|---|---|
| GET | `/api/remembered?token=…` | — | `200 {"ok": true, "items": [{"id", "summary", "tool", "param", "pattern", "createdAt", "expiresAt"}, …]}`（按 `createdAt` 先后）；文件读不出来 `200 {"ok": false, "reason": "unreadable", "items": []}` |
| POST | `/api/remembered/revoke` | 请求头 `X-Guanjia-Token`；`{"id": "rem-…"}` | `200 {"ok": true, "removed": "rem-…"}`；`404 {"ok": false, "reason": "not_found"}`；`403 {"ok": false, "reason": "not_remembered"}`（id 对上了但不是口头记住的）；`409 {"ok": false, "reason": "unreadable"}` |

参考实现：`src/guanjia/remembered_overrides.py` 的 `list_remembered(path)`、`revoke(id, path)`（返回 `(True, "removed")` / `(False, "not_found" | "not_remembered" | "unreadable")`）、`format_levels(obj)`；用例 `tests/test_perm_remembered.py`。面板计划直接调用这两个函数，不要自己再写一份读写逻辑。

## 上线同步

以后每次上线新的默认分级表，都用 `.venv\Scripts\python.exe -m guanjia.remembered_overrides sync-defaults F:\coding\guanjia\openclaw\plugins\guanjia-guard\defaults\levels.json`：线上表换成仓库默认表，线上已有的 `overrides`（已记住的和主人手写的）原样保留。**不要直接复制默认表覆盖线上文件**，那会把主人记住的放行全部清掉。线上文件读不出来时命令报错退出，不会写。`... list` 打印已记住的条目（每行一个 JSON）。

## 审计里与此相关的 decision（面板「工具/审批」方框可以据此着色）

插件写 `workspace\guanjia\audit.jsonl`，`kind` 为 `tool`：

| decision | 含义 | 有 `tool` 字段 |
|---|---|---|
| `allow` | 直接做（`source` 为 `override` 时带 `overrideId`，说明是按已记住的规则放行） | 有 |
| `block-need-confirm` | 先问（带 `category`） | 有 |
| `allow-confirmed` | 主人口头同意后放行 | 有 |
| `block-no-confirmer` | 定时任务/心跳里遇到先问的事，没做 | 有 |
| `block-refused` | tool_call 的真实目标无法确定，拒绝 | 有 |
| `l2-confirmed` / `l2-remembered` / `l2-remember-refused` / `l2-remember-failed` / `l2-dropped` | 主人回话后的处理结果（`l2-remembered` 带 `overrideId`；都带 `voice` 表示是不是语音轮） | 没有 |

P2.1 的 `audittail.py` 只认带 `tool` 字段的行，`block-refused` 会显示成默认的 info 状态；面板计划可以在 `DECISION_STATE` 里把它加成 `blocked`。`approval-*` 这组 decision 在 v2 之后不会再出现。
