"""领域规则回归测试：片段血缘、签署、发布冻结、版本链、场次解析与影响分析。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from narration.errors import ConflictError, NotFoundError, ValidationError
from narration.service import NarrationService
from narration.store import Store


class FakeClock:
    def __init__(self, start: datetime):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
        self.service = NarrationService(Store(":memory:"), clock=self.clock)
        self.theme = self.service.create_theme(name="青铜文明", actor="op")
        self.service.create_audience(code="junior", name="少儿 6-12 岁", min_age=6, max_age=12, actor="op")
        self.service.create_audience(code="teen", name="青少年 13-15 岁", min_age=13, max_age=15, actor="op")
        self.src1 = self.service.create_source(title="馆藏青铜器图录", publisher="文博出版社", actor="op")
        self.src2 = self.service.create_source(title="考古发掘报告", publisher="省考古院", actor="op")

    # ------------------------------------------------------------------
    def _fragment(self, title="青铜器的铸造", body="介绍块范法铸造流程。", source_ids=None):
        return self.service.create_fragment(
            theme_id=self.theme["id"], audience_code="junior",
            title=title, body=body,
            source_ids=source_ids if source_ids is not None else [self.src1["id"]],
            actor="editor",
        )

    def _signed_draft(self, fragment_ids):
        draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="junior",
            fragment_ids=fragment_ids, actor="editor",
        )
        self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op1")
        self.service.sign_draft(draft["id"], role="场馆负责人", signer="boss")
        return draft

    def _publish(self, fragment_ids):
        draft = self._signed_draft(fragment_ids)
        return self.service.publish_draft(draft["id"], actor="editor")

    # ------------------------------------------------------------------
    # 片段血缘：更正 / 替代 / 撤回
    # ------------------------------------------------------------------
    def test_fragment_revision_chain(self) -> None:
        f1 = self._fragment()
        f2 = self.service.revise_fragment(
            f1["id"], mode="correction", actor="editor",
            body="介绍块范法与失蜡法铸造流程。", note="更正：补充失蜡法",
        )
        f3 = self.service.revise_fragment(
            f2["id"], mode="replacement", actor="editor",
            title="青铜器铸造工艺", body="重写：从矿料到成器的完整流程。",
        )
        self.assertEqual(self.service.get_fragment(f1["id"])["status"], "superseded")
        self.assertEqual(self.service.get_fragment(f2["id"])["status"], "superseded")
        head = self.service.get_fragment(f3["id"])
        self.assertEqual(head["status"], "active")
        self.assertTrue(head["is_head"])
        kinds = [item["change_kind"] for item in head["revision_chain"]]
        self.assertEqual(kinds, ["original", "correction", "replacement"])
        self.assertEqual(head["revision"], 3)
        with self.assertRaises(ConflictError):
            self.service.revise_fragment(f1["id"], mode="correction", actor="editor", body="x", note="旧修订")

    def test_correction_requires_note(self) -> None:
        f1 = self._fragment()
        with self.assertRaises(ValidationError):
            self.service.revise_fragment(f1["id"], mode="correction", actor="editor", body="改动")

    def test_replacement_requires_full_content(self) -> None:
        f1 = self._fragment()
        with self.assertRaises(ValidationError):
            self.service.revise_fragment(f1["id"], mode="replacement", actor="editor", title="只有标题")

    def test_withdraw_fragment_terminates_lineage(self) -> None:
        f1 = self._fragment()
        self.service.withdraw_fragment(f1["id"], reason="内容失实，紧急撤回", actor="boss")
        self.assertEqual(self.service.get_fragment(f1["id"])["status"], "withdrawn")
        with self.assertRaises(ConflictError):
            self.service.withdraw_fragment(f1["id"], reason="重复撤回", actor="boss")
        with self.assertRaises(ConflictError):
            self.service.revise_fragment(f1["id"], mode="correction", actor="editor", body="x", note="已撤回")

    # ------------------------------------------------------------------
    # 签署与发布
    # ------------------------------------------------------------------
    def test_publish_requires_all_signoffs(self) -> None:
        f1 = self._fragment()
        draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="junior", fragment_ids=[f1["id"]], actor="editor"
        )
        self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op1")
        with self.assertRaises(ValidationError) as ctx:
            self.service.publish_draft(draft["id"], actor="editor")
        self.assertIn("场馆负责人", str(ctx.exception))

    def test_signoff_invalidated_on_content_change(self) -> None:
        f1, f2 = self._fragment(), self._fragment(title="青铜纹饰", body="介绍饕餮纹。")
        draft = self._signed_draft([f1["id"]])
        self.service.replace_draft_items(draft["id"], fragment_ids=[f1["id"], f2["id"]], actor="editor")
        state = self.service.get_draft(draft["id"])
        self.assertEqual(state["content_seq"], 2)
        self.assertEqual(state["signoffs"], [])
        self.assertEqual(state["missing_roles"], ["文博中心运营员", "场馆负责人"])
        with self.assertRaises(ValidationError):
            self.service.publish_draft(draft["id"], actor="editor")

    def test_duplicate_signoff_conflict_and_idempotent(self) -> None:
        f1 = self._fragment()
        draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="junior", fragment_ids=[f1["id"]], actor="editor"
        )
        self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op1")
        again = self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op1")
        self.assertEqual(len(again["signoffs"]), 1)
        with self.assertRaises(ConflictError):
            self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op2")

    def test_unknown_signoff_role_rejected(self) -> None:
        f1 = self._fragment()
        draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="junior", fragment_ids=[f1["id"]], actor="editor"
        )
        with self.assertRaises(ValidationError):
            self.service.sign_draft(draft["id"], role="志愿者", signer="v1")

    def test_publish_freezes_snapshot(self) -> None:
        f1 = self._fragment()
        v1 = self._publish([f1["id"]])
        self.service.revise_fragment(
            f1["id"], mode="correction", actor="editor", body="更正后的正文。", note="纠错"
        )
        snapshot = self.service.get_version(v1["id"])
        self.assertEqual(snapshot["items"][0]["body"], "介绍块范法铸造流程。")
        self.assertEqual(snapshot["items"][0]["lineage_status"], "superseded")
        self.assertEqual(snapshot["freshness"]["stale_item_count"], 1)
        self.assertEqual(snapshot["sources"][0]["title"], "馆藏青铜器图录")

    def test_version_chain(self) -> None:
        f1 = self._fragment()
        v1 = self._publish([f1["id"]])
        f2 = self.service.revise_fragment(
            f1["id"], mode="correction", actor="editor", body="更正后的正文。", note="纠错"
        )
        v2 = self._publish([f2["id"]])
        self.assertEqual(v1["label"], "v1")
        self.assertEqual(v2["label"], "v2")
        self.assertEqual(v2["parent_id"], v1["id"])
        self.assertNotEqual(v1["fingerprint"], v2["fingerprint"])
        chain = self.service.list_versions(theme_id=self.theme["id"], audience_code="junior")
        self.assertEqual([v["label"] for v in chain], ["v1", "v2"])

    def test_publish_rejects_stale_fragment(self) -> None:
        f1 = self._fragment()
        draft = self._signed_draft([f1["id"]])
        self.service.withdraw_fragment(f1["id"], reason="紧急纠错", actor="boss")
        with self.assertRaises(ConflictError):
            self.service.publish_draft(draft["id"], actor="editor")

    def test_draft_rejects_inactive_fragment(self) -> None:
        f1 = self._fragment()
        self.service.withdraw_fragment(f1["id"], reason="撤回", actor="boss")
        with self.assertRaises(ConflictError):
            self.service.create_draft(
                theme_id=self.theme["id"], audience_code="junior", fragment_ids=[f1["id"]], actor="editor"
            )

    def test_double_publish_conflict(self) -> None:
        f1 = self._fragment()
        draft = self._signed_draft([f1["id"]])
        self.service.publish_draft(draft["id"], actor="editor")
        with self.assertRaises(ConflictError):
            self.service.publish_draft(draft["id"], actor="editor")

    # ------------------------------------------------------------------
    # 场次版本解析
    # ------------------------------------------------------------------
    def _setup_two_versions(self):
        f1 = self._fragment()
        self.clock.advance(hours=1)  # 09:00 发布 v1
        v1 = self._publish([f1["id"]])
        f2 = self.service.revise_fragment(
            f1["id"], mode="correction", actor="editor", body="更正后的正文。", note="纠错"
        )
        self.clock.advance(hours=1)  # 10:00 发布 v2
        v2 = self._publish([f2["id"]])
        return v1, v2

    def test_session_resolution_latest_before_start(self) -> None:
        v1, v2 = self._setup_two_versions()
        later = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        self.assertEqual(later["resolution"]["version_id"], v2["id"])
        self.assertEqual(later["resolution"]["rule"], "latest_before_start")
        early = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="上午场", scheduled_start="2026-03-01T09:30:00+00:00", actor="op",
        )
        self.assertEqual(early["resolution"]["version_id"], v1["id"])

    def test_session_unresolved_before_any_version(self) -> None:
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="清晨场", scheduled_start="2026-03-01T07:00:00+00:00", actor="op",
        )
        self.assertIsNone(session["resolution"]["version_id"])
        self.assertEqual(session["resolution"]["rule"], "unresolved")
        with self.assertRaises(ValidationError):
            self.service.complete_session(session["id"], actor="op")

    def test_withdraw_version_falls_back(self) -> None:
        v1, v2 = self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        self.assertEqual(session["resolution"]["version_id"], v2["id"])
        self.service.withdraw_version(v2["id"], reason="发现事实性错误", actor="boss")
        resolved = self.service.get_session(session["id"])
        self.assertEqual(resolved["resolution"]["version_id"], v1["id"])

    def test_completed_session_keeps_basis(self) -> None:
        v1, v2 = self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        done = self.service.complete_session(session["id"], actor="op")
        self.assertEqual(done["completed_version_id"], v2["id"])
        # 紧急纠错：撤回 v2 并发布 v3，已完成场次依据不变
        self.service.withdraw_version(v2["id"], reason="内容失实", actor="boss")
        f3 = self._fragment(title="青铜器铸造（修订版）", body="第三次修订。")
        self.clock.advance(hours=1)
        self._publish([f3["id"]])
        after = self.service.get_session(session["id"])
        self.assertEqual(after["resolution"]["version_id"], v2["id"])
        self.assertEqual(after["resolution"]["rule"], "completed_frozen")
        self.assertEqual(after["basis"]["label"], "v2")
        self.assertEqual(after["basis"]["version_status"], "withdrawn")

    def test_complete_with_explicit_version(self) -> None:
        v1, v2 = self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        done = self.service.complete_session(session["id"], actor="op", version_id=v1["id"])
        self.assertEqual(done["completed_version_id"], v1["id"])

    def test_double_complete_and_cancel_conflicts(self) -> None:
        self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        self.service.complete_session(session["id"], actor="op")
        with self.assertRaises(ConflictError):
            self.service.complete_session(session["id"], actor="op")
        with self.assertRaises(ConflictError):
            self.service.cancel_session(session["id"], actor="op")

    def test_pinned_session(self) -> None:
        v1, v2 = self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="指定场次", scheduled_start="2026-03-01T12:00:00+00:00",
            pin_version_id=v1["id"], actor="op",
        )
        self.assertEqual(session["resolution"]["version_id"], v1["id"])
        self.assertEqual(session["resolution"]["rule"], "pinned")
        self.service.withdraw_version(v1["id"], reason="撤回旧版", actor="boss")
        resolved = self.service.get_session(session["id"])
        self.assertEqual(resolved["resolution"]["version_id"], v2["id"])
        self.assertIsNotNone(resolved["resolution"]["note"])
        with self.assertRaises(ValidationError):
            self.service.create_session(
                theme_id=self.theme["id"], audience_code="junior",
                title="非法指定", scheduled_start="2026-03-01T13:00:00+00:00",
                pin_version_id=v1["id"], actor="op",
            )

    # ------------------------------------------------------------------
    # 影响范围与差异比较
    # ------------------------------------------------------------------
    def test_impact_on_publish_and_withdraw(self) -> None:
        v1, v2 = self._setup_two_versions()
        future = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="中午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        early = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="上午场", scheduled_start="2026-03-01T09:30:00+00:00", actor="op",
        )
        impact = self.service.version_impact(v2["id"])
        self.assertEqual(impact["affected_count"], 1)
        hit = impact["affected_sessions"][0]
        self.assertEqual(hit["session_id"], future["id"])
        self.assertEqual(hit["previous_version_id"], v1["id"])
        self.assertEqual(hit["current_version_id"], v2["id"])

        self.service.withdraw_version(v2["id"], reason="紧急纠错", actor="boss")
        impact = self.service.version_impact(v2["id"])
        self.assertEqual(impact["affected_count"], 1)
        hit = impact["affected_sessions"][0]
        self.assertEqual(hit["previous_version_id"], v2["id"])
        self.assertEqual(hit["current_version_id"], v1["id"])
        # 已完成场次不受影响
        self.service.complete_session(early["id"], actor="op")
        impact = self.service.version_impact(v1["id"])
        self.assertNotIn(early["id"], [s["session_id"] for s in impact["affected_sessions"]])

    def test_diff_versions(self) -> None:
        f1 = self._fragment()
        f2 = self._fragment(title="青铜纹饰", body="介绍饕餮纹。")
        v1 = self._publish([f1["id"], f2["id"]])
        f1b = self.service.revise_fragment(
            f1["id"], mode="correction", actor="editor",
            body="更正：块范法细节。", note="纠错并更换引用",
            source_ids=[self.src2["id"]],
        )
        f3 = self._fragment(title="青铜礼器制度", body="列鼎制度。", source_ids=[self.src2["id"]])
        v2 = self._publish([f1b["id"], f3["id"]])

        diff = self.service.diff_versions(v1["id"], v2["id"])
        self.assertEqual(diff["summary"]["changed"], 1)
        self.assertEqual(diff["summary"]["added"], 1)
        self.assertEqual(diff["summary"]["removed"], 1)
        self.assertEqual(diff["fragments"]["changed"][0]["lineage_id"], f1["lineage_id"])
        self.assertEqual(diff["fragments"]["changed"][0]["to"]["fragment_id"], f1b["id"])
        self.assertEqual(diff["fragments"]["added"][0]["lineage_id"], f3["lineage_id"])
        self.assertEqual(diff["fragments"]["removed"][0]["lineage_id"], f2["lineage_id"])
        self.assertEqual(diff["sources"]["added"][0]["source_id"], self.src2["id"])
        self.assertEqual(diff["sources"]["removed"][0]["source_id"], self.src1["id"])

    def test_diff_rejects_cross_outline(self) -> None:
        f1 = self._fragment()
        v1 = self._publish([f1["id"]])
        teen_fragment = self.service.create_fragment(
            theme_id=self.theme["id"], audience_code="teen",
            title="青铜与礼制", body="面向青少年的内容。", source_ids=[], actor="editor",
        )
        draft = self.service.create_draft(
            theme_id=self.theme["id"], audience_code="teen",
            fragment_ids=[teen_fragment["id"]], actor="editor",
        )
        self.service.sign_draft(draft["id"], role="文博中心运营员", signer="op1")
        self.service.sign_draft(draft["id"], role="场馆负责人", signer="boss")
        v2 = self.service.publish_draft(draft["id"], actor="editor")
        with self.assertRaises(ValidationError):
            self.service.diff_versions(v1["id"], v2["id"])

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def test_audit_trail(self) -> None:
        v1, _ = self._setup_two_versions()
        session = self.service.create_session(
            theme_id=self.theme["id"], audience_code="junior",
            title="下午场", scheduled_start="2026-03-01T12:00:00+00:00", actor="op",
        )
        self.service.complete_session(session["id"], actor="op")
        actions = [event["action"] for event in self.service.audit_log()]
        for expected in ("version.published", "session.created", "session.completed", "draft.signed"):
            self.assertIn(expected, actions)
        completed = [e for e in self.service.audit_log() if e["action"] == "session.completed"][0]
        self.assertEqual(completed["detail"]["version_id"], session["resolution"]["version_id"])

    def test_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.get_version("ver_00000000")
        with self.assertRaises(NotFoundError):
            self.service.get_session("se_00000000")


if __name__ == "__main__":
    unittest.main()
