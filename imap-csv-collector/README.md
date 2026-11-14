# 仪器邮件 CSV 采集服务

检测中心资料员无需逐封下载邮件：服务通过 IMAP 自动采集仪器邮件中的 CSV 附件，
解析后写入 SQLite 样品结果库，并提供按样品编号查询的 HTTP API。
质控员可从异常结果一键溯源：按结果 ID 查回邮件标识、附件名与记录序号，
并下载附件解码后的原始 CSV 原件（即使源邮件已从邮箱清理）。

## 核心保证

| 需求 | 实现方式 |
| --- | --- |
| 整封邮件原子入库 | 一封邮件的**全部** CSV 解析成功后，先把全部附件原件落盘，检测记录、来源关联、投递记录、UID 进度才在同一个 SQLite 事务提交；解析、落盘或提交任一环节失败都整封放弃，无部分可查询结果 |
| 原件留存与溯源 | 新增采集的每封邮件，其附件解码后的原始字节以独立文件保存在持久卷（`COLLECTOR_FILES`，默认 `<db 目录>/originals/`），按 SHA-256 内容寻址，**相同 SHA-256 只存一份**；按结果 ID 返回邮件标识、附件名、非空数据记录序号（从 1 起），可下载对应原件 |
| 同名附件区分 | 同一封邮件的多个同名附件按邮件内附件序号 `attach_index`（从 1 起）与 SHA-256 区分，各自独立溯源与下载 |
| 旧记录兼容 | 溯源功能启用前采集的结果，迁移后保留原数据，溯源响应以 `original_retained=false` 标明原件未留存 |
| 崩溃恢复 | 进程在落盘与提交之间退出时，重启后自动清理未被任何已提交投递引用的原件，已发布原件全部保留，采集从断点续采；失败清理只删未引用文件，其他邮件仍引用的原件（相同 SHA-256）不受影响 |
| 失败停在该邮件并返回标识 | 坏附件 / 断线 / 冲突都通过响应的 `stopped` 字段返回 UID、Message-ID、主题和原因，进度不推进 |
| 重启可续采 | 进度持久化在 `ingest_state`，下次从 `last_uid + 1` 开始 |
| UIDVALIDITY 改变后重扫 | 检测到变化即清零进度重新扫描，历史投递记录保留用于去重 |
| 重投识别 | Message-ID + 全部附件的 SHA-256 摘要：同 ID 同摘要跳过；同 ID 不同摘要停采待核实，原结果原样保留 |
| 采集参数 | 每次采集请求显式传入 IMAP 主机、端口、文件夹及账号密码 |

## 目录结构

```
app/                 FastAPI + 采集核心 + SQLite 持久层
  main.py            HTTP 路由（采集、查询、溯源、原件下载）
  collector.py       IMAP 采集、原件落盘、重投识别、事务边界、崩溃恢复
  blobstore.py       原件内容寻址存储（原子写入、SHA-256 去重、引用清理）
  csv_parse.py       CSV 校验解析（表头：样品编号,检测项目,结果,单位）
  db.py              SQLite 建表/迁移与查询
imap_server/         本地 IMAP 样例服务（纯标准库实现 IMAP4rev1 子集）
  server.py          IMAP 协议服务，支持 FLAKY_FETCH_AFTER 断线注入
  mailbox.py         文件邮箱、附件替换、邮箱重建
  seed.py            演示种子：正常邮件（含同名附件邮件）+ 多附件坏 CSV 邮件
  cli.py             邮箱控制 CLI（重投/重建/修复）
tests/               19 个 pytest 用例，覆盖全部承诺语义
scripts/demo.sh      端到端演示脚本
docker-compose.yml   api + imap 两个服务 + mailctl 控制容器
```

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

- API 文档： http://localhost:8000/docs （可交互演示采集、溯源与原件下载）
- IMAP 样例服务： localhost:10143（明文 IMAP，账号 `instrument@lab.local` / `labpass`）
- SQLite 数据、邮箱、原件文件分别持久化在 `db_data`、`mail_data`、`file_data` 卷

运行完整演示（多附件失败、溯源下载、同名附件、重启恢复、重投、邮箱重建、冲突停采）：

```bash
bash scripts/demo.sh
```

### 断线场景演示

给 imap 服务注入故障：第 N 次 FETCH 响应发送一半即断开 TCP。

```bash
FLAKY_FETCH_AFTER=2 docker compose up -d --force-recreate imap
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host":"imap","port":10143,"folder":"INBOX",
  "username":"instrument@lab.local","password":"labpass","use_ssl":false}'
# stopped.uid 指向失败邮件；去掉环境变量重启 imap 后再次调用即从断点续采
```

## API

### POST /collect — 触发一轮采集

```bash
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host": "imap",
  "port": 10143,
  "folder": "INBOX",
  "username": "instrument@lab.local",
  "password": "labpass",
  "use_ssl": false
}'
```

响应（正常完成 `status=completed`；停采时 `status=stopped` 并带 `stopped` 邮件标识）：

```json
{
  "status": "stopped",
  "mailbox_key": "imap|10143|INBOX",
  "uidvalidity": 1794581572,
  "resume_from_uid": 3,
  "scanned_uids": [1, 2, 3],
  "ingested": [
    {
      "uid": 1,
      "message_id": "inst-0001@lab-instrument",
      "attachments": [
        {"attach_index": 1, "filename": "batch_b1.csv", "sha256": "...", "size_bytes": 128, "record_count": 3}
      ],
      "record_count": 3,
      "originals_retained": true
    }
  ],
  "skipped_duplicates": [],
  "stopped": {
    "reason": "坏 CSV 附件，整封邮件不入库: 附件 batch_b3_bad.csv: 第 3 行结果为空",
    "uid": 3,
    "message_id": "inst-0003@lab-instrument",
    "subject": "仪器结果 批次 B3（含损坏附件）"
  },
  "total_new_records": 5
}
```

### GET /results/{样品编号} — 按样品查询

```bash
curl localhost:8000/results/S-1001
```

每条结果带 `id`（结果 ID）、`record_seq`（附件内非空数据记录序号，从 1 起）、
`attachment_filename`、`original_retained` 等溯源字段。

### GET /trace/{结果ID} — 溯源邮件标识与原件

```bash
curl localhost:8000/trace/1
```

```json
{
  "result_id": 1,
  "sample_no": "S-1001",
  "test_item": "铅Pb",
  "result": "0.021",
  "unit": "mg/kg",
  "mail": {
    "mailbox_key": "imap|10143|INBOX",
    "message_id": "inst-0001@lab-instrument",
    "uid": 1
  },
  "original_retained": true,
  "original": {
    "attachment_id": 1,
    "attach_index": 1,
    "filename": "batch_b1.csv",
    "sha256": "…",
    "size_bytes": 128,
    "record_seq": 1,
    "file_present": true,
    "download_url": "/attachments/1/download"
  }
}
```

旧记录（溯源功能启用前采集）返回 `"original_retained": false`、`"original": null`
并附 `note` 说明原件未留存。

### GET /attachments/{附件ID}/download — 下载原始 CSV 原件

```bash
curl -OJ localhost:8000/attachments/1/download
```

返回附件解码后的原始字节（`Content-Disposition` 带原文件名，
`X-Content-SHA256` 头带内容摘要）；源邮件即使已从邮箱清理也可下载。

### 其他端点

| 端点 | 说明 |
| --- | --- |
| `GET /attachments` | 原件附件列表（可按 `message_id`、`mailbox_key` 过滤；同名附件按 `attach_index` 区分） |
| `GET /attachments/{id}` | 单个附件元数据 |
| `GET /samples` | 已入库样品汇总 |
| `GET /ingest/state` | 各邮箱 UIDVALIDITY 与 last_uid 进度 |
| `GET /ingest/deliveries` | 已处理投递（Message-ID、附件摘要） |
| `GET /health` | 健康检查 |
| `POST /admin/reset` | 清空结果库与原件文件（演示用） |

## CSV 约定

UTF-8（可带 BOM），首行表头固定为 `样品编号,检测项目,结果,单位`，
四个字段均不允许为空，否则该附件所在整封邮件判定失败。

## 本地无 Docker 运行 / 测试

```bash
python -m venv .venv && source .venv/bin/activate   # 或使用已有解释器
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q

# 手动起两个进程
python -m imap_server.cli serve --host 127.0.0.1 --port 10143
COLLECTOR_DB=./data/collector.db COLLECTOR_FILES=./data/originals \
  uvicorn app.main:app --port 8000
```

## 设计说明与边界

- 采集按邮箱文件夹维度维护进度，`mailbox_key = host|port|folder`。
- 附件摘要对「文件名 + 文件内容 SHA-256」排序后再做一次 SHA-256，
  附件顺序变化不影响判定，内容或文件名变化即视为不同投递。
- 原件库为内容寻址存储：`<原件目录>/blobs/<sha256[:2]>/<sha256>`，
  附件解码字节先写入 `tmp/` 临时文件再原子改名，崩溃不会留下写一半的原件；
  相同 SHA-256 只存一份，多封邮件引用同一内容不会重复占用空间。
- 每封邮件的处理顺序：解析全部附件 -> 全部原件落盘 -> 单个事务提交
  （检测记录 + 附件溯源关联 + 投递记录 + UID 进度）。进程在落盘与提交
  之间退出时，重启后 `recover_storage` 清理未引用原件并保留已发布原件，
  采集从断点续采；`POST /admin/reset` 会同时清空结果库与原件文件。
- 同名附件按邮件内附件序号 `attach_index`（从 1 起）区分；结果记录的
  `record_seq` 对应该附件内非空数据记录序号（从 1 起，空行不计）。
- 同名附件的下载 URL 以附件 ID 区分，`Content-Disposition` 文件名相同属正常。
- 样例 IMAP 服务仅实现采集所需命令（LOGIN/SELECT/STATUS/UID SEARCH/UID FETCH 等），
  面向演示与测试，不可作为生产邮箱使用。
- `/collect` 在单进程内加锁串行化；生产多副本部署时应改用分布式锁或单 worker。
