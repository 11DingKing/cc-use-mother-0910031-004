"""并发签署、并发发布与并发完成场次的确定性测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from narration.errors import DomainError
from narration.service import NarrationService
from narration.store import Store


def race(count: int, fn) -> list:
    """count 个线程同时执行 fn(i)，收集 ("ok", 结果) 或 (错误码, 消息)。"""
    barrier = threading.Barrier(count)
    results: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        try:
            value = fn(i)
            with lock:
                results.append(("ok", value))
        except DomainError as exc:
            with lock:
                results.append((exc.code, str(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    return results


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = NarrationService(Store(":memory:"))
        self.theme = self.service.create_theme(name="青铜文明", actor="op")
        self.service.create_audience(code="junior", name="少儿 6-12 岁", min_age=6, max_age=12, actor="op")
        self.fragment = self.service.create_fragment(
            theme_id=self.theme["id"], audience_code="junior",
            title="青铜器的铸造", body="介绍块范法铸造流程。", source_ids=[], actor="editor",
        )
        self.draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="junior",
            fragment_ids=[self.fragment["id"]], actor="editor",
        )

    def test_concurrent_signoffs_same_role_exactly_one_wins(self) -> None:
        results = race(5, lambda i: self.service.sign_draft(
            self.draft["id"], role="文博中心运营员", signer=f"op{i}"
        ))
        ok = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(conflicts), 4)
        draft = self.service.get_draft(self.draft["id"])
        self.assertEqual(len(draft["signoffs"]), 1)

    def test_concurrent_signoffs_distinct_roles_both_recorded(self) -> None:
        roles = ["文博中心运营员", "场馆负责人"]
        results = race(2, lambda i: self.service.sign_draft(
            self.draft["id"], role=roles[i], signer=f"signer{i}"
        ))
        self.assertEqual([r[0] for r in results], ["ok", "ok"])
        draft = self.service.get_draft(self.draft["id"])
        self.assertEqual(draft["missing_roles"], [])
        self.assertTrue(draft["publishable"])

    def test_concurrent_publish_exactly_one_version(self) -> None:
        self.service.sign_draft(self.draft["id"], role="文博中心运营员", signer="op1")
        self.service.sign_draft(self.draft["id"], role="场馆负责人", signer="boss")
        results = race(3, lambda i: self.service.publish_draft(self.draft["id"], actor=f"pub{i}"))
        ok = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(conflicts), 2)
        versions = self.service.list_versions(theme_id=self.theme["id"], audience_code="junior")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["label"], "v1")

    def test_concurrent_complete_exactly_one_wins(self) -> None:
        self.service.sign_draft(self.draft["id"], role="文博中心运营员", signer="op1")
        self.service.sign_draft(self.draft["id"], role="场馆负责人", signer="boss")
        version = self.service.publish_draft(self.draft["id"], actor="editor")
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2099-01-01T12:00:00+00:00", actor="op",
        )
        results = race(4, lambda i: self.service.complete_session(session["id"], actor=f"op{i}"))
        ok = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(conflicts), 3)
        done = self.service.get_session(session["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["completed_version_id"], version["id"])


if __name__ == "__main__":
    unittest.main()
