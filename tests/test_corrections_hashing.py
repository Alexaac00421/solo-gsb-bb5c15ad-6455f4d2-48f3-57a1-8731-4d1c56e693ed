"""补正链哈希与请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.hashing import (
    CORRECTIONS_GENESIS_HASH,
    correction_hash,
    correction_request_hash,
)


CID = uuid.UUID("11111111-2222-3333-4444-555555555555")
AT = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)


def _calc(**overrides):
    kw = dict(
        prev_hash=CORRECTIONS_GENESIS_HASH,
        correction_id=CID,
        exhibit_id="EX-0001",
        correction_seq=1,
        target_chain_seq=2,
        expected_digest="sha256:" + "ab" * 32,
        old_digest="sha256:" + "ab" * 32,
        new_digest="sha256:" + "cd" * 32,
        reason="录入笔误",
        anchor_record_hash="a" * 64,
        created_at=AT,
    )
    kw.update(overrides)
    return correction_hash(**kw)


def test_correction_hash_deterministic_and_hex():
    h1 = _calc()
    h2 = _calc()
    assert h1 == h2
    assert len(h1) == 64 and int(h1, 16) >= 0


def test_new_ring_chains_to_previous_hash():
    first = _calc()
    second = _calc(
        prev_hash=first,
        correction_seq=2,
        expected_digest="sha256:" + "cd" * 32,
        old_digest="sha256:" + "cd" * 32,
        new_digest="sha256:" + "ef" * 32,
    )
    assert first != second


def test_changing_any_field_breaks_hash():
    base = _calc()
    assert _calc(new_digest="sha256:" + "01" * 32) != base
    assert _calc(reason="另一个原因") != base
    assert _calc(expected_digest="sha256:" + "02" * 32) != base
    assert _calc(old_digest="sha256:" + "03" * 32) != base
    assert _calc(anchor_record_hash="b" * 64) != base
    assert _calc(target_chain_seq=3) != base
    assert _calc(correction_seq=9) != base
    assert _calc(prev_hash="c" * 64) != base
    assert _calc(exhibit_id="EX-0002") != base


def test_anchor_change_breaks_hash():
    # 被补正原记录哈希（锚点）进入补正哈希，原记录被改写即可被复算发现
    assert _calc(anchor_record_hash="deadbeef" * 8) != _calc(anchor_record_hash="00" * 32)


def test_reason_whitespace_normalized_in_hash():
    # 写入时对原因首尾空白做同一规范化，避免重算漂移
    assert _calc(reason=" 录入笔误 ") == _calc(reason="录入笔误")


def test_timestamp_timezone_normalized():
    # UTC Z 与 +00:00 等同一时刻表示必须得到相同哈希
    plus0 = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    )
    assert plus0.isoformat() != AT.isoformat()
    assert _calc(created_at=plus0) == _calc(created_at=AT)


def test_request_hash_same_payload_equal():
    a = correction_request_hash("EX-0001", 2, "d0", "d1", "原因")
    b = correction_request_hash("EX-0001", 2, "d0", "d1", "原因")
    assert a == b


def test_request_hash_different_payload_differs():
    base = correction_request_hash("EX-0001", 2, "d0", "d1", "原因")
    assert correction_request_hash("EX-0002", 2, "d0", "d1", "原因") != base
    assert correction_request_hash("EX-0001", 3, "d0", "d1", "原因") != base
    assert correction_request_hash("EX-0001", 2, "dX", "d1", "原因") != base
    assert correction_request_hash("EX-0001", 2, "d0", "dX", "原因") != base
    assert correction_request_hash("EX-0001", 2, "d0", "d1", "别的原因") != base
