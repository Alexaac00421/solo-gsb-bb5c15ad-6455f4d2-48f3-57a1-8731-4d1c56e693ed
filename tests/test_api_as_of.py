"""知悉时刻（as-of）交接状态快照端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 边界只认服务端落库时间：批次受理 batches.created_at、补正受理
  digest_corrections.created_at、撤回受理 batch_withdrawals.created_at；
  交接业务发生时间 occurred_at 不提前让历史交接出现。
- 三条事件链在同一数据库快照截取（REPEATABLE READ）。
- 该时点原链长度/链头（含其后被撤回记录）、有效记录、每环当时有效摘要、
  由最后有效记录确定的保管人/封签/最近交接时间；空链回退登记状态。
- 当时已撤回批次排除出有效列表，但原链长度与哈希仍包含它。
- 404：未知展品、早于登记时刻；422：缺时区/无法解析/缺参/未来时刻。
- 兼容：当前状态查询、三链审计、写入契约在新端点加入后行为不变。

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app@localhost:5432/seals' python3 -m pytest tests/test_api_as_of.py
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

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


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32
D4 = "sha256:" + "12" * 32


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestAsOfStateApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        self.ex_id = f"EX-AS-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "时点快照展品",
            "home_location": "库房A", "custodian": "张伟", "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.registered_at = _parse(r.json()["created_at"])

    # -- 辅助 ----------------------------------------------------------------

    def _state_at(self, at):
        if isinstance(at, datetime):
            at = at.isoformat()
        return self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at", params={"at": at}
        )

    def _submit(self, frm, to, digest, when, seal="SEAL-1", home=None, reason=None,
                key=None):
        body = {
            "location": home or "展厅", "from_person": frm, "to_person": to,
            "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
        }
        if reason:
            body["seal_change_reason"] = reason
        r = self.client.post(
            "/api/v1/handover-batches",
            json={"exhibit_id": self.ex_id, "records": [body]},
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["batch_id"]

    def _accepted_at(self, batch_id):
        r = self.client.get(f"/api/v1/handover-batches/{batch_id}")
        self.assertEqual(r.status_code, 200)
        return _parse(r.json()["created_at"])

    def _correct(self, seq, expected, new, key=None):
        return self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/{seq}/digest-corrections",
            json={"expected_digest": expected, "new_digest": new,
                  "reason": "摘要录入笔误"},
            headers={"Idempotency-Key": key or f"c-{uuid.uuid4()}"},
        )

    def _withdraw(self, batch_id, key=None):
        return self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": batch_id, "reason": "批次误登记，撤回"},
            headers={"Idempotency-Key": key or f"w-{uuid.uuid4()}"},
        )

    def _correction_created(self, seq, index=-1):
        rows = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/digest-corrections",
            params={"chain_seq": seq},
        ).json()
        return _parse(rows[index]["created_at"])

    def _withdrawal_created(self, index=-1):
        rows = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals"
        ).json()
        return _parse(rows[index]["created_at"])

    # -- 受理时间为边界（occurred_at 不提前）----------------------------------

    def test_batch_acceptance_time_is_boundary_not_occurred_at(self):
        # 业务时间发生在 2026-09-01，但批次此刻才受理
        bid = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                           home="库房A")
        accepted = self._accepted_at(bid)

        # 业务时间虽在过去，但受理之前的知悉时刻看不到该交接（空链 -> 登记状态）
        before = self._state_at(accepted - timedelta(milliseconds=1))
        self.assertEqual(before.status_code, 200, before.text)
        bj = before.json()
        self.assertEqual(bj["chain_length"], 0)
        self.assertEqual(bj["chain_head_hash"], "GENESIS")
        self.assertEqual(bj["effective_chain_length"], 0)
        self.assertEqual(bj["records"], [])
        self.assertEqual(bj["custodian"], "张伟")        # 回退登记保管人
        self.assertEqual(bj["current_seal"], "SEAL-1")  # 回退初始封签
        self.assertIsNone(bj["last_handover_time"])

        # 恰在受理时刻（含边界）即可见
        at = self._state_at(accepted)
        self.assertEqual(at.status_code, 200, at.text)
        aj = at.json()
        self.assertEqual(aj["chain_length"], 1)
        self.assertEqual(aj["effective_chain_length"], 1)
        self.assertEqual(len(aj["records"]), 1)
        rec = aj["records"][0]
        self.assertEqual(rec["chain_seq"], 1)
        self.assertEqual(rec["to_person"], "李静")
        self.assertEqual(rec["current_voucher_digest"], D1)
        self.assertEqual(aj["custodian"], "李静")
        self.assertEqual(aj["current_seal"], "SEAL-1")
        self.assertEqual(rec["occurred_at"][:19], "2026-09-01T01:00:00")  # UTC
        self.assertEqual(aj["last_handover_time"], rec["occurred_at"])

    def test_two_batches_chain_grows_by_acceptance_boundary(self):
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        a1 = self._accepted_at(b1)
        b2 = self._submit("李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        a2 = self._accepted_at(b2)
        head_now = self.client.get(f"/api/v1/exhibits/{self.ex_id}").json()["chain_head_hash"]

        j1 = self._state_at(a1).json()
        self.assertEqual(j1["chain_length"], 1)
        self.assertEqual(j1["custodian"], "李静")
        # 第 1 环链头（不等于包含第 2 环后的当前链头）
        head1 = j1["chain_head_hash"]
        self.assertEqual(len(head1), 64)

        j2 = self._state_at(a2).json()
        self.assertEqual(j2["chain_length"], 2)
        self.assertEqual(j2["effective_chain_length"], 2)
        self.assertEqual(j2["chain_head_hash"], head_now)
        self.assertNotEqual(head1, head_now)
        self.assertEqual([r["chain_seq"] for r in j2["records"]], [1, 2])
        self.assertEqual(j2["custodian"], "王强")
        self.assertEqual(j2["records"][0]["prev_hash"], "GENESIS")

    # -- 补正受理时间为边界：当时有效摘要 --------------------------------------

    def test_correction_acceptance_time_is_boundary_for_effective_digest(self):
        self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A")
        cr = self._correct(1, D1, D2)
        self.assertEqual(cr.status_code, 201, cr.text)
        corr_at = self._correction_created(1)

        # 补正受理前：当时有效摘要仍是原摘要，补正轨迹为空
        before = self._state_at(corr_at - timedelta(milliseconds=1)).json()
        rec = before["records"][0]
        self.assertEqual(rec["voucher_digest"], D1)
        self.assertEqual(rec["current_voucher_digest"], D1)
        self.assertEqual(rec["correction_trail"], [])
        self.assertEqual(before["correction_chain_length"], 0)

        # 补正受理后：当时有效摘要更新为新摘要，轨迹含 1 环
        after = self._state_at(corr_at).json()
        rec = after["records"][0]
        self.assertEqual(rec["voucher_digest"], D1)          # 原摘要恒定
        self.assertEqual(rec["current_voucher_digest"], D2)  # 当时有效摘要
        self.assertEqual(len(rec["correction_trail"]), 1)
        self.assertEqual(rec["correction_trail"][0]["new_digest"], D2)
        self.assertEqual(after["correction_chain_length"], 1)

    def test_second_correction_only_appears_after_its_acceptance(self):
        self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A")
        self._correct(1, D1, D2)
        c1_at = self._correction_created(1, 0)
        self._correct(1, D2, D3, key=f"c2-{uuid.uuid4()}")
        c2_at = self._correction_created(1, -1)

        between = self._state_at(c1_at).json()
        self.assertEqual(between["records"][0]["current_voucher_digest"], D2)
        self.assertEqual(len(between["records"][0]["correction_trail"]), 1)
        self.assertEqual(between["correction_chain_length"], 1)

        after2 = self._state_at(c2_at).json()
        self.assertEqual(after2["records"][0]["current_voucher_digest"], D3)
        self.assertEqual(len(after2["records"][0]["correction_trail"]), 2)
        self.assertEqual(after2["correction_chain_length"], 2)

    # -- 撤回受理时间为边界 ---------------------------------------------------

    def test_withdrawal_excluded_at_as_of_but_original_chain_retains_it(self):
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        self._accepted_at(b1)
        b2 = self._submit("李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        self._accepted_at(b2)
        wr = self._withdraw(b2)
        self.assertEqual(wr.status_code, 201, wr.text)
        wd_at = self._withdrawal_created()

        # 撤回受理前：两环均有效
        before = self._state_at(wd_at - timedelta(milliseconds=1)).json()
        self.assertEqual(before["chain_length"], 2)
        self.assertEqual(before["effective_chain_length"], 2)
        self.assertEqual(before["custodian"], "王强")
        self.assertEqual(before["withdrawal_chain_length"], 0)

        # 撤回受理后：原链长度/链头仍为 2（含被撤回环），有效列表只剩第 1 环
        after = self._state_at(wd_at).json()
        self.assertEqual(after["chain_length"], 2)
        self.assertEqual(after["chain_head_hash"], before["chain_head_hash"])
        self.assertEqual(after["effective_chain_length"], 1)
        self.assertEqual([r["chain_seq"] for r in after["records"]], [1])
        self.assertEqual(after["custodian"], "李静")
        self.assertEqual(after["current_seal"], "SEAL-1")
        self.assertEqual(after["last_handover_time"],
                         after["records"][0]["occurred_at"])
        self.assertEqual(after["withdrawal_chain_length"], 1)

    def test_withdraw_all_then_empty_chain_falls_back_to_registration(self):
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        self._withdraw(b1)
        wd_at = self._withdrawal_created()
        snap = self._state_at(wd_at).json()
        self.assertEqual(snap["chain_length"], 1)          # 原链仍含被撤回环
        self.assertEqual(len(snap["chain_head_hash"]), 64)
        self.assertEqual(snap["effective_chain_length"], 0)
        self.assertEqual(snap["records"], [])
        self.assertEqual(snap["custodian"], "张伟")
        self.assertEqual(snap["current_seal"], "SEAL-1")
        self.assertIsNone(snap["last_handover_time"])

    def test_withdrawal_does_not_erase_correction_view_on_remaining_record(self):
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        b2 = self._submit("李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        # 补正第 1 环（属于未撤回批次）
        self._correct(1, D1, D3)
        corr_at = self._correction_created(1)
        self._withdraw(b2)
        wd_at = self._withdrawal_created()
        snap = self._state_at(max(corr_at, wd_at) + timedelta(milliseconds=1)).json()
        self.assertEqual(snap["chain_length"], 2)
        self.assertEqual(snap["effective_chain_length"], 1)
        only = snap["records"][0]
        self.assertEqual(only["chain_seq"], 1)
        self.assertEqual(only["current_voucher_digest"], D3)
        self.assertEqual(len(only["correction_trail"]), 1)
        self.assertEqual(snap["custodian"], "李静")

    # -- 错误处理 -------------------------------------------------------------

    def test_unknown_exhibit_404(self):
        r = self.client.get("/api/v1/exhibits/EX-NO-SUCH/state-at",
                            params={"at": "2026-10-01T00:00:00+00:00"})
        self.assertEqual(r.status_code, 404)

    def test_before_registration_404(self):
        r = self._state_at(self.registered_at - timedelta(seconds=1))
        self.assertEqual(r.status_code, 404, r.text)

    def test_exactly_registration_is_ok_empty_chain(self):
        r = self._state_at(self.registered_at)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["chain_length"], 0)
        self.assertEqual(r.json()["effective_chain_length"], 0)
        self.assertEqual(r.json()["records"], [])

    def test_naive_datetime_422(self):
        for bad in ["2026-09-10T09:00:00", "2026-09-10 09:00:00"]:
            r = self._state_at(bad)
            self.assertEqual(r.status_code, 422, f"{bad} -> {r.status_code}")

    def test_missing_or_blank_or_garbled_at_422(self):
        self.assertEqual(self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at").status_code, 422)
        self.assertEqual(self._state_at("   ").status_code, 422)
        self.assertEqual(self._state_at("not-a-time").status_code, 422)
        self.assertEqual(self._state_at("2026-13-99T99:99:99+08:00").status_code, 422)

    def test_future_time_422(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.assertEqual(self._state_at(future).status_code, 422)

    def test_z_suffix_accepted_as_utc(self):
        # 受理发生在过去（相对服务器现在），用 Z 表示的知悉时刻（早于登记）应被解析后判 404，
        # 证明 Z 后缀被当作带时区时刻正确解析，而不是 422。
        r = self._state_at("2020-01-01T00:00:00Z")
        self.assertEqual(r.status_code, 404)

    # -- 同一快照：并发写入不影响单次结果 -------------------------------------

    def test_concurrent_writes_do_not_tear_snapshot(self):
        # 先有一批；在一次快照读取进行中并发提交新批次，快照结果仍只反映其边界内数据。
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        a1 = self._accepted_at(b1)
        # 取一个远晚于 a1、但早于“即将新提交批次受理时间”的知悉时刻：
        # 直接以 a1 为界，任何之后并发写入都不得出现在快照中。
        barrier = threading.Barrier(2)
        out = []

        def reader():
            barrier.wait()
            r = self._state_at(a1)
            out.append(("reader", r.status_code,
                        (r.json()["chain_length"], r.json()["effective_chain_length"])))

        def writer():
            barrier.wait()
            bid = self._submit("李静", "王强", D2, "2026-09-02T09:00:00+08:00")
            out.append(("writer", bid))

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        reader_result = next(x for x in out if x[0] == "reader")
        self.assertEqual(reader_result[1], 200)
        self.assertEqual(reader_result[2], (1, 1))  # 不含并发写入的第 2 环

    # -- 兼容性：当前状态、三链审计、写入契约不变 ------------------------------

    def test_current_state_and_audit_and_write_contracts_still_compatible(self):
        b1 = self._submit("张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                          home="库房A")
        a1 = self._accepted_at(b1)
        self._correct(1, D1, D2)
        b2 = self._submit("李静", "王强", D3, "2026-09-02T09:00:00+08:00")
        a2 = self._accepted_at(b2)
        # 写入互斥契约仍在：b1 已被补正引用，禁止撤回 -> 409
        self.assertEqual(self._withdraw(b1).status_code, 409)
        # 撤回未被补正引用的最新批次 b2 -> 201
        self.assertEqual(self._withdraw(b2).status_code, 201)
        wd_at = self._withdrawal_created()

        chain = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()
        self.assertEqual(chain["chain_length"], 2)            # 原链长度不变
        self.assertEqual(chain["effective_chain_length"], 1)  # b2 被撤回
        # 当前状态查询字段不变
        cur = self.client.get(f"/api/v1/exhibits/{self.ex_id}").json()
        for key in ("custodian", "current_seal", "chain_length", "chain_head_hash",
                    "effective_chain_length", "correction_chain_length",
                    "withdrawal_chain_length"):
            self.assertIn(key, cur)
        self.assertEqual(cur["custodian"], "李静")
        # 三链审计仍可用且通过
        audit = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(audit["valid"], audit["detail"])
        self.assertIn("handover_chain", audit)
        self.assertIn("corrections_chain", audit)
        self.assertIn("withdrawals_chain", audit)

        # 时点快照：以 b2 受理时刻 a2 为界（撤回尚未发生），两环有效，
        # 第 1 环为补正后摘要
        snap_before_wd = self._state_at(a2).json()
        self.assertEqual(snap_before_wd["chain_length"], 2)
        self.assertEqual(snap_before_wd["effective_chain_length"], 2)
        self.assertEqual(snap_before_wd["records"][0]["current_voucher_digest"], D2)
        # 撤回受理后 -> b2 排除，原链仍为 2
        snap_after_wd = self._state_at(wd_at).json()
        self.assertEqual(snap_after_wd["chain_length"], 2)
        self.assertEqual(snap_after_wd["effective_chain_length"], 1)
        self.assertEqual(snap_after_wd["records"][0]["current_voucher_digest"], D2)
        self.assertEqual(snap_after_wd["custodian"], "李静")
        self.assertEqual(snap_after_wd["withdrawal_chain_length"], 1)


if __name__ == "__main__":
    unittest.main()
