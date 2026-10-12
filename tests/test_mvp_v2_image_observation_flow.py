from hashlib import sha256
from pathlib import Path
import unittest

from PIL import Image

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.formal_generation_service import FormalVisionProviderPolicy
from mvp_v2.formal_image_candidates import search_package_candidates, search_project_candidates
from mvp_v2.image_understanding import ProjectImageVisionResult
from mvp_v2.project_image_authority import (
    ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
    ProjectImagePoolService, ProviderImageAnalysisInput,
    ProviderProjectImageAnalysisCommandService, _validate_analysis_payload,
)
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.visual_assets import _score_visual_match
from tests import test_mvp_v2_formal_generation_service as generation_fixture


class ImageObservationFlowTests(unittest.TestCase):
    def setUp(self):
        self.base = generation_fixture.FormalGenerationServiceTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        base = self.base
        self.pool = ProjectImagePoolService(base.store, data_root=base.root / "runtime")
        self.project = base.projects.get_project(base.token, base.project["project_id"])
        self.binding = self.pool.current_confirmed_brief_binding(
            base.token, workspace_id=base.workspace_id, project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"],
        )

    def _context(self):
        return {
            "workspace_id": self.base.workspace_id, "project_id": self.project["project_id"],
            "brief_version_id": self.binding[0], "brief_snapshot_id": self.binding[1],
            "expected_project_lock_version": self.project["project_version"],
        }

    def _candidate(self, index, **observations):
        path = self.base.root / f"observation-{index}.png"
        Image.new("RGB", (1280, 720), (20 + index * 25, 40, 100)).save(path)
        return {
            "media_id": f"media-{index}", "path": path,
            "content_sha256": sha256(path.read_bytes()).hexdigest(),
            "source": {"file": "anonymous.xlsx", "sheet": "创意库", "row": index + 3},
            "caption": "顾客在现场交流互动", "tags": ["顾客", "互动"], "subjects": ["顾客"],
            "scene": "户外活动", "purpose": "现场互动", "quality": "主体清晰", "brand": "无",
            **observations,
        }

    def _record_uploaded(self, candidate, **observations):
        image_id = self.pool.adopt_local_upload(
            self.base.token, **self._context(), filename=candidate["path"].name,
            content=candidate["path"].read_bytes(),
        )
        record = ProviderProjectImageAnalysisCommandService(self.pool).record_analysis(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"],
            expected_project_lock_version=self.project["project_version"],
            submission=ProviderImageAnalysisInput(
                image_id=image_id, image_sha256=candidate["content_sha256"],
                brief_version_id=self.binding[0], brief_snapshot_id=self.binding[1], status="pass",
                caption=candidate["caption"], tags=tuple(candidate["tags"]), subjects=tuple(candidate["subjects"]),
                scene=candidate["scene"], purpose=candidate["purpose"], quality=candidate["quality"],
                brand=candidate["brand"], evidence=("匿名图片可见主体",),
                provider="anonymous", model="anonymous-vision", prompt_version="observation-test",
                visual_observations=observations,
            ),
        )
        return image_id, record

    def _match_interaction(self, pool):
        return search_project_candidates(
            slot={"镜号": "1", "画面描述": "顾客在现场交流互动", "镜头功能": "现场互动"},
            brief={"脚本类型": "品牌活动", "品牌或客户": "无", "禁忌或不能出现的内容": "禁用示例字样"},
            assets=pool.assets,
        )

    def test_ocr_forbidden_content_survives_both_upload_and_category_adoption(self):
        package = [self._candidate(1, ocr_text="禁用示例字样"), self._candidate(2)]
        package_ids = self.pool.adopt_package_images(
            self.base.token, **self._context(), package_id="anonymous", package_version=1,
            candidates=tuple(package),
        )
        uploaded_bad, _ = self._record_uploaded(self._candidate(3), ocr_text="禁用示例字样")
        uploaded_good, _ = self._record_uploaded(self._candidate(4), camera_angle="unknown")
        restarted = ProjectImagePoolService(self.base.store, data_root=self.base.root / "runtime")
        delivery = restarted.resolve_for_delivery(self.base.token, **self._context())
        self.assertEqual(len(delivery.assets), 4, "观察不可将已有 pass 图片静默移出权威素材池")
        result = self._match_interaction(delivery)
        accepted = {item["image_id"] for item in result["candidates"]}
        self.assertEqual(accepted, {package_ids[1], uploaded_good})
        rejected = {item["image_id"]: item["reason"] for item in result["rejected"]}
        for image_id in (package_ids[0], uploaded_bad):
            self.assertIn("Brief 禁止视觉", rejected[image_id])

    def test_observation_quality_flags_survive_legacy_enum_projection(self):
        values = [self._candidate(1, quality_flags=["out_of_focus"]),
                  self._candidate(2, negative_flags=["高风险"]), self._candidate(3)]
        ids = self.pool.adopt_package_images(
            self.base.token, **self._context(), package_id="anonymous", package_version=1,
            candidates=tuple(values),
        )
        restarted = ProjectImagePoolService(self.base.store, data_root=self.base.root / "runtime")
        delivery = restarted.resolve_for_delivery(self.base.token, **self._context())
        self.assertEqual(len(delivery.assets), 3)
        summaries = {asset.image_id: asset.business_summary for asset in delivery.assets}
        self.assertEqual(summaries[ids[0]].visual_observations["quality_flags"], ["out_of_focus"])
        self.assertEqual(summaries[ids[1]].visual_observations["negative_flags"], ["高风险"])
        result = self._match_interaction(delivery)
        # Legacy quality observations remain visible warnings for references;
        # explicitly prohibited content keeps its existing gate.
        self.assertEqual({item["image_id"] for item in result["candidates"]}, {ids[0], ids[2]})
        self.assertTrue(next(item for item in result["candidates"] if item["image_id"] == ids[0])["quality_warning"])
        self.assertEqual({item["image_id"] for item in result["rejected"]}, {ids[1]})

    def test_source_only_reference_remains_reviewable_after_adoption_without_fake_vision(self):
        source = self._candidate(
            1, caption="画面待识别", tags=["类别默认图"], subjects=[], scene="场景待识别",
            visual_analysis={}, source_semantic_text="顾客在现场交流互动",
            source_links=[{"source_file": "anonymous.xlsx", "source_sheet": "创意库", "source_row": 4,
                           "visual_subject": "顾客", "shot_function": "现场互动", "tags": "顾客 互动"}],
        )
        observed = self._candidate(2, visual_analysis={
            "caption": "顾客在现场交流互动", "tags": ["顾客", "互动"], "brand_names": ["无"],
        })
        before = search_package_candidates(
            slot={"镜号": "1", "画面描述": "顾客在现场交流互动", "镜头功能": "现场互动"},
            brief={"脚本类型": "品牌活动", "品牌或客户": "无"}, media=[source, observed],
        )
        before_by_id = {item["image_id"]: item for item in before["candidates"]}
        self.assertEqual(set(before_by_id), {"media-1", "media-2"})
        self.assertTrue(before_by_id["media-1"]["requires_human_review"])
        self.assertEqual(before_by_id["media-1"]["evidence_kind"], "source_row")
        ids = self.pool.adopt_package_images(
            self.base.token, **self._context(), package_id="anonymous", package_version=1,
            candidates=(source, observed),
        )
        restarted = ProjectImagePoolService(self.base.store, data_root=self.base.root / "runtime")
        delivery = restarted.resolve_for_delivery(self.base.token, **self._context())
        self.assertEqual(len(delivery.assets), 2)
        source_asset = next(asset for asset in delivery.assets if asset.image_id == ids[0])
        self.assertEqual(source_asset.business_summary.evidence_status, "source_reference")
        self.assertEqual(source_asset.business_summary.visual_observations, {})
        after = self._match_interaction(delivery)
        after_by_id = {item["image_id"]: item for item in after["candidates"]}
        self.assertEqual(set(after_by_id), set(ids), "类别页可供人工核对的参考图采用后应仍可核对")
        self.assertTrue(after_by_id[ids[0]]["requires_human_review"])
        self.assertEqual(after_by_id[ids[0]]["evidence_kind"], "source_row")
        self.assertIn("顾客", after_by_id[ids[0]]["summary"], "原表文字应作为来源依据保留")
        self.assertEqual(after_by_id[ids[1]]["evidence_kind"], "visual_analysis")

    def test_formal_provider_keeps_v4_observations_and_legacy_v3_on_restart(self):
        candidates = [self._candidate(1), self._candidate(2)]
        identities = {}
        for candidate in candidates:
            image_id = self.pool.adopt_local_upload(
                self.base.token, **self._context(), filename=candidate["path"].name,
                content=candidate["path"].read_bytes(),
            )
            identities[candidate["content_sha256"]] = image_id
        observed = {"main_subject": "顾客", "foreground_subjects": ["顾客"],
                    "background_subjects": ["建筑"], "camera_angle": "unknown",
                    "ocr_text": "匿名现场", "uncertainties": ["不能确认人物身份"]}
        calls = []
        def invoke(path):
            digest = sha256(path.read_bytes()).hexdigest()
            calls.append(digest)
            return ProjectImageVisionResult(
                "pass", "顾客在现场交流互动", ("顾客", "互动"), ("顾客",),
                "户外活动", "现场互动", "主体清晰", "无", ("画面可见主体",),
                visual_observations=observed if digest == candidates[0]["content_sha256"] else {},
            )
        service = self.base._service(vision=FormalVisionProviderPolicy(
            invoke=invoke, provider="anonymous", model="anonymous-vision", prompt_version="observation-test",
        ))
        response = service.analyze_pending_images(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], expected_project_version=self.project["project_version"],
        )
        self.assertEqual(response["automatic_understanding"]["processed"], 2)
        restarted = ProjectImagePoolService(self.base.store, data_root=self.base.root / "runtime")
        ready = restarted.resolve_for_planning(self.base.token, **self._context())
        self.assertEqual((ready.total, ready.passed), (2, 2))
        for image in ready.images:
            payload = restarted.artifacts.read_analysis(image.current_analysis.evidence)
            self.assertEqual(image.business_summary.evidence_status, "provider_generated")
            self.assertNotIn("reviewed_by_actor_id", payload)
            self.assertNotIn("selected_by_actor_id", payload)
            if image.image_id == identities[candidates[0]["content_sha256"]]:
                self.assertEqual(payload["schema_version"], "mplus-project-image-analysis/v4")
                self.assertEqual(image.business_summary.visual_observations["background_subjects"], ["建筑"])
                self.assertEqual(image.business_summary.visual_observations["ocr_text"], "匿名现场")
                self.assertNotIn("camera_angle", image.business_summary.visual_observations)
            else:
                self.assertEqual(payload["schema_version"], "mplus-project-image-analysis/v3")
                self.assertEqual(image.business_summary.visual_observations, {})
        service.analyze_pending_images(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], expected_project_version=self.project["project_version"],
        )
        self.assertEqual(len(calls), 2, "已有同 SHA 分析不得被重复调用")

    def test_v1_v2_v3_artifacts_preserve_original_evidence_roles(self):
        candidate = self._candidate(1)
        image_id, provider = self._record_uploaded(candidate)
        manual = ManualProjectImageAnalysisCommandService(self.pool).record_analysis(
            self.base.token, workspace_id=self.base.workspace_id,
            project_id=self.project["project_id"], expected_project_lock_version=self.project["project_version"],
            submission=ManualImageAnalysisInput(
                image_id, candidate["content_sha256"], self.binding[0], self.binding[1], "pass",
                candidate["caption"], tuple(candidate["tags"]), tuple(candidate["subjects"]),
                candidate["scene"], candidate["purpose"], candidate["quality"], "无", ("已核对实际画面",),
            ),
        )
        ready = self.pool.resolve_for_planning(self.base.token, **self._context())
        image = ready.images[0]
        for version, record, role in ((1, manual, "verified"), (2, provider, "provider_generated"),
                                      (3, manual, "verified"), (3, provider, "provider_generated")):
            with self.subTest(version=version, role=role):
                payload = self.pool.artifacts.read_analysis(record.evidence)
                payload["schema_version"] = f"mplus-project-image-analysis/v{version}"
                if version < 3:
                    for key in ("quality_flags", "negative_flags", "pixel_width", "pixel_height", "perceptual_hash"):
                        payload.pop(key)
                if version == 1:
                    payload.pop("analysis_origin")
                summary = _validate_analysis_payload(
                    payload, {"image_id": image_id, "image_sha256": image.image_sha256, "links": image.source_links},
                    self.base.workspace_id, self.project["project_id"], *self.binding, record,
                    candidate["path"].read_bytes(),
                )
                self.assertEqual(summary.evidence_status, role)
                self.assertEqual(summary.visual_observations, {})
                self.assertEqual((summary.pixel_width, summary.pixel_height), (1280, 720))
                self.assertEqual(summary.caption, candidate["caption"])

    def test_supplement_before_first_script_keeps_confirmed_brief(self):
        base = self.base
        resolver = RunArtifactResolver(base.root / "runs")
        workspace = FormalImageWorkspaceService(FormalImageWorkspaceServices(
            store=base.store, projects=base.projects, delivery=VersionDeliveryService(base.store, resolver),
            artifacts=resolver, images=self.pool,
        ), runs_root=base.root / "runs", source_root=base.source_root)
        with base.store.connection() as connection:
            before = connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]
        content = (Path(__file__).parent / "fixtures/v10_image_delivery/anonymous-conference-arrival.png").read_bytes()
        first = workspace.supplement_local_image(
            base.token, workspace_id=base.workspace_id, project_id=self.project["project_id"],
            expected_project_version=self.project["project_version"], filename="现场图片.png", content=content,
        )
        self.assertFalse(first["brief_reupload_required"])
        self.assertTrue(first["requires_visual_analysis"])
        self.assertEqual(first["project_version"], self.project["project_version"])
        current = base.projects.get_project(base.token, self.project["project_id"])
        self.assertEqual(current["current_brief_snapshot_id"], self.binding[1])
        self.assertFalse(current["current_version_id"])
        with base.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0], before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_versions").fetchone()[0], 0)

    def test_package_observations_and_flags_survive_adoption_and_restart(self):
        base = self.base
        candidates = []
        for index, color in enumerate(("red", "blue"), start=1):
            path = base.root / f"anonymous-{index}.png"
            Image.new("RGB", (640, 360), color).save(path)
            candidates.append({
                "media_id": f"media-{index}", "path": path, "content_sha256": sha256(path.read_bytes()).hexdigest(),
                "source": {"file": "anonymous.xlsx", "sheet": "创意库", "row": index + 3},
                "caption": "顾客在前景交流，建筑仅在远处背景", "tags": ["顾客", "建筑"], "subjects": ["顾客"],
                "scene": "户外活动", "quality": "640×360，参考图", "brand": "无",
                "quality_flags": {"low_resolution": True, "severe_blur": False},
                "negative_flags": {"forbidden_content": False},
                "main_subject": "顾客", "foreground_subjects": ["顾客"],
                "background_subjects": ["建筑"], "camera_angle": "地面平视", "uncertainties": ["背景文字不清晰"],
            })
        ids = self.pool.adopt_package_images(
            base.token, workspace_id=base.workspace_id, project_id=self.project["project_id"],
            brief_version_id=self.binding[0], brief_snapshot_id=self.binding[1],
            expected_project_lock_version=self.project["project_version"], package_id="anonymous", package_version=1,
            candidates=tuple(candidates),
        )
        restarted = ProjectImagePoolService(base.store, data_root=base.root / "runtime")
        readiness = restarted.resolve_for_planning(
            base.token, workspace_id=base.workspace_id, project_id=self.project["project_id"],
            brief_version_id=self.binding[0], brief_snapshot_id=self.binding[1],
            expected_project_lock_version=self.project["project_version"],
        )
        self.assertEqual(len(ids), 2)
        for item in readiness.images:
            summary = item.business_summary
            self.assertEqual(summary.quality_flags, ("low_resolution",))
            self.assertEqual(summary.negative_flags, ())
            self.assertEqual(summary.visual_observations["main_subject"], "顾客")
            self.assertEqual(summary.visual_observations["background_subjects"], ["建筑"])
            self.assertEqual(summary.evidence_status, "source_reference")
            self.assertEqual((summary.pixel_width, summary.pixel_height), (640, 360))
            self.assertTrue(item.source_links[0].source_document_id.startswith("package:anonymous@1:"))


class StructuredVisualMatchingTests(unittest.TestCase):
    def test_structured_roles_keep_existing_multi_subject_adaptation(self):
        args = ("嘉宾在建筑前互动", "主体为嘉宾，背景为建筑", ["嘉宾", "建筑", "互动"])
        old = _score_visual_match(*args, apply_launch_conflicts=False)
        observed = _score_visual_match(*args, apply_launch_conflicts=False, observations={
            "main_subject": "嘉宾", "foreground_subjects": ["嘉宾"], "background_subjects": ["建筑"],
        })
        self.assertEqual(observed, old)

    def test_background_tags_cannot_override_structured_subject_and_angle(self):
        result = _score_visual_match(
            "航拍建筑外观，完整呈现场馆", "活动场地有顾客和建筑", ["建筑", "航拍"],
            apply_launch_conflicts=False,
            observations={"main_subject": "顾客", "foreground_subjects": ["顾客"],
                          "background_subjects": ["建筑"], "camera_angle": "地面平视"},
        )
        self.assertLess(result["score"], 55)
        self.assertTrue(any("背景" in item for item in result["conflicts"]))
        self.assertTrue(any("机位" in item for item in result["conflicts"]))

    def test_unknown_observations_preserve_existing_interaction_adaptation(self):
        args = ("观众互动体验", "顾客在现场交流体验", ["互动", "体验"])
        old = _score_visual_match(*args)
        self.assertEqual(_score_visual_match(*args, observations={"camera_angle": "未知"}), old)
