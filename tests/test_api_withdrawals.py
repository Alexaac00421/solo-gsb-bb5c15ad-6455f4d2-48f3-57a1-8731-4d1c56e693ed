"""已受理交接批次撤回端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 成功撤回（201）与幂等重放（200）、同键异载荷 409
- 拒绝：展品/批次不存在(404)、批次号格式非法/原因为空(422)、批次被驳回(409)、
        存在更晚已受理批次(409)、已被补正引用(409)、重复撤回(409)
- 不可变：原交接记录与 record_hash 不删除不改写，chain_length/chain_head_hash 不变
- 撤回后：当前保管人/封签/最近时间回退、有效交接列表按撤回链计算（全撤回到登记状态）
- 撤回后继续提交：业务校验以有效状态为基准，原链物理连续，审计三链均通过
- 补正与撤回互斥规则、撤回不推进原交接链、预演令牌语义
- 查询：撤回链列表、交接链/单环节撤回标注、批次历史撤回标注、有效列表过滤
- 审计：三链复算；篡改原记录/撤回记录可定位首个失效环；状态不一致可发现

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app@localhost:5432/seals' python3 -m pytest tests/test_api_withdrawals.py
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
         home=None, reason=None):
    body = {
        "location": home or "展厅", "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    if reason:
        body["seal_change_reason"] = reason
    return {"exhibit_id": exhibit_id, "records": [body]}


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestWithdrawalsApi(unittest.TestCase):
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
        self.ex_id = f"EX-WT-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "撤回测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        # 两环交接（两个批次）：张伟->李静->王强
        self.b1 = self._submit(
            _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A")
        )
        self.b2 = self._submit(
            _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        )

    # -- 辅助 ----------------------------------------------------------------

    def _submit(self, body, key=None):
        r = self.client.post(
            "/api/v1/handover-batches", json=body,
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["batch_id"]

    def _withdraw(self, batch_id, reason="交接批次误登记，申请撤回", key=None):
        return self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": batch_id, "reason": reason},
            headers={"Idempotency-Key": key or f"w-{uuid.uuid4()}"},
        )

    def _state(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _handovers(self, **params):
        return self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/handovers", params=params
        ).json()

    def _record(self, seq):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers/{seq}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _correct(self, seq, expected, new, reason="摘要录入笔误", key=None):
        return self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/{seq}/digest-corrections",
            json={"expected_digest": expected, "new_digest": new, "reason": reason},
            headers={"Idempotency-Key": key or f"c-{uuid.uuid4()}"},
        )

    # -- 成功路径 ------------------------------------------------------------

    def test_withdraw_latest_success_201(self):
        before = self._state()
        rec2_before = self._record(2)

        r = self._withdraw(self.b2)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["exhibit_id"], self.ex_id)
        self.assertEqual(body["batch_id"], self.b2)
        self.assertEqual(body["reason"], "交接批次误登记，申请撤回")
        self.assertEqual(body["withdrawal_seq"], 1)
        self.assertEqual(body["prev_hash"], "WITHDRAWALS_GENESIS")
        self.assertEqual(len(body["record_hash"]), 64)
        self.assertEqual(body["anchor_record_hashes"], [rec2_before["record_hash"]])
        # 撤回后状态：回退到被撤回批次之前
        self.assertEqual(body["chain_length"], 2)           # 原链历史长度不变
        self.assertEqual(body["effective_chain_length"], 1)
        self.assertEqual(body["custodian"], "李静")
        self.assertEqual(body["current_seal"], "SEAL-1")

        # 原交接记录/record_hash 不删除不改写
        rec2_after = self._record(2)
        self.assertEqual(rec2_after["record_hash"], rec2_before["record_hash"])
        self.assertEqual(rec2_after["voucher_digest"], D2)
        self.assertTrue(rec2_after["withdrawn"])
        self.assertEqual(rec2_after["withdrawal"]["withdrawal_id"],
                         body["withdrawal_id"])

        # 展品表原链字段不变，撤回链推进
        after = self._state()
        self.assertEqual(after["chain_length"], before["chain_length"])
        self.assertEqual(after["chain_head_hash"], before["chain_head_hash"])
        self.assertEqual(after["withdrawal_chain_length"],
                         before["withdrawal_chain_length"] + 1)
        self.assertEqual(after["withdrawal_head_hash"], body["record_hash"])
        self.assertEqual(after["custodian"], "李静")
        self.assertEqual(after["effective_chain_length"], 1)

    def test_withdraw_all_batches_rolls_back_to_registration(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        r = self._withdraw(self.b1)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["withdrawal_seq"], 2)
        self.assertEqual(body["effective_chain_length"], 0)
        self.assertEqual(body["custodian"], "张伟")          # 回退到登记保管人
        self.assertEqual(body["current_seal"], "SEAL-1")    # 回退到初始封签
        self.assertIsNone(body["last_time"])
        self.assertEqual(body["chain_length"], 2)           # 历史长度仍为 2

    def test_withdraw_multi_record_batch_anchors_all_records(self):
        # 再提交一个含两条记录的批次：王强->赵磊->孙倩
        bid = self._submit({
            "exhibit_id": self.ex_id,
            "records": [
                {"location": "展厅", "from_person": "王强", "to_person": "赵磊",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-03T09:00:00+08:00",
                 "voucher_digest": D3},
                {"location": "库房C", "from_person": "赵磊", "to_person": "孙倩",
                 "seal_no": "SEAL-9", "seal_change_reason": "授权换签",
                 "occurred_at": "2026-09-04T09:00:00+08:00",
                 "voucher_digest": D4},
            ],
        })
        r = self._withdraw(bid)
        self.assertEqual(r.status_code, 201, r.text)
        anchors = r.json()["anchor_record_hashes"]
        self.assertEqual(len(anchors), 2)                   # 锚定批次全部记录
        # 回退到该多记录批次之前（王强/SEAL-1）
        self.assertEqual(r.json()["custodian"], "王强")
        self.assertEqual(r.json()["current_seal"], "SEAL-1")
        self.assertEqual(r.json()["effective_chain_length"], 2)

    def test_withdraw_then_resubmit_continues_from_effective_state(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        # 从撤回后有效状态（李静 持有）继续提交
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "李静", "赵磊", D3, "2026-09-05T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        chain = self._handovers()
        # 原链物理连续：新环 seq=3，prev_hash 指向物理链尾（已撤回的 seq=2）
        self.assertEqual(chain["chain_length"], 3)
        self.assertEqual(chain["effective_chain_length"], 2)
        new_rec = chain["records"][2]
        self.assertEqual(new_rec["chain_seq"], 3)
        self.assertEqual(new_rec["prev_hash"], chain["records"][1]["record_hash"])
        self.assertFalse(new_rec["withdrawn"])
        st = self._state()
        self.assertEqual(st["custodian"], "赵磊")

    def test_withdraw_then_resubmit_requires_effective_custodian(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        # 仍按被撤回批次的交出人（王强）提交 -> 人员衔接断裂，REJECTED
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "王强", "赵磊", D3, "2026-09-05T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "REJECTED")
        # REJECTED 不落记录
        self.assertEqual(self._state()["chain_length"], 2)

    # -- 拒绝规则 ------------------------------------------------------------

    def test_unknown_exhibit_404(self):
        r = self.client.post(
            "/api/v1/exhibits/EX-NOPE/batch-withdrawals",
            json={"batch_id": self.b2, "reason": "x"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_batch_404(self):
        r = self._withdraw(str(uuid.uuid4()))
        self.assertEqual(r.status_code, 404, r.text)

    def test_batch_of_other_exhibit_404(self):
        # 另一件展品的已受理批次对当前展品而言等同不存在
        other = f"EX-WO-{uuid.uuid4().hex[:10]}"
        self.assertEqual(self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other, "name": "另一展品", "home_location": "库房Z",
            "custodian": "周明", "initial_seal": "SEAL-Z",
        }).status_code, 201)
        ob = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(other, "周明", "吴迪", D1, "2026-09-01T09:00:00+08:00", home="库房Z"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        ).json()["batch_id"]
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": ob, "reason": "x"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_invalid_batch_id_format_422(self):
        r = self._withdraw("not-a-uuid")
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_empty_reason_422(self):
        for bad in ["", "   ", "\t\n"]:
            r = self._withdraw(self.b2, reason=bad)
            self.assertEqual(r.status_code, 422, f"reason={bad!r} -> {r.status_code}")
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_rejected_batch_cannot_withdraw_409(self):
        bad = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "陌生人", "赵磊", "not-a-digest",
                      "2026-09-03T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        ).json()
        self.assertEqual(bad["status"], "REJECTED")
        rejected_id = bad["batch_id"]
        r = self._withdraw(rejected_id)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("ACCEPTED", r.json()["detail"])
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_later_accepted_batch_exists_409(self):
        # b1 之后存在更晚已受理批次 b2 -> 禁止撤回 b1
        r = self._withdraw(self.b1)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("最新的已受理批次", r.json()["detail"])
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_batch_referenced_by_correction_409(self):
        # 对最新批次 b2 的环节 2 做补正后，b2 被补正引用 -> 禁止撤回
        self.assertEqual(self._correct(2, D2, D3).status_code, 201)
        r = self._withdraw(self.b2)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("补正", r.json()["detail"])
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_correction_on_older_batch_does_not_block_latest(self):
        # b1 的环节 1 被补正不影响撤回最新 b2（只有目标批次自身被引用才阻止）
        self.assertEqual(self._correct(1, D1, D3).status_code, 201)
        r = self._withdraw(self.b2)
        self.assertEqual(r.status_code, 201, r.text)

    def test_double_withdraw_same_batch_409(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        r = self._withdraw(self.b2, reason="再撤一次", key=f"w-{uuid.uuid4()}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_withdraw_unwinds_stack_to_allow_full_rollback(self):
        # 撤回最新 b2 后，b1 成为当前有效链的最新批次，允许继续撤回（直至全量回退）
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        r = self._withdraw(self.b1)
        self.assertEqual(r.status_code, 201, r.text)

    # -- 幂等 ----------------------------------------------------------------

    def test_same_key_same_payload_replays(self):
        key = f"w-{uuid.uuid4()}"
        first = self._withdraw(self.b2, key=key)
        self.assertEqual(first.status_code, 201)
        first_body = first.json()

        replay = self._withdraw(self.b2, key=key)
        self.assertEqual(replay.status_code, 200, replay.text)
        rb = replay.json()
        self.assertTrue(rb["idempotent_replay"])
        self.assertEqual(rb["withdrawal_id"], first_body["withdrawal_id"])
        self.assertEqual(rb["withdrawal_seq"], 1)
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    def test_replay_precedence_even_after_later_withdrawal(self):
        # 键 K 撤回 b2；随后 b3 被受理并以另一键撤回；再用键 K 重放 -> 仍返回原结果
        key = f"w-{uuid.uuid4()}"
        first = self._withdraw(self.b2, key=key)
        self.assertEqual(first.status_code, 201)
        b3 = self._submit(
            _rec(self.ex_id, "李静", "赵磊", D3, "2026-09-05T09:00:00+08:00")
        )
        self.assertEqual(self._withdraw(b3).status_code, 201)

        replay = self._withdraw(self.b2, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["withdrawal_id"], first.json()["withdrawal_id"])
        self.assertEqual(self._state()["withdrawal_chain_length"], 2)

    def test_same_key_different_payload_409(self):
        key = f"w-{uuid.uuid4()}"
        self.assertEqual(self._withdraw(self.b2, key=key).status_code, 201)
        # 原因不同 -> 409
        self.assertEqual(self._withdraw(self.b2, reason="另一个原因", key=key).status_code, 409)
        # 批次不同 -> 409
        key2 = f"w-{uuid.uuid4()}"
        b3 = self._submit(
            _rec(self.ex_id, "李静", "赵磊", D3, "2026-09-05T09:00:00+08:00")
        )
        self.assertEqual(self._withdraw(b3, key=key2).status_code, 201)
        self.assertEqual(self._withdraw(self.b2, key=key2).status_code, 409)

    def test_concurrent_same_batch_only_one_succeeds(self):
        barrier = threading.Barrier(2)
        results = []

        def worker(key, out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
                json={"batch_id": self.b2, "reason": "并发撤回"},
                headers={"Idempotency-Key": key},
            )
            out.append(r.status_code)
            client.close()

        t1 = threading.Thread(target=worker, args=(f"wc-{uuid.uuid4()}", results))
        t2 = threading.Thread(target=worker, args=(f"wc-{uuid.uuid4()}", results))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual(results, [201, 409])
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    def test_concurrent_same_idempotency_key_one_201_one_200(self):
        barrier = threading.Barrier(2)
        results = []
        key = f"wk-{uuid.uuid4()}"

        def worker(out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
                json={"batch_id": self.b2, "reason": "同键并发撤回"},
                headers={"Idempotency-Key": key},
            )
            out.append((r.status_code, r.json()["withdrawal_id"]))
            client.close()

        t1 = threading.Thread(target=worker, args=(results,))
        t2 = threading.Thread(target=worker, args=(results,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual([s for s, _ in results], [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    # -- 查询 ----------------------------------------------------------------

    def test_list_withdrawals_and_filter(self):
        self._withdraw(self.b2)
        b3 = self._submit(
            _rec(self.ex_id, "李静", "赵磊", D3, "2026-09-05T09:00:00+08:00")
        )
        w2 = self._withdraw(b3).json()

        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["withdrawal_seq"] for x in r.json()], [1, 2])
        self.assertEqual([x["batch_id"] for x in r.json()], [self.b2, b3])

        only = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            params={"batch_id": b3},
        )
        self.assertEqual(only.status_code, 200)
        body = only.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["withdrawal_id"], w2["withdrawal_id"])

        r404 = self.client.get("/api/v1/exhibits/EX-NOPE/batch-withdrawals")
        self.assertEqual(r404.status_code, 404)

    def test_handovers_annotate_withdrawn_and_effective_filter(self):
        w = self._withdraw(self.b2).json()
        chain = self._handovers()
        self.assertEqual(chain["chain_length"], 2)
        self.assertEqual(chain["effective_chain_length"], 1)
        r1, r2 = chain["records"]
        self.assertFalse(r1["withdrawn"])
        self.assertIsNone(r1["withdrawal"])
        self.assertTrue(r2["withdrawn"])
        self.assertEqual(r2["withdrawal"]["record_hash"], w["record_hash"])

        eff = self._handovers(effective_only=True)
        self.assertEqual(eff["chain_length"], 2)                    # 历史长度不变
        self.assertEqual(eff["effective_chain_length"], 1)
        self.assertEqual([x["chain_seq"] for x in eff["records"]], [1])

    def test_batches_history_annotates_withdrawn(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches")
        self.assertEqual(r.status_code, 200)
        marks = {b["batch_id"]: (b["withdrawn"], b["withdrawal"]) for b in r.json()}
        self.assertEqual(marks[self.b2][0], True)
        self.assertIsNotNone(marks[self.b2][1])
        self.assertEqual(marks[self.b1][0], False)
        self.assertIsNone(marks[self.b1][1])

    # -- 预演令牌 -------------------------------------------------------------

    def test_token_snapshot_survives_withdrawal_but_business_state_rewinds(self):
        # 撤回不改变 chain_length/chain_head_hash（原链不增删记录），故绑定链快照的
        # 旧令牌不会报 409；但业务参照状态已回退到登记状态，用旧意图提交会按有效状态
        # REJECTED，调用方须重新预演以撤回后的有效状态为基准。
        ex2 = f"EX-WS-{uuid.uuid4().hex[:10]}"
        self.client.post("/api/v1/exhibits", json={
            "exhibit_id": ex2, "name": "单批次展品", "home_location": "库房A",
            "custodian": "张伟", "initial_seal": "SEAL-1",
        })
        only = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(ex2, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        ).json()["batch_id"]
        token = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(ex2, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        ).json()["state_token"]["token"]
        # 撤回：只追加撤回记录并回退展品有效状态，不动原交接链
        wr = self.client.post(
            f"/api/v1/exhibits/{ex2}/batch-withdrawals",
            json={"batch_id": only, "reason": "撤回首站批次"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(wr.status_code, 201, wr.text)
        # 撤回不改变原链长度/链头（原链未追加/未删除记录）
        self.assertEqual(self.client.get(f"/api/v1/exhibits/{ex2}").json()["chain_length"], 1)
        # 旧令牌快照与当前链快照仍一致（不报 409/400），但业务参照状态已回退：
        # 以原令牌提交「李静->王强」会因有效保管人为张伟而 REJECTED
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(ex2, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}", "Handover-Token": token},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "REJECTED")
        # 重新预演（按撤回后有效状态：首站需登记地点/保管人）可成功
        rehe = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(ex2, "张伟", "陈晨", D3, "2026-09-03T09:00:00+08:00", home="库房A"),
        )
        self.assertEqual(rehe.status_code, 200, rehe.text)
        self.assertEqual(rehe.json()["status"], "ACCEPTED")

    # -- 审计 ----------------------------------------------------------------

    def test_audit_valid_with_three_chains(self):
        self.assertEqual(self._correct(1, D1, D4).status_code, 201)
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertTrue(body["valid"], body["detail"])
        self.assertEqual(body["chain_length"], 2)
        self.assertEqual(body["correction_chain_length"], 1)
        self.assertEqual(body["withdrawal_chain_length"], 1)
        self.assertEqual(body["effective_chain_length"], 1)
        self.assertTrue(body["handover_chain"]["valid"])
        self.assertTrue(body["corrections_chain"]["valid"])
        self.assertTrue(body["withdrawals_chain"]["valid"])

    def test_audit_detects_tampered_withdrawn_original(self):
        w = self._withdraw(self.b2)
        self.assertEqual(w.status_code, 201)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE handover_records SET voucher_digest = %s "
                "WHERE exhibit_id = %s AND chain_seq = 2",
                (D3, self.ex_id),
            )
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertEqual(body["handover_chain"]["first_invalid_seq"], 2)
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertEqual(wb["first_invalid_seq"], 1)
        self.assertIn("锚定失效", wb["detail"])

    def test_audit_detects_tampered_withdrawal(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE batch_withdrawals SET reason = %s "
                "WHERE exhibit_id = %s AND withdrawal_seq = 1",
                ("被伪造的原因", self.ex_id),
            )
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["handover_chain"]["valid"])
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertEqual(wb["first_invalid_seq"], 1)

    def test_audit_detects_deleted_withdrawal_ring(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        b3 = self._submit(
            _rec(self.ex_id, "李静", "赵磊", D3, "2026-09-05T09:00:00+08:00")
        )
        self.assertEqual(self._withdraw(b3).status_code, 201)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "DELETE FROM batch_withdrawals "
                "WHERE exhibit_id = %s AND withdrawal_seq = 1",
                (self.ex_id,),
            )
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertEqual(body["withdrawals_chain"]["first_invalid_seq"], 2)

    def test_audit_detects_effective_state_mismatch(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        # 人为把展品保管人改成与撤回后有效状态不符的值
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE exhibits SET custodian = %s WHERE exhibit_id = %s",
                ("幽灵保管人", self.ex_id),
            )
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify"
        ).json()
        self.assertFalse(body["valid"])
        self.assertIn("有效状态不一致", body["withdrawals_chain"]["detail"])

    def test_multi_record_batch_anchor_order_matches_chain_seq(self):
        bid = self._submit({
            "exhibit_id": self.ex_id,
            "records": [
                {"location": "展厅", "from_person": "王强", "to_person": "赵磊",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-03T09:00:00+08:00",
                 "voucher_digest": D3},
                {"location": "库房C", "from_person": "赵磊", "to_person": "孙倩",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-04T09:00:00+08:00",
                 "voucher_digest": D4},
            ],
        })
        r = self._withdraw(bid)
        self.assertEqual(r.status_code, 201, r.text)
        anchors = r.json()["anchor_record_hashes"]
        chain = {x["chain_seq"]: x["record_hash"] for x in self._handovers()["records"]}
        # 锚点按 chain_seq 升序（seq3, seq4），乱序即为篡改
        self.assertEqual(anchors, [chain[3], chain[4]])

    def test_correction_then_referenced_batch_blocks_even_for_inner_record(self):
        # 多记录批次：补正引用其第 1 环（非最后一环）仍阻止撤回整个最新批次
        bid = self._submit({
            "exhibit_id": self.ex_id,
            "records": [
                {"location": "展厅", "from_person": "王强", "to_person": "赵磊",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-03T09:00:00+08:00",
                 "voucher_digest": D3},
                {"location": "库房C", "from_person": "赵磊", "to_person": "孙倩",
                 "seal_no": "SEAL-1", "occurred_at": "2026-09-04T09:00:00+08:00",
                 "voucher_digest": D4},
            ],
        })
        # 该批次两环为 seq=3,4；补正 seq=3
        self.assertEqual(self._correct(3, D3, D1).status_code, 201)
        r = self._withdraw(bid)
        self.assertEqual(r.status_code, 409)
        self.assertIn("补正", r.json()["detail"])

    def test_single_record_correction_endpoint_unchanged_after_withdrawal(self):
        # 兼容：撤回不影响既有单笔补正接口与单环节查询
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        # 被撤回批次（b2=seq2）记录仍可查，且补正接口路径不变（补正本身是另一业务，
        # 这里仅验证查询兼容；补正引用发生在撤回之后不被撤回规则拦截）
        rec = self._record(2)
        self.assertTrue(rec["withdrawn"])
        self.assertEqual(rec["voucher_digest"], D2)
        # 对有效批次 b1 的环节补正仍然正常
        self.assertEqual(self._correct(1, D1, D3).status_code, 201)
        rec1 = self._record(1)
        self.assertEqual(rec1["current_voucher_digest"], D3)
        self.assertFalse(rec1["withdrawn"])


if __name__ == "__main__":
    unittest.main()
