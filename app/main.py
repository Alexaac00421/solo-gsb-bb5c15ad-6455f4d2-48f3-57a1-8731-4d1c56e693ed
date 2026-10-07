"""HTTP API：文物借展封签交接核验服务。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import __version__
from .db import get_conn, wait_for_db
from .hashing import (
    CORRECTIONS_GENESIS_HASH,
    GENESIS_HASH,
    WITHDRAWALS_GENESIS_HASH,
    correction_hash,
    correction_request_hash,
    record_hash,
    request_hash,
    vehicle_request_hash,
    withdrawal_hash,
    withdrawal_request_hash,
)
from .migrations import ensure_schema
from .schemas import (
    AsOfStateOut,
    AuditVerifyOut,
    BatchReport,
    BatchRequest,
    BatchSummary,
    ChainOut,
    CorrectionOut,
    CorrectionRequest,
    ExhibitCreate,
    ExhibitOut,
    HandoverRecordOut,
    RehearsalOut,
    VehicleHandoverReport,
    VehicleHandoverRequest,
    WithdrawalOut,
    WithdrawalRequest,
)
from .tokens import (
    InvalidTokenError,
    TOKEN_SECRET,
    issue_token,
    parse_token,
    state_matches,
)
from .validation import ExhibitState, is_valid_voucher_digest, validate_batch


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    ensure_schema()
    yield


app = FastAPI(
    title="文物借展封签交接核验服务",
    version=__version__,
    lifespan=lifespan,
    description="校验借展交接链的首站、人员衔接、时间递增与封签连续性；"
                "幂等写入，失败批次不落任何交接记录，保留可查询的哈希审计链。",
)

# ---------------------------------------------------------------- 工具


def _withdrawn_batch_ids(conn, exhibit_id: str) -> set[uuid.UUID]:
    """该展品全部已撤回批次的 batch_id 集合（撤回链按批次只追加、不可撤销）。"""
    rows = conn.execute(
        "SELECT batch_id FROM batch_withdrawals WHERE exhibit_id = %s",
        (exhibit_id,),
    ).fetchall()
    return {r["batch_id"] for r in rows}


def _effective_chain_rows(conn, exhibit: dict, withdrawn: set[uuid.UUID]) -> list[dict]:
    """按撤回链排除被撤回批次后的有效交接记录（按 chain_seq 有序）。"""
    rows = conn.execute(
        """
        SELECT * FROM handover_records
         WHERE exhibit_id = %s
         ORDER BY chain_seq
        """,
        (exhibit["exhibit_id"],),
    ).fetchall()
    return [r for r in rows if r["batch_id"] not in withdrawn]


def _effective_state(conn, exhibit: dict, withdrawn: set[uuid.UUID] | None = None) -> dict:
    """撤回后展品当前有效状态：保管人/封签/最近时间/有效链长。

    撤回不删除、不改写任何原交接记录，也不改变 chain_length/chain_head_hash；
    当前状态由「排除被撤回批次后的最后一环有效记录」重放得出。
    有效链为空（全部撤回或尚未交接）时回退到登记状态。
    """
    if withdrawn is None:
        withdrawn = _withdrawn_batch_ids(conn, exhibit["exhibit_id"])
    effective = _effective_chain_rows(conn, exhibit, withdrawn)
    if effective:
        last = effective[-1]
        return {
            "custodian": last["to_person"],
            "current_seal": last["seal_no"],
            "last_time": last["occurred_at"],
            "effective_chain_length": len(effective),
        }
    # 有效链为空：回退到登记保管人/初始封签（旧卷可能无登记列，以首环字段兜底）
    custodian = exhibit.get("registered_custodian")
    seal = exhibit.get("registered_initial_seal")
    first = conn.execute(
        """
        SELECT from_person, seal_no FROM handover_records
         WHERE exhibit_id = %s ORDER BY chain_seq LIMIT 1
        """,
        (exhibit["exhibit_id"],),
    ).fetchone()
    if custodian is None and first is not None:
        custodian = first["from_person"]
    if seal is None and first is not None:
        seal = first["seal_no"]
    return {
        "custodian": custodian or exhibit["custodian"],
        "current_seal": seal or exhibit["current_seal"],
        "last_time": None,
        "effective_chain_length": 0,
    }


def _exhibit_state(row: dict, effective: dict | None = None) -> ExhibitState:
    """构造规则引擎参照状态。

    默认使用展品当前（物理）状态；撤回后提交/预演须传入按撤回链计算的
    有效状态（保管人/封签/最近时间/有效链长），使首站等规则以撤回后状态为基准。
    """
    if effective is None:
        custodian = row["custodian"]
        current_seal = row["current_seal"]
        last_time = row["last_time"]
        chain_length = row["chain_length"]
    else:
        custodian = effective["custodian"]
        current_seal = effective["current_seal"]
        last_time = effective["last_time"]
        chain_length = effective["effective_chain_length"]
    return ExhibitState(
        exhibit_id=row["exhibit_id"],
        home_location=row["home_location"],
        custodian=custodian,
        current_seal=current_seal,
        last_time=last_time,
        chain_length=chain_length,
    )


def _serialize_items(result) -> list[dict]:
    return [
        {
            "seq": item.seq,
            "ok": item.ok,
            "reasons": [{"rule": r.rule, "message": r.message} for r in item.reasons],
        }
        for item in result.items
    ]


def _serialize_first_break(result) -> dict | None:
    fb = result.first_break
    if fb is None:
        return None
    seq, reason = fb
    return {"seq": seq, "rule": reason.rule, "message": reason.message}


def _build_report(batch_id: uuid.UUID, exhibit_id: str, result, record_count: int, now: datetime) -> dict:
    return {
        "batch_id": str(batch_id),
        "exhibit_id": exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": record_count,
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "idempotent_replay": False,
        "created_at": now.isoformat(),
    }


def _replay_report(row: dict) -> dict:
    report = dict(row["report"])
    report["idempotent_replay"] = True
    return report


def _write_accepted_batch(conn, exhibit: dict, effective: dict, exhibit_id: str,
                          records, batch_id: uuid.UUID) -> None:
    """把一批已核验通过的交接记录线性追加到展品原交接链物理链尾并推进链上状态。

    单件提交与车次整车交接共用的同一写入路径：chain_seq 连续、prev_hash 指向
    上一物理环（含已撤回历史环）——原链是完整不可变的追加式历史，撤回只通过
    旁路撤回链标记批次失效，绝不打断原哈希链。业务参照系（交出人/封签/时间
    基准）由调用方按撤回后有效状态核验，本函数只负责落链。
    """
    prev_hash = exhibit["chain_head_hash"]
    prev_seal = effective["current_seal"]
    chain_seq = exhibit["chain_length"]
    for seq_in_batch, rec in enumerate(records, start=1):
        chain_seq += 1
        h = record_hash(
            prev_hash=prev_hash,
            exhibit_id=exhibit_id,
            chain_seq=chain_seq,
            batch_id=batch_id,
            location=rec.location,
            from_person=rec.from_person,
            to_person=rec.to_person,
            seal_no=rec.seal_no,
            seal_change_reason=rec.seal_change_reason,
            occurred_at=rec.occurred_at,
            voucher_digest=rec.voucher_digest,
        )
        conn.execute(
            """
            INSERT INTO handover_records
                (batch_id, exhibit_id, chain_seq, seq_in_batch, location,
                 from_person, to_person, seal_no, seal_changed, seal_change_reason,
                 occurred_at, voucher_digest, prev_hash, record_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (batch_id, exhibit_id, chain_seq, seq_in_batch, rec.location,
             rec.from_person, rec.to_person, rec.seal_no, rec.seal_no != prev_seal,
             rec.seal_change_reason, rec.occurred_at, rec.voucher_digest, prev_hash, h),
        )
        prev_hash = h
        prev_seal = rec.seal_no

    last = records[-1]
    conn.execute(
        """
        UPDATE exhibits
           SET custodian = %s, current_seal = %s, last_time = %s,
               chain_length = %s, chain_head_hash = %s
         WHERE exhibit_id = %s
        """,
        (last.to_person, last.seal_no, last.occurred_at, chain_seq,
         prev_hash, exhibit_id),
    )


# ---------------------------------------------------------------- 补正


def _serialize_correction(row: dict, *, replay: bool) -> dict:
    return {
        "correction_id": str(row["correction_id"]),
        "exhibit_id": row["exhibit_id"],
        "target_chain_seq": row["target_chain_seq"],
        "expected_digest": row["expected_digest"],
        "old_digest": row["old_digest"],
        "new_digest": row["new_digest"],
        "reason": row["reason"],
        "anchor_record_hash": row["anchor_record_hash"],
        "correction_seq": row["correction_seq"],
        "prev_hash": row["prev_hash"],
        "record_hash": row["record_hash"],
        "idempotent_replay": replay,
        "created_at": row["created_at"].isoformat(),
    }


def _load_corrections_by_target(conn, exhibit_id: str) -> dict[int, list[dict]]:
    """按展品取全部补正单（补正时序），索引到目标链序号。"""
    rows = conn.execute(
        """
        SELECT correction_id, target_chain_seq, correction_seq, expected_digest,
               old_digest, new_digest, reason, anchor_record_hash, prev_hash,
               record_hash, created_at
          FROM digest_corrections
         WHERE exhibit_id = %s
         ORDER BY correction_seq
        """,
        (exhibit_id,),
    ).fetchall()
    trail: dict[int, list[dict]] = {}
    for row in rows:
        trail.setdefault(row["target_chain_seq"], []).append({
            "correction_id": str(row["correction_id"]),
            "correction_seq": row["correction_seq"],
            "target_chain_seq": row["target_chain_seq"],
            "expected_digest": row["expected_digest"],
            "old_digest": row["old_digest"],
            "new_digest": row["new_digest"],
            "reason": row["reason"],
            "anchor_record_hash": row["anchor_record_hash"],
            "record_hash": row["record_hash"],
            "idempotent_replay": False,
            "created_at": row["created_at"].isoformat(),
        })
    return trail


def _current_digest(original_digest: str, trail: list[dict] | None) -> str:
    """当前有效摘要：该环节最后一笔补正的 new_digest；无补正即原摘要。"""
    return trail[-1]["new_digest"] if trail else original_digest


# ---------------------------------------------------------------- 撤回


def _withdrawal_ref(row: dict) -> dict:
    return {
        "withdrawal_id": str(row["withdrawal_id"]),
        "withdrawal_seq": row["withdrawal_seq"],
        "batch_id": str(row["batch_id"]),
        "reason": row["reason"],
        "record_hash": row["record_hash"],
        "created_at": row["created_at"].isoformat(),
    }


def _load_withdrawals(conn, exhibit_id: str) -> list[dict]:
    """按撤回时序取展品的全部撤回记录（用于标注交接记录/批次与计算有效状态）。"""
    return conn.execute(
        """
        SELECT withdrawal_id, exhibit_id, batch_id, reason, anchor_record_hashes,
               withdrawal_seq, prev_hash, record_hash, request_hash, created_at
          FROM batch_withdrawals
         WHERE exhibit_id = %s
         ORDER BY withdrawal_seq
        """,
        (exhibit_id,),
    ).fetchall()


def _withdrawal_replay(row: dict) -> dict:
    """幂等重放：返回首次受理响应快照，并标记为重放（200）。"""
    result = dict(row["result"])
    result["idempotent_replay"] = True
    return result


def _verify_withdrawals_chain(conn, exhibit: dict, records: list[dict],
                              honest_hashes: dict[int, str]) -> dict:
    """复算撤回链并校验撤回后展品有效状态。

    每环校验：
    - prev_hash 衔接与 record_hash 复算（含撤回业务字段和锚点列表）；
    - 被撤回批次存在、属于该展品且为 ACCEPTED；
    - 锚点列表与该批次现存交接记录的存储 record_hash 及诚实重算哈希
      逐条一致——原记录被删除/改写或锚点被伪造都在首个引用环定位。

    链尾另校验：撤回链头/链长与展品状态一致；
    按撤回链排除被撤回批次后重放的有效保管人/封签/最近时间，
    须与展品当前列一致（否则说明撤回状态没有正确落地）。
    """
    rows = conn.execute(
        "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s ORDER BY withdrawal_seq",
        (exhibit["exhibit_id"],),
    ).fetchall()
    records_by_batch: dict[uuid.UUID, list[dict]] = {}
    for r in records:
        records_by_batch.setdefault(r["batch_id"], []).append(r)

    batch_cache: dict[uuid.UUID, dict] = {}

    def _batch(batch_id: uuid.UUID) -> dict | None:
        if batch_id not in batch_cache:
            batch_cache[batch_id] = conn.execute(
                "SELECT * FROM batches WHERE batch_id = %s",
                (batch_id,),
            ).fetchone()
        return batch_cache[batch_id]

    prev = WITHDRAWALS_GENESIS_HASH
    for row in rows:
        expected = withdrawal_hash(
            prev_hash=prev,
            withdrawal_id=row["withdrawal_id"],
            exhibit_id=row["exhibit_id"],
            withdrawal_seq=row["withdrawal_seq"],
            batch_id=row["batch_id"],
            reason=row["reason"],
            anchor_record_hashes=list(row["anchor_record_hashes"]),
            created_at=row["created_at"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "block": {
                    "chain_length": len(rows),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["withdrawal_seq"],
                    "detail": f"撤回链第 {row['withdrawal_seq']} 环哈希校验失败："
                              "撤回记录被篡改、删除或顺序被调换",
                },
                "effective": None,
            }

        batch = _batch(row["batch_id"])
        batch_records = sorted(
            records_by_batch.get(row["batch_id"], []), key=lambda r: r["chain_seq"]
        )
        if batch is None or batch["exhibit_id"] != exhibit["exhibit_id"]:
            return {
                "block": {
                    "chain_length": len(rows),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["withdrawal_seq"],
                    "detail": f"撤回链第 {row['withdrawal_seq']} 环锚定失效："
                              f"被撤回批次「{row['batch_id']}」不存在或不属于该展品",
                },
                "effective": None,
            }
        if batch["status"] != "ACCEPTED":
            return {
                "block": {
                    "chain_length": len(rows),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["withdrawal_seq"],
                    "detail": f"撤回链第 {row['withdrawal_seq']} 环锚定失效："
                              "被撤回批次状态不是 ACCEPTED",
                },
                "effective": None,
            }
        stored_hashes = [r["record_hash"] for r in batch_records]
        honest_anchors = [honest_hashes[r["chain_seq"]] for r in batch_records]
        if list(row["anchor_record_hashes"]) != stored_hashes or stored_hashes != honest_anchors:
            return {
                "block": {
                    "chain_length": len(rows),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["withdrawal_seq"],
                    "detail": f"撤回链第 {row['withdrawal_seq']} 环锚定失效："
                              "被撤回批次的交接记录不存在、其内容/record_hash 已被改写，"
                              "或锚点列表与批次记录不一致",
                },
                "effective": None,
            }
        prev = row["record_hash"]

    if prev != exhibit["withdrawal_head_hash"] or len(rows) != exhibit["withdrawal_chain_length"]:
        return {
            "block": {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": None,
                "detail": "撤回链尾哈希或链长与展品当前撤回状态不一致",
            },
            "effective": None,
        }

    # 撤回后有效状态一致性：重放有效记录后须与展品当前列一致
    withdrawn = {r["batch_id"] for r in rows}
    effective_rows = [r for r in records if r["batch_id"] not in withdrawn]
    if effective_rows:
        last = effective_rows[-1]
        eff_custodian, eff_seal, eff_time = (
            last["to_person"], last["seal_no"], last["occurred_at"]
        )
    else:
        eff = _effective_state(conn, exhibit, withdrawn)
        eff_custodian, eff_seal, eff_time = (
            eff["custodian"], eff["current_seal"], None
        )
    if (
        exhibit["custodian"] != eff_custodian
        or exhibit["current_seal"] != eff_seal
        or exhibit["last_time"] != eff_time
    ):
        return {
            "block": {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": None,
                "detail": "撤回链完整，但展品当前保管人/封签/最近时间"
                          "与按撤回链重放的有效状态不一致",
            },
            "effective": None,
        }

    return {
        "block": {
            "chain_length": len(rows),
            "head_hash": prev,
            "valid": True,
            "first_invalid_seq": None,
            "detail": "撤回哈希链完整，各环锚点与原交接记录一致，撤回后有效状态正确",
        },
        "effective": {
            "effective_chain_length": len(effective_rows),
        },
    }


def _verify_handover_chain(conn, exhibit: dict, records: list[dict]) -> dict:
    """复算交接链（不改动任何数据）。

    返回审计块、计算链尾，以及按各环**存储的 prev_hash** 独立重算出的
    「诚实」record_hash（全量，不因链路在某环断裂而中断）——供补正链
    锚点校验逐环比对，即使交接链前段断裂，后段记录的内容篡改仍可定位。
    """
    honest: dict[int, str] = {}
    for row in records:
        honest[row["chain_seq"]] = record_hash(
            prev_hash=row["prev_hash"],
            exhibit_id=row["exhibit_id"],
            chain_seq=row["chain_seq"],
            batch_id=row["batch_id"],
            location=row["location"],
            from_person=row["from_person"],
            to_person=row["to_person"],
            seal_no=row["seal_no"],
            seal_change_reason=row["seal_change_reason"],
            occurred_at=row["occurred_at"],
            voucher_digest=row["voucher_digest"],
        )

    prev = GENESIS_HASH
    for row in records:
        expected = honest[row["chain_seq"]]
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "block": {
                    "chain_length": len(records),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["chain_seq"],
                    "detail": f"交接链第 {row['chain_seq']} 环哈希校验失败："
                              "记录被篡改、删除或顺序被调换",
                },
                "head": prev,
                "honest": honest,
            }
        prev = row["record_hash"]

    if prev != exhibit["chain_head_hash"] or len(records) != exhibit["chain_length"]:
        return {
            "block": {
                "chain_length": len(records),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": None,
                "detail": "交接链尾哈希或链长与展品当前状态不一致",
            },
            "head": prev,
            "honest": honest,
        }
    return {
        "block": {
            "chain_length": len(records),
            "head_hash": prev,
            "valid": True,
            "first_invalid_seq": None,
            "detail": "交接哈希链完整，与展品当前状态一致",
        },
        "head": prev,
        "honest": honest,
    }


def _verify_corrections_chain(conn, exhibit: dict, handover_by_seq: dict[int, dict],
                              honest_hashes: dict[int, str]) -> dict:
    """复算补正链：逐环重算哈希并校验锚点仍诚实地指向被补正原记录。

    锚点（anchor_record_hash）必须同时：
    - 等于补正单落库时记录在案的原 record_hash（历史事实）；
    - 等于按当前交接记录业务字段复算出的「诚实」record_hash。
    因此原记录一旦被改写（哪怕有人连带伪造存储哈希列），
    首个引用它的补正环即判锚定失效。
    """
    rows = conn.execute(
        "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
        (exhibit["exhibit_id"],),
    ).fetchall()

    prev = CORRECTIONS_GENESIS_HASH
    for row in rows:
        expected = correction_hash(
            prev_hash=prev,
            correction_id=row["correction_id"],
            exhibit_id=row["exhibit_id"],
            correction_seq=row["correction_seq"],
            target_chain_seq=row["target_chain_seq"],
            expected_digest=row["expected_digest"],
            old_digest=row["old_digest"],
            new_digest=row["new_digest"],
            reason=row["reason"],
            anchor_record_hash=row["anchor_record_hash"],
            created_at=row["created_at"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": row["correction_seq"],
                "detail": f"补正链第 {row['correction_seq']} 环哈希校验失败："
                          "补正单被篡改、删除或顺序被调换",
            }

        stored = handover_by_seq.get(row["target_chain_seq"])
        honest = honest_hashes.get(row["target_chain_seq"])
        if (
            stored is None
            or honest is None
            or stored["record_hash"] != row["anchor_record_hash"]
            or honest != row["anchor_record_hash"]
        ):
            return {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": row["correction_seq"],
                "detail": f"补正链第 {row['correction_seq']} 环锚定失效："
                          f"被补正交接记录（链序号 {row['target_chain_seq']}）"
                          "不存在或其内容/record_hash 已被改写",
            }
        prev = row["record_hash"]

    if prev != exhibit["correction_head_hash"] or len(rows) != exhibit["correction_chain_length"]:
        return {
            "chain_length": len(rows),
            "head_hash": prev,
            "valid": False,
            "first_invalid_seq": None,
            "detail": "补正链尾哈希或链长与展品当前补正状态不一致",
        }
    return {
        "chain_length": len(rows),
        "head_hash": prev,
        "valid": True,
        "first_invalid_seq": None,
        "detail": "补正哈希链完整，各环锚点与原交接记录一致",
    }


# ---------------------------------------------------------------- 健康检查


@app.get("/health", tags=["meta"])
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- 展品


@app.post("/api/v1/exhibits", response_model=ExhibitOut, status_code=201, tags=["exhibits"])
def create_exhibit(payload: ExhibitCreate):
    """登记展品（馆藏地/保管人/初始封签为首站与连续性校验的基准）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal,
                                      registered_custodian, registered_initial_seal)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.exhibit_id, payload.name, payload.home_location, payload.custodian,
                 payload.initial_seal, payload.custodian, payload.initial_seal),
            ).fetchone()
            row = dict(row)
            row["effective_chain_length"] = 0
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail=f"展品「{payload.exhibit_id}」已存在")


@app.get("/api/v1/exhibits/{exhibit_id}", response_model=ExhibitOut, tags=["exhibits"])
def get_exhibit(exhibit_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        withdrawn = _withdrawn_batch_ids(conn, exhibit_id)
        effective = _effective_state(conn, row, withdrawn)
    row = dict(row)
    row["effective_chain_length"] = effective["effective_chain_length"]
    return row


# ---------------------------------------------------------------- 交接批次（核心）


@app.post("/api/v1/handover-batches/rehearse", response_model=RehearsalOut, tags=["handovers"])
def rehearse_handover_batch(payload: BatchRequest):
    """预演一批交接：只读核验，不写入批次报告、交接记录，也不推进展品状态。

    - 接受与正式提交完全相同的展品编号与按序记录，记录格式仍按现有规则校验
      （载荷格式非法时返回 422）。
    - 返回逐条核验结果、首个断点，以及绑定展品当前链状态的 `state_token`。
    - 未知展品返回 404。
    - 正式提交时可在 `Handover-Token` 头携带令牌；链状态未变化才被受理。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s",
            (payload.exhibit_id,),
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")
        # 撤回后预演：以按撤回链计算的有效状态作为规则核验参照（只读，不写任何数据）
        withdrawn = _withdrawn_batch_ids(conn, payload.exhibit_id)
        effective = _effective_state(conn, exhibit, withdrawn)

    result = validate_batch(_exhibit_state(exhibit, effective), payload.records)
    now = datetime.now(timezone.utc)
    # 令牌与审计意义上的链快照仍是原交接链（撤回不删除原记录、原链长度不变）
    token = issue_token(
        TOKEN_SECRET,
        payload.exhibit_id,
        exhibit["chain_length"],
        exhibit["chain_head_hash"],
        issued_at=int(now.timestamp()),
    )
    return {
        "exhibit_id": payload.exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": len(payload.records),
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "rehearsed_at": now.isoformat(),
        "state_token": {
            "token": token,
            "exhibit_id": payload.exhibit_id,
            "chain_length": exhibit["chain_length"],
            "chain_head_hash": exhibit["chain_head_hash"],
            "issued_at": now.isoformat(),
        },
    }


@app.post("/api/v1/handover-batches", response_model=BatchReport, tags=["handovers"])
def submit_handover_batch(
    payload: BatchRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
    handover_token: str | None = Header(
        default=None,
        alias="Handover-Token",
        max_length=2048,
        description="可选：预演（/rehearse）签发的展品状态令牌",
    ),
):
    """提交一批交接记录并核验。

    - 幂等：携带 `Idempotency-Key` 请求头；同键同载荷重试返回同一报告
      （`idempotent_replay=true`），同键不同载荷返回 409。
      携带 `Handover-Token` 时令牌计入载荷身份；同键成功重试即使链已推进，
      也直接返回原报告。
    - 预演令牌（可选）：令牌格式无效 -> 400；跨展品或链状态已变化 -> 409，
      冲突时不生成批次报告，调用方须重新预演。不带令牌的提交行为不变。
    - 事务：核验通过则批次报告与全部交接记录一并写入并推进链上状态；
      核验失败仅落核验报告（供幂等重试与审计），不写入任何交接记录。
    """
    # 1) 令牌先验格式/签名（纯本地校验，400 参数错误），再在展品行锁上比对状态
    parsed_token = None
    if handover_token and handover_token.strip():
        try:
            parsed_token = parse_token(TOKEN_SECRET, handover_token)
        except InvalidTokenError as exc:
            raise HTTPException(status_code=400, detail=f"Handover-Token 无效：{exc}")

    records_for_hash = [r.model_dump(mode="json") for r in payload.records]
    req_hash = request_hash(payload.exhibit_id, records_for_hash, handover_token)

    try:
        with get_conn() as conn:
            # 2) 幂等预检（同键成功重试即使链已推进/令牌已过期，也返回原报告）
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的请求载荷",
                    )
                return _replay_report(existing)

            # 3) 锁定展品行，串行化同一展品的批次，保证链式状态一致
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (payload.exhibit_id,),
            ).fetchone()
            if not exhibit:
                if parsed_token is not None:
                    # 令牌签名有效但展品对不上：按跨展品状态冲突处理
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

            # 4) 令牌归属与链状态比对（行锁内完成，与链推进互斥）
            if parsed_token is not None:
                if parsed_token.exhibit_id != payload.exhibit_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                if not state_matches(
                    parsed_token, exhibit["chain_length"], exhibit["chain_head_hash"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="展品链状态自预演后已变化（哈希链已推进），"
                               "令牌失效，请重新预演后再提交",
                    )

            # 5) 核验：撤回后以按撤回链计算的有效状态为参照（原链长度/链头不变）
            withdrawn = _withdrawn_batch_ids(conn, payload.exhibit_id)
            effective = _effective_state(conn, exhibit, withdrawn)
            result = validate_batch(_exhibit_state(exhibit, effective), payload.records)
            batch_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            report = _build_report(batch_id, payload.exhibit_id, result, len(payload.records), now)

            conn.execute(
                """
                INSERT INTO batches (batch_id, idempotency_key, exhibit_id, request_hash, status, report, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (batch_id, idempotency_key, payload.exhibit_id, req_hash,
                 report["status"], Jsonb(report), now),
            )

            # 6) 通过才写交接记录并推进链上状态；失败则只有批次报告落库
            if result.accepted:
                # 业务参照系（交出人/封签/时间基准）已在第 5 步使用撤回后有效状态；
                # 落链与车次整车交接共用同一写入路径（物理链尾线性追加）
                _write_accepted_batch(conn, exhibit, effective, payload.exhibit_id,
                                      payload.records, batch_id)

            return report
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在的批次返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            return _replay_report(existing)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的请求载荷")


@app.get("/api/v1/handover-batches/{batch_id}", response_model=BatchReport, tags=["handovers"])
def get_handover_batch(batch_id: uuid.UUID):
    """按批次号查询核验报告。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id = %s", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"批次「{batch_id}」不存在")
    return row["report"]


# ---------------------------------------------------------------- 车次整车交接


def _vehicle_replay(row: dict) -> dict:
    """幂等重放：返回整单报告快照（含拒绝结果），并标记为重放。"""
    report = dict(row["report"])
    report["idempotent_replay"] = True
    return report


@app.post("/api/v1/vehicle-handovers", response_model=VehicleHandoverReport, tags=["vehicle"])
def submit_vehicle_handover(
    payload: VehicleHandoverRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """借展车次整车交接：同一车次下 2 至 20 件互不重复展品的一次性整单确认。

    - 逐件沿用单件接口的同一套规则，以撤回后的有效状态核验；
      各件记录仍线性追加在各自原交接链物理链尾。
    - 全数通过：同一事务中为各件写入既有批次（batches）与原交接哈希链
      （handover_records）并推进各展品状态，返回车次号及各批次报告。
    - 任一件核验失败：仅落整单 REJECTED 报告（列出各件逐条结果），
      不写交接批次和记录、不推进任何展品状态。
    - 展品不存在（404）或请求格式不合法（422）：整单报错，不写入任何数据。
    - 幂等：携带 `Idempotency-Key` 请求头；同键同载荷（含拒绝结果）重放原报告
      （`idempotent_replay=true`），同键不同载荷返回 409。
    - 并发：按展品编号排序对全部涉及展品行加锁（确定性顺序避免死锁），
      与单件提交、补正、撤回互斥——重叠车次或单件提交不会导致部分入链。
    """
    items_for_hash = [
        {"exhibit_id": item.exhibit_id,
         "records": [r.model_dump(mode="json") for r in item.records]}
        for item in payload.items
    ]
    req_hash = vehicle_request_hash(payload.vehicle_id, items_for_hash)

    try:
        with get_conn() as conn:
            # 1) 幂等预检：同键同载荷（含拒绝结果）直接重放原报告
            existing = conn.execute(
                "SELECT * FROM vehicle_handovers WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的整车交接请求载荷",
                    )
                return _vehicle_replay(existing)

            # 2) 按展品编号排序锁定全部涉及展品行：串行化整车/单件/补正/撤回，
            #    确定性加锁顺序保证重叠车次之间不会死锁
            exhibit_ids = sorted({item.exhibit_id for item in payload.items})
            rows = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = ANY(%s) ORDER BY exhibit_id FOR UPDATE",
                (exhibit_ids,),
            ).fetchall()
            by_id = {row["exhibit_id"]: row for row in rows}
            missing = [eid for eid in exhibit_ids if eid not in by_id]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品不存在：{'、'.join(missing)}，整单未写入任何数据",
                )

            # 2b) 行锁内再查一次幂等：同键并发请求在此被串行化，
            #     先到者已落单则后到者按重试重放（同载荷）或判冲突（异载荷）
            existing = conn.execute(
                "SELECT * FROM vehicle_handovers WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的整车交接请求载荷",
                    )
                return _vehicle_replay(existing)

            # 3) 逐件核验：以撤回后的有效状态为参照（与单件接口同一套规则引擎）
            per_item = []
            for item in payload.items:
                exhibit = by_id[item.exhibit_id]
                withdrawn = _withdrawn_batch_ids(conn, item.exhibit_id)
                effective = _effective_state(conn, exhibit, withdrawn)
                result = validate_batch(_exhibit_state(exhibit, effective), item.records)
                per_item.append((item, exhibit, effective, result))
            all_ok = all(result.accepted for _, _, _, result in per_item)

            # 4) 组装整单报告：列出各件逐条结果；全数通过时各件才分配批次号
            order_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            item_reports = []
            batch_ids: dict[str, uuid.UUID] = {}
            for item, _, _, result in per_item:
                batch_id = uuid.uuid4() if all_ok else None
                if batch_id is not None:
                    batch_ids[item.exhibit_id] = batch_id
                item_reports.append({
                    "exhibit_id": item.exhibit_id,
                    "batch_id": str(batch_id) if batch_id is not None else None,
                    "status": "ACCEPTED" if result.accepted else "REJECTED",
                    "record_count": len(item.records),
                    "first_break": _serialize_first_break(result),
                    "items": _serialize_items(result),
                })
            report = {
                "vehicle_order_id": str(order_id),
                "vehicle_id": payload.vehicle_id,
                "status": "ACCEPTED" if all_ok else "REJECTED",
                "item_count": len(item_reports),
                "items": item_reports,
                "idempotent_replay": False,
                "created_at": now.isoformat(),
            }

            # 5) 全数通过：为各件写入既有批次与原交接哈希链（同一事务，要么全入链要么全不入）
            if all_ok:
                for item, exhibit, effective, result in per_item:
                    batch_id = batch_ids[item.exhibit_id]
                    batch_report = _build_report(
                        batch_id, item.exhibit_id, result, len(item.records), now,
                    )
                    conn.execute(
                        """
                        INSERT INTO batches (batch_id, idempotency_key, exhibit_id, request_hash, status, report, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (batch_id, f"{idempotency_key}#veh#{item.exhibit_id}", item.exhibit_id,
                         req_hash, "ACCEPTED", Jsonb(batch_report), now),
                    )
                    _write_accepted_batch(conn, exhibit, effective, item.exhibit_id,
                                          item.records, batch_id)

            # 6) 落整单报告（ACCEPTED 与 REJECTED 都落：幂等重放与审计追溯）
            conn.execute(
                """
                INSERT INTO vehicle_handovers
                    (vehicle_order_id, idempotency_key, vehicle_id, request_hash, status, report, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (order_id, idempotency_key, payload.vehicle_id, req_hash,
                 report["status"], Jsonb(report), now),
            )
            return report
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在的整单返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM vehicle_handovers WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            return _vehicle_replay(existing)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的整车交接请求载荷")


@app.get("/api/v1/vehicle-handovers/{vehicle_order_id}", response_model=VehicleHandoverReport, tags=["vehicle"])
def get_vehicle_handover(vehicle_order_id: uuid.UUID):
    """按整单号查询车次整车交接核验报告（含各件逐条结果/批次报告）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_handovers WHERE vehicle_order_id = %s",
            (vehicle_order_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"车次交接单「{vehicle_order_id}」不存在")
    return row["report"]


# ---------------------------------------------------------------- 已受理批次撤回


def _reject_withdrawal(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@app.post(
    "/api/v1/exhibits/{exhibit_id}/batch-withdrawals",
    response_model=WithdrawalOut,
    status_code=201,
    tags=["withdrawals"],
)
def withdraw_handover_batch(
    exhibit_id: str,
    payload: WithdrawalRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """撤回该展品最新且 ACCEPTED、尚未被补正引用的已受理交接批次。

    - 撤回**不删除、不改写**任何原交接记录与其 `record_hash`，只追加一条
      不可变撤回记录（展品独立撤回哈希链，首环 `prev_hash=WITHDRAWALS_GENESIS`，
      锚定被撤回批次全部交接记录的 `record_hash`）。
    - 原交接链历史长度 `chain_length`/链头保持不变；展品当前保管人、封签、
      最近时间与有效交接列表按撤回后状态计算（回退到被撤回批次之前）。
    - 拒绝规则：
      - 展品/批次不存在，或批次不属于该展品 -> 404；
      - 原因为空（纯空白）/ 批次号格式非法 -> 422；
      - 批次状态为 REJECTED -> 409；
      - 存在比该批次更晚的已受理批次（目标不是最新 ACCEPTED 批次）-> 409；
      - 该批次交接记录已被任何补正单引用 -> 409；
      - 该批次已被撤回 -> 409。
    - 幂等（先于业务校验按幂等键重放）：
      - 同键同载荷重试 -> 返回首次撤回结果，`idempotent_replay=true`（HTTP 200）；
      - 同键异载荷 -> 409。
    """
    # 0) 入参格式：批次号必须是 UUID（格式非法 -> 422，不落任何记录）
    try:
        target_batch_id = uuid.UUID(payload.batch_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="batch_id 必须是 UUID 格式的批次号")

    req_hash = withdrawal_request_hash(exhibit_id, target_batch_id, payload.reason)

    try:
        with get_conn() as conn:
            # 1) 幂等预检：同键重试即使业务前提已变化，也先返回首次撤回结果
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的撤回请求载荷",
                    )
                response.status_code = 200
                return _withdrawal_replay(existing)

            # 2) 锁定展品行：串行化撤回与交接批次链推进/补正
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")

            # 2b) 行锁内再查一次幂等：串行化同键并发请求
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的撤回请求载荷",
                    )
                response.status_code = 200
                return _withdrawal_replay(existing)

            # 3) 目标批次：存在、属于该展品、状态为 ACCEPTED
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = %s",
                (target_batch_id,),
            ).fetchone()
            if not batch or batch["exhibit_id"] != exhibit_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品「{exhibit_id}」不存在批次「{target_batch_id}」",
                )
            if batch["status"] != "ACCEPTED":
                raise _reject_withdrawal(
                    f"批次「{target_batch_id}」状态为 {batch['status']}，"
                    "仅允许撤回状态为 ACCEPTED 的批次"
                )

            batch_records = conn.execute(
                """
                SELECT * FROM handover_records
                 WHERE exhibit_id = %s AND batch_id = %s
                 ORDER BY chain_seq
                """,
                (exhibit_id, target_batch_id),
            ).fetchall()

            # 4) 必须是该展品当前最新的「未撤回」已受理批次：
            # 取原链上最后一个不属于已撤回批次的记录所属批次。
            # 撤回按栈式展开：撤回最新批次后，前一批次成为新的最新批次，
            # 可继续撤回直至有效链清空。
            withdrawn_so_far = _withdrawn_batch_ids(conn, exhibit_id)
            latest_row = conn.execute(
                """
                SELECT batch_id FROM handover_records
                 WHERE exhibit_id = %s
                 ORDER BY chain_seq DESC
                """,
                (exhibit_id,),
            ).fetchall()
            latest_batch_id = None
            for lr in latest_row:
                if lr["batch_id"] not in withdrawn_so_far:
                    latest_batch_id = lr["batch_id"]
                    break
            if latest_batch_id != target_batch_id:
                raise _reject_withdrawal(
                    f"批次「{target_batch_id}」不是展品「{exhibit_id}」最新的已受理批次"
                    f"（当前最新未撤回已受理批次为「{latest_batch_id}」），"
                    "存在更晚已受理批次时禁止撤回"
                )

            # 5) 该批次尚未被补正引用：任一环存在补正单即拒绝
            referenced = conn.execute(
                """
                SELECT 1 FROM digest_corrections c
                 WHERE c.exhibit_id = %s
                   AND c.target_chain_seq = ANY(%s)
                 LIMIT 1
                """,
                (exhibit_id, [r["chain_seq"] for r in batch_records]),
            ).fetchone()
            if referenced:
                raise _reject_withdrawal(
                    f"批次「{target_batch_id}」的交接记录已被补正单引用，禁止撤回"
                )

            # 6) 未被撤回（撤回栈语义：已撤回的批次不再是有效最新批次）
            already = conn.execute(
                "SELECT 1 FROM batch_withdrawals WHERE batch_id = %s",
                (target_batch_id,),
            ).fetchone()
            if already:
                raise _reject_withdrawal(f"批次「{target_batch_id}」已被撤回，不可重复撤回")

            # 7) 追加不可变撤回记录：锚定被撤回批次全部交接记录的 record_hash
            anchor_hashes = [r["record_hash"] for r in batch_records]
            withdrawal_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            withdrawal_seq = exhibit["withdrawal_chain_length"] + 1
            prev_hash = exhibit["withdrawal_head_hash"]
            h = withdrawal_hash(
                prev_hash=prev_hash,
                withdrawal_id=withdrawal_id,
                exhibit_id=exhibit_id,
                withdrawal_seq=withdrawal_seq,
                batch_id=target_batch_id,
                reason=payload.reason,
                anchor_record_hashes=anchor_hashes,
                created_at=now,
            )

            # 8) 撤回后有效状态（不触碰 chain_length/chain_head_hash 与任何交接记录）
            withdrawn = _withdrawn_batch_ids(conn, exhibit_id)
            withdrawn.add(target_batch_id)
            effective = _effective_state(conn, exhibit, withdrawn)

            result_body = {
                "withdrawal_id": str(withdrawal_id),
                "exhibit_id": exhibit_id,
                "batch_id": str(target_batch_id),
                "reason": payload.reason,
                "anchor_record_hashes": anchor_hashes,
                "withdrawal_seq": withdrawal_seq,
                "prev_hash": prev_hash,
                "record_hash": h,
                "idempotent_replay": False,
                "created_at": now.isoformat(),
                "chain_length": exhibit["chain_length"],
                "effective_chain_length": effective["effective_chain_length"],
                "custodian": effective["custodian"],
                "current_seal": effective["current_seal"],
                "last_time": effective["last_time"].isoformat() if effective["last_time"] else None,
            }

            conn.execute(
                """
                INSERT INTO batch_withdrawals
                    (withdrawal_id, idempotency_key, exhibit_id, batch_id, reason,
                     anchor_record_hashes, withdrawal_seq, prev_hash, record_hash,
                     request_hash, result, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (withdrawal_id, idempotency_key, exhibit_id, target_batch_id, payload.reason,
                 Jsonb(anchor_hashes), withdrawal_seq, prev_hash, h,
                 req_hash, Jsonb(result_body), now),
            )
            conn.execute(
                """
                UPDATE exhibits
                   SET withdrawal_chain_length = %s, withdrawal_head_hash = %s,
                       custodian = %s, current_seal = %s, last_time = %s
                 WHERE exhibit_id = %s
                """,
                (withdrawal_seq, h, effective["custodian"], effective["current_seal"],
                 effective["last_time"], exhibit_id),
            )

            return result_body
    except UniqueViolation:
        # 并发下同键插入或同批次撤回：回滚后按已存在记录判定（等价后到重试/冲突）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            response.status_code = 200
            return _withdrawal_replay(existing)
        raise HTTPException(status_code=409, detail="撤回冲突：同一幂等键提交了不同载荷，或该批次已被并发撤回")


@app.get(
    "/api/v1/exhibits/{exhibit_id}/batch-withdrawals",
    response_model=list[WithdrawalOut],
    tags=["withdrawals"],
)
def list_batch_withdrawals(
    exhibit_id: str,
    batch_id: uuid.UUID | None = Query(default=None, description="仅返回指定批次的撤回记录"),
):
    """查询展品的撤回链（撤回时序；可按被撤回批次过滤）。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        if batch_id is None:
            rows = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s ORDER BY withdrawal_seq",
                (exhibit_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM batch_withdrawals
                 WHERE exhibit_id = %s AND batch_id = %s
                 ORDER BY withdrawal_seq
                """,
                (exhibit_id, batch_id),
            ).fetchall()
    return [r["result"] for r in rows]



# ---------------------------------------------------------------- 凭证摘要补正


@app.post(
    "/api/v1/exhibits/{exhibit_id}/handovers/{chain_seq}/digest-corrections",
    response_model=CorrectionOut,
    status_code=201,
    tags=["corrections"],
)
def submit_digest_correction(
    exhibit_id: str,
    chain_seq: int,
    payload: CorrectionRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """对已入链交接记录的凭证摘要提交补正单。

    - 原交接记录与其 `record_hash` 永不改写；补正按展品另成一条哈希链，
      每环锚定被补正原记录的 `record_hash`。
    - 拒绝规则：
      - 目标记录不存在（展品不存在 / 链序号越界）-> 404；
      - 补正原因为空、新旧摘要格式/相同性不满足要求 -> 422；
      - `expected_digest` 与当前有效摘要不符 -> 409 冲突；
      - 不同幂等键以同一预期值并发补正，仅一笔生效，其余 409。
    - 幂等（先于一切业务校验按幂等键重放）：
      - 同键同载荷重试 -> 返回原补正单，`idempotent_replay=true`（HTTP 200），
        即使其后已发生新的补正也先按幂等键重放；
      - 同键异载荷 -> 409。
    - 补正不推进交接链、不改变交接链头，既有预演令牌继续有效。
    """
    if chain_seq < 1:
        raise HTTPException(status_code=422, detail="链序号 chain_seq 必须为正整数")
    if not is_valid_voucher_digest(payload.expected_digest):
        raise HTTPException(status_code=422, detail="expected_digest 格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>")
    if not is_valid_voucher_digest(payload.new_digest):
        raise HTTPException(status_code=422, detail="new_digest 格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>")

    req_hash = correction_request_hash(
        exhibit_id, chain_seq, payload.expected_digest, payload.new_digest, payload.reason
    )

    try:
        with get_conn() as conn:
            # 1) 幂等预检：即使后续补正已发生，也先按幂等键重放原补正单
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的补正请求载荷",
                    )
                response.status_code = 200
                return _serialize_correction(existing, replay=True)

            # 2) 锁定展品行：串行化同一展品的补正（也与交接批次链推进互斥）
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")

            # 2b) 行锁内再查一次幂等：同键并发请求在此被串行化，
            #     先到者已落单则后到者按重试重放（同载荷 200 / 异载荷 409）
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的补正请求载荷",
                    )
                response.status_code = 200
                return _serialize_correction(existing, replay=True)

            target = conn.execute(
                """
                SELECT * FROM handover_records
                 WHERE exhibit_id = %s AND chain_seq = %s
                """,
                (exhibit_id, chain_seq),
            ).fetchone()
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品「{exhibit_id}」不存在链序号 {chain_seq} 的交接记录，无可补正目标",
                )

            # 3) 当前有效摘要 = 该环节最后一笔补正的 new_digest，否则为原摘要
            last_corr = conn.execute(
                """
                SELECT * FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq DESC
                 LIMIT 1
                """,
                (exhibit_id, chain_seq),
            ).fetchone()
            current_digest = last_corr["new_digest"] if last_corr else target["voucher_digest"]

            # 4) CAS：预期值不符 -> 冲突（并发同预期补正仅一笔生效，其余在此落败）
            if payload.expected_digest != current_digest:
                raise HTTPException(
                    status_code=409,
                    detail=f"预期当前有效摘要与现状不符：预期「{payload.expected_digest}」，"
                           f"当前有效摘要「{current_digest}」",
                )

            # 5) 新旧相同 -> 拒绝（无实质补正）
            if payload.new_digest == current_digest:
                raise HTTPException(
                    status_code=422,
                    detail="新摘要与当前有效摘要相同，未构成补正",
                )

            # 6) 追加补正链一环：锚定被补正原记录的 record_hash（绝不改写原记录）
            correction_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            correction_seq = exhibit["correction_chain_length"] + 1
            prev_hash = exhibit["correction_head_hash"]
            h = correction_hash(
                prev_hash=prev_hash,
                correction_id=correction_id,
                exhibit_id=exhibit_id,
                correction_seq=correction_seq,
                target_chain_seq=chain_seq,
                expected_digest=payload.expected_digest,
                old_digest=current_digest,
                new_digest=payload.new_digest,
                reason=payload.reason,
                anchor_record_hash=target["record_hash"],
                created_at=now,
            )
            conn.execute(
                """
                INSERT INTO digest_corrections
                    (correction_id, idempotency_key, exhibit_id, target_chain_seq,
                     expected_digest, old_digest, new_digest, reason,
                     anchor_record_hash, correction_seq, prev_hash, record_hash,
                     request_hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (correction_id, idempotency_key, exhibit_id, chain_seq,
                 payload.expected_digest, current_digest, payload.new_digest, payload.reason,
                 target["record_hash"], correction_seq, prev_hash, h, req_hash, now),
            )
            conn.execute(
                """
                UPDATE exhibits
                   SET correction_chain_length = %s, correction_head_hash = %s
                 WHERE exhibit_id = %s
                """,
                (correction_seq, h, exhibit_id),
            )

            row = conn.execute(
                "SELECT * FROM digest_corrections WHERE correction_id = %s",
                (correction_id,),
            ).fetchone()
            return _serialize_correction(row, replay=False)
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在补正单返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            response.status_code = 200
            return _serialize_correction(existing, replay=True)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的补正请求载荷")


@app.get(
    "/api/v1/exhibits/{exhibit_id}/digest-corrections",
    response_model=list[CorrectionOut],
    tags=["corrections"],
)
def list_digest_corrections(
    exhibit_id: str,
    chain_seq: int | None = Query(default=None, ge=1, description="仅返回指定链序号环节的补正轨迹"),
):
    """查询展品的补正链（可按被补正链序号过滤），按时序返回。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        if chain_seq is None:
            rows = conn.execute(
                "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
                (exhibit_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq
                """,
                (exhibit_id, chain_seq),
            ).fetchall()
    return [_serialize_correction(r, replay=False) for r in rows]


@app.get(
    "/api/v1/exhibits/{exhibit_id}/handovers/{chain_seq}",
    response_model=HandoverRecordOut,
    tags=["corrections"],
)
def get_handover_record(exhibit_id: str, chain_seq: int):
    """查询单个交接环节：原摘要、当前有效摘要与补正轨迹一并呈现。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s AND chain_seq = %s",
            (exhibit_id, chain_seq),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在链序号 {chain_seq} 的交接记录")
        trail_map = _load_corrections_by_target(conn, exhibit_id)
        withdrawals = _load_withdrawals(conn, exhibit_id)

    trail = trail_map.get(chain_seq, [])
    wd = next((w for w in withdrawals if w["batch_id"] == row["batch_id"]), None)
    return {
        "chain_seq": row["chain_seq"],
        "batch_id": str(row["batch_id"]),
        "seq_in_batch": row["seq_in_batch"],
        "location": row["location"],
        "from_person": row["from_person"],
        "to_person": row["to_person"],
        "seal_no": row["seal_no"],
        "seal_changed": row["seal_changed"],
        "seal_change_reason": row["seal_change_reason"],
        "occurred_at": row["occurred_at"],
        "voucher_digest": row["voucher_digest"],
        "prev_hash": row["prev_hash"],
        "record_hash": row["record_hash"],
        "created_at": row["created_at"],
        "current_voucher_digest": _current_digest(row["voucher_digest"], trail),
        "correction_trail": trail,
        "withdrawn": wd is not None,
        "withdrawal": _withdrawal_ref(wd) if wd else None,
    }


# ---------------------------------------------------------------- 审计链


@app.get("/api/v1/exhibits/{exhibit_id}/handovers", response_model=ChainOut, tags=["audit"])
def list_handovers(
    exhibit_id: str,
    effective_only: bool = Query(default=False, description="为 true 时仅返回撤回后的有效交接记录；默认返回完整原链历史"),
):
    """查询展品的完整交接链（含每条记录的链式哈希）。

    撤回不删除、不改写任何原交接记录：默认返回完整原链历史
    （`chain_length` 为原链历史长度），被撤回批次的记录带 `withdrawn=true`
    及撤回引用；`effective_chain_length` 为按撤回链排除后的有效长度。
    `?effective_only=true` 时仅返回有效记录（列表长度等于 effective_chain_length）。

    每条记录同时呈现：
    - `voucher_digest`：原交接记录上的凭证摘要（永不改写）；
    - `current_voucher_digest`：当前有效摘要（经补正链覆盖后的最新值）；
    - `correction_trail`：该环节按补正时序排列的完整补正轨迹。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        rows = conn.execute(
            """
            SELECT chain_seq, batch_id, seq_in_batch, location, from_person, to_person,
                   seal_no, seal_changed, seal_change_reason, occurred_at, voucher_digest,
                   prev_hash, record_hash, created_at
              FROM handover_records
             WHERE exhibit_id = %s
             ORDER BY chain_seq
            """,
            (exhibit_id,),
        ).fetchall()
        trail_map = _load_corrections_by_target(conn, exhibit_id)
        withdrawals = _load_withdrawals(conn, exhibit_id)

    wd_by_batch = {w["batch_id"]: w for w in withdrawals}
    records = []
    effective_count = 0
    for r in rows:
        trail = trail_map.get(r["chain_seq"], [])
        wd = wd_by_batch.get(r["batch_id"])
        if wd is None:
            effective_count += 1
        if effective_only and wd is not None:
            continue
        records.append({
            "chain_seq": r["chain_seq"],
            "batch_id": str(r["batch_id"]),
            "seq_in_batch": r["seq_in_batch"],
            "location": r["location"],
            "from_person": r["from_person"],
            "to_person": r["to_person"],
            "seal_no": r["seal_no"],
            "seal_changed": r["seal_changed"],
            "seal_change_reason": r["seal_change_reason"],
            "occurred_at": r["occurred_at"],
            "voucher_digest": r["voucher_digest"],
            "prev_hash": r["prev_hash"],
            "record_hash": r["record_hash"],
            "created_at": r["created_at"],
            "current_voucher_digest": _current_digest(r["voucher_digest"], trail),
            "correction_trail": trail,
            "withdrawn": wd is not None,
            "withdrawal": _withdrawal_ref(wd) if wd else None,
        })
    return {
        "exhibit_id": exhibit_id,
        "chain_length": len(rows),
        "effective_chain_length": effective_count,
        "records": records,
    }


# ---------------------------------------------------------------- 知悉时刻快照


def _parse_as_of(raw: str) -> datetime:
    """解析知悉时刻查询参数：必须是带时区的 ISO-8601 时刻。

    - 缺省/空串/无法解析 -> 422；
    - 能解析但不带时区（naive）-> 422（要求显式时区，如 +08:00 / Z）；
    - 未来时刻由路由在读取当前时间后判 422。
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="查询参数 at 为必填：带时区的知悉时刻，如 2026-10-06T09:00:00+08:00")
    # 兼容末尾的 Z（datetime.fromisoformat 在 3.11 已支持，统一兜底）
    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="at 不是合法的 ISO-8601 日期时间，应如 2026-10-06T09:00:00+08:00 或 2026-10-06T01:00:00Z",
        )
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail="at 必须携带时区，如 2026-10-06T09:00:00+08:00 或 2026-10-06T01:00:00Z")
    return parsed


def _as_of_trail_item(row: dict) -> dict:
    return {
        "correction_id": str(row["correction_id"]),
        "correction_seq": row["correction_seq"],
        "target_chain_seq": row["target_chain_seq"],
        "expected_digest": row["expected_digest"],
        "old_digest": row["old_digest"],
        "new_digest": row["new_digest"],
        "reason": row["reason"],
        "anchor_record_hash": row["anchor_record_hash"],
        "record_hash": row["record_hash"],
        "idempotent_replay": False,
        "created_at": row["created_at"],
    }


@app.get(
    "/api/v1/exhibits/{exhibit_id}/state-at",
    response_model=AsOfStateOut,
    tags=["audit"],
)
def get_exhibit_state_at(exhibit_id: str, at: str = Query(..., description="知悉时刻（必须带时区）")):
    """查询展品在指定「知悉时刻」的交接状态（历史时点快照）。

    三条事件链在**同一数据库快照**（REPEATABLE READ 只读事务）内按服务端
    落库时间截取，边界互不借用：

    - 交接链：以**批次受理时间** `batches.created_at` 为界（`at` 之前受理的
      ACCEPTED 批次记录才出现）；交接业务发生时间 `occurred_at` 不作为边界，
      因此事后补录、业务时间落在过去的历史交接不会提前出现。
    - 补正链：以**补正受理时间** `digest_corrections.created_at` 为界；
      每环的「当时有效摘要」只叠加该时刻之前已受理的补正。
    - 撤回链：以**撤回受理时间** `batch_withdrawals.created_at` 为界；
      该时点已撤回批次的记录排除出有效列表，但**原链长度与链头仍包含它们**。

    返回该时点原交接链长度与链头、有效交接记录（每环带当时有效摘要），
    以及由最后一条有效记录确定的保管人/封签/最近交接时间；有效记录为空时
    回到登记保管人与初始封签（最近交接时间为 null）。

    错误：未知展品、或知悉时刻早于展品登记时间 -> 404；
    缺时区/无法解析/未来时刻 -> 422。
    """
    as_of = _parse_as_of(at)
    now = datetime.now(timezone.utc)
    if as_of > now:
        raise HTTPException(
            status_code=422,
            detail=f"知悉时刻 {as_of.isoformat()} 晚于当前服务器时间 {now.isoformat()}，不接受未来时刻",
        )

    with get_conn() as conn:
        # 单条只读事务固定一次快照：三条链在同一快照内截取，避免并发写入导致跨链不一致。
        # 隔离级别/只读须在事务内第一条查询之前设置（psycopg 在事务开始时下发 BEGIN）。
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s",
                (exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
            registered_at = exhibit["created_at"]
            if as_of < registered_at:
                raise HTTPException(
                    status_code=404,
                    detail=f"知悉时刻 {as_of.isoformat()} 早于展品「{exhibit_id}」登记时间 "
                           f"{registered_at.isoformat()}，该时点展品尚未登记",
                )

            # 1) 交接链：仅取 at 之前「已受理」批次写入的记录（以 batches.created_at 为界）。
            #    REJECTED 批次从不落交接记录；这里再显式按 ACCEPTED + 受理时间上界过滤，
            #    明确「业务发生时间不能替代受理时间」。
            records = conn.execute(
                """
                SELECT r.chain_seq, r.batch_id, r.seq_in_batch, r.location,
                       r.from_person, r.to_person, r.seal_no, r.seal_changed,
                       r.seal_change_reason, r.occurred_at, r.voucher_digest,
                       r.prev_hash, r.record_hash, b.created_at AS accepted_at
                  FROM handover_records r
                  JOIN batches b ON b.batch_id = r.batch_id
                 WHERE r.exhibit_id = %s
                   AND b.status = 'ACCEPTED'
                   AND b.created_at <= %s
                 ORDER BY r.chain_seq
                """,
                (exhibit_id, as_of),
            ).fetchall()

            # 2) 补正链：以补正受理时间为界；只保留锚定目标在该时点可见原链内的环
            corrections = conn.execute(
                """
                SELECT correction_id, target_chain_seq, correction_seq, expected_digest,
                       old_digest, new_digest, reason, anchor_record_hash,
                       prev_hash, record_hash, created_at
                  FROM digest_corrections
                 WHERE exhibit_id = %s AND created_at <= %s
                 ORDER BY correction_seq
                """,
                (exhibit_id, as_of),
            ).fetchall()

            # 3) 撤回链：以撤回受理时间为界
            withdrawals = conn.execute(
                """
                SELECT withdrawal_id, batch_id, withdrawal_seq, reason, record_hash, created_at
                  FROM batch_withdrawals
                 WHERE exhibit_id = %s AND created_at <= %s
                 ORDER BY withdrawal_seq
                """,
                (exhibit_id, as_of),
            ).fetchall()

    # 原链：chain_seq 连续前缀，链头取最后一环（其后被撤回也不影响该时点长度/链头）
    chain_length = len(records)
    chain_head_hash = records[-1]["record_hash"] if records else GENESIS_HASH

    visible_seqs = {r["chain_seq"] for r in records}
    trail_by_seq: dict[int, list[dict]] = {}
    applied_corrections = 0
    for c in corrections:
        if c["target_chain_seq"] not in visible_seqs:
            # 补正受理时目标交接尚未受理（理论上被外键与受理顺序阻止），快照中忽略越界环
            continue
        trail_by_seq.setdefault(c["target_chain_seq"], []).append(_as_of_trail_item(c))
        applied_corrections += 1

    withdrawn_batch_ids = {w["batch_id"] for w in withdrawals}

    effective_records = []
    for r in records:
        if r["batch_id"] in withdrawn_batch_ids:
            continue  # 该时点已撤回批次：排除出有效列表（原链长度/链头仍包含）
        trail = trail_by_seq.get(r["chain_seq"], [])
        current_digest = trail[-1]["new_digest"] if trail else r["voucher_digest"]
        effective_records.append({
            "chain_seq": r["chain_seq"],
            "batch_id": str(r["batch_id"]),
            "seq_in_batch": r["seq_in_batch"],
            "location": r["location"],
            "from_person": r["from_person"],
            "to_person": r["to_person"],
            "seal_no": r["seal_no"],
            "seal_changed": r["seal_changed"],
            "seal_change_reason": r["seal_change_reason"],
            "occurred_at": r["occurred_at"],
            "voucher_digest": r["voucher_digest"],
            "prev_hash": r["prev_hash"],
            "record_hash": r["record_hash"],
            "accepted_at": r["accepted_at"],
            "current_voucher_digest": current_digest,
            "correction_trail": trail,
        })

    # 保管人/封签/最近交接时间：由该时点最后一条有效记录确定；空链回退登记状态
    if effective_records:
        last = effective_records[-1]
        custodian = last["to_person"]
        current_seal = last["seal_no"]
        last_handover_time = last["occurred_at"]
    else:
        custodian = exhibit.get("registered_custodian") or exhibit["custodian"]
        current_seal = exhibit.get("registered_initial_seal") or exhibit["current_seal"]
        last_handover_time = None

    return {
        "exhibit_id": exhibit_id,
        "as_of": as_of,
        "registered_at": registered_at,
        "chain_length": chain_length,
        "chain_head_hash": chain_head_hash,
        "effective_chain_length": len(effective_records),
        "records": effective_records,
        "custodian": custodian,
        "current_seal": current_seal,
        "last_handover_time": last_handover_time,
        "correction_chain_length": applied_corrections,
        "withdrawal_chain_length": len(withdrawals),
    }


@app.get("/api/v1/exhibits/{exhibit_id}/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
def verify_audit_chain(exhibit_id: str):
    """重放两条哈希链并校验完整性：

    - 交接链：逐条复算 `record_hash = sha256(prev_hash ‖ 规范化业务字段)`，
      比对 prev_hash 衔接与展品链头（补正永不改写原记录与链头）；
    - 补正链：按展品独立成链复算，并校验每环锚点仍等于被补正原记录的
      `record_hash`。

    任一链被篡改、删除、乱序，都会在对应链块给出首个失效环；
    顶层 `valid/first_invalid_seq/detail` 保持原语义（以交接链为准），
    补正链结果见 `corrections_chain`，交接链块见 `handover_chain`。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        records = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s ORDER BY chain_seq",
            (exhibit_id,),
        ).fetchall()
        handover_by_seq = {r["chain_seq"]: r for r in records}
        handover_result = _verify_handover_chain(conn, exhibit, records)
        corrections_block = _verify_corrections_chain(
            conn, exhibit, handover_by_seq, handover_result["honest"]
        )
        withdrawals_result = _verify_withdrawals_chain(
            conn, exhibit, records, handover_result["honest"]
        )

    handover_block = handover_result["block"]
    withdrawals_block = withdrawals_result["block"]
    all_valid = handover_block["valid"] and corrections_block["valid"] and withdrawals_block["valid"]
    if all_valid:
        detail = "交接链、补正链与撤回链均完整；补正/撤回锚点与原交接记录一致，撤回后有效状态正确"
    elif not handover_block["valid"]:
        detail = handover_block["detail"]
    elif not corrections_block["valid"]:
        detail = corrections_block["detail"]
    else:
        detail = withdrawals_block["detail"]

    effective_info = withdrawals_result.get("effective") or {}
    return {
        "exhibit_id": exhibit_id,
        "chain_length": handover_block["chain_length"],
        "valid": all_valid,
        "first_invalid_seq": handover_block["first_invalid_seq"],
        "detail": detail,
        "correction_chain_length": corrections_block["chain_length"],
        "withdrawal_chain_length": withdrawals_block["chain_length"],
        "effective_chain_length": effective_info.get("effective_chain_length"),
        "handover_chain": handover_block,
        "corrections_chain": corrections_block,
        "withdrawals_chain": withdrawals_block,
    }


@app.get("/api/v1/exhibits/{exhibit_id}/batches", response_model=list[BatchSummary], tags=["audit"])
def list_batches(
    exhibit_id: str,
    status: str | None = Query(default=None, pattern="^(ACCEPTED|REJECTED)$"),
):
    """查询展品的批次历史（含被驳回与已撤回批次，供审计追溯）。"""
    sql = """
        SELECT batch_id, status, report, created_at
          FROM batches
         WHERE exhibit_id = %s
    """
    params: list = [exhibit_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at, batch_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        wd_rows = conn.execute(
            "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s",
            (exhibit_id,),
        ).fetchall()
    wd_by_batch = {w["batch_id"]: w for w in wd_rows}
    return [
        {
            "batch_id": str(r["batch_id"]),
            "status": r["status"],
            "record_count": r["report"]["record_count"],
            "first_break": r["report"]["first_break"],
            "created_at": r["created_at"],
            "withdrawn": r["batch_id"] in wd_by_batch,
            "withdrawal": _withdrawal_ref(wd_by_batch[r["batch_id"]])
            if r["batch_id"] in wd_by_batch else None,
        }
        for r in rows
    ]
