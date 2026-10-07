"""规则引擎单元测试（纯标准库，python3 -m unittest 或 pytest 均可运行）。"""
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from app.validation import (
    ExhibitState,
    validate_batch,
    RULE_FIRST_STATION_LOCATION,
    RULE_FIRST_STATION_CUSTODIAN,
    RULE_CUSTODIAN_CHAIN_BREAK,
    RULE_TIME_NOT_INCREASING,
    RULE_SEAL_CHANGED_WITHOUT_AUTH,
    RULE_INVALID_VOUCHER_DIGEST,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))
DIGEST = "sha256:" + "ab" * 32


def rec(location="库房A", from_person="张伟", to_person="李静", seal_no="SEAL-1",
        occurred_at=T0, voucher_digest=DIGEST, seal_change_reason=None):
    return SimpleNamespace(
        location=location, from_person=from_person, to_person=to_person,
        seal_no=seal_no, occurred_at=occurred_at, voucher_digest=voucher_digest,
        seal_change_reason=seal_change_reason,
    )


def state(**kw):
    base = dict(exhibit_id="EX-1", home_location="库房A", custodian="张伟",
                current_seal="SEAL-1", last_time=None, chain_length=0)
    base.update(kw)
    return ExhibitState(**base)


def rules(item):
    return {r.rule for r in item.reasons}


class TestValidateBatch(unittest.TestCase):
    def test_happy_path_with_authorized_seal_change(self):
        records = [
            rec(),
            rec(location="上海", from_person="李静", to_person="王强",
                seal_no="SEAL-2", occurred_at=T0 + timedelta(hours=1),
                seal_change_reason="馆长书面授权换签"),
            rec(location="展厅", from_person="王强", to_person="陈晨",
                seal_no="SEAL-2", occurred_at=T0 + timedelta(hours=2)),
        ]
        result = validate_batch(state(), records)
        self.assertTrue(result.accepted)
        self.assertIsNone(result.first_break)
        self.assertTrue(all(i.ok for i in result.items))

    def test_first_station_location_mismatch(self):
        result = validate_batch(state(), [rec(location="别处")])
        self.assertFalse(result.accepted)
        self.assertIn(RULE_FIRST_STATION_LOCATION, rules(result.items[0]))
        seq, reason = result.first_break
        self.assertEqual(seq, 1)
        self.assertEqual(reason.rule, RULE_FIRST_STATION_LOCATION)

    def test_first_station_custodian_mismatch(self):
        result = validate_batch(state(), [rec(from_person="陌生人")])
        self.assertIn(RULE_FIRST_STATION_CUSTODIAN, rules(result.items[0]))

    def test_custodian_chain_break(self):
        records = [rec(), rec(from_person="孙倩", occurred_at=T0 + timedelta(hours=1))]
        result = validate_batch(state(), records)
        self.assertIn(RULE_CUSTODIAN_CHAIN_BREAK, rules(result.items[1]))
        self.assertEqual(result.first_break[0], 2)

    def test_time_must_strictly_increase(self):
        records = [rec(), rec(from_person="李静", occurred_at=T0)]  # 等于上一环
        result = validate_batch(state(), records)
        self.assertIn(RULE_TIME_NOT_INCREASING, rules(result.items[1]))

    def test_seal_change_requires_reason(self):
        records = [rec(seal_no="SEAL-NEW")]  # 首环即换签且无理由
        result = validate_batch(state(), records)
        self.assertIn(RULE_SEAL_CHANGED_WITHOUT_AUTH, rules(result.items[0]))

    def test_blank_reason_counts_as_missing(self):
        records = [rec(seal_no="SEAL-NEW", seal_change_reason="   ")]
        result = validate_batch(state(), records)
        self.assertIn(RULE_SEAL_CHANGED_WITHOUT_AUTH, rules(result.items[0]))

    def test_invalid_voucher_digest(self):
        result = validate_batch(state(), [rec(voucher_digest="not-a-digest")])
        self.assertIn(RULE_INVALID_VOUCHER_DIGEST, rules(result.items[0]))

    def test_existing_chain_validates_against_current_state(self):
        st = state(custodian="陈晨", current_seal="SEAL-9",
                   last_time=T0, chain_length=3)
        ok = validate_batch(st, [rec(location="展厅", from_person="陈晨",
                                     seal_no="SEAL-9",
                                     occurred_at=T0 + timedelta(hours=1))])
        self.assertTrue(ok.accepted)
        # 非首条链不做首站校验，但人员/时间/封签须与链头衔接
        bad = validate_batch(st, [rec(location="展厅", from_person="张伟",
                                      seal_no="SEAL-9",
                                      occurred_at=T0 + timedelta(hours=1))])
        self.assertIn(RULE_CUSTODIAN_CHAIN_BREAK, rules(bad.items[0]))
        self.assertNotIn(RULE_FIRST_STATION_LOCATION, rules(bad.items[0]))

    def test_multiple_reasons_on_one_item_and_first_break(self):
        records = [
            rec(),
            rec(from_person="孙倩", seal_no="SEAL-X", occurred_at=T0,
                voucher_digest="bad"),
        ]
        result = validate_batch(state(), records)
        item2 = rules(result.items[1])
        self.assertIn(RULE_CUSTODIAN_CHAIN_BREAK, item2)
        self.assertIn(RULE_TIME_NOT_INCREASING, item2)
        self.assertIn(RULE_SEAL_CHANGED_WITHOUT_AUTH, item2)
        self.assertIn(RULE_INVALID_VOUCHER_DIGEST, item2)
        self.assertEqual(result.first_break[0], 2)


if __name__ == "__main__":
    unittest.main()
