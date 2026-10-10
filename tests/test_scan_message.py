"""A manual scan that waited behind the start-up catch-up scan explains where the
new items came from, instead of a bare and confusing "0 new"."""

import time

from fastapi.testclient import TestClient

from nexus import scanstate


class _FakeCollector:
    def __init__(self, boot_new: int, boot_after_request: bool) -> None:
        self.boot_new = boot_new
        self.boot_after_request = boot_after_request
        self.last_boot_sync = {"new": 0, "finished": 0.0}

    def scan(self) -> dict:
        # Simulate the boot sync finishing while this scan waited for the lock.
        finished = time.time() if self.boot_after_request else time.time() - 3600
        self.last_boot_sync = {"new": self.boot_new, "finished": finished}
        return {"_update": {"new": 0}}


def _wait_done() -> dict:
    for _ in range(100):
        snap = scanstate.state()
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def test_reports_catch_up_items_added_while_waiting(temp_db):
    scanstate.start(_FakeCollector(boot_new=200, boot_after_request=True))
    snap = _wait_done()
    assert snap["new_items"] == 0 and snap["catchup_new"] == 200

    from nexus.web.app import app

    html = TestClient(app, base_url="http://127.0.0.1").get("/scan/status").text
    assert "start-up catch-up scan had just added 200" in html


def test_ignores_an_older_catch_up(temp_db):
    scanstate.start(_FakeCollector(boot_new=200, boot_after_request=False))
    snap = _wait_done()
    assert snap["catchup_new"] == 0
