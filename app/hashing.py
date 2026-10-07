"""规范化序列化与哈希链计算（纯标准库，便于单测与审计复算）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# 每条展品审计链的创世前驱哈希
GENESIS_HASH = "GENESIS"
# 补正链独立的创世前驱哈希：补正按展品另起一条链，不接在交接链尾
CORRECTIONS_GENESIS_HASH = "CORRECTIONS_GENESIS"
# 撤回链独立的创世前驱哈希：撤回只追加不可变记录，原交接记录与原链恒不变
WITHDRAWALS_GENESIS_HASH = "WITHDRAWALS_GENESIS"


def canonical_json(obj) -> bytes:
    """确定性 JSON 序列化：键排序、无空白、UTF-8。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_ts(dt: datetime) -> str:
    """时间戳规范化：统一换算到 UTC、微秒精度、Z 后缀。

    写入与审计复算必须使用同一规范化，否则同一时刻的不同表示
    （+00:00 / Z / 不同精度）会得到不同哈希。
    """
    if dt.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def request_hash(exhibit_id: str, records: list[dict], handover_token: str | None = None) -> str:
    """请求体指纹：用于幂等键下检测「同键不同载荷」。

    携带 Handover-Token 的提交，令牌计入载荷身份：
    同键但有/无令牌、或令牌不同，均视为不同载荷（409）。
    """
    payload: dict = {"exhibit_id": exhibit_id, "records": records}
    if handover_token:
        payload["handover_token"] = handover_token
    return sha256_hex(canonical_json(payload))


def vehicle_request_hash(vehicle_id: str, items: list[dict]) -> str:
    """车次整车交接单请求体指纹：用于幂等键下检测「同键不同载荷」。

    items 为 [{"exhibit_id": ..., "records": [...]}]（按提交顺序），
    任一件的展品编号或交接记录不同都视为不同载荷（409）。
    """
    payload = {"vehicle_id": vehicle_id, "items": items}
    return sha256_hex(canonical_json(payload))


def record_hash(
    *,
    prev_hash: str,
    exhibit_id: str,
    chain_seq: int,
    batch_id,
    location: str,
    from_person: str,
    to_person: str,
    seal_no: str,
    seal_change_reason: str | None,
    occurred_at: datetime,
    voucher_digest: str,
) -> str:
    """单条交接记录的链式哈希：record_hash = sha256(prev_hash ‖ 业务字段)。

    任何字段被篡改、记录被删除或顺序被调换，都会使后续哈希校验失败。
    """
    payload = {
        "prev_hash": prev_hash,
        "exhibit_id": exhibit_id,
        "chain_seq": chain_seq,
        "batch_id": str(batch_id),
        "location": location,
        "from_person": from_person,
        "to_person": to_person,
        "seal_no": seal_no,
        "seal_change_reason": seal_change_reason or "",
        "occurred_at": norm_ts(occurred_at),
        "voucher_digest": voucher_digest,
    }
    return sha256_hex(canonical_json(payload))


def correction_request_hash(
    exhibit_id: str,
    target_chain_seq: int,
    expected_digest: str,
    new_digest: str,
    reason: str,
) -> str:
    """补正单请求体指纹：用于幂等键下检测「同键不同载荷」。"""
    payload = {
        "exhibit_id": exhibit_id,
        "target_chain_seq": int(target_chain_seq),
        "expected_digest": expected_digest,
        "new_digest": new_digest,
        "reason": reason,
    }
    return sha256_hex(canonical_json(payload))


def correction_hash(
    *,
    prev_hash: str,
    correction_id,
    exhibit_id: str,
    correction_seq: int,
    target_chain_seq: int,
    expected_digest: str,
    old_digest: str,
    new_digest: str,
    reason: str,
    anchor_record_hash: str,
    created_at: datetime,
) -> str:
    """单张补正单的链式哈希：

        correction_hash = sha256(prev_hash ‖ 补正业务字段 ‖ 被补正原记录哈希锚点)

    补正链按展品独立成链（首环 prev_hash = CORRECTIONS_GENESIS），
    每环显式锚定被补正交接记录的 record_hash：
    原记录被篡改、补正单被改写、删除或乱序，都会使补正链复算失败。
    """
    payload = {
        "prev_hash": prev_hash,
        "kind": "digest_correction",
        "correction_id": str(correction_id),
        "exhibit_id": exhibit_id,
        "correction_seq": correction_seq,
        "target_chain_seq": int(target_chain_seq),
        "expected_digest": expected_digest,
        "old_digest": old_digest,
        "new_digest": new_digest,
        "reason": (reason or "").strip(),
        "anchor_record_hash": anchor_record_hash,
        "created_at": norm_ts(created_at),
    }
    return sha256_hex(canonical_json(payload))


def withdrawal_request_hash(
    exhibit_id: str,
    batch_id,
    reason: str,
) -> str:
    """撤回请求体指纹：用于幂等键下检测「同键不同载荷」。"""
    payload = {
        "exhibit_id": exhibit_id,
        "batch_id": str(batch_id),
        "reason": (reason or "").strip(),
    }
    return sha256_hex(canonical_json(payload))


def withdrawal_hash(
    *,
    prev_hash: str,
    withdrawal_id,
    exhibit_id: str,
    withdrawal_seq: int,
    batch_id,
    reason: str,
    anchor_record_hashes: list[str],
    created_at: datetime,
) -> str:
    """单条撤回记录的链式哈希：

        withdrawal_hash = sha256(prev_hash ‖ 撤回业务字段 ‖
                                  被撤回批次全部交接记录 record_hash 锚点列表)

    撤回链按展品独立成链（首环 prev_hash = WITHDRAWALS_GENESIS），
    每环按 chain_seq 有序锚定被撤回批次的全部交接记录 record_hash：
    原交接记录被篡改、撤回记录被改写、删除或乱序，都会使撤回链复算失败。
    撤回只追加本记录，原交接记录与其 record_hash 永不删除、永不改写。
    """
    payload = {
        "prev_hash": prev_hash,
        "kind": "batch_withdrawal",
        "withdrawal_id": str(withdrawal_id),
        "exhibit_id": exhibit_id,
        "withdrawal_seq": withdrawal_seq,
        "batch_id": str(batch_id),
        "reason": (reason or "").strip(),
        "anchor_record_hashes": list(anchor_record_hashes),
        "created_at": norm_ts(created_at),
    }
    return sha256_hex(canonical_json(payload))
