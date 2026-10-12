import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from mvp_v2 import package_registry as module
from mvp_v2.package_registry import LocalPackageRegistry, PackageRegistryError
from tests import test_mvp_v2_package_registry as registry_tests


class PackageVerificationReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = registry_tests.PackageRegistryTests()._package(self.root)
        self.registry = LocalPackageRegistry(self.root / 'registry.json')
        self.record = self.registry.register(self.package)
        self.registry.promote('launch', 'launch-core', 4)

    def test_verified_package_survives_process_restart_without_rehashing(self):
        first = self.registry.resolve_formal('launch')
        code = '''
import json, sys
from unittest.mock import patch
from mvp_v2.package_registry import LocalPackageRegistry
with patch('mvp_v2.package_registry.build_package_record', side_effect=AssertionError('unchanged verified package was rehashed after restart')):
    record = LocalPackageRegistry(sys.argv[1]).resolve_formal('launch')
print(json.dumps(record))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.registry.path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), first)

    def test_parallel_first_reads_share_one_integrity_scan(self):
        barrier = threading.Barrier(4)
        original = module.build_package_record

        def build(path):
            time.sleep(.05)
            return original(path)

        def read(_):
            barrier.wait()
            return LocalPackageRegistry(self.registry.path).resolve_formal('launch')

        with patch.object(module, 'build_package_record', side_effect=build) as scan:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(read, range(4)))
        self.assertEqual(scan.call_count, 1)
        self.assertTrue(all(item == results[0] for item in results))

    def test_mutating_resolved_record_cannot_change_verified_file_evidence(self):
        resolved = self.registry.resolve_formal('launch')
        item = resolved['files'][0]
        path = Path(resolved['local_path']) / item['path']
        expected = item['sha256']
        item['sha256'] = '0' * 64
        self.assertEqual(self.registry.verified_file_sha256(resolved, path), expected)
        self.assertEqual(self.registry.resolve_formal('launch')['files'][0]['sha256'], expected)

    def test_same_size_change_with_restored_mtime_invalidates_restart_receipt(self):
        self.registry.resolve_formal('launch')
        rules = self.package.parent / 'template_rules.json'
        stat = rules.stat()
        raw = rules.read_bytes()
        self.assertIn(b'launch', raw)
        rules.write_bytes(raw.replace(b'launch', b'launcX', 1))
        os.utime(rules, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        module._RESOLVED_PACKAGE_CACHE.clear()
        with self.assertRaises(PackageRegistryError):
            LocalPackageRegistry(self.registry.path).resolve_formal('launch')

    def test_corrupt_receipt_falls_back_to_real_scan(self):
        self.registry.resolve_formal('launch')
        cache = self.registry.path.parent / '.package-verification-cache'
        receipts = list(cache.glob('*.json'))
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        payload = json.loads(receipt.read_text())
        payload['signature'] = '0' * 64
        receipt.write_text(json.dumps(payload))
        module._RESOLVED_PACKAGE_CACHE.clear()
        with patch.object(module, 'build_package_record', wraps=module.build_package_record) as scan:
            result = LocalPackageRegistry(self.registry.path).resolve_formal('launch')
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(result['package_sha256'], self.record['package_sha256'])

    def test_cache_storage_failure_does_not_block_verified_package(self):
        (self.root / '.package-verification-cache').write_text('unavailable optional cache')
        result = self.registry.resolve_formal('launch')
        self.assertEqual(result['package_sha256'], self.record['package_sha256'])

    def test_new_unregistered_file_cannot_hide_behind_restart_receipt(self):
        self.registry.resolve_formal('launch')
        (self.package.parent / 'unexpected.txt').write_text('unregistered input')
        module._RESOLVED_PACKAGE_CACHE.clear()
        with self.assertRaises(PackageRegistryError):
            LocalPackageRegistry(self.registry.path).resolve_formal('launch')
