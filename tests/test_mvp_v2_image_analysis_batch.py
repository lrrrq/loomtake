from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import project_image_authority as image_authority
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_image_authority import (
    ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
    ProjectImagePoolService, ProviderImageAnalysisInput,
    ProviderProjectImageAnalysisCommandService,
)
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_generation_service as generation_fixture


class ImageAnalysisBatchTests(unittest.TestCase):
    def setUp(self):
        self.base = generation_fixture.FormalGenerationServiceTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.pool = ProjectImagePoolService(self.base.store, data_root=self.base.root / "runtime")
        self.commands = ManualProjectImageAnalysisCommandService(self.pool)
        self.project = self.base.projects.get_project(self.base.token, self.base.project["project_id"])
        self.binding = self.pool.current_confirmed_brief_binding(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"],
        )
        self.provider_inputs = {}
        first = self._provider_image(1)
        second = self._reference_image(2)
        third = self._provider_image(3)
        self.image_ids = [first, second, third]
        images = self._images()
        self.selections = [{
            "image_key": image_id,
            "analysis_key": images[image_id].current_analysis.analysis_version_id,
        } for image_id in self.image_ids]

    def _context(self):
        return {
            "workspace_id": self.base.workspace_id, "project_id": self.project["project_id"],
            "brief_version_id": self.binding[0], "brief_snapshot_id": self.binding[1],
            "expected_project_lock_version": self.project["project_version"],
        }

    def _picture(self, number):
        path = self.base.root / f"batch-analysis-{number}.png"
        Image.new("RGB", (640, 360), (20 + number * 30, 60, 120)).save(path)
        return path

    def _provider_image(self, number, *, analyzed=True, status="pass", negative_flags=()):
        path = self._picture(number)
        image_id = self.pool.adopt_local_upload(
            self.base.token, **self._context(), filename=path.name, content=path.read_bytes(),
        )
        if not analyzed:
            return image_id
        submission = ProviderImageAnalysisInput(
            image_id=image_id, image_sha256=sha256(path.read_bytes()).hexdigest(),
            brief_version_id=self.binding[0], brief_snapshot_id=self.binding[1], status=status,
            caption=f"匿名图片{number}中的顾客交流。", tags=("顾客", "交流"), subjects=("顾客",),
            scene="户外活动", purpose="参考现场交流", quality="主体可见", brand="未见可辨品牌",
            evidence=(f"匿名观察{number}：前景有人物，建筑在背景。",),
            provider="fixture", model="fixture-vision", prompt_version="fixture-v4",
            quality_flags=("low_resolution",),
            negative_flags=negative_flags,
            visual_observations={
                "main_subject": "顾客", "foreground_subjects": ["顾客"],
                "background_subjects": ["建筑"], "camera_angle": "地面平视",
                "uncertainties": ["人物身份未确认"],
            },
        )
        # Seed a recorded provider result, without invoking a model or a provider.
        ProviderProjectImageAnalysisCommandService(self.pool).record_analysis(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"], submission=submission,
        )
        self.provider_inputs[image_id] = submission
        return image_id

    def _reference_image(self, number):
        path = self._picture(number)
        return self.pool.adopt_package_images(
            self.base.token, **self._context(), package_id="anonymous-batch", package_version=1,
            candidates=({
                "media_id": f"reference-{number}", "path": path,
                "content_sha256": sha256(path.read_bytes()).hexdigest(),
                "source": {"file": "anonymous.xlsx", "sheet": "创意库", "row": number + 3},
                "caption": "顾客在前景交流，建筑处于背景。", "tags": ["顾客", "交流"],
                "subjects": ["顾客"], "scene": "户外活动", "quality": "主体可见", "brand": "无",
                "main_subject": "顾客", "foreground_subjects": ["顾客"],
                "background_subjects": ["建筑"], "camera_angle": "地面平视",
                "source_semantic_text": "模板推荐标签，不证明顾客身份或活动名称。",
            },),
        )[0]

    def _images(self):
        ready = self.pool.resolve_for_planning(self.base.token, **self._context())
        return {image.image_id: image for image in ready.images}

    def _state(self):
        with self.base.store.connection() as connection:
            rows = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in (
                        "project_image_analysis_versions", "project_image_analysis_current_links",
                        "project_images", "project_image_package_source_links", "project_versions",
                    )}
        root = self.pool.artifacts.root / "project-image-analysis"
        files = sorted((str(path.relative_to(root)), sha256(path.read_bytes()).hexdigest())
                       for path in root.rglob("*") if path.is_file()) if root.exists() else []
        return rows, files

    def _confirm(self, selections, *, note="", key="batch-confirm", token=None):
        with patch.object(ProviderProjectImageAnalysisCommandService, "record_analysis", side_effect=AssertionError("人工确认不得触发机器分析")):
            return self.commands.confirm_existing_analyses(
                token or self.base.token, workspace_id=self.base.workspace_id,
                project_id=self.project["project_id"],
                expected_project_lock_version=self.project["project_version"],
                selections=selections, note=note, idempotency_key=key,
            )

    def test_two_visible_analyses_confirm_together_preserve_observations_and_common_note(self):
        before = self._images()
        before_state = self._state()
        original_payloads = {key: self.pool.artifacts.read_analysis(image.current_analysis.evidence)
                             for key, image in before.items()}
        self.assertEqual(original_payloads[self.image_ids[0]]["analysis_origin"], "provider")
        self.assertEqual(original_payloads[self.image_ids[1]]["analysis_origin"], "source_reference")
        self.assertTrue(all("reviewed_by_actor_id" not in payload for payload in original_payloads.values()))
        note = "已对照预览核对所选两张图片的分析结果。"
        confirmed = self._confirm(self.selections[:2], note=note)
        self.assertIsInstance(confirmed, tuple)
        self.assertEqual(len(confirmed), 2)
        after = self._images()
        self.assertEqual(after[self.image_ids[2]], before[self.image_ids[2]], "未选择图片保持原样")
        for selection in self.selections[:2]:
            image_id = selection["image_key"]
            old = original_payloads[image_id]
            current = after[image_id]
            new = self.pool.artifacts.read_analysis(current.current_analysis.evidence)
            self.assertEqual(current.business_summary.evidence_status, "verified")
            self.assertNotEqual(current.current_analysis.analysis_version_id, selection["analysis_key"])
            self.assertTrue(new.get("reviewed_by_actor_id"))
            self.assertTrue(new.get("reviewed_at"))
            self.assertIn(note, json.dumps(new, ensure_ascii=False))
            self.assertIn(selection["analysis_key"], json.dumps(new), "人工确认必须绑定所见分析版本")
            for field in (
                "caption", "tags", "subjects", "scene", "purpose", "quality", "brand", "evidence",
                "quality_flags", "negative_flags", "pixel_width", "pixel_height", "perceptual_hash",
                "visual_observations", "source_semantic_text", "visual_analysis_available",
            ):
                self.assertEqual(new.get(field), old.get(field), field)
            self.assertEqual(current.source_links, before[image_id].source_links)
            self.assertEqual(self.pool.artifacts.read_analysis(before[image_id].current_analysis.evidence), old)
        state = self._state()
        old_rows = before_state[0]["project_image_analysis_versions"]
        self.assertTrue(all(row in state[0]["project_image_analysis_versions"] for row in old_rows))
        self.assertEqual(len(state[0]["project_image_analysis_versions"]), len(old_rows) + 2)
        self.assertEqual(self._confirm(self.selections[:2], note=note), confirmed)
        self.assertEqual(self._state(), state, "同一确认重放不追加人工证据")

    def test_two_valid_and_one_stale_analysis_leave_entire_batch_unchanged(self):
        third = self.provider_inputs[self.image_ids[2]]
        self.commands.record_analysis(
            self.base.token, workspace_id=self.base.workspace_id, project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"],
            submission=ManualImageAnalysisInput(
                **{name: getattr(third, name) for name in ManualImageAnalysisInput.__dataclass_fields__},
            ),
        )
        self.assertNotEqual(self._images()[third.image_id].current_analysis.analysis_version_id, self.selections[2]["analysis_key"])
        before = self._state()
        with self.assertRaises(WorkspaceServiceError):
            self._confirm(self.selections, note="共同意见")
        self.assertEqual(self._state(), before)

    def test_already_reviewed_noop_reserves_its_operation_key_without_new_human_evidence(self):
        submission = self.provider_inputs[self.image_ids[0]]
        manual = self.commands.record_analysis(self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], expected_project_lock_version=self.project["project_version"],
            submission=ManualImageAnalysisInput(**{name: getattr(submission, name)
                for name in ManualImageAnalysisInput.__dataclass_fields__}))
        selection = [{"image_key": self.image_ids[0], "analysis_key": manual.analysis_version_id}]
        before = self._state()
        self.assertEqual(self._confirm(selection, key="noop-operation"), (manual,))
        self.assertEqual(self._state(), before)
        self.assertEqual(self._confirm(selection, key="noop-operation"), (manual,))
        with self.assertRaises(WorkspaceServiceError) as caught:
            self._confirm([self.selections[2]], key="noop-operation")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._state(), before)

    def test_manual_correction_persists_new_caption_and_rejection_without_rewriting_history(self):
        provider = self.provider_inputs[self.image_ids[0]]
        submission = ManualImageAnalysisInput(**{name: getattr(provider, name)
            for name in ManualImageAnalysisInput.__dataclass_fields__})
        kwargs = dict(workspace_id=self.base.workspace_id, project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"])
        first = self.commands.record_analysis(self.base.token, submission=submission, **kwargs)
        original = self.pool.artifacts.read_analysis(first.evidence)
        correction = replace(submission, caption="修正：画面是空场，不存在顾客。", status="blocked")
        second = self.commands.record_analysis(self.base.token, submission=correction,
            expected_analysis_key=first.analysis_version_id, **kwargs)
        self.assertNotEqual(first.analysis_version_id, second.analysis_version_id)
        self.assertEqual(self.pool.artifacts.read_analysis(second.evidence)["caption"], correction.caption)
        self.assertEqual(second.status, "blocked")
        self.assertEqual(self.pool.artifacts.read_analysis(first.evidence), original)
        before = self._state()
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.commands.record_analysis(self.base.token, submission=submission,
                expected_analysis_key=first.analysis_version_id, **kwargs)
        self.assertEqual(caught.exception.code, "IMAGE_ANALYSIS_VERSION_CONFLICT")
        self.assertEqual(self._state(), before)

    def test_confirmation_receipt_retains_original_counts_after_later_correction(self):
        confirmed = self._confirm(self.selections[:2], key="lost-response")
        receipt = self.commands.read_confirmation(self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], idempotency_key="lost-response")
        self.assertEqual(receipt["newly_confirmed_count"], 2)
        self.assertEqual(receipt["already_confirmed_count"], 0)
        provider = self.provider_inputs[self.image_ids[0]]
        self.commands.record_analysis(self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], expected_project_lock_version=self.project["project_version"],
            submission=replace(ManualImageAnalysisInput(**{name: getattr(provider, name)
                for name in ManualImageAnalysisInput.__dataclass_fields__}), caption="后来人工修正的内容"))
        before = self._state()
        self.assertEqual(self._confirm(self.selections[:2], key="lost-response"), confirmed)
        self.assertEqual(self._state(), before)
        self.assertEqual(self.commands.read_confirmation(self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], idempotency_key="lost-response"), receipt)

    def test_receipt_differentiates_new_and_already_confirmed_analyses(self):
        prior = self._confirm(self.selections[:1], key="first")
        self._confirm([{"image_key": self.image_ids[0], "analysis_key": prior[0].analysis_version_id},
            self.selections[1]], key="mixed")
        receipt = self.commands.read_confirmation(self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], idempotency_key="mixed")
        self.assertEqual((receipt["confirmed_count"], receipt["newly_confirmed_count"], receipt["already_confirmed_count"]), (2, 1, 1))

    def test_two_valid_and_one_analysis_missing_field_write_no_rows_or_artifacts(self):
        image = self._images()[self.image_ids[2]]
        third_evidence = image.current_analysis.evidence
        original_read = self.pool.artifacts.read_analysis

        def missing_field(evidence):
            payload = original_read(evidence)
            if evidence == third_evidence:
                payload = deepcopy(payload)
                payload.pop("quality")
            return payload

        before = self._state()
        with patch.object(self.pool.artifacts, "read_analysis", side_effect=missing_field):
            with self.assertRaises(WorkspaceServiceError):
                self._confirm(self.selections, note="共同意见")
        self.assertEqual(self._state(), before)

    def test_image_without_visible_analysis_cannot_be_filled_or_auto_analyzed_by_batch(self):
        pending_id = self._provider_image(4, analyzed=False)
        selections = [*self.selections[:2], {"image_key": pending_id, "analysis_key": None}]
        before = self._state()
        with self.assertRaises(WorkspaceServiceError):
            self._confirm(selections, note="共同意见")
        self.assertEqual(self._state(), before)
        self.assertIsNone(self._images()[pending_id].current_analysis)

    def test_blank_common_note_does_not_replace_existing_observation_evidence(self):
        images = self._images()
        original = {key: self.pool.artifacts.read_analysis(images[key].current_analysis.evidence)["evidence"]
                    for key in self.image_ids[:2]}
        result = self._confirm(self.selections[:2], note="")
        self.assertEqual(len(result), 2)
        images = self._images()
        for key in self.image_ids[:2]:
            payload = self.pool.artifacts.read_analysis(images[key].current_analysis.evidence)
            self.assertEqual(payload["evidence"], original[key])

    def test_second_artifact_promote_failure_rolls_back_all_rows_and_promoted_files(self):
        before = self._state()
        with self.base.store.connection() as connection:
            receipts_before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("manual_command_confirmations", "manual_command_idempotency", "manual_command_audits")}
        original_promote = image_authority._AnalysisArtifactBatch.promote
        promoted = []

        def fail_second(batch):
            if promoted:
                raise OSError("fixture: second analysis promotion failed")
            result = original_promote(batch)
            promoted.append(batch.evidence)
            self.assertTrue(batch.final_path.is_file(), "首个文件确实已进入最终目录")
            return result

        with patch.object(image_authority._AnalysisArtifactBatch, "promote", fail_second):
            with self.assertRaisesRegex(OSError, "second analysis promotion failed"):
                self._confirm(self.selections, note="共同确认")
        self.assertEqual(len(promoted), 1)
        self.assertEqual(self._state(), before, "数据库、已落盘文件和 staging 必须一起回滚")
        with self.base.store.connection() as connection:
            for table, rows in receipts_before.items():
                self.assertEqual([tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")], rows,
                    "失败批次不能留下成功操作回执")

    def test_explicit_forbidden_content_cannot_be_approved_with_valid_images(self):
        forbidden = self._provider_image(4, negative_flags=("forbidden_content",))
        analysis = self._images()[forbidden].current_analysis
        before = self._state()
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ANALYSIS_NOT_CONFIRMABLE"):
            self._confirm([*self.selections[:2], {
                "image_key": forbidden, "analysis_key": analysis.analysis_version_id,
            }])
        self.assertEqual(self._state(), before)

    def test_read_only_actor_cannot_confirm_even_visible_analyses(self):
        identity = LocalIdentityAdapter(self.base.store)
        workspace = self.base.projects.get_workspace(self.base.token, self.base.workspace_id)
        actor = identity.create_actor(
            self.base.token, workspace_id=self.base.workspace_id,
            display_name="匿名图片查看者", auth_source="local", role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        token = identity.issue_auth_session(actor_id=actor["actor_id"], token="batch-viewer-fixture")["auth_token"]
        self.assertEqual(self.base.projects.get_project(token, self.project["project_id"])["project_id"], self.project["project_id"])
        before = self._state()
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self._confirm(self.selections[:2], token=token)
        self.assertEqual(self._state(), before)

    def test_foreign_project_image_cannot_join_an_authorized_batch(self):
        workspace = self.base.projects.get_workspace(self.base.token, self.base.workspace_id)
        other = self.base.projects.create_project(
            self.base.token, workspace_id=self.base.workspace_id, name="匿名其他项目",
            category_id="launch", expected_workspace_version=workspace["workspace_version"],
        )
        with patch.object(self.base, "project", other):
            self.base._confirmed_brief()
        other = self.base.projects.get_project(self.base.token, other["project_id"])
        binding = self.pool.current_confirmed_brief_binding(
            self.base.token, workspace_id=self.base.workspace_id, project_id=other["project_id"],
            expected_project_lock_version=other["project_version"],
        )
        with patch.object(self, "project", other), patch.object(self, "binding", binding):
            foreign = self._provider_image(4)
            foreign_analysis = self._images()[foreign].current_analysis.analysis_version_id
        identity = LocalIdentityAdapter(self.base.store)
        workspace = self.base.projects.get_workspace(self.base.token, self.base.workspace_id)
        actor = identity.create_actor(
            self.base.token, workspace_id=self.base.workspace_id, project_id=self.project["project_id"],
            display_name="匿名项目审核员", auth_source="local", role="reviewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        token = identity.issue_auth_session(actor_id=actor["actor_id"], token="batch-reviewer-fixture")["auth_token"]
        before = self._state()
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ANALYSIS_SELECTION_INVALID"):
            self._confirm([*self.selections[:2], {
                "image_key": foreign, "analysis_key": foreign_analysis,
            }], token=token)
        self.assertEqual(self._state(), before)
        self.assertEqual(len(self._confirm(self.selections[:2], token=token)), 2)

    def test_current_analysis_key_cannot_be_reassigned_to_another_selected_image(self):
        selections = deepcopy(self.selections)
        selections[-1]["analysis_key"] = selections[0]["analysis_key"]
        before = self._state()
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ANALYSIS_VERSION_CONFLICT"):
            self._confirm(selections)
        self.assertEqual(self._state(), before)

    def test_same_operation_key_cannot_confirm_a_different_image_set(self):
        self._confirm(self.selections[:2], note="第一批共同意见", key="one-confirmation")
        before = self._state()
        with self.assertRaises(WorkspaceServiceError):
            self._confirm(self.selections[2:], note="另一批共同意见", key="one-confirmation")
        self.assertEqual(self._state(), before, "一个操作标识只能绑定一个明确的确认请求")

    def test_complete_low_confidence_provider_analysis_can_be_human_confirmed(self):
        image_id = self._provider_image(4, status="low_confidence")
        old = self._images()[image_id]
        result = self._confirm([{
            "image_key": image_id, "analysis_key": old.current_analysis.analysis_version_id,
        }], note="已核对主体和场景，保留原始不确定事项。")
        self.assertEqual([item.status for item in result], ["pass"])
        current = self._images()[image_id]
        self.assertEqual(current.business_summary.evidence_status, "verified")
        self.assertEqual(current.business_summary.visual_observations, old.business_summary.visual_observations)
        self.assertEqual(current.business_summary.quality_flags, old.business_summary.quality_flags)
        self.assertEqual(self.pool.artifacts.read_analysis(old.current_analysis.evidence)["status"], "low_confidence")

    def test_prior_manual_uncertainty_is_not_reported_as_completed_confirmation(self):
        provider = self.provider_inputs[self.image_ids[2]]
        values = {name: getattr(provider, name) for name in ManualImageAnalysisInput.__dataclass_fields__}
        values["status"] = "low_confidence"
        manual = self.commands.record_analysis(
            self.base.token, workspace_id=self.base.workspace_id, project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"],
            submission=ManualImageAnalysisInput(**values),
        )
        before = self._state()
        selected = [*self.selections[:2], {
            "image_key": self.image_ids[2], "analysis_key": manual.analysis_version_id,
        }]
        try:
            result = self._confirm(selected, note="本轮共同确认")
        except WorkspaceServiceError:
            self.assertEqual(self._state(), before, "无法批量处理的待复核项必须整批拒绝")
        else:
            self.assertEqual([item.status for item in result], ["pass"] * len(selected),
                             "已有人工证据不代表 low_confidence 已通过本轮确认")


if __name__ == "__main__":
    unittest.main()
