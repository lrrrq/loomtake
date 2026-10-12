"""Content-bound image presentation helpers; no project or approval state is stored."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from urllib.parse import quote, urlencode

from PIL import Image, ImageOps, __version__ as pillow_version, features


_THUMBNAIL_SIZE = 480
_CACHE_LOCKS = tuple(Lock() for _ in range(64))


def formal_image_preview_url(candidate: dict, *, project_id: str, workspace_id: str,
                             expected_project_version: int, version_id: str = "") -> str:
    """Present an already resolved image identity through the scoped preview API."""
    if candidate.get("image_key"):
        identity = quote(str(candidate["image_key"]), safe="")
    elif candidate.get("media_id"):
        identity = "default-candidates/" + quote(str(candidate["media_id"]), safe="")
    else:
        return ""
    return f"/api/formal/projects/{quote(project_id, safe='')}/images/{identity}/preview?" + urlencode({
        "workspace_id": workspace_id, "expected_project_version": expected_project_version,
        **({"version_id": version_id} if version_id else {}),
    })


def _thumbnail_spec() -> str:
    return f"thumbnail-v1:{_THUMBNAIL_SIZE}:poster-exif:webp-q82-m4-png:Pillow-{pillow_version}:WebP-{features.version('webp')}"


def _image_mime(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("图片预览格式无效")


def _encode_thumbnail(content: bytes) -> tuple[bytes, str]:
    with Image.open(BytesIO(content)) as original:
        # An animation's poster is presentation only; its full content SHA remains its identity.
        original.seek(0)
        image = ImageOps.exif_transpose(original).convert("RGBA")
        image.thumbnail((_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
        output = BytesIO()
        if features.check("webp"):
            try:
                image.save(output, format="WEBP", quality=82, method=4)
                return output.getvalue(), "image/webp"
            except (OSError, ValueError, KeyError):
                output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), "image/png"


def _read_cached(path: Path, content_sha256: str, spec: str) -> tuple[bytes, str] | None:
    try:
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("content_sha256") != content_sha256 or metadata.get("spec") != spec:
            return None
        content = path.read_bytes()
        if sha256(content).hexdigest() != metadata.get("preview_sha256"):
            return None
        media_type = _image_mime(content)
        if media_type not in {"image/webp", "image/png"} or metadata.get("media_type") != media_type:
            return None
        return content, media_type
    except (OSError, ValueError):
        return None


def _atomic_cache_write(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".preview-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def image_preview_bytes(
    content: bytes, *, content_sha256: str, variant: str = "original",
    cache_root: str | Path | None = None,
) -> tuple[bytes, str]:
    """Return original bytes or a verified 480px poster, with an optional disposable cache.

    Callers retain authentication, project/version checks and source resolution. Even a
    warm derivative never bypasses checking the supplied original bytes against their SHA.
    """
    if variant not in {"original", "thumbnail"}:
        raise ValueError("图片预览规格无效")
    if not re.fullmatch(r"[0-9a-f]{64}", str(content_sha256)) or sha256(content).hexdigest() != content_sha256:
        raise ValueError("图片预览内容 SHA-256 不一致")
    if variant == "original":
        return content, _image_mime(content)
    spec = _thumbnail_spec()
    key = sha256(f"{content_sha256}:{spec}".encode()).hexdigest()
    path = Path(cache_root) / key[:2] / f"{key}.preview" if cache_root is not None else None
    # Fixed stripes bound memory while coalescing simultaneous derivations of one source.
    with _CACHE_LOCKS[int(key[:8], 16) % len(_CACHE_LOCKS)]:
        if path is not None:
            cached = _read_cached(path, content_sha256, spec)
            if cached is not None:
                return cached
        preview, media_type = _encode_thumbnail(content)
        if path is not None:
            metadata = json.dumps({"content_sha256": content_sha256, "spec": spec,
                "preview_sha256": sha256(preview).hexdigest(), "media_type": media_type}, sort_keys=True).encode()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_cache_write(path, preview)
                _atomic_cache_write(path.with_suffix(".json"), metadata)
            except OSError:
                # A read-only or full cache volume must not prevent viewing verified pixels.
                pass
        return preview, media_type
