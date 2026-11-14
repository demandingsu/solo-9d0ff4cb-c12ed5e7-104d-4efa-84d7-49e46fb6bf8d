"""演示用初始邮件：两封正常邮件（第二封含两个同名附件）+ 一封多附件坏 CSV 邮件。"""
from __future__ import annotations

from . import mailbox

HEADER = "样品编号,检测项目,结果,单位"


def csv_text(rows: list[tuple[str, str, str, str]]) -> bytes:
    lines = [HEADER]
    lines.extend(",".join(row) for row in rows)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


BAD_CSV = (
    "样品编号,检测项目,结果,单位\r\n"
    "S-1002,铅Pb,0.03,mg/kg\r\n"
    "S-1002,镉Cd,,mg/kg\r\n"  # 结果为空 -> 整封判定坏附件
).encode("utf-8")


def seed(root: str, folder: str = "INBOX") -> None:
    if mailbox.folder_exists(root, folder) and mailbox.list_uids(root, folder):
        return
    mailbox.ensure_folder(root, folder)

    mail1 = mailbox.build_message(
        message_id="inst-0001@lab-instrument",
        subject="仪器结果 批次 B1",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b1.csv",
                csv_text(
                    [
                        ("S-1001", "铅Pb", "0.021", "mg/kg"),
                        ("S-1001", "镉Cd", "0.008", "mg/kg"),
                        ("S-1002", "铅Pb", "0.030", "mg/kg"),
                    ]
                ),
            )
        ],
    )
    mailbox.append_message(root, folder, mail1)

    # 第二封：两个同名附件 batch_b2.csv（不同仪器同名导出），
    # 用于演示同名附件按邮件内序号 attach_index 区分
    mail2 = mailbox.build_message(
        message_id="inst-0002@lab-instrument",
        subject="仪器结果 批次 B2（两个同名附件）",
        sender="instrument-b@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b2.csv",
                csv_text(
                    [
                        ("S-1003", "汞Hg", "0.005", "mg/kg"),
                    ]
                ),
            ),
            (
                "batch_b2.csv",
                csv_text(
                    [
                        ("S-1004", "砷As", "0.012", "mg/kg"),
                    ]
                ),
            ),
        ],
    )
    mailbox.append_message(root, folder, mail2)

    # 第三封：两个附件，其中一个 CSV 缺字段 -> 整封失败、停在此邮件
    mail3 = mailbox.build_message(
        message_id="inst-0003@lab-instrument",
        subject="仪器结果 批次 B3（含损坏附件）",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b3_good.csv",
                csv_text(
                    [
                        ("S-1005", "铬Cr", "0.044", "mg/kg"),
                    ]
                ),
            ),
            ("batch_b3_bad.csv", BAD_CSV),
        ],
    )
    mailbox.append_message(root, folder, mail3)
