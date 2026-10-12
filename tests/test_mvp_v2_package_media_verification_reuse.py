from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.package_registry import LocalPackageRegistry
from mvp_v2.run_contract import sha256_file
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_library_browse import build_anonymous_library_package


class PackageMediaVerificationReuseTests(unittest.TestCase):
    def test_registered_media_reuses_verified_content_and_still_rejects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = build_anonymous_library_package(root / 'fixture')
            registry = LocalPackageRegistry(root / 'registry.json')
            record = registry.register(package)
            project = {'category_id': 'launch',
                       'template_package_ref': 'anonymous-gallery@1',
                       'template_package_sha256': record['package_sha256']}
            service = FormalDefaultImageService(
                projects=None, tasks=None, delivery=None, artifacts=None,
                image_pool=None, image_workspace=None, package_registry=registry,
                asset_package_resolver=lambda _: package, writes_enabled=False)
            with patch('mvp_v2.formal_default_images.sha256_file', wraps=sha256_file) as hashing:
                _, media = service._package_media(project, {})
            media_paths = {item['path'] for item in media}
            media_hashes = [call for call in hashing.call_args_list
                            if str(Path(call.args[0]).resolve()) in media_paths]
            self.assertGreater(len(media), 24)
            self.assertEqual(media_hashes, [], 'registered images were already verified')
            code = '''
import json, sys
from unittest.mock import patch
from PIL import Image
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_image_candidates import _package_search_inputs
from mvp_v2.package_registry import LocalPackageRegistry
registry = LocalPackageRegistry(sys.argv[1])
service = FormalDefaultImageService(projects=None, tasks=None, delivery=None, artifacts=None,
    image_pool=None, image_workspace=None, package_registry=registry,
    asset_package_resolver=lambda _: None, writes_enabled=False)
with patch('PIL.Image.open', wraps=Image.open) as decoding:
    _, media = service._package_media(json.loads(sys.argv[2]), {})
    _package_search_inputs(media)
assert bool(decoding.call_count) == (sys.argv[3] == 'recompute'), decoding.call_count
print(len(media))
'''
            restarted = subprocess.run([sys.executable, '-c', code, str(registry.path),
                json.dumps(project), 'cached'], cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(int(restarted.stdout), len(media))
            cache_file = next((root / '.package-verification-cache').glob('pixels-*.json'))
            payload = json.loads(cache_file.read_text())
            payload['signature'] = '0' * 64
            cache_file.write_text(json.dumps(payload))
            recomputed = subprocess.run([sys.executable, '-c', code, str(registry.path),
                json.dumps(project), 'recompute'], cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True)
            self.assertEqual(recomputed.returncode, 0, recomputed.stderr)
            self.assertEqual(int(recomputed.stdout), len(media))
            path = Path(media[0]['path'])
            path.write_bytes(b'changed anonymous content')
            with self.assertRaises(WorkspaceServiceError):
                service._package_media(project, {})
