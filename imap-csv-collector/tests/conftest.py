import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imap_server import mailbox, seed as seed_mod
from imap_server.server import ThreadedImapServer

USER = "tester@lab.local"
PASSWORD = "secret"


@pytest.fixture()
def mail_root(tmp_path):
    root = tmp_path / "mail"
    seed_mod.seed(str(root), "INBOX")
    return str(root)


@pytest.fixture()
def imap_server(mail_root):
    server = ThreadedImapServer(("127.0.0.1", 0), mail_root, USER, PASSWORD)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    time.sleep(0.1)
    yield host, port, mail_root
    server.shutdown()
    server.server_close()


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("COLLECTOR_DB", path)
    return path
