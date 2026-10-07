"""借展车次整车交接端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 全数通过：同一事务为各件写入既有批次与原交接哈希链，返回车次号及各批次报告；
  单件查询接口、交接链与三链审计对新入链记录保持兼容
- 任一件核验失败：保存整单拒绝报告（列出各件逐条结果），不写交接批次和记录、
  不推进任何展品状态；同键重放原拒绝报告
- 展品不存在（404）/ 请求格式不合法（422：件数越界、展品重复、记录格式非法）
  整单报错且不写入
- 幂等：同键同载荷（含拒绝结果）重放原报告；同键异载荷 409
- 逐件以撤回后的有效状态核验；撤回/补正/单件接口对整车入链记录行为不变
- 并发：重叠车次、整车与单件并发提交均不产生部分入链

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app@localhost:5432/seals' python3 -m pytest tests/test_api_vehicle_handovers.py
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
D5 = "sha256:" + "34" * 32
D6 = "sha256:" + "56" * 32


def _handover(frm, to, digest, when, location="展厅", seal="SEAL-1", reason=None):
    body = {
        "location": location, "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    if reason:
        body["seal_change_reason"] = reason
    return body


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestVehicleHandoversApi(unittest.TestCase):
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
        suffix = uuid.uuid4().hex[:10]
        self.ex_a = f"EX-VA-{suffix}"
        self.ex_b = f"EX-VB-{suffix}"
        self.vehicle_id = f"TRAIN-{suffix}"
        for ex_id, custodian, home in (
            (self.ex_a, "张伟", "库房A"),
            (self.ex_b, "王芳", "库房B"),
        ):
            r = self.client.post("/api/v1/exhibits", json={
                "exhibit_id": ex_id, "name": "整车测试展品",
                "home_location": home, "custodian": custodian,
                "initial_seal": "SEAL-1",
            })
            self.assertEqual(r.status_code, 201, r.text)

    # -- 辅助 ----------------------------------------------------------------

    def _vehicle_payload(self, items, vehicle_id=None):
        return {"vehicle_id": vehicle_id or self.vehicle_id, "items": items}

    def _item(self, exhibit_id, records):
        return {"exhibit_id": exhibit_id, "records": records}

    def _submit_vehicle(self, payload, key=None):
        return self.client.post(
            "/api/v1/vehicle-handovers", json=payload,
            headers={"Idempotency-Key": key or f"veh-{uuid.uuid4()}"},
        )

    def _submit_single(self, exhibit_id, records, key=None):
        return self.client.post(
            "/api/v1/handover-batches",
            json={"exhibit_id": exhibit_id, "records": records},
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )

    def _state(self, exhibit_id):
        r = self.client.get(f"/api/v1/exhibits/{exhibit_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _batches(self, exhibit_id):
        r = self.client.get(f"/api/v1/exhibits/{exhibit_id}/batches")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _audit(self, exhibit_id):
        r = self.client.get(f"/api/v1/exhibits/{exhibit_id}/audit/verify")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _valid_items(self):
        """两件展品各一条合规首站记录（张伟/王芳为登记保管人）。"""
        return [
            self._item(self.ex_a, [
                _handover("张伟", "李静", D1, "2026-09-01T09:00:00+08:00", location="库房A"),
            ]),
            self._item(self.ex_b, [
                _handover("王芳", "赵磊", D2, "2026-09-01T10:00:00+08:00", location="库房B"),
            ]),
        ]

    # -- 全数通过 ------------------------------------------------------------

    def test_accept_all_items_writes_batches_and_chains_atomically(self):
        payload = self._vehicle_payload(self._valid_items())
        r = self._submit_vehicle(payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(body["vehicle_id"], self.vehicle_id)
        self.assertEqual(body["item_count"], 2)
        self.assertFalse(body["idempotent_replay"])
        by_exhibit = {it["exhibit_id"]: it for it in body["items"]}
        self.assertEqual(set(by_exhibit), {self.ex_a, self.ex_b})
        for ex_id in (self.ex_a, self.ex_b):
            item = by_exhibit[ex_id]
            self.assertEqual(item["status"], "ACCEPTED")
            self.assertEqual(item["record_count"], 1)
            self.assertIsNotNone(item["batch_id"])
            self.assertIsNone(item["first_break"])
            self.assertEqual(item["items"], [{"seq": 1, "ok": True, "reasons": []}])

            # 各件写入既有批次：单件批次查询接口可直接查到同一报告
            batch = self.client.get(
                f"/api/v1/handover-batches/{item['batch_id']}"
            )
            self.assertEqual(batch.status_code, 200)
            self.assertEqual(batch.json()["exhibit_id"], ex_id)
            self.assertEqual(batch.json()["status"], "ACCEPTED")

            # 原交接哈希链推进一环，链头与记录一致
            state = self._state(ex_id)
            self.assertEqual(state["chain_length"], 1)
            chain = self.client.get(f"/api/v1/exhibits/{ex_id}/handovers").json()
            self.assertEqual(chain["chain_length"], 1)
            rec = chain["records"][0]
            self.assertEqual(rec["batch_id"], item["batch_id"])
            self.assertEqual(rec["prev_hash"], "GENESIS")
            self.assertEqual(state["chain_head_hash"], rec["record_hash"])

            # 三链审计复算通过
            audit = self._audit(ex_id)
            self.assertTrue(audit["valid"], audit["detail"])

        # 整单报告可按整单号查询（内容一致、非重放标记）
        order = self.client.get(
            f"/api/v1/vehicle-handovers/{body['vehicle_order_id']}"
        )
        self.assertEqual(order.status_code, 200)
        self.assertEqual(order.json()["vehicle_order_id"], body["vehicle_order_id"])
        self.assertFalse(order.json()["idempotent_replay"])
        self.assertEqual(
            [it["batch_id"] for it in order.json()["items"]],
            [it["batch_id"] for it in body["items"]],
        )

    def test_accept_multi_record_items_and_20_items_upper_bound(self):
        # 20 件展品、其中一件含多条时序记录：一次整车确认
        exhibits = []
        for i in range(20):
            ex_id = f"EX-VN-{uuid.uuid4().hex[:10]}"
            r = self.client.post("/api/v1/exhibits", json={
                "exhibit_id": ex_id, "name": f"整车上限测试{i}",
                "home_location": "库房A", "custodian": "张伟",
                "initial_seal": "SEAL-1",
            })
            self.assertEqual(r.status_code, 201, r.text)
            exhibits.append(ex_id)
        items = []
        for i, ex_id in enumerate(exhibits):
            records = [
                _handover("张伟", "李静", D1, "2026-09-01T09:00:00+08:00", location="库房A"),
            ]
            if i == 0:
                records.append(
                    _handover("李静", "王强", D2, "2026-09-02T09:00:00+08:00")
                )
            items.append(self._item(ex_id, records))
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(body["item_count"], 20)
        self.assertEqual(self._state(exhibits[0])["chain_length"], 2)
        self.assertEqual(self._state(exhibits[0])["custodian"], "王强")
        for ex_id in exhibits[1:]:
            self.assertEqual(self._state(ex_id)["chain_length"], 1)
        self.assertTrue(self._audit(exhibits[0])["valid"])

    # -- 任一件失败：整单拒绝 -------------------------------------------------

    def test_reject_saves_order_report_but_writes_nothing(self):
        items = self._valid_items()
        # 第二件交出人不是登记保管人 -> 该件 REJECTED，整单拒绝
        items[1]["records"][0]["from_person"] = "陌生人"
        payload = self._vehicle_payload(items)
        r = self._submit_vehicle(payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "REJECTED")
        by_exhibit = {it["exhibit_id"]: it for it in body["items"]}
        # 各件逐条结果：合格件仍给出逐条通过结果，不合格件给出断点与原因
        self.assertEqual(by_exhibit[self.ex_a]["status"], "ACCEPTED")
        self.assertIsNone(by_exhibit[self.ex_a]["batch_id"])
        bad = by_exhibit[self.ex_b]
        self.assertEqual(bad["status"], "REJECTED")
        self.assertIsNone(bad["batch_id"])
        self.assertEqual(bad["first_break"]["rule"], "FIRST_STATION_CUSTODIAN")
        self.assertFalse(bad["items"][0]["ok"])

        # 不写交接批次和记录、不推进任何展品状态（含逐件合格的 ex_a）
        for ex_id, custodian in ((self.ex_a, "张伟"), (self.ex_b, "王芳")):
            state = self._state(ex_id)
            self.assertEqual(state["chain_length"], 0)
            self.assertEqual(state["custodian"], custodian)
            self.assertEqual(self._batches(ex_id), [])
            chain = self.client.get(f"/api/v1/exhibits/{ex_id}/handovers").json()
            self.assertEqual(chain["records"], [])

        # 整单拒绝报告已保存，可按整单号查询
        order = self.client.get(
            f"/api/v1/vehicle-handovers/{body['vehicle_order_id']}"
        )
        self.assertEqual(order.status_code, 200)
        self.assertEqual(order.json()["status"], "REJECTED")
        self.assertEqual(order.json()["items"], body["items"])

    def test_rejected_order_replays_original_report(self):
        items = self._valid_items()
        items[0]["records"][0]["voucher_digest"] = "not-a-digest"
        payload = self._vehicle_payload(items)
        key = f"veh-{uuid.uuid4()}"
        first = self._submit_vehicle(payload, key=key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "REJECTED")

        replay = self._submit_vehicle(payload, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["vehicle_order_id"],
                         first.json()["vehicle_order_id"])
        self.assertEqual(replay.json()["items"], first.json()["items"])
        # 重放不产生任何副作用
        self.assertEqual(self._state(self.ex_a)["chain_length"], 0)
        self.assertEqual(self._state(self.ex_b)["chain_length"], 0)

    # -- 格式与存在性：整单报错且不写入 ----------------------------------------

    def test_unknown_exhibit_404_and_nothing_written(self):
        items = self._valid_items()
        items.append(self._item("EX-NO-SUCH", [
            _handover("周明", "吴迪", D3, "2026-09-01T11:00:00+08:00"),
        ]))
        key = f"veh-{uuid.uuid4()}"
        payload = self._vehicle_payload(items)
        r = self._submit_vehicle(payload, key=key)
        self.assertEqual(r.status_code, 404, r.text)
        # 不写入：合格展品状态不变，且同键重提仍按 404 处理（无整单报告可重放）
        self.assertEqual(self._state(self.ex_a)["chain_length"], 0)
        self.assertEqual(self._state(self.ex_b)["chain_length"], 0)
        again = self._submit_vehicle(payload, key=key)
        self.assertEqual(again.status_code, 404)

    def test_item_count_bounds_422(self):
        one = self._vehicle_payload(self._valid_items()[:1])
        self.assertEqual(self._submit_vehicle(one).status_code, 422)
        many = self._vehicle_payload(
            [self._item(f"EX-X{i:02d}", [
                _handover("a", "b", D1, "2026-09-01T09:00:00+08:00"),
            ]) for i in range(21)]
        )
        self.assertEqual(self._submit_vehicle(many).status_code, 422)
        self.assertEqual(self._state(self.ex_a)["chain_length"], 0)

    def test_duplicate_exhibit_ids_422(self):
        items = self._valid_items()
        items.append(self._item(self.ex_a, [
            _handover("李静", "王强", D3, "2026-09-02T09:00:00+08:00"),
        ]))
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._state(self.ex_a)["chain_length"], 0)

    def test_invalid_record_format_422(self):
        items = self._valid_items()
        items[0]["records"][0]["occurred_at"] = "2026-09-01 09:00:00"  # 缺时区
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 422)
        items = self._valid_items()
        items[1]["records"] = []  # 记录为空
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 422)

    def test_get_unknown_vehicle_order_404(self):
        r = self.client.get(f"/api/v1/vehicle-handovers/{uuid.uuid4()}")
        self.assertEqual(r.status_code, 404)

    # -- 幂等 -----------------------------------------------------------------

    def test_same_key_same_payload_replay_after_accept(self):
        payload = self._vehicle_payload(self._valid_items())
        key = f"veh-{uuid.uuid4()}"
        first = self._submit_vehicle(payload, key=key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "ACCEPTED")

        replay = self._submit_vehicle(payload, key=key)
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["vehicle_order_id"], first.json()["vehicle_order_id"])
        self.assertEqual(body["items"], first.json()["items"])
        # 不重复写入：两件展品链长仍为 1
        self.assertEqual(self._state(self.ex_a)["chain_length"], 1)
        self.assertEqual(self._state(self.ex_b)["chain_length"], 1)
        self.assertEqual(len(self._batches(self.ex_a)), 1)

    def test_same_key_different_payload_409(self):
        key = f"veh-{uuid.uuid4()}"
        first = self._submit_vehicle(self._vehicle_payload(self._valid_items()), key=key)
        self.assertEqual(first.status_code, 200)
        # 同键异载荷（换一条记录）-> 409
        other = self._valid_items()
        other[0]["records"][0]["to_person"] = "王强"
        r = self._submit_vehicle(self._vehicle_payload(other), key=key)
        self.assertEqual(r.status_code, 409, r.text)
        # 同键异载荷（换车次号）-> 409
        r = self._submit_vehicle(
            self._vehicle_payload(self._valid_items(), vehicle_id="TRAIN-OTHER"),
            key=key,
        )
        self.assertEqual(r.status_code, 409)

    # -- 撤回后有效状态 --------------------------------------------------------

    def test_validation_uses_effective_state_after_withdrawal(self):
        # ex_a 先入链两批（张伟->李静->王强），随后撤回最新批次（有效保管人回退为李静）
        b1 = self._submit_single(self.ex_a, [
            _handover("张伟", "李静", D1, "2026-09-01T09:00:00+08:00", location="库房A"),
        ]).json()["batch_id"]
        b2 = self._submit_single(self.ex_a, [
            _handover("李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        ]).json()["batch_id"]
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_a}/batch-withdrawals",
            json={"batch_id": b2, "reason": "批次误登记，撤回"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self._state(self.ex_a)["custodian"], "李静")

        # 整车单按撤回后有效状态（李静持有）核验：从李静出发 -> 通过
        items = [
            self._item(self.ex_a, [
                _handover("李静", "赵磊", D3, "2026-09-03T09:00:00+08:00"),
            ]),
            self._item(self.ex_b, [
                _handover("王芳", "陈晨", D4, "2026-09-03T10:00:00+08:00", location="库房B"),
            ]),
        ]
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        # 新记录线性追加在原链物理链尾（seq=3，prev_hash 接物理上一环——已撤回的 seq=2）
        chain = self.client.get(f"/api/v1/exhibits/{self.ex_a}/handovers").json()
        self.assertEqual(chain["chain_length"], 3)
        self.assertEqual(chain["effective_chain_length"], 2)
        self.assertEqual(chain["records"][2]["prev_hash"],
                         chain["records"][1]["record_hash"])
        self.assertTrue(chain["records"][1]["withdrawn"])
        self.assertTrue(self._audit(self.ex_a)["valid"])

        # 沿用被撤回批次的交出人（王强）则按撤回后状态判不合格 -> 整单拒绝
        items[0]["records"][0] = _handover(
            "王强", "赵磊", D5, "2026-09-04T09:00:00+08:00"
        )
        r = self._submit_vehicle(self._vehicle_payload(items))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "REJECTED")
        self.assertEqual(self._state(self.ex_a)["chain_length"], 3)
        self.assertEqual(self._state(self.ex_b)["chain_length"], 1)

    # -- 与既有功能兼容 --------------------------------------------------------

    def test_vehicle_batches_work_with_single_endpoints_corrections_withdrawals(self):
        body = self._submit_vehicle(self._vehicle_payload(self._valid_items())).json()
        self.assertEqual(body["status"], "ACCEPTED")
        by_exhibit = {it["exhibit_id"]: it for it in body["items"]}

        # 单件接口继续提交：链在整车入链记录后续接
        r = self._submit_single(self.ex_a, [
            _handover("李静", "王强", D3, "2026-09-02T09:00:00+08:00"),
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        self.assertEqual(self._state(self.ex_a)["chain_length"], 2)

        # 对整车入链记录提交补正：补正链正常锚定
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_b}/handovers/1/digest-corrections",
            json={"expected_digest": D2, "new_digest": D6, "reason": "摘要录入笔误"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)

        # 撤回 ex_a 最新批次（单件接口写入的那批）后，整车批次成为最新可撤回批次
        latest = self._batches(self.ex_a)[-1]["batch_id"]
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_a}/batch-withdrawals",
            json={"batch_id": latest, "reason": "撤回最新批次"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_a}/batch-withdrawals",
            json={"batch_id": by_exhibit[self.ex_a]["batch_id"], "reason": "撤回整车批次"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self._state(self.ex_a)["custodian"], "张伟")

        # 三链审计均通过
        self.assertTrue(self._audit(self.ex_a)["valid"])
        self.assertTrue(self._audit(self.ex_b)["valid"])

    # -- 并发 -----------------------------------------------------------------

    def test_concurrent_overlapping_vehicle_orders_no_partial_chain(self):
        # 第三件展品，仅被车次 B 覆盖
        ex_c = f"EX-VC-{uuid.uuid4().hex[:10]}"
        self.assertEqual(self.client.post("/api/v1/exhibits", json={
            "exhibit_id": ex_c, "name": "整车并发测试", "home_location": "库房C",
            "custodian": "周明", "initial_seal": "SEAL-1",
        }).status_code, 201)

        # 车次 A：ex_a(张伟->李静) + ex_b(王芳->赵磊)
        payload_a = self._vehicle_payload(self._valid_items(), vehicle_id="TRAIN-A")
        # 车次 B：ex_a(李静->王强，仅当 A 先落链才合格) + ex_c(周明->吴迪)
        payload_b = self._vehicle_payload([
            self._item(self.ex_a, [
                _handover("李静", "王强", D3, "2026-09-02T09:00:00+08:00"),
            ]),
            self._item(ex_c, [
                _handover("周明", "吴迪", D4, "2026-09-02T10:00:00+08:00", location="库房C"),
            ]),
        ], vehicle_id="TRAIN-B")

        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def worker(name, payload):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                "/api/v1/vehicle-handovers", json=payload,
                headers={"Idempotency-Key": f"veh-{name}-{uuid.uuid4()}"},
            )
            results[name] = {"status_code": r.status_code, "body": r.json()}
            client.close()

        t1 = threading.Thread(target=worker, args=("A", payload_a))
        t2 = threading.Thread(target=worker, args=("B", payload_b))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results["A"]["status_code"], 200, results)
        self.assertEqual(results["B"]["status_code"], 200, results)
        status_a = results["A"]["body"]["status"]
        status_b = results["B"]["body"]["status"]
        # B 的 ex_a 项只有在 A 整车入链后才可能合格，因此不存在
        # 「B 部分入链」的中间态：要么 A 先（A ACCEPTED、B ACCEPTED），
        # 要么 B 先（B 因 ex_a 人员断裂整单 REJECTED、ex_c 不落链）
        if status_b == "ACCEPTED":
            self.assertEqual(status_a, "ACCEPTED")
            self.assertEqual(self._state(self.ex_a)["chain_length"], 2)
            self.assertEqual(self._state(ex_c)["chain_length"], 1)
        else:
            self.assertEqual(status_b, "REJECTED")
            self.assertEqual(status_a, "ACCEPTED")
            self.assertEqual(self._state(self.ex_a)["chain_length"], 1)
            self.assertEqual(self._state(ex_c)["chain_length"], 0)
            self.assertEqual(self._batches(ex_c), [])
        # 无论哪种时序：所有展品链完整、无部分入链
        for ex_id in (self.ex_a, self.ex_b, ex_c):
            self.assertTrue(self._audit(ex_id)["valid"], ex_id)

    def test_concurrent_vehicle_and_single_submission_no_partial_chain(self):
        # 同一保管人衔接：整车单（ex_a+ex_b）与单件（ex_a）竞争同一状态，
        # 先落链者使另一方的 ex_a 核验失败 -> 另一方整单拒绝，绝不部分入链
        vehicle_payload = self._vehicle_payload(self._valid_items())
        single_payload = {
            "exhibit_id": self.ex_a,
            "records": [_handover("张伟", "李静", D3,
                                  "2026-09-01T09:00:00+08:00", location="库房A")],
        }

        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}

        def worker(name):
            client = TestClient(self.main.app)
            barrier.wait()
            if name == "vehicle":
                r = client.post(
                    "/api/v1/vehicle-handovers", json=vehicle_payload,
                    headers={"Idempotency-Key": f"veh-{uuid.uuid4()}"},
                )
            else:
                r = client.post(
                    "/api/v1/handover-batches", json=single_payload,
                    headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
                )
            results[name] = {"status_code": r.status_code, "body": r.json()}
            client.close()

        t1 = threading.Thread(target=worker, args=("vehicle",))
        t2 = threading.Thread(target=worker, args=("single",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results["vehicle"]["status_code"], 200, results)
        self.assertEqual(results["single"]["status_code"], 200, results)
        vehicle_ok = results["vehicle"]["body"]["status"] == "ACCEPTED"
        single_ok = results["single"]["body"]["status"] == "ACCEPTED"
        # 二者竞争同一衔接点，至多一方成功
        self.assertFalse(vehicle_ok and single_ok)
        # ex_a 链长恰为 1（胜者的那一环），ex_b 与整车单结果严格一致
        self.assertEqual(self._state(self.ex_a)["chain_length"], 1)
        self.assertEqual(self._state(self.ex_b)["chain_length"],
                         1 if vehicle_ok else 0)
        self.assertTrue(self._audit(self.ex_a)["valid"])
        self.assertTrue(self._audit(self.ex_b)["valid"])

    def test_concurrent_same_key_same_payload_only_one_writes(self):
        payload = self._vehicle_payload(self._valid_items())
        key = f"veh-{uuid.uuid4()}"
        barrier = threading.Barrier(2)
        results = []

        def worker():
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                "/api/v1/vehicle-handovers", json=payload,
                headers={"Idempotency-Key": key},
            )
            results.append(r.json())
            client.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()

        # 一笔落单、一笔重放；整单号一致，链只推进一次
        replays = sorted(b["idempotent_replay"] for b in results)
        self.assertEqual(replays, [False, True], results)
        self.assertEqual(results[0]["vehicle_order_id"],
                         results[1]["vehicle_order_id"])
        self.assertEqual(self._state(self.ex_a)["chain_length"], 1)
        self.assertEqual(self._state(self.ex_b)["chain_length"], 1)


if __name__ == "__main__":
    unittest.main()
