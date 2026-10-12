"""RV06-01: a failed creator cannot delete content adopted by another request."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys

from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.creative_standard import CreativeStandardArtifactStore
from mvp_v2.formal_brief_workspace_service import FormalBriefArtifactStore
from mvp_v2.formal_brief_input_adapter import _FormalInputStore


class SharedBlobRollbackTests(unittest.TestCase):
    def test_shared_payload_survives_creating_request_rollback(self):
        for cls in (BriefArtifactStore, FormalBriefArtifactStore, CreativeStandardArtifactStore):
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as root:
                creator, adopter = cls(root), cls(root)
                published, adopted = threading.Event(), threading.Event()
                def write(store):
                    return store.freeze({'项目名称': '匿名共同资料'}) if cls is BriefArtifactStore else store.write(b'{"shared":true}')
                def read(store, ref):
                    return store.read(ref['brief_ref'], ref['brief_sha256']) if cls is BriefArtifactStore else store.read(ref)
                def failing_request():
                    checkpoint = creator.checkpoint()
                    write(creator)
                    published.set()
                    self.assertTrue(adopted.wait(5))
                    creator.rollback_to(checkpoint)
                def successful_request():
                    self.assertTrue(published.wait(5))
                    ref = write(adopter)
                    read(adopter, ref)
                    adopted.set()
                    return ref
                with ThreadPoolExecutor(2) as pool:
                    a, b = pool.submit(failing_request), pool.submit(successful_request)
                    a.result(10)
                    ref = b.result(10)
                self.assertTrue(read(adopter, ref))

    def test_promoted_upload_survives_other_batch_commit_then_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            store = _FormalInputStore(root)
            source = Path(root) / 'input.txt'
            source.write_bytes(b'anonymous shared input')
            digest = sha256(source.read_bytes()).hexdigest()
            first, second = store.begin_batch(), store.begin_batch()
            first.stage_temp(source, digest, '.txt')
            first.promote()
            reference, _ = second.stage_temp(source, digest, '.txt')
            second.promote()
            second.commit()
            first.rollback()
            self.assertEqual(store.read(reference, suffix='.txt'), source.read_bytes())
            self.assertFalse(first.path.exists())
            self.assertFalse(second.path.exists())

    def test_promoted_blob_survives_process_exit_and_later_adoption(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'input.txt'
            source.write_bytes(b'anonymous process recovery')
            digest = sha256(source.read_bytes()).hexdigest()
            program = '''
import os,sys
from pathlib import Path
from mvp_v2.formal_brief_input_adapter import _FormalInputStore
store = _FormalInputStore(sys.argv[1])
batch = store.begin_batch()
batch.stage_temp(Path(sys.argv[2]), sys.argv[3], '.txt')
batch.promote()
os._exit(19)
'''
            result = subprocess.run([sys.executable, '-c', program, root, str(source), digest],
                timeout=15, capture_output=True)
            self.assertEqual(result.returncode, 19, result.stderr.decode())
            store = _FormalInputStore(root)
            adopter = store.begin_batch()
            ref, _ = adopter.stage_temp(source, digest, '.txt')
            adopter.promote()
            adopter.commit()
            self.assertEqual(store.read(ref, suffix='.txt'), source.read_bytes())

    def test_promote_rejects_existing_mismatched_blob_without_overwriting(self):
        from mvp_v2.formal_brief_input_adapter import FormalBriefInputError
        with tempfile.TemporaryDirectory() as root:
            store = _FormalInputStore(root)
            source = Path(root) / 'input.txt'
            source.write_bytes(b'expected bytes')
            batch = store.begin_batch()
            batch.stage_temp(source, sha256(source.read_bytes()).hexdigest(), '.txt')
            target = batch.entries[0][1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'existing inconsistent evidence')
            with self.assertRaisesRegex(FormalBriefInputError, 'ARTIFACT_HASH_MISMATCH'):
                batch.promote()
            batch.rollback()
            self.assertEqual(target.read_bytes(), b'existing inconsistent evidence')
