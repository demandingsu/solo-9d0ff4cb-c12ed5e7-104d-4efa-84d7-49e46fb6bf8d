"""SQLite 持久层。

表说明：
  ingest_state     每个邮箱文件夹的采集进度（UIDVALIDITY / 最后处理 UID）
  mail_delivery    已处理邮件投递（Message-ID + 附件摘要，去重/冲突识别）
  test_result      检测记录
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DEFAULT_DB_PATH = "/workspace/imap-csv-collector/data/collector.db"

_ingest_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> str:
    return os.environ.get("COLLECTOR_DB", DEFAULT_DB_PATH)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingest_state (
            mailbox_key   TEXT PRIMARY KEY,
            uidvalidity   INTEGER NOT NULL,
            last_uid      INTEGER NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mail_delivery (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox_key       TEXT NOT NULL,
            message_id        TEXT NOT NULL,
            attachment_digest TEXT NOT NULL,
            first_uid         INTEGER NOT NULL,
            uidvalidity       INTEGER NOT NULL,
            ingested_at       TEXT NOT NULL,
            UNIQUE (mailbox_key, message_id)
        );

        CREATE TABLE IF NOT EXISTS test_result (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox_key       TEXT NOT NULL,
            message_id        TEXT NOT NULL,
            uid               INTEGER NOT NULL,
            sample_no         TEXT NOT NULL,
            test_item         TEXT NOT NULL,
            result            TEXT NOT NULL,
            unit              TEXT NOT NULL,
            ingested_at       TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_test_result_sample
            ON test_result (sample_no);
        CREATE INDEX IF NOT EXISTS idx_delivery_mailbox
            ON mail_delivery (mailbox_key);
        """
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_state(conn: sqlite3.Connection, mailbox_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ingest_state WHERE mailbox_key = ?", (mailbox_key,)
    ).fetchone()


def upsert_state(
    conn: sqlite3.Connection,
    mailbox_key: str,
    uidvalidity: int,
    last_uid: int,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_state (mailbox_key, uidvalidity, last_uid, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(mailbox_key) DO UPDATE SET
            uidvalidity = excluded.uidvalidity,
            last_uid    = excluded.last_uid,
            updated_at  = excluded.updated_at
        """,
        (mailbox_key, uidvalidity, last_uid, _utcnow()),
    )


def find_delivery(
    conn: sqlite3.Connection, mailbox_key: str, message_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mail_delivery WHERE mailbox_key = ? AND message_id = ?",
        (mailbox_key, message_id),
    ).fetchone()


def insert_delivery(
    conn: sqlite3.Connection,
    mailbox_key: str,
    message_id: str,
    attachment_digest: str,
    uid: int,
    uidvalidity: int,
) -> None:
    conn.execute(
        """
        INSERT INTO mail_delivery
            (mailbox_key, message_id, attachment_digest, first_uid,
             uidvalidity, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            mailbox_key,
            message_id,
            attachment_digest,
            uid,
            uidvalidity,
            _utcnow(),
        ),
    )


def insert_results(
    conn: sqlite3.Connection,
    mailbox_key: str,
    message_id: str,
    uid: int,
    records: list[tuple[str, str, str, str]],
) -> None:
    now = _utcnow()
    conn.executemany(
        """
        INSERT INTO test_result
            (mailbox_key, message_id, uid, sample_no, test_item,
             result, unit, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (mailbox_key, message_id, uid, sample_no, test_item, result, unit, now)
            for sample_no, test_item, result, unit in records
        ],
    )


def query_results_by_sample(
    conn: sqlite3.Connection, sample_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, sample_no, test_item, result, unit, message_id, uid,
               mailbox_key, ingested_at
        FROM test_result
        WHERE sample_no = ?
        ORDER BY id
        """,
        (sample_no,),
    ).fetchall()


def list_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sample_no,
               COUNT(*)        AS result_count,
               MIN(ingested_at) AS first_ingested_at,
               MAX(ingested_at) AS last_ingested_at
        FROM test_result
        GROUP BY sample_no
        ORDER BY sample_no
        """,
    ).fetchall()


def list_deliveries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT mailbox_key, message_id, attachment_digest, first_uid,
               uidvalidity, ingested_at
        FROM mail_delivery
        ORDER BY id
        """,
    ).fetchall()


def reset_all(db_path: str) -> None:
    """清空全部业务数据（演示/测试用）。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM test_result")
        conn.execute("DELETE FROM mail_delivery")
        conn.execute("DELETE FROM ingest_state")
        conn.commit()


def dump_state_json(db_path: str) -> str:
    with connect(db_path) as conn:
        payload = {
            "states": [dict(r) for r in conn.execute("SELECT * FROM ingest_state")],
            "deliveries": [dict(r) for r in list_deliveries(conn)],
            "results": [
                dict(r)
                for r in conn.execute(
                    "SELECT id, mailbox_key, message_id, uid, sample_no,"
                    " test_item, result, unit, ingested_at FROM test_result"
                )
            ],
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)
