"""指定知悉时刻交接状态查询的端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 同一 REPEATABLE READ 快照按 batches/digest_corrections/batch_withdrawals.created_at 截链
- 交接业务时间 occurred_at 不替代批次受理时间；晚录入的历史交接不得提前出现
- 补正、撤回在各自创建时刻前后生效；边界 created_at 使用闭区间
- 当时已撤回批次排除出 effective_records，但原链长度/链头仍包含其哈希
- 全部有效记录为空时回到登记保管人和初始封签
- 未知展品/早于登记 404；无时区/未来时刻/缺少 as_of 422

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app:app@localhost:5432/seals' \
    python3 -m pytest tests/test_api_point_in_time.py
"""
from __future__ import annotations

import os
import time
import unittest
import uuid
from datetime import datetime, timedelta

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)

D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rec(exhibit_id: str, frm: str, to_person: str, digest: str, when: str,
         seal: str = "SEAL-1", home: str | None = None):
    return {
        "exhibit_id": exhibit_id,
        "records": [{
            "location": home or "展厅",
            "from_person": frm,
            "to_person": to_person,
            "seal_no": seal,
            "occurred_at": when,
            "voucher_digest": digest,
        }],
    }


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestPointInTimeStateApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        main.DATABASE_URL = DATABASE_URL
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        self.ex_id = f"EX-PT-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id,
            "name": "时点状态测试展品",
            "home_location": "库房A",
            "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.registered_at = _parse(r.json()["created_at"])

    def _submit(self, body, key: str | None = None) -> str:
        r = self.client.post(
            "/api/v1/handover-batches",
            json=body,
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED", r.text)
        return r.json()["batch_id"]

    def _batch_created_at(self, batch_id: str) -> datetime:
        rows = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json()
        row = next(row for row in rows if row["batch_id"] == batch_id)
        return _parse(row["created_at"])

    def _state_at(self, when: datetime | str):
        value = when if isinstance(when, str) else when.isoformat()
        return self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"as_of": value},
        )

    def test_empty_chain_at_registration_returns_registration_state(self):
        r = self._state_at(self.registered_at)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["original_chain"]["chain_length"], 0)
        self.assertEqual(body["original_chain"]["head_hash"], "GENESIS")
        self.assertEqual(body["corrections_chain"], {
            "chain_length": 0,
            "head_hash": "CORRECTIONS_GENESIS",
            "rings": None,
        })
        self.assertEqual(body["withdrawals_chain"], {
            "chain_length": 0,
            "head_hash": "WITHDRAWALS_GENESIS",
            "rings": None,
        })
        self.assertEqual(body["effective_chain_length"], 0)
        self.assertEqual(body["effective_records"], [])
        self.assertEqual(body["custodian"], "张伟")
        self.assertEqual(body["current_seal"], "SEAL-1")
        self.assertIsNone(body["last_time"])

    def test_server_accepted_time_not_business_occurred_time_is_boundary(self):
        # 业务发生时间故意早于登记时刻；真正可见边界仍是服务端批次受理时间。
        before_batch = self.registered_at + timedelta(milliseconds=10)
        bid = self._submit(
            _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                 home="库房A"),
        )
        accepted_at = self._batch_created_at(bid)

        r = self._state_at(before_batch)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["original_chain"]["chain_length"], 0)
        self.assertEqual(r.json()["effective_records"], [])

        # created_at 为闭区间：精确在批次受理时刻应立即可见。
        at_accept = self._state_at(accepted_at)
        self.assertEqual(at_accept.status_code, 200, at_accept.text)
        body = at_accept.json()
        self.assertEqual(body["original_chain"]["chain_length"], 1)
        self.assertEqual(body["original_chain"]["head_hash"],
                         body["effective_records"][0]["record_hash"])
        self.assertEqual(body["effective_records"][0]["accepted_at"],
                         accepted_at.isoformat())
        self.assertEqual(body["custodian"], "李静")
        self.assertEqual(body["last_time"], "2026-09-01T01:00:00+00:00")

    def test_correction_and_withdrawal_boundaries_and_original_chain_preserved(self):
        b1 = self._submit(
            _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                 home="库房A"),
        )
        b1_at = self._batch_created_at(b1)
        time.sleep(0.03)

        b2 = self._submit(
            _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        )
        b2_at = self._batch_created_at(b2)
        time.sleep(0.03)

        cr = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/2/digest-corrections",
            json={"expected_digest": D2, "new_digest": D3, "reason": "时点测试补正"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(cr.status_code, 201, cr.text)
        correction_at = _parse(cr.json()["created_at"])
        correction_hash = cr.json()["record_hash"]
        time.sleep(0.03)

        wd = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "时点测试撤回"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(wd.status_code, 201, wd.text)
        withdrawal_at = _parse(wd.json()["created_at"])
        withdrawal_hash = wd.json()["record_hash"]

        after_b1_before_b2 = b1_at + (b2_at - b1_at) / 2
        body = self._state_at(after_b1_before_b2).json()
        self.assertEqual(body["original_chain"]["chain_length"], 1)
        self.assertEqual([r["chain_seq"] for r in body["effective_records"]], [1])
        self.assertEqual(body["custodian"], "李静")

        # 第二批次已受理、补正尚未录入：每环有效摘要仍是原摘要。
        before_correction = b2_at + (correction_at - b2_at) / 2
        body = self._state_at(before_correction).json()
        self.assertEqual(body["original_chain"]["chain_length"], 2)
        self.assertEqual(body["effective_chain_length"], 2)
        self.assertEqual(body["corrections_chain"]["chain_length"], 0)
        self.assertEqual(body["original_chain"]["rings"][1]["current_voucher_digest"], D2)
        self.assertEqual(body["effective_records"][1]["current_voucher_digest"], D2)

        # 精确到补正创建时刻：第二环摘要变为 D3，交接链长度/链头不受补正影响。
        body = self._state_at(correction_at).json()
        self.assertEqual(body["corrections_chain"]["chain_length"], 1)
        self.assertEqual(body["corrections_chain"]["head_hash"], correction_hash)
        self.assertEqual(body["original_chain"]["rings"][1]["current_voucher_digest"], D3)
        self.assertEqual(body["effective_records"][1]["current_voucher_digest"], D3)
        self.assertEqual(len(body["effective_records"][1]["correction_trail"]), 1)
        self.assertEqual(body["withdrawals_chain"]["chain_length"], 0)

        # 精确到撤回创建时刻：批次 2 从有效列表排除，但仍在原链长度和链头中。
        body = self._state_at(withdrawal_at).json()
        self.assertEqual(body["withdrawals_chain"]["chain_length"], 1)
        self.assertEqual(body["withdrawals_chain"]["head_hash"], withdrawal_hash)
        self.assertEqual(body["original_chain"]["chain_length"], 2)
        self.assertEqual(body["original_chain"]["head_hash"],
                         body["original_chain"]["rings"][1]["record_hash"])
        self.assertTrue(body["original_chain"]["rings"][1]["withdrawn"])
        self.assertEqual(body["effective_chain_length"], 1)
        self.assertEqual([r["chain_seq"] for r in body["effective_records"]], [1])
        self.assertEqual(body["custodian"], "李静")
        self.assertEqual(body["current_seal"], "SEAL-1")
        self.assertEqual(body["last_time"], "2026-09-01T01:00:00+00:00")

    def test_withdraw_all_batches_returns_registration_state_but_keeps_history(self):
        b1 = self._submit(
            _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                 home="库房A"),
        )
        b2 = self._submit(
            _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        )
        self.assertEqual(self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "先撤回第二批"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        ).status_code, 201)
        self.assertEqual(self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b1, "reason": "再撤回第一批"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        ).status_code, 201)

        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"as_of": datetime.now().astimezone().isoformat()},
        ).json()
        self.assertEqual(body["original_chain"]["chain_length"], 2)
        self.assertTrue(all(r["withdrawn"] for r in body["original_chain"]["rings"]))
        self.assertEqual(body["effective_chain_length"], 0)
        self.assertEqual(body["effective_records"], [])
        self.assertEqual(body["custodian"], "张伟")
        self.assertEqual(body["current_seal"], "SEAL-1")
        self.assertIsNone(body["last_time"])

    def test_error_conditions(self):
        unknown = self.client.get(
            "/api/v1/exhibits/EX-NOPE/state-at",
            params={"as_of": self.registered_at.isoformat()},
        )
        self.assertEqual(unknown.status_code, 404)

        before_registration = self.registered_at - timedelta(seconds=1)
        early = self._state_at(before_registration)
        self.assertEqual(early.status_code, 404, early.text)

        naive = self._state_at("2026-09-01T09:00:00")
        self.assertEqual(naive.status_code, 422, naive.text)

        future = self._state_at("2099-01-01T00:00:00+00:00")
        self.assertEqual(future.status_code, 422, future.text)

        missing = self.client.get(f"/api/v1/exhibits/{self.ex_id}/state-at")
        self.assertEqual(missing.status_code, 422)


if __name__ == "__main__":
    unittest.main()
