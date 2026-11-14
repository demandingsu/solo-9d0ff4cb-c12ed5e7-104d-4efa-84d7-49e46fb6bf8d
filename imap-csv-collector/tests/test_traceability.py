"""溯源与原件留存语义测试。

覆盖：
  * 按结果 ID 溯源：邮件标识、附件名、非空数据记录序号（从 1 起）、原件下载
  * 同名附件可区分（attach_index / sha256）
  * 相同 SHA-256 的原件只存一份
  * 邮件从邮箱清理后原件仍可下载
  * 旧记录（溯源功能启用前）标明原件未留存，迁移保留旧数据
  * 进程在落盘与提交之间退出：重启清理未引用文件、保全已发布原件、可续采
  * 提交失败：停在该邮件、无部分可查询结果、共享原件不被误删、可重试续采
"""
import hashlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.blobstore import BlobStore
from app.collector import collect, recover_storage
from app.main import app
from imap_server import mailbox
from imap_server import seed as seed_mod
from tests.conftest import PASSWORD, USER

B1_ROWS = [
    ("S-1001", "铅Pb", "0.021", "mg/kg"),
    ("S-1001", "镉Cd", "0.008", "mg/kg"),
    ("S-1002", "铅Pb", "0.030", "mg/kg"),
]


def _payload(host, port):
    return {
        "host": host,
        "port": port,
        "folder": "INBOX",
        "username": USER,
        "password": PASSWORD,
        "use_ssl": False,
    }


def _run(host, port):
    return collect(
        host=host,
        port=port,
        folder="INBOX",
        username=USER,
        password=PASSWORD,
        use_ssl=False,
    )


def _store() -> BlobStore:
    return BlobStore(db.get_files_dir())


def test_trace_and_download_roundtrip(imap_server, db_path):
    host, port, mail_root = imap_server
    client = TestClient(app)

    body = client.post("/collect", json=_payload(host, port)).json()
    assert body["status"] == "stopped"  # 坏邮件 UID3 停采，前两封已入库
    first = body["ingested"][0]
    assert first["originals_retained"] is True
    assert first["attachments"][0]["filename"] == "batch_b1.csv"
    assert first["attachments"][0]["sha256"] == hashlib.sha256(
        seed_mod.csv_text(B1_ROWS)
    ).hexdigest()

    # 样品查询结果带溯源字段（结果 ID、记录序号、是否留存原件）
    results = client.get("/results/S-1001").json()["results"]
    assert [r["record_seq"] for r in results] == [1, 2]
    assert all(r["original_retained"] for r in results)
    assert all(r["attachment_filename"] == "batch_b1.csv" for r in results)

    # 按结果 ID 溯源：邮件标识 + 附件名 + 非空数据记录序号（从 1 起）
    rid = results[0]["id"]
    trace = client.get(f"/trace/{rid}").json()
    assert trace["result_id"] == rid
    assert trace["mail"]["message_id"] == "inst-0001@lab-instrument"
    assert trace["mail"]["uid"] == 1
    assert trace["mail"]["mailbox_key"].endswith("|INBOX")
    assert trace["original_retained"] is True
    original = trace["original"]
    assert original["filename"] == "batch_b1.csv"
    assert original["attach_index"] == 1
    assert original["record_seq"] == 1
    assert original["file_present"] is True
    expected = seed_mod.csv_text(B1_ROWS)
    assert original["sha256"] == hashlib.sha256(expected).hexdigest()

    # 即使源邮件已从邮箱清理，原件仍可下载且字节一致
    os.remove(f"{mailbox.folder_dir(mail_root, 'INBOX')}/1.eml")
    resp = client.get(original["download_url"])
    assert resp.status_code == 200
    assert resp.content == expected
    assert "batch_b1.csv" in resp.headers["content-disposition"]
    assert resp.headers["x-content-sha256"] == original["sha256"]

    # 不存在的溯源/附件 -> 404
    assert client.get("/trace/99999").status_code == 404
    assert client.get("/attachments/99999").status_code == 404
    assert client.get("/attachments/99999/download").status_code == 404


def test_same_name_attachments_distinguished(imap_server, db_path):
    host, port, _ = imap_server
    client = TestClient(app)
    client.post("/collect", json=_payload(host, port))

    # S-1003 / S-1004 来自同一封邮件的两个同名附件 batch_b2.csv
    rid3 = client.get("/results/S-1003").json()["results"][0]["id"]
    rid4 = client.get("/results/S-1004").json()["results"][0]["id"]
    trace3 = client.get(f"/trace/{rid3}").json()
    trace4 = client.get(f"/trace/{rid4}").json()

    assert trace3["mail"]["message_id"] == trace4["mail"]["message_id"]
    assert trace3["original"]["filename"] == "batch_b2.csv"
    assert trace4["original"]["filename"] == "batch_b2.csv"
    # 同名附件以邮件内附件序号与摘要区分
    assert trace3["original"]["attach_index"] == 1
    assert trace4["original"]["attach_index"] == 2
    assert trace3["original"]["attachment_id"] != trace4["original"]["attachment_id"]
    assert trace3["original"]["sha256"] != trace4["original"]["sha256"]
    assert trace3["original"]["record_seq"] == 1
    assert trace4["original"]["record_seq"] == 1

    dl3 = client.get(trace3["original"]["download_url"])
    dl4 = client.get(trace4["original"]["download_url"])
    assert b"S-1003" in dl3.content and b"S-1004" not in dl3.content
    assert b"S-1004" in dl4.content and b"S-1003" not in dl4.content

    # 列表端点同样按 attach_index 区分同名附件
    listing = client.get(
        "/attachments", params={"message_id": "inst-0002@lab-instrument"}
    ).json()
    assert listing["count"] == 2
    assert [a["attach_index"] for a in listing["attachments"]] == [1, 2]
    assert {a["filename"] for a in listing["attachments"]} == {"batch_b2.csv"}


def test_legacy_records_marked_not_retained(tmp_path, monkeypatch):
    """溯源功能启用前的旧记录：迁移保留数据，溯源标明原件未留存。"""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(
        """
        CREATE TABLE ingest_state (
            mailbox_key TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL,
            last_uid INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE mail_delivery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox_key TEXT NOT NULL, message_id TEXT NOT NULL,
            attachment_digest TEXT NOT NULL, first_uid INTEGER NOT NULL,
            uidvalidity INTEGER NOT NULL, ingested_at TEXT NOT NULL,
            UNIQUE (mailbox_key, message_id)
        );
        CREATE TABLE test_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mailbox_key TEXT NOT NULL, message_id TEXT NOT NULL,
            uid INTEGER NOT NULL, sample_no TEXT NOT NULL,
            test_item TEXT NOT NULL, result TEXT NOT NULL,
            unit TEXT NOT NULL, ingested_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO test_result"
        " (mailbox_key, message_id, uid, sample_no, test_item, result, unit,"
        "  ingested_at)"
        " VALUES ('imap|10143|INBOX', 'legacy-0001@lab', 7,"
        "         'S-OLD', '铅Pb', '0.05', 'mg/kg', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("COLLECTOR_DB", str(legacy))
    monkeypatch.setenv("COLLECTOR_FILES", str(tmp_path / "originals"))

    # 启动（lifespan 执行恢复，含迁移）后旧记录可溯源但标明未留存
    with TestClient(app) as client:
        results = client.get("/results/S-OLD").json()["results"]
        assert results[0]["original_retained"] is False
        assert results[0]["record_seq"] is None

        trace = client.get(f"/trace/{results[0]['id']}").json()
        assert trace["original_retained"] is False
        assert trace["original"] is None
        assert "未留存" in trace["note"]
        assert trace["mail"]["message_id"] == "legacy-0001@lab"

    # 迁移后旧列数据未丢失，且新列已补齐
    with db.connect(str(legacy)) as conn:
        row = conn.execute("SELECT * FROM test_result WHERE id = 1").fetchone()
        assert row["result"] == "0.05"
        assert row["attachment_id"] is None
        assert row["record_seq"] is None


def test_restart_recovery_sweeps_orphans_and_preserves_published(
    imap_server, db_path
):
    host, port, _ = imap_server
    report = _run(host, port)
    assert len(report.ingested) == 2  # 停在坏邮件 UID3

    store = _store()
    published = set(store.list_stored())
    assert len(published) == 3  # batch_b1.csv + 两个同名 batch_b2.csv（内容不同）

    # 模拟进程在「落盘」与「提交」之间退出：留下未引用原件与临时文件
    orphan_sha = store.put(b"uncommitted,original\r\n")
    os.makedirs(store.tmp_dir, exist_ok=True)
    junk = os.path.join(store.tmp_dir, "partial.123.part")
    with open(junk, "wb") as fh:
        fh.write(b"half-written")
    assert orphan_sha in store.list_stored()

    # 重启恢复：未引用文件被清理，已发布原件全部保留
    recovery = recover_storage()
    assert orphan_sha not in store.list_stored()
    assert not os.path.exists(junk)
    assert set(store.list_stored()) == published
    assert any(orphan_sha in path for path in recovery["removed_unreferenced"])

    # 采集可从断点续采：仍停在坏邮件 UID3，无重复入库
    again = _run(host, port)
    assert again.stopped is not None
    assert again.stopped["uid"] == 3
    assert again.ingested == []


def test_commit_failure_stops_clean_and_keeps_shared_blob(
    imap_server, db_path, monkeypatch
):
    host, port, mail_root = imap_server
    first = _run(host, port)
    assert len(first.ingested) == 2

    # 移除坏邮件，追加一封新邮件：附件一与 batch_b1.csv 内容相同（共享原件），
    # 附件二是全新内容
    os.remove(f"{mailbox.folder_dir(mail_root, 'INBOX')}/3.eml")
    shared = seed_mod.csv_text(B1_ROWS)
    fresh = seed_mod.csv_text([("S-2001", "锌Zn", "1.234", "mg/kg")])
    shared_sha = hashlib.sha256(shared).hexdigest()
    fresh_sha = hashlib.sha256(fresh).hexdigest()
    new_mail = mailbox.build_message(
        message_id="inst-0004@lab-instrument",
        subject="仪器结果 批次 B4",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[("copy_of_b1.csv", shared), ("batch_b4.csv", fresh)],
    )
    mailbox.append_message(mail_root, "INBOX", new_mail)

    # 提交阶段注入故障（投递记录插入失败 -> 事务回滚）
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("模拟提交失败")

    original_insert_delivery = db.insert_delivery
    monkeypatch.setattr(db, "insert_delivery", _boom)
    report = _run(host, port)
    assert report.stopped is not None
    assert report.stopped["uid"] == 4
    assert "提交失败" in report.stopped["reason"]

    # 不留部分可查询结果：新样品查不到，投递记录与进度未推进
    with db.connect(db_path) as conn:
        assert db.query_results_by_sample(conn, "S-2001") == []
        assert db.find_delivery(
            conn, f"{host}|{port}|INBOX", "inst-0004@lab-instrument"
        ) is None
        state = db.get_state(conn, f"{host}|{port}|INBOX")
        assert state["last_uid"] == 2

    # 失败清理：新内容的未引用原件被删除；
    # 与已提交邮件共享（相同 SHA-256）的原件仍在引用，必须保留
    store = _store()
    assert not store.has(fresh_sha)
    assert store.has(shared_sha)

    # 故障解除后重试：整封重新处理，共享原件只存一份
    monkeypatch.setattr(db, "insert_delivery", original_insert_delivery)
    report = _run(host, port)
    assert report.stopped is None
    assert report.total_new_records == 4
    assert store.has(fresh_sha)
    assert store.list_stored().count(shared_sha) == 1
    with db.connect(db_path) as conn:
        rows = db.query_results_by_sample(conn, "S-2001")
        assert len(rows) == 1
        assert rows[0]["record_seq"] == 1
        attachments = db.list_attachments(
            conn, message_id="inst-0004@lab-instrument"
        )
        assert [a["attach_index"] for a in attachments] == [1, 2]
        assert attachments[0]["sha256"] == shared_sha


def test_reset_clears_results_and_originals(imap_server, db_path):
    host, port, _ = imap_server
    client = TestClient(app)
    client.post("/collect", json=_payload(host, port))
    store = _store()
    assert store.list_stored() != []

    assert client.post("/admin/reset").json() == {"status": "reset"}
    assert store.list_stored() == []
    assert client.get("/samples").json()["count"] == 0
