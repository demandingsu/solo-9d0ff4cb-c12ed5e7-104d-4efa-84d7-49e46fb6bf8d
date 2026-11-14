"""SQLite 持久层。

表说明：
  ingest_state     每个邮箱文件夹的采集进度（UIDVALIDITY / 最后处理 UID）
  mail_delivery    已处理邮件投递（Message-ID + 附件摘要，去重/冲突识别）
  test_result      检测记录（attachment_id/record_seq 为溯源信息，NULL 表示
                   溯源功能启用前采集的旧记录，原件未留存）
  attachment_blob  已发布原件的内容寻址注册表（SHA-256 -> 大小），
                   对应 db.get_files_dir() 下的独立文件，引用计数语义：
                   只要本表存在该 SHA-256，原件文件就必须保留
  mail_attachment  邮件-附件关联（同名附件用 attach_index 区分，从 1 起）
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


def get_files_dir(db_path: str | None = None) -> str:
    """原件库根目录（独立文件，持久卷）。

    优先取 COLLECTOR_FILES；未设置时默认放在数据库文件旁的 originals/，
    与数据库同属一个持久卷。
    """
    env = os.environ.get("COLLECTOR_FILES")
    if env:
        return env
    base = db_path or get_db_path()
    return os.path.join(os.path.dirname(os.path.abspath(base)), "originals")


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
            attachment_id     INTEGER,
            record_seq        INTEGER,
            sample_no         TEXT NOT NULL,
            test_item         TEXT NOT NULL,
            result            TEXT NOT NULL,
            unit              TEXT NOT NULL,
            ingested_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attachment_blob (
            sha256      TEXT PRIMARY KEY,
            size_bytes  INTEGER NOT NULL,
            stored_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mail_attachment (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox_key   TEXT NOT NULL,
            message_id    TEXT NOT NULL,
            attach_index  INTEGER NOT NULL,
            filename      TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL,
            record_count  INTEGER NOT NULL,
            ingested_at   TEXT NOT NULL,
            UNIQUE (mailbox_key, message_id, attach_index)
        );

        CREATE INDEX IF NOT EXISTS idx_test_result_sample
            ON test_result (sample_no);
        CREATE INDEX IF NOT EXISTS idx_delivery_mailbox
            ON mail_delivery (mailbox_key);
        CREATE INDEX IF NOT EXISTS idx_attachment_message
            ON mail_attachment (mailbox_key, message_id);
        """
    )
    _migrate_test_result(conn)
    conn.commit()


def _migrate_test_result(conn: sqlite3.Connection) -> None:
    """为溯源功能启用前创建的 test_result 表补充溯源列（旧记录为 NULL）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(test_result)")}
    if "attachment_id" not in cols:
        conn.execute("ALTER TABLE test_result ADD COLUMN attachment_id INTEGER")
    if "record_seq" not in cols:
        conn.execute("ALTER TABLE test_result ADD COLUMN record_seq INTEGER")


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
    attachment_id: int,
    records: list[tuple[int, str, str, str, str]],
) -> None:
    """records: (record_seq, sample_no, test_item, result, unit)。"""
    now = _utcnow()
    conn.executemany(
        """
        INSERT INTO test_result
            (mailbox_key, message_id, uid, attachment_id, record_seq,
             sample_no, test_item, result, unit, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                mailbox_key,
                message_id,
                uid,
                attachment_id,
                record_seq,
                sample_no,
                test_item,
                result,
                unit,
                now,
            )
            for record_seq, sample_no, test_item, result, unit in records
        ],
    )


def insert_blob(
    conn: sqlite3.Connection, sha256: str, size_bytes: int
) -> None:
    """登记已发布原件（内容寻址，同一 SHA-256 只存一份）。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO attachment_blob (sha256, size_bytes, stored_at)
        VALUES (?, ?, ?)
        """,
        (sha256, size_bytes, _utcnow()),
    )


def insert_attachment(
    conn: sqlite3.Connection,
    mailbox_key: str,
    message_id: str,
    attach_index: int,
    filename: str,
    sha256: str,
    size_bytes: int,
    record_count: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO mail_attachment
            (mailbox_key, message_id, attach_index, filename, sha256,
             size_bytes, record_count, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mailbox_key,
            message_id,
            attach_index,
            filename,
            sha256,
            size_bytes,
            record_count,
            _utcnow(),
        ),
    )
    return int(cur.lastrowid)


def list_blob_hashes(conn: sqlite3.Connection) -> set[str]:
    """已被提交投递引用的原件 SHA-256 集合（sweep 的 keep 集合）。"""
    return {row[0] for row in conn.execute("SELECT sha256 FROM attachment_blob")}


def get_attachment(
    conn: sqlite3.Connection, attachment_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mail_attachment WHERE id = ?", (attachment_id,)
    ).fetchone()


def list_attachments(
    conn: sqlite3.Connection,
    message_id: str | None = None,
    mailbox_key: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM mail_attachment"
    clauses: list[str] = []
    params: list[str] = []
    if message_id is not None:
        clauses.append("message_id = ?")
        params.append(message_id)
    if mailbox_key is not None:
        clauses.append("mailbox_key = ?")
        params.append(mailbox_key)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY mailbox_key, message_id, attach_index"
    return conn.execute(sql, params).fetchall()


def query_result_trace(
    conn: sqlite3.Connection, result_id: int
) -> sqlite3.Row | None:
    """按结果 ID 取溯源信息：邮件标识 + 附件（同名附件以 attach_index 区分）。"""
    return conn.execute(
        """
        SELECT tr.id, tr.mailbox_key, tr.message_id, tr.uid,
               tr.attachment_id, tr.record_seq,
               tr.sample_no, tr.test_item, tr.result, tr.unit, tr.ingested_at,
               ma.attach_index, ma.filename, ma.sha256, ma.size_bytes,
               ma.record_count AS attachment_record_count
        FROM test_result tr
        LEFT JOIN mail_attachment ma ON ma.id = tr.attachment_id
        WHERE tr.id = ?
        """,
        (result_id,),
    ).fetchone()


def query_results_by_sample(
    conn: sqlite3.Connection, sample_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT tr.id, tr.sample_no, tr.test_item, tr.result, tr.unit,
               tr.message_id, tr.uid, tr.mailbox_key, tr.ingested_at,
               tr.attachment_id, tr.record_seq,
               ma.filename     AS attachment_filename,
               ma.attach_index AS attachment_index
        FROM test_result tr
        LEFT JOIN mail_attachment ma ON ma.id = tr.attachment_id
        WHERE tr.sample_no = ?
        ORDER BY tr.id
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
    """清空全部业务数据（演示/测试用）。原件文件由调用方按引用清理。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM test_result")
        conn.execute("DELETE FROM mail_attachment")
        conn.execute("DELETE FROM attachment_blob")
        conn.execute("DELETE FROM mail_delivery")
        conn.execute("DELETE FROM ingest_state")
        conn.commit()


def dump_state_json(db_path: str) -> str:
    with connect(db_path) as conn:
        payload = {
            "states": [dict(r) for r in conn.execute("SELECT * FROM ingest_state")],
            "deliveries": [dict(r) for r in list_deliveries(conn)],
            "attachments": [dict(r) for r in conn.execute("SELECT * FROM mail_attachment")],
            "blobs": [dict(r) for r in conn.execute("SELECT * FROM attachment_blob")],
            "results": [
                dict(r)
                for r in conn.execute(
                    "SELECT id, mailbox_key, message_id, uid, attachment_id,"
                    " record_seq, sample_no, test_item, result, unit, ingested_at"
                    " FROM test_result"
                )
            ],
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)
