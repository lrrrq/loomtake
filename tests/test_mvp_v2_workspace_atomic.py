from concurrent.futures import ThreadPoolExecutor

import pytest

from mvp_v2.workspace_store import WorkspaceStore


def test_composed_authority_writes_rollback_together(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite3')
    store.initialize()
    with store.transaction() as conn:
        conn.execute('CREATE TABLE atomic_probe (value TEXT)')
    same_database = WorkspaceStore(store.database_path)
    with pytest.raises(ValueError, match='second action'):
        with store.atomic():
            with same_database.transaction() as conn:
                conn.execute("INSERT INTO atomic_probe VALUES ('text')")
            same_database.initialize()
            with same_database.connection() as conn:
                assert conn.execute('SELECT value FROM atomic_probe').fetchone()[0] == 'text'
            with same_database.transaction() as conn:
                conn.execute("INSERT INTO atomic_probe VALUES ('image')")
                raise ValueError('second action failed')
    with store.connection() as conn:
        assert conn.execute('SELECT * FROM atomic_probe').fetchall() == []


def test_nested_rollback_isolated_and_unrelated_reader_sees_committed_state(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite3')
    store.initialize()
    with store.transaction() as conn:
        conn.execute('CREATE TABLE atomic_probe (value TEXT)')
    def values():
        with store.connection() as conn:
            return [row[0] for row in conn.execute('SELECT value FROM atomic_probe')]
    with store.atomic():
        with store.transaction() as conn:
            conn.execute("INSERT INTO atomic_probe VALUES ('text')")
        with pytest.raises(ValueError):
            with store.atomic() as conn:
                conn.execute("INSERT INTO atomic_probe VALUES ('discard')")
                raise ValueError()
        assert values() == ['text']
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(values).result() == []
    assert values() == ['text']
