"""状态令牌单元测试（纯标准库，python3 -m unittest 或 pytest 均可运行）。"""
import unittest

from app.tokens import (
    InvalidTokenError,
    StateToken,
    issue_token,
    parse_token,
    state_matches,
)

SECRET = "unit-test-secret"


class TestStateToken(unittest.TestCase):
    def test_roundtrip(self):
        raw = issue_token(SECRET, "EX-1", 3, "abc123", issued_at=1780000000)
        tok = parse_token(SECRET, raw)
        self.assertEqual(
            tok, StateToken(exhibit_id="EX-1", chain_length=3,
                            chain_head_hash="abc123", issued_at=1780000000)
        )
        self.assertTrue(raw.startswith("stv1."))

    def test_wrong_secret_rejected(self):
        raw = issue_token(SECRET, "EX-1", 0, "GENESIS")
        with self.assertRaises(InvalidTokenError):
            parse_token("another-secret", raw)

    def test_tampered_body_rejected(self):
        raw = issue_token(SECRET, "EX-1", 0, "GENESIS")
        prefix, body, sig = raw.split(".")
        # 翻转载荷最后一个字符的样子（仍需是合法 base64 字符以走到签名比对）
        tampered = body[:-1] + ("A" if body[-1] != "A" else "B")
        with self.assertRaises(InvalidTokenError):
            parse_token(SECRET, f"{prefix}.{tampered}.{sig}")

    def test_tampered_signature_rejected(self):
        raw = issue_token(SECRET, "EX-1", 0, "GENESIS")
        prefix, body, sig = raw.split(".")
        forged = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        with self.assertRaises(InvalidTokenError):
            parse_token(SECRET, f"{prefix}.{body}.{forged}")

    def test_malformed_strings_rejected(self):
        for bad in ["", "garbage", "stv1.onlyone", "stv1..", "stv2.abc.def",
                    "stv1.a.b.c", "stv1.%%%.sig"]:
            with self.assertRaises(InvalidTokenError, msg=bad):
                parse_token(SECRET, bad)

    def test_non_string_rejected(self):
        with self.assertRaises(InvalidTokenError):
            parse_token(SECRET, None)
        with self.assertRaises(InvalidTokenError):
            parse_token(SECRET, 123)

    def test_state_matches_only_when_chain_unchanged(self):
        tok = parse_token(SECRET, issue_token(SECRET, "EX-1", 3, "head-3"))
        self.assertTrue(state_matches(tok, 3, "head-3"))
        # 链推进：长度与链头同时变化
        self.assertFalse(state_matches(tok, 4, "head-4"))
        # 仅一项变化也不接受
        self.assertFalse(state_matches(tok, 4, "head-3"))
        self.assertFalse(state_matches(tok, 3, "other"))


if __name__ == "__main__":
    unittest.main()
