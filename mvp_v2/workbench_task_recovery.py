"""Reconcile workbench tasks through their existing execution lock and events."""

from . import creative_workbench
from .run_state import try_exclusive_lock


def validate_child_parent(tasks, auth_token, child, parent_task_id):
    from .workspace_store import WorkspaceServiceError
    parent = tasks.get_task(auth_token, parent_task_id)
    if (parent['project_id'] != child['project_id'] or parent['agent_session_id'] != child['agent_session_id']
            or parent['task_type'] != 'formal_production_advisor'):
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '联合建议父任务与当前项目会话不一致')
    return parent['task_id']


def parent_for_child(tasks, auth_token, task_id):
    """Resolve only a persisted, same-project workbench relationship."""
    child = tasks.get_task(auth_token, task_id)
    with tasks.store.connection() as connection:
        marker = connection.execute("""SELECT json_extract(details_json,'$.workbench_parent_task_id') AS parent
            FROM task_events WHERE task_id=? AND json_extract(details_json,'$.workbench_parent_task_id') IS NOT NULL
            ORDER BY rowid DESC LIMIT 1""", (task_id,)).fetchone()
        if marker:
            return validate_child_parent(tasks, auth_token, child, marker['parent'])
        row = connection.execute("""
            SELECT p.task_id FROM task_events e JOIN tasks p ON p.task_id=e.task_id
            WHERE p.project_id=? AND p.agent_session_id=?
              AND p.task_type='formal_production_advisor'
              AND (json_extract(e.details_json,'$.workbench.text_task_id')=?
                   OR json_extract(e.details_json,'$.workbench.image_task_id')=?)
            ORDER BY e.rowid DESC LIMIT 1
        """, (child['project_id'], child['agent_session_id'], task_id, task_id)).fetchone()
    if row:
        # Keep the ordinary ownership check even though the child is authorized.
        return tasks.get_task(auth_token, row['task_id'])['task_id']
    return None


def require_standalone_child(tasks, auth_token, task_id):
    from .workspace_store import WorkspaceServiceError
    if parent_for_child(tasks, auth_token, task_id):
        raise WorkspaceServiceError('WORKBENCH_CONFIRMATION_REQUIRED',
            '这是创作助手联合建议的一部分，请打开创作助手，按完整段落组采用或重新预览。')


def sync_workbench(agent, auth_token, task_id):
    task, _, _ = creative_workbench._load(agent, auth_token, task_id)
    if task['status'] != 'executing':
        return creative_workbench.read(agent, auth_token, task_id)

    lock_path = agent.services.store.database_path.parent / '.workbench-actions' / (task_id + '.lock')
    with try_exclusive_lock(lock_path, run_id=task_id) as acquired:
        if not acquired:
            return creative_workbench.read(agent, auth_token, task_id)
        # Execution may have finished between the first read and acquiring its
        # lock. Only the persisted state observed under that lock is decisive.
        task, data, _ = creative_workbench._load(agent, auth_token, task_id)
        if task['status'] != 'executing':
            return creative_workbench.read(agent, auth_token, task_id)
        instruction = data['request']['content']
        response = {
            'type': 'workbench_failed',
            'task_id': task_id,
            'stage': data['before']['stage'],
            'title': '本次分析已中断',
            'summary': '本次分析已中断，尚未采用本次修改。原稿、已保存图片与原要求均已保留；请重新预览这条要求。',
            'error_code': 'WORKBENCH_INTERRUPTED',
            'choices': [{'label': '重新预览这条要求', 'instruction': instruction}],
        }
        return creative_workbench._record(agent, auth_token, task, data, response, 'failed')
