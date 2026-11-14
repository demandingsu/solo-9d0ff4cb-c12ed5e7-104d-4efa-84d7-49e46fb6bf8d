"""文件型邮箱存储，供样例 IMAP 服务与演示脚本共用。

目录结构：
  <root>/<folder>/meta.json   {"uidvalidity": int, "next_uid": int}
  <root>/<folder>/<uid>.eml   单封 RFC822 邮件
"""
from __future__ import annotations

import email
import json
import os
import re
from email.utils import formatdate
from typing import Optional

DEFAULT_ROOT = os.environ.get("MAIL_ROOT", "/srv/mail")
DEFAULT_USER = os.environ.get("MAIL_USER", "instrument@lab.local")
DEFAULT_PASSWORD = os.environ.get("MAIL_PASSWORD", "labpass")


def safe_folder(folder: str) -> str:
    folder = folder.strip()
    if not folder or not re.fullmatch(r"[A-Za-z0-9._\-]+", folder):
        raise ValueError(f"非法文件夹名: {folder!r}")
    return folder


def folder_dir(root: str, folder: str) -> str:
    return os.path.join(root, safe_folder(folder))


def _meta_path(root: str, folder: str) -> str:
    return os.path.join(folder_dir(root, folder), "meta.json")


def folder_exists(root: str, folder: str) -> bool:
    return os.path.isfile(_meta_path(root, folder))


def ensure_folder(root: str, folder: str, uidvalidity: Optional[int] = None) -> None:
    os.makedirs(folder_dir(root, folder), exist_ok=True)
    path = _meta_path(root, folder)
    if not os.path.exists(path):
        import time

        meta = {
            "uidvalidity": uidvalidity or int(time.time()),
            "next_uid": 1,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)


def read_meta(root: str, folder: str) -> dict:
    ensure_folder(root, folder)
    with open(_meta_path(root, folder), encoding="utf-8") as fh:
        return json.load(fh)


def write_meta(root: str, folder: str, meta: dict) -> None:
    with open(_meta_path(root, folder), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def list_uids(root: str, folder: str) -> list[int]:
    directory = folder_dir(root, folder)
    uids = []
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            if name.endswith(".eml"):
                uids.append(int(name[:-4]))
    return sorted(uids)


def read_message(root: str, folder: str, uid: int) -> Optional[bytes]:
    path = os.path.join(folder_dir(root, folder), f"{uid}.eml")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def append_message(
    root: str, folder: str, raw: bytes, *, uid: Optional[int] = None
) -> int:
    ensure_folder(root, folder)
    meta = read_meta(root, folder)
    if uid is None:
        uid = meta["next_uid"]
    path = os.path.join(folder_dir(root, folder), f"{uid}.eml")
    with open(path, "wb") as fh:
        fh.write(raw)
    meta["next_uid"] = max(meta["next_uid"], uid + 1)
    write_meta(root, folder, meta)
    return uid


def reset_uidvalidity(root: str, folder: str, uidvalidity: int) -> None:
    """模拟邮箱重建：换 UIDVALIDITY 并重排 UID（内容保持不变）。"""
    ensure_folder(root, folder)
    directory = folder_dir(root, folder)
    uids = list_uids(root, folder)
    stored = {uid: read_message(root, folder, uid) for uid in uids}
    for uid in uids:
        os.remove(os.path.join(directory, f"{uid}.eml"))
    for index, old_uid in enumerate(uids, start=1):
        with open(os.path.join(directory, f"{index}.eml"), "wb") as fh:
            fh.write(stored[old_uid])
    write_meta(
        root,
        folder,
        {"uidvalidity": uidvalidity, "next_uid": len(uids) + 1},
    )


def replace_attachment(raw: bytes, attachment_name: str, payload: bytes) -> bytes:
    """重建指定附件部件（保持 Message-ID 不变），返回新的 RFC822 字节。

    直接 set_payload 会破坏原部件的 base64 编码，这里整体重建 MIME 部件，
    模拟运维侧“用修复后的 CSV 替换坏附件并重新投递”。
    """
    msg = email.message_from_bytes(raw)
    target = None
    container = None
    for part in msg.walk():
        if part.is_multipart():
            for child in part.get_payload():
                if child.get_filename() == attachment_name:
                    target = child
                    container = part
                    break
        if target is not None:
            break
    if target is None or container is None:
        raise ValueError(f"邮件中找不到附件 {attachment_name}")

    from email.message import EmailMessage

    new_part = EmailMessage()
    new_part.set_content(
        payload,
        maintype="text",
        subtype="csv",
        filename=attachment_name,
    )
    parts = container.get_payload()
    index = parts.index(target)
    parts[index] = new_part
    return msg.as_bytes()


def build_message(
    message_id: str,
    subject: str,
    sender: str,
    recipient: str,
    attachments: list[tuple[str, bytes]],
    *,
    date: Optional[str] = None,
) -> bytes:
    """构造带若干附件的 multipart/mixed RFC822 邮件。"""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = date or formatdate(localtime=True)
    msg.set_content("仪器自动上传的检测结果，详见 CSV 附件。")
    for name, payload in attachments:
        msg.add_attachment(
            payload,
            maintype="text",
            subtype="csv",
            filename=name,
        )
    return msg.as_bytes()
