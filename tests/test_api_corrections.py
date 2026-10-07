"""凭证摘要补正端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 成功补正与补正链推进；原交接记录/record_hash 不改写
- 404（展品/目标环节不存在）、422（原因空缺、摘要非法、新旧相同）
- 409（预期值不符、同键异载荷、并发同预期仅一笔生效）
- 幂等：同键同载荷重放原单（200），即使其后已发生补正也先按幂等键重放
- 交接链不推进、预演令牌不失效
- 查询：原摘要/当前有效摘要/补正轨迹；补正链列表与单环节查询
- 审计：同时复算两条链，篡改原记录/补正单可定位首个失效环

数据库不可用时整个模块自动 skip：

    DATABASE_URL='postgresql://app@/seals?host=/tmp' python3 -m pytest tests/test_api_corrections.py
"""
from __future__ import annotations

import os
import threading
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


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32
D4 = "sha256:" + "12" * 32


def _rec(exhibit_id: str, frm: str, to: str, digest, when, seal="SEAL-1",
         home=None, custodian=None):
    body = {
        "location": home or "展厅", "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    return {"exhibit_id": exhibit_id, "records": [body]}


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestCorrectionsApi(unittest.TestCase):
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
        self.ex_id = f"EX-CR-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "补正测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        # 两环交接：张伟->李静->王强
        b1 = _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00",
                  home="库房A", custodian="张伟")
        r1 = self.client.post("/api/v1/handover-batches", json=b1,
                              headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
        self.assertEqual(r1.status_code, 200, r1.text)
        b2 = _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        r2 = self.client.post("/api/v1/handover-batches", json=b2,
                              headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
        self.assertEqual(r2.status_code, 200, r2.text)

    # -- 辅助 ----------------------------------------------------------------

    def _correct(self, seq, expected, new, reason="摘要录入笔误", key=None):
        headers = {"Idempotency-Key": key or f"c-{uuid.uuid4()}"}
        return self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/{seq}/digest-corrections",
            json={"expected_digest": expected, "new_digest": new, "reason": reason},
            headers=headers,
        )

    def _original(self, seq):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers/{seq}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _state(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    # -- 成功路径 ------------------------------------------------------------

    def test_correct_success_201_and_original_untouched(self):
        before_state = self._state()
        rec2_before = self._original(2)

        r = self._correct(2, D2, D3)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["exhibit_id"], self.ex_id)
        self.assertEqual(body["target_chain_seq"], 2)
        self.assertEqual(body["expected_digest"], D2)
        self.assertEqual(body["old_digest"], D2)
        self.assertEqual(body["new_digest"], D3)
        self.assertEqual(body["reason"], "摘要录入笔误")
        self.assertEqual(body["correction_seq"], 1)
        self.assertEqual(body["prev_hash"], "CORRECTIONS_GENESIS")
        self.assertEqual(len(body["record_hash"]), 64)
        # 锚点 = 被补正原记录 record_hash
        self.assertEqual(body["anchor_record_hash"], rec2_before["record_hash"])

        # 原交接记录与 record_hash 不得改写
        rec2_after = self._original(2)
        self.assertEqual(rec2_after["voucher_digest"], D2)
        self.assertEqual(rec2_after["record_hash"], rec2_before["record_hash"])
        # 当前有效摘要变为新值，轨迹 1 环
        self.assertEqual(rec2_after["current_voucher_digest"], D3)
        self.assertEqual(len(rec2_after["correction_trail"]), 1)
        self.assertEqual(rec2_after["correction_trail"][0]["new_digest"], D3)

        # 交接链不推进，补正链推进
        after_state = self._state()
        self.assertEqual(after_state["chain_length"], before_state["chain_length"])
        self.assertEqual(after_state["chain_head_hash"], before_state["chain_head_hash"])
        self.assertEqual(after_state["correction_chain_length"],
                         before_state["correction_chain_length"] + 1)
        self.assertEqual(after_state["correction_head_hash"], body["record_hash"])

    def test_stacked_corrections_chain_trail_and_current(self):
        r1 = self._correct(1, D1, D2)
        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r1.json()["correction_seq"], 1)
        r2 = self._correct(1, D2, D3)
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(r2.json()["correction_seq"], 2)
        self.assertEqual(r2.json()["prev_hash"], r1.json()["record_hash"])
        self.assertEqual(r2.json()["old_digest"], D2)

        rec = self._original(1)
        self.assertEqual(rec["voucher_digest"], D1)          # 原摘要恒为 D1
        self.assertEqual(rec["current_voucher_digest"], D3)  # 当前为最新
        self.assertEqual([t["new_digest"] for t in rec["correction_trail"]], [D2, D3])

    # -- 拒绝规则 ------------------------------------------------------------

    def test_unknown_exhibit_404(self):
        r = self.client.post(
            "/api/v1/exhibits/EX-NOPE/handovers/1/digest-corrections",
            json={"expected_digest": D1, "new_digest": D2, "reason": "x"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_target_record_missing_404(self):
        r = self._correct(999, D1, D2)
        self.assertEqual(r.status_code, 404, r.text)

    def test_empty_reason_422(self):
        for bad in ["", "   ", "\t\n"]:
            r = self._correct(1, D1, D2, reason=bad)
            self.assertEqual(r.status_code, 422, f"reason={bad!r} -> {r.status_code}")
        # 未写入任何补正
        self.assertEqual(self._state()["correction_chain_length"], 0)

    def test_bad_digest_format_422(self):
        r = self._correct(1, "not-a-digest", D2)
        self.assertEqual(r.status_code, 422)
        r = self._correct(1, D1, "garbage")
        self.assertEqual(r.status_code, 422)

    def test_same_new_as_current_422(self):
        r = self._correct(1, D1, D1)
        self.assertEqual(r.status_code, 422, r.text)
        # 与当前有效摘要相同（已补正过一次后）也拒绝
        ok = self._correct(1, D1, D2)
        self.assertEqual(ok.status_code, 201)
        again = self._correct(1, D2, D2)
        self.assertEqual(again.status_code, 422)

    def test_expected_mismatch_409(self):
        # 当前有效为 D1，预期成别的值
        r = self._correct(1, D2, D3)
        self.assertEqual(r.status_code, 409, r.text)
        # 冲突不落补正
        self.assertEqual(self._state()["correction_chain_length"], 0)

    def test_expected_must_match_current_not_original_after_prior_correction(self):
        self.assertEqual(self._correct(1, D1, D2).status_code, 201)
        # 此刻当前有效是 D2：拿原摘要 D1 当预期 -> 409
        self.assertEqual(self._correct(1, D1, D3).status_code, 409)
        # 拿当前有效 D2 当预期 -> 成功
        self.assertEqual(self._correct(1, D2, D3).status_code, 201)

    # -- 幂等 ----------------------------------------------------------------

    def test_same_key_same_payload_replays_original(self):
        key = f"c-{uuid.uuid4()}"
        first = self._correct(1, D1, D2, key=key)
        self.assertEqual(first.status_code, 201)
        first_body = first.json()

        replay = self._correct(1, D1, D2, key=key)
        self.assertEqual(replay.status_code, 200, replay.text)
        rb = replay.json()
        self.assertTrue(rb["idempotent_replay"])
        self.assertEqual(rb["correction_id"], first_body["correction_id"])
        self.assertEqual(rb["correction_seq"], 1)
        self.assertEqual(self._state()["correction_chain_length"], 1)

    def test_replay_takes_precedence_even_after_later_correction(self):
        # 键 K 完成 D1->D2；之后另一键把当前有效推进到 D3；
        # 再用键 K 同载荷（expected=D1）重放：必须先按幂等键返回原单，不报 409
        key = f"c-{uuid.uuid4()}"
        first = self._correct(1, D1, D2, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self._correct(1, D2, D3).status_code, 201)
        self.assertEqual(self._original(1)["current_voucher_digest"], D3)

        replay = self._correct(1, D1, D2, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["correction_id"], first.json()["correction_id"])

    def test_same_key_different_payload_409(self):
        key = f"c-{uuid.uuid4()}"
        self.assertEqual(self._correct(1, D1, D2, key=key).status_code, 201)
        # 异载荷（new 不同）-> 409
        self.assertEqual(self._correct(1, D1, D3, key=key).status_code, 409)
        # 异载荷（reason 不同）-> 409
        self.assertEqual(
            self._correct(1, D1, D2, reason="另一个原因", key=key).status_code, 409
        )
        # 异载荷（target seq 不同）-> 409
        key2 = f"c-{uuid.uuid4()}"
        self.assertEqual(self._correct(2, D2, D3, key=key2).status_code, 201)
        self.assertEqual(self._correct(1, D1, D3, key=key2).status_code, 409)

    def test_concurrent_same_expected_only_one_succeeds(self):
        # 不同幂等键、同一预期值并发补正同一环节：仅一笔 201，另一笔 409
        barrier = threading.Barrier(2)
        results = []

        def worker(key, out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/handovers/1/digest-corrections",
                json={"expected_digest": D1, "new_digest": D2, "reason": "并发补正"},
                headers={"Idempotency-Key": key},
            )
            out.append(r.status_code)
            client.close()

        t1 = threading.Thread(target=worker, args=(f"cc-{uuid.uuid4()}", results))
        t2 = threading.Thread(target=worker, args=(f"cc-{uuid.uuid4()}", results))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual(results, [201, 409])
        self.assertEqual(self._state()["correction_chain_length"], 1)
        self.assertEqual(self._original(1)["current_voucher_digest"], D2)

    def test_concurrent_same_idempotency_key_same_payload_one_201_one_200(self):
        barrier = threading.Barrier(2)
        results = []
        key = f"ck-{uuid.uuid4()}"

        def worker(out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/handovers/1/digest-corrections",
                json={"expected_digest": D1, "new_digest": D2, "reason": "同键并发"},
                headers={"Idempotency-Key": key},
            )
            out.append((r.status_code, r.json()["correction_id"]))
            client.close()

        t1 = threading.Thread(target=worker, args=(results,))
        t2 = threading.Thread(target=worker, args=(results,))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 201])
        self.assertEqual(results[0][1], results[1][1])  # 同一补正单
        self.assertEqual(self._state()["correction_chain_length"], 1)

    # -- 令牌不受补正影响 -----------------------------------------------------

    def test_correction_does_not_invalidate_rehearsal_token(self):
        token = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(self.ex_id, "王强", "赵磊", D3, "2026-09-03T09:00:00+08:00"),
        ).json()["state_token"]["token"]

        # 中间插入两笔补正（补正不推进交接链）
        self.assertEqual(self._correct(1, D1, D4).status_code, 201)
        self.assertEqual(self._correct(2, D2, D3).status_code, 201)

        # 令牌仍有效：链状态（chain_length/head）未变
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "王强", "赵磊", D3, "2026-09-03T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}", "Handover-Token": token},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        self.assertEqual(self._state()["chain_length"], 3)

    # -- 查询 ----------------------------------------------------------------

    def test_list_corrections_and_filter(self):
        self.assertEqual(self._correct(1, D1, D2).status_code, 201)
        self.assertEqual(self._correct(2, D2, D3).status_code, 201)

        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/digest-corrections")
        self.assertEqual(r.status_code, 200)
        seq = [(c["correction_seq"], c["target_chain_seq"]) for c in r.json()]
        self.assertEqual(seq, [(1, 1), (2, 2)])

        only1 = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/digest-corrections?chain_seq=1"
        )
        self.assertEqual(only1.status_code, 200)
        self.assertEqual([c["target_chain_seq"] for c in only1.json()], [1])

        r404 = self.client.get("/api/v1/exhibits/EX-NOPE/digest-corrections")
        self.assertEqual(r404.status_code, 404)

    def test_handovers_listing_shows_current_and_trail(self):
        self._correct(2, D2, D3)
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["chain_length"], 2)
        by_seq = {x["chain_seq"]: x for x in body["records"]}
        self.assertEqual(by_seq[1]["current_voucher_digest"], D1)
        self.assertEqual(by_seq[1]["correction_trail"], [])
        self.assertEqual(by_seq[2]["voucher_digest"], D2)
        self.assertEqual(by_seq[2]["current_voucher_digest"], D3)
        self.assertEqual(len(by_seq[2]["correction_trail"]), 1)

    # -- 审计 ----------------------------------------------------------------

    def test_audit_valid_with_two_chains(self):
        self._correct(1, D1, D2)
        self._correct(2, D2, D4)
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/audit/verify")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["valid"], body["detail"])
        self.assertEqual(body["chain_length"], 2)
        self.assertEqual(body["correction_chain_length"], 2)
        self.assertTrue(body["handover_chain"]["valid"])
        self.assertTrue(body["corrections_chain"]["valid"])
        self.assertIsNone(body["corrections_chain"]["first_invalid_seq"])

    def test_audit_detects_tampered_original_record(self):
        corr = self._correct(1, D1, D2)
        self.assertEqual(corr.status_code, 201)

        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE handover_records SET voucher_digest = %s "
                "WHERE exhibit_id = %s AND chain_seq = 1",
                (D3, self.ex_id),
            )
            conn.commit()

        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        # 交接链：改 voucher_digest 直接打断 record_hash，首失效环 = 1
        self.assertEqual(body["handover_chain"]["first_invalid_seq"], 1)
        # 补正链：锚点失配定位到引用它的补正环 1
        self.assertFalse(body["corrections_chain"]["valid"])
        self.assertEqual(body["corrections_chain"]["first_invalid_seq"], 1)
        self.assertIn("锚定失效", body["corrections_chain"]["detail"])

    def test_audit_detects_tampered_correction(self):
        self._correct(1, D1, D2)
        self._correct(2, D2, D3)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE digest_corrections SET new_digest = %s "
                "WHERE exhibit_id = %s AND correction_seq = 1",
                (D4, self.ex_id),
            )
            conn.commit()

        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["handover_chain"]["valid"])
        cb = body["corrections_chain"]
        self.assertFalse(cb["valid"])
        self.assertEqual(cb["first_invalid_seq"], 1)

    def test_audit_detects_deleted_correction_ring(self):
        self._correct(1, D1, D2)
        self._correct(1, D2, D3)
        with psycopg.connect(DATABASE_URL) as conn:
            # 删第一环：第二环 prev_hash 衔接立即失效，首失效环 = 2
            conn.execute(
                "DELETE FROM digest_corrections "
                "WHERE exhibit_id = %s AND correction_seq = 1",
                (self.ex_id,),
            )
            conn.commit()

        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertEqual(body["corrections_chain"]["first_invalid_seq"], 2)


if __name__ == "__main__":
    unittest.main()
