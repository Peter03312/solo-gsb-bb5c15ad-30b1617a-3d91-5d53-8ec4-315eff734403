# 博物馆展柜温度暴露审定服务

对博物馆展柜在指定**审定窗口**内采集到的温度读数进行暴露审定：按时间排序后，
以“每条读数只覆盖到下一条读数”的采样保持语义切分区段，识别 **正常 / 超限 / 未知**
区段，计算**最长连续超限时长**，并给出审定结论；支持读数幂等提交、修订替换、
**采样仪器按审定窗口批量补传（整批事务、互换时刻、重放不产生新修订）**、
**误采作废与恢复（作废留存修订：原因/操作时间/前一版本；更高修订号恢复）**、
**窗口审定规则版本化修订（目标规则修订号 + 四项完整新值 + 非空原因；
初始规则为第 1 版，目标号为当前版加 1 才生效，同号同内容重放幂等）**、
证书版本化签发（快照不可变），并可对同一审定窗口的两张已签发证书生成
**修订影响报告**（只读快照比对：四项审定规则前后值与变化、读数新增/变更/移除、
时间线状态变化半开区间、结论与最长连续超限时长变化；时间线分别依据两张
证书快照各自的规则与读数重建），并为每个窗口保存**按时间追溯的审定履历**
（创建时序号 0 基线；单条/批量读数提交、作废或恢复、规则修订每次生效后在
同一写事务内连续编号留痕；幂等重放、签发与失败请求不产生履历）。

- 技术栈：Python 3.11 · FastAPI · SQLite（WAL 模式）· Pydantic v2
- 无外部服务依赖，`docker compose up -d` 一键启动并自动初始化数据库

---

## 1. 审定规则

设审定窗口为 `[start_at, end_at]`，允许温度闭区间 `[min, max]`，
最长连续超限时长 `T_exceed`，最大采样间隔 `T_gap`。

### 区段语义

1. 读数按时间戳升序排列。第 `i` 条读数覆盖半开区间 `[t_i, t_{i+1})`，
   区间内温度取读数 `i` 的温度（采样保持，直到下一条读数）。
2. 相邻读数间隔 **严格大于** `T_gap` 时，该区间整段记为 **未知**；
   恰好等于 `T_gap` 仍算有效。
3. 窗口起点到首条读数、末条读数之后到窗口终点这两段**边界无读数**区域，记为 **未知**。
4. 有效区间内：温度超出 `[min, max]`（等于边界不算超限）记为 **超限**，否则 **正常**。
5. 相邻且状态相同的区段会合并输出。

> **关于窗口终点读数**：读数提交的窗口是闭区间 `[start_at, end_at]`，
> 允许恰好在 `end_at` 时刻提交一条读数，它只作为“覆盖终止采样”
> （结束上一条读数的覆盖，自身区段时长为 0）。这样当首读在起点、
> 末读在终点、所有采样间隔合法且温度全部在区间内时，可以得到没有
> 未知区段的“合格”结论；输出的温度区段始终是半开 `[start_at, end_at)`。

### 结论判定（按优先级）

| 条件 | 结论 | `verdict` |
|---|---|---|
| 最长连续超限时长 **严格大于** `T_exceed` | 不合格 | `fail` |
| 未超限（或恰好等于限值），但存在未知区段 | 待定 | `pending` |
| 无超限超时、无未知区段 | 合格 | `pass` |

“最长连续超限”指相邻的超限区段时长之和，被正常/未知区段打断后重新累计。
即使存在未知区段，只要超限超时，结论仍为不合格。

### 读数提交、幂等与修订

读数以 `(reading_id, revision)` 标识，同一窗口内：

- **相同 ID + 相同修订号重复提交**：内容（时间戳、温度）完全一致 → 幂等返回
  （`effect: idempotent`）；内容不一致 → **409 内容冲突**。
- **更高修订号**：替换该 ID 的当前读数并立即重算（`effect: updated`），
  旧修订号在历史表中标记为 `superseded`。
- **旧修订号**（低于当前修订号）→ **409 拒绝**。
- 同一窗口内不同读数不允许同一时间戳 → **422 拒收**。
- 时间戳超出 `[start_at, end_at]` → **422 拒收**；所有时间戳必须带时区。

### 批量补传（采样仪器）

`POST /api/v1/windows/{id}/readings/batch` 在**单个写事务**内整批提交一个
**非空**读数批次（请求体 `{"readings": [...]}`，单项结构与单条提交完全相同）：

- 每项沿用现有 `reading_id`、`revision`、`measured_at`、`temperature`，
  以及单条提交的时区、窗口闭区间边界、幂等/修订/冲突语义；
  **同批不得重复 `reading_id`**（否则 **422**）。
- 同刻唯一按**批次应用后的最终状态**校验，而不是按写入顺序逐条校验：
  因此允许两条读数都以更高修订号**互换采样时刻**（`a`、`b` 互相改到对方
  原时刻），不会因中间状态误拒；最终状态下仍同刻则 **422 拒收**。
- 任一项冲突（**409**：同 ID+修订号内容冲突、旧修订号覆盖）或无效
  （**422**：窗口外、缺时区、最终状态同刻、批内重复 ID 等）→
  **整批拒收**：活动读数与修订历史均不改变（沿用现有冲突与校验错误语义）。
- 成功时**一次提交全部变化**，响应逐项给出 `created` / `updated` /
  `idempotent` 及最终读数（顺序与请求一致），并返回**按最终状态一次性重算**
  的审定结果 `current_evaluation`；**同批重放不产生新修订**（全部
  `idempotent`，历史表不新增行）。
- 批量补传与单条提交、证书签发互不影响：已签发证书快照不可变，补传后
  再次签发形成新版本，旧版本内容保持不变。

### 误采作废与恢复

馆员确认某条记录为误采时，
`POST /api/v1/windows/{id}/readings/{reading_id}/void`
凭**递增修订号 + 非空原因**作废该 ID 的当前读数：

- 请求体 `{"revision": <高于当前修订号>, "reason": "<非空原因>"}`；
  作废本身作为该 ID 的一条修订（`status=voided`）**留存原因、操作时间
  （`voided_at`）及前一版本**（被作废读数的修订号/时刻/温度随作废行保存，
  响应 `void.previous` 返回），可经
  `GET /api/v1/windows/{id}/readings/{reading_id}/revisions` 查阅全部修订历史。
- 作废生效后当前审定**立即剔除该读数并在同一事务内重算**（响应含
  `current_evaluation`）；该读数不再出现在活动读数列表，其原时刻随之释放，
  可被其它读数使用。
- **幂等与冲突**（沿用现有 409 语义）：
  - 相同作废修订号 + 相同原因重放 → 幂等（`effect: idempotent`，不产生新修订）；
  - 相同作废修订号但原因不同 → **409**；作废修订号不高于当前修订号 → **409**；
  - 读数从未存在 → **404**；已作废的读数不能再次以新修订号作废 → **409**；
  - 原因为空或只含空白 → **422**。
- **恢复**：对该 ID 单条或批量提交**更高修订号**的读数即恢复
  （`effect: restored`），恢复同样遵守窗口闭区间边界与（批量）最终状态
  同刻唯一约束；**旧版提交不得复活**——不高于作废修订号的提交一律 **409**，
  批量中任一项冲突仍整批回滚（作废状态不变）。
- 作废/恢复与证书：已签发证书快照保持原样；作废或恢复后再次签发形成
  新版本，修订影响报告将该读数呈现为**移除（removed）**或**再出现（added）**。

### 窗口审定规则修订

馆员修订审定窗口的四项规则（允许温度上下限、最长连续超限时长、最大采样间隔）时，
`POST /api/v1/windows/{id}/rules/revisions`
提交**目标规则修订号 + 四项完整新值 + 非空原因**：

```json
{
  "target_revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 21.5,
  "max_continuous_exceed_seconds": 240,
  "max_sample_interval_seconds": 600,
  "reason": "季度复核：收紧温度上限与最长连续超限时长"
}
```

- **版本语义**：现有窗口的**初始规则视为第 1 版**；
  `target_revision` 为**当前版加 1** 才生效（`effect: revised`）；
  `target_revision` **等于当前版且四项规则值与原因均一致**的重放 →
  幂等（`effect: idempotent`，不留新记录）；
  同号但规则值或原因不一致、旧号、跳号提交 → **409** 且不留新记录。
- **校验**：数值须有限、温度下限不大于上限、两个时长为正、原因非空
  （不能只含空白）→ 否则 **422**；窗口不存在 → **404**。
- **生效**：修订记录**留存前后规则、原因与时间**（可经
  `GET /api/v1/windows/{id}/rules/revisions` 查阅全部规则版本，含初始第 1 版），
  并**按当前活动读数立即重算**（响应含 `current_evaluation`）；
  窗口起止时间与读数保持不变。
- **与证书**：后续签发采用新规则（快照记录 `rule_revision` 与四项规则值）；
  既有证书快照不变。跨规则版本的两张证书生成修订影响报告时，
  报告 `rules` 给出四项规则的前后值与变化（详见“修订影响报告”）。

### 签发与并发

- 签发把**当前已提交状态**（窗口参数 + 全部活动读数 + 审定结果）固化为不可变快照，
  形成一个证书版本（`version` 从 1 递增）。
- 读数修订后再次签发产生**新版本**；旧版本证书与快照永久保留、内容不变。
- 数据库开启 **WAL**，写事务使用 `BEGIN IMMEDIATE` + `busy_timeout`：
  签发（读）与修订（写）并发时，以事务为边界取同一已提交状态——
  修订先提交则纳入本次签发，否则等签发完成后再生效，不会出现脏读或快照撕裂。

### 审定履历（按时间追溯）

每个窗口保存一份**窗口内连续编号**的审定履历，供馆员按时间追溯审定的变化：

- **序号 0 基线**：窗口创建时，在同一写事务内以当时的初始规则与活动读数
  （建窗时为空）重算审定状态并保存（`detail.origin=window_created`）。
  **既有窗口升级**时，启动建表迁移会以窗口**当时的规则与活动读数**补建一条
  序号 0 基线（`detail.origin=backfilled`，操作时间取补建时刻），不补造任何
  更早的操作；已存在基线的窗口（含再次启动）幂等跳过。
- **生效即留痕**：单条读数提交（`created`/`updated`/`restored`）、误采作废
  （`voided`；恢复走单条/批量提交入口）、规则修订（`revised`）每次**实际
  生效后**，在**同一写事务**内追加一条履历，序号在窗口内连续递增；
  **批量补传整批只记一条**（逐项 effect 与最终读数入详情）。
- 每条履历记录：操作类型（`baseline` / `reading_submit` / `readings_batch`
  / `reading_void` / `rules_revision`）、涉及的**读数 ID** 或**规则版本**、
  操作**前后结论与最长连续超限时长**（附当时规则版本与活动读数条数），以及
  **前后状态不同的半开区段**（按前后时间线全部边界对齐，仅保留 `status`
  发生变化的 `[start, end)`，含变化前后状态与证据，规则同修订影响报告
  时间线；相邻同态区间合并）。**即使结论不变**，有效操作也记录。
- **不产生履历与序号**：幂等重放（读数/作废/规则修订内容一致重放、整批
  重放全部幂等）、**签发证书**、以及失败请求（409/422/404，事务回滚）。
- **并发**：写事务以 `BEGIN IMMEDIATE` 串行化，序号按事务**实际提交顺序**
  分配，连续无缺号；序号读取自同一写事务，随后续提交递增。
- **读取**：`GET /api/v1/windows/{id}/audit-history?start_seq=&limit=`
  按窗口与起始序号（含）升序返回，响应给出下一页起始序号 `next_start_seq`；
  窗口不存在 → **404**，起始序号非法（非整数/负数）或 `limit` 越界 → **422**。

---

## 2. 启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
docker compose logs -f          # 查看日志
docker compose down             # 停止
```

启动后自动建表初始化（幂等）。SQLite 数据保存在命名卷 `audit-data`
（容器内 `/data/audit.db`），容器重建不丢数据。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `./audit.db` | SQLite 数据库文件路径；Compose 中为 `/data/audit.db` |
| `AUDIT_DB_TIMEOUT` | `10` | 写锁等待秒数（对应 SQLite `busy_timeout` 毫秒数 ×1000） |
| `LOG_LEVEL` | `info` | 日志级别（透传给运行环境） |

### 地址与端口

- 服务地址：`http://localhost:8000`（Compose 映射 `8000:8000`，可在
  `docker-compose.yml` 的 `ports` 修改宿主机端口）
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

---

## 3. HTTP API

所有请求/响应均为 JSON；时间戳必须是带时区的 ISO-8601（如
`2026-10-01T00:00:00Z` 或 `2026-10-01T08:00:00+08:00`，统一按时刻处理并以 UTC 输出）。

### 窗口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows` | 创建审定窗口（窗口倒置/温度区间倒置返回 422） |
| `GET` | `/api/v1/windows?limit=&offset=` | 窗口列表（分页） |
| `GET` | `/api/v1/windows/{id}` | 窗口详情：活动读数 + 当前审定 + 最新证书 |
| `GET` | `/api/v1/windows/{id}/evaluation` | 只看当前审定结果 |
| `POST` | `/api/v1/windows/{id}/rules/revisions` | 修订审定规则：目标规则修订号 + 四项完整新值 + 非空原因；目标号为当前版加 1 生效，同号同内容重放幂等，响应含立即重算结果 |
| `GET` | `/api/v1/windows/{id}/rules/revisions` | 全部规则修订历史（含初始第 1 版；每版留存前后规则、原因与时间），按规则版本升序 |
| `GET` | `/api/v1/windows/{id}/audit-history?start_seq=&limit=` | 审定履历：按窗口与起始序号升序（序号 0 为建窗基线）；窗口不存在 404，非法序号 422 |

创建窗口请求体：

```json
{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}
```

窗口响应含当前规则版本 `rule_revision`（初始为 1）。规则修订请求体见上文
“窗口审定规则修订”，成功/幂等响应示例：

```json
{
  "effect": "revised",
  "rule_revision": {
    "window_id": 1,
    "revision": 2,
    "rules": {"allowed_temp_min": 18.0, "allowed_temp_max": 21.5,
              "max_continuous_exceed_seconds": 240.0,
              "max_sample_interval_seconds": 600.0},
    "previous_rules": {"allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
                       "max_continuous_exceed_seconds": 300.0,
                       "max_sample_interval_seconds": 600.0},
    "reason": "季度复核：收紧温度上限与最长连续超限时长",
    "created_at": "2026-10-01T08:00:00.000Z"
  },
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

### 审定履历

`GET /api/v1/windows/{id}/audit-history`（查询参数 `start_seq` 默认 0、含；
`limit` 默认 100、1~500）按窗口与起始序号升序返回履历。每条履历含窗口内
连续 `seq`（0 为基线）、`operation`、`occurred_at`（实际生效时间 UTC）、
涉及的 `reading_ids` / `rule_revisions`、操作 `detail`、前后状态摘要
`before` / `after`（结论、最长连续超限时长、规则版本、活动读数条数；基线的
`before` 为 `null`），以及前后状态不同的半开区段 `changed_segments`
（结构同修订影响报告的 `timeline.changes` 项）。尚有后续时响应给出
`next_start_seq`（末条序号 + 1），已到末尾为 `null`。

```json
{
  "window_id": 1,
  "start_seq": 0,
  "limit": 100,
  "entries": [
    {
      "seq": 0,
      "window_id": 1,
      "operation": "baseline",
      "occurred_at": "2026-10-01T00:00:00.123Z",
      "reading_ids": [],
      "rule_revisions": [],
      "detail": {"origin": "window_created"},
      "before": null,
      "after": {"verdict": "pending", "max_continuous_exceeded_seconds": 0.0,
                "rule_revision": 1, "reading_count": 0},
      "changed_segments": []
    },
    {
      "seq": 1,
      "operation": "reading_submit",
      "occurred_at": "2026-10-01T00:10:00.456Z",
      "reading_ids": ["b"],
      "rule_revisions": [],
      "detail": {"effect": "created", "reading": {"reading_id": "b", "...": "..."}},
      "before": {"verdict": "pending", "max_continuous_exceeded_seconds": 0.0,
                 "rule_revision": 1, "reading_count": 1},
      "after": {"verdict": "fail", "max_continuous_exceeded_seconds": 600.0,
                "rule_revision": 1, "reading_count": 2},
      "changed_segments": [
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T00:10:00Z",
         "duration_seconds": 600.0,
         "before": {"status": "unknown",
                    "evidence": {"unknown_reason": "boundary_gap",
                                 "boundary_side": "trailing",
                                 "left_reading_id": "a", "left_revision": 1}},
         "after": {"status": "exceeded",
                   "evidence": {"reading_id": "a", "revision": 1}}}
      ]
    }
  ],
  "next_start_seq": null
}
```

各类操作的 `operation` / `detail` 要点：

| 场景 | `operation` | `detail` 要点 | `reading_ids` / `rule_revisions` |
|---|---|---|---|
| 建窗基线 / 升级补建 | `baseline` | `origin=window_created` / `backfilled`，`before=null` | 均为空 |
| 单条提交生效 | `reading_submit` | `effect=created/updated/restored` + 最终读数 | 该读数 ID |
| 批量补传生效 | `readings_batch` | `results`：逐项 `effect` 与最终读数（整批一条） | 批内读数 ID（请求顺序） |
| 误采作废生效 | `reading_void` | 作废修订号、原因、前一版本 `previous` | 被作废读数 ID |
| 规则修订生效 | `rules_revision` | 目标/前一规则版本、前后四项规则值、原因 | 目标规则版本 |

> 幂等重放、签发证书、失败（409/422/404 回滚）请求不产生履历；批量仅当
> 至少一项实际生效时记一条（全部幂等的同批重放不留痕）。结论不变的有效
> 操作同样留痕（`changed_segments` 可能为空数组）。

最小调用示例：

```bash
# 全量读取（默认 start_seq=0&limit=100）
curl -sS "http://localhost:8000/api/v1/windows/1/audit-history"

# 从序号 5 开始每页 20 条；响应 next_start_seq 为下一页起点（null 表示到末尾）
curl -sS "http://localhost:8000/api/v1/windows/1/audit-history?start_seq=5&limit=20"
# 窗口不存在 -> 404；start_seq 为负数/非整数、limit 越界 -> 422
```

### 读数

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/readings` | 提交/幂等重放/修订一条读数，响应含重算结果；对已作废 ID 提交更高修订号即恢复（`effect: restored`） |
| `POST` | `/api/v1/windows/{id}/readings/batch` | 采样仪器补传：整批提交非空读数批次（整批事务），逐项返回 effect 与最终读数，响应含按最终状态重算的结果；批内可含恢复项 |
| `POST` | `/api/v1/windows/{id}/readings/{reading_id}/void` | 误采作废：凭递增修订号 + 非空原因作废当前读数，作废修订留存（原因/操作时间/前一版本），响应含剔除后重算结果 |
| `GET` | `/api/v1/windows/{id}/readings` | 当前活动读数列表（按时间升序；已作废读数不在其中） |
| `GET` | `/api/v1/windows/{id}/readings/{reading_id}/revisions` | 该读数 ID 的全部修订历史（含作废记录：原因/操作时间/前一版本修订号），按修订号升序；从未存在返回 404 |

读数请求体：

```json
{ "reading_id": "r-01", "revision": 1,
  "measured_at": "2026-10-01T00:10:00Z", "temperature": 22.4 }
```

单条提交响应中的 `effect` 为 `created` / `updated` / `restored` / `idempotent`
（`restored` 表示以更高修订号恢复了已作废的读数）。

批量补传请求体与响应：

```json
// 请求：{ "readings": [ <读数>, <读数>, ... ] }（至少 1 项，同批 reading_id 唯一）
{
  "results": [
    { "effect": "created", "reading": { "reading_id": "a", "revision": 1, "...": "..." } },
    { "effect": "updated",   "reading": { "reading_id": "b", "revision": 2, "...": "..." } },
    { "effect": "restored",  "reading": { "reading_id": "c", "revision": 3, "...": "..." } },
    { "effect": "idempotent","reading": { "reading_id": "d", "revision": 1, "...": "..." } }
  ],
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

误采作废请求体与响应：

```json
// 请求：POST /api/v1/windows/1/readings/r-03/void
// { "revision": 2, "reason": "误采：该时刻传感器未校准" }
{
  "effect": "voided",
  "void": {
    "reading_id": "r-03", "revision": 2, "status": "voided",
    "reason": "误采：该时刻传感器未校准",
    "voided_at": "2026-10-01T08:00:00.000Z",
    "previous": { "revision": 1, "measured_at": "2026-10-01T00:30:00Z", "temperature": 20.5 }
  },
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

### 审定结果结构

```json
{
  "verdict": "fail",
  "normal_segments":   [{"start":"...","end":"...","status":"normal",  "duration_seconds":60.0}],
  "exceeded_segments": [{"start":"...","end":"...","status":"exceeded","duration_seconds":420.0}],
  "unknown_segments":  [{"start":"...","end":"...","status":"unknown", "duration_seconds":3120.0}],
  "max_continuous_exceeded_seconds": 420.0,
  "max_exceed_seconds": 300.0,
  "has_exceeded": true,
  "has_unknown": true,
  "reading_count": 5
}
```

### 签发证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/certificates/issue` | 签发（可带 `{"note":"..."}`，也可空 body） |
| `GET` | `/api/v1/windows/{id}/certificates` | 该窗口全部证书版本（按版本升序） |
| `GET` | `/api/v1/certificates/{id}` | 按证书主键获取，`snapshot` 为签发时不可变快照 |
| `GET` | `/api/v1/certificates/revision-report?old_certificate_id=&new_certificate_id=` | 对同一窗口两张已签发证书生成修订影响报告 |

证书含 `id`、`window_id`、`version`、`verdict`、`verdict_label_zh`
（合格/待定/不合格）、`issued_at`、`note`、`snapshot`。

### 修订影响报告

输入**同一审定窗口**的两张**已签发**证书主键（旧版在前、新版在后），服务只读取
两证各自的**不可变快照**，不读取当前活动读数，输出：

- `rules`：两版快照各自留存的**审定规则**前后对照——`before`/`after`
  各含 `rule_revision`（规则版本，旧快照未记录时按初始第 1 版计）与四项规则值；
  `changed_fields` 列出四项规则中发生变化的字段，`rules_changed` 标识是否有
  规则变更。**即使审定结论未变，也可据此看出规则变更**。
- `readings_diff`：按 `reading_id` 对齐两版快照读数。
  - `added`：新版新增读数（含 `revision`/`measured_at`/`temperature`）；
  - `changed`：两版都存在但读数有变化的项，含 `before`/`after` 全量值与
    `changed_fields`（`revision` / `measured_at` / `temperature` 子集，
    采样时间按时刻比较，时区表示不同不视为变化）；
  - `removed`：新版消失的读数（两证签发之间该读数被误采作废；恢复后再次
    签发则体现为 `added`）；
- `timeline.changes`：用两版快照**各自的规则参数**与读数分别重建半开覆盖区段
  （与第 1 节同一条采样保持/间隔超限/边界空白规则），取两版**全部区段边界
  的并集**切分后，只保留**状态发生变化**的半开区间，每个区间给出：
  - `start`/`end`/`duration_seconds`（半开 `[start, end)`）；
  - `before`/`after`：变化前后 `status`（`normal`/`exceeded`/`unknown`）
    及该侧证据 `evidence`——
    - 已知区段：`{"reading_id", "revision"}`（采样保持该区段的读数）；
    - 未知区段含 `unknown_reason`：`sample_gap`（相邻读数间隔**严格超过**
      最大采样间隔，附 `left_*`/`right_*` 间隔两侧读数 ID 与修订号）或
      `boundary_gap`（边界无读数空白，附 `boundary_side`：`leading` 首读
      之前并给 `right_*` 首条读数、`trailing` 末读之后并给 `left_*` 末条
      读数、`empty` 整窗无读数两侧均无引用）。
  - 相邻且变化前后**状态与证据均相同**的区间自动合并（被无变化区间隔开的
    不合并）。
- `summary`：审定结论变化（`verdict_before/after`、中文标签、
  `verdict_direction` 改善/恶化/持平）、最长连续超限时长前后值与增量
  `max_continuous_exceeded_seconds_delta`、规则版本前后值
  （`rule_revision_before/after`）与 `rules_changed`、时间线变化区间数，
  以及一段中文汇总结论 `conclusion_zh`（含规则前后值描述）。

> 时间线只输出**状态**变化；若未知原因由“边界空白”变为“采样间隔超限”
> （或反之）但两侧都仍为 `unknown`，该区间状态未变、不进入 `timeline.changes`，
> 但该区间在两版快照中对应的未知来源可经上面的证据规则重建。

请求与拒绝约定：

- 查询参数 `old_certificate_id`、`new_certificate_id` 均为证书主键、必填正整数；
- 任一证书不存在 → `404`；
- 两 ID 相同、两证不属于同一窗口、或旧版 `version` 不严格小于新版
  （版本未按先后排列）→ `422`，响应 `detail` 说明具体原因；
- 报告是只读 GET，不改变任何既有内容；既有证书结构、查询响应与审定规则
  保持不变（仅新增端点与响应模型）。

报告响应示例（fail → pass 整改）：

```json
{
  "old_certificate": {"id": 1, "window_id": 1, "version": 1, "verdict": "fail", "...": "..."},
  "new_certificate": {"id": 2, "window_id": 1, "version": 2, "verdict": "pass", "...": "..."},
  "rules": {
    "before": {"rule_revision": 1, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "after":  {"rule_revision": 1, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "changed_fields": [],
    "rules_changed": false
  },
  "readings_diff": {
    "added": [], "removed": [],
    "changed": [{
      "reading_id": "b",
      "before": {"reading_id": "b", "revision": 1, "measured_at": "2026-10-01T00:01:00Z", "temperature": 23.0},
      "after":  {"reading_id": "b", "revision": 2, "measured_at": "2026-10-01T00:01:00Z", "temperature": 20.0},
      "changed_fields": ["revision", "temperature"]
    }],
    "added_count": 0, "removed_count": 0, "changed_count": 2
  },
  "timeline": {
    "max_sample_interval_seconds": 600.0,
    "change_count": 2,
    "changes": [{
      "start": "2026-10-01T00:01:00Z", "end": "2026-10-01T00:07:00Z",
      "duration_seconds": 360.0,
      "before": {"status": "exceeded", "evidence": {"reading_id": "b", "revision": 1}},
      "after":  {"status": "normal",   "evidence": {"reading_id": "b", "revision": 2}}
    }]
  },
  "summary": {
    "verdict_before": "fail", "verdict_after": "pass",
    "verdict_direction": "改善", "verdict_changed": true,
    "max_continuous_exceeded_seconds_before": 420.0,
    "max_continuous_exceeded_seconds_after": 0.0,
    "max_continuous_exceeded_seconds_delta": -420.0,
    "max_exceed_seconds_limit": 300.0,
    "timeline_change_count": 2,
    "rule_revision_before": 1, "rule_revision_after": 1,
    "rules_changed": false,
    "conclusion_zh": "审定结论：不合格（fail）→ 合格（pass），改善。最长连续超限时长：420s → 0s（减少 420s；限值 300s）。……"
  }
}
```

跨规则版本的报告（两证签发之间发生过规则修订）中，`rules.changed_fields`
列出变化的规则字段、`summary.conclusion_zh` 给出前后值描述，时间线按两版
各自规则重建——例如收紧最大采样间隔后，原本正常的区段在新版时间线中变为
未知（`normal → unknown`，证据 `unknown_reason=sample_gap`）。

### 错误约定

- `404`：窗口/证书不存在（修订影响报告中任一证书缺失）；
  作废或查询修订历史时读数从未存在；规则修订时窗口不存在；
  查询审定履历的窗口不存在
- `409`：同 ID+修订号内容冲突；旧修订号覆盖（单条与批量语义一致）；
  作废修订号原因冲突或不高于当前修订号；已作废读数再次以新修订号作废；
  以不高于作废修订号的提交尝试复活读数；
  规则修订目标号同号但规则值/原因不一致、旧号或跳号（均不留新记录）
- `422`：无效窗口、窗口外读数、同刻不同读数（批量按最终状态判定）、
  批量请求为空批次/同批 `reading_id` 重复、缺时区/字段非法、
  作废原因为空或只含空白、规则修订数值非有限/温度下限大于上限/
  时长非正/原因为空等；
  修订影响报告的两证书相同、跨审定窗口、版本未按先后排列也返回 422；
  审定履历 `start_seq` 为负数/非整数、`limit` 越界也返回 422

> 批量补传中任一项命中 409/422，整批回滚，错误 `detail` 指出具体原因；
> 不会出现“批内前几项已生效、后几项失败”的部分写入。

---

## 4. 调用示例

服务启动后：

```bash
# curl 全流程（创建窗口 → 提交/幂等/冲突/修订 → 审定 → 两次签发 → 修订影响报告 → 批量补传 → 误采作废与恢复 → 规则修订 → 审定履历查询）
bash examples/curl-examples.sh

# Python 全流程（仅标准库，演示三种结论、拒收、修订、版本快照、修订影响报告、批量补传、误采作废与恢复、规则修订、审定履历）
python3 examples/python_client.py
# 或指定地址：
BASE_URL=http://127.0.0.1:8000 python3 examples/python_client.py
```

修订影响报告最小示例（`OLD_ID`、`NEW_ID` 为同一窗口两张已签发证书主键）：

```bash
curl -sS "http://localhost:8000/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID"
```

最小示例：

```bash
curl -sS http://localhost:8000/api/v1/windows \
  -H 'Content-Type: application/json' -d '{
    "start_at":"2026-10-01T00:00:00Z","end_at":"2026-10-01T00:10:00Z",
    "allowed_temp_min":18,"allowed_temp_max":22,
    "max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600}'

curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

curl -sS http://localhost:8000/api/v1/windows/1/evaluation
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"首签"}'
```

批量补传最小示例（互换采样时刻：`a`、`b` 同时以更高修订号改到对方原时刻）：

```bash
# 先有 a@00:00 rev1、b@00:10 rev1，再整批互换时刻（按最终状态判同刻，顺序无关）
curl -sS http://localhost:8000/api/v1/windows/1/readings/batch \
  -H 'Content-Type: application/json' -d '{
    "readings":[
      {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
      {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.2}
    ]}'
# 原样重放同一批次 -> results 全部 idempotent，不产生新修订
```

误采作废与恢复最小示例（承接上文窗口 1 与读数 `a` rev2）：

```bash
# 作废：更高修订号 + 非空原因；当前审定立即剔除该读数并重算
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/void \
  -H 'Content-Type: application/json' \
  -d '{"revision":3,"reason":"误采：该时刻传感器未校准"}'
# 相同修订号+相同原因重放 -> effect=idempotent；原因不同/低修订号 -> 409；
# 读数从未存在 -> 404；已作废再以新修订号作废 -> 409

# 查阅该 ID 全部修订历史（含作废记录：原因/操作时间/前一版本）
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/revisions

# 恢复：单条（或批量）提交更高修订号 -> effect=restored
curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":4,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1}'
# 旧版提交（revision <= 作废修订号）不得复活 -> 409

# 作废/恢复后再次签发形成新版本；修订影响报告将该读数呈现为 removed / added
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"作废/恢复后重签"}'
```

窗口审定规则修订最小示例（承接上文窗口 1，初始规则为第 1 版）：

```bash
# 修订：目标号=当前版+1（2），四项完整新值 + 非空原因；立即按当前读数重算
curl -sS http://localhost:8000/api/v1/windows/1/rules/revisions \
  -H 'Content-Type: application/json' -d '{
    "target_revision": 2,
    "allowed_temp_min": 18.0, "allowed_temp_max": 21.5,
    "max_continuous_exceed_seconds": 240, "max_sample_interval_seconds": 600,
    "reason": "季度复核：收紧温度上限与最长连续超限时长"}'
# 相同目标号+相同内容重放 -> effect=idempotent（不留新记录）；
# 同号不同内容/原因、旧号、跳号 -> 409；非法值/空原因 -> 422；窗口不存在 -> 404

# 查阅全部规则版本（含初始第 1 版；每版留存前后规则、原因与时间）
curl -sS http://localhost:8000/api/v1/windows/1/rules/revisions

# 规则修订后再次签发采用新规则；跨规则版本报告的 rules 块展示四项规则
# 前后值与变化（即使结论未变也可看出规则变更）
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"规则修订后重签"}'
```

---

## 5. 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest tests/ -q
```

- `tests/test_evaluation.py`：审定算法纯函数测试（未知/超限/合格、
  边界等于、时区归一、区段无缝拼接等）。
- `tests/test_report.py`：修订影响报告纯函数测试（读数差异、带证据
  区段、边界对齐、未知来源、区间合并、汇总方向、规则前后值与变化）。
- `tests/test_api.py`：HTTP 集成测试（幂等、冲突、修订替换、拒收、
  证书版本快照、签发/修订并发串行化）。
- `tests/test_batch_api.py`：批量补传 HTTP 集成测试（整批创建/混合 effect、
  窗口外/同刻/重复 ID 整批原子回滚、409 冲突与旧修订号、活动表与历史表不变、
  更高修订号互换时刻及顺序无关、重放不产生新修订、已签发快照不变、时区归一）。
- `tests/test_void_api.py`：误采作废与恢复 HTTP 集成测试（作废剔除并立即重算、
  作废修订留存原因/操作时间/前一版本、同修订号同原因重放幂等、原因不同或
  低修订号 409、从未存在 404、已作废再次作废 409、空原因 422、单条/批量
  更高修订号恢复、恢复遵守窗口边界与最终状态同刻唯一、旧版提交不得复活、
  批量冲突整批回滚、证书快照不变与报告呈现 removed/added）。
- `tests/test_rules_api.py`：窗口审定规则修订 HTTP 集成测试（初始规则第 1 版、
  目标号=当前版+1 生效并留存前后规则/原因/时间且立即重算、同号同内容重放
  幂等、同号冲突/旧号/跳号 409 且不留新记录、非法值与空原因 422、窗口不存在
  404、窗口起止时间与读数不变、后续签发采用新规则且既有快照不变、跨规则版本
  报告分别按两证快照计算时间线并展示四项规则前后值与变化）。
- `tests/test_report_api.py`：修订影响报告 HTTP 测试（fail→pass、
  边界空白 vs 采样间隔超限、只依快照、跨窗口/倒序/同 ID/不存在拒绝）。
- `tests/test_audit_api.py`：审定履历 HTTP 集成测试（建窗序号 0 基线、
  单条 created/updated/restored、作废、批量只记一条（含部分幂等/全幂等/
  整批回滚）、规则修订（含收紧间隔的状态变化区段）、结论不变也留痕、
  幂等重放/签发/失败请求不留痕、按起始序号读取与分页、窗口不存在 404、
  非法序号 422、既有窗口升级补建基线不伪造更早操作、并发写入序号连续）。

## 6. 目录结构

```
app/
  config.py        # 环境变量配置
  database.py      # SQLite 连接、WAL、建表（含旧库补列迁移与初始规则/履历基线补建）、写事务（BEGIN IMMEDIATE）
  evaluation.py    # 核心审定算法（纯函数）
  report.py        # 修订影响报告（规则前后对照、快照比对、时间线对齐，纯函数）
  audit.py         # 审定履历（序号 0 基线、生效操作同事务连续留痕、按窗口/序号读取；复用时间线对齐）
  models.py        # 结论枚举/常量
  schemas.py       # Pydantic 请求/响应模型
  services.py      # 窗口、读数（含误采作废/恢复）、规则修订、审定、签发、修订影响报告业务逻辑
  main.py          # FastAPI 路由
examples/
  curl-examples.sh
  python_client.py
tests/
Dockerfile
docker-compose.yml
requirements.txt
```
