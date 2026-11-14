"""FastAPI 应用：采集触发、样品结果查询与结果溯源（邮件标识/原件下载）。"""
from __future__ import annotations

import os
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .blobstore import BlobStore
from .collector import collect, recover_storage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动恢复：清理上次进程在「落盘」与「提交」之间退出留下的
    # 未引用原件；已发布原件保留，采集进度不变，可继续断点续采
    recovery = recover_storage()
    if recovery["removed_unreferenced"]:
        print(
            "原件库恢复：清理未引用文件 "
            f"{len(recovery['removed_unreferenced'])} 个",
            flush=True,
        )
    yield


app = FastAPI(
    title="仪器邮件 CSV 采集服务",
    description=(
        "从 IMAP 邮箱采集仪器 CSV 附件并入库，支持按样品编号查询检测结果，"
        "并可按结果 ID 溯源邮件标识与下载原始 CSV 原件。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


class CollectRequest(BaseModel):
    host: str = Field(..., description="IMAP 服务器地址")
    port: int = Field(..., ge=1, le=65535, description="IMAP 端口")
    folder: str = Field(..., description="邮箱文件夹，如 INBOX")
    username: str
    password: str
    use_ssl: bool = Field(False, description="是否使用 IMAP over SSL")


class CollectResponse(BaseModel):
    status: str
    mailbox_key: str
    folder: str
    uidvalidity: int
    resume_from_uid: int
    scanned_uids: list[int]
    ingested: list[dict[str, Any]]
    skipped_duplicates: list[dict[str, Any]]
    stopped: dict[str, Any] | None
    total_new_records: int


def _result_row_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["original_retained"] = row["attachment_id"] is not None
    return data


def _attachment_dict(row) -> dict[str, Any]:
    return {
        "attachment_id": row["id"],
        "mailbox_key": row["mailbox_key"],
        "message_id": row["message_id"],
        "attach_index": row["attach_index"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "record_count": row["record_count"],
        "ingested_at": row["ingested_at"],
        "download_url": f"/attachments/{row['id']}/download",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/collect", response_model=CollectResponse)
def trigger_collect(req: CollectRequest) -> dict[str, Any]:
    report = collect(
        host=req.host,
        port=req.port,
        folder=req.folder,
        username=req.username,
        password=req.password,
        use_ssl=req.use_ssl,
    )
    return report.to_dict()


@app.get("/results/{sample_no}")
def get_results(sample_no: str) -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.query_results_by_sample(conn, sample_no)
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到样品 {sample_no} 的结果")
    return {
        "sample_no": sample_no,
        "count": len(rows),
        "results": [_result_row_dict(row) for row in rows],
    }


@app.get("/trace/{result_id}")
def get_trace(result_id: int) -> dict[str, Any]:
    """按结果 ID 溯源：邮件标识、附件名、非空数据记录序号（从 1 起）、原件下载。

    同名附件以 attach_index（邮件内附件序号）与 sha256 区分；
    溯源功能启用前采集的旧记录标明 original_retained=false（原件未留存）。
    """
    with db.connect() as conn:
        row = db.query_result_trace(conn, result_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到结果记录 {result_id}")
    response: dict[str, Any] = {
        "result_id": row["id"],
        "sample_no": row["sample_no"],
        "test_item": row["test_item"],
        "result": row["result"],
        "unit": row["unit"],
        "ingested_at": row["ingested_at"],
        "mail": {
            "mailbox_key": row["mailbox_key"],
            "message_id": row["message_id"],
            "uid": row["uid"],
        },
        "original_retained": row["attachment_id"] is not None,
        "original": None,
    }
    if row["attachment_id"] is None:
        response["note"] = "该结果采集于原件留存启用前，原始 CSV 未留存"
    else:
        store = BlobStore(db.get_files_dir())
        response["original"] = {
            "attachment_id": row["attachment_id"],
            "attach_index": row["attach_index"],
            "filename": row["filename"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "record_seq": row["record_seq"],
            "file_present": store.has(row["sha256"]),
            "download_url": f"/attachments/{row['attachment_id']}/download",
        }
    return response


@app.get("/attachments")
def list_attachments(
    message_id: str | None = None, mailbox_key: str | None = None
) -> dict[str, Any]:
    """列出已留存原件的附件（可按 Message-ID 过滤；同名附件按 attach_index 区分）。"""
    with db.connect() as conn:
        rows = db.list_attachments(
            conn, message_id=message_id, mailbox_key=mailbox_key
        )
    return {
        "count": len(rows),
        "attachments": [_attachment_dict(row) for row in rows],
    }


@app.get("/attachments/{attachment_id}")
def get_attachment(attachment_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = db.get_attachment(conn, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到附件 {attachment_id}")
    return _attachment_dict(row)


@app.get("/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int) -> FileResponse:
    """下载附件解码后的原始字节（即使源邮件已从邮箱清理）。"""
    with db.connect() as conn:
        row = db.get_attachment(conn, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到附件 {attachment_id}")
    store = BlobStore(db.get_files_dir())
    path = store.blob_path(row["sha256"])
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404, detail="原件文件缺失，可能已被清理或存储卷未挂载"
        )
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=row["filename"],
        headers={"X-Content-SHA256": row["sha256"]},
    )


@app.get("/samples")
def get_samples() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_samples(conn)
    return {"count": len(rows), "samples": [dict(row) for row in rows]}


@app.get("/ingest/state")
def get_ingest_state() -> dict[str, Any]:
    with db.connect() as conn:
        states = [
            dict(row)
            for row in conn.execute("SELECT * FROM ingest_state ORDER BY mailbox_key")
        ]
    return {"count": len(states), "states": states}


@app.get("/ingest/deliveries")
def get_deliveries() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_deliveries(conn)
    return {"count": len(rows), "deliveries": [dict(row) for row in rows]}


@app.post("/admin/reset")
def reset_database() -> dict[str, str]:
    """清空全部业务数据与原件文件（演示/测试用，生产环境请经网关限制访问）。"""
    db.reset_all(db.get_db_path())
    store = BlobStore(db.get_files_dir())
    store.ensure_dirs()
    store.sweep(set())
    return {"status": "reset"}
