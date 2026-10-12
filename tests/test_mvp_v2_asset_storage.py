from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from mvp_v2.asset_storage import AssetStorageError, LocalAssetStorage


class LocalAssetStorageTests(unittest.TestCase):
    def test_put_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalAssetStorage(Path(tmp) / "blobs")
            first_ref, first_sha = storage.put(BytesIO(b"same-content"))
            second_ref, second_sha = storage.put(BytesIO(b"same-content"))

            self.assertEqual(first_ref, second_ref)
            self.assertEqual(first_sha, second_sha)
            self.assertTrue(storage.verify(first_ref, first_sha))
            self.assertEqual(len(list((Path(tmp) / "blobs").glob("*/*"))), 1)

    def test_open_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalAssetStorage(Path(tmp) / "blobs")
            storage_ref, _ = storage.put(BytesIO(b"original"))
            storage.path_for(storage_ref).write_bytes(b"changed")

            with self.assertRaisesRegex(AssetStorageError, "STORAGE_INTEGRITY_ERROR"):
                storage.open(storage_ref)

    def test_materialize_never_overwrites_different_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = LocalAssetStorage(root / "blobs")
            storage_ref, _ = storage.put(BytesIO(b"asset"))
            target = root / "out" / "asset.bin"
            storage.materialize(storage_ref, target)
            self.assertEqual(target.read_bytes(), b"asset")
            target.write_bytes(b"different")

            with self.assertRaisesRegex(AssetStorageError, "STORAGE_TARGET_CONFLICT"):
                storage.materialize(storage_ref, target)


if __name__ == "__main__":
    unittest.main()
