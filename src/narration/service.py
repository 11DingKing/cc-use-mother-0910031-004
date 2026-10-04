"""讲解内容版本发布的领域服务。

核心规则：
- 内容片段按血缘（lineage）修订：局部更正（correction）与整体替代（replacement）
  产生新修订，旧修订置为 superseded；撤回（withdrawn）后该血缘终止；
- 发布需多级审核签署，签署绑定草稿内容序号（content_seq），草稿内容变更即失效；
  并发签署由唯一约束与串行写事务保证结果确定；
- 发布时把片段与引用来源冻结为不可变快照，生成稳定版本号（v1、v2…）与内容指纹，
  版本通过 parent_id 形成版本链；
- 已排场次按规则解析版本（指定版本优先且未撤回，否则取开场前最新已发布未撤回版本），
  已完成场次永久冻结当时依据，后续撤回/更正不影响历史服务记录。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Callable, Iterable

from .diff import diff_snapshots
from .errors import ConflictError, NotFoundError, ValidationError
from .store import Store

CHANGE_ORIGINAL = "original"
CHANGE_CORRECTION = "correction"
CHANGE_REPLACEMENT = "replacement"
CHANGE_KINDS = (CHANGE_CORRECTION, CHANGE_REPLACEMENT)

FRAGMENT_ACTIVE = "active"
FRAGMENT_SUPERSEDED = "superseded"
FRAGMENT_WITHDRAWN = "withdrawn"

DRAFT_OPEN = "open"
DRAFT_PUBLISHED = "published"
DRAFT_ABANDONED = "abandoned"

VERSION_PUBLISHED = "published"
VERSION_WITHDRAWN = "withdrawn"

SESSION_SCHEDULED = "scheduled"
SESSION_COMPLETED = "completed"
SESSION_CANCELLED = "cancelled"

DEFAULT_REQUIRED_ROLES = ("文博中心运营员", "场馆负责人")

_AUDIENCE_CODE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_instant(text: object, field_name: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError(f"{field_name} 不能为空")
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValidationError(f"{field_name} 不是合法的 ISO 时间：{text}") from exc
    return _iso(parsed)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} 不能为空")
    return value.strip()


class NarrationService:
    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] = _utcnow,
        required_roles: Iterable[str] = DEFAULT_REQUIRED_ROLES,
    ):
        self.store = store
        self.clock = clock
        self.required_roles = tuple(required_roles)
        if not self.required_roles:
            raise ValueError("至少需要一个审核签署角色")

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return _iso(self.clock())

    def _exists(self, table: str, row_id: str) -> bool:
        return self.store.one(f"SELECT 1 AS ok FROM {table} WHERE id = ?", (row_id,)) is not None

    def _new_id(self, table: str, prefix: str) -> str:
        while True:
            candidate = f"{prefix}_{secrets.token_hex(4)}"
            if not self._exists(table, candidate):
                return candidate

    def _must_get(self, table: str, row_id: str, what: str) -> dict:
        row = self.store.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        if row is None:
            raise NotFoundError(f"{what}不存在：{row_id}")
        return row

    def _must_audience(self, code: str) -> dict:
        row = self.store.one("SELECT * FROM audience_levels WHERE code = ?", (code,))
        if row is None:
            raise NotFoundError(f"受众级别不存在：{code}")
        return row

    def _audit(self, conn, actor: str, action: str, entity: str, entity_id: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_events(at, actor, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
            (self._now(), actor, action, entity, entity_id, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )

    # ------------------------------------------------------------------
    # 主题 / 受众级别 / 引用来源
    # ------------------------------------------------------------------
    def create_theme(self, *, name: str, description: str = "", actor: str = "system") -> dict:
        name = _require_text(name, "主题名称")
        actor = _require_text(actor, "操作人")
        theme_id = self._new_id("themes", "th")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO themes(id, name, description, created_at) VALUES (?,?,?,?)",
                (theme_id, name, description or "", self._now()),
            )
            self._audit(conn, actor, "theme.created", "theme", theme_id, {"name": name})
        return self._must_get("themes", theme_id, "主题")

    def list_themes(self) -> list[dict]:
        return self.store.all("SELECT * FROM themes ORDER BY created_at, id")

    def create_audience(
        self,
        *,
        code: str,
        name: str,
        min_age: int | None = None,
        max_age: int | None = None,
        actor: str = "system",
    ) -> dict:
        code = _require_text(code, "受众级别代码")
        if not _AUDIENCE_CODE.match(code):
            raise ValidationError("受众级别代码只能包含小写字母、数字与连字符")
        name = _require_text(name, "受众级别名称")
        actor = _require_text(actor, "操作人")
        if min_age is not None and max_age is not None and min_age > max_age:
            raise ValidationError("受众年龄区间不合法")
        if self.store.one("SELECT 1 AS ok FROM audience_levels WHERE code = ?", (code,)):
            raise ConflictError(f"受众级别已存在：{code}")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO audience_levels(code, name, min_age, max_age, created_at) VALUES (?,?,?,?,?)",
                (code, name, min_age, max_age, self._now()),
            )
            self._audit(conn, actor, "audience.created", "audience_level", code, {"name": name})
        return self._must_audience(code)

    def list_audiences(self) -> list[dict]:
        return self.store.all("SELECT * FROM audience_levels ORDER BY code")

    def create_source(
        self,
        *,
        title: str,
        publisher: str = "",
        url: str = "",
        accessed_at: str | None = None,
        actor: str = "system",
    ) -> dict:
        title = _require_text(title, "来源标题")
        actor = _require_text(actor, "操作人")
        if accessed_at is not None:
            accessed_at = _parse_instant(accessed_at, "访问时间")
        source_id = self._new_id("sources", "src")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO sources(id, title, publisher, url, accessed_at, created_at) VALUES (?,?,?,?,?,?)",
                (source_id, title, publisher or "", url or "", accessed_at, self._now()),
            )
            self._audit(conn, actor, "source.created", "source", source_id, {"title": title})
        return self._must_get("sources", source_id, "引用来源")

    def list_sources(self) -> list[dict]:
        return self.store.all("SELECT * FROM sources ORDER BY created_at, id")

    # ------------------------------------------------------------------
    # 内容片段
    # ------------------------------------------------------------------
    def _validate_sources(self, source_ids: Iterable[str]) -> list[str]:
        result: list[str] = []
        for source_id in source_ids:
            source_id = _require_text(source_id, "引用来源编号")
            self._must_get("sources", source_id, "引用来源")
            if source_id not in result:
                result.append(source_id)
        return result

    def _fragment_source_ids(self, fragment_id: str) -> list[str]:
        rows = self.store.all(
            "SELECT source_id FROM fragment_sources WHERE fragment_id = ? ORDER BY source_id",
            (fragment_id,),
        )
        return [row["source_id"] for row in rows]

    def create_fragment(
        self,
        *,
        theme_id: str,
        audience_code: str,
        title: str,
        body: str,
        source_ids: Iterable[str] = (),
        actor: str = "system",
    ) -> dict:
        self._must_get("themes", theme_id, "主题")
        self._must_audience(audience_code)
        title = _require_text(title, "片段标题")
        body = _require_text(body, "片段正文")
        actor = _require_text(actor, "操作人")
        sources = self._validate_sources(source_ids or ())
        fragment_id = self._new_id("fragments", "fr")
        lineage_id = self._new_id("fragments", "ln")
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO fragments(id, lineage_id, theme_id, audience_code, title, body,
                                         change_kind, correction_note, supersedes_id, revision,
                                         status, withdrawn_reason, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fragment_id, lineage_id, theme_id, audience_code, title, body,
                    CHANGE_ORIGINAL, None, None, 1, FRAGMENT_ACTIVE, None, actor, self._now(),
                ),
            )
            for source_id in sources:
                conn.execute(
                    "INSERT INTO fragment_sources(fragment_id, source_id) VALUES (?,?)",
                    (fragment_id, source_id),
                )
            self._audit(conn, actor, "fragment.created", "fragment", fragment_id, {"lineage_id": lineage_id})
        return self.get_fragment(fragment_id)

    def revise_fragment(
        self,
        fragment_id: str,
        *,
        mode: str,
        actor: str,
        title: str | None = None,
        body: str | None = None,
        source_ids: Iterable[str] | None = None,
        note: str | None = None,
    ) -> dict:
        """局部更正（correction）或整体替代（replacement）：产生新修订并替代旧修订。"""
        if mode not in CHANGE_KINDS:
            raise ValidationError(f"修订方式必须是 {' 或 '.join(CHANGE_KINDS)}")
        actor = _require_text(actor, "操作人")
        if mode == CHANGE_CORRECTION and not (note and note.strip()):
            raise ValidationError("局部更正必须填写更正说明")
        if mode == CHANGE_REPLACEMENT and (title is None or body is None):
            raise ValidationError("替代片段需提供完整标题与正文")

        with self.store.transaction() as conn:
            head = self._must_get("fragments", fragment_id, "内容片段")
            if head["status"] != FRAGMENT_ACTIVE:
                raise ConflictError("该片段已被替代或撤回，不能在其上修订")

            new_title = _require_text(title, "片段标题") if title is not None else head["title"]
            new_body = _require_text(body, "片段正文") if body is not None else head["body"]
            old_sources = self._fragment_source_ids(fragment_id)
            new_sources = self._validate_sources(source_ids) if source_ids is not None else old_sources
            if new_title == head["title"] and new_body == head["body"] and new_sources == old_sources:
                raise ValidationError("修订内容与原片段一致")

            new_id = self._new_id("fragments", "fr")
            conn.execute("UPDATE fragments SET status = ? WHERE id = ?", (FRAGMENT_SUPERSEDED, fragment_id))
            conn.execute(
                """INSERT INTO fragments(id, lineage_id, theme_id, audience_code, title, body,
                                         change_kind, correction_note, supersedes_id, revision,
                                         status, withdrawn_reason, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id, head["lineage_id"], head["theme_id"], head["audience_code"],
                    new_title, new_body, mode, note, fragment_id, head["revision"] + 1,
                    FRAGMENT_ACTIVE, None, actor, self._now(),
                ),
            )
            for source_id in new_sources:
                conn.execute(
                    "INSERT INTO fragment_sources(fragment_id, source_id) VALUES (?,?)",
                    (new_id, source_id),
                )
            self._audit(
                conn, actor, "fragment.revised", "fragment", new_id,
                {"mode": mode, "lineage_id": head["lineage_id"], "supersedes": fragment_id},
            )
        return self.get_fragment(new_id)

    def withdraw_fragment(self, fragment_id: str, *, reason: str, actor: str) -> dict:
        reason = _require_text(reason, "撤回原因")
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            head = self._must_get("fragments", fragment_id, "内容片段")
            if head["status"] != FRAGMENT_ACTIVE:
                raise ConflictError("只有生效中的片段可以撤回")
            conn.execute(
                "UPDATE fragments SET status = ?, withdrawn_reason = ? WHERE id = ?",
                (FRAGMENT_WITHDRAWN, reason, fragment_id),
            )
            self._audit(
                conn, actor, "fragment.withdrawn", "fragment", fragment_id,
                {"reason": reason, "lineage_id": head["lineage_id"]},
            )
        return self.get_fragment(fragment_id)

    def get_fragment(self, fragment_id: str) -> dict:
        row = self._must_get("fragments", fragment_id, "内容片段")
        row["source_ids"] = self._fragment_source_ids(fragment_id)
        row["revision_chain"] = self.store.all(
            """SELECT id, revision, change_kind, correction_note, status, created_by, created_at
               FROM fragments WHERE lineage_id = ? ORDER BY revision""",
            (row["lineage_id"],),
        )
        row["is_head"] = row["status"] == FRAGMENT_ACTIVE
        return row

    def list_fragments(
        self,
        *,
        theme_id: str | None = None,
        audience_code: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM fragments WHERE 1=1"
        params: list = []
        if theme_id:
            sql += " AND theme_id = ?"
            params.append(theme_id)
        if audience_code:
            sql += " AND audience_code = ?"
            params.append(audience_code)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY lineage_id, revision"
        return self.store.all(sql, tuple(params))

    # ------------------------------------------------------------------
    # 发布草稿与多级审核签署
    # ------------------------------------------------------------------
    def _validate_draft_fragments(self, theme_id: str, audience_code: str, fragment_ids: Iterable[str]) -> list[str]:
        ids = [_require_text(fid, "片段编号") for fid in fragment_ids]
        if not ids:
            raise ValidationError("发布草稿至少包含一个内容片段")
        if len(set(ids)) != len(ids):
            raise ValidationError("草稿包含重复片段")
        for fid in ids:
            fragment = self._must_get("fragments", fid, "内容片段")
            if fragment["theme_id"] != theme_id or fragment["audience_code"] != audience_code:
                raise ValidationError(f"片段 {fid} 不属于该主题与受众级别")
            if fragment["status"] != FRAGMENT_ACTIVE:
                raise ConflictError(f"片段 {fid} 不是生效状态，不能加入草稿")
        return ids

    def create_draft(
        self,
        *,
        theme_id: str,
        audience_code: str,
        fragment_ids: Iterable[str],
        actor: str = "system",
    ) -> dict:
        self._must_get("themes", theme_id, "主题")
        self._must_audience(audience_code)
        actor = _require_text(actor, "操作人")
        ids = self._validate_draft_fragments(theme_id, audience_code, fragment_ids)
        draft_id = self._new_id("release_drafts", "dr")
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO release_drafts(id, theme_id, audience_code, status, content_seq,
                                              published_version_id, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (draft_id, theme_id, audience_code, DRAFT_OPEN, 1, None, actor, self._now()),
            )
            for position, fid in enumerate(ids):
                conn.execute(
                    "INSERT INTO draft_items(draft_id, fragment_id, position) VALUES (?,?,?)",
                    (draft_id, fid, position),
                )
            self._audit(conn, actor, "draft.created", "release_draft", draft_id, {"fragment_count": len(ids)})
        return self.get_draft(draft_id)

    def replace_draft_items(self, draft_id: str, *, fragment_ids: Iterable[str], actor: str) -> dict:
        """替换草稿内容：内容序号 +1，已有签署全部失效。"""
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            draft = self._must_get("release_drafts", draft_id, "发布草稿")
            if draft["status"] != DRAFT_OPEN:
                raise ConflictError("草稿已发布或废弃，不能修改内容")
            ids = self._validate_draft_fragments(draft["theme_id"], draft["audience_code"], fragment_ids)
            conn.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft_id,))
            for position, fid in enumerate(ids):
                conn.execute(
                    "INSERT INTO draft_items(draft_id, fragment_id, position) VALUES (?,?,?)",
                    (draft_id, fid, position),
                )
            new_seq = draft["content_seq"] + 1
            conn.execute("UPDATE release_drafts SET content_seq = ? WHERE id = ?", (new_seq, draft_id))
            cursor = conn.execute("DELETE FROM signoffs WHERE draft_id = ?", (draft_id,))
            self._audit(
                conn, actor, "draft.items_replaced", "release_draft", draft_id,
                {"content_seq": new_seq, "invalidated_signoffs": cursor.rowcount},
            )
        return self.get_draft(draft_id)

    def sign_draft(self, draft_id: str, *, role: str, signer: str) -> dict:
        """并发安全的多级签署：同角色重复签署冲突，同人同内容重签幂等。"""
        role = _require_text(role, "签署角色")
        signer = _require_text(signer, "签署人")
        with self.store.transaction() as conn:
            draft = self._must_get("release_drafts", draft_id, "发布草稿")
            if draft["status"] != DRAFT_OPEN:
                raise ConflictError("草稿已发布或废弃，不能签署")
            if role not in self.required_roles:
                raise ValidationError(f"该角色无需签署：{role}")
            existing = self.store.one(
                "SELECT * FROM signoffs WHERE draft_id = ? AND role = ?", (draft_id, role)
            )
            if existing:
                if existing["signer"] == signer and existing["content_seq"] == draft["content_seq"]:
                    return self.get_draft(draft_id)
                raise ConflictError(f"角色「{role}」已签署，如需重签请先更新草稿内容")
            signoff_id = self._new_id("signoffs", "sg")
            conn.execute(
                "INSERT INTO signoffs(id, draft_id, role, signer, content_seq, signed_at) VALUES (?,?,?,?,?,?)",
                (signoff_id, draft_id, role, signer, draft["content_seq"], self._now()),
            )
            self._audit(
                conn, signer, "draft.signed", "release_draft", draft_id,
                {"role": role, "content_seq": draft["content_seq"]},
            )
        return self.get_draft(draft_id)

    def abandon_draft(self, draft_id: str, *, actor: str) -> dict:
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            draft = self._must_get("release_drafts", draft_id, "发布草稿")
            if draft["status"] != DRAFT_OPEN:
                raise ConflictError("草稿已发布或废弃")
            conn.execute("UPDATE release_drafts SET status = ? WHERE id = ?", (DRAFT_ABANDONED, draft_id))
            self._audit(conn, actor, "draft.abandoned", "release_draft", draft_id, {})
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict:
        draft = self._must_get("release_drafts", draft_id, "发布草稿")
        items = self.store.all(
            """SELECT di.position, f.id AS fragment_id, f.lineage_id, f.title, f.revision, f.status, f.change_kind
               FROM draft_items di JOIN fragments f ON f.id = di.fragment_id
               WHERE di.draft_id = ? ORDER BY di.position""",
            (draft_id,),
        )
        for item in items:
            item["stale"] = item["status"] != FRAGMENT_ACTIVE
        signoffs = self.store.all(
            "SELECT role, signer, content_seq, signed_at FROM signoffs WHERE draft_id = ? ORDER BY signed_at, role",
            (draft_id,),
        )
        for signoff in signoffs:
            signoff["stale"] = signoff["content_seq"] != draft["content_seq"]
        valid_roles = {s["role"] for s in signoffs if not s["stale"]}
        missing = [role for role in self.required_roles if role not in valid_roles]
        draft["items"] = items
        draft["signoffs"] = signoffs
        draft["required_roles"] = list(self.required_roles)
        draft["missing_roles"] = missing
        draft["publishable"] = (
            draft["status"] == DRAFT_OPEN and bool(items) and not missing and not any(i["stale"] for i in items)
        )
        return draft

    # ------------------------------------------------------------------
    # 发布与版本链
    # ------------------------------------------------------------------
    def publish_draft(self, draft_id: str, *, actor: str) -> dict:
        """发布：校验签署与片段状态，冻结依赖快照，生成稳定版本号与内容指纹。"""
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            draft = self._must_get("release_drafts", draft_id, "发布草稿")
            if draft["status"] != DRAFT_OPEN:
                raise ConflictError("草稿已发布或废弃")
            items = self.store.all(
                """SELECT di.position, f.* FROM draft_items di JOIN fragments f ON f.id = di.fragment_id
                   WHERE di.draft_id = ? ORDER BY di.position""",
                (draft_id,),
            )
            if not items:
                raise ValidationError("草稿没有内容片段，不能发布")
            stale = [item["id"] for item in items if item["status"] != FRAGMENT_ACTIVE]
            if stale:
                raise ConflictError("草稿包含已撤回或已被替代的片段：" + "、".join(stale) + "，请更新草稿")

            signed = self.store.all(
                "SELECT role FROM signoffs WHERE draft_id = ? AND content_seq = ?",
                (draft_id, draft["content_seq"]),
            )
            signed_roles = {row["role"] for row in signed}
            missing = [role for role in self.required_roles if role not in signed_roles]
            if missing:
                raise ValidationError("缺少审核签署：" + "、".join(missing))

            row = self.store.one(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM versions WHERE theme_id = ? AND audience_code = ?",
                (draft["theme_id"], draft["audience_code"]),
            )
            seq = row["max_seq"] + 1
            parent = self.store.one(
                "SELECT id FROM versions WHERE theme_id = ? AND audience_code = ? AND seq = ?",
                (draft["theme_id"], draft["audience_code"], seq - 1),
            ) if seq > 1 else None
            version_id = self._new_id("versions", "ver")
            label = f"v{seq}"
            now = self._now()
            conn.execute(
                """INSERT INTO versions(id, theme_id, audience_code, seq, label, parent_id, fingerprint,
                                        status, withdrawn_reason, withdrawn_at, published_by, published_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, draft["theme_id"], draft["audience_code"], seq, label,
                    parent["id"] if parent else None, "", VERSION_PUBLISHED, None, None, actor, now,
                ),
            )

            snapshot_items = []
            ordered_source_ids: list[str] = []
            for item in items:
                source_ids = self._fragment_source_ids(item["id"])
                conn.execute(
                    """INSERT INTO version_items(version_id, fragment_id, lineage_id, title, body,
                                                 change_kind, correction_note, position)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        version_id, item["id"], item["lineage_id"], item["title"], item["body"],
                        item["change_kind"], item["correction_note"], item["position"],
                    ),
                )
                snapshot_items.append(
                    {
                        "position": item["position"],
                        "fragment_id": item["id"],
                        "lineage_id": item["lineage_id"],
                        "title": item["title"],
                        "body": item["body"],
                        "change_kind": item["change_kind"],
                        "correction_note": item["correction_note"],
                        "source_ids": source_ids,
                    }
                )
                for source_id in source_ids:
                    if source_id not in ordered_source_ids:
                        ordered_source_ids.append(source_id)

            snapshot_sources = []
            for source_id in sorted(ordered_source_ids):
                source = self._must_get("sources", source_id, "引用来源")
                conn.execute(
                    "INSERT INTO version_sources(version_id, source_id, title, publisher, url) VALUES (?,?,?,?,?)",
                    (version_id, source_id, source["title"], source["publisher"], source["url"]),
                )
                snapshot_sources.append(
                    {
                        "source_id": source_id,
                        "title": source["title"],
                        "publisher": source["publisher"],
                        "url": source["url"],
                    }
                )

            canonical = json.dumps(
                {
                    "theme_id": draft["theme_id"],
                    "audience_code": draft["audience_code"],
                    "items": snapshot_items,
                    "sources": snapshot_sources,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            conn.execute("UPDATE versions SET fingerprint = ? WHERE id = ?", (fingerprint, version_id))
            conn.execute(
                "UPDATE release_drafts SET status = ?, published_version_id = ? WHERE id = ?",
                (DRAFT_PUBLISHED, version_id, draft_id),
            )
            self._audit(
                conn, actor, "version.published", "version", version_id,
                {"label": label, "seq": seq, "parent_id": parent["id"] if parent else None, "draft_id": draft_id},
            )
        return self.get_version(version_id)

    def withdraw_version(self, version_id: str, *, reason: str, actor: str) -> dict:
        """撤回版本：未来场次不再选用；已完成场次的历史依据保持不变。"""
        reason = _require_text(reason, "撤回原因")
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            version = self._must_get("versions", version_id, "版本")
            if version["status"] != VERSION_PUBLISHED:
                raise ConflictError("版本已撤回")
            conn.execute(
                "UPDATE versions SET status = ?, withdrawn_reason = ?, withdrawn_at = ? WHERE id = ?",
                (VERSION_WITHDRAWN, reason, self._now(), version_id),
            )
            self._audit(
                conn, actor, "version.withdrawn", "version", version_id,
                {"reason": reason, "label": version["label"]},
            )
        return self.get_version(version_id)

    def _lineage_status(self, lineage_id: str, fragment_id: str) -> str:
        head = self.store.one(
            "SELECT id FROM fragments WHERE lineage_id = ? AND status = ?",
            (lineage_id, FRAGMENT_ACTIVE),
        )
        if head is None:
            return FRAGMENT_WITHDRAWN
        return FRAGMENT_ACTIVE if head["id"] == fragment_id else FRAGMENT_SUPERSEDED

    def get_version(self, version_id: str) -> dict:
        version = self._must_get("versions", version_id, "版本")
        items = self.store.all(
            "SELECT * FROM version_items WHERE version_id = ? ORDER BY position", (version_id,)
        )
        stale_count = 0
        for item in items:
            item["lineage_status"] = self._lineage_status(item["lineage_id"], item["fragment_id"])
            if item["lineage_status"] != FRAGMENT_ACTIVE:
                stale_count += 1
        version["items"] = items
        version["sources"] = self.store.all(
            "SELECT * FROM version_sources WHERE version_id = ? ORDER BY source_id", (version_id,)
        )
        version["freshness"] = {"stale_item_count": stale_count, "item_count": len(items)}
        return version

    def list_versions(
        self, *, theme_id: str | None = None, audience_code: str | None = None
    ) -> list[dict]:
        """版本链：按主题与受众级别有序返回，含父版本与撤回状态。"""
        sql = "SELECT * FROM versions WHERE 1=1"
        params: list = []
        if theme_id:
            sql += " AND theme_id = ?"
            params.append(theme_id)
        if audience_code:
            sql += " AND audience_code = ?"
            params.append(audience_code)
        sql += " ORDER BY theme_id, audience_code, seq"
        return self.store.all(sql, tuple(params))

    def _version_snapshot(self, version_id: str) -> dict:
        version = self._must_get("versions", version_id, "版本")
        return {
            "id": version["id"],
            "label": version["label"],
            "seq": version["seq"],
            "theme_id": version["theme_id"],
            "audience_code": version["audience_code"],
            "status": version["status"],
            "fingerprint": version["fingerprint"],
            "published_at": version["published_at"],
            "items": self.store.all(
                "SELECT * FROM version_items WHERE version_id = ? ORDER BY position", (version_id,)
            ),
            "sources": self.store.all(
                "SELECT * FROM version_sources WHERE version_id = ? ORDER BY source_id", (version_id,)
            ),
        }

    def diff_versions(self, from_id: str, to_id: str) -> dict:
        """比较两个版本的冻结快照：片段增删改与引用来源变化。"""
        a = self._version_snapshot(from_id)
        b = self._version_snapshot(to_id)
        if (a["theme_id"], a["audience_code"]) != (b["theme_id"], b["audience_code"]):
            raise ValidationError("只能比较同一主题与受众级别的版本")
        result = diff_snapshots(a, b)
        result["from"] = {
            "version_id": a["id"], "label": a["label"], "seq": a["seq"],
            "published_at": a["published_at"], "fingerprint": a["fingerprint"],
        }
        result["to"] = {
            "version_id": b["id"], "label": b["label"], "seq": b["seq"],
            "published_at": b["published_at"], "fingerprint": b["fingerprint"],
        }
        return result

    # ------------------------------------------------------------------
    # 场次与版本解析
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        theme_id: str,
        audience_code: str,
        title: str,
        scheduled_start: str,
        pin_version_id: str | None = None,
        actor: str = "system",
    ) -> dict:
        self._must_get("themes", theme_id, "主题")
        self._must_audience(audience_code)
        title = _require_text(title, "场次名称")
        actor = _require_text(actor, "操作人")
        start = _parse_instant(scheduled_start, "开场时间")
        if pin_version_id is not None:
            version = self._must_get("versions", pin_version_id, "版本")
            if version["theme_id"] != theme_id or version["audience_code"] != audience_code:
                raise ValidationError("指定版本不属于该主题与受众级别")
            if version["status"] != VERSION_PUBLISHED:
                raise ValidationError("不能指定已撤回的版本")
        session_id = self._new_id("sessions", "se")
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO sessions(id, theme_id, audience_code, title, scheduled_start, status,
                                        pin_version_id, completed_version_id, completed_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, theme_id, audience_code, title, start, SESSION_SCHEDULED,
                 pin_version_id, None, None, self._now()),
            )
            self._audit(
                conn, actor, "session.created", "session", session_id,
                {"scheduled_start": start, "pin_version_id": pin_version_id},
            )
        return self.get_session(session_id)

    def _resolve_session(
        self,
        session: dict,
        *,
        extra_withdrawn: frozenset = frozenset(),
        extra_published: frozenset = frozenset(),
    ) -> dict:
        """解析场次应使用的版本。

        规则：已完成场次返回冻结依据；指定版本（pin）优先且未撤回时生效；
        否则取开场时间前最新已发布且未撤回的版本。
        extra_withdrawn / extra_published 用于影响范围分析的假设场景。
        """
        if session["status"] == SESSION_COMPLETED:
            return {"version_id": session["completed_version_id"], "rule": "completed_frozen", "note": None}
        if session["status"] == SESSION_CANCELLED:
            return {"version_id": None, "rule": "cancelled", "note": None}

        note = None
        pin = session["pin_version_id"]
        if pin:
            pinned = self._must_get("versions", pin, "版本")
            withdrawn = (pinned["status"] == VERSION_WITHDRAWN or pin in extra_withdrawn) and pin not in extra_published
            if not withdrawn:
                return {"version_id": pin, "rule": "pinned", "note": None}
            note = "指定版本已撤回，按规则回退"

        candidates = self.store.all(
            """SELECT id, status FROM versions
               WHERE theme_id = ? AND audience_code = ? AND published_at <= ?
               ORDER BY seq DESC""",
            (session["theme_id"], session["audience_code"], session["scheduled_start"]),
        )
        for candidate in candidates:
            withdrawn = (
                candidate["status"] == VERSION_WITHDRAWN or candidate["id"] in extra_withdrawn
            ) and candidate["id"] not in extra_published
            if not withdrawn:
                return {"version_id": candidate["id"], "rule": "latest_before_start", "note": note}
        return {"version_id": None, "rule": "unresolved", "note": note or "开场时间前没有已发布版本"}

    def get_session(self, session_id: str) -> dict:
        session = self._must_get("sessions", session_id, "场次")
        session["resolution"] = self._resolve_session(session)
        basis = None
        if session["status"] == SESSION_COMPLETED and session["completed_version_id"]:
            version = self._must_get("versions", session["completed_version_id"], "版本")
            basis = {
                "version_id": version["id"],
                "label": version["label"],
                "fingerprint": version["fingerprint"],
                "published_at": version["published_at"],
                "version_status": version["status"],
            }
        session["basis"] = basis
        return session

    def list_sessions(
        self,
        *,
        status: str | None = None,
        theme_id: str | None = None,
        audience_code: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if theme_id:
            sql += " AND theme_id = ?"
            params.append(theme_id)
        if audience_code:
            sql += " AND audience_code = ?"
            params.append(audience_code)
        sql += " ORDER BY scheduled_start, id"
        sessions = self.store.all(sql, tuple(params))
        for session in sessions:
            session["resolution"] = self._resolve_session(session)
        return sessions

    def complete_session(
        self,
        session_id: str,
        *,
        actor: str,
        version_id: str | None = None,
        at: str | None = None,
    ) -> dict:
        """完成场次：冻结实际采用版本作为服务记录依据，之后不再变化。"""
        actor = _require_text(actor, "操作人")
        completed_at = _parse_instant(at, "完成时间") if at else self._now()
        with self.store.transaction() as conn:
            session = self._must_get("sessions", session_id, "场次")
            if session["status"] != SESSION_SCHEDULED:
                raise ConflictError("场次已结束或取消")
            if version_id is not None:
                version = self._must_get("versions", version_id, "版本")
                if (
                    version["theme_id"] != session["theme_id"]
                    or version["audience_code"] != session["audience_code"]
                ):
                    raise ValidationError("记录版本不属于该主题与受众级别")
                chosen, rule = version_id, "explicit"
            else:
                resolution = self._resolve_session(session)
                if resolution["version_id"] is None:
                    raise ValidationError("没有可用版本，无法完成场次")
                chosen, rule = resolution["version_id"], resolution["rule"]
            conn.execute(
                "UPDATE sessions SET status = ?, completed_version_id = ?, completed_at = ? WHERE id = ?",
                (SESSION_COMPLETED, chosen, completed_at, session_id),
            )
            self._audit(
                conn, actor, "session.completed", "session", session_id,
                {"version_id": chosen, "rule": rule, "completed_at": completed_at},
            )
        return self.get_session(session_id)

    def cancel_session(self, session_id: str, *, actor: str) -> dict:
        actor = _require_text(actor, "操作人")
        with self.store.transaction() as conn:
            session = self._must_get("sessions", session_id, "场次")
            if session["status"] != SESSION_SCHEDULED:
                raise ConflictError("场次已结束或取消")
            conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (SESSION_CANCELLED, session_id))
            self._audit(conn, actor, "session.cancelled", "session", session_id, {})
        return self.get_session(session_id)

    # ------------------------------------------------------------------
    # 影响范围分析
    # ------------------------------------------------------------------
    def version_impact(self, version_id: str) -> dict:
        """列出受该版本影响的未来场次：发布导致切换、撤回导致回退。"""
        version = self._must_get("versions", version_id, "版本")
        sessions = self.store.all(
            """SELECT * FROM sessions
               WHERE theme_id = ? AND audience_code = ? AND status = ?
               ORDER BY scheduled_start, id""",
            (version["theme_id"], version["audience_code"], SESSION_SCHEDULED),
        )
        affected = []
        for session in sessions:
            current = self._resolve_session(session)
            if version["status"] == VERSION_PUBLISHED:
                hypothetical = self._resolve_session(session, extra_withdrawn=frozenset({version_id}))
            else:
                hypothetical = self._resolve_session(session, extra_published=frozenset({version_id}))
            if current["version_id"] != hypothetical["version_id"]:
                affected.append(
                    {
                        "session_id": session["id"],
                        "title": session["title"],
                        "scheduled_start": session["scheduled_start"],
                        "previous_version_id": hypothetical["version_id"],
                        "current_version_id": current["version_id"],
                    }
                )
        return {
            "version": {
                "version_id": version["id"],
                "label": version["label"],
                "status": version["status"],
                "theme_id": version["theme_id"],
                "audience_code": version["audience_code"],
            },
            "affected_sessions": affected,
            "affected_count": len(affected),
        }

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit_log(self, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        rows = self.store.all("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows
