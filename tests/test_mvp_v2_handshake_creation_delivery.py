"""Full anonymous service journey A, with explicit simulated image/human analysis.

Run with --output PATH to keep the entire isolated runtime, source and delivery.
Photo pixels are existing anonymous test fixtures; analysis entries deliberately
exercise service contracts and do not attest actual model or human visual quality.
"""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from openpyxl import load_workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.formal_review_workspace import (FormalReviewWorkspaceService, FormalReviewWorkspaceServices,
    WorkspaceBoundFinalConfirmation, WorkspaceBoundVersionQuality, WorkspaceReviewerActorResolver)
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2.project_image_authority import (ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
    ProjectImagePoolService)
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from tests.test_mvp_v2_formal_agent import FakeFormalGeneration
from tests.test_mvp_v2_formal_image_workspace import _photo_variants
from tests.test_mvp_v2_handshake_creation import run_creation


def build_workspaces(created, *, store=None):
    root = created['root']
    store = store or created['store']
    projects = ProjectService(store)
    tasks = TaskService(store)
    artifacts = RunArtifactResolver(root / 'runs')
    delivery = VersionDeliveryService(store, artifacts)
    pool = ProjectImagePoolService(store, data_root=root / 'runtime')
    quality = WorkspaceBoundVersionQuality(store=store, delivery=delivery, artifacts=artifacts,
                                           data_root=root / 'quality-evidence')
    commands = ManualReviewCommandService(ManualReviewCommandServices(store=store, delivery=delivery, quality=quality,
        final_confirmation=WorkspaceBoundFinalConfirmation(quality=quality, actor_resolver=WorkspaceReviewerActorResolver(store)),
        evidence_authority=ReviewEvidenceAuthority(FileReviewEvidenceArtifactStore(root / 'review-evidence'))))
    images = FormalImageWorkspaceService(FormalImageWorkspaceServices(store=store, projects=projects,
        delivery=delivery, artifacts=artifacts, images=pool), runs_root=root / 'runs', source_root=root / 'source',
        package_registry=created['packages'].registry)
    script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(store=store, projects=projects,
        delivery=delivery, commands=commands, artifacts=artifacts, image_workspace=images),
        runs_root=root / 'runs', source_root=root / 'source', package_registry=created['packages'].registry)
    revision = FormalRevisionService(projects=projects, tasks=tasks, workspace=script, image_workspace=images, writes_enabled=True)
    readonly = ReadonlyAgentService(ReadonlyAgentServices(store=store, identity=LocalIdentityAdapter(store),
        projects=projects, tasks=tasks, versions=VersionService(store), artifacts=artifacts))
    agent = FormalExecutableAgentService(ExecutableAgentServices(readonly=readonly, projects=projects, tasks=tasks,
        confirmations=ActionConfirmationService(store, b'a' * 32), idempotency=ToolIdempotencyService(store),
        brief_store=BriefArtifactStore(root / 'runtime'), backend=FormalExecutionBackend()),
        generation=FakeFormalGeneration(), advisor=None, writes_enabled=True, script_revision=revision)
    review = FormalReviewWorkspaceService(FormalReviewWorkspaceServices(store=store, projects=projects,
        delivery=delivery, artifacts=artifacts, commands=commands, image_workspace=images))
    return dict(projects=projects, artifacts=artifacts, delivery=delivery, pool=pool, images=images,
                script=script, revision=revision, agent=agent, review=review)


def run_delivery(created):
    root, store, token, scope = (created[key] for key in ('root', 'store', 'token', 'scope'))
    services = build_workspaces(created)
    script, images, pool = (services[key] for key in ('script', 'images', 'pool'))
    current = script.read_current(token, **scope)
    count = len(current['shots'])
    assert count == 25
    _, context = script._current_context(token, **scope)
    frozen = json.loads(services['artifacts'].resolve_stage_artifact(context.run_id, 'script_plan').path.read_text())
    binding = frozen['confirmed_brief_input']
    brief_version_id, brief_snapshot_id = binding['brief_version_id'], binding['brief_snapshot_id']
    rows = created['result']['script_rows']
    planned_by_no = {str(row['shot_no']): row for row in frozen['shot_plans']}
    pictures = Path(__file__).parent / 'fixtures' / 'formal_visual_quality'
    variants = list(_photo_variants([pictures / 'anonymous-launch-atlas-a.png', pictures / 'anonymous-launch-atlas-b.png'], count))
    analysis = ManualProjectImageAnalysisCommandService(pool)
    upload_evidence = []
    for index, (row, content) in enumerate(zip(rows, variants), start=1):
        filename = f'anonymous-annual-shot-{index:02d}.jpg'
        image_id = pool.adopt_local_upload(token, **scope, brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id, expected_project_lock_version=current['project_version'],
            filename=filename, content=content)
        planned = planned_by_no[str(row['镜号'])]
        # These observations are declared anonymous fixture data derived from the
        # current annual shot, rather than the launch fixture's numbered functions.
        visual = str(row['画面描述'])
        structure = str(row['结构'])
        technical = '；'.join(str(row.get(key) or '') for key in ('类型', '景别', '运镜'))
        observation = f'年会现场；{visual}；{technical}'
        analysis.record_analysis(token, **scope, expected_project_lock_version=current['project_version'],
            submission=ManualImageAnalysisInput(image_id=image_id, image_sha256=sha256(content).hexdigest(),
                brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id, status='pass',
                caption=observation, tags=('年会', structure, f'镜{index}'), subjects=('参与者', '活动空间'),
                scene=observation, purpose=f'{structure}；{planned.get("primary_event") or visual}',
                quality='匿名测试图片清晰可读', brand='匿名团队',
                evidence=('隔离匿名fixture：图片分析为模拟输入，不代表真实图片内容或人工质量认证',)))
        upload_evidence.append({'shot_no': str(row['镜号']), 'image_id': image_id,
                                'sha256': sha256(content).hexdigest(), 'filename': filename})
    assert len({item['sha256'] for item in upload_evidence}) == count
    (root / 'upload-evidence.json').write_text(json.dumps(upload_evidence, ensure_ascii=False, indent=2) + '\n')
    selections = []
    for shot, uploaded in zip(current['shots'], upload_evidence):
        found = images.search_shot(token, **scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'], shot_no=shot['shot_no'], query='')
        assert uploaded['image_id'] in {item['image_key'] for item in found['candidates']}, (shot['shot_no'], found.get('gap_reason'))
        selections.append({'shot_id': shot['shot_id'], 'image_key': uploaded['image_id'], 'checked': False,
                           'reuse_reason': '隔离匿名验收：已逐镜核对相似场景用途'})
    progress = images.read_progress(token, **scope)
    progress = images.save_progress(token, **scope, base_version_id=current['base_version_id'],
        expected_project_version=current['project_version'], expected_revision=progress['revision'],
        expected_script_revision=current['revision'], selections=selections, idempotency_key='creation-unchecked-images')
    assert len(progress['selections']) == count and all(not item['checked'] for item in progress['selections'])
    args = dict(**scope, base_version_id=current['base_version_id'], expected_project_version=current['project_version'],
                expected_revision=progress['revision'], expected_script_revision=current['revision'], selections=selections)
    preview = images.preview_progress_confirmation(token, **args)
    (root / 'image-confirmation-preview.json').write_text(json.dumps(preview, ensure_ascii=False, indent=2) + '\n')
    assert preview['processable_count'] == count and preview['pending_count'] == 0
    args.update(preview_digest=preview['preview_digest'], accepted_shot_ids=[item['shot_id'] for item in selections],
                idempotency_key='creation-confirm-all')
    confirmed_images = images.confirm_progress(token, **args)
    assert confirmed_images['confirmed_count'] == count and all(item['checked'] for item in confirmed_images['selections'])
    assert images.confirm_progress(token, **args) == confirmed_images
    restarted = build_workspaces(created, store=WorkspaceStore(store.database_path))
    restarted_progress = restarted['images'].read_progress(token, **scope)
    assert [(item['shot_id'], item['image_key'], item['checked']) for item in restarted_progress['selections']] == [
        (item['shot_id'], item['image_key'], item['checked']) for item in confirmed_images['selections']]
    assert restarted['images'].confirm_progress(token, **args) == confirmed_images
    with store.connection() as connection:
        assert connection.execute('SELECT count(*) FROM project_versions').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM version_review_human_decisions').fetchone()[0] == 0
    prepared = images.prepare_working_version(token, **scope, expected_project_version=current['project_version'],
        expected_script_revision=current['revision'], expected_progress_revision=confirmed_images['revision'])
    assert prepared['shot_count'] == count
    for decision in prepared['decisions']:
        assert sha256(Path(decision['controlled_path']).read_bytes()).hexdigest() == decision['image_sha256']
        location = decision['source_location']
        assert location['file'] and location['sheet'] and int(location['row']) > 0
    proposal = services['revision'].propose_draft_save(token, project_id=scope['project_id'],
        expected_project_version=current['project_version'], expected_revision=current['revision'],
        expected_progress_revision=confirmed_images['revision'], reason='隔离年会25镜文字及确认图片统一保存',
        idempotency_key='creation-unified-save')
    result = services['agent'].confirm_action(token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])
    assert result['task']['status'] == 'completed', result
    assert services['agent'].confirm_action(token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])['task']['status'] == 'completed'
    saved = script.read_current(token, **scope)
    assert saved['shots'] == current['shots']
    assert images.read_progress(token, **scope)['selections'] == []
    with store.connection() as connection:
        assert connection.execute('SELECT count(*) FROM project_versions').fetchone()[0] == 2
        assert connection.execute('SELECT count(*) FROM version_review_human_decisions').fetchone()[0] == 0
    review = services['review']
    current_review = review.read_current(token, **scope)
    assert current_review['state'] == 'ready' and not current_review['review_started'], current_review
    started = review.start_review(token, **scope, expected_project_version=current_review['project_version'], idempotency_key='creation-review')
    approved = review.decide(token, **scope, expected_project_version=started['project_version'],
        issue_keys=tuple(item['issue_key'] for item in started['issues']), decision='approve',
        note='隔离匿名测试模拟逐项审核，不是生产人工结论', idempotency_key='creation-approve')
    final = review.confirm_final(token, **scope, expected_project_version=approved['project_version'],
        note='隔离匿名测试模拟整片确认，不代表真实业务质量', idempotency_key='creation-final')
    assert final['state'] == 'confirmed'
    delivery = review.prepare_excel_delivery(token, **scope, expected_project_version=final['project_version'])
    final_path = root / 'M+隔离路径A正式交付-v1.xlsx'
    shutil.copy2(delivery.path, final_path)
    workbook = load_workbook(final_path)
    try:
        assert len(workbook['脚本']._images) == count
        edges = [row for row in workbook['相邻镜头复核'].iter_rows(min_row=2, values_only=True) if any(value is not None for value in row)]
        assert len(edges) == count - 1
        assert workbook['审核记录'].cell(workbook['审核记录'].max_row, 3).value == '通过'
    finally:
        workbook.close()
    with store.connection() as connection:
        artifact_ref = connection.execute("SELECT artifact_ref FROM delivery_artifact_bindings WHERE artifact_kind='excel' AND availability='available'").fetchone()[0]
    trace = review.delivery_snapshots.reverse_trace(token, project_id=scope['project_id'], artifact_ref=artifact_ref)
    assert trace['status'] == 'verified' and trace['image_decision_count'] == count
    assert all(row['source_file'] and row['source_sheet'] and int(row['source_row']) > 0 for row in trace['source_trace'])
    recovered_services = build_workspaces(created, store=WorkspaceStore(store.database_path))
    recovered = recovered_services['review'].read_current(token, **scope)
    assert recovered['state'] == 'confirmed'
    recovered_delivery = recovered_services['review'].prepare_excel_delivery(token, **scope, expected_project_version=recovered['project_version'])
    digest = sha256(final_path.read_bytes()).hexdigest()
    assert sha256(recovered_delivery.path.read_bytes()).hexdigest() == digest
    image_model = recovered_services['images'].read_current(token, **scope)
    expected = {item['shot_no']: item['sha256'] for item in upload_evidence}
    assert {item['shot_no']: item['current_image']['content_sha256'] for item in image_model['shots']} == expected
    summary = {'scope': 'complete_anonymous_service_journey_A', 'creation': created['summary'],
        'shot_count': count, 'adjacent_count': len(edges), 'image_count': count,
        'analysis': 'explicit_simulated_manual_fixture_not_visual_quality_certification',
        'image_sources_verified': count, 'confirmed_progress_survives_restart': True,
        'version_count': 2, 'human_decisions_before_review': 0, 'review_state_after_restart': recovered['state'],
        'excel': {'path': str(final_path), 'sha256': digest}, 'reverse_trace': trace,
        'runtime': str(store.database_path), 'ui_journey': 'not_exercised_by_this_service_test'}
    (root / 'complete-evidence.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


def run_creation_delivery(root):
    return run_delivery(run_creation(Path(root)))


class HandshakeCreationDeliveryTests(unittest.TestCase):
    def test_new_annual_project_reaches_25_shot_reviewed_excel_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='mplus-handshake-complete-') as directory:
            result = run_creation_delivery(Path(directory))
            self.assertEqual((result['shot_count'], result['adjacent_count']), (25, 24))


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--output':
        print(json.dumps(run_creation_delivery(Path(sys.argv[2])), ensure_ascii=False, indent=2))
    else:
        unittest.main()
