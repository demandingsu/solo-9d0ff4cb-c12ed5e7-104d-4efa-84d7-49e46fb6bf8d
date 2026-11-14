"""附件原件的内容寻址存储（每个原件一个独立文件，相同 SHA-256 只存一份）。

布局（root 默认位于持久卷，见 db.get_files_dir）：
  <root>/blobs/<sha256[:2]>/<sha256>   附件解码后的原始字节，内容寻址
  <root>/tmp/                          写入中的临时文件（崩溃残留，启动时清理）

保证：
  * put 原子：先写 tmp 再 os.replace，崩溃不会留下写一半的正式文件；
    相同内容的附件永远只占用一份存储。
  * sweep(keep) 只删除不在 keep 集合中的原件 —— 其他邮件仍引用的
    原件（相同 SHA-256 共享一份）不会被失败清理或重启恢复误删。
"""
from __future__ import annotations

import hashlib
import os


class BlobStore:
    def __init__(self, root: str):
        self.root = root
        self.blob_dir = os.path.join(root, "blobs")
        self.tmp_dir = os.path.join(root, "tmp")

    def ensure_dirs(self) -> None:
        os.makedirs(self.blob_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

    def blob_path(self, sha256: str) -> str:
        return os.path.join(self.blob_dir, sha256[:2], sha256)

    def put(self, data: bytes) -> str:
        """写入一份原件，返回其 SHA-256；相同内容已存在则直接复用。"""
        self.ensure_dirs()
        sha256 = hashlib.sha256(data).hexdigest()
        final = self.blob_path(sha256)
        if os.path.isfile(final):
            return sha256
        os.makedirs(os.path.dirname(final), exist_ok=True)
        tmp_path = os.path.join(self.tmp_dir, f"{sha256}.{os.getpid()}.part")
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final)
        return sha256

    def has(self, sha256: str) -> bool:
        return os.path.isfile(self.blob_path(sha256))

    def read(self, sha256: str) -> bytes:
        with open(self.blob_path(sha256), "rb") as fh:
            return fh.read()

    def list_stored(self) -> list[str]:
        """返回当前磁盘上全部原件的 SHA-256（含未引用的残留）。"""
        hashes: list[str] = []
        if not os.path.isdir(self.blob_dir):
            return hashes
        for sub in os.listdir(self.blob_dir):
            subdir = os.path.join(self.blob_dir, sub)
            if not os.path.isdir(subdir):
                continue
            for name in os.listdir(subdir):
                if len(name) == 64 and all(
                    ch in "0123456789abcdef" for ch in name
                ):
                    hashes.append(name)
        return sorted(hashes)

    def sweep(self, keep: set[str]) -> list[str]:
        """删除所有不在 keep 中的原件与临时残留，返回被删文件路径。

        keep 之外的文件只能是「落盘后未提交」的残留；已提交投递引用
        的原件（keep 内）一律保留。
        """
        removed: list[str] = []
        for sha256 in self.list_stored():
            if sha256 not in keep:
                path = self.blob_path(sha256)
                os.remove(path)
                removed.append(path)
        # 空的分片目录一并清掉
        if os.path.isdir(self.blob_dir):
            for sub in os.listdir(self.blob_dir):
                subdir = os.path.join(self.blob_dir, sub)
                if os.path.isdir(subdir) and not os.listdir(subdir):
                    os.rmdir(subdir)
        # 写入中残留的临时文件
        if os.path.isdir(self.tmp_dir):
            for name in os.listdir(self.tmp_dir):
                path = os.path.join(self.tmp_dir, name)
                if os.path.isfile(path):
                    os.remove(path)
                    removed.append(path)
        return removed
