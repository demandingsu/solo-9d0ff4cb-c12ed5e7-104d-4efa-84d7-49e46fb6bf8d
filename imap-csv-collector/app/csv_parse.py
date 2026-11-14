"""仪器 CSV 附件解析与校验。

约定 CSV 为 UTF-8（可带 BOM），首行表头必须严格为：
    样品编号,检测项目,结果,单位
其后每行一条检测记录，四个字段均不允许为空。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

REQUIRED_HEADER = ["样品编号", "检测项目", "结果", "单位"]


class CsvError(ValueError):
    """CSV 内容不符合约定，整封邮件应判定为失败。"""

    def __init__(self, attachment: str, message: str):
        super().__init__(f"附件 {attachment}: {message}")
        self.attachment = attachment
        self.message = message


@dataclass(frozen=True)
class ResultRecord:
    sample_no: str
    test_item: str
    result: str
    unit: str
    # 附件内非空数据记录序号（从 1 起），用于从检测记录回溯原件中的行
    seq: int = 0

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.sample_no, self.test_item, self.result, self.unit)


def parse_csv(raw: bytes, attachment_name: str) -> list[ResultRecord]:
    """解析单个 CSV 附件，任何不合规都抛 CsvError。"""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError(attachment_name, f"不是合法的 UTF-8 文本: {exc}") from exc

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvError(attachment_name, "空文件，没有表头和数据")

    header = [cell.strip() for cell in rows[0]]
    if header != REQUIRED_HEADER:
        raise CsvError(
            attachment_name,
            f"表头应为 {REQUIRED_HEADER}，实际为 {header}",
        )

    records: list[ResultRecord] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) != len(REQUIRED_HEADER):
            raise CsvError(
                attachment_name,
                f"第 {line_no} 行列数为 {len(row)}，应为 {len(REQUIRED_HEADER)}",
            )
        sample_no, test_item, result, unit = (cell.strip() for cell in row)
        if not sample_no:
            raise CsvError(attachment_name, f"第 {line_no} 行样品编号为空")
        if not test_item:
            raise CsvError(attachment_name, f"第 {line_no} 行检测项目为空")
        if not result:
            raise CsvError(attachment_name, f"第 {line_no} 行结果为空")
        if not unit:
            raise CsvError(attachment_name, f"第 {line_no} 行单位为空")
        records.append(
            ResultRecord(sample_no, test_item, result, unit, seq=len(records) + 1)
        )

    if not records:
        raise CsvError(attachment_name, "只有表头，没有检测记录")
    return records
