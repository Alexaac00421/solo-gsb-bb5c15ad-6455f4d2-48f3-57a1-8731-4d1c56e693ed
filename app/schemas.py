"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HandoverIn(BaseModel):
    """单条交接记录（按时间排列提交，序号由提交顺序决定）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=200, description="交接地点")
    from_person: str = Field(min_length=1, max_length=100, description="交出人")
    to_person: str = Field(min_length=1, max_length=100, description="接收人")
    seal_no: str = Field(min_length=1, max_length=100, description="封签号")
    occurred_at: datetime = Field(description="交接时间（必须带时区）")
    voucher_digest: str = Field(min_length=1, max_length=200, description="凭证摘要，如 sha256:<64hex>")
    seal_change_reason: Optional[str] = Field(default=None, max_length=500, description="换签授权理由（换签时必填）")

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at 必须携带时区，如 2026-09-01T09:00:00+08:00 或 2026-09-01T01:00:00Z")
        return v


class BatchRequest(BaseModel):
    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    records: list[HandoverIn] = Field(min_length=1, max_length=1000, description="按时间排列的交接记录")


# ---------------------------------------------------------------- 车次整车交接


class VehicleItemIn(BaseModel):
    """车次整车交接单中一件展品的交接内容（记录按时间排列）。"""

    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    records: list[HandoverIn] = Field(min_length=1, max_length=1000, description="该展品按时间排列的交接记录")


class VehicleHandoverRequest(BaseModel):
    """车次整车交接单：同一车次下 2 至 20 件互不重复的展品一次确认整车结果。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    vehicle_id: str = Field(min_length=1, max_length=64, description="车次号")
    items: list[VehicleItemIn] = Field(min_length=2, max_length=20, description="2 至 20 件互不重复的展品")

    @field_validator("items")
    @classmethod
    def _unique_exhibits(cls, items: list[VehicleItemIn]) -> list[VehicleItemIn]:
        ids = [item.exhibit_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("同一车次内展品编号不得重复")
        return items


# ---------------------------------------------------------------- 凭证摘要补正


class CorrectionRequest(BaseModel):
    """凭证摘要补正单（原交接记录与其 record_hash 永不改写，只追加补正）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    expected_digest: str = Field(min_length=1, max_length=200, description="预期当前有效摘要（与现状不符 -> 409 冲突）")
    new_digest: str = Field(min_length=1, max_length=200, description="新摘要（须为 算法:十六进制；与当前有效摘要相同 -> 422 拒绝）")
    reason: str = Field(min_length=1, max_length=500, description="补正原因（非空；空缺 -> 422 拒绝）")


class WithdrawalRequest(BaseModel):
    """已受理交接批次撤回请求（原交接记录与其 record_hash 永不改写，只追加撤回）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    batch_id: str = Field(min_length=1, max_length=64, description="被撤回的批次号（须为该展品最新且 ACCEPTED 的批次）")
    reason: str = Field(min_length=1, max_length=500, description="撤回原因（非空；空缺 -> 422 拒绝）")


class ReasonOut(BaseModel):
    rule: str
    message: str


class ItemOut(BaseModel):
    seq: int
    ok: bool
    reasons: list[ReasonOut]


class FirstBreakOut(BaseModel):
    seq: int
    rule: str
    message: str


class BatchReport(BaseModel):
    """核验报告（POST 提交与 GET 查询共用）。"""

    batch_id: str
    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    idempotent_replay: bool
    created_at: datetime


class VehicleItemReport(BaseModel):
    """整车交接单中一件展品的逐件结果。

    整单 ACCEPTED 时 batch_id 为该展品写入的既有批次号（可经单件接口查询）；
    整单 REJECTED 时不写任何批次，batch_id 为 null。
    """

    exhibit_id: str
    batch_id: Optional[str]
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]


class VehicleHandoverReport(BaseModel):
    """车次整车交接核验报告（整单一次确认；POST 提交与 GET 查询共用）。"""

    vehicle_order_id: str
    vehicle_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    item_count: int
    items: list[VehicleItemReport]
    idempotent_replay: bool
    created_at: datetime


class ExhibitCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    exhibit_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    home_location: str = Field(min_length=1, max_length=200, description="登记馆藏地（首站校验基准）")
    custodian: str = Field(min_length=1, max_length=100, description="登记保管人")
    initial_seal: str = Field(min_length=1, max_length=100, description="初始封签号")


class ExhibitOut(BaseModel):
    exhibit_id: str
    name: str
    home_location: str
    custodian: str
    current_seal: str
    last_time: Optional[datetime]
    chain_length: int
    chain_head_hash: str
    created_at: datetime
    # 补正链状态（新增字段；补正不推进交接链，故上面 chain_length/chain_head_hash 不变）
    correction_chain_length: Optional[int] = 0
    correction_head_hash: Optional[str] = None
    # 撤回链状态（新增字段；撤回不删除原链，chain_length/chain_head_hash 保持不变）
    withdrawal_chain_length: Optional[int] = 0
    withdrawal_head_hash: Optional[str] = None
    # 撤回后按撤回链计算的有效交接链长度（= chain_length 减去被撤回批次的记录数）
    effective_chain_length: Optional[int] = None


class HandoverRecordOut(BaseModel):
    chain_seq: int
    batch_id: str
    seq_in_batch: int
    location: str
    from_person: str
    to_person: str
    seal_no: str
    seal_changed: bool
    seal_change_reason: Optional[str]
    occurred_at: datetime
    voucher_digest: str
    prev_hash: str
    record_hash: str
    created_at: datetime
    # 补正引入的新字段（原字段保持不变，向后兼容）：
    # 当前有效摘要（无补正则等于原 voucher_digest）与该环节的补正轨迹
    current_voucher_digest: Optional[str] = None
    correction_trail: list["CorrectionTrailItem"] = Field(default_factory=list)
    # 撤回引入的新字段（原字段保持不变，向后兼容）：该环节所属批次的撤回状态。
    # 被撤回时 withdrawn=true 并附撤回引用；原记录、原哈希仍完整保留在链上。
    withdrawn: bool = False
    withdrawal: Optional["WithdrawalRefOut"] = None


class WithdrawalRefOut(BaseModel):
    """交接记录/批次上引用的撤回信息（撤回链中对应环的摘要）。"""

    withdrawal_id: str
    withdrawal_seq: int
    batch_id: str
    reason: str
    record_hash: str
    created_at: datetime


class CorrectionTrailItem(BaseModel):
    """交接查询中某环节的补正轨迹条目（按补正时序）。"""

    correction_id: str
    correction_seq: int
    target_chain_seq: int
    expected_digest: str
    old_digest: str
    new_digest: str
    reason: str
    anchor_record_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime


class CorrectionOut(BaseModel):
    """补正单（首次提交 201；同键同载荷重放 200）。"""

    correction_id: str
    exhibit_id: str
    target_chain_seq: int
    expected_digest: str
    old_digest: str
    new_digest: str
    reason: str
    anchor_record_hash: str
    correction_seq: int
    prev_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime


class WithdrawalOut(BaseModel):
    """撤回结果（首次受理 201；同键同载荷重放 200）。

    撤回只追加不可变撤回记录：返回撤回后的有效链上状态，
    并携带被撤回批次的全部交接记录 record_hash 锚点。
    """

    withdrawal_id: str
    exhibit_id: str
    batch_id: str
    reason: str
    anchor_record_hashes: list[str]
    withdrawal_seq: int
    prev_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime
    # 撤回后按撤回链计算的展品当前有效状态
    chain_length: int = Field(description="原交接链历史长度（撤回不删除记录，保持不变）")
    effective_chain_length: int = Field(description="撤回后的有效交接链长度")
    custodian: str = Field(description="撤回后的当前保管人")
    current_seal: str = Field(description="撤回后的当前封签")
    last_time: Optional[datetime] = Field(default=None, description="撤回后最近一次有效交接时间")


class ChainAuditBlock(BaseModel):
    """审计复算结果中的一条链（交接链 / 补正链）。"""

    chain_length: int
    head_hash: Optional[str] = Field(default=None, description="复算得到的链尾哈希")
    valid: bool
    first_invalid_seq: Optional[int] = None
    detail: str


class ChainOut(BaseModel):
    exhibit_id: str
    chain_length: int = Field(description="原交接链历史长度（撤回不删除记录，长度保持不变）")
    effective_chain_length: Optional[int] = Field(default=None, description="按撤回链排除被撤回批次后的有效链长度")
    records: list[HandoverRecordOut]


class AsOfRecordOut(BaseModel):
    """知悉时刻快照中的一条有效交接记录（该时点已受理且未被撤回）。

    `voucher_digest` 恒定为原摘要；`current_voucher_digest` 为截至知悉时刻
    该环节的当时有效摘要（只含该时刻之前已受理的补正）；`correction_trail`
    同样只含该时刻之前已受理的补正轨迹。
    """

    chain_seq: int
    batch_id: str
    seq_in_batch: int
    location: str
    from_person: str
    to_person: str
    seal_no: str
    seal_changed: bool
    seal_change_reason: Optional[str]
    occurred_at: datetime = Field(description="交接业务发生时间（仅用于呈现，不作为知悉时刻边界）")
    voucher_digest: str
    prev_hash: str
    record_hash: str
    accepted_at: datetime = Field(description="服务端批次受理时间（该记录进入可见链的边界）")
    current_voucher_digest: str
    correction_trail: list["CorrectionTrailItem"] = Field(default_factory=list)


class AsOfStateOut(BaseModel):
    """展品在指定知悉时刻（as-of）的交接状态快照。

    边界一律取服务端落库时间：批次受理时间（batches.created_at）、
    补正受理时间（digest_corrections.created_at）、撤回受理时间
    （batch_withdrawals.created_at）；交接业务发生时间 occurred_at
    不参与可见性判定，故事后补录的历史交接不会提前出现在快照里。
    `chain_length`/`chain_head_hash` 为该时点原交接链（含其后被撤回的记录）；
    `records` 仅为该时点的有效记录（已撤回批次已排除）。
    """

    exhibit_id: str
    as_of: datetime = Field(description="知悉时刻（带时区，回显）")
    registered_at: datetime = Field(description="展品登记时间（早于该时刻查询 -> 404）")
    # 该时点原交接链（含当时存在、其后可能被撤回的记录）
    chain_length: int = Field(description="知悉时刻原交接链长度（含其后被撤回批次的记录）")
    chain_head_hash: str = Field(description="知悉时刻原交接链链头（空链为 GENESIS）")
    # 该时点有效交接记录（排除当时已撤回批次），及当时有效状态
    effective_chain_length: int = Field(description="知悉时刻有效交接记录数（排除当时已撤回批次）")
    records: list[AsOfRecordOut]
    custodian: str = Field(description="由该时点最后一条有效记录确定的保管人；有效记录为空时为登记保管人")
    current_seal: str = Field(description="由该时点最后一条有效记录确定的封签；有效记录为空时为初始封签")
    last_handover_time: Optional[datetime] = Field(
        default=None, description="该时点最近一次有效交接的业务发生时间；有效记录为空时为 null"
    )
    # 三条事件链在该时点的长度（三链在同一数据库快照截取）
    correction_chain_length: int = Field(description="知悉时刻已受理补正环数（按目标在可见原链内计）")
    withdrawal_chain_length: int = Field(description="知悉时刻已受理撤回环数")


class AuditVerifyOut(BaseModel):
    exhibit_id: str
    chain_length: int
    valid: bool
    first_invalid_seq: Optional[int]
    detail: str
    # 补正引入：同时复算交接链与补正链（新增字段，不改动原有字段，向后兼容）
    correction_chain_length: Optional[int] = 0
    corrections_chain: Optional[ChainAuditBlock] = None
    handover_chain: Optional[ChainAuditBlock] = None
    # 撤回引入：同时复算撤回链并校验撤回后展品有效状态（新增字段，向后兼容）
    withdrawal_chain_length: Optional[int] = 0
    withdrawals_chain: Optional[ChainAuditBlock] = None
    effective_chain_length: Optional[int] = None


class BatchSummary(BaseModel):
    batch_id: str
    status: str
    record_count: int
    first_break: Optional[FirstBreakOut]
    created_at: datetime
    # 撤回引入：批次是否已被撤回及其撤回引用（新增字段，向后兼容）
    withdrawn: bool = False
    withdrawal: Optional[WithdrawalRefOut] = None


class HandoverTokenOut(BaseModel):
    """预演签发的展品状态令牌（正式提交可通过 Handover-Token 头携带）。"""

    token: str = Field(description="自包含 HMAC 令牌，绑定展品与签发时的哈希链状态")
    exhibit_id: str
    chain_length: int
    chain_head_hash: str
    issued_at: datetime


class RehearsalOut(BaseModel):
    """预演核验结果：只读，不写批次报告/交接记录/展品状态。"""

    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    rehearsed_at: datetime
    state_token: HandoverTokenOut


# HandoverRecordOut 前向引用了稍后定义的 CorrectionTrailItem，显式重绑模型
HandoverRecordOut.model_rebuild()
