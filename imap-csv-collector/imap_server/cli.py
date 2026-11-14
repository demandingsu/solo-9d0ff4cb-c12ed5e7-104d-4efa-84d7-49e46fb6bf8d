"""样例 IMAP 服务与演示场景控制 CLI。

子命令：
  serve                 启动 IMAP 服务
  seed-if-empty         邮箱为空时写入演示种子邮件
  list                  查看文件夹 UID 与 Message-ID
  replace-uid           用修复后的 CSV 重写指定 UID 的邮件
  redeliver             以新 UID 重投指定邮件（附件原样）
  redeliver-modified    以新 UID 重投同 Message-ID 但替换附件内容的邮件
  rebuild               模拟邮箱重建：更换 UIDVALIDITY 并把 UID 从 1 重排
"""
from __future__ import annotations

import argparse
import email
import sys
import time

from . import mailbox, seed as seed_mod
from .server import serve


def _message_id(raw: bytes) -> str:
    msg = email.message_from_bytes(raw)
    return (msg.get("Message-ID") or "").strip("<>")


def _load_attachment_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _replace_attachment(raw: bytes, attachment_name: str, payload: bytes) -> bytes:
    return mailbox.replace_attachment(raw, attachment_name, payload)


def cmd_serve(args: argparse.Namespace) -> None:
    mailbox.ensure_folder(args.root, args.folder)
    if args.seed:
        seed_mod.seed(args.root, args.folder)
    serve(args.host, args.port, args.root, args.username, args.password)


def cmd_seed_if_empty(args: argparse.Namespace) -> None:
    seed_mod.seed(args.root, args.folder)
    print(f"种子邮件就绪: {args.root}/{args.folder}")


def cmd_list(args: argparse.Namespace) -> None:
    if not mailbox.folder_exists(args.root, args.folder):
        raise SystemExit(f"文件夹不存在: {args.folder}")
    meta = mailbox.read_meta(args.root, args.folder)
    print(f"folder={args.folder} uidvalidity={meta['uidvalidity']} "
          f"next_uid={meta['next_uid']}")
    for uid in mailbox.list_uids(args.root, args.folder):
        raw = mailbox.read_message(args.root, args.folder, uid) or b""
        msg = email.message_from_bytes(raw)
        names = [
            part.get_filename()
            for part in msg.walk()
            if part.get_filename()
        ]
        print(f"  UID={uid} Message-ID={_message_id(raw)} attachments={names}")


def cmd_replace_uid(args: argparse.Namespace) -> None:
    raw = mailbox.read_message(args.root, args.folder, args.uid)
    if raw is None:
        raise SystemExit(f"UID {args.uid} 不存在")
    payload = _load_attachment_bytes(args.csv_path)
    new_raw = _replace_attachment(raw, args.attachment, payload)
    path = f"{mailbox.folder_dir(args.root, args.folder)}/{args.uid}.eml"
    with open(path, "wb") as fh:
        fh.write(new_raw)
    print(f"已重写 UID={args.uid} 的附件 {args.attachment}")


def cmd_redeliver(args: argparse.Namespace) -> None:
    raw = mailbox.read_message(args.root, args.folder, args.uid)
    if raw is None:
        raise SystemExit(f"UID {args.uid} 不存在")
    new_uid = mailbox.append_message(args.root, args.folder, raw)
    print(f"已重投邮件 Message-ID={_message_id(raw)}，新 UID={new_uid}")


def cmd_redeliver_modified(args: argparse.Namespace) -> None:
    raw = mailbox.read_message(args.root, args.folder, args.uid)
    if raw is None:
        raise SystemExit(f"UID {args.uid} 不存在")
    payload = _load_attachment_bytes(args.csv_path)
    new_raw = _replace_attachment(raw, args.attachment, payload)
    new_uid = mailbox.append_message(args.root, args.folder, new_raw)
    print(
        f"已重投（附件被修改）Message-ID={_message_id(raw)}，新 UID={new_uid}"
    )


def cmd_rebuild(args: argparse.Namespace) -> None:
    new_validity = args.uidvalidity or int(time.time())
    mailbox.reset_uidvalidity(args.root, args.folder, new_validity)
    print(
        f"邮箱已重建: folder={args.folder} 新 UIDVALIDITY={new_validity}，"
        f"UID 从 1 重排，邮件内容保持不变"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 IMAP 样例服务与演示控制")
    parser.add_argument("--root", default=mailbox.DEFAULT_ROOT)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--username", default=mailbox.DEFAULT_USER)
    parser.add_argument("--password", default=mailbox.DEFAULT_PASSWORD)

    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 IMAP 服务")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=10143)
    p_serve.add_argument(
        "--no-seed", dest="seed", action="store_false", help="不自动写种子邮件"
    )
    p_serve.set_defaults(seed=True, func=cmd_serve)

    sub.add_parser("seed-if-empty", help="写入演示种子邮件").set_defaults(
        func=cmd_seed_if_empty
    )
    sub.add_parser("list", help="列出文件夹邮件").set_defaults(func=cmd_list)

    p_replace = sub.add_parser("replace-uid", help="重写指定 UID 邮件的附件")
    p_replace.add_argument("uid", type=int)
    p_replace.add_argument("--attachment", required=True)
    p_replace.add_argument("--csv-path", required=True)
    p_replace.set_defaults(func=cmd_replace_uid)

    p_redeliver = sub.add_parser("redeliver", help="原样重投指定 UID 邮件")
    p_redeliver.add_argument("uid", type=int)
    p_redeliver.set_defaults(func=cmd_redeliver)

    p_modified = sub.add_parser(
        "redeliver-modified", help="重投同 Message-ID 但附件内容不同的邮件"
    )
    p_modified.add_argument("uid", type=int)
    p_modified.add_argument("--attachment", required=True)
    p_modified.add_argument("--csv-path", required=True)
    p_modified.set_defaults(func=cmd_redeliver_modified)

    p_rebuild = sub.add_parser("rebuild", help="模拟邮箱重建（换 UIDVALIDITY）")
    p_rebuild.add_argument("--uidvalidity", type=int, default=0)
    p_rebuild.set_defaults(func=cmd_rebuild)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
