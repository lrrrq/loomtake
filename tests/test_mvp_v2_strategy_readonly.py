from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from tests.test_mvp_v2_creative_strategy_tools import StrategyToolFixture
from mvp_v2.creative_strategy import build_creative_strategies
from mvp_v2.ui_governance import UiGovernanceService, UiGovernanceServices


class StrategyReadonlyTests(unittest.TestCase):
    def service(self, fixture):
        return UiGovernanceService(UiGovernanceServices(
            strategies=fixture.tools, knowledge=None, identity=fixture.identity))

    def snapshot(self, fixture):
        with fixture.store.connection() as connection:
            rows = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table}')]
                    for table in ('tasks', 'task_events', 'projects', 'project_brief_snapshots')}
        files = {str(path.relative_to(fixture.data_root)): path.read_bytes()
                 for path in fixture.data_root.rglob('*.json')}
        return rows, files

    def test_get_before_and_after_analysis_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = StrategyToolFixture(Path(tmp)); ui = self.service(f)
            before = self.snapshot(f)
            for _ in range(3):
                self.assertEqual('not_analyzed', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            self.assertEqual(before, self.snapshot(f))
            result = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            self.assertEqual('available', result['status'])
            after = self.snapshot(f)
            for _ in range(3):
                self.assertEqual('available', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            self.assertEqual(after, self.snapshot(f))
            replay = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            self.assertEqual(result['task_id'], replay['task_id'])
            self.assertEqual(after, self.snapshot(f))

    def test_changed_brief_package_and_strategy_version_are_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = StrategyToolFixture(Path(tmp)); ui = self.service(f)
            result = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            with patch('mvp_v2.creative_strategy_tools.STRATEGY_VERSION', 999):
                self.assertEqual('stale', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            package = f.services.packages.load('launch')
            package['binding']['index_fingerprint'] = 'different'
            with patch.object(f.services.packages, 'load', return_value=package):
                self.assertEqual('stale', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            fresh = f.briefs.freeze({**f.original_brief, '备注': '新需求'})
            f.projects.add_brief_snapshot(f.token, project_id=f.project['project_id'],
                brief_ref=fresh['brief_ref'], brief_sha256=fresh['brief_sha256'],
                expected_project_version=result['project_version'])
            before = self.snapshot(f)
            self.assertEqual('stale', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            self.assertEqual(before, self.snapshot(f))
            new = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            self.assertNotEqual(result['task_id'], new['task_id'])
            self.assertEqual('available', ui.strategy_context(f.token, agent_session_id=f.session)['status'])

    def test_input_change_during_analysis_is_not_returned_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = StrategyToolFixture(Path(tmp)); ui = self.service(f)
            def changing(*args, **kwargs):
                frozen = f.briefs.freeze({**f.original_brief, '备注': '并发新需求'})
                f.projects.add_brief_snapshot(f.token, project_id=f.project['project_id'],
                    brief_ref=frozen['brief_ref'], brief_sha256=frozen['brief_sha256'],
                    expected_project_version=f.project['project_version'])
                return build_creative_strategies(*args, **kwargs)
            with patch('mvp_v2.creative_strategy_tools.build_creative_strategies', side_effect=changing):
                result = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            self.assertEqual('stale', result['status'])
            self.assertIsNone(result['portfolio'])

    def test_corrupt_artifact_is_never_replayed_as_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = StrategyToolFixture(Path(tmp)); ui = self.service(f)
            result = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            digest = result['portfolio_ref'].rsplit('/', 1)[-1]
            path = f.portfolios.root / digest[:2] / (digest + '.json')
            path.write_text('{}')
            before = self.snapshot(f)
            self.assertEqual('stale', ui.strategy_context(f.token, agent_session_id=f.session)['status'])
            replay = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
            self.assertEqual('blocked', replay['status'])
            self.assertEqual(before, self.snapshot(f))

    def test_overlapping_double_click_creates_one_task_and_one_computation(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = StrategyToolFixture(Path(tmp)); ui = self.service(f)
            entered, release = Event(), Event()
            def held(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test release missing')
                return build_creative_strategies(*args, **kwargs)
            with patch('mvp_v2.creative_strategy_tools.build_creative_strategies', side_effect=held) as build:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(ui.analyze_strategy_context, f.token, agent_session_id=f.session)
                    try:
                        self.assertTrue(entered.wait(10))
                        second = ui.analyze_strategy_context(f.token, agent_session_id=f.session)
                        self.assertEqual('analyzing', second['status'])
                    finally:
                        release.set()
                    done = first.result(timeout=10)
                self.assertEqual(second['task_id'], done['task_id'])
                self.assertEqual(1, build.call_count)
            with f.store.connection() as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM tasks WHERE task_type='get_creative_strategies'").fetchone()[0])
