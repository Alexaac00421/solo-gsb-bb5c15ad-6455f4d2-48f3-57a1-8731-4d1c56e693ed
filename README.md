# 文物借展封签交接核验服务

Python (FastAPI) + PostgreSQL 实现的借展交接核验服务。调用方提交展品编号及按时间排列的交接记录（地点、交出人、接收人、封签号、时间、凭证摘要），服务完成规则核验、幂等写入与哈希链审计；已入链交接记录的凭证摘要录错时，馆方可提交**补正单**更正——原交接记录与其 `record_hash` 永不改写，补正按展品独立成链并锚定原记录。馆方还可对**最新且已受理、未被补正引用的交接批次**提交**撤回**——撤回同样只追加不可变撤回记录（展品独立撤回哈希链），原交接记录与原链历史长度不变，展品当前保管人、封签与有效交接列表按撤回后状态计算。借展车次同时交接多件展品时，馆方可提交**车次整车交接单**——同一车次下 2 至 20 件互不重复的展品一次确认整车结果：逐件沿用同一套规则、以撤回后的有效状态核验，全数通过才在同一事务中为各件写入既有批次与原交接哈希链；任一件失败仅落整单拒绝报告，不写批次和记录、不推进任何展品状态。

## 功能特性

- **规则核验**：首站校验、人员衔接、时间严格递增、封签连续性（换签必须带授权理由）、凭证摘要格式
- **提交前预演**：`/rehearse` 只读核验整批记录，返回逐条结果、首个断点与展品状态令牌；不写批次报告、交接记录，也不推进链状态
- **状态令牌**：正式提交可携带预演令牌；令牌属于该展品且链状态未变化才受理——格式无效报 400，跨展品/链已推进报 409。**补正不推进交接链，既有令牌继续有效**
- **幂等提交**：`Idempotency-Key` 请求头去重，相同请求重试返回同一结果；同键不同载荷返回 409（令牌计入载荷身份）
- **事务原子性**：核验失败的批次**不写入任何交接记录**、不推进链上状态（仅落核验报告供幂等重试与审计追溯）
- **可查询审计链**：每条交接记录携带 `prev_hash` / `record_hash` 构成哈希链，提供链查询与完整性复算接口
- **凭证摘要补正**：对已入链记录提交补正单（展品编号、链序号、预期当前有效摘要、新摘要、补正原因、幂等键）；原记录与 `record_hash` 不改写，补正按展品另成一条哈希链、每环锚定被补正原记录；交接查询同时呈现**原摘要 / 当前有效摘要 / 补正轨迹**，审计同时复算两条链并定位首个失效环
- **已受理批次撤回**：对展品**最新且 ACCEPTED、未被补正引用**的交接批次提交撤回（展品编号、批次号、撤回原因、幂等键）；存在更晚已受理批次、目标不存在、批次被驳回或原因为空时拒绝。撤回**不删除、不改写**原交接记录与哈希，只追加不可变撤回记录（展品独立撤回哈希链，锚定被撤回批次全部记录的 `record_hash`）；**原链历史长度保持不变**，展品当前保管人/封签/最近时间与有效交接列表按撤回后状态重算，审计同时复算**交接链、补正链、撤回链**三条链
- **车次整车交接**：同一车次下 2 至 20 件互不重复展品一次确认整车结果（车次号、各件展品编号及各自时序交接记录、幂等键）；逐件沿用现有规则、以撤回后的有效状态核验。全数通过时在同一事务中为各件写入既有批次与原交接哈希链，返回车次号及各批次报告；任一件失败仅落整单拒绝报告（列出各件逐条结果），不写交接批次和记录、不推进任何展品状态；展品不存在或请求格式不合法时整单报错且不写入。并发重叠车次或单件提交不会导致部分入链
- **逐项诊断**：输出核验状态、首个断点（first_break）及每条记录的全部不合格原因

## 快速开始

```bash
docker compose up --build
```

启动后：

- API: http://localhost:8000 （交互式文档 http://localhost:8000/docs ）
- PostgreSQL: localhost:5432 （用户/密码 `app`/`app`，库 `seals`）
- `db/init.sql` 在数据库首次启动时自动执行，建表并写入两件种子展品（`EX-0001`、`EX-0002`）

一键跑通完整演示（登记展品 → 预演取令牌 → 凭令牌提交 → 幂等重试 → 令牌失效/非法 → 409 冲突 → 违规批次 → 链查询 → 审计校验 → 凭证摘要补正与双链审计 → 已受理批次撤回与三链审计 → 车次整车交接整单确认/拒绝/幂等与单件共存）：

```bash
./examples/demo.sh
```

重置数据：`docker compose down -v`（清空数据卷后重新初始化）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://app:app@localhost:5432/seals` | PostgreSQL 连接串 |
| `DB_CONNECT_RETRIES` | `30` | 启动等待数据库的重试次数 |
| `DB_CONNECT_DELAY` | `1.0` | 重试间隔（秒） |
| `TOKEN_SECRET` | `dev-insecure-rehearsal-secret-change-me` | 预演状态令牌的 HMAC 签名密钥。**仅供本地演示**；生产部署必须设置为高熵随机值，且多实例共享同一密钥（可用 `TOKEN_SECRET=$(openssl rand -hex 32)` 生成，经 Compose 同名环境变量传入） |

## 核验规则

| 规则码 | 说明 |
|---|---|
| `FIRST_STATION_LOCATION` | 展品首条链的第一站地点必须等于登记馆藏地 |
| `FIRST_STATION_CUSTODIAN` | 首站交出人必须是登记保管人 |
| `CUSTODIAN_CHAIN_BREAK` | 交出人必须等于上一环接收人（批次首环则等于当前保管人） |
| `TIME_NOT_INCREASING` | 交接时间必须严格晚于上一环（批次首环则晚于链上最近时间） |
| `SEAL_CHANGED_WITHOUT_AUTH` | 封签号与上一环（或当前封签）不一致时，必须提供换签授权理由 |
| `INVALID_VOUCHER_DIGEST` | 凭证摘要须为 `算法:十六进制`，如 `sha256:<64hex>` |

逐项判定均以「提交的前一条记录」为参照（首条以展品当前链上状态为参照），因此链条中途断裂后，后续各项仍能给出独立可定位的原因。批次任一记录不合格即整体 `REJECTED`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/exhibits` | 登记展品（馆藏地/保管人/初始封签） |
| GET | `/api/v1/exhibits/{id}` | 查询展品当前链上状态 |
| POST | `/api/v1/handover-batches/rehearse` | **预演交接批次（只读）**：同载荷规则核验，返回逐条结果、首个断点与状态令牌，不写任何数据 |
| POST | `/api/v1/handover-batches` | **提交交接批次并核验**（需 `Idempotency-Key` 头，可选 `Handover-Token` 头） |
| GET | `/api/v1/handover-batches/{batch_id}` | 查询批次核验报告 |
| POST | `/api/v1/vehicle-handovers` | **车次整车交接**：同一车次 2 至 20 件展品一次确认整单结果（需 `Idempotency-Key` 头） |
| GET | `/api/v1/vehicle-handovers/{vehicle_order_id}` | 查询车次整车交接整单报告（含各件逐条结果/批次报告） |
| GET | `/api/v1/exhibits/{id}/handovers` | 查询完整交接链（含哈希；每环附当前有效摘要与补正轨迹） |
| GET | `/api/v1/exhibits/{id}/handovers/{seq}` | 查询单个交接环节（原摘要、当前有效摘要、补正轨迹） |
| POST | `/api/v1/exhibits/{id}/handovers/{seq}/digest-corrections` | **提交凭证摘要补正单**（需 `Idempotency-Key` 头） |
| GET | `/api/v1/exhibits/{id}/digest-corrections` | 查询补正链（可按 `chain_seq` 过滤被补正环节） |
| POST | `/api/v1/exhibits/{id}/batch-withdrawals` | **撤回最新已受理交接批次**（请求体含批次号与撤回原因，需 `Idempotency-Key` 头） |
| GET | `/api/v1/exhibits/{id}/batch-withdrawals` | 查询撤回链（可按 `batch_id` 过滤被撤回批次） |
| GET | `/api/v1/exhibits/{id}/audit/verify` | 同时复算交接链、补正链与撤回链，校验完整性并定位首个失效环 |
| GET | `/api/v1/exhibits/{id}/batches` | 批次历史（含被驳回、已撤回批次，可按 `status` 过滤） |

## 调用示例

### 1. 登记展品

```bash
curl -X POST http://localhost:8000/api/v1/exhibits \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-1001","name":"宋代青瓷莲花尊","home_location":"南京博物院库房B区","custodian":"张伟","initial_seal":"SEAL-NJ-0001"}'
```

### 2. 预演交接批次（正式提交前只读核验）

预演接受与正式提交**完全相同**的展品编号与按序记录（记录格式仍按现有规则校验，格式非法返回 `422`），返回逐条核验结果、首个断点 `first_break` 与该展品当前状态的令牌 `state_token`。预演**不写入**批次报告、交接记录，也不推进展品状态；未知展品返回 `404`。

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches/rehearse \
  -H 'Content-Type: application/json' \
  -d @examples/accept.json
```

响应（即使 `REJECTED` 也照常签发令牌——驳回不改变链状态）：

```json
{
  "exhibit_id": "EX-0001",
  "status": "ACCEPTED",
  "record_count": 3,
  "first_break": null,
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": true, "reasons": []},
    {"seq": 3, "ok": true, "reasons": []}
  ],
  "rehearsed_at": "2026-10-05T08:00:00.000000Z",
  "state_token": {
    "token": "stv1.eyJ2IjoxLCJleCI6IkVYLTAwMDEiLCJuIjowLCJoIjoiR0VORVNJUyIsImlhdCI6MTc4...<HMAC>",
    "exhibit_id": "EX-0001",
    "chain_length": 0,
    "chain_head_hash": "GENESIS",
    "issued_at": "2026-10-05T08:00:00.000000Z"
  }
}
```

令牌为自包含 HMAC 结构 `stv1.<base64url(payload)>.<base64url(hmac)>`，载荷只绑定展品身份与签发时的链快照（`chain_length` + `chain_head_hash`），不绑定提交内容、不入库。

### 3. 携带令牌正式提交

取出上一步的 `state_token.token`，通过 `Handover-Token` 请求头提交（`Idempotency-Key` 规则不变）：

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0001' \
  -H 'Handover-Token: stv1.eyJ...<预演返回的完整令牌>' \
  -d @examples/accept.json
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 令牌属于该展品且链状态未变化 | 按现有规则核验处理（`ACCEPTED` / `REJECTED`） |
| 令牌格式无效（无法解析、签名不符） | `400` 参数错误，不生成批次报告 |
| 令牌属于其他展品 | `409` 状态冲突，不生成批次报告 |
| 预演后该展品链已被其他成功批次推进 | `409` 状态冲突，不生成批次报告，**调用方须重新预演** |
| 未携带 `Handover-Token` | 行为与此前完全一致，继续可用 |

说明：

- 只有成功（`ACCEPTED`）批次会推进链；`REJECTED` 批次只落报告、链不动，因此**驳回后凭同一令牌修正载荷重提仍然有效**。
- 令牌**计入幂等载荷身份**：同键同载荷（含相同令牌）的重试直接返回原报告——即使其间链已推进、令牌快照已过期，`idempotent_replay=true` 且 `batch_id` 不变；同键但有/无令牌或令牌不同，仍按异载荷返回 `409`。
- 令牌无过期时间约束，唯一有效性条件是「展品对得上 + 链状态未变」。

### 4. 幂等重试与冲突（不带令牌的既有语义）

- **相同请求重试**（同 `Idempotency-Key`、同载荷）：返回与首次完全一致的报告，`idempotent_replay=true`，不重复写入。
- **同键不同载荷**：返回 `409`，说明幂等键被复用。

### 5. 提交违规批次

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0002' \
  -d @examples/reject.json
```

响应（`status=REJECTED`，给出首个断点与逐项原因；**未写入任何交接记录**）：

```json
{
  "status": "REJECTED",
  "first_break": {"seq": 2, "rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": false, "reasons": [
      {"rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
      {"rule": "TIME_NOT_INCREASING", "message": "交接时间 ... 未严格晚于上一环节 ..."},
      {"rule": "SEAL_CHANGED_WITHOUT_AUTH", "message": "封签号由「SEAL-BJ-0002」变更为「SEAL-XA-2001」，但未提供换签授权理由"},
      {"rule": "INVALID_VOUCHER_DIGEST", "message": "凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>"}
    ]}
  ]
}
```

### 6. 查询审计链并校验完整性

```bash
curl http://localhost:8000/api/v1/exhibits/EX-0001/handovers
curl http://localhost:8000/api/v1/exhibits/EX-0001/audit/verify
```

`audit/verify` 逐条复算交接链 `record_hash = sha256(prev_hash ‖ 规范化业务字段)`，并同时复算补正链与撤回链（见后两节）；任何篡改、删除、乱序都会在对应链块定位到首个失效环：

```json
{
  "exhibit_id": "EX-0001",
  "chain_length": 3,
  "valid": true,
  "first_invalid_seq": null,
  "detail": "交接链、补正链与撤回链均完整；补正/撤回锚点与原交接记录一致，撤回后有效状态正确",
  "correction_chain_length": 0,
  "withdrawal_chain_length": 0,
  "effective_chain_length": 3,
  "corrections_chain": {"chain_length": 0, "head_hash": "CORRECTIONS_GENESIS", "valid": true, "first_invalid_seq": null, "detail": "补正哈希链完整，各环锚点与原交接记录一致"},
  "withdrawals_chain": {"chain_length": 0, "head_hash": "WITHDRAWALS_GENESIS", "valid": true, "first_invalid_seq": null, "detail": "撤回哈希链完整，各环锚点与原交接记录一致，撤回后有效状态正确"},
  "handover_chain": {"chain_length": 3, "head_hash": "…", "valid": true, "first_invalid_seq": null, "detail": "交接哈希链完整，与展品当前状态一致"}
}
```

### 7. 凭证摘要补正

已入链交接记录的**凭证摘要**录错时，馆方可对指定环节（链序号 `seq`）提交补正单。补正**永不改写**原交接记录及其 `record_hash`，而是按展品另起一条补正哈希链，每环锚定被补正原记录；同一环节多次补正按时序追加，当前有效摘要取该环节最后一笔补正的新摘要。

请求体字段：`expected_digest`（预期当前有效摘要，CAS 乐观锁）、`new_digest`（新摘要）、`reason`（补正原因）；另需 `Idempotency-Key` 请求头。

```bash
curl -X POST http://localhost:8000/api/v1/exhibits/EX-0001/handovers/2/digest-corrections \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: corr-2026-0001' \
  -d @examples/correction.json
```

首次受理返回 **201**，补正单按展品独立成链（首环 `prev_hash = CORRECTIONS_GENESIS`），并回带锚点 `anchor_record_hash`（被补正原记录的 `record_hash`）：

```json
{
  "correction_id": "…", "exhibit_id": "EX-0001", "target_chain_seq": 2,
  "expected_digest": "sha256:1a2b…f809", "old_digest": "sha256:1a2b…f809",
  "new_digest": "sha256:0f1e…4f50", "reason": "凭证摘要录入笔误…",
  "anchor_record_hash": "e4b4dd23…",
  "correction_seq": 1, "prev_hash": "CORRECTIONS_GENESIS", "record_hash": "87b91940…",
  "idempotent_replay": false, "created_at": "2026-10-06T08:00:00.000000Z"
}
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 目标展品不存在 / 链序号环节不存在 | `404` |
| 补正原因为空（含纯空白）、`expected_digest`/`new_digest` 格式非法 | `422` |
| 新摘要与当前有效摘要相同（未构成补正） | `422` |
| `expected_digest` 与当前有效摘要不符 | `409` 冲突（不生成补正单） |
| 不同幂等键以同一预期值并发补正同一环节 | 仅一笔 `201`，其余 `409` |
| 同键同载荷重试 | 原补正单，`200` + `idempotent_replay=true` |
| 同键异载荷 | `409` |

幂等重放**优先于一切业务校验**：即使该环节此后又被其他补正推进过，只要键与载荷与首单一致，仍先按幂等键返回原补正单（不会因 `expected_digest` 已过期而报 `409`）。

**查询**：交接链/单环节查询同时呈现三个视图——`voucher_digest`（原摘要，恒定）、`current_voucher_digest`（当前有效摘要）、`correction_trail`（按补正时序的完整轨迹）；补正链另有独立列表，可按环节过滤：

```bash
curl http://localhost:8000/api/v1/exhibits/EX-0001/handovers/2
curl 'http://localhost:8000/api/v1/exhibits/EX-0001/digest-corrections?chain_seq=2'
```

**不变性**：补正只追加 `digest_corrections` 行并推进展品的 `correction_chain_length`/`correction_head_hash`，**不改动** `chain_length`/`chain_head_hash`、不改 custodian/seal/last_time，因此补正**不推进交接链、不使既有预演令牌失效**；原 `/handover-batches` 提交接口及其响应字段完全保持兼容（交接链查询仅新增字段，不改动既有字段）。

### 8. 已受理批次撤回

馆方发现**整笔已受理交接批次**登记有误（而非单条凭证摘要笔误）时，可撤回该展品**最新且 `ACCEPTED`、尚未被补正引用**的批次。撤回是**只追加**操作：

- 原交接记录与其 `record_hash` **永不删除、永不改写**，`batches` 报告也原样保留；
- 新增一条不可变撤回记录，按展品另成一条**撤回哈希链**（首环 `prev_hash = WITHDRAWALS_GENESIS`），每环锚定被撤回批次**全部交接记录**的 `record_hash`（按 `chain_seq` 有序）；
- 展品原交接链 `chain_length` / `chain_head_hash` **保持不变**（原链历史长度不变，仍是完整可复算的追加式哈希链）；
- 展品当前保管人、封签、最近时间与「有效交接列表」**按撤回后状态重算**：回退到被撤回批次之前；全部批次撤回后回退到登记保管人/初始封签。

请求体字段：`batch_id`（被撤回批次号，UUID）、`reason`（撤回原因，非空）；另需 `Idempotency-Key` 请求头。

```bash
curl -X POST http://localhost:8000/api/v1/exhibits/EX-0001/batch-withdrawals \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: wd-2026-0001' \
  -d '{"batch_id":"51c1a0e9-…","reason":"交接批次误登记，撤回后重新核对凭证再提交（复核单 WD-2026-1001）"}'
```

首次受理返回 **201**，撤回记录按展品独立成链，并回带被撤回批次的全部锚点与撤回后的有效状态：

```json
{
  "withdrawal_id": "…", "exhibit_id": "EX-0001", "batch_id": "…",
  "reason": "交接批次误登记…",
  "anchor_record_hashes": ["e4b4dd23…"],
  "withdrawal_seq": 1, "prev_hash": "WITHDRAWALS_GENESIS", "record_hash": "9c77f0a1…",
  "idempotent_replay": false, "created_at": "2026-10-06T09:00:00.000000Z",
  "chain_length": 3, "effective_chain_length": 2,
  "custodian": "王强", "current_seal": "SEAL-SH-1002",
  "last_time": "2026-09-03T06:30:00.000000Z"
}
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 目标展品不存在 / 批次不存在 / 批次不属于该展品 | `404` |
| 撤回原因为空（含纯空白）、批次号不是 UUID | `422` |
| 批次状态为 `REJECTED`（被驳回） | `409` |
| 存在比目标更晚的**未撤回**已受理批次（目标不是最新） | `409` |
| 目标批次的任一交接记录已被补正单引用 | `409` |
| 该批次已被撤回（换新幂等键重复撤回） | `409` |
| 同键同载荷重试 | 首次撤回结果，`200` + `idempotent_replay=true` |
| 同键异载荷 | `409` |

撤回按**栈式**展开：撤回最新批次后，前一批次即成为新的「最新未撤回已受理批次」，可继续撤回，直至有效链清空（回退登记状态）。幂等重放**优先于一切业务校验**：即使该批次在此后又被其他请求撤回/操作，同键同载荷仍先返回首次撤回结果。

**撤回后继续交接**：新批次的规则核验（交出人衔接、时间递增、封签连续性、首站校验）以**撤回后的有效状态**为参照；通过后记录仍**线性追加在原交接链物理链尾**（`chain_seq` 连续、`prev_hash` 指向上一物理环，含已撤回历史环）——原链是完整不可变的追加式历史，撤回只由旁路撤回链标记逻辑失效。因此**原链历史长度保持不变**，审计对原链的复算不被撤回影响。

**与预演令牌的关系**：令牌快照绑定原交接链的 `chain_length`/`chain_head_hash`。撤回不增删原交接记录，故这两个值不变，旧令牌**不会**报 409/400；但业务参照状态已回退，沿用旧意图提交会按撤回后有效状态被 `REJECTED`，调用方应重新 `/rehearse` 后再提交（撤回后有效链为空时，预演/提交按首站规则，须以登记馆藏地、登记保管人为起点）。

**查询**：

```bash
# 完整原链历史：chain_length 为历史长度，被撤回记录带 withdrawn=true 与撤回引用
curl http://localhost:8000/api/v1/exhibits/EX-0001/handovers
# 仅撤回后的有效交接列表（长度等于 effective_chain_length）
curl 'http://localhost:8000/api/v1/exhibits/EX-0001/handovers?effective_only=true'
# 撤回链（可按被撤回批次过滤）
curl 'http://localhost:8000/api/v1/exhibits/EX-0001/batch-withdrawals'
curl 'http://localhost:8000/api/v1/exhibits/EX-0001/batch-withdrawals?batch_id=<uuid>'
# 批次历史：已撤回批次带 withdrawn 与撤回引用
curl http://localhost:8000/api/v1/exhibits/EX-0001/batches
```

交接链/单环节查询与批次历史仅**新增** `withdrawn` / `withdrawal`（及顶层 `effective_chain_length`）字段，既有字段不变；`GET /exhibits/{id}` 同样新增 `withdrawal_chain_length`、`withdrawal_head_hash`、`effective_chain_length`，`custodian`/`current_seal`/`last_time` 为撤回后有效值。查询与单笔补正接口的路径、方法、既有字段完全保持兼容。

**审计**：`audit/verify` 同时复算三条链——交接链（原链历史，含已撤回记录）、补正链、撤回链。撤回链逐环重算 `record_hash = sha256(prev_hash ‖ 撤回业务字段 ‖ 被撤回批次全部原记录 record_hash 锚点 ‖ created_at)`，并校验：被撤回批次存在、属于该展品且为 `ACCEPTED`；锚点列表与该批次现存记录的存储哈希及诚实重算哈希逐条一致（原记录被删除/改写即在首个引用环报「锚定失效」）；链尾与展品撤回状态一致；按撤回链重放的有效保管人/封签/最近时间与展品当前列一致。

### 9. 车次整车交接（同一车次多件展品一次确认）

借展车次同时交接多件展品时，馆方提交**整车交接单**：同一车次号下 **2 至 20 件互不重复**的展品编号及各自按时序排列的交接记录，并携带 `Idempotency-Key` 请求头。逐件沿用单件接口的同一套规则、以**撤回后的有效状态**核验；馆方一次确认整车结果：

- **全数通过**：在**同一事务**中为各件写入既有批次（`batches`）与原交接哈希链（`handover_records`，各件记录仍线性追加在各自展品物理链尾）并推进各展品状态；返回车次号及各批次报告，各件批次可经 `GET /api/v1/handover-batches/{batch_id}` 等既有接口查询。
- **任一件核验失败**：保存整单 `REJECTED` 报告（列出各件逐条结果，合格件也给出逐条通过结果但 `batch_id` 为 `null`），**不写交接批次和记录、不推进任何展品状态**。
- **展品不存在 / 请求格式不合法**（件数越界、展品重复、记录格式非法）：整单报错（`404` / `422`），**不写入任何数据**（整单报告也不留痕）。
- **并发**：重叠车次或单件提交在同一组展品行锁下串行，**不会导致部分入链**。

```bash
curl -X POST http://localhost:8000/api/v1/vehicle-handovers \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: veh-2026-1001' \
  -d @examples/vehicle.json
```

受理响应（`status=ACCEPTED`，`items` 为各件批次报告）：

```json
{
  "vehicle_order_id": "…", "vehicle_id": "TRAIN-2026-1001",
  "status": "ACCEPTED", "item_count": 2,
  "items": [
    {"exhibit_id": "EX-0001", "batch_id": "…", "status": "ACCEPTED", "record_count": 1,
     "first_break": null, "items": [{"seq": 1, "ok": true, "reasons": []}]},
    {"exhibit_id": "EX-0002", "batch_id": "…", "status": "ACCEPTED", "record_count": 1,
     "first_break": null, "items": [{"seq": 1, "ok": true, "reasons": []}]}
  ],
  "idempotent_replay": false, "created_at": "2026-10-06T08:00:00.000000Z"
}
```

拒绝响应（`status=REJECTED`；示例见 `examples/vehicle-reject.json`——第二件首站地点/交出人与登记不符、无授权换签且凭证非法，整单拒绝，两件展品链状态均不推进）：

```json
{
  "vehicle_order_id": "…", "vehicle_id": "TRAIN-2026-1002",
  "status": "REJECTED", "item_count": 2,
  "items": [
    {"exhibit_id": "EX-0001", "batch_id": null, "status": "ACCEPTED", "record_count": 1,
     "first_break": null, "items": [{"seq": 1, "ok": true, "reasons": []}]},
    {"exhibit_id": "EX-0002", "batch_id": null, "status": "REJECTED", "record_count": 1,
     "first_break": {"seq": 1, "rule": "FIRST_STATION_LOCATION", "message": "首站地点「上海博物馆交接区」与登记馆藏地「国家博物馆一层库房」不一致"},
     "items": [{"seq": 1, "ok": false, "reasons": [
       {"rule": "FIRST_STATION_LOCATION", "message": "首站地点「上海博物馆交接区」与登记馆藏地「国家博物馆一层库房」不一致"},
       {"rule": "FIRST_STATION_CUSTODIAN", "message": "首站交出人「孙倩」与登记保管人「王芳」不一致"},
       {"rule": "SEAL_CHANGED_WITHOUT_AUTH", "message": "封签号由「SEAL-BJ-0002」变更为「SEAL-XA-2001」，但未提供换签授权理由"},
       {"rule": "INVALID_VOUCHER_DIGEST", "message": "凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>"}
     ]}]}
  ],
  "idempotent_replay": false, "created_at": "2026-10-06T08:05:00.000000Z"
}
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 全部展品存在且逐件核验通过 | `200` + `status=ACCEPTED`，同一事务各件入链 |
| 任一件核验失败 | `200` + `status=REJECTED`，仅落整单拒绝报告 |
| 任一展品不存在 | `404`，整单不写入 |
| 件数 <2 或 >20、展品编号重复、记录格式非法 | `422`，整单不写入 |
| 同键同载荷重试（含拒绝结果） | 原整单报告，`idempotent_replay=true` |
| 同键不同载荷 | `409` |

**查询**：`GET /api/v1/vehicle-handovers/{vehicle_order_id}` 按整单号查询整单报告；各件批次报告、交接链、补正、撤回与三链审计均沿用既有单件接口，行为完全兼容。

## 设计说明

- **预演**：`POST /rehearse` 仅做一次普通 `SELECT` 读取展品当前状态，复用同一套 `validate_batch` 规则引擎；不落批次报告、不落交接记录、不推进展品状态，可安全重复调用。
- **状态令牌**：`app/tokens.py` 实现的自包含 HMAC 令牌（`stv1.` 前缀），载荷为展品编号 + 签发时 `chain_length`/`chain_head_hash` 快照，密钥来自 `TOKEN_SECRET`，令牌不入库、无状态。提交时先在事务外校验格式/签名（无效即 `400`），再在展品行锁 `SELECT ... FOR UPDATE` 内比对归属与链快照（跨展品或链已推进即 `409`）——行锁保证「比对 → 推进」与其他成功批次互斥，不会读到推进途中的状态。补正只推进 `correction_*` 字段，令牌快照（交接链长度/链头）不变，故补正后令牌仍有效。撤回只追加撤回链并回退展品有效状态（custodian/seal/last_time），同样不增删原交接记录，故 `chain_length`/`chain_head_hash` 不变、令牌快照比对仍通过；但业务参照状态已回退，沿用旧意图提交会按撤回后有效状态核验为 `REJECTED`，调用方须重新预演。
- **幂等**：`batches.idempotency_key` 唯一约束 + `request_hash`（请求体规范化 JSON 的 SHA-256，携带令牌时令牌计入指纹）。同键同载荷直接返回已存报告（即使链已推进，令牌比对也不执行）；同键不同载荷 409；并发同键插入由唯一约束兜底，后到者按重试返回。补正单在 `digest_corrections.idempotency_key` 上同样全局唯一，配 `request_hash` 指纹；幂等预检在展品行锁前后各做一次——锁前快速重放，锁内串行化同键/同展品并发，使「同键同载荷并发」一笔 201、另一笔 200 重放，「异键同预期并发」仅一笔生效。
- **原子性**：核验、批次报告、交接记录、展品链上状态推进全部在单事务内完成；`REJECTED` 批次仅落 `batches` 报告行（用于幂等重试返回同一结果与审计追溯），`handover_records` 与 `exhibits` 状态零变更；令牌 `400/409` 在报告写入前中止事务，批次表也不留痕。补正单与其链状态推进同样在单事务内完成；`404/409/422` 在写入前中止，不留任何补正行。撤回单、撤回链推进与展品有效状态回退也在同一单事务（同一展品行锁）内完成；全部拒绝（`404/409/422`）在写入前中止，不留撤回行。
- **并发一致性**：处理批次/补正/撤回时均对展品行 `SELECT ... FOR UPDATE`，串行化同一展品的链推进、补正与撤回。补正用「预期当前有效摘要」做 CAS：行锁内读取该环节当前有效摘要，与 `expected_digest` 不符即 `409`，因此不同幂等键以同一预期值并发补正同一环节时，先提交者生效，后提交者在锁内读到已更新的当前值而落败。撤回收紧「最新未撤回已受理批次 + 未被补正引用」两个前置条件，配合 `batch_withdrawals.batch_id` 唯一约束：不同幂等键并发撤回同一批次仅一笔 201、其余 409，同键并发则一笔 201、一笔 200 重放。
- **交接哈希链**：首环 `prev_hash = GENESIS`，之后每环指向前环 `record_hash`；时间统一规范化为 UTC 微秒精度 `Z` 格式后参与哈希，保证写入与复算一致。撤回不删除、不改写任何一环：撤回后新受理批次的记录仍线性追加在物理链尾（`prev_hash` 接物理上一环，可能是已撤回记录），因此原链始终是连续完整的追加式哈希链；哪些环节「当前有效」由旁路撤回链在查询/核验时计算。
- **补正哈希链**：按展品独立成链，首环 `prev_hash = CORRECTIONS_GENESIS`；每环 `record_hash = sha256(prev_hash ‖ 补正业务字段 ‖ 被补正原记录 record_hash 锚点 ‖ created_at)`。补正不触碰 `handover_records`，原记录与原交接链恒不变。审计时对每条交接记录按其存储 `prev_hash` **独立**重算「诚实」`record_hash`（全量，不在断裂处中断）：补正环锚点须同时等于存储的原 `record_hash` 与重算出的诚实哈希——原记录内容被改写（即便连带伪造存储哈希列）也会在首个引用它的补正环报「锚定失效」。
- **撤回哈希链**：按展品独立成链，首环 `prev_hash = WITHDRAWALS_GENESIS`；每环 `record_hash = sha256(prev_hash ‖ 撤回业务字段（withdrawal_id/batch_id/reason/seq/created_at）‖ 被撤回批次全部交接记录 record_hash 锚点列表)`。撤回不触碰 `handover_records`/`batches` 与原链头；审计时撤回环锚点列表须逐序等于该批次记录的存储哈希与诚实哈希——被撤回批次记录被删除、改写或乱序即在首个引用它的撤回环报「锚定失效」。审计还按撤回链重放有效链，校验展品当前 custodian/current_seal/last_time 与重放结果一致。有效状态为空（全部撤回）时回退到登记保管人/初始封签（旧卷迁移数据以首环交出人/封签兜底）。
- **车次整车交接**：`vehicle_handovers` 表保存整单报告（`idempotency_key` 全局唯一 + `request_hash` 指纹，幂等语义与单件接口一致，拒绝结果同样可重放）。处理时按展品编号排序对全部涉及展品行 `SELECT ... FOR UPDATE`（确定性加锁顺序避免重叠车次死锁），与单件提交、补正、撤回在同一锁层级互斥；逐件以撤回后有效状态核验。全数通过时各件批次行（派生幂等键 `{整车键}#veh#{展品编号}`）、交接记录与展品状态推进都在同一事务提交；任一件失败仅落整单 REJECTED 报告行，`batches`/`handover_records`/`exhibits` 零变更；`404/422` 在整单报告写入前中止，不留痕。整车入链的批次与记录就是普通批次与记录：单件查询、补正、撤回、批次历史与三链审计无需任何特判。
- **升级兼容**：`db/init.sql` 为全新库的完整结构；对已有数据卷，应用启动时 `app/migrations.py:ensure_schema()` 以 `ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` 幂等补齐补正列与 `digest_corrections` 表、撤回列（`withdrawal_chain_length`/`withdrawal_head_hash`/`registered_custodian`/`registered_initial_seal`）与 `batch_withdrawals` 表、`vehicle_handovers` 表，并对旧数据幂等回填登记保管人/初始封签，旧卷平滑升级、可重复执行。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                 # 规则引擎、令牌、补正/撤回哈希单测（无数据库依赖）
uvicorn app.main:app --reload # 需本地 PostgreSQL 并执行 db/init.sql（旧库启动时自动迁移）
```

`tests/test_api_rehearsal.py`（预演/令牌）、`tests/test_api_corrections.py`（凭证摘要补正）、
`tests/test_api_withdrawals.py`（已受理批次撤回）与 `tests/test_api_vehicle_handovers.py`
（车次整车交接）为端到端测试，需可达的 PostgreSQL，
通过 `DATABASE_URL` 指定；数据库不可达时自动跳过：

```bash
DATABASE_URL='postgresql://app:app@localhost:5432/seals' pytest tests/test_api_rehearsal.py tests/test_api_corrections.py tests/test_api_withdrawals.py tests/test_api_vehicle_handovers.py
```

## 目录结构

```
app/
  main.py        # FastAPI 路由与事务编排（/rehearse、令牌、补正单、批次撤回、车次整车交接与三链审计）
  validation.py  # 规则引擎（纯标准库，可独立单测）
  tokens.py      # 展品状态令牌：签发/验签/链快照比对（纯标准库，可独立单测）
  hashing.py     # 规范化序列化、请求指纹、交接/补正/撤回哈希链
  migrations.py  # 启动时幂等迁移：旧卷补齐补正/撤回列与 digest_corrections/batch_withdrawals/vehicle_handovers 表
  schemas.py     # 请求/响应模型
  db.py          # 连接与启动等待
db/init.sql      # 建表 + 种子展品（Compose 首启自动执行）
examples/        # accept/reject/correction/withdrawal/vehicle 示例载荷与端到端 demo.sh（含补正、撤回与整车交接流程）
tests/           # 规则引擎、令牌、补正/撤回/整车哈希单测与预演/补正/撤回/整车交接端到端测试
Dockerfile  docker-compose.yml  requirements*.txt
```
