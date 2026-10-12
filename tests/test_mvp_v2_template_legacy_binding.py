from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package, load_asset_package
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_image_workspace import ImageVersionMaterializer
from mvp_v2.formal_runtime import build_formal_source_root_resolver
from mvp_v2.formal_script_workspace import DraftVersionMaterializer
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry, PackageRegistryError, build_package_record
from mvp_v2.runtime_config import configured_asset_package
from tests import test_mvp_v2_formal_generation_service as fixture


class TemplateLegacyBindingTests(unittest.TestCase):
    setUp = fixture.FormalGenerationServiceTests.setUp
    _confirmed_brief = fixture.FormalGenerationServiceTests._confirmed_brief
    _service = fixture.FormalGenerationServiceTests._service

    def _legacy_package(self):
        workbook_path = next(self.source_root.glob("*.xlsx"))
        workbook = load_workbook(workbook_path)
        try:
            for index, color in enumerate(("blue", "green")):
                image_path = self.root / f"anonymous-reference-{index}.png"
                Image.new("RGB", (80, 45), color).save(image_path)
                workbook["模版"].add_image(ExcelImage(image_path), f"C{7 + index}")
            workbook.save(workbook_path)
        finally:
            workbook.close()
        refresh_ai_spec(workbook_path)
        editable = create_asset_package(
            self.root / "editable", package_id="legacy-configured", source_root=self.source_root,
        )
        return bundle_asset_package_v3(editable, self.root / "legacy-package", package_version=1)

    def test_first_candidate_import_keeps_configured_default_without_promoting_alias(self):
        package = self._legacy_package()
        registry = LocalPackageRegistry(self.root / "registry.json")
        runtime = {
            "package_registry_path": str(registry.path),
            "asset_packages": {"发布会": str(package)},
        }
        source_resolver = build_formal_source_root_resolver(runtime, self.source_root)
        expected_source = load_asset_package(package)["source_root_path"]
        self.assertEqual(source_resolver("发布会"), expected_source)
        self.assertEqual(Path(configured_asset_package("发布会", runtime)), package)
        publication = AssetPackagePublicationService(
            storage=LocalAssetStorage(self.root / "assets"), registry=registry,
            packages_root=self.root / "imported-packages",
        )
        workbook = next(self.source_root.glob("*.xlsx"))

        publication.import_template(
            category_id="launch", filename=workbook.name, content=workbook.read_bytes(),
            display_name="仅导入的匿名候选",
        )

        self.assertEqual(registry.list()["aliases"], {})
        with self.subTest(entry="formal_source"):
            self.assertEqual(source_resolver("发布会"), expected_source)
        with self.subTest(entry="creative_strategy"):
            self.assertEqual(Path(configured_asset_package("发布会", runtime)), package)

    def test_two_generated_versions_keep_frozen_packages_after_alias_upgrade_and_rollback(self):
        self._generated_versions_across_alias_changes()

    def _generated_versions_across_alias_changes(self):
        from mvp_v2.artifact_service import RunArtifactResolver
        from mvp_v2.version_delivery import VersionDeliveryService
        from mvp_v2.run_contract import sha256_file
        old = self._legacy_package()
        registry = LocalPackageRegistry(self.root / 'isolated-alias.json')
        first_record = registry.register(old)
        registry.promote('launch', first_record['package_id'], 1)
        calls = []
        policy = fixture.FormalTextProviderPolicy(
            writer=fixture.FormalGenerationServiceTests._provider(self, 'writer', calls),
            editor=fixture.FormalGenerationServiceTests._provider(self, 'editor', calls),
            writer_provider='anonymous-writer', writer_model='anonymous-writer',
            editor_provider='anonymous-editor', editor_model='anonymous-editor')
        service = self._service(policy, asset_package_resolver=lambda _: {**registry.resolve_formal('launch'), 'alias': 'formal'})
        scope = dict(workspace_id=self.workspace_id, project_id=self.project['project_id'])
        def generate():
            version = self.projects.get_project(self.token, self.project['project_id'])['project_version']
            plan = service.prepare_plan(self.token, **scope, expected_project_version=version,
                generation_mode='text_only_draft')
            evidence = service._plan_event(self.token, plan['plan_id'])['formal_asset_package']
            result = service.confirm_plan_and_generate(self.token, **scope, plan_id=plan['plan_id'],
                expected_project_version=self.projects.get_project(self.token, self.project['project_id'])['project_version'])
            return result, evidence
        artifacts = RunArtifactResolver(self.root / 'runs')
        delivery = VersionDeliveryService(self.store, artifacts)

        def snapshot(version, current_version):
            context = delivery.resolve(self.token, self.project['project_id'], version['version_id'],
                ancestor_of=current_version['version_id'])
            paths = [artifacts.resolve_stage_artifact(context.run_id, stage).path
                for stage in ('script_plan', 'script_draft', 'visual_binding')]
            paths.append(artifacts.resolve_artifact(context.run_id, 'workbook').path)
            return {path: sha256_file(path) for path in paths}

        first, first_binding = generate()
        frozen_first = snapshot(first, first)
        workbook_path = next(self.source_root.glob('*.xlsx'))
        workbook = load_workbook(workbook_path)
        additional = self.root / 'anonymous-new-reference.png'
        Image.new('RGB', (80, 45), 'red').save(additional)
        workbook['模版'].add_image(ExcelImage(additional), 'C9')
        workbook.save(workbook_path)
        workbook.close()
        refresh_ai_spec(workbook_path)
        seed = create_asset_package(self.root / 'new-editable', package_id='legacy-configured',
            source_root=self.source_root)
        new = bundle_asset_package_v3(seed, self.root / 'new-package', package_version=2)
        second_record = registry.register(new)
        registry.promote('launch', second_record['package_id'], 2)
        second, second_binding = generate()
        self.assertEqual(first_binding['resolved_ref'], 'legacy-configured@1')
        self.assertEqual(second_binding['resolved_ref'], 'legacy-configured@2')
        self.assertNotEqual(first_binding['package_sha256'], second_binding['package_sha256'])
        self.assertEqual(snapshot(first, second), frozen_first)
        frozen = {first['version_id']: frozen_first,
                  second['version_id']: snapshot(second, second)}
        defaults = FormalDefaultImageService(projects=self.projects, tasks=service.services.tasks,
            delivery=delivery, artifacts=artifacts, image_pool=service.services.images,
            image_workspace=None, asset_package_resolver=lambda _: {**registry.resolve_formal('launch'), 'alias': 'formal'},
            writes_enabled=False, package_registry=registry)
        for alias_version in (2, 1):
            registry.promote('launch', 'legacy-configured', alias_version)
            self.assertEqual(registry.resolve_formal('launch')['package_version'], alias_version)
            for version, binding, count in ((first, first_binding, 2), (second, second_binding, 3)):
                package, media = defaults._package_media(self.project, {'脚本类型': '发布会'}, binding)
                self.assertEqual(package['package_version'], binding['package_version'])
                self.assertEqual(len(media), count)
                materializer = DraftVersionMaterializer(artifacts=artifacts, runs_root=self.root/'runs',
                    source_root=self.source_root, package_registry=registry)
                self.assertEqual(materializer._source_root_for('发布会', binding),
                    load_asset_package(old if count == 2 else new)['source_workbook_path'])
                for path, digest in frozen[version['version_id']].items():
                    self.assertEqual(sha256_file(path), digest)
        self.assertEqual(build_package_record(old)['package_sha256'], first_record['package_sha256'])
        self.assertEqual(build_package_record(new)['package_sha256'], second_record['package_sha256'])
        third, third_binding = generate()
        self.assertEqual(third_binding['resolved_ref'], first_binding['resolved_ref'])
        self.assertEqual(third_binding['package_sha256'], first_binding['package_sha256'])
        for version in (first, second):
            self.assertEqual(snapshot(version, third), frozen[version['version_id']])
        self.assertEqual(len(self.projects.list_project_versions(self.token, self.project['project_id'])), 3)
        return dict(registry=registry, generation=service, artifacts=artifacts,
            delivery=delivery, versions=(first, second, third), frozen=frozen)


    def test_present_but_corrupt_alias_never_falls_back_to_configured_package(self):
        package = self._legacy_package()
        configured_copy = self.root / "configured-copy"
        shutil.copytree(package.parent, configured_copy)
        registry = LocalPackageRegistry(self.root / "registry.json")
        registered = registry.register(package)
        registry.promote("launch", registered["package_id"], registered["package_version"])
        runtime = {
            "package_registry_path": str(registry.path),
            "asset_packages": {"发布会": str(configured_copy)},
        }
        original_registry = registry.path.read_text(encoding="utf-8")
        damaged = json.loads(original_registry)
        damaged["aliases"]["launch"]["formal"] = "missing-package@1"
        registry.path.write_text(json.dumps(damaged), encoding="utf-8")
        with self.subTest(corruption="missing_alias_target"):
            with self.assertRaises(ValueError):
                build_formal_source_root_resolver(runtime, self.source_root)("发布会")
            self.assertEqual(configured_asset_package("发布会", runtime), "")
        registry.path.write_text(original_registry, encoding="utf-8")
        rules = package.parent / "template_rules.json"
        rules.write_bytes(rules.read_bytes() + b"\n")

        with self.subTest(corruption="package_sha"):
            with self.assertRaises(ValueError):
                build_formal_source_root_resolver(runtime, self.source_root)("发布会")
            self.assertEqual(configured_asset_package("发布会", runtime), "")

    def test_configured_default_can_be_listed_selected_and_frozen_without_alias_change(self):
        package = self._legacy_package()
        registry = LocalPackageRegistry(
            self.root / "registry.json", configured_packages={"launch": package},
        )
        publication = AssetPackagePublicationService(
            storage=LocalAssetStorage(self.root / "assets"), registry=registry,
            packages_root=self.root / "imported-packages",
        )
        service = self._service(asset_package_resolver=lambda _: package)
        service.template_packages = publication
        scope = {"workspace_id": self.workspace_id, "project_id": self.project["project_id"]}
        catalog = service.template_catalog(self.token, **scope)
        self.assertEqual(len(catalog["items"]), 1)
        self.assertTrue(catalog["items"][0]["is_default"])
        self.assertEqual(catalog["selected_ref"], "legacy-configured@1")
        self.assertFalse(registry.path.exists())

        selected = service.select_template(
            self.token, **scope, expected_project_version=2,
            template_ref=catalog["selected_ref"], confirmed=True,
        )
        preview = service.prepare_plan(
            self.token, **scope, expected_project_version=3, generation_mode="text_only_draft",
        )
        evidence = service._plan_event(self.token, preview["plan_id"])["formal_asset_package"]

        self.assertTrue(selected["is_bound"])
        self.assertEqual(evidence["resolved_ref"], catalog["selected_ref"])
        self.assertEqual(evidence["alias"], "project")
        self.assertEqual(registry.list()["aliases"], {})
        self.assertEqual(registry.resolve_evidence("launch", evidence)["resolved_ref"], catalog["selected_ref"])

    def test_configured_plan_keeps_source_and_default_image_access_without_registration(self):
        package = self._legacy_package()
        service = self._service(asset_package_resolver=lambda _: package)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2, generation_mode="text_only_draft",
        )
        evidence = service._plan_event(self.token, preview["plan_id"])["formal_asset_package"]
        self.assertEqual(evidence["alias"], "legacy-configured")
        registry = LocalPackageRegistry(
            self.root / "empty-registry.json", configured_packages={"launch": package},
        )
        workbook = load_asset_package(package)["source_workbook_path"]
        for alias in ("configured", "legacy-configured"):
            binding = {**evidence, "alias": alias}
            for materializer_type in (DraftVersionMaterializer, ImageVersionMaterializer):
                with self.subTest(alias=alias, materializer=materializer_type.__name__):
                    materializer = materializer_type(
                        artifacts=service.services.artifacts, runs_root=self.root / "runs",
                        source_root=self.source_root, package_registry=registry,
                    )
                    self.assertEqual(materializer._source_root_for("发布会", binding), workbook)
            defaults = FormalDefaultImageService(
                projects=self.projects, tasks=service.services.tasks, delivery=None,
                artifacts=service.services.artifacts, image_pool=service.services.images,
                image_workspace=None, asset_package_resolver=lambda _: None,
                writes_enabled=False, package_registry=registry,
            )
            with self.subTest(alias=alias, materializer="default_images"):
                resolved, _media = defaults._package_media(
                    self.project, {"脚本类型": "发布会"}, binding,
                )
                self.assertEqual(resolved["package_id"], evidence["package_id"])
                self.assertEqual(len(_media), 2)
        self.assertFalse(registry.path.exists())

    def test_registered_frozen_plan_resolves_default_image_library(self):
        package = self._legacy_package()
        registry = LocalPackageRegistry(self.root / "registry.json")
        record = registry.register(package)
        reference = "legacy-configured@1"
        service = self._service()
        defaults = FormalDefaultImageService(
            projects=self.projects, tasks=service.services.tasks, delivery=None,
            artifacts=service.services.artifacts, image_pool=service.services.images,
            image_workspace=None, asset_package_resolver=lambda _: None,
            writes_enabled=False, package_registry=registry,
        )

        for name, evidence in (
            ("formal", {**record, "resolved_ref": reference, "alias": "formal"}),
            ("project", {**record, "resolved_ref": reference, "alias": "project"}),
            ("project_binding", {
                "template_package_ref": reference, "template_package_sha256": record["package_sha256"],
            }),
        ):
            with self.subTest(binding=name):
                resolved, _media = defaults._package_media(self.project, {"脚本类型": "发布会"}, evidence)
                self.assertEqual(resolved["package_id"], record["package_id"])
                self.assertEqual(len(_media), 2)
        with self.assertRaises(PackageRegistryError):
            registry.resolve_evidence("launch", {
                "template_package_ref": reference, "template_package_sha256": "0" * 64,
            })

    def test_configured_fallback_requires_exact_reference_category_and_sha(self):
        package = self._legacy_package()
        record = build_package_record(package)
        evidence = {**record, "resolved_ref": "legacy-configured@1", "alias": "configured"}
        registry = LocalPackageRegistry(
            self.root / "registry.json", configured_packages={"launch": package, "sports": package},
        )
        for category, changes in (
            ("launch", {"package_sha256": "0" * 64}),
            ("launch", {"resolved_ref": "legacy-configured@2"}),
            ("sports", {}),
            ("launch", {"alias": "formal"}),
            ("launch", {"alias": "project"}),
        ):
            with self.subTest(category=category, changes=changes), self.assertRaises(PackageRegistryError):
                registry.resolve_evidence(category, {**evidence, **changes})
        self.assertFalse(registry.path.exists())

    def test_registered_corruption_does_not_fall_back_to_valid_configured_copy(self):
        package = self._legacy_package()
        copy_root = self.root / "valid-configured-copy"
        shutil.copytree(package.parent, copy_root)
        registry = LocalPackageRegistry(
            self.root / "registry.json", configured_packages={"launch": copy_root},
        )
        registered = registry.register(package)
        evidence = {**registered, "resolved_ref": "legacy-configured@1", "alias": "configured"}
        self.assertEqual(build_package_record(copy_root)["package_sha256"], evidence["package_sha256"])
        registry.resolve_evidence("launch", evidence)
        rules = package.parent / "template_rules.json"
        rules.write_bytes(rules.read_bytes() + b"\n")

        with self.assertRaises(PackageRegistryError):
            registry.resolve_evidence("launch", evidence)


if __name__ == "__main__":
    unittest.main()
