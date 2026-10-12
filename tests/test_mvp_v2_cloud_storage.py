import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from mvp_v2.cloud_storage import (
    CloudStorageError,
    CosObjectStorage,
    cos_ref,
    parse_cos_ref,
)


class _NotFound(Exception):
    def get_status_code(self):
        return 404

    def get_error_code(self):
        return "NoSuchKey"


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def get_raw_stream(self):
        return io.BytesIO(self.payload)


class FakeCosClient:
    def __init__(self):
        self.objects = {}
        self.upload_calls = 0
        self.public = False

    def get_bucket_acl(self, *, Bucket):
        grantee = (
            {"Type": "Group", "URI": "http://cam.qcloud.com/groups/global/AllUsers"}
            if self.public
            else {"Type": "CanonicalUser", "ID": "owner"}
        )
        return {"AccessControlList": {"Grant": [{"Grantee": grantee, "Permission": "FULL_CONTROL"}]}}

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _NotFound()
        payload, metadata = self.objects[(Bucket, Key)]
        return {"Content-Length": len(payload), "x-cos-meta-sha256": metadata["sha256"]}

    def upload_file(self, *, Bucket, Key, LocalFilePath, Metadata, **_kwargs):
        self.upload_calls += 1
        self.objects[(Bucket, Key)] = (Path(LocalFilePath).read_bytes(), dict(Metadata))

    def put_object(self, *, Bucket, Key, Body, ContentLength, Metadata):
        payload = Body.read()
        assert len(payload) == ContentLength
        self.objects[(Bucket, Key)] = (payload, dict(Metadata))

    def download_file(self, *, Bucket, Key, DestFilePath):
        Path(DestFilePath).write_bytes(self.objects[(Bucket, Key)][0])

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)][0])}

    def get_presigned_download_url(self, *, Bucket, Key, Expired):
        return f"https://signed.example/{Bucket}/{Key}?expires={Expired}"


class CloudStorageTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeCosClient()
        self.storage = CosObjectStorage(
            region="ap-guangzhou",
            bucket="private-123",
            secret_id="unused",
            secret_key="unused",
            client=self.client,
        )

    def test_resumable_upload_is_sha_idempotent_and_download_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "brief.xlsx"
            source.write_bytes(b"anonymous-xlsx")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            first = self.storage.put_file(source, expected_sha256=digest)
            second = self.storage.put_file(source, expected_sha256=digest)
            target = self.storage.download_file(first.storage_ref, root / "download.xlsx")

            self.assertEqual(first, second)
            self.assertEqual(self.client.upload_calls, 1)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(self.storage.verify(first.storage_ref, digest, source.stat().st_size))
            self.assertIn("expires=600", self.storage.signed_download_url(first.storage_ref))

    def test_existing_object_with_wrong_metadata_never_gets_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.txt"
            source.write_text("truth", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            key = self.storage.content_key(digest)
            self.client.objects[(self.storage.bucket, key)] = (source.read_bytes(), {"sha256": "0" * 64})

            with self.assertRaisesRegex(CloudStorageError, "完整性"):
                self.storage.put_file(source)
            self.assertEqual(self.client.upload_calls, 0)

    def test_cos_reference_binds_bucket_key_and_sha(self):
        digest = "a" * 64
        value = cos_ref("private-123", "mplus/a b.txt", digest)
        self.assertEqual(parse_cos_ref(value), ("private-123", "mplus/a b.txt", digest))
        with self.assertRaises(CloudStorageError):
            parse_cos_ref("https://example.invalid/file")

    def test_environment_storage_rejects_a_public_bucket(self):
        environment = {
            "MPLUS_COS_REGION": "ap-guangzhou",
            "MPLUS_COS_BUCKET": "private-123",
            "MPLUS_COS_SECRET_ID": "unused",
            "MPLUS_COS_SECRET_KEY": "unused",
        }
        self.assertTrue(
            CosObjectStorage.from_environment(environment, client=self.client).assert_private_bucket()
        )
        self.client.public = True
        with self.assertRaisesRegex(CloudStorageError, "公共访问"):
            CosObjectStorage.from_environment(environment, client=self.client)


if __name__ == "__main__":
    unittest.main()
