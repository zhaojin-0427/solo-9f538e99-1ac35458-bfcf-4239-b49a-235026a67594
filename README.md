# 单浮标潮位校时服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。
另外接收整批联测资料，把各开机段不同标尺的潮位换算到海图零点。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；更正校时点或覆盖观测解除冲突后自动恢复。
  同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，旧数据照常供数。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新）都在**单个 SQLite 事务**中
完成，并由进程级锁串行化读写：原文、校时依据、分析供数同次生效，
查询和（崩溃后的）重启都不会看到半次重算的中间态。

## 海图零点换算

不同开机段的潮位可能来自不同标尺，整编海图前要统一到海图零点。
联测资料（海图零点基准名、各开机段所用标尺、最多 16 条联测记录）
通过 `PUT /survey` **整批替换**。

- **联测记录**：`connection_id`（唯一编号）、`from_datum`（起点基准）、
  `to_datum`（终点基准）、`delta_mm`（从起点换到终点要加的毫米数，整数）。
  反向使用时取反。
- **换算**：在基准名构成的无向图上寻找「该段标尺 → 海图零点」的路线，
  把路线累计高差（毫米）加到以米计的原潮位上，返回换算潮位与沿途
  联测编号。历史观测也按当前资料换算。
- **待联测**（`pending_survey`）：该段未登记标尺，或标尺到海图零点无路线。
- **待复测**（`pending_resurvey`）：存在两条**各自不重复经过基准点**的
  路线，且累计高差不同。此时返回两条路线的联测编号与高差，不给换算值。
- **旁支闭环**：只接在一个基准点上的闭环会产生重复基准点的路线，
  不是简单路线，因此不影响换算。
- 未校时（`pending_calibration`）或冲突（`conflict`）的观测不进入
  换算查询，与 `/tide-levels` 的可用口径一致。

联测资料三张表与观测同在 SQLite，资料替换是单个事务：替换失败（校验
或写入错误）整批回滚、旧资料照常供数；查询在只读事务快照中进行，
补传、校时与资料替换交错时，每次查询只看到同一已提交状态的观测与
联测资料。资料与观测都落盘，重启后仍可查询。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34}
  ]
}
```

### `POST /calibrations` — 分批提交校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

两个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测 ID**
（含新观测，以及被卷入/解除冲突的其他观测）：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}]
}
```

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用原潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。潮位为原始值，
不做海图零点换算（换算见 `/chart-tide-levels`）。

### `PUT /survey` — 整批替换联测资料

```json
{
  "chart_datum": "chart-0",
  "staffs": [
    {"boot_segment": "boot-1", "staff": "staff-A"},
    {"boot_segment": "boot-2", "staff": "staff-B"}
  ],
  "connections": [
    {"connection_id": "s1", "from_datum": "staff-A", "to_datum": "staff-B", "delta_mm": 120},
    {"connection_id": "s2", "from_datum": "staff-B", "to_datum": "chart-0", "delta_mm": -35}
  ]
}
```

- 三部分一次性整体替换旧资料；`connections` 最多 16 条，`connection_id`
  必须唯一（否则 `422`，旧资料不变）。`staffs`、`connections` 可省略。
- 写入失败整批回滚，旧资料照常供数；资料落盘，重启后仍在。
- 响应回显已提交的资料，附 `staff_count` / `connection_count`。
- `GET /survey` 查看当前资料。

### `GET /chart-tide-levels?start_ms=...&end_ms=...` — 换算到海图零点

按 UTC 范围逐条返回可用观测（`status=ok`）及其换算结果：

```json
{
  "count": 1,
  "chart_datum": "chart-0",
  "items": [
    {
      "obs_id": "obs-1", "utc_ms": 1700000000500,
      "status": "ok", "reason": null,
      "original_tide_level": 2.34,
      "tide_level": 2.425, "chart_tide_level": 2.425, "delta_mm": 85,
      "connection_ids": ["s1", "s2"],
      "routes": []
    }
  ]
}
```

- `status=ok`：`tide_level`（同 `chart_tide_level`）为原潮位加路线累计高差，
  `connection_ids` 为沿途联测编号。
- `status=pending_survey`（待联测）：未登记标尺或无路线，换算值为 `null`。
- `status=pending_resurvey`（待复测）：两条简单路线高差不同，换算值为
  `null`，`routes` 给出两条路线的 `connection_ids` 与 `delta_mm`：

```json
{
  "obs_id": "obs-2", "utc_ms": 1700000000600,
  "status": "pending_resurvey", "reason": "……待复测",
  "original_tide_level": 2.5, "tide_level": null, "chart_tide_level": null,
  "connection_ids": [],
  "routes": [
    {"connection_ids": ["d1"], "delta_mm": 100},
    {"connection_ids": ["d2a", "d2b"], "delta_mm": 130}
  ]
}
```

### `GET /observations/{obs_id}` — 按观测 ID 查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py    # FastAPI 路由与请求/响应模型
  core.py    # 校时换算、批量写入、重算、冲突检测、变更快照
  survey.py  # 联测资料替换、基准网路线、海图零点换算
  db.py      # SQLite 连接、模式、事务与锁
tests/
  test_api.py     # 校时、冲突、原子性
  test_survey.py  # 联测资料、海图零点换算
Dockerfile
docker-compose.yml
```
