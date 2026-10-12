"""Frozen review copies remain readable across isolated formal alias switches."""
from io import BytesIO
from pathlib import Path
import hashlib
import unittest

from openpyxl import load_workbook
from playwright.sync_api import expect

from mvp_v2 import local_ui
from mvp_v2.excel_io import read_managed_sheet_rows
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from tests import test_mvp_v2_template_legacy_binding as package_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class PackageHistoryBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_alias_upgrade_rollback_keeps_both_historical_review_copies(self):
        f = package_fixture.TemplateLegacyBindingTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        prepared = f._generated_versions_across_alias_changes()
        artifacts, delivery = prepared['artifacts'], prepared['delivery']
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=f.store, projects=f.projects, delivery=delivery, commands=None, artifacts=artifacts),
            runs_root=f.root / 'runs', source_root=f.source_root, package_registry=prepared['registry'])
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=f.store, identity=LocalIdentityAdapter(f.store), projects=f.projects,
            tasks=TaskService(f.store), versions=VersionService(f.store), artifacts=artifacts))
        app = local_ui.create_app(agent_api=agent, formal_script_service=script,
            formal_generation_service=prepared['generation'],
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(
                f.store, artifact_root=f.root / 'runtime'), data_root=f.root / 'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def contents(book):
            return (read_managed_sheet_rows(book, '脚本'),
                [hashlib.sha256(image._data()).hexdigest() for image in book['脚本']._images])

        for alias_version in (2, 1):
            prepared['registry'].promote('launch', 'legacy-configured', alias_version)
            page.goto(origin + '/projects/' + f.project['project_id'] + '/script')
            page.wait_for_load_state('networkidle')
            page.locator('#formal-script-versions-title').click()
            for version_no, version in enumerate(prepared['versions'][:2], 1):
                button = page.locator(f'[data-script-version-workbook="{version_no}"]')
                expect(button).to_be_visible()
                with page.expect_download(timeout=60000) as download:
                    button.click()
                self.assertIsNone(download.value.failure())
                self.assertIn(f'-V{version_no}.xlsx', download.value.suggested_filename)
                actual = load_workbook(BytesIO(Path(download.value.path()).read_bytes()))
                context = delivery.resolve(f.token, f.project['project_id'], version['version_id'],
                    ancestor_of=prepared['versions'][-1]['version_id'])
                original = load_workbook(artifacts.resolve_artifact(context.run_id, 'workbook').path)
                try:
                    self.assertEqual(contents(actual), contents(original))
                finally:
                    actual.close()
                    original.close()
                for path, digest in prepared['frozen'][version['version_id']].items():
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(len(f.projects.list_project_versions(f.token, f.project['project_id'])), 3)
        self.assertEqual(errors, [])
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM delivery_snapshots').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_quality_final_confirmations').fetchone()[0], 0)
