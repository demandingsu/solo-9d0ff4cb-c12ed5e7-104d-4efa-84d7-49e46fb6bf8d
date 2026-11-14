"""FastAPI 应用：采集触发与样品结果查询。"""
from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db
from .collector import collect


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="仪器邮件 CSV 采集服务",
    description="从 IMAP 邮箱采集仪器 CSV 附件并入库，支持按样品编号查询检测结果。",
    version="1.0.0",
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
        "results": [dict(row) for row in rows],
    }


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
    """清空全部业务数据（演示/测试用，生产环境请经网关限制访问）。"""
    db.reset_all(db.get_db_path())
    return {"status": "reset"}
