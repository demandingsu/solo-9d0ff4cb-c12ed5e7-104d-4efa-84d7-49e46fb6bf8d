import email
import os

from app import db
from app.collector import collect, make_mailbox_key
from imap_server import mailbox
from imap_server.server import ThreadedImapServer
from tests.conftest import PASSWORD, USER


def _run(host, port):
    return collect(
        host=host,
        port=port,
        folder="INBOX",
        username=USER,
        password=PASSWORD,
        use_ssl=False,
    )


def _replace_attachment(raw: bytes, name: str, payload: bytes) -> bytes:
    return mailbox.replace_attachment(raw, name, payload)


def test_happy_path_stops_on_bad_attachment(imap_server, db_path):
    host, port, root = imap_server
    report = _run(host, port)

    # 停在第三封（多附件之一损坏），前两封成功
    assert report.stopped is not None
    assert report.stopped["uid"] == 3
    assert "坏 CSV" in report.stopped["reason"]
    assert len(report.ingested) == 2
    assert report.total_new_records == 5

    with db.connect(db_path) as conn:
        rows = db.query_results_by_sample(conn, "S-1001")
        assert len(rows) == 2
        # 坏邮件里的好附件也不得部分入库
        assert db.query_results_by_sample(conn, "S-1005") == []
        state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
        assert state["last_uid"] == 2


def test_resume_after_fix(imap_server, db_path):
    host, port, root = imap_server
    first = _run(host, port)
    assert first.stopped["uid"] == 3

    # 运维用修复后的 CSV 生成新邮件（新 Message-ID、新 UID），坏邮件仍留在邮箱
    fixed = (
        "样品编号,检测项目,结果,单位\r\n"
        "S-1002,铅Pb,0.03,mg/kg\r\n"
        "S-1002,镉Cd,0.009,mg/kg\r\n"
        "S-1005,铬Cr,0.044,mg/kg\r\n"
    ).encode("utf-8")
    repair_mail = mailbox.build_message(
        message_id="inst-0003-fixed@lab-instrument",
        subject="仪器结果 批次 B3（修复版）",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[("batch_b3_fixed.csv", fixed)],
    )
    mailbox.append_message(root, "INBOX", repair_mail)

    report = _run(host, port)
    # 坏 UID3 仍会先报错停采：演示“停在该邮件、不留部分结果、可重试”
    assert report.stopped is not None
    assert report.stopped["uid"] == 3
    assert report.ingested == []

    # 坏邮件移除后重启续采：从 UID3 继续，修复版入库
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")
    report = _run(host, port)
    assert report.stopped is None
    assert report.scanned_uids == [4]
    assert report.total_new_records == 3

    # 再跑一轮：没有新邮件，幂等无写入
    again = _run(host, port)
    assert again.stopped is None
    assert again.ingested == []
    assert again.scanned_uids == []


def test_rebuild_rescan_identifies_redelivery(imap_server, db_path):
    host, port, root = imap_server
    _run(host, port)

    # 移除坏邮件，投递修复版（新 Message-ID），完成一轮干净采集
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")
    fixed = (
        "样品编号,检测项目,结果,单位\r\n"
        "S-1002,镉Cd,0.009,mg/kg\r\n"
    ).encode("utf-8")
    repair_mail = mailbox.build_message(
        message_id="inst-0003-fixed@lab-instrument",
        subject="仪器结果 批次 B3（修复版）",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[("batch_b3_fixed.csv", fixed)],
    )
    mailbox.append_message(root, "INBOX", repair_mail)
    _run(host, port)

    with db.connect(db_path) as conn:
        before = len(db.query_results_by_sample(conn, "S-1001"))
        old_state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
        old_validity = old_state["uidvalidity"]

    # 邮箱重建：UIDVALIDITY 改变，UID 从 1 重排，内容不变
    mailbox.reset_uidvalidity(root, "INBOX", old_validity + 999)

    report = _run(host, port)
    assert report.stopped is None
    assert report.uidvalidity == old_validity + 999
    assert report.scanned_uids == [1, 2, 3]
    # 全部识别为重投，零重复入库
    assert report.ingested == []
    assert len(report.skipped_duplicates) == 3

    with db.connect(db_path) as conn:
        assert len(db.query_results_by_sample(conn, "S-1001")) == before
        state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
        assert state["uidvalidity"] == old_validity + 999
        assert state["last_uid"] == 3


def test_same_message_id_changed_attachment_stops(imap_server, db_path):
    host, port, root = imap_server
    _run(host, port)
    # 移除坏邮件 UID3，避免它挡在重投邮件之前
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")

    # 原样重投 UID1：应被跳过
    raw1 = mailbox.read_message(root, "INBOX", 1)
    new_uid = mailbox.append_message(root, "INBOX", raw1)
    report = _run(host, port)
    assert report.stopped is None
    assert len(report.skipped_duplicates) == 1
    assert report.skipped_duplicates[0]["uid"] == new_uid

    # 同 Message-ID 但附件内容被修改：停采，保留原结果
    changed = (
        "样品编号,检测项目,结果,单位\r\n"
        "S-1001,铅Pb,9.999,mg/kg\r\n"
    ).encode("utf-8")
    modified_uid = mailbox.append_message(
        root, "INBOX", _replace_attachment(raw1, "batch_b1.csv", changed)
    )
    report = _run(host, port)
    assert report.stopped is not None
    assert report.stopped["uid"] == modified_uid
    assert "Message-ID" in report.stopped["reason"]

    with db.connect(db_path) as conn:
        rows = db.query_results_by_sample(conn, "S-1001")
        # 原有 0.021 保持不变，伪造的 9.999 未入库
        assert all(row["result"] != "9.999" for row in rows)
        state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
        assert state["last_uid"] == new_uid  # 停在冲突邮件，进度未推进


def test_disconnect_resumes_without_partial_results(
    mail_root, db_path
):
    # 两个服务实例复用同一地址：断线后“重启服务”，邮箱 key 保持不变
    address = ("127.0.0.1", 0)
    server = ThreadedImapServer(
        address, mail_root, USER, PASSWORD, flaky_fetch_after=2
    )
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        report = _run(host, port)
        assert report.stopped is not None
        assert report.stopped["uid"] == 2
        assert "中断" in report.stopped["reason"]

        with db.connect(db_path) as conn:
            state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
            assert state["last_uid"] == 1  # 仅 UID1 落库
    finally:
        server.shutdown()
        server.server_close()

    # 服务重启（不再断线），同一 host/port 续采，仍会停在坏附件 UID3
    server2 = ThreadedImapServer(
        (host, port), mail_root, USER, PASSWORD
    )
    thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
    thread2.start()
    try:
        report = _run(host, port)
        assert [item["uid"] for item in report.ingested] == [2]
        assert report.stopped["uid"] == 3
    finally:
        server2.shutdown()
        server2.server_close()


def test_bad_login_reported(imap_server, db_path):
    host, port, _root = imap_server
    report = collect(
        host=host,
        port=port,
        folder="INBOX",
        username=USER,
        password="wrong-password",
        use_ssl=False,
    )
    assert report.stopped is not None
    assert "登录失败" in report.stopped["reason"]
    assert report.ingested == []
