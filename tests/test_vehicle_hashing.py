"""车次整车交接单请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

from app.hashing import vehicle_request_hash


def _items():
    return [
        {
            "exhibit_id": "EX-0001",
            "records": [
                {"location": "库房A", "from_person": "张伟", "to_person": "李静",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-01T01:00:00.000000Z",
                 "voucher_digest": "sha256:" + "ab" * 32},
            ],
        },
        {
            "exhibit_id": "EX-0002",
            "records": [
                {"location": "库房B", "from_person": "王芳", "to_person": "赵磊",
                 "seal_no": "SEAL-2", "occurred_at": "2026-09-01T02:00:00.000000Z",
                 "voucher_digest": "sha256:" + "cd" * 32},
            ],
        },
    ]


def test_request_hash_deterministic_and_hex():
    h1 = vehicle_request_hash("TRAIN-001", _items())
    h2 = vehicle_request_hash("TRAIN-001", _items())
    assert h1 == h2
    assert len(h1) == 64 and int(h1, 16) >= 0


def test_request_hash_vehicle_id_matters():
    base = vehicle_request_hash("TRAIN-001", _items())
    assert vehicle_request_hash("TRAIN-002", _items()) != base


def test_request_hash_any_item_change_differs():
    base = vehicle_request_hash("TRAIN-001", _items())

    renamed = _items()
    renamed[1]["exhibit_id"] = "EX-0003"
    assert vehicle_request_hash("TRAIN-001", renamed) != base

    edited = _items()
    edited[0]["records"][0]["to_person"] = "王强"
    assert vehicle_request_hash("TRAIN-001", edited) != base

    appended = _items()
    appended[1]["records"] = appended[1]["records"] + [dict(appended[1]["records"][0])]
    assert vehicle_request_hash("TRAIN-001", appended) != base


def test_request_hash_item_order_matters():
    base = vehicle_request_hash("TRAIN-001", _items())
    swapped = list(reversed(_items()))
    assert vehicle_request_hash("TRAIN-001", swapped) != base
