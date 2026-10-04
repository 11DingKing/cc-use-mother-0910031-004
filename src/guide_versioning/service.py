"""领域服务：内容维护、发布冻结、签署、撤回、场次解析与影响分析。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .clock import Clock
from .errors import ConflictError, NotFoundError, RuleError
from .store import Store

# 审核链：按顺序签署；全部完成后版本进入“已确认”。
SIGN_CHAIN = ("内容审核", "场馆负责人")

# 允许局部更正的字段（片段级紧急纠错）。
CORRECTABLE_FIELDS = {"title", "body"}

FRAGMENT_KINDS = {"opening", "section", "interaction", "closing"}


def version_str(r: Any) -> str:
    return f"{r['version_major']}.{r['version_minor']}.{r['version_patch']}"


def parse_ts(value: str) -> str:
    """规范化 ISO 时间字符串。"""
    from datetime import datetime

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.isoformat()


class GuideService:
    def __init__(self, store: Store, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()

    # ================= 基础数据：受众、主题、来源 =================

    def create_audience(self, code: str, name: str, min_age: int | None = None,
                        max_age: int | None = None) -> dict:
        if min_age is not None and max_age is not None and min_age > max_age:
            raise RuleError("受众年龄下限不能大于上限")
        with self.store.lock:
            try:
                self.store.execute(
                    "INSERT INTO audience_levels(code, name, min_age, max_age) VALUES (?,?,?,?)",
                    (code, name, min_age, max_age),
                )
                self.store.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"受众级别已存在：{code}") from exc
        return self.get_audience(code)

    def get_audience(self, code: str) -> dict:
        row = self.store.query_one("SELECT * FROM audience_levels WHERE code=?", (code,))
        if row is None:
            raise NotFoundError(f"受众级别不存在：{code}")
        return dict(row)

    def list_audiences(self) -> list[dict]:
        return [dict(r) for r in self.store.query_all("SELECT * FROM audience_levels ORDER BY code")]

    def create_theme(self, code: str, title: str) -> dict:
        with self.store.lock:
            try:
                self.store.execute(
                    "INSERT INTO themes(code, title, created_at) VALUES (?,?,?)",
                    (code, title, self.clock.now()),
                )
                self.store.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"主题已存在：{code}") from exc
        return self.get_theme(code)

    def get_theme(self, code: str) -> dict:
        row = self.store.query_one("SELECT * FROM themes WHERE code=?", (code,))
        if row is None:
            raise NotFoundError(f"主题不存在：{code}")
        return dict(row)

    def list_themes(self) -> list[dict]:
        return [dict(r) for r in self.store.query_all("SELECT * FROM themes ORDER BY code")]

    def create_source(self, code: str, title: str, **fields: Any) -> dict:
        allowed = {"author", "publisher", "uri", "locator", "year"}
        data = {k: v for k, v in fields.items() if k in allowed}
        cols = ["code", "title", *data.keys()]
        marks = ",".join("?" for _ in cols)
        with self.store.lock:
            try:
                self.store.execute(
                    f"INSERT INTO sources({','.join(cols)}) VALUES ({marks})",
                    (code, title, *data.values()),
                )
                self.store.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"引用来源已存在：{code}") from exc
        return self.get_source(code)

    def get_source(self, code: str) -> dict:
        row = self.store.query_one("SELECT * FROM sources WHERE code=?", (code,))
        if row is None:
            raise NotFoundError(f"引用来源不存在：{code}")
        return dict(row)

    def list_sources(self) -> list[dict]:
        return [dict(r) for r in self.store.query_all("SELECT * FROM sources ORDER BY code")]

    # ================= 内容片段（含替代、局部更正） =================

    def create_fragment(self, code: str, theme_code: str, audience_code: str, kind: str,
                        title: str, body: str, source_refs: list[dict] | None = None,
                        replaces_code: str | None = None) -> dict:
        if kind not in FRAGMENT_KINDS:
            raise RuleError(f"片段类型必须是：{sorted(FRAGMENT_KINDS)}")
        self.get_theme(theme_code)
        self.get_audience(audience_code)
        if replaces_code is not None:
            old = self._get_fragment_row(replaces_code)
            if old["theme_code"] != theme_code or old["audience_code"] != audience_code:
                raise RuleError("替代片段必须与被替代片段属于同一主题与受众级别")
        source_refs = self._validate_source_refs(source_refs or [])
        with self.store.lock:
            try:
                self.store.execute(
                    """INSERT INTO fragments(code, theme_code, audience_code, kind,
                       replaces_code, status, current_rev, created_at)
                       VALUES (?,?,?,?,?, 'active', 1, ?)""",
                    (code, theme_code, audience_code, kind, replaces_code, self.clock.now()),
                )
                self.store.execute(
                    """INSERT INTO fragment_revisions(fragment_code, rev, title, body, created_at)
                       VALUES (?,?,?,?,?)""",
                    (code, 1, title, body, self.clock.now()),
                )
                self._insert_source_refs(code, 1, source_refs)
                if replaces_code is not None:
                    self.store.execute(
                        "UPDATE fragments SET status='replaced' WHERE code=?", (replaces_code,))
                self.store.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"片段已存在或数据非法：{code}") from exc
        return self.get_fragment(code)

    def _validate_source_refs(self, refs: list[dict]) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        for ref in refs:
            src_code = ref.get("source_code")
            if not src_code:
                raise RuleError("来源引用缺少 source_code")
            self.get_source(src_code)  # 存在性
            if src_code in seen:
                raise RuleError(f"来源重复引用：{src_code}")
            seen.add(src_code)
            result.append({"source_code": src_code, "note": ref.get("note")})
        return result

    def _insert_source_refs(self, fragment_code: str, rev: int, refs: list[dict]) -> None:
        for ref in refs:
            self.store.execute(
                "INSERT INTO fragment_sources(fragment_code, rev, source_code, note) VALUES (?,?,?,?)",
                (fragment_code, rev, ref["source_code"], ref.get("note")),
            )

    def _get_fragment_row(self, code: str):
        row = self.store.query_one("SELECT * FROM fragments WHERE code=?", (code,))
        if row is None:
            raise NotFoundError(f"片段不存在：{code}")
        return row

    def get_fragment(self, code: str) -> dict:
        frag = self._get_fragment_row(code)
        data = dict(frag)
        data["revision"] = self._get_revision(code, frag["current_rev"])
        data["lineage"] = self.fragment_lineage(code)
        return data

    def _get_revision(self, code: str, rev: int) -> dict:
        row = self.store.query_one(
            "SELECT * FROM fragment_revisions WHERE fragment_code=? AND rev=?", (code, rev))
        if row is None:
            raise NotFoundError(f"片段修订不存在：{code} r{rev}")
        data = dict(row)
        data["sources"] = [
            dict(r) for r in self.store.query_all(
                "SELECT s.*, fs.note FROM fragment_sources fs JOIN sources s ON s.code=fs.source_code"
                " WHERE fs.fragment_code=? AND fs.rev=? ORDER BY s.code", (code, rev))
        ]
        return data

    def fragment_lineage(self, code: str) -> list[str]:
        """沿 replaces_code 向上追溯，返回从最早祖先到当前的 code 链。"""
        chain: list[str] = []
        current: str | None = code
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise RuleError("片段替代关系存在环")
            seen.add(current)
            chain.append(current)
            row = self.store.query_one("SELECT replaces_code FROM fragments WHERE code=?", (current,))
            current = row["replaces_code"] if row else None
        chain.reverse()
        return chain

    def correct_fragment(self, code: str, fields: dict, reason: str | None = None) -> dict:
        """局部更正：仅允许标题/正文，追加一个修订号。不改变已冻结的发布。"""
        unknown = set(fields) - CORRECTABLE_FIELDS
        if unknown:
            raise RuleError(f"局部更正只允许字段 {sorted(CORRECTABLE_FIELDS)}，收到：{sorted(unknown)}")
        if not fields:
            raise RuleError("更正内容为空")
        with self.store.lock:
            frag = self._get_fragment_row(code)
            old = self.store.query_one(
                "SELECT * FROM fragment_revisions WHERE fragment_code=? AND rev=?",
                (code, frag["current_rev"]))
            new_rev = frag["current_rev"] + 1
            self.store.execute(
                """INSERT INTO fragment_revisions(fragment_code, rev, title, body, created_at)
                   VALUES (?,?,?,?,?)""",
                (code, new_rev,
                 fields.get("title", old["title"]),
                 fields.get("body", old["body"]),
                 self.clock.now()),
            )
            # 更正默认继承上一版的来源引用。
            old_refs = self.store.query_all(
                "SELECT source_code, note FROM fragment_sources WHERE fragment_code=? AND rev=?",
                (code, frag["current_rev"]))
            self._insert_source_refs(code, new_rev, [dict(r) for r in old_refs])
            self.store.execute(
                "UPDATE fragments SET current_rev=? WHERE code=?", (new_rev, code))
            self.store.commit()
        return self.get_fragment(code)

    def list_fragments(self, theme_code: str, audience_code: str) -> list[dict]:
        rows = self.store.query_all(
            "SELECT code FROM fragments WHERE theme_code=? AND audience_code=? ORDER BY code",
            (theme_code, audience_code))
        return [self.get_fragment(r["code"]) for r in rows]

    # ================= 提纲（片段的有序依赖） =================

    def _outline_id(self, theme_code: str, audience_code: str, create: bool = False) -> int:
        row = self.store.query_one(
            "SELECT id FROM outlines WHERE theme_code=? AND audience_code=?",
            (theme_code, audience_code))
        if row is not None:
            return row["id"]
        if not create:
            raise NotFoundError(f"提纲不存在：{theme_code}/{audience_code}")
        self.get_theme(theme_code)
        self.get_audience(audience_code)
        cur = self.store.execute(
            "INSERT INTO outlines(theme_code, audience_code) VALUES (?,?)",
            (theme_code, audience_code))
        return int(cur.lastrowid)

    def set_outline_entries(self, theme_code: str, audience_code: str,
                            fragment_codes: list[str]) -> dict:
        with self.store.lock:
            outline_id = self._outline_id(theme_code, audience_code, create=True)
            self.store.execute("DELETE FROM outline_entries WHERE outline_id=?", (outline_id,))
            seen: set[str] = set()
            for pos, code in enumerate(fragment_codes, start=1):
                if code in seen:
                    raise RuleError(f"提纲中片段重复：{code}")
                seen.add(code)
                frag = self._get_fragment_row(code)
                if frag["theme_code"] != theme_code or frag["audience_code"] != audience_code:
                    raise RuleError(f"片段 {code} 不属于该主题/受众")
                if frag["status"] == "replaced":
                    raise RuleError(f"片段 {code} 已被替代，不能进入提纲")
                self.store.execute(
                    "INSERT INTO outline_entries(outline_id, position, fragment_code) VALUES (?,?,?)",
                    (outline_id, pos, code))
            self.store.commit()
        return self.get_outline(theme_code, audience_code)

    def get_outline(self, theme_code: str, audience_code: str) -> dict:
        outline_id = self._outline_id(theme_code, audience_code)
        entries = []
        for row in self.store.query_all(
            "SELECT position, fragment_code FROM outline_entries WHERE outline_id=? ORDER BY position",
                (outline_id,)):
            frag = self._get_fragment_row(row["fragment_code"])
            entries.append({"position": row["position"], "fragment": self.get_fragment(frag["code"])})
        return {"theme_code": theme_code, "audience_code": audience_code, "entries": entries}

    # ================= 发布：冻结依赖并生成稳定版本 =================

    def _build_snapshot(self, theme_code: str, audience_code: str) -> dict:
        """收集提纲依赖闭包（片段当前修订 + 引用来源），生成确定性快照。"""
        outline = self.get_outline(theme_code, audience_code)
        items: list[dict] = []
        for entry in outline["entries"]:
            frag = entry["fragment"]
            rev = frag["revision"]
            items.append({
                "position": entry["position"],
                "fragment_code": frag["code"],
                "kind": frag["kind"],
                "rev": frag["current_rev"],
                "title": rev["title"],
                "body": rev["body"],
                "replaces_code": frag["replaces_code"],
                "lineage": frag["lineage"],
                "sources": [
                    {k: s[k] for k in ("code", "title", "author", "publisher", "uri", "locator", "year")}
                    for s in rev["sources"]
                ],
            })
        return {
            "schema_version": 1,
            "theme_code": theme_code,
            "audience_code": audience_code,
            "frozen_at": self.clock.now(),
            "items": items,
        }

    @staticmethod
    def _hash_snapshot(snapshot: dict) -> str:
        # frozen_at 不参与内容指纹：同内容应得到同指纹。
        content = {k: v for k, v in snapshot.items() if k != "frozen_at"}
        return hashlib.sha256(Store.canonical(content)).hexdigest()

    def _latest_release_row(self, theme_code: str, audience_code: str):
        return self.store.query_one(
            """SELECT * FROM releases WHERE theme_code=? AND audience_code=?
               ORDER BY version_major DESC, version_minor DESC, version_patch DESC LIMIT 1""",
            (theme_code, audience_code))

    def publish(self, theme_code: str, audience_code: str, change_kind: str,
                reason: str | None = None, *, major_bump: bool = False) -> dict:
        """发布新版本。

        change_kind:
          - correction：局部更正（紧急纠错），版本号 +0.0.1
          - replacement：片段替代/提纲结构变化，+0.1.0
          - major：馆藏调整等结构性改版，+1.0.0（需显式 major_bump）
        草稿（草拟）状态下创建首版 1.0.0；发布即进入“待核验”等待签署。
        """
        if change_kind not in {"correction", "replacement", "major"}:
            raise RuleError("change_kind 必须是 correction/replacement/major")
        with self.store.lock:
            snapshot = self._build_snapshot(theme_code, audience_code)
            content_sha = self._hash_snapshot(snapshot)
            parent = self._latest_release_row(theme_code, audience_code)

            duplicate = self.store.query_one(
                "SELECT id FROM releases WHERE theme_code=? AND audience_code=? AND content_sha=?",
                (theme_code, audience_code, content_sha))
            if duplicate is not None:
                raise ConflictError("当前内容与已发布版本完全一致，无需重复发布")

            if parent is not None and change_kind == "major" and not major_bump:
                raise RuleError("在已有版本上发布主版本需要显式确认 major_bump=true")

            if parent is None:
                ver = (1, 0, 0)
            elif change_kind == "major" or major_bump:
                ver = (parent["version_major"] + 1, 0, 0)
            elif change_kind == "replacement":
                ver = (parent["version_major"], parent["version_minor"] + 1, 0)
            else:
                ver = (parent["version_major"], parent["version_minor"], parent["version_patch"] + 1)

            cur = self.store.execute(
                """INSERT INTO releases(theme_code, audience_code,
                     version_major, version_minor, version_patch,
                     change_kind, reason, content_sha, snapshot_json, status,
                     parent_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?,?)""",
                (theme_code, audience_code, *ver, change_kind, reason,
                 content_sha, Store.canonical(snapshot).decode("utf-8"),
                 parent["id"] if parent else None, self.clock.now()),
            )
            release_id = int(cur.lastrowid)
            if parent is not None and parent["status"] in {"pending", "confirmed"}:
                # 新版本取代旧的“现行依据”；已完成场次仍引用旧版不受影响。
                self.store.execute(
                    "UPDATE releases SET superseded_by_id=? WHERE id=?",
                    (release_id, parent["id"]))
            self.store.commit()
            return self.get_release(release_id)

    def get_release(self, release_id: int) -> dict:
        row = self.store.query_one("SELECT * FROM releases WHERE id=?", (release_id,))
        if row is None:
            raise NotFoundError(f"发布版本不存在：{release_id}")
        return self._release_dict(row)

    def get_release_by_version(self, theme_code: str, audience_code: str, version: str) -> dict:
        parts = version.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise RuleError("版本号格式应为 major.minor.patch")
        row = self.store.query_one(
            """SELECT * FROM releases WHERE theme_code=? AND audience_code=?
               AND version_major=? AND version_minor=? AND version_patch=?""",
            (theme_code, audience_code, *map(int, parts)))
        if row is None:
            raise NotFoundError(f"版本不存在：{theme_code}/{audience_code}@{version}")
        return self._release_dict(row)

    def _release_dict(self, row) -> dict:
        data = dict(row)
        data["version"] = version_str(row)
        data["snapshot"] = json.loads(row["snapshot_json"])
        data.pop("snapshot_json", None)
        data["signatures"] = [
            dict(r) for r in self.store.query_all(
                "SELECT * FROM signatures WHERE release_id=? ORDER BY seq", (row["id"],))
        ]
        if row["parent_id"]:
            prow = self.store.query_one("SELECT * FROM releases WHERE id=?", (row["parent_id"],))
            data["parent_version"] = version_str(prow)
        if row["superseded_by_id"]:
            srow = self.store.query_one("SELECT * FROM releases WHERE id=?", (row["superseded_by_id"],))
            data["superseded_by_version"] = version_str(srow)
        # 状态名映射到契约术语
        data["state"] = STATUS_LABELS.get(row["status"], row["status"])
        return data

    def list_releases(self, theme_code: str, audience_code: str) -> list[dict]:
        rows = self.store.query_all(
            """SELECT * FROM releases WHERE theme_code=? AND audience_code=?
               ORDER BY version_major, version_minor, version_patch""",
            (theme_code, audience_code))
        return [self._release_dict(r) for r in rows]

    def release_chain(self, theme_code: str, audience_code: str) -> list[dict]:
        """版本链：从最新版沿 parent_id 回溯到首版。"""
        latest = self._latest_release_row(theme_code, audience_code)
        if latest is None:
            return []
        chain: list[dict] = []
        current_id: int | None = latest["id"]
        seen: set[int] = set()
        while current_id is not None:
            if current_id in seen:
                break
            seen.add(current_id)
            row = self.store.query_one("SELECT * FROM releases WHERE id=?", (current_id,))
            chain.append(self._release_dict(row))
            current_id = row["parent_id"]
        return chain

    # ================= 多级审核签署（并发安全） =================

    def sign(self, release_id: int, role: str, signer: str, comment: str | None = None,
             expected_row_version: int | None = None) -> dict:
        """对发布执行签署。

        - 必须按 SIGN_CHAIN 顺序签署，不得越级、不得重复；
        - UNIQUE(release_id, role) 保证并发重复签署只有一个成功；
        - expected_row_version 提供乐观并发控制，防止基于过期状态决策。
        """
        if role not in SIGN_CHAIN:
            raise RuleError(f"签署角色必须是：{'、'.join(SIGN_CHAIN)}")
        with self.store.lock:
            row = self.store.query_one("SELECT * FROM releases WHERE id=?", (release_id,))
            if row is None:
                raise NotFoundError(f"发布版本不存在：{release_id}")
            if expected_row_version is not None and row["row_version"] != expected_row_version:
                raise ConflictError(
                    f"版本已被其他操作更新（row_version={row['row_version']}），请刷新后重试")
            if row["status"] in {"withdrawn", "archived"}:
                raise RuleError("版本已撤回/归档，不能签署")
            existing = {r["role"] for r in self.store.query_all(
                "SELECT role FROM signatures WHERE release_id=?", (release_id,))}
            if role in existing:
                raise ConflictError(f"{role} 已签署，不能重复签署")
            required_before = set(SIGN_CHAIN[:SIGN_CHAIN.index(role)])
            missing = required_before - existing
            if missing:
                raise RuleError(f"须先完成前置签署：{'、'.join(sorted(missing))}")
            seq = len(existing) + 1
            try:
                self.store.execute(
                    """INSERT INTO signatures(release_id, role, signer, comment, seq, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (release_id, role, signer, comment, seq, self.clock.now()),
                )
            except sqlite3.IntegrityError as exc:  # 并发下的唯一约束
                raise ConflictError(f"{role} 已被并发签署") from exc
            self.store.execute(
                "UPDATE releases SET row_version=row_version+1 WHERE id=?", (release_id,))
            if role == SIGN_CHAIN[-1]:
                self.store.execute(
                    "UPDATE releases SET status='confirmed', confirmed_at=?, row_version=row_version+1 WHERE id=?",
                    (self.clock.now(), release_id))
            self.store.commit()
        return self.get_release(release_id)

    # ================= 撤回 =================

    def withdraw(self, release_id: int, note: str, *, force: bool = False) -> dict:
        """撤回版本。已被未来未完成场次引用时需 force，并触发影响分析提示。"""
        with self.store.lock:
            row = self.store.query_one("SELECT * FROM releases WHERE id=?", (release_id,))
            if row is None:
                raise NotFoundError(f"发布版本不存在：{release_id}")
            if row["status"] == "withdrawn":
                raise ConflictError("版本已处于撤回状态")
            if row["status"] == "archived":
                raise RuleError("已归档版本不可撤回")
            affected = self._future_sessions_locked(row["theme_code"], row["audience_code"])
            using = [s for s in affected if self._resolve_release_id_locked(
                row["theme_code"], row["audience_code"], s["starts_at"]) == release_id]
            if using and not force:
                raise RuleError(
                    f"有 {len(using)} 个未来场次将使用该版本，确认撤回请带 force=true",
                    code="withdraw_needs_confirmation")
            self.store.execute(
                "UPDATE releases SET status='withdrawn', withdrawn_at=?, withdraw_note=? WHERE id=?",
                (self.clock.now(), note, release_id))
            self.store.commit()
        return self.get_release(release_id)

    def archive_obsolete(self, before_release_id: int | None = None) -> int:
        """将已被替代且无未来场次使用的旧版本归档（已归档=执行记录的历史依据）。"""
        archived = 0
        with self.store.lock:
            rows = self.store.query_all(
                "SELECT * FROM releases WHERE status='confirmed' AND superseded_by_id IS NOT NULL")
            for row in rows:
                affected = self._future_sessions_locked(row["theme_code"], row["audience_code"])
                in_use = any(
                    self._resolve_release_id_locked(
                        row["theme_code"], row["audience_code"], s["starts_at"]) == row["id"]
                    for s in affected)
                if not in_use:
                    self.store.execute(
                        "UPDATE releases SET status='archived', archived_at=? WHERE id=?",
                        (self.clock.now(), row["id"]))
                    archived += 1
            self.store.commit()
        return archived

    # ================= 场次：排期、版本选择、完成冻结 =================

    def schedule_session(self, theme_code: str, audience_code: str, starts_at: str,
                         venue: str | None = None) -> dict:
        self.get_theme(theme_code)
        self.get_audience(audience_code)
        starts_at = parse_ts(starts_at)
        with self.store.lock:
            # 排期时即验证规则：未来必须能选出可用版本。
            selected = self._resolve_release_id_locked(theme_code, audience_code, starts_at)
            if selected is None:
                raise RuleError("该主题/受众尚无已确认版本，无法排期")
            cur = self.store.execute(
                """INSERT INTO sessions(theme_code, audience_code, starts_at, venue,
                   status, created_at) VALUES (?,?,?,?, 'scheduled', ?)""",
                (theme_code, audience_code, starts_at, venue, self.clock.now()))
            session_id = int(cur.lastrowid)
            self.store.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: int) -> dict:
        row = self.store.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if row is None:
            raise NotFoundError(f"场次不存在：{session_id}")
        data = dict(row)
        data["state"] = SESSION_STATE_LABELS.get(row["status"], row["status"])
        if row["frozen_release_id"]:
            rel = self.store.query_one("SELECT * FROM releases WHERE id=?", (row["frozen_release_id"],))
            data["basis"] = {"release_id": rel["id"], "version": version_str(rel),
                             "content_sha": rel["content_sha"]}
        else:
            rid = self._resolve_release_id_locked(row["theme_code"], row["audience_code"], row["starts_at"])
            data["effective_release_id"] = rid
        return data

    def list_sessions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.store.query_all("SELECT * FROM sessions WHERE status=? ORDER BY starts_at", (status,))
        else:
            rows = self.store.query_all("SELECT * FROM sessions ORDER BY starts_at")
        return [self.get_session(r["id"]) for r in rows]

    def start_session(self, session_id: int) -> dict:
        """开讲：进入执行中；版本依据在完成时最终冻结（开讲时刻也可提前锁定）。"""
        with self.store.lock:
            row = self.store.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
            if row is None:
                raise NotFoundError(f"场次不存在：{session_id}")
            if row["status"] != "scheduled":
                raise ConflictError("只有已排场次可以开讲")
            self.store.execute("UPDATE sessions SET status='running' WHERE id=?", (session_id,))
            self.store.commit()
        return self.get_session(session_id)

    def complete_session(self, session_id: int) -> dict:
        """完成服务：把当时实际依据的版本永久冻结到服务记录上。"""
        with self.store.lock:
            row = self.store.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
            if row is None:
                raise NotFoundError(f"场次不存在：{session_id}")
            if row["status"] == "completed":
                raise ConflictError("场次已完成，依据不可更改")
            rid = self._resolve_release_id_locked(
                row["theme_code"], row["audience_code"], row["starts_at"])
            if rid is None:
                raise RuleError("无已确认版本可作为完成依据")
            self.store.execute(
                "UPDATE sessions SET status='completed', frozen_release_id=?, frozen_at=? WHERE id=?",
                (rid, self.clock.now(), session_id))
            self.store.commit()
        return self.get_session(session_id)

    def _eligible_releases_locked(self, theme_code: str, audience_code: str):
        """可选版本：已确认、未撤回、未归档，按版本号倒序。"""
        return self.store.query_all(
            """SELECT * FROM releases
               WHERE theme_code=? AND audience_code=? AND status='confirmed'
               ORDER BY version_major DESC, version_minor DESC, version_patch DESC""",
            (theme_code, audience_code))

    def _resolve_release_id_locked(self, theme_code: str, audience_code: str,
                                   at_time: str) -> int | None:
        """版本选择规则：at_time（默认开讲时间）之前最新已确认版本。

        已确认时间为空（理论上不会）不选。撤回/归档版本一律不选。
        """
        rows = self.store.query_all(
            """SELECT * FROM releases
               WHERE theme_code=? AND audience_code=? AND status='confirmed'
                 AND confirmed_at IS NOT NULL AND confirmed_at <= ?
               ORDER BY version_major DESC, version_minor DESC, version_patch DESC, confirmed_at DESC
               LIMIT 1""",
            (theme_code, audience_code, at_time))
        return rows[0]["id"] if rows else None

    # ================= 版本差异与影响分析 =================

    def diff_releases(self, release_id_a: int, release_id_b: int) -> dict:
        a = self.get_release(release_id_a)
        b = self.get_release(release_id_b)
        items_a = {i["fragment_code"]: i for i in a["snapshot"]["items"]}
        items_b = {i["fragment_code"]: i for i in b["snapshot"]["items"]}
        added, removed, changed = [], [], []
        for code in sorted(set(items_a) | set(items_b)):
            ia, ib = items_a.get(code), items_b.get(code)
            if ia is None:
                added.append({"fragment_code": code, "position": ib["position"], "rev": ib["rev"]})
            elif ib is None:
                removed.append({"fragment_code": code, "position": ia["position"], "rev": ia["rev"]})
            elif ia["rev"] != ib["rev"] or ia["body"] != ib["body"] or ia["title"] != ib["title"]:
                changed.append({
                    "fragment_code": code,
                    "from_rev": ia["rev"], "to_rev": ib["rev"],
                    "title_changed": ia["title"] != ib["title"],
                    "body_changed": ia["body"] != ib["body"],
                    "sources_changed": ia["sources"] != ib["sources"],
                })
        # 替代关系：b 中存在 replaces_code 指向 a 中片段世系的情况
        replacements = []
        for code, ib in items_b.items():
            if ib.get("replaces_code") and ib["replaces_code"] in items_a and code not in items_a:
                replacements.append({"old_fragment": ib["replaces_code"], "new_fragment": code})
        return {
            "from": {"release_id": a["id"], "version": a["version"], "content_sha": a["content_sha"]},
            "to": {"release_id": b["id"], "version": b["version"], "content_sha": b["content_sha"]},
            "added": added, "removed": removed, "changed": changed,
            "replacements": replacements,
            "identical": a["content_sha"] == b["content_sha"],
        }

    def _future_sessions_locked(self, theme_code: str, audience_code: str) -> list:
        now_iso = self.clock.now()
        return self.store.query_all(
            """SELECT * FROM sessions
               WHERE theme_code=? AND audience_code=? AND status IN ('scheduled','running')
                 AND starts_at >= ?
               ORDER BY starts_at""",
            (theme_code, audience_code, now_iso))

    def affected_sessions(self, release_id: int) -> dict:
        """列出受某版本影响的未来场次：将采用该版本，或在其撤回后无可用版本。"""
        rel = self.get_release(release_id)
        with self.store.lock:
            future = self._future_sessions_locked(rel["theme_code"], rel["audience_code"])
            will_use, fall_back, stranded = [], [], []
            for s in future:
                selected = self._resolve_release_id_locked(
                    rel["theme_code"], rel["audience_code"], s["starts_at"])
                entry = {"session_id": s["id"], "starts_at": s["starts_at"], "venue": s["venue"],
                         "status": s["status"]}
                if selected == release_id:
                    will_use.append(entry)
                elif selected is None:
                    stranded.append(entry)
                else:
                    sr = self.store.query_one("SELECT * FROM releases WHERE id=?", (selected,))
                    entry["effective_version"] = version_str(sr)
                    fall_back.append(entry)
            completed = self.store.query_all(
                """SELECT id AS session_id, starts_at, venue, frozen_release_id, frozen_at
                   FROM sessions
                   WHERE theme_code=? AND audience_code=? AND status='completed'
                     AND frozen_release_id=? ORDER BY starts_at""",
                (rel["theme_code"], rel["audience_code"], release_id))
        return {
            "release": {"release_id": rel["id"], "version": rel["version"], "status": rel["status"]},
            "future_sessions_using": [dict(s) for s in will_use],
            "future_sessions_fallback": [dict(s) for s in fall_back],
            "future_sessions_stranded": [dict(s) for s in stranded],
            "completed_sessions_on_this_basis": [dict(s) for s in completed],
        }


STATUS_LABELS = {
    "pending": "待核验",
    "confirmed": "已确认",
    "withdrawn": "已撤回",
    "archived": "已归档",
}

SESSION_STATE_LABELS = {
    "scheduled": "已排期",
    "running": "执行中",
    "completed": "已完成",
}
