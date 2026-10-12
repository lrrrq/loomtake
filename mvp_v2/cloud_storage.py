from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

from .run_contract import sha256_file


COS_REF_SCHEME = "cos"


class CloudStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudObject:
    storage_ref: str
    sha256: str
    size_bytes: int
    bucket: str
    key: str


class CosObjectStorage:
    """Private Tencent COS adapter with content verification and resumable uploads."""

    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        secret_id: str,
        secret_key: str,
        token: str = "",
        prefix: str = "mplus",
        client=None,
    ) -> None:
        self.region = _required(region, "COS region")
        self.bucket = _required(bucket, "COS bucket")
        self.prefix = _safe_key(prefix).rstrip("/")
        if client is None:
            if not secret_id or not secret_key:
                raise CloudStorageError("COS 凭据缺失，拒绝回退到本地存储")
            try:
                from qcloud_cos import CosConfig, CosS3Client
            except ImportError as exc:
                raise CloudStorageError("未安装 cos-python-sdk-v5") from exc
            config = CosConfig(
                Region=self.region,
                SecretId=secret_id,
                SecretKey=secret_key,
                Token=token or None,
                Scheme="https",
            )
            client = CosS3Client(config)
        self.client = client

    @classmethod
    def from_environment(cls, environ: dict | None = None, *, client=None):
        values = os.environ if environ is None else environ
        storage = cls(
            region=values.get("MPLUS_COS_REGION") or "",
            bucket=values.get("MPLUS_COS_BUCKET") or "",
            secret_id=values.get("MPLUS_COS_SECRET_ID") or "",
            secret_key=values.get("MPLUS_COS_SECRET_KEY") or "",
            token=values.get("MPLUS_COS_TOKEN") or "",
            prefix=values.get("MPLUS_COS_PREFIX") or "mplus",
            client=client,
        )
        storage.assert_private_bucket()
        return storage

    def assert_private_bucket(self) -> bool:
        try:
            response = dict(self.client.get_bucket_acl(Bucket=self.bucket))
        except Exception as exc:
            raise CloudStorageError(f"COS 私有桶 ACL 检查失败: {type(exc).__name__}") from exc
        grants = dict(response.get("AccessControlList") or {}).get("Grant") or []
        if isinstance(grants, dict):
            grants = [grants]
        if not isinstance(grants, list):
            raise CloudStorageError("COS 私有桶 ACL 结构无效")
        for grant in grants:
            grantee = dict(dict(grant or {}).get("Grantee") or {})
            identity = " ".join(str(value) for value in grantee.values()).casefold()
            if "allusers" in identity or "anonymous" in identity:
                raise CloudStorageError("COS 桶存在公共访问授权，拒绝运行")
        return True

    def content_key(self, sha256: str, *, namespace: str = "objects") -> str:
        digest = _sha256(sha256)
        return self.key(f"{_safe_key(namespace)}/{digest[:2]}/{digest}")

    def key(self, relative: str) -> str:
        suffix = _safe_key(relative)
        return f"{self.prefix}/{suffix}" if self.prefix else suffix

    def put_file(
        self,
        path: str | Path,
        *,
        key: str | None = None,
        expected_sha256: str | None = None,
    ) -> CloudObject:
        source = Path(path).expanduser().resolve()
        if not source.is_file() or source.is_symlink():
            raise CloudStorageError("COS 上传源必须是普通文件")
        digest = sha256_file(source)
        if expected_sha256 and digest != _sha256(expected_sha256):
            raise CloudStorageError("上传前文件 SHA-256 不一致")
        object_key = _safe_key(key) if key else self.content_key(digest)
        existing = self._head_optional(object_key)
        if existing is not None:
            self._verify_head(existing, digest, source.stat().st_size)
            return self._object(object_key, digest, source.stat().st_size)
        try:
            self.client.upload_file(
                Bucket=self.bucket,
                Key=object_key,
                LocalFilePath=str(source),
                PartSize=10,
                MAXThread=4,
                EnableMD5=True,
                Metadata={"sha256": digest},
            )
        except Exception as exc:
            raise CloudStorageError(f"COS 上传失败: {type(exc).__name__}") from exc
        self._verify_head(self._head_required(object_key), digest, source.stat().st_size)
        return self._object(object_key, digest, source.stat().st_size)

    def put_bytes(
        self,
        payload: bytes,
        *,
        key: str | None = None,
        expected_sha256: str | None = None,
    ) -> CloudObject:
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 and digest != _sha256(expected_sha256):
            raise CloudStorageError("上传前内容 SHA-256 不一致")
        object_key = _safe_key(key) if key else self.content_key(digest)
        existing = self._head_optional(object_key)
        if existing is None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=object_key,
                    Body=io.BytesIO(payload),
                    ContentLength=len(payload),
                    Metadata={"sha256": digest},
                )
            except Exception as exc:
                raise CloudStorageError(f"COS 上传失败: {type(exc).__name__}") from exc
            existing = self._head_required(object_key)
        self._verify_head(existing, digest, len(payload))
        return self._object(object_key, digest, len(payload))

    def download_file(
        self,
        storage_ref: str,
        target: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        bucket, key, ref_sha = parse_cos_ref(storage_ref)
        if bucket != self.bucket:
            raise CloudStorageError("COS 对象引用不属于当前私有桶")
        expected = _sha256(expected_sha256 or ref_sha)
        destination = Path(target).expanduser()
        if destination.exists():
            if destination.is_file() and sha256_file(destination) == expected:
                return destination
            raise CloudStorageError("COS 下载目标冲突")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.cos-{uuid.uuid4().hex}.tmp")
        try:
            try:
                self.client.download_file(
                    Bucket=self.bucket,
                    Key=key,
                    DestFilePath=str(temporary),
                )
            except Exception as exc:
                raise CloudStorageError(f"COS 下载失败: {type(exc).__name__}") from exc
            if sha256_file(temporary) != expected:
                raise CloudStorageError("COS 下载后 SHA-256 不一致")
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def open(self, storage_ref: str, *, expected_sha256: str | None = None) -> BinaryIO:
        bucket, key, ref_sha = parse_cos_ref(storage_ref)
        if bucket != self.bucket:
            raise CloudStorageError("COS 对象引用不属于当前私有桶")
        expected = _sha256(expected_sha256 or ref_sha)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].get_raw_stream()
            payload = body.read()
        except Exception as exc:
            raise CloudStorageError(f"COS 读取失败: {type(exc).__name__}") from exc
        if hashlib.sha256(payload).hexdigest() != expected:
            raise CloudStorageError("COS 读取内容 SHA-256 不一致")
        return io.BytesIO(payload)

    def verify(self, storage_ref: str, expected_sha256: str, size_bytes: int) -> bool:
        bucket, key, ref_sha = parse_cos_ref(storage_ref)
        expected = _sha256(expected_sha256)
        if bucket != self.bucket or ref_sha != expected:
            raise CloudStorageError("COS 对象引用与期望 SHA-256 不一致")
        self._verify_head(self._head_required(key), expected, size_bytes)
        return True

    def signed_download_url(self, storage_ref: str, *, expires_seconds: int = 600) -> str:
        bucket, key, ref_sha = parse_cos_ref(storage_ref)
        if bucket != self.bucket:
            raise CloudStorageError("COS 对象引用不属于当前私有桶")
        if type(expires_seconds) is not int or not 1 <= expires_seconds <= 600:
            raise CloudStorageError("COS 下载链接有效期必须为 1-600 秒")
        head = self._head_required(key)
        metadata = {str(name).casefold(): str(value) for name, value in dict(head).items()}
        actual_sha = (
            metadata.get("x-cos-meta-sha256")
            or metadata.get("sha256")
            or dict(head.get("Metadata") or {}).get("sha256")
        )
        if actual_sha != ref_sha:
            raise CloudStorageError("COS 对象完整性元数据与引用 SHA-256 不一致")
        try:
            return str(self.client.get_presigned_download_url(
                Bucket=self.bucket,
                Key=key,
                Expired=expires_seconds,
            ))
        except Exception as exc:
            raise CloudStorageError(f"COS 下载签名失败: {type(exc).__name__}") from exc

    def _object(self, key: str, digest: str, size_bytes: int) -> CloudObject:
        return CloudObject(
            storage_ref=cos_ref(self.bucket, key, digest),
            sha256=digest,
            size_bytes=size_bytes,
            bucket=self.bucket,
            key=key,
        )

    def _head_optional(self, key: str) -> dict | None:
        try:
            return dict(self.client.head_object(Bucket=self.bucket, Key=key))
        except Exception as exc:
            if _not_found(exc):
                return None
            raise CloudStorageError(f"COS 对象检查失败: {type(exc).__name__}") from exc

    def _head_required(self, key: str) -> dict:
        result = self._head_optional(key)
        if result is None:
            raise CloudStorageError("COS 对象上传后不存在")
        return result

    @staticmethod
    def _verify_head(head: dict, digest: str, size_bytes: int) -> None:
        metadata = {str(key).casefold(): str(value) for key, value in dict(head).items()}
        actual_sha = (
            metadata.get("x-cos-meta-sha256")
            or metadata.get("sha256")
            or dict(head.get("Metadata") or {}).get("sha256")
        )
        actual_size = head.get("Content-Length", head.get("ContentLength"))
        if actual_sha != digest or int(actual_size or -1) != int(size_bytes):
            raise CloudStorageError("COS 对象已存在但完整性元数据不一致")


def cos_ref(bucket: str, key: str, sha256: str) -> str:
    digest = _sha256(sha256)
    return f"cos://{_required(bucket, 'bucket')}/{quote(_safe_key(key), safe='/')}?sha256={digest}"


def parse_cos_ref(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != COS_REF_SCHEME or not parsed.netloc:
        raise CloudStorageError("COS 对象引用无效")
    key = _safe_key(unquote(parsed.path.lstrip("/")))
    values = parse_qs(parsed.query)
    digest = _sha256((values.get("sha256") or [""])[0])
    return parsed.netloc, key, digest


def _safe_key(value: str) -> str:
    text = str(value or "").strip().strip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CloudStorageError("COS 对象 Key 无效")
    return path.as_posix()


def _sha256(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CloudStorageError("SHA-256 无效")
    return digest


def _required(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CloudStorageError(f"{field} 未配置")
    return normalized


def _not_found(exc: Exception) -> bool:
    status = getattr(exc, "get_status_code", lambda: None)()
    code = getattr(exc, "get_error_code", lambda: None)()
    return status == 404 or str(code or "") in {"NoSuchKey", "NoSuchResource", "404"}
