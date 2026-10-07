"""展品状态令牌：预演签发、正式提交校验（自包含、无状态、不落库）。

令牌格式::

    stv1.<base64url(payload_json)>.<base64url(hmac_sha256(secret, "stv1." + body))>

payload 绑定展品与签发时刻的哈希链状态（chain_length + chain_head_hash）：

- 改任意字节 / 伪造签名  -> 令牌格式无效（提交时 400 参数错误）
- 令牌属于其他展品       -> 跨展品状态冲突（提交时 409）
- 展品链被其他批次推进   -> 链状态冲突（提交时 409，调用方须重新预演）

链状态仅在批次 ACCEPTED 时推进；REJECTED 批次只落报告、不动链，
因此被驳回后凭原令牌修正载荷重提仍然有效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

TOKEN_VERSION = "stv1"

# 演示环境默认值；生产部署必须通过环境变量覆盖（多实例需共享同一密钥）。
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "dev-insecure-rehearsal-secret-change-me")


class InvalidTokenError(ValueError):
    """令牌格式非法、字段缺失或签名校验失败。"""


@dataclass(frozen=True)
class StateToken:
    exhibit_id: str
    chain_length: int
    chain_head_hash: str
    issued_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    try:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise InvalidTokenError("令牌 base64 段非法") from exc


def issue_token(
    secret: str,
    exhibit_id: str,
    chain_length: int,
    chain_head_hash: str,
    issued_at: int | None = None,
) -> str:
    """签发绑定展品当前链状态的令牌。"""
    payload = {
        "v": 1,
        "ex": exhibit_id,
        "n": int(chain_length),
        "h": chain_head_hash,
        "iat": int(issued_at if issued_at is not None else time.time()),
    }
    body = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{TOKEN_VERSION}.{body}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{TOKEN_VERSION}.{body}.{_b64encode(signature)}"


def parse_token(secret: str, raw: object) -> StateToken:
    """解析并验签；任何不符都抛 InvalidTokenError（对应 400 参数错误）。"""
    if not isinstance(raw, str):
        raise InvalidTokenError("令牌必须是字符串")
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION or not parts[1] or not parts[2]:
        raise InvalidTokenError(f"令牌格式应为 {TOKEN_VERSION}.<payload>.<signature>")
    _, body, sig_b64 = parts

    given_sig = _b64decode(sig_b64)
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        f"{TOKEN_VERSION}.{body}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(given_sig, expected_sig):
        raise InvalidTokenError("令牌签名校验失败")

    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidTokenError("令牌载荷不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidTokenError("令牌载荷必须是 JSON 对象")

    try:
        version = payload["v"]
        exhibit_id = payload["ex"]
        chain_length = payload["n"]
        chain_head_hash = payload["h"]
        issued_at = payload["iat"]
    except KeyError as exc:
        raise InvalidTokenError(f"令牌载荷缺少字段 {exc}") from exc

    # bool 是 int 的子类，需显式排除
    if version != 1:
        raise InvalidTokenError("令牌版本不受支持")
    if not isinstance(exhibit_id, str) or not exhibit_id:
        raise InvalidTokenError("令牌展品字段非法")
    if not isinstance(chain_length, int) or isinstance(chain_length, bool) or chain_length < 0:
        raise InvalidTokenError("令牌链长字段非法")
    if not isinstance(chain_head_hash, str) or not chain_head_hash:
        raise InvalidTokenError("令牌链头哈希字段非法")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        raise InvalidTokenError("令牌签发时间字段非法")

    return StateToken(exhibit_id, chain_length, chain_head_hash, issued_at)


def state_matches(token: StateToken, chain_length: int, chain_head_hash: str) -> bool:
    """令牌快照与展品当前链状态是否一致（链推进后两项必然同时变化）。"""
    return token.chain_length == chain_length and token.chain_head_hash == chain_head_hash
