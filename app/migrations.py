"""启动时增量迁移（幂等）：让旧数据卷平滑升级到最新库结构。

全新库由 db/init.sql 经 Postgres 容器 entrypoint 初始化；
旧卷不会重跑 init.sql，故启动时在此补齐新增结构。
所有语句均为 IF NOT EXISTS，重复执行无副作用，与 init.sql 保持等价。

历史版本：
- v1.1：补正链（digest_corrections 表与 exhibits.correction_* 列）
- v1.3：批次撤回链（batch_withdrawals 表、exhibits.withdrawal_* 列，
        以及撤回后回退状态所需的 registered_custodian/registered_initial_seal 列）
- v1.4：车次整车交接单（vehicle_handovers 表）
"""
from __future__ import annotations

from .db import get_conn

# 补正链创世前驱哈希（与交接链 GENESIS 区分，避免补正首环误接到交接链尾）
CORRECTIONS_GENESIS_HASH = "CORRECTIONS_GENESIS"
# 撤回链创世前驱哈希（撤回链同样独立成链）
WITHDRAWALS_GENESIS_HASH = "WITHDRAWALS_GENESIS"

_STATEMENTS = (
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS correction_chain_length BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS correction_head_hash TEXT NOT NULL DEFAULT 'CORRECTIONS_GENESIS'
    """,
    """
    CREATE TABLE IF NOT EXISTS digest_corrections (
        correction_id      UUID        PRIMARY KEY,
        idempotency_key    TEXT        NOT NULL,
        exhibit_id         TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
        target_chain_seq   BIGINT      NOT NULL,
        expected_digest    TEXT        NOT NULL,
        old_digest         TEXT        NOT NULL,
        new_digest         TEXT        NOT NULL,
        reason             TEXT        NOT NULL,
        anchor_record_hash TEXT        NOT NULL,
        correction_seq     BIGINT      NOT NULL,
        prev_hash          TEXT        NOT NULL,
        record_hash        TEXT        NOT NULL,
        request_hash       TEXT        NOT NULL,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exhibit_id, correction_seq),
        UNIQUE (idempotency_key),
        UNIQUE (record_hash),
        FOREIGN KEY (exhibit_id, target_chain_seq)
            REFERENCES handover_records (exhibit_id, chain_seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_corr_exhibit_seq ON digest_corrections (exhibit_id, correction_seq)",
    "CREATE INDEX IF NOT EXISTS idx_corr_target ON digest_corrections (exhibit_id, target_chain_seq, correction_seq)",
    # ---------------------------------------------------------------- 撤回链
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS withdrawal_chain_length BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS withdrawal_head_hash TEXT NOT NULL DEFAULT 'WITHDRAWALS_GENESIS'
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS registered_custodian TEXT
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS registered_initial_seal TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_withdrawals (
        withdrawal_id        UUID        PRIMARY KEY,
        idempotency_key      TEXT        NOT NULL,
        exhibit_id           TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
        batch_id             UUID        NOT NULL REFERENCES batches (batch_id),
        reason               TEXT        NOT NULL,
        anchor_record_hashes JSONB       NOT NULL,
        withdrawal_seq       BIGINT      NOT NULL,
        prev_hash            TEXT        NOT NULL,
        record_hash          TEXT        NOT NULL,
        request_hash         TEXT        NOT NULL,
        result               JSONB       NOT NULL,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exhibit_id, withdrawal_seq),
        UNIQUE (idempotency_key),
        UNIQUE (record_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wd_exhibit_seq ON batch_withdrawals (exhibit_id, withdrawal_seq)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_wd_batch ON batch_withdrawals (batch_id)",
    # 旧卷登记时未保存登记保管人/初始封签：用首环（chain_seq=1）交出人/参照封签兜底回填。
    # 首站发生过换签的历史数据，初始封签无法从现有数据还原，留 NULL，
    # 撤回首站批次时以首环封签兜底（见 main._rollback_state）。
    """
    UPDATE exhibits e
       SET registered_custodian = r.from_person
      FROM handover_records r
     WHERE e.registered_custodian IS NULL
       AND r.exhibit_id = e.exhibit_id
       AND r.chain_seq = 1
    """,
    """
    UPDATE exhibits e
       SET registered_initial_seal = e.current_seal
     WHERE e.registered_initial_seal IS NULL
       AND e.withdrawal_chain_length = 0
       AND NOT EXISTS (
           SELECT 1 FROM batch_withdrawals w WHERE w.exhibit_id = e.exhibit_id
       )
    """,
    # ---------------------------------------------------------------- 车次整车交接单
    """
    CREATE TABLE IF NOT EXISTS vehicle_handovers (
        vehicle_order_id UUID        PRIMARY KEY,
        idempotency_key  TEXT        NOT NULL,
        vehicle_id       TEXT        NOT NULL,
        request_hash     TEXT        NOT NULL,
        status           TEXT        NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
        report           JSONB       NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vehicle_handovers_vehicle ON vehicle_handovers (vehicle_id, created_at)",
)


def ensure_schema() -> None:
    """幂等地补齐新功能所需库结构（旧卷升级；新库已是最新结构）。"""
    with get_conn() as conn:
        for stmt in _STATEMENTS:
            conn.execute(stmt)
