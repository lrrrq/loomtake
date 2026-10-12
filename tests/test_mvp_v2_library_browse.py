from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin
from openpyxl import load_workbook
from pypdf import PdfReader

from mvp_v2.asset_package import create_asset_package, bundle_asset_package_v3
from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_image_workspace import ImageShotSelection
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.run_contract import sha256_file
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests.test_mvp_v2_asset_package import _write_source_with_reference_image
from tests import test_mvp_v2_project_image_authority as image_authority_tests
from tests import test_mvp_v2_formal_review_workspace as review_workspace_tests


def library_confirmation_agent(*, root, store, projects, service):
    tasks = TaskService(store)
    readonly = ReadonlyAgentService(ReadonlyAgentServices(
        store=store, identity=LocalIdentityAdapter(store), projects=projects, tasks=tasks,
        versions=VersionService(store), artifacts=RunArtifactResolver(root / "runs"),
    ))
    return FormalExecutableAgentService(
        ExecutableAgentServices(
            readonly=readonly, projects=projects, tasks=tasks,
            confirmations=ActionConfirmationService(store, b"l" * 32),
            idempotency=ToolIdempotencyService(store), brief_store=BriefArtifactStore(root / "data"),
            backend=FormalExecutionBackend(),
        ), generation=None, advisor=None, writes_enabled=True, default_images=service,
    )


def build_anonymous_library_package(root: Path, *, caption="外场入口人群在展板前交流", tags=("外场", "人群", "交流")) -> Path:
    """Thirty external image identities, one static copy and one forbidden example; no Provider."""
    root.mkdir(parents=True, exist_ok=True)
    source, media = root / "source", root / "library-media"
    source.mkdir()
    media.mkdir()
    _write_source_with_reference_image(source / "M+发布会类通用脚本库.xlsx", root / "linked.png")
    manifests, analyses = [], []
    for index in range(29):
        image = Image.new("RGB", (96, 64), (index * 7 % 255, index * 19 % 255, 90))
        ImageDraw.Draw(image).rectangle((index, 5, index + 15, 45), fill="white")
        path = media / f"anonymous-{index:02}.png"
        image.save(path)
        manifests.append({"image_id": str(index), "原图路径": str(path)})
        analyses.append({"image_id": str(index), "caption": f"匿名编号{index:02}：{caption}",
                         "tags": list(tags), "brand_names": ["无"],
                         "source": "anonymous_fixture", "quality_flags": ["forbidden_content"] if index == 28 else []})
    duplicate = media / "same-pixels-another-source.png"
    with Image.open(media / "anonymous-00.png") as image:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("source", "anonymous copy")
        image.save(duplicate, pnginfo=metadata)
    manifests.append({"image_id": "copy", "原图路径": str(duplicate)})
    analyses.append({**analyses[0], "image_id": "copy"})
    (media / "image_manifest.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in manifests))
    (media / "image_analysis.minimax.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in analyses))
    seed = create_asset_package(root / "seed", package_id="anonymous-gallery", source_root=source,
                                media_roots=[media], metadata_roots=[media])
    return bundle_asset_package_v3(seed, root / "package", package_version=1)


class LibraryBrowseTests(unittest.TestCase):
    def setUp(self):
        base = image_authority_tests.ProjectImageAuthorityTests("runTest")
        base.setUp()
        self.addCleanup(base.tearDown)
        with base.store.transaction() as connection:
            connection.execute("UPDATE projects SET category_id='launch'")
        self.base = base
        self.confirmed, _ = base._confirmed_first()
        self.root = base.root
        self.project_id = base.first["project_id"]
        self.args = {"workspace_id": base.workspace_id, "project_id": self.project_id,
                     "expected_project_version": 2}
        self.package = build_anonymous_library_package(self.root)
        self.tasks = TaskService(base.store)
        self.service = FormalDefaultImageService(
            projects=base.projects, tasks=self.tasks, delivery=None, artifacts=None,
            image_pool=base.pool, image_workspace=None,
            asset_package_resolver=lambda _: self.package, writes_enabled=True,
        )

    def browse(self, **overrides):
        return self.service.candidates(self.base.token, **{**self.args, "view": "library", **overrides})

    def test_full_library_pages_include_unlinked_images_and_keep_static_groups_together(self):
        first, second = self.browse(), self.browse(page=2)
        self.assertEqual(first["selection_mode"], "category_library_multi")
        self.assertEqual(first["page_size"], 24)
        self.assertEqual(first["displayed_groups"], 24)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["displayed_groups"], second["page_groups"])
        self.assertEqual(second["displayed_images"], len(second["library_candidates"]))
        self.assertGreater(first["inventory_records"], first["total_images"])
        self.assertEqual(first["status_counts"]["forbidden"], 1)
        self.assertTrue(first["status_counts_overlap"])
        first_groups = {x["duplicate_group_id"] for x in first["library_candidates"]}
        second_groups = {x["duplicate_group_id"] for x in second["library_candidates"]}
        self.assertFalse(first_groups & second_groups)
        self.assertEqual(len(first_groups | second_groups), first["total_groups"])
        all_candidates = first["library_candidates"] + second["library_candidates"]
        self.assertEqual(len(all_candidates), first["total_images"])
        self.assertFalse(any("编号28" in x["caption"] for x in all_candidates))
        self.assertTrue(any(not x["template_recommendations"] for x in second["library_candidates"]))
        self.assertTrue(any(x["duplicate_count"] for x in all_candidates))
        found = self.browse(query="编号27")
        self.assertEqual(found["total_groups"], 1)
        self.assertEqual(found["total_images"], 1)
        candidate = found["library_candidates"][0]
        content, mime = self.service.preview(self.base.token, **self.args, media_id=candidate["candidate_key"])
        self.assertEqual(mime, "image/png")
        self.assertEqual(Image.open(BytesIO(content)).size, (96, 64))
        self.assertNotIn(str(self.root), json.dumps(found["library_candidates"]))

    def test_later_page_selection_is_proposed_adopted_and_stale_version_is_rejected(self):
        later = self.browse(page=2)["library_candidates"][0]
        proposal = self.service.propose(self.base.token, **self.args, agent_session_id="",
                                        selections=[{"shot_no": "library:1", "candidate_key": later["candidate_key"]}],
                                        idempotency_key="later-page")
        self.assertEqual(proposal["status"], "proposed")
        parameters = next(event["details"]["internal_formal_parameters"] for event in
                          self.tasks.list_task_events(self.base.token, proposal["task_id"])
                          if event["details"].get("internal_formal_parameters"))
        self.assertEqual(parameters["selections"][0]["content_sha256"], later["content_sha256"])
        project = self.base.projects.get_project(self.base.token, self.project_id)
        for field, value in (("content_sha256", "0" * 64), ("sources", [{"file": "wrong.xlsx", "sheet": "错误", "row": 1}])):
            with self.subTest(drift=field):
                invalid = deepcopy(parameters)
                invalid["selections"][0][field] = value
                with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_DRIFT"):
                    self.service.execute(self.base.token, project=project, parameters=invalid)
        agent = library_confirmation_agent(root=self.root, store=self.base.store, projects=self.base.projects, service=self.service)
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            agent.confirm_action(self.base.token, proposal["task_id"], proposal_digest="wrong-digest")
        result = agent.confirm_action(self.base.token, proposal["task_id"], proposal_digest=proposal["response"]["proposal_digest"])
        self.assertEqual(result["response"]["adopted_image_count"], 1)
        replay = agent.confirm_action(self.base.token, proposal["task_id"], proposal_digest=proposal["response"]["proposal_digest"])
        self.assertEqual(replay["task"]["status"], "completed")
        with self.base.store.connection() as connection:
            row = connection.execute("SELECT image_sha256 FROM project_images WHERE project_id=?", (self.project_id,)).fetchone()
        self.assertEqual(row[0], later["content_sha256"])
        with self.base.store.transaction() as connection:
            connection.execute("UPDATE projects SET lock_version=lock_version+1 WHERE project_id=?", (self.project_id,))
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.browse()

    def test_script_browse_later_page_keeps_mapping_and_allows_explicit_subset(self):
        original_context = self.service._context

        def scripted_context(*args):
            project, context, plan, _, binding = original_context(*args)
            plan.brief = {"脚本类型": "发布会"}
            plan.shot_plans = [{"primary_event": "现场人气"}] * 3
            draft = SimpleNamespace(script_rows=[{"镜号": str(i), "画面描述": "外场入口人群交流"} for i in range(1, 4)], detail_rows=[])
            return project, context, plan, draft, binding

        with patch.object(self.service, "_context", side_effect=scripted_context):
            page = self.browse(shot_no="2", page=2)
            self.assertEqual(page["selection_mode"], "category_library_browse")
            self.assertEqual(len(page["shots"]), 3)
            later = page["library_candidates"][0]
            selections = [{"shot_no": shot["shot_no"], "candidate_key": shot["selected_candidate_key"]} for shot in page["shots"]]
            selections[1]["candidate_key"] = later["candidate_key"]
            proposal = self.service.propose(self.base.token, **self.args, agent_session_id="", selections=selections, idempotency_key="script-later-page")
            self.assertEqual(proposal["status"], "proposed")
            subset = self.service.propose(self.base.token, **self.args, agent_session_id="", selections=selections[:1], idempotency_key="selected-subset")
            self.assertEqual(subset["response"]["parameters"]["pending_shot_count"], 2)
            for invalid in (selections[:1] * 2, [{"shot_no": "99", "candidate_key": later["candidate_key"]}]):
                with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_DECISION_MISMATCH"):
                    self.service.propose(self.base.token, **self.args, agent_session_id="", selections=invalid, idempotency_key="invalid-shots")
            with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
                self.browse(shot_no="999")

    def test_existing_single_source_adoption_appends_explicit_location_once(self):
        package, media = self.service._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
        candidate = deepcopy(next(item for item in media if "编号00" in item["caption"]))
        _, _, plan, _, _ = self.service._context(self.base.token, self.base.workspace_id, self.project_id, 2)
        args = dict(workspace_id=self.base.workspace_id, project_id=self.project_id,
            brief_version_id=plan.confirmed_brief_input["brief_version_id"],
            brief_snapshot_id=plan.confirmed_brief_input["brief_snapshot_id"],
            expected_project_lock_version=2, package_id=package["package_id"],
            package_version=package["package_version"])
        def adopt():
            return self.base.pool.adopt_package_images(self.base.token, **args, candidates=(candidate,))
        identity = adopt()
        def rows(table):
            with self.base.store.connection() as connection:
                return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        original_links = rows("project_image_package_source_links")
        original_analysis = rows("project_image_analysis_versions")
        self.assertEqual(len(original_links), 1)
        self.assertEqual(len(original_analysis), 1)
        # Compatibility fixture: authoritative package metadata now exposes the
        # second row that an older adoption persisted only in the package.
        origin = candidate["package_sources"][0]
        origin["sources"].append({"file": "anonymous-second.xlsx", "sheet": "模版", "row": 8})
        self.assertEqual(adopt(), identity)
        updated_links = rows("project_image_package_source_links")
        self.assertEqual(len(updated_links), 2)
        self.assertEqual(updated_links[0], original_links[0])
        updated_analysis = rows("project_image_analysis_versions")
        self.assertEqual(updated_analysis[0], original_analysis[0])
        self.assertEqual(len(updated_analysis), 2)
        for key in ("status", "created_by_actor_id", "created_at"):
            self.assertEqual(updated_analysis[1][key], original_analysis[0][key])
        self.assertEqual(adopt(), identity)
        self.assertEqual(rows("project_image_package_source_links"), updated_links)
        self.assertEqual(rows("project_image_analysis_versions"), updated_analysis)
        self.assertEqual(len(rows("project_images")), 1)

    def test_legacy_same_content_identities_return_one_project_image_and_preserve_sources(self):
        package, media = self.service._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
        original = deepcopy(next(item for item in media if "编号00" in item["caption"]))
        alias = deepcopy(original)
        alias["media_id"] = "anonymous-legacy-second-identity"
        alias["source"] = {"file": "anonymous-second-source.xlsx", "sheet": "参考", "row": 12}
        alias["source_links"] = []
        other = deepcopy(next(item for item in media if "编号01" in item["caption"]))
        self.assertEqual(original["content_sha256"], sha256_file(original["path"]))
        self.assertEqual(original["content_sha256"], alias["content_sha256"])
        self.assertNotEqual(original["media_id"], alias["media_id"])
        choices = (original, alias, other)
        # The current bundler deduplicates SHA before assigning IDs. This DTO-only
        # compatibility fixture exercises legacy distinct IDs without weakening
        # that package contract; matching, adoption, DB mapping and Agent stay real.
        with patch.object(self.service, "_package_media", return_value=(package, list(choices))):
            proposal = self.service.propose(
                self.base.token, **self.args, agent_session_id="",
                selections=[{"shot_no": f"library:{i}", "candidate_key": item["media_id"]}
                            for i, item in enumerate(choices, 1)],
                idempotency_key="legacy-shared-content",
            )
            agent = library_confirmation_agent(root=self.root, store=self.base.store,
                projects=self.base.projects, service=self.service)
            result = agent.confirm_action(self.base.token, proposal["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"])
        response = result["response"]
        self.assertEqual(result["task"]["status"], "completed")
        self.assertEqual(response["adopted_image_count"], 2)
        mapped = {item["shot_no"]: item for item in response["adopted_selections"]}
        self.assertEqual(set(mapped), {"library:1", "library:2", "library:3"})
        self.assertEqual(mapped["library:1"]["image_key"], mapped["library:2"]["image_key"])
        self.assertNotEqual(mapped["library:1"]["image_key"], mapped["library:3"]["image_key"])
        for index, item in enumerate(choices, 1):
            self.assertEqual(mapped[f"library:{index}"]["content_sha256"], item["content_sha256"])
        with self.base.store.connection() as connection:
            images = connection.execute(
                "SELECT project_image_id, image_sha256 FROM project_images WHERE project_id=?",
                (self.project_id,),
            ).fetchall()
            links = connection.execute(
                """SELECT media_id, content_sha256, source_file, source_sheet, source_row
                FROM project_image_package_source_links WHERE project_image_id=?""",
                (mapped["library:1"]["image_key"],),
            ).fetchall()
        self.assertEqual(len(images), 2)
        self.assertEqual({tuple(row) for row in links}, {
            (item["media_id"], item["content_sha256"], item["source"]["file"],
             item["source"]["sheet"], item["source"]["row"])
            for item in (original, alias)
        })
        _, _, plan, _, _ = self.service._context(self.base.token, self.base.workspace_id, self.project_id, 2)
        pool = self.base.pool.resolve_for_delivery(
            self.base.token, workspace_id=self.base.workspace_id, project_id=self.project_id,
            brief_version_id=plan.confirmed_brief_input["brief_version_id"],
            brief_snapshot_id=plan.confirmed_brief_input["brief_snapshot_id"],
            expected_project_lock_version=2,
        )
        shared = next(item for item in pool.assets if item.image_id == mapped["library:1"]["image_key"])
        self.assertEqual(len(shared.source_links), 2)
        self.assertEqual({link.source_label for link in shared.source_links},
                         {item["source"]["file"] for item in (original, alias)})

    def test_browse_and_later_page_proposal_preserve_cross_project_and_blob_checks(self):
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.browse(workspace_id="other-workspace")
        candidate = self.browse(page=2)["library_candidates"][0]
        wrong_session = self.tasks.create_agent_session(self.base.token, project_id=self.base.second["project_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.service.propose(self.base.token, **self.args, agent_session_id=wrong_session["agent_session_id"],
                                 selections=[{"shot_no": "library:1", "candidate_key": candidate["candidate_key"]}], idempotency_key="cross-project")
        _, media = self.service._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
        path = Path(next(x["path"] for x in media if x["media_id"] == candidate["candidate_key"]))
        path.write_bytes(path.read_bytes() + b"anonymous-drift")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_BLOB_DRIFT"):
            self.service.propose(self.base.token, **self.args, agent_session_id="", selections=[{"shot_no": "library:1", "candidate_key": candidate["candidate_key"]}], idempotency_key="drift")


class LibraryDeliveryJourneyTests(unittest.TestCase):
    def test_later_page_image_survives_confirmation_review_excel_pdf_and_restart(self):
        base = review_workspace_tests.FormalReviewWorkspaceTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        images = base.base
        workspace = images.workspace
        token = base.token
        scope = {"workspace_id": base.workspace_id, "project_id": base.project_id}
        current = workspace.read_current(token, **scope)
        count = len(current["shots"])
        self.assertGreaterEqual(count, 8)
        first_no = current["shots"][0]["shot_no"]
        before_context = images.delivery.resolve(token, base.project_id, current["base_version_id"])
        parent_draft = images.resolver.resolve_stage_artifact(before_context.run_id, "script_draft").path
        parent_sha = sha256_file(parent_draft)
        parent_rows = json.loads(parent_draft.read_text())["script_rows"]
        package = build_anonymous_library_package(
            base.root / "later-page-library", caption=current["shots"][0]["visual"],
            tags=("品牌标识", "主视觉", "logo"),
        )
        service = FormalDefaultImageService(
            projects=images.projects, tasks=TaskService(base.store), delivery=images.delivery,
            artifacts=images.resolver, image_pool=images.pool, image_workspace=workspace,
            asset_package_resolver=lambda _: package, writes_enabled=True,
        )
        page = service.candidates(token, **scope, expected_project_version=current["project_version"],
                                  view="library", page=2, shot_no=first_no)
        self.assertGreater(page["total_groups"], 24)
        later = page["library_candidates"][0]
        selected = [{"shot_no": shot["shot_no"], "candidate_key": shot["selected_candidate_key"] or shot["candidates"][0]["candidate_key"]}
                    for shot in page["shots"] if shot["state"] == "ready"]
        next(item for item in selected if item["shot_no"] == first_no)["candidate_key"] = later["candidate_key"]
        proposal = service.propose(token, **scope, expected_project_version=current["project_version"],
                                    agent_session_id="", selections=selected, idempotency_key="journey-library-adoption")
        agent = library_confirmation_agent(root=base.root, store=base.store, projects=images.projects, service=service)
        adopted = agent.confirm_action(token, proposal["task_id"], proposal_digest=proposal["response"]["proposal_digest"])
        self.assertEqual(adopted["task"]["status"], "completed", adopted.get("response"))
        self.assertGreaterEqual(adopted["response"]["adopted_image_count"], 1)
        self.assertEqual(adopted["response"]["base_version_id"], current["base_version_id"])
        self.assertEqual(adopted["response"]["project_version"], current["project_version"])
        adopted_by_shot = {item["shot_no"]: item for item in adopted["response"]["adopted_selections"]}
        self.assertEqual(set(adopted_by_shot), {item["shot_no"] for item in selected})
        first_mapping = adopted_by_shot[first_no]
        self.assertEqual(first_mapping["content_sha256"], later["content_sha256"])
        ready = workspace.read_current(token, **scope)
        first_shot = next(item for item in ready["shots"] if item["shot_no"] == first_no)
        chosen = next(item for item in first_shot["candidates"] if item["image_key"] == first_mapping["image_key"])
        self.assertEqual(chosen["content_sha256"], first_mapping["content_sha256"])
        # Adoption supplies a pending user choice, leaving the saved image and
        # its confirmation unchanged until the separate image-version action.
        self.assertEqual(ready["base_version_id"], current["base_version_id"])
        self.assertEqual(first_shot["selected_image_key"], current["shots"][0]["selected_image_key"])
        self.assertNotEqual(chosen["image_key"], first_shot["selected_image_key"])
        self.assertEqual(first_shot["confirmed"], current["shots"][0]["confirmed"])
        self.assertFalse(adopted["response"]["review_eligible"])
        self.assertFalse(adopted["response"]["delivery_eligible"])
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CONFIRMATION_REQUIRED"):
            ImageShotSelection(first_no, chosen["image_key"], False,
                               candidate_snapshot_digest=first_shot["candidate_snapshot_digest"])
        selections = tuple(ImageShotSelection(
            item["shot_no"], chosen["image_key"] if item["shot_no"] == first_no else item["selected_image_key"], True,
            candidate_snapshot_digest=item["candidate_snapshot_digest"],
            reuse_reason="匿名旅程中确认同一参考构图的各镜用途",
        ) for item in ready["shots"])
        preview = workspace.preview_version(token, **scope, base_version_id=ready["base_version_id"],
                                             expected_project_version=ready["project_version"], selections=selections,
                                             idempotency_key="journey-later-page-image-save")
        saved = workspace.confirm_version(token, proposal_id=preview["proposal_id"], proposal_digest=preview["proposal_digest"])
        self.assertEqual(saved["save_status"], "completed")
        refreshed = workspace.read_current(token, **scope)
        self.assertEqual(refreshed["shots"][0]["selected_image_key"], chosen["image_key"])
        self.assertEqual(len(refreshed["shots"]), count)
        self.assertEqual(sha256_file(parent_draft), parent_sha)
        child_context = images.delivery.resolve(token, base.project_id, refreshed["base_version_id"])
        child_draft = images.resolver.resolve_stage_artifact(child_context.run_id, "script_draft").path
        child_rows = json.loads(child_draft.read_text())["script_rows"]
        text_fields = ("镜号", "结构", "类型", "画面描述", "景别", "运镜", "声音", "时长(秒)")
        self.assertEqual([{k: row.get(k) for k in text_fields} for row in child_rows],
                         [{k: row.get(k) for k in text_fields} for row in parent_rows])
        confirmed = base._confirm_current()
        excel = base.review.prepare_excel_delivery(token, **scope, expected_project_version=confirmed["project_version"])
        pdf = base.review.prepare_pdf_delivery(token, **scope, expected_project_version=confirmed["project_version"])
        self.assertEqual(pdf.status, "available", pdf.unavailable_reason)
        self.assertEqual(len(PdfReader(str(pdf.path)).pages), count)
        book = load_workbook(excel.path)
        try:
            self.assertEqual(len(book["脚本"]._images), count)
            self.assertEqual(book[ADJACENT_REVIEW_SHEET_NAME].max_row - 1, count - 1)
            first_image = min(book["脚本"]._images, key=lambda item: item.anchor._from.row)
            with Image.open(BytesIO(first_image._data())) as embedded:
                preview_bytes, _ = service.preview(token, **{**scope, "expected_project_version": confirmed["project_version"]}, media_id=later["candidate_key"])
                with Image.open(BytesIO(preview_bytes)) as original:
                    self.assertEqual(embedded.convert("RGB").tobytes(), original.convert("RGB").tobytes())
        finally:
            book.close()
        with base.store.connection() as connection:
            artifact_ref = connection.execute("SELECT artifact_ref FROM delivery_artifact_bindings WHERE artifact_kind='excel' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        trace = base.review.delivery_snapshots.reverse_trace(token, project_id=base.project_id, artifact_ref=artifact_ref)
        self.assertEqual(trace["image_decision_count"], count)
        self.assertTrue(all(item["source_file"] and item["source_sheet"] and int(item["source_row"]) > 0 for item in trace["source_trace"]))
        restarted = base._review_service(WorkspaceStore(base.store.database_path))
        reread = restarted.read_current(token, **scope)
        self.assertEqual(reread["state"], "confirmed")
        second_excel = restarted.prepare_excel_delivery(token, **scope, expected_project_version=reread["project_version"])
        self.assertEqual(sha256_file(second_excel.path), sha256_file(excel.path))
