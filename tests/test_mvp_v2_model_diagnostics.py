import copy
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from mvp_v2 import image_understanding as vision
from mvp_v2.script_draft import ScriptDraftError, _ground_primary_events, create_script_draft
from mvp_v2.text_providers import TextProviderError, TextProviderResponse, deepseek_http_provider, minimax_cli_provider
from tests import test_mvp_v2_script_draft as script_fixture


class ModelDiagnosticsTests(unittest.TestCase):
    def test_structure_only_conflict_keeps_original_and_reports_the_actual_reason(self):
        original = {"shots": [
            {"shot_no": "2", "visual_description": "主持人站在会场中景处。", "sound": "轻音乐"},
            {"shot_no": "3", "visual_description": "参与者在入口取阅手册。", "sound": "脚步声"},
        ]}
        unchanged = copy.deepcopy(original)
        for logic in ({"forbidden_terms": "主持人"}, {"blocked_materials": "主持人特写"}):
            with self.subTest(logic=logic):
                source = {"shots": [{
                    "shot_no": "2", "editing_logic": logic,
                    "planning_contract": {"generated_structure_only": True,
                        "primary_event": "核心节奏·中景承接",
                        "events": [{"event_id": "template_slot_002", "role": "primary", "text": "核心节奏·中景承接"}]},
                }]}
                with self.assertRaisesRegex(ScriptDraftError, "GROUNDING_CONFLICT.*2.*画面描述"):
                    _ground_primary_events(original, source)
                self.assertEqual(original, unchanged)

    def test_conflicting_sound_without_valid_basis_is_not_cleared_or_invented(self):
        payload = {"shots": [{"shot_no": "2", "visual_description": "光线掠过会场入口。", "sound": "主持人原声"}]}
        for basis in ("", "主持人报幕"):
            with self.subTest(basis=basis):
                original = copy.deepcopy(payload)
                with self.assertRaisesRegex(ScriptDraftError, "GROUNDING_CONFLICT.*2.*声音"):
                    _ground_primary_events(payload, {"shots": [{"shot_no": "2", "sound_basis": basis,
                        "editing_logic": {"forbidden_terms": "主持人"}, "planning_contract": {"generated_structure_only": True}}]})
                self.assertEqual(payload, original)

    def test_validation_failure_preserves_redacted_raw_response_and_processing_stages(self):
        raw_payload = script_fixture.ScriptDraftTests._response("正常活动画面")
        raw_payload["shots"][0].pop("voiceover")
        raw = json.dumps(raw_payload, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ScriptDraftError, "缺少 voiceover"):
                create_script_draft(script_fixture.ScriptDraftTests._plan(), quality_profile="client-review", text_model="anonymous",
                    text_command="", cache_dir=root, provider=lambda *_: raw)
            failures = [json.loads(path.read_text()) for path in (root / "diagnostics").glob("*.json")]
            self.assertEqual(len(failures), 1)
            record = failures[0]
            self.assertEqual(record["raw_response"], raw)
            self.assertEqual(record["failure_stage"], "validation")
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["stages"][0]["stage"], "parse")
            self.assertEqual(record["stages"][-1]["stage"], "validation")
            self.assertEqual(record["stages"][-1]["input"]["shots"][0]["visual_description"], "正常活动画面。")
            self.assertFalse(list(root.glob("*.json")), "invalid results must not remain reusable cache entries")

    def test_grounding_failure_retains_nonempty_normalized_response_in_evidence(self):
        plan = script_fixture.ScriptDraftTests._plan()
        plan.shot_plans = [{"shot_no": "1", "primary_event": "核心节奏·中景承接", "generated_structure_only": True,
            "events": [{"event_id": "template_slot_001", "role": "primary", "text": "核心节奏·中景承接"}]}]
        raw = json.dumps(script_fixture.ScriptDraftTests._response("主持人站在会场中景处。"), ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ScriptDraftError, "GROUNDING_CONFLICT"):
                create_script_draft(plan, quality_profile="client-review", text_model="anonymous", text_command="",
                    cache_dir=temporary, provider=lambda *_: raw)
            record = json.loads(next((Path(temporary) / "diagnostics").glob("*.json")).read_text())
            self.assertEqual(record["failure_stage"], "ground_primary_events")
            self.assertEqual(record["raw_response"], raw)
            self.assertEqual(record["stages"][-1]["input"]["shots"][0]["visual_description"], "主持人站在会场中景处。")

    def test_transport_records_actual_request_and_finish_without_exposing_credentials(self):
        secret = "anonymous-provider-secret-8257"
        envelope = {"id": "anonymous-request", "model": "anonymous-model", "choices": [{"finish_reason": "stop",
            "message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 13, "completion_tokens": 7}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), patch("mvp_v2.text_providers.urlopen", return_value=response) as opener:
            result = deepseek_http_provider()("anonymous prompt", "anonymous-model", 8192)
        self.assertIsInstance(result, TextProviderResponse)
        self.assertEqual(result, '{"ok":true}')
        sent = json.loads(opener.call_args.args[0].data)
        self.assertEqual(result.diagnostics["request"]["max_tokens"], sent["max_tokens"])
        self.assertEqual(result.diagnostics["request"]["thinking"], sent["thinking"])
        self.assertEqual(result.diagnostics["response"]["finish_reason"], "stop")
        self.assertEqual(result.diagnostics["response"]["usage"]["completion_tokens"], 7)
        self.assertEqual(json.loads(result.diagnostics["response"]["raw_response"]), envelope)
        self.assertNotIn(secret, json.dumps(result.diagnostics))
        self.assertNotIn("Authorization", json.dumps(result.diagnostics))

    def test_success_and_cached_replay_keep_actual_transport_limit_and_matching_prompt_digest(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(script_fixture.ScriptDraftTests._response("参与者沿入口指引入场"), ensure_ascii=False)}}]}).encode()
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "anonymous-key"}), patch(
            "mvp_v2.text_providers.urlopen", return_value=response,
        ) as opener:
            provider = deepseek_http_provider()
            values = dict(quality_profile="client-review", text_model="anonymous", text_command="", cache_dir=temporary)
            first = create_script_draft(script_fixture.ScriptDraftTests._plan(), **values,
                provider=lambda prompt, model, requested: provider(prompt, model, min(requested, 8192)))
            second = create_script_draft(script_fixture.ScriptDraftTests._plan(), **values,
                provider=lambda *_: self.fail("cached replay must not invoke transport"))
            records = [json.loads(path.read_text()) for path in (Path(temporary) / "diagnostics").glob("*.json")]
            self.assertEqual(opener.call_count, 2)
            self.assertEqual(first["script_rows"], second["script_rows"])
            self.assertTrue(any(row["cache_hit"] for row in records))
            for record in records:
                self.assertEqual(record["invocation"]["max_tokens_requested"], 65536)
                self.assertEqual(record["transport"]["request"]["max_tokens"], 8192)
                self.assertEqual(record["invocation"]["prompt_sha256"], record["transport"]["request"]["prompt_sha256"])
            ledger = [json.loads(line) for line in (Path(temporary) / "text_call_ledger.jsonl").read_text().splitlines()]
            for record in ledger:
                self.assertEqual(record["max_output_tokens_scope"], "provider_callable_request")
                self.assertEqual(record["transport_provider"], "deepseek-http")
                self.assertEqual(record["transport_request"]["max_tokens"], 8192)

    def test_transport_secret_remains_redacted_after_configuration_scope_exits(self):
        secret = "anonymous-retired-provider-key-4568"
        invalid = {**script_fixture.ScriptDraftTests._response("普通活动画面"), "provider_echo": secret}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(invalid, ensure_ascii=False)}}]}).encode()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            provider = deepseek_http_provider()
        with tempfile.TemporaryDirectory() as temporary, patch("mvp_v2.text_providers.urlopen", return_value=response):
            with self.assertRaises(ScriptDraftError):
                create_script_draft(script_fixture.ScriptDraftTests._plan(), quality_profile="client-review", text_model="anonymous",
                    text_command="", cache_dir=temporary, provider=provider)
            text = "".join(path.read_text() for path in Path(temporary).rglob("*") if path.is_file())
            self.assertNotIn(secret, text)

    def test_malformed_transport_envelope_keeps_raw_failure_evidence(self):
        response = MagicMock()
        raw = json.dumps({"choices": ["malformed choice"], "usage": {"completion_tokens": 4}})
        response.__enter__.return_value.read.return_value = raw.encode()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "anonymous-key"}), patch("mvp_v2.text_providers.urlopen", return_value=response):
            with self.assertRaises(TextProviderError) as caught:
                deepseek_http_provider()("anonymous", "anonymous", 64)
            self.assertEqual(caught.exception.diagnostics["response"]["raw_response"], raw)
            self.assertEqual(caught.exception.diagnostics["attempts"][0]["status"], "failed")

    def test_cli_malformed_choices_keeps_response_and_failure_stage(self):
        raw = json.dumps({"choices": ["malformed choice"]})
        result = subprocess.CompletedProcess([], 0, stdout=raw, stderr="")
        with patch("mvp_v2.text_providers.subprocess.run", return_value=result):
            with self.assertRaises(TextProviderError) as caught:
                minimax_cli_provider("mmx")("anonymous", "anonymous", 32)
        self.assertEqual(caught.exception.diagnostics["response"]["raw_response"], raw)
        self.assertEqual(caught.exception.diagnostics["attempts"][0]["status"], "failed")

    def test_cli_timeout_keeps_partial_response_before_retry_exhaustion(self):
        failure = subprocess.TimeoutExpired("mmx", 630, output=b'{"unfinished":', stderr=b'connection ended')
        with patch("mvp_v2.text_providers.subprocess.run", side_effect=failure), patch("mvp_v2.text_providers.time.sleep"):
            with self.assertRaises(TextProviderError) as caught:
                minimax_cli_provider("mmx")("anonymous", "anonymous", 32)
        self.assertEqual(len(caught.exception.diagnostics["attempts"]), 4)
        self.assertEqual(caught.exception.diagnostics["response"]["raw_response"], '{"unfinished":')
        self.assertEqual(caught.exception.diagnostics["attempts"][-1]["stderr"], 'connection ended')

    def test_vision_custom_command_does_not_claim_unsent_mmx_parameters(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"caption":"anonymous"}', stderr="")
        with patch("mvp_v2.image_understanding.subprocess.run", return_value=result) as runner:
            response = vision._run_minimax_vision("anonymous-command --fixture", Path("anonymous.png"), "", 17, prompt="not sent")
        self.assertEqual(runner.call_args.args[0], ["anonymous-command", "--fixture"])
        request = response.diagnostics["request"]
        self.assertEqual(request["operation"], "custom_command")
        self.assertEqual(request["parameters_available"], False)
        self.assertEqual(request["process_timeout_seconds"], runner.call_args.kwargs["timeout"])
        self.assertNotIn("prompt_sha256", request)
        self.assertNotIn("model_selection", request)

    def test_vision_timeout_keeps_partial_response_and_redacted_stderr(self):
        secret = "anonymous-vision-key"
        failure = subprocess.TimeoutExpired("mmx", 27, output=b'{"caption":', stderr=secret.encode())
        with patch("mvp_v2.image_understanding.subprocess.run", side_effect=failure):
            with self.assertRaises(vision.VisionProviderError) as caught:
                vision._run_minimax_vision("mmx", Path("anonymous.png"), secret, 17, prompt="anonymous")
        self.assertEqual(caught.exception.diagnostics["response"]["raw_response"], '{"caption":')
        self.assertNotIn(secret, json.dumps(caught.exception.diagnostics))

    def test_empty_truncated_transport_preserves_diagnostic_failure_and_redacts_secret(self):
        secret = "anonymous-provider-secret-7722"
        envelope = {"choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"completion_tokens": 8192}, "error_context": "Bearer " + secret}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), patch(
            "mvp_v2.text_providers.urlopen", return_value=response,
        ):
            provider = deepseek_http_provider()
            with self.assertRaisesRegex(TextProviderError, "finish_reason=length") as caught:
                provider("anonymous", "anonymous", 8192)
            self.assertEqual(caught.exception.diagnostics["response"]["finish_reason"], "length")
            self.assertNotIn(secret, json.dumps(caught.exception.diagnostics))
            with self.assertRaises(ScriptDraftError):
                create_script_draft(script_fixture.ScriptDraftTests._plan(), quality_profile="client-review", text_model="anonymous",
                    text_command="", cache_dir=temporary, provider=lambda prompt, model, limit: provider(prompt, model, min(limit, 8192)))
            record = json.loads(next((Path(temporary) / "diagnostics").glob("*.json")).read_text())
            self.assertEqual(record["failure_stage"], "provider")
            self.assertEqual(record["transport"]["request"]["max_tokens"], 8192)
            self.assertNotIn(secret, "".join(path.read_text() for path in Path(temporary).rglob("*") if path.is_file()))

    def test_vision_diagnostics_distinguish_truncation_missing_fields_and_malformed_json(self):
        complete = {"status": "pass", "caption": "参与者站在会场入口", "tags": ["人物"], "subjects": ["参与者"],
            "scene": "会场", "purpose": "现场参考", "quality": "清晰", "brand": "未见品牌", "evidence": ["可见入口"],
            "quality_flags": [], "negative_flags": []}
        cases = [
            (json.dumps({key: value for key, value in complete.items() if key != "brand"}), "VISION_RESPONSE_FIELDS_MISSING"),
            (json.dumps({"content": '{"status":"pass","caption":"入口', "stop_reason": "max_tokens"}), "VISION_RESPONSE_TRUNCATED"),
            (json.dumps(complete).replace(', "tags"', ' "tags"'), "VISION_RESPONSE_PARSE_FAILED"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "anonymous.png"
            Image.new("RGB", (48, 32), "blue").save(image)
            for index, (raw, code) in enumerate(cases):
                with self.subTest(code=code), patch("mvp_v2.image_understanding.minimax_credentials_available", return_value=True), patch(
                    "mvp_v2.image_understanding._run_minimax_vision", return_value=raw,
                ):
                    directory = root / str(index)
                    with self.assertRaisesRegex(vision.VisionProviderError, code):
                        vision.generate_minimax_project_image_analysis(image, diagnostics_dir=directory)
                    record = json.loads(next(directory.glob("*.json")).read_text())
                    self.assertEqual(record["raw_response"], raw)
                    self.assertEqual(record["error"]["code"], code)
                    self.assertNotIn("_parse_warning", record.get("parsed", {}))
                    if code == "VISION_RESPONSE_FIELDS_MISSING":
                        self.assertEqual(record["missing_fields"], ["brand"])

    def test_vision_parser_does_not_turn_unstructured_error_into_caption_or_tags(self):
        parsed, diagnostics = vision._parse_minimax_response("upstream operation failed")
        self.assertEqual(parsed, {})
        self.assertEqual(diagnostics["status"], "invalid")
        self.assertNotIn("_parse_warning", parsed)


if __name__ == "__main__":
    unittest.main()
