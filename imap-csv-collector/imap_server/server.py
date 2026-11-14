"""最小可用的本地 IMAP 样例服务（仅实现采集客户端需要的命令子集）。

支持：CAPABILITY / NOOP / LOGIN / LOGOUT / SELECT / EXAMINE /
      STATUS (UIDVALIDITY) / UID SEARCH / UID FETCH (BODY.PEEK[])
邮件以 RFC822 原始字节存于文件邮箱（见 mailbox.py）。

环境变量 FLAKY_FETCH_AFTER=N 时，每条连接第 N 次 FETCH 发出响应后
直接断开 TCP，用于演示“断线停在该邮件、重启续采”。
"""
from __future__ import annotations

import os
import shlex
import socket
import socketserver
from typing import Optional

from . import mailbox

CRLF = b"\r\n"


class ImapConnection:
    def __init__(self, conn: socket.socket, root: str, username: str, password: str):
        self.conn = conn
        self.root = root
        self.username = username
        self.password = password
        self.authed = False
        self.selected: Optional[str] = None
        self.fetch_count = 0
        self.flaky_after = int(os.environ.get("FLAKY_FETCH_AFTER", "0") or 0)
        self.fobj = conn.makefile("rb")

    # ---------- 底层收发 ----------
    def send_line(self, line: str) -> None:
        self.conn.sendall(line.encode("utf-8") + CRLF)

    def send_literal_response(self, header: str, payload: bytes, tail: str) -> None:
        self.conn.sendall(header.encode("utf-8"))
        self.conn.sendall(payload)
        self.conn.sendall(tail.encode("utf-8"))

    def read_line(self) -> str:
        line = self.fobj.readline()
        if not line:
            raise ConnectionError("客户端关闭连接")
        return line.rstrip(CRLF).decode("utf-8", errors="replace")

    def read_literal(self, size: int) -> bytes:
        chunk = self.fobj.read(size)
        if len(chunk) != size:
            raise ConnectionError("字面量读取不完整")
        # 吃掉字面量后的 CRLF
        self.fobj.read(2)
        return chunk

    def close(self) -> None:
        try:
            self.fobj.close()
        finally:
            self.conn.close()

    # ---------- 命令循环 ----------
    def serve(self) -> None:
        self.send_line("* OK local sample IMAP4rev1 service ready")
        while True:
            line = self.read_line()
            if not line:
                continue
            try:
                args = shlex.split(line)
            except ValueError:
                args = line.split()
            if not args:
                continue

            # 客户端同步字面量：行以 {n} 结尾时先续读
            literal_size = self._trailing_literal(line)
            if literal_size is not None:
                tag, cmd = args[0], args[1].upper()
                self.send_line("+ continue")
                literal = self.read_literal(literal_size)
                rest = self.read_line()
                rest_args = shlex.split(rest) if rest.strip() else []
                args = args[:-1] + [literal.decode("utf-8", "replace")] + rest_args
            else:
                tag, cmd = args[0], args[1].upper()
                args = args[2:]

            if cmd == "CAPABILITY":
                self.send_line(
                    "* CAPABILITY IMAP4rev1 AUTH=PLAIN UIDPLUS "
                    "LITERAL+ UNSELECT"
                )
                self.send_line(f"{tag} OK CAPABILITY completed")
            elif cmd == "NOOP":
                self.send_line(f"{tag} OK NOOP completed")
            elif cmd == "LOGIN":
                self._handle_login(tag, args)
            elif cmd in ("SELECT", "EXAMINE"):
                self._handle_select(tag, args)
            elif cmd == "STATUS":
                self._handle_status(tag, args)
            elif cmd == "UID":
                self._handle_uid(tag, args)
            elif cmd == "LOGOUT":
                self.send_line("* BYE logging out")
                self.send_line(f"{tag} OK LOGOUT completed")
                return
            else:
                self.send_line(f"{tag} BAD 未支持的命令: {cmd}")

    @staticmethod
    def _trailing_literal(line: str) -> Optional[int]:
        stripped = line.rstrip()
        if stripped.endswith("}") and "{" in stripped:
            size_token = stripped[stripped.rfind("{") + 1 : -1]
            if size_token.isdigit():
                return int(size_token)
        return None

    # ---------- 各命令 ----------
    def _handle_login(self, tag: str, args: list[str]) -> None:
        if len(args) < 2:
            self.send_line(f"{tag} BAD LOGIN 参数不足")
            return
        user, password = args[0], args[1]
        if user == self.username and password == self.password:
            self.authed = True
            self.send_line(f"{tag} OK LOGIN completed")
        else:
            self.send_line(f"{tag} NO 登录失败：用户名或密码错误")

    def _require_auth(self, tag: str) -> bool:
        if not self.authed:
            self.send_line(f"{tag} NO 请先 LOGIN")
            return False
        return True

    def _handle_select(self, tag: str, args: list[str]) -> None:
        if not self._require_auth(tag):
            return
        if not args:
            self.send_line(f"{tag} BAD 缺少文件夹参数")
            return
        folder = args[0]
        if not mailbox.folder_exists(self.root, folder):
            self.send_line(f"{tag} NO 文件夹不存在: {folder}")
            return
        meta = mailbox.read_meta(self.root, folder)
        uids = mailbox.list_uids(self.root, folder)
        self.selected = folder
        self.send_line(f"* {len(uids)} EXISTS")
        self.send_line("* 0 RECENT")
        self.send_line("* FLAGS (\\Seen)")
        self.send_line('* OK [PERMANENTFLAGS (\\Seen)] limited')
        self.send_line(f"* OK [UIDVALIDITY {meta['uidvalidity']}] ok")
        self.send_line(f"* OK [UIDNEXT {meta['next_uid']}] ok")
        self.send_line(f"{tag} OK [READ-ONLY] SELECT completed")

    def _handle_status(self, tag: str, args: list[str]) -> None:
        if not self._require_auth(tag):
            return
        if len(args) < 2:
            self.send_line(f"{tag} BAD STATUS 参数不足")
            return
        folder, item_blob = args[0], " ".join(args[1:]).strip("()")
        if not mailbox.folder_exists(self.root, folder):
            self.send_line(f"{tag} NO 文件夹不存在: {folder}")
            return
        meta = mailbox.read_meta(self.root, folder)
        items = []
        if "UIDVALIDITY" in item_blob.upper():
            items.append(f"UIDVALIDITY {meta['uidvalidity']}")
        if "MESSAGES" in item_blob.upper():
            items.append(f"MESSAGES {len(mailbox.list_uids(self.root, folder))}")
        if "UIDNEXT" in item_blob.upper():
            items.append(f"UIDNEXT {meta['next_uid']}")
        self.send_line(f"* STATUS {folder} ({' '.join(items)})")
        self.send_line(f"{tag} OK STATUS completed")

    def _handle_uid(self, tag: str, args: list[str]) -> None:
        if not self._require_auth(tag):
            return
        if not args:
            self.send_line(f"{tag} BAD UID 缺少子命令")
            return
        sub = args[0].upper()
        if sub == "SEARCH":
            self._handle_uid_search(tag, args[1:])
        elif sub == "FETCH":
            self._handle_uid_fetch(tag, args[1:])
        else:
            self.send_line(f"{tag} BAD 未支持的 UID 子命令: {sub}")

    def _handle_uid_search(self, tag: str, args: list[str]) -> None:
        if self.selected is None:
            self.send_line(f"{tag} NO 请先 SELECT 文件夹")
            return
        uids = mailbox.list_uids(self.root, self.selected)
        criteria = [a.upper() for a in args]
        if criteria and criteria[0] != "ALL":
            # 解析 UID n:m / n:* 形式
            range_token = next(
                (a for a in args if ":" in a or a.isdigit()), None
            )
            if range_token is None:
                self.send_line("* SEARCH")
                self.send_line(f"{tag} OK UID SEARCH completed")
                return
            start_text, end_text = range_token.split(":", 1)
            start = int(start_text)
            end = max(uids) if end_text == "*" else int(end_text)
            uids = [u for u in uids if start <= u <= end]
        self.send_line("* SEARCH " + " ".join(str(u) for u in uids))
        self.send_line(f"{tag} OK UID SEARCH completed")

    def _handle_uid_fetch(self, tag: str, args: list[str]) -> None:
        if self.selected is None:
            self.send_line(f"{tag} NO 请先 SELECT 文件夹")
            return
        if not args:
            self.send_line(f"{tag} BAD FETCH 缺少参数")
            return
        wanted = self._expand_uid_set(args[0])
        for uid in wanted:
            raw = mailbox.read_message(self.root, self.selected, uid)
            if raw is None:
                continue
            size = len(raw)
            self.fetch_count += 1
            if self.flaky_after and self.fetch_count >= self.flaky_after:
                # 模拟断线：响应只发一半就掐断 TCP，客户端读取必然失败
                header = f"* {uid} FETCH (UID {uid} BODY[] {{{size}}}\r\n"
                self.conn.sendall(header.encode("utf-8"))
                self.conn.sendall(raw[: max(1, size // 2)])
                self.conn.shutdown(socket.SHUT_RDWR)
                return
            self.send_literal_response(
                f"* {uid} FETCH (UID {uid} BODY[] {{{size}}}\r\n",
                raw,
                ")\r\n",
            )
        self.send_line(f"{tag} OK UID FETCH completed")

    def _expand_uid_set(self, token: str) -> list[int]:
        available = mailbox.list_uids(self.root, self.selected)
        if token == "*":
            return available[-1:] if available else []
        result: list[int] = []
        for part in token.split(","):
            if ":" in part:
                start_text, end_text = part.split(":", 1)
                start = int(start_text)
                end = max(available) if end_text == "*" else int(end_text)
                result.extend(u for u in available if start <= u <= end)
            elif part.isdigit():
                uid = int(part)
                if uid in available:
                    result.append(uid)
        return result


class ThreadedImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address,
        root: str,
        username: str,
        password: str,
        flaky_fetch_after: int = 0,
    ):
        self.root = root
        self.username = username
        self.password = password
        self.flaky_fetch_after = flaky_fetch_after
        super().__init__(server_address, ImapHandler)


class ImapHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: ThreadedImapServer = self.server  # type: ignore[assignment]
        env_value = int(os.environ.get("FLAKY_FETCH_AFTER", "0") or 0)
        flaky = env_value or server.flaky_fetch_after
        connection = ImapConnection(
            self.request, server.root, server.username, server.password
        )
        connection.flaky_after = flaky
        try:
            connection.serve()
        except (ConnectionError, OSError):
            return
        finally:
            connection.close()


def serve(host: str, port: int, root: str, username: str, password: str) -> None:
    with ThreadedImapServer((host, port), root, username, password) as server:
        print(f"IMAP 样例服务监听 {host}:{port}，邮箱根目录 {root}", flush=True)
        server.serve_forever()
