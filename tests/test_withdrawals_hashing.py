"""撤回链哈希与请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.hashing import (
    WITHDRAWALS_GENESIS_HASH,
    withdrawal_hash,
    withdrawal_request_hash,
)


WID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
BID = uuid.UUID("12345678-1234-1234-1234-1234567890ab")
AT = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)
ANCHORS = ["a" * 64, "b" * 64]


def _calc(**overrides):
    kw = dict(
        prev_hash=WITHDRAWALS_GENESIS_HASH,
        withdrawal_id=WID,
        exhibit_id="EX-0001",
        withdrawal_seq=1,
        batch_id=BID,
        reason="交接批次误登记，申请撤回",
        anchor_record_hashes=ANCHORS,
        created_at=AT,
    )
    kw.update(overrides)
    return withdrawal_hash(**kw)


def test_withdrawal_hash_deterministic_and_hex():
    h1 = _calc()
    h2 = _calc()
    assert h1 == h2
    assert len(h1) == 64 and int(h1, 16) >= 0


def test_new_ring_chains_to_previous_hash():
    first = _calc()
    second = _calc(prev_hash=first, withdrawal_seq=2,
                   withdrawal_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    assert first != second


def test_changing_any_field_breaks_hash():
    base = _calc()
    assert _calc(reason="另一个原因") != base
    assert _calc(batch_id=uuid.uuid4()) != base
    assert _calc(withdrawal_seq=9) != base
    assert _calc(prev_hash="c" * 64) != base
    assert _calc(exhibit_id="EX-0002") != base
    assert _calc(withdrawal_id=uuid.uuid4()) != base


def test_anchor_list_change_breaks_hash():
    # 锚点列表（含顺序与内容）进入撤回哈希：
    # 被撤回批次记录被改写/删除/乱序，复算即可发现
    assert _calc(anchor_record_hashes=["a" * 64]) != _calc(anchor_record_hashes=ANCHORS)
    assert _calc(anchor_record_hashes=["b" * 64, "a" * 64]) != _calc(anchor_record_hashes=ANCHORS)
    assert _calc(anchor_record_hashes=["a" * 64, "c" * 64]) != _calc(anchor_record_hashes=ANCHORS)


def test_reason_whitespace_normalized_in_hash():
    assert _calc(reason=" 撤回原因 ") == _calc(reason="撤回原因")


def test_timestamp_timezone_normalized():
    plus8 = datetime(2026, 10, 6, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert plus8.isoformat() != AT.isoformat()
    assert _calc(created_at=plus8) == _calc(created_at=AT)


def test_request_hash_same_payload_equal():
    a = withdrawal_request_hash("EX-0001", BID, "原因")
    b = withdrawal_request_hash("EX-0001", BID, "原因")
    assert a == b


def test_request_hash_different_payload_differs():
    base = withdrawal_request_hash("EX-0001", BID, "原因")
    assert withdrawal_request_hash("EX-0002", BID, "原因") != base
    assert withdrawal_request_hash("EX-0001", uuid.uuid4(), "原因") != base
    assert withdrawal_request_hash("EX-0001", BID, "别的原因") != base
