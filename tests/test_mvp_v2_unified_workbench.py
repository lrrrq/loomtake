from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from mvp_v2 import command


ROOT = Path(__file__).parents[1]


class UnifiedWorkbenchContractTests(unittest.TestCase):
    def test_assistant_route_activates_existing_page_and_uses_one_name(self):
        script = (ROOT / "mvp_v2" / "ui" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "mvp_v2" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('displayPage === "assistant" ? "agent" : displayPage', script)
        self.assertIn('id="page-agent"', page)
        self.assertIn("脚本助手", script + page)
        self.assertNotIn("受控助手", script + page)
        self.assertIn("项目助手", script + page)

    def test_project_assistant_is_a_thin_agent_api_surface(self):
        script = (ROOT / "mvp_v2" / "ui" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "mvp_v2" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="agent-message-form"', page)
        self.assertIn('id="agent-message"', page)
        self.assertIn('id="agent-session-status"', page)
        for endpoint in (
            "/api/agent/sessions",
            "/messages",
        ):
            self.assertIn(endpoint, script)
        for function, action in (
            ("confirmAgentAction", "confirm"), ("dismissAgentAction", "dismiss"),
            ("selectAgentImageCandidate", "image-selection"),
            ("selectAgentTemporaryTextBasis", "temporary-text-basis"),
        ):
            handler = script.split(f"async function {function}(", 1)[1].split("\nasync function ", 1)[0]
            self.assertIn("runAgentTaskAction(", handler)
            self.assertIn(f'"{action}"', handler)
        dispatcher = script.split("async function runAgentTaskAction(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("await api(`/api/agent/tasks/${encodeURIComponent(pending.taskId)}/${action}`", dispatcher)
        self.assertIn("ensureAgentProjectSession", script)
        self.assertIn("renderAgentConversation", script)
        session_loader = script.split("async function loadAgentSession(", 1)[1].split("\nasync function ", 1)[0]
        conversation_renderer = script.split("function renderAgentConversation(", 1)[1].split("\nasync function ", 1)[0]
        self.assertIn("await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}`)", session_loader)
        self.assertIn("renderAgentConversation(current)", session_loader)
        self.assertIn("session?.timeline", conversation_renderer)
        self.assertIn("response?.will_change", script)
        self.assertIn("response?.next_href", script)
        self.assertNotIn("这里只显示服务端记录的状态，不会创建任务或修改项目", page)
        self.assertNotIn("parameters", page.split('id="page-agent"', 1)[1].split('id="page-versions"', 1)[0])
        self.assertNotIn('agent-mode-stamp">只读', page)

    def test_script_and_image_closure_stays_a_thin_dto_adapter(self):
        script = (ROOT / "mvp_v2" / "ui" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "mvp_v2" / "ui" / "index.html").read_text(encoding="utf-8")
        styles = (ROOT / "mvp_v2" / "ui" / "app.css").read_text(encoding="utf-8")

        for element_id in (
            "formal-script-protection",
            "formal-script-lock-summary",
            "formal-script-quick-feedback",
            "formal-script-scope",
            "formal-script-scope-options",
            "formal-script-discard",
        ):
            self.assertIn(f'id="{element_id}"', page)
        for quick_feedback in (
            "减少航拍", "增加特写", "旁白更自然",
            "字幕更简洁", "减少重复", "减少套话", "节奏更紧凑",
        ):
            self.assertIn(f">{quick_feedback}</button>", page)
        self.assertIn('id="formal-script-protection-title">锁定镜头</h2>', page)
        self.assertIn("尚未锁定镜头", page + script)
        self.assertNotIn("ASSISTANT GUARD", page)
        self.assertNotIn("助手保护", page + script)
        for contract_field in (
            "shot_id",
            "assistant_locked",
            "camera_type",
            "shot_size",
            "camera_move",
            "image_reconfirmation_required",
            "analysis_task_id",
            "selected_shot_ids",
            "scope_required",
            "action_proposal",
            "source_match_required",
            "replan_required",
            "next_href",
            "match_explanation",
            "failure_summary",
        ):
            self.assertIn(contract_field, script)
        self.assertIn("/script/assistant-locks", script)
        self.assertIn('method: "PUT"', script)
        self.assertIn("locked_shot_ids", script)
        self.assertIn("formalImageCandidateHasRealPreview", script)
        self.assertIn('addEventListener("beforeunload"', script)
        self.assertIn('addEventListener("input", queueFormalScriptCellSave)', script)
        self.assertIn("flushFormalScriptChanges", script)
        self.assertIn('discardFormalScriptChanges', script)
        self.assertNotIn("/结构|删除.*镜/", script)
        self.assertIn("writing-mode: horizontal-tb", styles)
        self.assertIn("word-break: keep-all", styles)

    def test_cli_has_one_project_bound_execution_contract(self):
        ui = command.parser().parse_args(["ui", "--no-open"])
        self.assertEqual(ui.command, "ui")
        self.assertTrue(ui.no_open)
        generated = command.parser().parse_args([
            "run", "generate",
            "--project-id", "project-anonymous",
            "--plan-id", "plan-anonymous",
            "--plan-sha", "a" * 64,
        ])
        self.assertFalse(generated.confirm)
        stderr = StringIO()
        with patch("mvp_v2.command.load_local_secrets"), patch(
            "mvp_v2.command._session", side_effect=AssertionError("不应创建会话")
        ), redirect_stderr(stderr):
            exit_code = command.main([
                "run", "generate",
                "--project-id", "project-anonymous",
                "--plan-id", "plan-anonymous",
                "--plan-sha", "a" * 64,
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("必须显式提供 --confirm", stderr.getvalue())

    def test_both_launchers_forward_to_the_same_cli(self):
        primary = (ROOT / "启动M+脚本工具.command").read_text(encoding="utf-8")
        launch = (ROOT / "启动发布会工具.command").read_text(encoding="utf-8")
        self.assertIn('exec "$PROJECT_DIR/mplus" ui "$@"', primary)
        self.assertIn('exec "$PROJECT_DIR/启动M+脚本工具.command" "$@"', launch)
        self.assertNotIn("mvp_v2.local_ui", primary + launch)


if __name__ == "__main__":
    unittest.main()
