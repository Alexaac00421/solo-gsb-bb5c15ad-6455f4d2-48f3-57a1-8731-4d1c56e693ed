"""核验规则引擎（纯标准库、无框架依赖，可独立单测）。

规则一览：
  FIRST_STATION_LOCATION    首站地点必须与展品登记馆藏地一致（仅首条链）
  FIRST_STATION_CUSTODIAN   首站交出人必须是登记保管人（仅首条链）
  CUSTODIAN_CHAIN_BREAK     交出人必须与上一环节接收人（或当前保管人）一致
  TIME_NOT_INCREASING       交接时间必须严格递增
  SEAL_CHANGED_WITHOUT_AUTH 封签号变更必须携带换签授权理由
  INVALID_VOUCHER_DIGEST    凭证摘要必须为「算法:十六进制」格式

逐项判定均以「提交的前一条记录」为参照（首条以展品当前状态为参照），
因此即使链条中途断裂，后续各项仍能给出独立、可定位的原因。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# 凭证摘要：算法名:hex，如 sha256:<64hex>、sha512:<128hex>、md5:<32hex>
VOUCHER_RE = re.compile(r"^[a-z0-9]{2,12}:[0-9a-fA-F]{32,128}$")

RULE_FIRST_STATION_LOCATION = "FIRST_STATION_LOCATION"
RULE_FIRST_STATION_CUSTODIAN = "FIRST_STATION_CUSTODIAN"
RULE_CUSTODIAN_CHAIN_BREAK = "CUSTODIAN_CHAIN_BREAK"
RULE_TIME_NOT_INCREASING = "TIME_NOT_INCREASING"
RULE_SEAL_CHANGED_WITHOUT_AUTH = "SEAL_CHANGED_WITHOUT_AUTH"
RULE_INVALID_VOUCHER_DIGEST = "INVALID_VOUCHER_DIGEST"


@dataclass
class ExhibitState:
    """展品当前链上状态（校验时的参照系）。"""

    exhibit_id: str
    home_location: str
    custodian: str          # 当前保管人（初始为登记保管人）
    current_seal: str       # 当前封签号（初始为登记初始封签）
    last_time: datetime | None
    chain_length: int


@dataclass
class Reason:
    rule: str
    message: str


@dataclass
class ItemResult:
    seq: int
    reasons: list[Reason] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


@dataclass
class ValidationResult:
    items: list[ItemResult]

    @property
    def accepted(self) -> bool:
        return all(item.ok for item in self.items)

    @property
    def first_break(self) -> tuple[int, Reason] | None:
        """首个断点：按提交顺序第一条不合格记录的首个原因。"""
        for item in self.items:
            if not item.ok:
                return item.seq, item.reasons[0]
        return None


def is_valid_voucher_digest(value: str | None) -> bool:
    """凭证摘要是否符合「算法:十六进制摘要」格式（补正接口复用同一判定）。"""
    return bool(value) and bool(VOUCHER_RE.match(value))


def validate_batch(state: ExhibitState, records: list) -> ValidationResult:
    """校验整批交接记录。records 元素需具备 location/from_person/to_person/
    seal_no/occurred_at/voucher_digest/seal_change_reason 属性。"""
    items: list[ItemResult] = []
    genesis = state.chain_length == 0

    for idx, rec in enumerate(records):
        seq = idx + 1
        is_first = idx == 0
        reasons: list[Reason] = []

        # 参照系：首条对展品当前状态，其余对提交的前一条
        if is_first:
            expected_from = state.custodian
            expected_seal = state.current_seal
            prev_time = state.last_time
        else:
            prev = records[idx - 1]
            expected_from = prev.to_person
            expected_seal = prev.seal_no
            prev_time = prev.occurred_at

        # 1) 首站校验（仅展品首条链）
        if is_first and genesis:
            if rec.location != state.home_location:
                reasons.append(Reason(
                    RULE_FIRST_STATION_LOCATION,
                    f"首站地点「{rec.location}」与登记馆藏地「{state.home_location}」不一致",
                ))
            if rec.from_person != state.custodian:
                reasons.append(Reason(
                    RULE_FIRST_STATION_CUSTODIAN,
                    f"首站交出人「{rec.from_person}」与登记保管人「{state.custodian}」不一致",
                ))
        elif rec.from_person != expected_from:
            who = "当前保管人" if is_first else "上一站接收人"
            reasons.append(Reason(
                RULE_CUSTODIAN_CHAIN_BREAK,
                f"交出人「{rec.from_person}」与{who}「{expected_from}」不一致，人员衔接断裂",
            ))

        # 2) 时间严格递增
        if prev_time is not None and rec.occurred_at <= prev_time:
            reasons.append(Reason(
                RULE_TIME_NOT_INCREASING,
                f"交接时间 {rec.occurred_at.isoformat()} 未严格晚于上一环节 {prev_time.isoformat()}",
            ))

        # 3) 封签连续性：变更必须带授权理由
        reason_text = (rec.seal_change_reason or "").strip()
        if rec.seal_no != expected_seal and not reason_text:
            reasons.append(Reason(
                RULE_SEAL_CHANGED_WITHOUT_AUTH,
                f"封签号由「{expected_seal}」变更为「{rec.seal_no}」，但未提供换签授权理由",
            ))

        # 4) 凭证摘要格式
        if not VOUCHER_RE.match(rec.voucher_digest or ""):
            reasons.append(Reason(
                RULE_INVALID_VOUCHER_DIGEST,
                "凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>",
            ))

        items.append(ItemResult(seq=seq, reasons=reasons))

    return ValidationResult(items=items)
