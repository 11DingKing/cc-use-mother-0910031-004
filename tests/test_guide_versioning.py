"""讲解内容版本发布：领域规则回归测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guide_versioning import Clock, ConflictError, GuideService, NotFoundError, RuleError, Store
from guide_versioning.api import build_server


def make_service(fixed_time: str = "2026-10-01T09:00:00+00:00") -> tuple[GuideService, Clock]:
    clock = Clock(fixed_time)
    service = GuideService(Store(":memory:"), clock)
    service.create_audience("kids", "少儿版", 6, 9)
    service.create_audience("teen", "青少年版", 10, 15)
    service.create_theme("bronze", "青铜器的一天")
    service.create_source("s1", "馆藏总目·青铜卷", author="编目组", year=2021)
    service.create_source("s2", "考古简报第 12 期", author="考古队", year=2019)
    return service, clock


def seed_v1(service: GuideService) -> int:
    """构造 1.0.0 并完成两级签署，返回 release_id。"""
    service.create_fragment("f-open", "bronze", "kids", "opening", "开场", "小朋友们好",
                            [{"source_code": "s1"}])
    service.create_fragment("f-main", "bronze", "kids", "section", "鼎是什么", "鼎是煮肉的锅",
                            [{"source_code": "s1"}, {"source_code": "s2", "note": "形制依据"}])
    service.set_outline_entries("bronze", "kids", ["f-open", "f-main"])
    rel = service.publish("bronze", "kids", "major")
    service.sign(rel["id"], "内容审核", "张编辑")
    service.sign(rel["id"], "场馆负责人", "李馆长")
    return rel["id"]


class PublishingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()

    def test_first_release_is_1_0_0_with_frozen_snapshot(self) -> None:
        rid = seed_v1(self.service)
        rel = self.service.get_release(rid)
        self.assertEqual(rel["version"], "1.0.0")
        self.assertEqual(rel["state"], "已确认")
        self.assertEqual(len(rel["snapshot"]["items"]), 2)
        self.assertEqual(len(rel["signatures"]), 2)
        self.assertEqual(rel["content_sha"], rid and rel["content_sha"])
        self.assertRegex(rel["content_sha"], r"^[0-9a-f]{64}$")

    def test_snapshot_is_immutable_after_later_edits(self) -> None:
        rid = seed_v1(self.service)
        sha_before = self.service.get_release(rid)["content_sha"]
        snapshot_body = self.service.get_release(rid)["snapshot"]["items"][1]["body"]
        # 发布后再改正文与追加修订，不影响已冻结快照。
        self.service.correct_fragment("f-main", {"body": "鼎也用于祭祀"})
        rel = self.service.get_release(rid)
        self.assertEqual(rel["content_sha"], sha_before)
        self.assertEqual(rel["snapshot"]["items"][1]["body"], snapshot_body)

    def test_duplicate_content_publish_rejected(self) -> None:
        seed_v1(self.service)
        with self.assertRaises(ConflictError):
            self.service.publish("bronze", "kids", "correction")

    def test_signing_must_follow_chain_order(self) -> None:
        self.service.create_fragment("f-open", "bronze", "kids", "opening", "开场", "好")
        self.service.set_outline_entries("bronze", "kids", ["f-open"])
        rel = self.service.publish("bronze", "kids", "major")
        with self.assertRaises(RuleError):  # 场馆负责人不能越过内容审核
            self.service.sign(rel["id"], "场馆负责人", "李馆长")
        self.service.sign(rel["id"], "内容审核", "张编辑")
        with self.assertRaises(ConflictError):  # 不得重复签署
            self.service.sign(rel["id"], "内容审核", "王编辑")
        self.service.sign(rel["id"], "场馆负责人", "李馆长")
        self.assertEqual(self.service.get_release(rel["id"])["state"], "已确认")

    def test_concurrent_signature_same_role_only_one_wins(self) -> None:
        self.service.create_fragment("f-open", "bronze", "kids", "opening", "开场", "好")
        self.service.set_outline_entries("bronze", "kids", ["f-open"])
        rel = self.service.publish("bronze", "kids", "major")
        results: list[str] = []

        def sign(editor: str) -> None:
            try:
                self.service.sign(rel["id"], "内容审核", editor)
                results.append(f"{editor}:ok")
            except ConflictError:
                results.append(f"{editor}:lost")

        threads = [threading.Thread(target=sign, args=(n,)) for n in ("张编辑", "王编辑", "赵编辑")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [r for r in results if r.endswith(":ok")]
        self.assertEqual(len(wins), 1, results)
        roles = {s["role"] for s in self.service.get_release(rel["id"])["signatures"]}
        self.assertEqual(roles, {"内容审核"})

    def test_optimistic_row_version_blocks_stale_sign(self) -> None:
        rid = seed_v1(self.service)
        self.service.correct_fragment("f-main", {"body": "新内容"})
        rel2 = self.service.publish("bronze", "kids", "correction")
        self.service.sign(rel2["id"], "内容审核", "张编辑")
        with self.assertRaises(ConflictError):
            self.service.sign(rel2["id"], "场馆负责人", "李馆长", expected_row_version=1)

    def test_correction_bumps_patch_and_appears_in_diff(self) -> None:
        rid1 = seed_v1(self.service)
        self.service.correct_fragment("f-main", {"body": "鼎是煮肉的锅，也是礼器"})
        rel2 = self.service.publish("bronze", "kids", "correction", reason="紧急纠错")
        self.assertEqual(rel2["version"], "1.0.1")
        self.assertEqual(rel2["parent_version"], "1.0.0")
        diff = self.service.diff_releases(rid1, rel2["id"])
        self.assertFalse(diff["identical"])
        self.assertEqual([c["fragment_code"] for c in diff["changed"]], ["f-main"])
        self.assertTrue(diff["changed"][0]["body_changed"])
        self.assertEqual(diff["changed"][0]["from_rev"], 1)
        self.assertEqual(diff["changed"][0]["to_rev"], 2)

    def test_replacement_bumps_minor_and_keeps_lineage(self) -> None:
        seed_v1(self.service)
        self.service.create_fragment("f-main-v2", "bronze", "kids", "section", "鼎是什么",
                                     "鼎不只是锅，更是身份的象征",
                                     [{"source_code": "s2"}], replaces_code="f-main")
        self.service.set_outline_entries("bronze", "kids", ["f-open", "f-main-v2"])
        rel = self.service.publish("bronze", "kids", "replacement", reason="片段替代")
        self.assertEqual(rel["version"], "1.1.0")
        lineage = self.service.get_fragment("f-main-v2")["lineage"]
        self.assertEqual(lineage, ["f-main", "f-main-v2"])
        self.assertEqual(self.service.get_fragment("f-main")["status"], "replaced")

    def test_version_chain_walks_parents(self) -> None:
        r1 = seed_v1(self.service)
        self.service.correct_fragment("f-main", {"body": "更正1"})
        r2 = self.service.publish("bronze", "kids", "correction")
        self.service.sign(r2["id"], "内容审核", "张编辑")
        self.service.sign(r2["id"], "场馆负责人", "李馆长")
        self.service.correct_fragment("f-open", {"body": "更正2"})
        r3 = self.service.publish("bronze", "kids", "correction")
        chain = self.service.release_chain("bronze", "kids")
        self.assertEqual([c["version"] for c in chain], ["1.0.2", "1.0.1", "1.0.0"])

    def test_withdraw_blocks_until_confirmed_then_stranded(self) -> None:
        rid = seed_v1(self.service)
        s1 = self.service.schedule_session("bronze", "kids", "2026-10-10T10:00:00+00:00", "第一展厅")
        # 有未来场次使用，撤回需显式确认
        with self.assertRaises(RuleError) as ctx:
            self.service.withdraw(rid, "事实错误")
        self.assertEqual(ctx.exception.code, "withdraw_needs_confirmation")
        affected = self.service.affected_sessions(rid)
        self.assertEqual([s["session_id"] for s in affected["future_sessions_using"]], [s1["id"]])
        self.service.withdraw(rid, "事实错误", force=True)
        self.assertEqual(self.service.get_release(rid)["state"], "已撤回")
        # 撤回后不能再排期（无可用版本）
        with self.assertRaises(RuleError):
            self.service.schedule_session("bronze", "kids", "2026-10-11T10:00:00+00:00")
        # 已撤回版本不能签署
        with self.assertRaises(RuleError):
            self.service.sign(rid, "内容审核", "张编辑")

    def test_completed_session_keeps_original_basis_after_correction(self) -> None:
        r1 = seed_v1(self.service)
        done = self.service.schedule_session("bronze", "kids", "2026-10-05T10:00:00+00:00")
        upcoming = self.service.schedule_session("bronze", "kids", "2026-10-20T10:00:00+00:00")

        self.clock.freeze("2026-10-05T11:00:00+00:00")
        self.service.complete_session(done["id"])
        done_after = self.service.get_session(done["id"])
        self.assertEqual(done_after["state"], "已完成")
        self.assertEqual(done_after["basis"]["release_id"], r1)

        # 紧急纠错并发布 1.0.1
        self.clock.freeze("2026-10-06T09:00:00+00:00")
        self.service.correct_fragment("f-main", {"body": "鼎是礼器"})
        r2 = self.service.publish("bronze", "kids", "correction")
        self.service.sign(r2["id"], "内容审核", "张编辑")
        self.service.sign(r2["id"], "场馆负责人", "李馆长")

        # 已完成场次依据不变，且不可被误改
        done_again = self.service.get_session(done["id"])
        self.assertEqual(done_again["basis"]["release_id"], r1)
        self.assertEqual(done_again["basis"]["version"], "1.0.0")
        with self.assertRaises(ConflictError):
            self.service.complete_session(done["id"])

        # 未来场次按规则自动改用新版
        upcoming_after = self.service.get_session(upcoming["id"])
        self.assertEqual(upcoming_after["effective_release_id"], r2["id"])
        affected = self.service.affected_sessions(r2["id"])
        self.assertEqual({s["session_id"] for s in affected["future_sessions_using"]},
                         {upcoming["id"]})
        self.assertEqual({s["session_id"] for s in affected["completed_sessions_on_this_basis"]},
                         set())  # 新版尚无完成场次
        affected_old = self.service.affected_sessions(r1)
        self.assertEqual({s["session_id"] for s in affected_old["completed_sessions_on_this_basis"]},
                         {done["id"]})

    def test_version_choice_uses_latest_confirmed_before_session_start(self) -> None:
        r1 = seed_v1(self.service)
        # 开讲时间早于确认时间的场次不会选到尚未确认的版本
        early = self.service.schedule_session("bronze", "kids", "2026-10-02T08:00:00+00:00")
        self.assertEqual(self.service.get_session(early["id"])["effective_release_id"], r1)
        self.service.correct_fragment("f-main", {"body": "更正"})
        r2 = self.service.publish("bronze", "kids", "correction")
        self.clock.freeze("2026-10-10T09:00:00+00:00")
        self.service.sign(r2["id"], "内容审核", "张编辑")
        self.service.sign(r2["id"], "场馆负责人", "李馆长")
        # 10-02 的场次确认于 10-10 之后才完成，仍按“开讲前最新已确认”冻结到 1.0.0
        self.clock.freeze("2026-10-02T09:00:00+00:00")
        completed = self.service.complete_session(early["id"])
        self.assertEqual(completed["basis"]["version"], "1.0.0")

    def test_archive_does_not_touch_completed_records(self) -> None:
        r1 = seed_v1(self.service)
        done = self.service.schedule_session("bronze", "kids", "2026-10-05T10:00:00+00:00")
        self.clock.freeze("2026-10-05T11:00:00+00:00")
        self.service.complete_session(done["id"])
        self.clock.freeze("2026-10-06T09:00:00+00:00")
        self.service.correct_fragment("f-main", {"body": "更正"})
        r2 = self.service.publish("bronze", "kids", "correction")
        self.service.sign(r2["id"], "内容审核", "张编辑")
        self.service.sign(r2["id"], "场馆负责人", "李馆长")
        n = self.service.archive_obsolete()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.service.get_release(r1)["state"], "已归档")
        # 完成记录的依据仍可查
        self.assertEqual(self.service.get_session(done["id"])["basis"]["release_id"], r1)

    def test_diff_reports_added_removed_and_replacement(self) -> None:
        r1 = seed_v1(self.service)
        self.service.create_fragment("f-close", "bronze", "kids", "closing", "结尾", "再见",
                                    [{"source_code": "s1"}])
        self.service.create_fragment("f-main-v2", "bronze", "kids", "section", "鼎", "新表述",
                                    [{"source_code": "s2"}], replaces_code="f-main")
        self.service.set_outline_entries("bronze", "kids", ["f-open", "f-main-v2", "f-close"])
        r2 = self.service.publish("bronze", "kids", "replacement")
        diff = self.service.diff_releases(r1, r2["id"])
        self.assertEqual([i["fragment_code"] for i in diff["added"]], ["f-close", "f-main-v2"])
        self.assertEqual([i["fragment_code"] for i in diff["removed"]], ["f-main"])
        self.assertEqual(diff["replacements"], [{"old_fragment": "f-main", "new_fragment": "f-main-v2"}])

    def test_other_audience_versions_are_independent(self) -> None:
        rid_kids = seed_v1(self.service)
        self.service.create_fragment("t-open", "bronze", "teen", "opening", "开场", "同学们好")
        self.service.set_outline_entries("bronze", "teen", ["t-open"])
        rel_teen = self.service.publish("bronze", "teen", "major")
        self.assertEqual(rel_teen["version"], "1.0.0")
        self.assertEqual(len(self.service.list_releases("bronze", "kids")), 1)
        self.assertEqual(self.service.list_releases("bronze", "kids")[0]["id"], rid_kids)


class ApiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        self.server = build_server("127.0.0.1", 0, self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        status, body = self._call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

        status, frag = self._call("POST", "/api/fragments", {
            "code": "f-open", "theme_code": "bronze", "audience_code": "kids",
            "kind": "opening", "title": "开场", "body": "小朋友们好",
            "source_refs": [{"source_code": "s1"}]})
        self.assertEqual(status, 201)

        status, outline = self._call("POST", "/api/outlines", {
            "theme_code": "bronze", "audience_code": "kids",
            "fragment_codes": ["f-open"]})
        self.assertEqual(status, 201)
        self.assertEqual(outline["entries"][0]["fragment"]["code"], "f-open")

        status, rel = self._call("POST", "/api/releases", {
            "theme_code": "bronze", "audience_code": "kids", "change_kind": "major"})
        self.assertEqual(status, 201)
        rid = rel["id"]

        status, rel = self._call("POST", f"/api/releases/{rid}/sign",
                                 {"role": "内容审核", "signer": "张编辑"})
        self.assertEqual(status, 200)
        status, rel = self._call("POST", f"/api/releases/{rid}/sign",
                                 {"role": "场馆负责人", "signer": "李馆长"})
        self.assertEqual(status, 200)
        self.assertEqual(rel["state"], "已确认")

        status, session = self._call("POST", "/api/sessions", {
            "theme_code": "bronze", "audience_code": "kids",
            "starts_at": "2026-10-20T10:00:00+00:00", "venue": "第一展厅"})
        self.assertEqual(status, 201)
        self.assertEqual(session["effective_release_id"], rid)

        status, affected = self._call("GET", f"/api/releases/{rid}/affected")
        self.assertEqual(status, 200)
        self.assertEqual(len(affected["future_sessions_using"]), 1)

        # 越级签署返回规则错误
        status, rel2 = self._call("POST", "/api/fragments/f-open/corrections",
                                  {"body": "大家好呀"})
        self.assertEqual(status, 200)
        status, new_rel = self._call("POST", "/api/releases", {
            "theme_code": "bronze", "audience_code": "kids", "change_kind": "correction"})
        self.assertEqual((status, new_rel["version"]), (201, "1.0.1"))
        status, body = self._call("POST", f"/api/releases/{new_rel['id']}/sign",
                                  {"role": "场馆负责人", "signer": "李馆长"})
        self.assertEqual(status, 422)

        status, diff = self._call("GET", f"/api/releases/{rid}/diff?other={new_rel['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(diff["to"]["version"], "1.0.1")


if __name__ == "__main__":
    unittest.main()
