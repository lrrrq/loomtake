from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2 import workspace_store as schema
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore, sha256_text
from tests.test_mvp_v2_knowledge_service import KnowledgeServiceFixture


class DirectorSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = KnowledgeServiceFixture(self.root)
        self.tasks = TaskService(self.fixture.store)
        self.auth = self.fixture.editor_auth
        self.project_id = self.fixture.launch["project_id"]

    def tearDown(self):
        self.temporary.cleanup()

    def _session(self, *, auth=None, project_id=None):
        return self.tasks.create_agent_session(
            auth or self.auth, project_id=project_id or self.project_id,
        )["agent_session_id"]

    def _task(self, session_id, message="匿名消息", *, auth=None):
        token = auth or self.auth
        task = self.tasks.create_task(
            token, agent_session_id=session_id, task_type="formal_agent_workbench",
            payload_sha256=sha256_text(session_id + message), expected_project_version=0,
        )
        return self.tasks.transition_task(
            token, task_id=task["task_id"], expected_task_version=0,
            status="executing", details={"workbench": {"request": {"content": message}}},
        )

    @staticmethod
    def _ids(sessions):
        return {item["agent_session_id"] for item in sessions}

    @staticmethod
    def _rows(store, table):
        with store.connection() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_new_session_starts_empty_without_previous_sources_or_temporary_selection(self):
        previous = self._session()
        self.tasks.update_agent_session(
            self.auth, previous, expected_metadata_revision=0,
            title="已有创作讨论", draft_content="暂未发送的改稿指令",
            selected_source_ids=["brief:confirmed", "version:anonymous"],
        )
        old_task = self._task(previous, "只修改镜 2 和镜 5")
        self.tasks.record_task_progress(
            self.auth, task_id=old_task["task_id"], expected_task_version=1,
            details={"workbench": {"request": {"content": "只修改镜 2 和镜 5", "selected_shot_ids": ["shot-2", "shot-5"]}}},
        )
        self.tasks.conversation_summary(self.auth, previous, value={"content": "旧会话的临时选区"})

        current = self._session()
        session = self.tasks.get_agent_session(self.auth, current)
        self.assertEqual(session["title"], "")
        self.assertEqual(session["draft_content"], "")
        self.assertIsNone(session["selected_source_ids"])
        self.assertEqual(session["metadata_revision"], 0)
        self.assertFalse(session["archived"])
        self.assertNotIn("summary_json", session)
        self.assertNotIn("selected_shot_ids", session)
        self.assertEqual(self.tasks.conversation_summary(self.auth, current), {})
        self.assertEqual(self.tasks.list_agent_session_tasks(self.auth, current), [])
        listed = self.tasks.list_agent_sessions(self.auth, self.project_id)
        new_item = next(item for item in listed if item["agent_session_id"] == current)
        self.assertEqual(new_item["task_count"], 0)
        self.assertEqual(new_item["draft_content"], "")
        self.assertIsNone(new_item["selected_source_ids"])

    def test_unicode_title_and_original_message_search_find_only_matching_conversations(self):
        titled = self._session()
        self.tasks.update_agent_session(self.auth, titled, expected_metadata_revision=0, title="方向二 · 夜色🌙 Straße")
        spoken = self._session()
        original = '开头保持克制\n第二个方向叫"雨后城市"；路径 C:\\参考'
        self._task(spoken, original)
        unrelated = self._session()
        self._task(unrelated, "另一个项目议题，不包含目标词")

        for query, expected in (
            ("夜色🌙", {titled}),
            ("STRASSE", {titled}),
            ("雨后城市", {spoken}),
            (original, {spoken}),
            ('叫"雨后城市"', {spoken}),
            ("不存在的创作方向", set()),
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    self._ids(self.tasks.list_agent_sessions(self.auth, self.project_id, q=query)), expected,
                )
        readback = self.tasks.list_agent_session_tasks(self.auth, spoken)
        events = self.tasks.list_task_events(self.auth, readback[0]["task_id"])
        self.assertEqual(events[-1]["details"]["workbench"]["request"]["content"], original)

    def test_lists_are_actor_and_project_scoped_and_foreign_sessions_reject_reads_and_writes(self):
        mine = self._session()
        other_actor = self._session(auth=self.fixture.auth)
        other_project = self._session(auth=self.fixture.auth, project_id=self.fixture.exhibition["project_id"])
        self.tasks.update_agent_session(self.fixture.auth, other_actor, title="同项目其他人的私人讨论")
        self.tasks.update_agent_session(self.fixture.auth, other_project, title="另一个项目的私人讨论")
        self._task(other_actor, "不可泄露的私有消息", auth=self.fixture.auth)

        self.assertEqual(self._ids(self.tasks.list_agent_sessions(self.auth, self.project_id)), {mine})
        self.assertEqual(self._ids(self.tasks.list_agent_sessions(self.fixture.auth, self.project_id)), {other_actor})
        self.assertEqual(self.tasks.list_agent_sessions(self.auth, self.project_id, q="不可泄露"), [])
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.tasks.list_agent_sessions(self.auth, self.fixture.exhibition["project_id"])
        before = self._rows(self.fixture.store, "agent_sessions")
        for operation in (
            lambda: self.tasks.get_agent_session(self.auth, other_actor),
            lambda: self.tasks.get_agent_session(self.auth, other_project),
            lambda: self.tasks.update_agent_session(self.auth, other_actor, title="冒用身份改名"),
            lambda: self.tasks.update_agent_session(self.auth, other_actor, archived=True),
            lambda: self.tasks.conversation_summary(self.auth, other_actor),
            lambda: self.tasks.conversation_summary(self.auth, other_actor, value={"content": "冒用摘要"}),
            lambda: self._task(other_actor, "冒用身份发言"),
            lambda: self.tasks.get_agent_session(self.fixture.auth, mine),
        ):
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                operation()
        self.assertEqual(self._rows(self.fixture.store, "agent_sessions"), before)
        self.tasks.update_agent_session(self.fixture.auth, other_actor, archived=True)
        before_tasks = self._rows(self.fixture.store, "tasks")
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self._task(other_actor, "不能借归档响应探测另一操作者会话")
        self.assertEqual(self._rows(self.fixture.store, "tasks"), before_tasks)

    def test_archive_preserves_readable_history_and_restore_allows_new_task(self):
        session_id = self._session()
        task = self._task(session_id, "归档前的创作讨论")
        before_events = self.tasks.list_task_events(self.auth, task["task_id"])
        archived = self.tasks.update_agent_session(self.auth, session_id, expected_metadata_revision=0, archived=True)
        self.assertTrue(archived["archived"])
        self.assertEqual(self.tasks.list_agent_sessions(self.auth, self.project_id), [])
        self.assertEqual(self._ids(self.tasks.list_agent_sessions(self.auth, self.project_id, include_archived=True)), {session_id})
        self.assertEqual(self.tasks.list_task_events(self.auth, task["task_id"]), before_events)
        self.assertEqual(len(self.tasks.list_agent_session_tasks(self.auth, session_id)), 1)
        with self.assertRaisesRegex(WorkspaceServiceError, "SESSION_ARCHIVED"):
            self._task(session_id, "归档后不能发送")
        self.assertEqual(len(self.tasks.list_agent_session_tasks(self.auth, session_id)), 1)

        restored = self.tasks.update_agent_session(
            self.auth, session_id, expected_metadata_revision=archived["metadata_revision"], archived=False,
        )
        self.assertFalse(restored["archived"])
        next_task = self._task(session_id, "恢复后继续原讨论")
        self.assertNotEqual(next_task["task_id"], task["task_id"])
        self.assertEqual(len(self.tasks.list_agent_session_tasks(self.auth, session_id)), 2)
        self.assertEqual(self.tasks.list_task_events(self.auth, task["task_id"]), before_events)

    def test_archived_draft_and_source_selection_are_read_only_until_restore(self):
        session_id = self._session()
        self.tasks.update_agent_session(self.auth, session_id, draft_content="保留归档时的输入", selected_source_ids=["brief:confirmed"])
        archived = self.tasks.update_agent_session(self.auth, session_id, archived=True)
        for changes in ({"draft_content": "归档后覆盖输入"}, {"selected_source_ids": []}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(WorkspaceServiceError, "SESSION_ARCHIVED"):
                    self.tasks.update_agent_session(self.auth, session_id, **changes)
        self.assertEqual(self.tasks.get_agent_session(self.auth, session_id), archived)

    def test_metadata_conflict_preserves_draft_and_restored_service_reads_same_sources(self):
        session_id = self._session()
        saved = self.tasks.update_agent_session(
            self.auth, session_id, expected_metadata_revision=0,
            title="待续的长对话🌿", draft_content='原样保留："第二个方向"\n结尾不要改',
            selected_source_ids=["brief:confirmed", "version:anonymous"],
        )
        self.assertEqual(saved["metadata_revision"], 1)
        with self.assertRaisesRegex(WorkspaceServiceError, "SESSION_VERSION_CONFLICT"):
            self.tasks.update_agent_session(
                self.auth, session_id, expected_metadata_revision=0,
                title="过期标题", draft_content="旧页面内容", selected_source_ids=[],
            )
        restarted = TaskService(WorkspaceStore(self.root / "workspace.sqlite3"))
        self.assertEqual(restarted.get_agent_session(self.auth, session_id), saved)
        listed = restarted.list_agent_sessions(self.auth, self.project_id)[0]
        for key in ("title", "draft_content", "selected_source_ids", "metadata_revision"):
            self.assertEqual(listed[key], saved[key])
        selected_none = restarted.update_agent_session(
            self.auth, session_id, expected_metadata_revision=1, selected_source_ids=[],
        )
        self.assertEqual(selected_none["selected_source_ids"], [])
        selected_defaults = restarted.update_agent_session(
            self.auth, session_id, expected_metadata_revision=2, selected_source_ids=None,
        )
        self.assertIsNone(selected_defaults["selected_source_ids"])
        self.assertEqual(selected_defaults["draft_content"], saved["draft_content"])

    def test_summary_cache_roundtrips_without_replacing_raw_events_or_exposing_it_in_session_list(self):
        session_id = self._session()
        task = self._task(session_id, "用户原文：保留结尾，不采用第一个方向")
        original_events = self._rows(self.fixture.store, "task_events")
        summary = {
            "retired_digest": "a" * 64,
            "chunks": {"anonymous-message-digest": {
                "content": "采用第二方向，原结尾保留",
                "source_message_ids": [task["task_id"] + ":user"],
                "covers_message_ids": [task["task_id"] + ":user"],
            }},
        }
        self.assertEqual(self.tasks.conversation_summary(self.auth, session_id), {})
        self.assertEqual(self.tasks.conversation_summary(self.auth, session_id, value=summary), summary)
        restarted = TaskService(WorkspaceStore(self.root / "workspace.sqlite3"))
        self.assertEqual(restarted.conversation_summary(self.auth, session_id), summary)
        for item in (restarted.get_agent_session(self.auth, session_id), *restarted.list_agent_sessions(self.auth, self.project_id)):
            self.assertNotIn("summary_json", item)
            self.assertNotIn("chunks", item)
        self.assertEqual(restarted.conversation_summary(self.auth, session_id, value={}), {})
        self.assertEqual(self._rows(self.fixture.store, "task_events"), original_events)

    def test_migration_28_preserves_preexisting_sessions_tasks_and_event_bytes(self):
        legacy_root = self.root / "before-migration"
        legacy_root.mkdir()
        original_message = '旧会话原话：第二个方向\n保留"雨后"结尾。'
        with patch.object(schema, "_MIGRATIONS", tuple(item for item in schema._MIGRATIONS if item[0] <= 27)):
            legacy = KnowledgeServiceFixture(legacy_root)
            legacy_tasks = TaskService(legacy.store)
            session = legacy_tasks.create_agent_session(legacy.editor_auth, project_id=legacy.launch["project_id"])
            timestamp = "2026-01-01T00:00:00Z"
            task_id = "task_anonymous_pre_migration"
            with legacy.store.transaction() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")}
                self.assertNotIn("metadata_revision", columns)
                connection.execute(
                    """INSERT INTO tasks(task_id, agent_session_id, auth_session_id, actor_id, project_id,
                    task_type, payload_sha256, status, result_ref, error_code, lock_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'formal_agent_workbench', ?, 'completed', NULL, NULL, 2, ?, ?)""",
                    (task_id, session["agent_session_id"], session["auth_session_id"], session["actor_id"],
                     session["project_id"], sha256_text(original_message), timestamp, timestamp),
                )
                for sequence, status, details in (
                    (1, "received", {}),
                    (2, "executing", {"user_message": original_message}),
                    (3, "completed", {"response": {"content": "已比较两个方向，保留原结尾"}}),
                ):
                    connection.execute(
                        """INSERT INTO task_events(task_event_id, task_id, sequence_no, status, details_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (f"task_event_anonymous_legacy_{sequence}", task_id, sequence, status,
                         json.dumps(details, ensure_ascii=False, separators=(",", ":")), timestamp),
                    )
            before_sessions = self._rows(legacy.store, "agent_sessions")
            before_tasks = self._rows(legacy.store, "tasks")
            before_events = self._rows(legacy.store, "task_events")

        migrated_store = WorkspaceStore(legacy_root / "workspace.sqlite3")
        migrated = TaskService(migrated_store)
        migrated_store.initialize()
        with migrated_store.connection() as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, list(range(1, 29)))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_project_convention_events'",
            ).fetchone())
        after_session = self._rows(migrated_store, "agent_sessions")[0]
        self.assertEqual({key: after_session[key] for key in before_sessions[0]}, before_sessions[0])
        self.assertEqual(self._rows(migrated_store, "tasks"), before_tasks)
        self.assertEqual(self._rows(migrated_store, "task_events"), before_events)
        readback = migrated.get_agent_session(legacy.editor_auth, session["agent_session_id"])
        self.assertFalse(readback["archived"])
        self.assertEqual(readback["title"], "")
        self.assertEqual(readback["draft_content"], "")
        self.assertIsNone(readback["selected_source_ids"])
        self.assertEqual(readback["metadata_revision"], 0)
        self.assertEqual(migrated.conversation_summary(legacy.editor_auth, session["agent_session_id"]), {})
        self.assertEqual(migrated.list_agent_session_tasks(legacy.editor_auth, session["agent_session_id"])[0]["task_id"], task_id)
        self.assertEqual(migrated.list_task_events(legacy.editor_auth, task_id)[1]["details"]["user_message"], original_message)
        self.assertEqual(self._ids(migrated.list_agent_sessions(legacy.editor_auth, session["project_id"], q="第二个方向")), {session["agent_session_id"]})
        continued = migrated.create_task(
            legacy.editor_auth, agent_session_id=session["agent_session_id"],
            task_type="formal_agent_workbench", payload_sha256=sha256_text("迁移后继续"),
            expected_project_version=0,
        )
        self.assertEqual(continued["status"], "received")
        self.assertEqual(migrated.list_task_events(legacy.editor_auth, task_id)[1]["details"]["user_message"], original_message)


if __name__ == "__main__":
    unittest.main()
