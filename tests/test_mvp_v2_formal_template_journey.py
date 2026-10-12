from __future__ import annotations

import json
import unittest

from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from PIL import Image

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.asset_package import bundle_asset_package_v3, load_asset_package
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.formal_generation_service import FormalGenerationBlocked, FormalTextProviderPolicy
from mvp_v2.formal_image_workspace import ImageVersionMaterializer
from mvp_v2.formal_script_workspace import DraftVersionMaterializer
from mvp_v2.generation_stages import read_visual_binding
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from tests import test_mvp_v2_formal_generation_service as fixture


class FormalTemplateJourneyTests(unittest.TestCase):
    setUp = fixture.FormalGenerationServiceTests.setUp
    _confirmed_brief = fixture.FormalGenerationServiceTests._confirmed_brief
    _service = fixture.FormalGenerationServiceTests._service
    _provider = fixture.FormalGenerationServiceTests._provider

    def _write_template_variant(self, source_path, filename, label, width, color):
        path = source_path.with_name(filename)
        source = load_workbook(source_path)
        try:
            sheet = source["模版"]
            sheet["A3"] = label
            sheet.row_dimensions[3].height = 31
            sheet.column_dimensions["I"].width = width
            sheet["C6"].fill = PatternFill(fill_type="solid", fgColor=color)
            # The arbitrary filename intentionally has no built-in reuse policy.
            # Keep this anonymous uploaded workbook independently AI-spec ready.
            for name in ("模版", "创意库"):
                current = source[name]
                for column, title in enumerate(("镜头功能", "可复用等级", "禁用说明", "来源品牌"), start=12):
                    current.cell(6, column, title)
                for row in range(7, current.max_row + 1):
                    if current.cell(row, 9).value:
                        current.cell(row, 12, "建立空间")
                        current.cell(row, 13, "可复用")
                        current.cell(row, 14, "")
                        current.cell(row, 15, "匿名素材")
            if label == "方案乙版式":
                sheet.cell(7, 1, "场地开篇")
            source.save(path)
        finally:
            source.close()
        report = refresh_ai_spec(path)
        self.assertTrue(report.in_sync)
        self.assertTrue(report.usable)
        self.assertTrue(all(row.source_file == filename for row in report.rows))
        return path

    def _assert_selected_template_rendered(self, path, *, image_count=None):
        workbook = load_workbook(path)
        try:
            sheet = workbook["脚本"]
            self.assertEqual(sheet["A3"].value, "方案乙版式")
            self.assertEqual(sheet.column_dimensions["I"].width, 81)
            self.assertEqual(sheet.row_dimensions[3].height, 31)
            self.assertEqual(sheet["C6"].fill.fgColor.rgb, "00316649")
            self.assertIn("A1:K1", {str(value) for value in sheet.merged_cells.ranges})
            if image_count is not None:
                self.assertEqual(len(sheet._images), image_count)
        finally:
            workbook.close()

    def _templates(self):
        registry = LocalPackageRegistry(self.root / "registry.json")
        packages = AssetPackagePublicationService(
            registry=registry, storage=LocalAssetStorage(self.root / "assets"),
            packages_root=self.root / "packages",
        )
        workbook = next(self.source_root.glob("*.xlsx"))
        first_workbook = self._write_template_variant(workbook, "匿名方案甲.xlsx", "方案甲版式", 73, "A14722")
        second_workbook = self._write_template_variant(workbook, "匿名方案乙.xlsx", "方案乙版式", 81, "316649")
        first = packages.import_template(category_id="launch", filename=first_workbook.name,
                                         content=first_workbook.read_bytes(), display_name="方案甲")
        second = packages.import_template(category_id="launch", filename=second_workbook.name,
                                          content=second_workbook.read_bytes(), display_name="方案乙")
        registry.promote("launch", first["package_id"], first["package_version"])
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy, asset_package_resolver=lambda _: {
            **registry.resolve_formal("launch"), "alias": "formal",
        })
        service.template_packages = packages
        return service, packages, first, second, calls

    def test_frozen_cross_category_library_reaches_source_catalog_and_generation(self):
        service, packages, first, _, _ = self._templates()
        source = next(self.source_root.glob('*.xlsx'))
        annual = self._write_template_variant(source, '匿名年会模板.xlsx', '年会结构', 73, 'A14722')
        workbook = load_workbook(annual)
        try:
            for shot in range(1, 3):
                workbook['创意库'].append([shot, '产品展示', '固定镜头', None, '光效', '地面',
                    '中景', '固定', '工作人员展示物料细节'+str(shot), '现场声音', '',
                    '展示物料', '可复用', '', '匿名素材'])
            workbook.save(annual)
        finally:
            workbook.close()
        self.assertTrue(refresh_ai_spec(annual).usable)
        record = packages.import_template(category_id='annual', filename=annual.name, content=annual.read_bytes())
        packages.registry.promote('annual', record['package_id'], record['package_version'])
        scope = {'workspace_id': self.workspace_id, 'project_id': self.project['project_id']}
        preview = service.prepare_plan(self.token, **scope, expected_project_version=2, generation_mode='text_only_draft')
        self.expected_shot_count = preview['shot_count']
        result = service.confirm_plan_and_generate(self.token, **scope, plan_id=preview['plan_id'], expected_project_version=3)
        self.assertEqual(result['status'], 'completed')
        version = self.projects.list_project_versions(self.token, scope['project_id'])[-1]
        catalog = json.loads(service.services.artifacts.resolve_stage_artifact(version['run_id'], 'source_catalog').path.read_text())
        self.assertEqual({r['source_category_id'] for r in catalog['records'] if r['record_type']=='template'}, {'launch'})
        self.assertIn('annual', {r['source_category_id'] for r in catalog['records'] if r['record_type']=='creative'})
        self.assertTrue(any(b['resolved_ref']==record['resolved_ref'] for b in catalog['formal_asset_package']['library_bindings']))

    def test_import_trial_select_generate_and_frozen_source_survive_alias_upgrade(self):
        service, packages, first, second, calls = self._templates()
        scope = {"workspace_id": self.workspace_id, "project_id": self.project["project_id"]}
        catalog = service.template_catalog(self.token, **scope)
        self.assertEqual(len(catalog["items"]), 2)
        self.assertFalse(catalog["is_bound"])
        trial = service.trial_template(self.token, **scope, expected_project_version=2,
                                       template_ref=second["resolved_ref"])
        self.assertEqual(trial["plan"]["status"], "trial_completed")
        self.assertEqual(calls, [])
        self.assertEqual(self.projects.get_project(self.token, scope["project_id"])["project_version"], 2)
        self.assertEqual(service.list_waiting_plans(self.token, **scope)["items"], [])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)
        selected = service.select_template(self.token, **scope, expected_project_version=2,
                                           template_ref=second["resolved_ref"], confirmed=True)
        self.assertTrue(selected["is_bound"])
        self.assertEqual(selected["project_version"], 3)
        preview = service.prepare_plan(self.token, **scope, expected_project_version=3,
                                        generation_mode="text_only_draft")
        summary = preview["template_summary"]
        self.assertEqual(summary["selection_mode"], "explicit")
        self.assertEqual(summary["package_version"], second["package_version"])
        self.assertEqual(summary["original_segments"][0]["structure"], "场地开篇")
        self.assertEqual(sum(item["shot_count"] for item in summary["current_segments"]), preview["shot_count"])
        self.assertEqual(summary["structure_change_count"], sum(shot["structure_source"] == "brief_confirmed_override" for shot in preview["shots"]))
        upgraded = bundle_asset_package_v3(first["local_path"], self.root / "upgraded", package_version=2)
        record = build_package_record(upgraded)
        packages.registry.register_records([record])
        packages.registry.promote("launch", record["package_id"], 2)
        # Reconstruct generation to prove the pin is persisted, not an in-memory choice.
        restarted = self._service(service.provider_policy, asset_package_resolver=service.asset_package_resolver)
        restarted.template_packages = packages
        self.assertEqual(restarted.get_plan_preview(self.token, **scope, plan_id=preview["plan_id"]), preview)
        result = restarted.confirm_plan_and_generate(self.token, **scope,
            plan_id=preview["plan_id"], expected_project_version=4)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["script_rows"]), preview["shot_count"])
        self.assertEqual(len(calls), 2)
        evidence = restarted._plan_event(self.token, preview["plan_id"])["formal_asset_package"]
        self.assertEqual(evidence["resolved_ref"], second["resolved_ref"])
        completed = restarted.services.tasks.list_task_events(self.token, preview["plan_id"])[-1]["details"]
        workbook = restarted.services.artifacts.resolve_artifact(completed["run_id"], "workbook")
        self._assert_selected_template_rendered(workbook.path)
        binding = read_visual_binding(restarted.services.artifacts.resolve_stage_artifact(
            completed["run_id"], "visual_binding",
        ).path)
        selected_workbook = load_asset_package(second["local_path"])["source_workbook_path"]
        self.assertEqual(selected_workbook.name, "匿名方案乙.xlsx")
        images = {}
        for index, row in enumerate(binding.script_rows, start=1):
            path = self.root / f"anonymous-frame-{index}.png"
            Image.new("RGB", (96, 54), (index * 7 % 255, 90, 120)).save(path)
            images[str(row["镜号"])] = path
        for materializer_type in (DraftVersionMaterializer, ImageVersionMaterializer):
            with self.subTest(materializer=materializer_type.__name__):
                materializer = materializer_type(artifacts=service.services.artifacts,
                    runs_root=self.root / "runs", source_root=self.source_root,
                    package_registry=packages.registry)
                frozen_workbook = materializer._source_root_for("发布会", evidence)
                self.assertEqual(frozen_workbook, selected_workbook)
                if materializer_type is DraftVersionMaterializer:
                    renderer, _ = materializer._workbook_functions(
                        binding, self.source_root, template_workbook_path=frozen_workbook,
                    )
                    expected_images = 0
                else:
                    renderer, _ = materializer._workbook_functions(
                        binding, images, self.source_root, template_workbook_path=frozen_workbook,
                    )
                    expected_images = len(binding.script_rows)
                output = self.root / f"{materializer_type.__name__}.xlsx"
                renderer(binding, output, [])
                self._assert_selected_template_rendered(output, image_count=expected_images)
        public = json.dumps(selected, ensure_ascii=False)
        self.assertNotIn(str(self.root), public)
        for item in selected["items"]:
            self.assertRegex(item["source_semantics"]["original_workbook_sha256"], r"^[0-9a-f]{64}$")

    def test_stale_or_unconfirmed_selection_writes_nothing(self):
        service, _, first, second, _ = self._templates()
        scope = {"workspace_id": self.workspace_id, "project_id": self.project["project_id"]}
        for expected, confirmed, ref in ((1, True, second["resolved_ref"]),
                                          (2, False, second["resolved_ref"]),
                                          (2, True, "missing@1")):
            with self.assertRaises(FormalGenerationBlocked):
                service.select_template(self.token, **scope, expected_project_version=expected,
                                        template_ref=ref, confirmed=confirmed)
        project = self.projects.get_project(self.token, scope["project_id"])
        self.assertEqual(project["project_version"], 2)
        self.assertIsNone(project["template_package_ref"])
        # Explicitly pinning the currently displayed default must be possible.
        chosen = service.select_template(self.token, **scope, expected_project_version=2,
                                         template_ref=first["resolved_ref"], confirmed=True)
        self.assertTrue(chosen["is_bound"])
        self.assertEqual(chosen["selected_ref"], first["resolved_ref"])

    def test_changing_template_invalidates_old_plan(self):
        service, _, first, second, _ = self._templates()
        scope = {"workspace_id": self.workspace_id, "project_id": self.project["project_id"]}
        service.select_template(self.token, **scope, expected_project_version=2,
                                template_ref=first["resolved_ref"], confirmed=True)
        plan = service.prepare_plan(self.token, **scope, expected_project_version=3,
                                    generation_mode="text_only_draft")
        service.select_template(self.token, **scope, expected_project_version=4,
                                template_ref=second["resolved_ref"], confirmed=True)
        with self.assertRaises(FormalGenerationBlocked):
            service.confirm_plan_and_generate(self.token, **scope, plan_id=plan["plan_id"],
                                               expected_project_version=5)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)

    def test_same_project_switches_six_types_and_freezes_creative_image_and_old_version_sources(self):
        from io import BytesIO
        from hashlib import sha256
        from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
        from mvp_v2.formal_default_images import FormalDefaultImageService
        from mvp_v2.generation_stages import read_script_plan
        from mvp_v2.script_type_adapter import metadata_for_category_id, category_id_for_script_type
        from mvp_v2.version_delivery import VersionDeliveryService
        from tests.test_mvp_v2_template_selection import CATEGORIES
        from tests.test_mvp_v2 import write_launch_library
        from openpyxl.drawing.image import Image as ExcelImage
        service, packages, _, _, _ = self._templates()
        service.asset_package_resolver = lambda label: {**packages.registry.resolve_formal(category_id_for_script_type(label)), 'alias':'formal'}
        adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / 'runtime')
        scope = dict(workspace_id=self.workspace_id, project_id=self.project['project_id'])
        defaults = FormalDefaultImageService(projects=self.projects, tasks=service.services.tasks,
            delivery=VersionDeliveryService(self.store, service.services.artifacts), artifacts=service.services.artifacts,
            image_pool=service.services.images, image_workspace=None, asset_package_resolver=service.asset_package_resolver,
            writes_enabled=True, package_registry=packages.registry)
        initial = None
        def current():
            return self.projects.get_project(self.token, scope['project_id'])
        for index, category in enumerate(CATEGORIES):
            label = metadata_for_category_id(category).display_name
            folder = self.root / category
            folder.mkdir()
            path = folder / ('匿名'+label+'模板.xlsx')
            segments = (("场地建立", 3), ("产品展示", 4 + index % 2), ("互动体验", 3), ("收束", 2))
            structures = [name for name, count in segments for _ in range(count)]
            rows = [[name, shot, None, None, "光效", "地面", "全景", "固定",
                     label+name+"，工作人员展示活动物料", "现场声音", ""]
                    for shot, name in enumerate(structures, start=1)]
            creative = [[shot, "产品展示", "固定镜头", None, "光效", "地面", "中景", "固定",
                         label+"工作人员展示物料细节"+str(shot), "现场声音", ""] for shot in range(1, 4)]
            write_launch_library(path, rows, creative)
            book = load_workbook(path)
            try:
                for sheet_name in ("模版", "创意库"):
                    sheet = book[sheet_name]
                    for column, title in enumerate(("镜头功能", "可复用等级", "禁用说明",
                                                    "来源品牌", "画面主体", "标签"), start=12):
                        sheet.cell(6, column, title)
                    for row in range(7, sheet.max_row + 1):
                        for column, value in enumerate(("现场记录", "可复用", "",
                                                       "匿名品牌", "工作人员与物料", label+"; 物料; 现场"), start=12):
                            sheet.cell(row, column, value)
                for image_index, color in enumerate(((30+index, 90, 60), (90, 30+index, 60))):
                    image_path = folder / f"anonymous-{image_index}.png"
                    Image.new("RGB", (80, 45), color).save(image_path)
                    book["模版"].add_image(ExcelImage(image_path), f"C{7+image_index}")
                book.save(path)
            finally:
                book.close()
            report = refresh_ai_spec(path)
            self.assertTrue(report.usable and report.in_sync, report.validation_codes)
            record = packages.import_template(category_id=category, filename=path.name, content=path.read_bytes())
            packages.registry.promote(category, record['package_id'], record['package_version'])
            before = current()
            catalog = service.template_catalog(self.token, **scope, target_category_id=category)
            self.assertEqual(catalog['category_id'], category)
            self.assertEqual(current(), before)
            desired = {'项目名称':'匿名多类型项目','脚本类型':label,'品牌或客户':'匿名品牌',
                '目标时长':'60秒','风格调性':'克制真实','视频目标':'展示'+label+'现场',
                '使用场景':'活动回顾','必须出现的内容':'产品展示',
                '禁止或不能出现的内容':'不得虚构事实','关键事件与流程':'；'.join(name for name, _ in segments)}
            pending = adapter.import_uploads(self.token, **scope, expected_project_version=current()['project_version'],
                _replace_document_ids={'anonymous_doc_0'} if index == 0 else None,
                uploads=[UploadedDocument('匿名'+category+'.txt', BytesIO('\n'.join(k+'：'+v for k,v in desired.items()).encode()))])
            pending = self.briefs.read_structured(self.token, **scope, brief_version_id=pending['brief_key'])
            while any(c['resolution_status']=='unresolved' for c in pending['conflicts']):
                conflict = next(c for c in pending['conflicts'] if c['resolution_status']=='unresolved')
                chosen = next(f for f in pending['facts'] if f['fact_id'] in conflict['fact_ids']
                    and json.loads(f['value_json']) == desired[conflict['field_path']])
                resolved = adapter.resolve_conflict(self.token, **scope, brief_version_id=pending['brief_version_id'],
                    conflict_id=conflict['conflict_id'], selected_fact_id=chosen['fact_id'], expected_project_version=current()['project_version'])
                pending = self.briefs.read_structured(self.token, **scope, brief_version_id=resolved['brief_key'])
            adapter.confirm(self.token, **scope, brief_version_id=pending['brief_version_id'],
                confirmed_at='2026-09-15T00:00:00Z', expected_project_version=current()['project_version'])
            service.select_template(self.token, **scope, template_ref=record['resolved_ref'], confirmed=True,
                expected_project_version=current()['project_version'])
            plan = service.prepare_plan(self.token, **scope, expected_project_version=current()['project_version'], generation_mode='text_only_draft')
            result = service.confirm_plan_and_generate(self.token, **scope, plan_id=plan['plan_id'], expected_project_version=current()['project_version'])
            self.assertEqual(result['status'], 'completed')
            version = self.projects.list_project_versions(self.token, scope['project_id'])[-1]
            artifact = read_script_plan(service.services.artifacts.resolve_stage_artifact(version['run_id'], 'script_plan').path)
            self.assertEqual(artifact.constraints['formal_asset_package']['category_id'], category)
            source = json.loads(service.services.artifacts.resolve_stage_artifact(version['run_id'], 'source_catalog').path.read_text())
            self.assertEqual(source['category_id'], category)
            current_creative = [row for row in source['records'] if row['record_type'] == 'creative'
                                and row['source_category_id'] == category]
            self.assertEqual(len(current_creative), 3)
            self.assertGreaterEqual(source['creative_count'], 3)
            self.assertEqual({row['source_file'] for row in current_creative}, {path.name})
            frozen_categories = {item['category_id'] for item in artifact.constraints['formal_asset_package']['library_bindings']}
            self.assertTrue({row['source_category_id'] for row in source['records']
                             if row['record_type'] == 'creative'}.issubset(frozen_categories | {category}))
            self.assertEqual({row['script_type'] for row in source['records']
                              if row['record_type'] == 'template'}, {label})
            images = defaults.candidates(self.token, **scope, expected_project_version=current()['project_version'], view='library', match_scope='all')
            self.assertEqual(images['package']['category_id'], category)
            self.assertGreater(images['candidate_count'], 1)
            _, media = defaults._package_media(current(), artifact.brief, artifact.constraints['formal_asset_package'])
            if initial is None:
                image = media[0]
                initial = (version['version_id'], image['media_id'], image['content_sha256'])
            old_image, _ = defaults.preview(self.token, **scope, expected_project_version=current()['project_version'],
                version_id=initial[0], media_id=initial[1])
            self.assertEqual(sha256(old_image).hexdigest(), initial[2])
        self.assertEqual(len(self.projects.list_project_versions(self.token, scope['project_id'])), 6)
