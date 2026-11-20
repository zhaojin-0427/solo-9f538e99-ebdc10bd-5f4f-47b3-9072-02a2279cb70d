# 统计区改划 · 新旧区人数精确换算服务

区划调整后，历年按旧区公布的人数需要换算到新区。本服务用两个独立的
FastAPI 实例完成这件事，全程使用有理数精确运算（`fractions.Fraction`），
不使用浮点数。

## 数学模型

设街块人数为未知量 x，0/1 关联矩阵：

- `A`（街块 × 旧区）：旧区人数 `p = A·x`
- `B`（街块 × 新区）：新区人数 `q = B·x`
- 同一街块可同时属于多个区（矩阵列允许重叠）

由此得到两类精确代数关系：

1. **旧区之间的等式**：`ker(A)` 中的每个向量 k 满足 `k·p = 0`；
2. **新区对旧区的表达式**：对每个新区列 bⱼ 解 `A·c = bⱼ`，
   有解则 `qⱼ = c·p`，系数为精确分数（如 `1/2`、`-3`）。

## 两个实例

| 实例 | 端口 | 接口 | 职责 |
|---|---|---|---|
| package-builder | 8001 | `POST /build`，文档 `/docs` | 输入新旧区号及各自覆盖街块，输出 JSON 交接包 |
| converter | 8002 | `POST /convert`、`/archives/*`，文档 `/docs` | 输入交接包与本次有数的旧区人数，输出新区人数；并提供年鉴核对与存档 |

### 交接包内容（不含任何街块编号或逐块归属）

```json
{
  "schema_version": 1,
  "old_districts": ["O1", "O2"],
  "new_districts": ["N1"],
  "old_relations": [{"O1": 1, "O2": -1}],
  "new_district_relations": [
    {"code": "N1", "derivable": true, "coefficients": {"O1": "1/2", "O2": "1/2"}}
  ]
}
```

- `old_relations`：旧区人数必须满足的精确线性等式（加权和为 0），
  整数系数，已约分为本原向量；
- `new_district_relations`：每个新区一个条目，`derivable=false` 表示
  覆盖关系无法用旧区精确表达，`coefficients` 为 `"p/q"` 形式的精确分数
  （整数写作 `"2"`）。

### 换算规则（converter）

- **可算范围只按本次提交的旧区数据判定**：仅当新区表达式能（借助交接包
  内的旧区关系）改写为只含本次有数旧区时才计算，其余新区列入 `missing`，
  并注明 `not_expressible_from_old_districts` 或
  `insufficient_known_old_districts`；
- 本次有数旧区所支撑的每条旧区等式都会实际校验，结果放在
  `checked_relations`；等式涉及无数据旧区时自动跳过；
- 旧区人数必须是非负整数（请求体 schema 强制）；
- **整次失败**（HTTP 400，只返回错误体）的情形：
  - 旧区人数违背覆盖关系推出的等式；
  - 任一新区换算结果为负数；
  - 任一新区换算结果不是整数（分数系数导致的半整数等）。

成功响应示例：

```json
{
  "available_old_districts": ["O1", "O2"],
  "checked_relations": [],
  "computed": [
    {"code": "N1", "status": "computed", "population": 8,
     "coefficients": {"O1": "1/2", "O2": "1/2"},
     "expression": "1/2*O1 + 1/2*O2", "missing_reason": null}
  ],
  "missing": [],
  "results": [ /* computed 与 missing 的完整列表，顺序与新区一致 */ ]
}
```

## 年鉴核对与存档（converter 实例）

年鉴从同一张旧区人口表摘录历年数据，表头里的两个区号可能写反。以下接口
挂在换算服务（8002）上，与 `/convert` 并存，全部可在
<http://localhost:8002/docs> 交互调用。存档写入磁盘，重启容器后仍可按档号查询。

### 提交待核稿表 `POST /archives/{档号}/submissions`

请求体：

```json
{
  "package": { /* builder 产出的 v1 交接包，原样提交 */ },
  "table": {
    "header": ["O1", "O2", "O3"],
    "rows": [
      {"period": "2018", "values": [22, 12, null]},
      {"period": "2019", "values": [31, 9, 22]}
    ]
  }
}
```

- **表头必须与包内旧区号一一对应**：不多、不少、不重复；否则 422
  `invalid_table`。列顺序可以与包内顺序不同，等式按区号对齐。
- 每格填**非负整数**或**留空**；留空（`null` 或 `""`）表示该期缺报。
- `table` 也接受 `values` 二维数组、行对象列表、以及首行为表头的 CSV
  文本（`"period,O1,O2,O3\n2018,22,12,10\n"`）。
- 返回当次**修订号**（首次为 1）。同档号重复提交返回 409
  `archive_exists`——补报请走 supplements。

### 核对规则

每期只用**该期实报**的旧区，校验**全部可检验的旧区等式**：

- 包内每条旧区等式（`old_relations`）在其涉及的旧区全部有数时校验；
- 还会校验**多条等式合成**出来的关系：凡是由包内等式线性组合而成、
  且只涉及该期实报旧区的等式都算可检验（`synthesized=true`）。
  例如 O1 - O2 = 0 与 O1 - O3 = 0 可推出 O2 - O3 = 0，即使该式不在包内。

响应中的 `review`：

```json
{
  "contradiction_found": true,
  "message": "contradiction found in period(s): 2018",
  "periods": [
    {"period": "2018", "reported_districts": ["O1","O2","O3"],
     "missing_districts": [],
     "checked_relations": [{"relation": {"O1":1,"O2":1,"O3":-1},
       "expression": "O1 + O2 - O3", "value": 2, "satisfied": false,
       "synthesized": false, "source_relation_indices": [0]}],
     "violations": [ /* 同上，未通过者 */ ],
     "satisfied": false}
  ],
  "violations": [],
  "candidates": [{"first": "O1", "second": "O3"}],
  "candidate_unique": true,
  "can_confirm": true
}
```

- **原表通过**：`contradiction_found=false`、`message="no contradiction
  found"`、`candidates=[]`，`can_confirm=false`（无需确认）。
- **原表有矛盾**：`candidates` 列出**能让各期都通过的全部列对**。判定方法
  是每次尝试都在**每一期互换同一对列**（缺报单元格随列一起移动），
  只有所有期都通过才算候选；候选可能是 0 个或多个。

### 补报 `POST /archives/{档号}/supplements`

```json
{"table": { /* 整表替换 */ }, "revision": 1}
```

- 整表替换待核稿表，**沿用原包**（可选 `package` 回显；若给出必须与原包
  完全一致，否则 409 `package_mismatch`）。返回**新版核对结果**与新修订号。
- 可选 `revision` 为期望的当前修订号；不一致返回 409 `revision_mismatch`。
- 已确认的档号不能再补报：409 `archive_confirmed`。

### 确认 `POST /archives/{档号}/confirmations`

```json
{"revision": 1, "pair": {"first": "O1", "second": "O3"}}
```

- `revision` 必须是**所见那次核对结果**的修订号。若其后又有补报，旧修订
  不能再确认：409 `revision_stale`（响应带当前 `revision`）。
- **候选唯一时**凭所见修订确认，当次返回修正表：

```json
{
  "archive_id": "A-1", "revision": 1, "status": "confirmed",
  "swapped_pair": {"first": "O1", "second": "O3"},
  "corrected_table": {"header": ["O1","O2","O3"],
    "rows": [{"period": "2018", "values": [10, 12, 22]}]},
  "review": { /* 修正表的核对结果，contradiction_found=false */ }
}
```

- `pair` 可选。给出时必须与唯一候选一致（否则 409 `candidate_mismatch`）；
  多于一个候选一律 409 `candidate_not_unique` 并列出全部候选；没有候选
  则 409 `no_candidate`。
- 原表通过（无矛盾）时无需求换列，确认返回 409 `no_contradiction`。

### 查询 `GET /archives/{档号}`

返回封存的**原表、所换列对、修正表**（以及两版核对结果），重启后仍可查。
`GET /archives` 列出全部档号。

### 流程互斥与故障保护

- **补报先提交则旧修订不能确认**：旧修订号返回 409 `revision_stale`。
- **确认先提交则该档不能再补报**：返回 409 `archive_confirmed`。
- **写入失败保留上次存档状态**：存档写入同目录临时文件并 `fsync` 后原子
  rename；任何失败返回 500 `archive_write_failed`，磁盘上的旧存档不受影响。

### 档号

档号是调用方自选的 1–200 位字母、数字、`_ . : -`。不合规返回 400
`invalid_archive_id`。存档目录由 `ARCHIVE_DIR` 指定（容器内默认
`/srv/data/archives`，compose 使用命名卷 `archives`）。

## Docker Compose 启动

```bash
docker compose up --build
```

- 交接包服务（含 Swagger 交互文档）：<http://localhost:8001/docs>
- 换算服务（含 Swagger 交互文档）：<http://localhost:8002/docs>
- 健康检查：`GET /health`
- 存档持久化：converter 使用命名卷 `archives`（容器内 `/srv/data/archives`），
  `docker compose down` 之后存档仍在；`docker compose down -v` 才会清除。

## 本地运行与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
uvicorn app.builder_app:app --port 8001
uvicorn app.converter_app:app --port 8002
pytest -q
```

## 典型调用流程

1. 在 `http://localhost:8001/docs` 的 `POST /build` 提交新旧区及街块覆盖，
   保存返回的交接包 JSON；
2. 在 `http://localhost:8002/docs` 的 `POST /convert` 提交该交接包与
   当期已有旧区人数（非负整数），当次得到可精确推出的新区人数、
   带分数系数的算式，以及缺资料新区列表。

## 目录结构

```
app/
  rationals.py      # 精确有理线性代数（RREF、零空间、线性方程组）
  expressions.py    # 分数与算式格式化
  models.py         # Pydantic 请求/响应模型与校验
  engine.py         # 交接包生成与换算的纯领域逻辑
  almanac.py        # 年鉴表格规范化、等式核对、列对枚举
  archive_store.py  # 原子、可重读的存档读写
  review_service.py # 提交/补报/确认的流程规则与修订号
  archive_api.py    # 存档相关 FastAPI 路由
  builder_app.py    # 实例 1：/build
  converter_app.py  # 实例 2：/convert 与 /archives/*
tests/
  test_service.py   # 14 个换算服务端到端测试
  test_archives.py  # 28 个年鉴存档端到端测试
Dockerfile
docker-compose.yml
```
