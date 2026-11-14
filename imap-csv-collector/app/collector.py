"""IMAP 采集核心：拉取邮件 -> 解析 CSV -> 整封邮件原子入库。

保证：
  * 一封邮件的全部 CSV 都解析成功，才在同一个 SQLite 事务里
    一起提交检测记录、投递记录与 UID 进度；任一附件损坏则整封放弃。
  * 网络中断/坏附件/附件冲突都停在该邮件，不写部分结果；
    进度未推进，重启后从同一 UID 续采。
  * UIDVALIDITY 改变后从头重新扫描；用 Message-ID + 附件摘要识别重投：
    同 Message-ID 且摘要相同 -> 跳过不重复入库；
    同 Message-ID 但摘要不同 -> 停采待人工核实，保留原结果。
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from . import db
from .csv_parse import CsvError, parse_csv

# 单次 FETCH 的规模控制（逐个拉取，失败边界即单封邮件）
IMAP_TIMEOUT_SECONDS = 30


class IngestError(Exception):
    """采集在某封邮件处停止。"""

    def __init__(
        self,
        reason: str,
        *,
        uid: int | None = None,
        message_id: str | None = None,
        subject: str | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.uid = uid
        self.message_id = message_id
        self.subject = subject


@dataclass
class IngestReport:
    mailbox_key: str
    folder: str
    uidvalidity: int
    resume_from_uid: int
    scanned_uids: list[int] = field(default_factory=list)
    ingested: list[dict[str, Any]] = field(default_factory=list)
    skipped_duplicates: list[dict[str, Any]] = field(default_factory=list)
    stopped: dict[str, Any] | None = None
    total_new_records: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "stopped" if self.stopped else "completed",
            "mailbox_key": self.mailbox_key,
            "folder": self.folder,
            "uidvalidity": self.uidvalidity,
            "resume_from_uid": self.resume_from_uid,
            "scanned_uids": self.scanned_uids,
            "ingested": self.ingested,
            "skipped_duplicates": self.skipped_duplicates,
            "stopped": self.stopped,
            "total_new_records": self.total_new_records,
        }


def make_mailbox_key(host: str, port: int, folder: str) -> str:
    return f"{host}|{port}|{folder}"


def _decode_imap_string(value: Any) -> str:
    if isinstance(value, bytes):
        # 邮箱文件夹名可能是 IMAP UTF-7，样例服务与演示均为 ASCII
        return value.decode("utf-8", errors="replace")
    return str(value)


def _open_imap(
    host: str, port: int, username: str, password: str, use_ssl: bool
) -> imaplib.IMAP4:
    try:
        if use_ssl:
            client = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT_SECONDS)
        else:
            client = imaplib.IMAP4(host, port, timeout=IMAP_TIMEOUT_SECONDS)
    except OSError as exc:
        raise IngestError(f"无法连接 IMAP 服务器 {host}:{port}: {exc}") from exc
    try:
        client.login(username, password)
    except imaplib.IMAP4.error as exc:
        client.logout()
        raise IngestError(f"IMAP 登录失败: {exc}") from exc
    return client


def _fetch_uidvalidity(client: imaplib.IMAP4, folder: str) -> int:
    """优先用 STATUS 取 UIDVALIDITY（不改变当前选中状态）。"""
    typ, data = client.status(folder, "(UIDVALIDITY)")
    if typ != "OK":
        raise IngestError(f"STATUS 查询失败，文件夹可能不存在: {folder}")
    text = _decode_imap_string(data[0])
    match = re.search(r"UIDVALIDITY\s+(\d+)", text)
    if not match:
        raise IngestError(f"无法解析 UIDVALIDITY: {text!r}")
    return int(match.group(1))


def _select_folder(client: imaplib.IMAP4, folder: str) -> int:
    typ, data = client.select(folder, readonly=True)
    if typ != "OK":
        detail = _decode_imap_string(data[0]) if data else ""
        raise IngestError(f"无法选中文件夹 {folder!r}: {detail}")
    # 用 SELECT 响应里的 UIDVALIDITY 再核对一次
    typ, response = client.response("UIDVALIDITY")
    if typ == "UIDVALIDITY" and response and response[0]:
        return int(response[0])
    raise IngestError("SELECT 响应缺少 UIDVALIDITY")


def _search_uids(client: imaplib.IMAP4, start_uid: int) -> list[int]:
    criteria = f"UID {start_uid}:*" if start_uid > 0 else "ALL"
    typ, data = client.uid("search", None, criteria)
    if typ != "OK":
        raise IngestError("UID SEARCH 失败")
    if not data or not data[0]:
        return []
    return [int(part) for part in data[0].split()]


def _fetch_message(client: imaplib.IMAP4, uid: int) -> bytes:
    typ, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise IngestError(f"UID {uid} FETCH 失败", uid=uid)
    if not data:
        raise IngestError(f"UID {uid} FETCH 未返回内容", uid=uid)
    # data 形如 [(b'1 (UID 3 RFC822 {123}', bytes), b')']
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            return item[1]
    raise IngestError(f"UID {uid} FETCH 响应结构无法识别", uid=uid)


def _extract_message_id(msg: Message) -> str:
    raw = msg.get("Message-ID") or msg.get("Message-Id")
    if not raw:
        raise IngestError("邮件缺少 Message-ID 头，无法识别投递")
    return raw.strip().strip("<>") or ""


def _extract_subject(msg: Message) -> str | None:
    subject = msg.get("Subject")
    if subject is None:
        return None
    try:
        decoded = email.header.decode_header(subject)
        parts = [
            piece.decode(charset or "utf-8") if isinstance(piece, bytes) else piece
            for piece, charset in decoded
        ]
        return "".join(parts)
    except (LookupError, UnicodeDecodeError):
        return str(subject)


def _csv_attachments(msg: Message) -> list[tuple[str, bytes]]:
    attachments: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = (part.get_content_type() or "").lower()
        is_csv = (filename is not None and filename.lower().endswith(".csv")) or (
            content_type == "text/csv"
        )
        if not is_csv:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        attachments.append((filename or "unnamed.csv", payload))
    return attachments


def _attachment_digest(attachments: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(attachments, key=lambda item: item[0]):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _stop(report: IngestReport, err: IngestError) -> IngestReport:
    report.stopped = {
        "reason": err.reason,
        "uid": err.uid,
        "message_id": err.message_id,
        "subject": err.subject,
    }
    return report


def collect(
    *,
    host: str,
    port: int,
    folder: str,
    username: str,
    password: str,
    use_ssl: bool = False,
    db_path: str | None = None,
) -> IngestReport:
    """执行一轮采集，返回结构化报告。任何失败都通过 report.stopped 表达。"""
    mailbox_key = make_mailbox_key(host, port, folder)
    conn = db.connect(db_path)
    db.init_db(conn)
    # 同一进程内串行化采集，避免两个请求并发推进同一邮箱进度
    with db._ingest_lock:
        report = _collect_locked(
            conn=conn,
            mailbox_key=mailbox_key,
            host=host,
            port=port,
            folder=folder,
            username=username,
            password=password,
            use_ssl=use_ssl,
        )
    conn.close()
    return report


def _collect_locked(
    *,
    conn,
    mailbox_key: str,
    host: str,
    port: int,
    folder: str,
    username: str,
    password: str,
    use_ssl: bool,
) -> IngestReport:
    remote_validity = 0
    last_uid = 0
    current_uid: int | None = None
    client: imaplib.IMAP4 | None = None
    report = IngestReport(
        mailbox_key=mailbox_key,
        folder=folder,
        uidvalidity=0,
        resume_from_uid=1,
    )
    try:
        client = _open_imap(host, port, username, password, use_ssl)
        remote_validity = _fetch_uidvalidity(client, folder)
        selected_validity = _select_folder(client, folder)
        if selected_validity != remote_validity:
            raise IngestError(
                "STATUS 与 SELECT 返回的 UIDVALIDITY 不一致，停止采集"
            )

        state = db.get_state(conn, mailbox_key)
        if state is None:
            # 全新邮箱：登记 UIDVALIDITY，从 UID 1 开始
            db.upsert_state(conn, mailbox_key, remote_validity, 0)
            conn.commit()
            last_uid = 0
        elif state["uidvalidity"] != remote_validity:
            # 邮箱被重建：保留历史投递记录用于重投识别，进度清零重扫
            db.upsert_state(conn, mailbox_key, remote_validity, 0)
            conn.commit()
            last_uid = 0
        else:
            last_uid = state["last_uid"]

        report.uidvalidity = remote_validity
        report.resume_from_uid = last_uid + 1

        uids = _search_uids(client, last_uid + 1)
        for uid in uids:
            current_uid = uid
            report.scanned_uids.append(uid)
            try:
                raw_message = _fetch_message(client, uid)
            except IngestError as exc:
                if exc.uid is None:
                    exc.uid = uid
                return _stop(report, exc)

            try:
                msg = email.message_from_bytes(raw_message)
                message_id = _extract_message_id(msg)
                subject = _extract_subject(msg)
                attachments = _csv_attachments(msg)
                if not attachments:
                    raise IngestError(
                        "邮件没有 CSV 附件",
                        uid=uid,
                        message_id=message_id or None,
                        subject=subject,
                    )

                # 先全部解析成功，再谈入库 —— 坏附件整封放弃
                parsed: list[tuple[str, list]] = []
                for name, payload in attachments:
                    try:
                        parsed.append((name, parse_csv(payload, name)))
                    except CsvError as exc:
                        raise IngestError(
                            f"坏 CSV 附件，整封邮件不入库: {exc}",
                            uid=uid,
                            message_id=message_id,
                            subject=subject,
                        ) from exc

                digest = _attachment_digest(attachments)
                existing = db.find_delivery(conn, mailbox_key, message_id)
                if existing is not None:
                    if existing["attachment_digest"] == digest:
                        # 同一投递重投（含 UIDVALIDITY 重建后的重扫）：只推进度，不重复入库
                        report.skipped_duplicates.append(
                            {
                                "uid": uid,
                                "message_id": message_id,
                                "attachment_digest": digest,
                            }
                        )
                        db.upsert_state(conn, mailbox_key, remote_validity, uid)
                        conn.commit()
                        last_uid = uid
                        report.resume_from_uid = last_uid + 1
                        continue
                    # Message-ID 相同但附件不同：停采待核实，原结果原样保留
                    raise IngestError(
                        "Message-ID 已入库但附件摘要不同，疑似重投内容被修改，"
                        "停止采集待人工核实，原有结果保持不变",
                        uid=uid,
                        message_id=message_id,
                        subject=subject,
                    )

                all_records = [
                    record.as_tuple()
                    for _, records in parsed
                    for record in records
                ]
                # 检测记录 + 投递识别 + UID 进度 同一事务原子提交
                with db.transaction(conn):
                    db.insert_results(
                        conn, mailbox_key, message_id, uid, all_records
                    )
                    db.insert_delivery(
                        conn, mailbox_key, message_id, digest,
                        uid, remote_validity,
                    )
                    db.upsert_state(conn, mailbox_key, remote_validity, uid)

                report.ingested.append(
                    {
                        "uid": uid,
                        "message_id": message_id,
                        "subject": subject,
                        "attachments": [name for name, _ in attachments],
                        "record_count": len(all_records),
                        "attachment_digest": digest,
                    }
                )
                report.total_new_records += len(all_records)
                last_uid = uid
                report.resume_from_uid = last_uid + 1
            except IngestError as exc:
                if exc.uid is None:
                    exc.uid = uid
                return _stop(report, exc)

        return report
    except IngestError as exc:
        # 登录/选目录/解析等业务失败：未提交即无部分结果，可原样重试
        report.uidvalidity = remote_validity
        report.resume_from_uid = last_uid + 1
        return _stop(report, exc)
    except (OSError, imaplib.IMAP4.error) as exc:
        # 断线/协议错误：本封未提交即无部分结果，进度保持在最后成功的 UID
        report.uidvalidity = remote_validity
        report.resume_from_uid = last_uid + 1
        detail = exc.args[0].decode("utf-8", "replace") if exc.args and isinstance(
            exc.args[0], bytes
        ) else str(exc)
        return _stop(
            report,
            IngestError(
                f"IMAP 连接中断或协议错误: {detail}",
                uid=current_uid,
            ),
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
