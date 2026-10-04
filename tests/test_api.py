"""HTTP API 端到端测试：完整发布流程、版本差异、场次依据与错误形态。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from narration.api import make_server
from narration.service import NarrationService
from narration.store import Store


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = NarrationService(Store(":memory:"))
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8"))

    def test_health(self) -> None:
        status, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_full_publish_flow(self) -> None:
        status, theme = self.request("POST", "/api/themes", {"name": "青铜文明", "actor": "op"})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/audiences", {
            "code": "junior", "name": "少儿 6-12 岁", "min_age": 6, "max_age": 12, "actor": "op",
        })
        self.assertEqual(status, 201)
        status, source = self.request("POST", "/api/sources", {
            "title": "馆藏青铜器图录", "publisher": "文博出版社", "actor": "op",
        })
        self.assertEqual(status, 201)
        status, fragment = self.request("POST", "/api/fragments", {
            "theme_id": theme["id"], "audience_code": "junior",
            "title": "青铜器的铸造", "body": "介绍块范法铸造流程。",
            "source_ids": [source["id"]], "actor": "editor",
        })
        self.assertEqual(status, 201)

        status, draft = self.request("POST", "/api/drafts", {
            "theme_id": theme["id"], "audience_code": "junior",
            "fragment_ids": [fragment["id"]], "actor": "editor",
        })
        self.assertEqual(status, 201)
        self.assertFalse(draft["publishable"])

        # 缺一级签署时发布被拒绝
        status, payload = self.request("POST", f"/api/drafts/{draft['id']}/signoffs",
                                       {"role": "文博中心运营员", "signer": "op1"})
        self.assertEqual(status, 200)
        status, payload = self.request("POST", f"/api/drafts/{draft['id']}/publish", {"actor": "editor"})
        self.assertEqual(status, 422)
        self.assertIn("场馆负责人", payload["error"]["message"])

        # 重复签署冲突
        status, payload = self.request("POST", f"/api/drafts/{draft['id']}/signoffs",
                                       {"role": "文博中心运营员", "signer": "op2"})
        self.assertEqual(status, 409)

        status, _ = self.request("POST", f"/api/drafts/{draft['id']}/signoffs",
                                 {"role": "场馆负责人", "signer": "boss"})
        self.assertEqual(status, 200)
        status, v1 = self.request("POST", f"/api/drafts/{draft['id']}/publish", {"actor": "editor"})
        self.assertEqual(status, 201)
        self.assertEqual(v1["label"], "v1")
        self.assertTrue(v1["fingerprint"])

        # 发布后冻结：局部更正不影响 v1 快照
        status, revised = self.request("POST", f"/api/fragments/{fragment['id']}/revise", {
            "mode": "correction", "body": "更正：补充失蜡法。", "note": "纠错", "actor": "editor",
        })
        self.assertEqual(status, 201)
        status, frozen = self.request("GET", f"/api/versions/{v1['id']}")
        self.assertEqual(frozen["items"][0]["body"], "介绍块范法铸造流程。")
        self.assertEqual(frozen["items"][0]["lineage_status"], "superseded")

        # 发布 v2 并比较差异
        status, draft2 = self.request("POST", "/api/drafts", {
            "theme_id": theme["id"], "audience_code": "junior",
            "fragment_ids": [revised["id"]], "actor": "editor",
        })
        for role, signer in (("文博中心运营员", "op1"), ("场馆负责人", "boss")):
            self.request("POST", f"/api/drafts/{draft2['id']}/signoffs", {"role": role, "signer": signer})
        status, v2 = self.request("POST", f"/api/drafts/{draft2['id']}/publish", {"actor": "editor"})
        self.assertEqual(status, 201)
        self.assertEqual(v2["parent_id"], v1["id"])

        status, diff = self.request("GET", f"/api/diff?from={v1['id']}&to={v2['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(diff["summary"]["changed"], 1)

        # 场次：已排按规则选版，完成后冻结依据
        status, session = self.request("POST", "/api/sessions", {
            "theme_id": theme["id"], "audience_code": "junior",
            "title": "周末亲子场", "scheduled_start": "2099-05-01T10:00:00+00:00", "actor": "op",
        })
        self.assertEqual(status, 201)
        self.assertEqual(session["resolution"]["version_id"], v2["id"])

        status, impact = self.request("GET", f"/api/versions/{v2['id']}/impact")
        self.assertEqual(status, 200)
        self.assertEqual(impact["affected_count"], 1)
        self.assertEqual(impact["affected_sessions"][0]["session_id"], session["id"])

        status, done = self.request("POST", f"/api/sessions/{session['id']}/complete", {"actor": "op"})
        self.assertEqual(status, 200)
        self.assertEqual(done["completed_version_id"], v2["id"])

        # 紧急纠错撤回 v2：已完成场次依据不变
        status, withdrawn = self.request("POST", f"/api/versions/{v2['id']}/withdraw", {
            "reason": "发现事实性错误", "actor": "boss",
        })
        self.assertEqual(status, 200)
        self.assertEqual(withdrawn["status"], "withdrawn")
        status, after = self.request("GET", f"/api/sessions/{session['id']}")
        self.assertEqual(after["resolution"]["version_id"], v2["id"])
        self.assertEqual(after["basis"]["label"], "v2")

        status, audit = self.request("GET", "/api/audit?limit=50")
        self.assertEqual(status, 200)
        actions = [event["action"] for event in audit["events"]]
        self.assertIn("version.withdrawn", actions)

    def test_error_shapes(self) -> None:
        status, payload = self.request("GET", "/api/versions/ver_00000000")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

        status, payload = self.request("GET", "/api/nonexistent")
        self.assertEqual(status, 404)

        status, payload = self.request("POST", "/api/themes", {"name": "  "})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "validation")

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/themes", body="{not json", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["code"], "bad_json")


if __name__ == "__main__":
    unittest.main()
