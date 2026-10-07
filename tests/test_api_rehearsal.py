"""预演 / 状态令牌端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

数据库不可用时整个模块自动 skip。本地无 PostgreSQL 时：

    pip install -r requirements-dev.txt   # 规则引擎与令牌单测不受影响

```bash
DATABASE_URL='postgresql://app@/seals?host=/tmp/pgrun' python3 -m pytest tests/test_api_rehearsal.py
```
"""
from __future__ import annotations

import os
import unittest
import uuid

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


DIGEST1 = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
DIGEST3 = "sha256:" + "ef" * 32


def _first_rec(exhibit_id: str, home="库房A", custodian="张伟") -> dict:
    return {
        "exhibit_id": exhibit_id,
        "records": [
            {"location": home, "from_person": custodian, "to_person": "李静",
             "seal_no": "SEAL-1", "occurred_at": "2026-09-01T09:00:00+08:00",
             "voucher_digest": DIGEST1},
        ],
    }


def _next_rec(exhibit_id: str) -> dict:
    return {
        "exhibit_id": exhibit_id,
        "records": [
            {"location": "展厅B", "from_person": "李静", "to_person": "王强",
             "seal_no": "SEAL-1", "occurred_at": "2026-09-02T09:00:00+08:00",
             "voucher_digest": DIGEST2},
        ],
    }


def _bad_rec(exhibit_id: str) -> dict:
    """首站即违规：地点/交出人不对、凭证摘要非法。"""
    return {
        "exhibit_id": exhibit_id,
        "records": [
            {"location": "别处", "from_person": "陌生人", "to_person": "李静",
             "seal_no": "SEAL-1", "occurred_at": "2026-09-01T09:00:00+08:00",
             "voucher_digest": "not-a-digest"},
        ],
    }


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestRehearsalApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 确保 app 读到目标数据库
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        main.DATABASE_URL = DATABASE_URL
        cls.main = main
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        self.ex_id = f"EX-IT-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "集成测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)

    # -- 辅助 ----------------------------------------------------------------

    def _state(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _rehearse(self, body=None):
        return self.client.post(
            "/api/v1/handover-batches/rehearse", json=body or _first_rec(self.ex_id)
        )

    def _submit(self, body, key, token=None):
        headers = {"Idempotency-Key": key}
        if token is not None:
            headers["Handover-Token"] = token
        return self.client.post("/api/v1/handover-batches", json=body, headers=headers)

    # -- 预演 ----------------------------------------------------------------

    def test_rehearse_unknown_exhibit_404(self):
        r = self.client.post("/api/v1/handover-batches/rehearse",
                             json=_first_rec("EX-NOPE-NONEXISTENT"))
        self.assertEqual(r.status_code, 404)

    def test_rehearse_validates_payload_format(self):
        r = self.client.post("/api/v1/handover-batches/rehearse", json={
            "exhibit_id": self.ex_id,
            "records": [{"location": "x"}],  # 缺必填字段
        })
        self.assertEqual(r.status_code, 422)

    def test_rehearse_ok_is_readonly_and_returns_token(self):
        before = self._state()
        r = self._rehearse()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(body["record_count"], 1)
        self.assertIsNone(body["first_break"])
        self.assertTrue(body["items"][0]["ok"])
        st = body["state_token"]
        self.assertTrue(st["token"].startswith("stv1."))
        self.assertEqual(st["exhibit_id"], self.ex_id)
        self.assertEqual(st["chain_length"], before["chain_length"])
        self.assertEqual(st["chain_head_hash"], before["chain_head_hash"])

        # 只读：展品状态、交接链、批次表均零变更
        after = self._state()
        self.assertEqual(before, after)
        chain = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()
        self.assertEqual(chain["chain_length"], 0)
        batches = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json()
        self.assertEqual(batches, [])

    def test_rehearse_rejected_keeps_token_and_writes_nothing(self):
        r = self._rehearse(_bad_rec(self.ex_id))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "REJECTED")
        self.assertEqual(body["first_break"]["seq"], 1)
        self.assertFalse(body["items"][0]["ok"])
        self.assertTrue(body["state_token"]["token"].startswith("stv1."))
        self.assertEqual(self._state()["chain_length"], 0)

    # -- 带令牌提交 ----------------------------------------------------------

    def test_submit_with_valid_token_accepted(self):
        token = self._rehearse().json()["state_token"]["token"]
        r = self._submit(_first_rec(self.ex_id), f"k-{uuid.uuid4()}", token)
        self.assertEqual(r.status_code, 200, r.text)
        report = r.json()
        self.assertEqual(report["status"], "ACCEPTED")
        self.assertFalse(report["idempotent_replay"])
        self.assertEqual(self._state()["chain_length"], 1)

    def test_submit_without_token_still_works(self):
        r = self._submit(_first_rec(self.ex_id), f"k-{uuid.uuid4()}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        self.assertEqual(self._state()["chain_length"], 1)

    def test_same_key_same_token_replay_after_chain_advanced(self):
        body = _first_rec(self.ex_id)
        key = f"k-{uuid.uuid4()}"
        token = self._rehearse(body).json()["state_token"]["token"]
        first = self._submit(body, key, token)
        self.assertEqual(first.status_code, 200)
        first_json = first.json()
        self.assertFalse(first_json["idempotent_replay"])

        # 同键同载荷（含令牌）重试：链已推进，令牌快照已过期，仍返回原报告
        replay = self._submit(body, key, token)
        self.assertEqual(replay.status_code, 200)
        replay_json = replay.json()
        self.assertTrue(replay_json["idempotent_replay"])
        self.assertEqual(replay_json["batch_id"], first_json["batch_id"])
        # 没有重复推进链
        self.assertEqual(self._state()["chain_length"], 1)

    def test_stale_token_after_other_batch_accepted_is_409_and_no_report(self):
        body = _first_rec(self.ex_id)
        token = self._rehearse(body).json()["state_token"]["token"]

        # 另一批次先推进链
        push = self._submit(body, f"k-{uuid.uuid4()}")
        self.assertEqual(push.status_code, 200)
        self.assertEqual(push.json()["status"], "ACCEPTED")

        # 原令牌再提交 -> 409，且不生成批次报告
        conflict_key = f"k-{uuid.uuid4()}"
        r = self._submit(_next_rec(self.ex_id), conflict_key, token)
        self.assertEqual(r.status_code, 409, r.text)
        # 只有先到的那次批次报告
        batches = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["status"], "ACCEPTED")
        self.assertEqual(self._state()["chain_length"], 1)

        # 重新预演后令牌恢复可用
        token2 = self._rehearse(_next_rec(self.ex_id)).json()["state_token"]["token"]
        r2 = self._submit(_next_rec(self.ex_id), conflict_key, token2)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["status"], "ACCEPTED")
        self.assertEqual(self._state()["chain_length"], 2)

    def test_token_for_other_exhibit_is_409(self):
        other_id = f"EX-IT-{uuid.uuid4().hex[:12]}"
        cr = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other_id, "name": "另一展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(cr.status_code, 201)
        token = self._rehearse().json()["state_token"]["token"]  # 属于 self.ex_id

        r = self._submit(_first_rec(other_id), f"k-{uuid.uuid4()}", token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._state()["chain_length"], 0)
        batches = self.client.get(f"/api/v1/exhibits/{other_id}/batches").json()
        self.assertEqual(batches, [])

    def test_malformed_token_is_400(self):
        for bad in ["garbage", "stv1.abc", "stv1.abc.def", "stv2.abc.def"]:
            r = self._submit(_first_rec(self.ex_id), f"k-{uuid.uuid4()}", bad)
            self.assertEqual(r.status_code, 400, f"{bad} -> {r.status_code}")
        self.assertEqual(self._state()["chain_length"], 0)

    def test_token_with_valid_shape_but_bad_signature_is_400(self):
        import base64
        import json
        payload = base64.urlsafe_b64encode(json.dumps({
            "v": 1, "ex": self.ex_id, "n": 0, "h": "GENESIS", "iat": 1780000000,
        }).encode()).rstrip(b"=").decode()
        forged = f"stv1.{payload}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        r = self._submit(_first_rec(self.ex_id), f"k-{uuid.uuid4()}", forged)
        self.assertEqual(r.status_code, 400)

    def test_token_still_valid_after_rejected_batch(self):
        # 预演一个会被驳回的批次并提交 -> REJECTED 但链不动，令牌仍有效
        token = self._rehearse(_bad_rec(self.ex_id)).json()["state_token"]["token"]
        bad = self._submit(_bad_rec(self.ex_id), f"k-{uuid.uuid4()}", token)
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.json()["status"], "REJECTED")
        self.assertEqual(self._state()["chain_length"], 0)

        # 同令牌改提合规载荷：链状态未变 -> 正常受理
        ok = self._submit(_first_rec(self.ex_id), f"k-{uuid.uuid4()}", token)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["status"], "ACCEPTED")
        self.assertEqual(self._state()["chain_length"], 1)

    def test_token_counts_into_idempotency_identity(self):
        body = _first_rec(self.ex_id)
        key = f"k-{uuid.uuid4()}"
        token = self._rehearse(body).json()["state_token"]["token"]

        first = self._submit(body, key, token)
        self.assertEqual(first.status_code, 200)

        # 同键但载荷身份不同（这次不带令牌）-> 409
        r_no_token = self._submit(body, key)
        self.assertEqual(r_no_token.status_code, 409)

        # 同键但不同令牌（重新预演）-> 409
        token2 = self._rehearse(body).json()["state_token"]["token"]
        self.assertNotEqual(token, token2)
        r_other_token = self._submit(body, key, token2)
        self.assertEqual(r_other_token.status_code, 409)


if __name__ == "__main__":
    unittest.main()
