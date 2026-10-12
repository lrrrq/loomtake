from contextlib import contextmanager
import unittest

from mvp_v2.cloud_packages import sync_packages
from mvp_v2.cloud_runtime import CloudRuntimeError


class Repository:
    @contextmanager
    def package_sync_lock(self):
        yield

    def preflight_packages(self, records):
        raise CloudRuntimeError("不可变资产包版本冲突")


class Storage:
    def __init__(self):
        self.put_count = 0

    def put_file(self, *args, **kwargs):
        self.put_count += 1


class CloudPackageTests(unittest.TestCase):
    def test_batch_conflict_blocks_before_any_cos_upload(self):
        storage = Storage()
        with self.assertRaisesRegex(CloudRuntimeError, "版本冲突"):
            sync_packages([{"package_id": "launch-core"}], storage, Repository())
        self.assertEqual(storage.put_count, 0)


if __name__ == "__main__":
    unittest.main()
