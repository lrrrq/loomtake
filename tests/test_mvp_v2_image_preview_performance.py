from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import image_preview
from mvp_v2.formal_generation_service import FormalGenerationService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_project_image_authority as fixtures


def _png(color: str, size=(1600, 900)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class ProjectPreviewReadTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ProjectImageAuthorityTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        base = self.base
        imported = base.adapter.import_uploads(
            base.token, workspace_id=base.workspace_id, project_id=base.first["project_id"],
            expected_project_version=0, uploads=[*fixtures._representative_uploads(),
                fixtures._upload("second.png", _png("red")), fixtures._upload("third.png", _png("green"))],
        )
        confirmed = base.adapter.confirm(
            base.token, workspace_id=base.workspace_id, project_id=base.first["project_id"],
            brief_version_id=imported["brief_key"], confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=1,
        )
        self.scope = dict(workspace_id=base.workspace_id, project_id=base.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=base._snapshot_id(),
            expected_project_lock_version=2)
        self.image_ids = base.pool.synchronize_confirmed_brief(base.token, **self.scope)
        self.assertEqual(len(self.image_ids), 3)
        self.service = object.__new__(FormalGenerationService)
        self.service.services = SimpleNamespace(images=base.pool)
        paired = SimpleNamespace(brief=SimpleNamespace(brief_version_id=confirmed["brief_key"],
            brief_snapshot_id=self.scope["brief_snapshot_id"]))
        current_pair = patch.object(self.service, "_resolve_current_paired", return_value=paired)
        current_pair.start()
        self.addCleanup(current_pair.stop)
        self.request = dict(workspace_id=base.workspace_id, project_id=base.first["project_id"],
            expected_project_version=2)

    def test_three_previews_read_each_target_once_without_resolving_the_whole_pool(self):
        with patch.object(self.base.pool.artifacts, "read_source_image", wraps=self.base.pool.artifacts.read_source_image) as pixels, \
                patch.object(self.base.pool.artifacts, "read_analysis", wraps=self.base.pool.artifacts.read_analysis) as analyses, \
                patch.object(self.service, "_resolve_images", wraps=self.service._resolve_images) as pool:
            for image_id in self.image_ids:
                content, mime = self.service.read_image_preview(self.base.token, **self.request, image_id=image_id)
                self.assertEqual(mime, "image/png")
                self.assertTrue(content.startswith(b"\x89PNG"))
            self.assertEqual(pixels.call_count, 3, "three previews must not reread all three images per request")
            pool.assert_not_called()
            analyses.assert_not_called()

    def test_target_drift_still_fails_but_an_unrelated_broken_blob_does_not_block_preview(self):
        with self.base.store.connection() as connection:
            rows = connection.execute("SELECT project_image_id,image_sha256 FROM project_images ORDER BY project_image_id").fetchall()
        broken = rows[0]
        path = self.base.pool.artifacts.root / "formal-images" / broken["image_sha256"][:2] / broken["image_sha256"]
        path.write_bytes(b"changed original")
        content, _ = self.service.read_image_preview(self.base.token, **self.request, image_id=rows[1]["project_image_id"])
        self.assertTrue(content)
        with self.assertRaisesRegex(WorkspaceServiceError, "DRIFT|FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED"):
            self.service.read_image_preview(self.base.token, **self.request, image_id=broken["project_image_id"])

    def test_warm_thumbnail_rechecks_target_sha_and_current_project_version(self):
        image_id = self.image_ids[1]
        content, mime = self.service.read_image_preview(self.base.token, **self.request, image_id=image_id, variant="thumbnail")
        with Image.open(BytesIO(content)) as image:
            self.assertLessEqual(max(image.size), 480)
        self.assertIn(mime, {"image/png", "image/webp"})
        with patch.object(self.base.pool.artifacts, "read_source_image", wraps=self.base.pool.artifacts.read_source_image) as pixels:
            with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                self.service.read_image_preview(self.base.token, **{**self.request, "expected_project_version": 3},
                    image_id=image_id, variant="thumbnail")
            pixels.assert_not_called()
            with self.assertRaises(WorkspaceServiceError):
                self.service.read_image_preview("invalid-auth", **self.request, image_id=image_id, variant="thumbnail")
            pixels.assert_not_called()
        with self.base.store.connection() as connection:
            digest = connection.execute("SELECT image_sha256 FROM project_images WHERE project_image_id=?", (image_id,)).fetchone()[0]
        source = self.base.pool.artifacts.root / "formal-images" / digest[:2] / digest
        source.write_bytes(b"changed after thumbnail was cached")
        with self.assertRaisesRegex(WorkspaceServiceError, "DRIFT|FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED"):
            self.service.read_image_preview(self.base.token, **self.request, image_id=image_id, variant="thumbnail")

    def test_cross_project_image_is_still_denied_before_reading_pixels(self):
        base = self.base
        other = base.second["project_id"]
        imported = base.adapter.import_uploads(base.token, workspace_id=base.workspace_id,
            project_id=other, expected_project_version=0, uploads=fixtures._representative_uploads())
        confirmed = base.adapter.confirm(base.token, workspace_id=base.workspace_id, project_id=other,
            brief_version_id=imported["brief_key"], confirmed_at="2026-08-14T00:00:00Z", expected_project_version=1)
        with base.store.connection() as connection:
            snapshot = connection.execute("SELECT current_brief_snapshot_id FROM projects WHERE project_id=?", (other,)).fetchone()[0]
        ids = base.pool.synchronize_confirmed_brief(base.token, workspace_id=base.workspace_id, project_id=other,
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=2)
        with patch.object(base.pool.artifacts, "read_source_image", wraps=base.pool.artifacts.read_source_image) as pixels:
            with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
                self.service.read_image_preview(base.token, **self.request, image_id=ids[0], variant="thumbnail")
            pixels.assert_not_called()


class ThumbnailCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mplus-image-thumbnail-")
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "derived"
        self.original = _png("purple")
        self.digest = sha256(self.original).hexdigest()

    def preview(self, content=None, **options):
        content = self.original if content is None else content
        return image_preview.image_preview_bytes(content, **{
            "content_sha256": sha256(content).hexdigest(), "variant": "thumbnail", "cache_root": self.cache, **options})

    def test_thumbnail_dimensions_original_preservation_and_warm_cache(self):
        preview = self.preview()
        with Image.open(BytesIO(preview[0])) as image:
            self.assertEqual(image.size, (480, 270))
        with patch.object(image_preview, "_encode_thumbnail", side_effect=AssertionError("warm cache must not decode source")):
            self.assertEqual(self.preview(), preview)
        self.assertEqual(self.preview(variant="original"), (self.original, "image/png"))
        with Image.open(BytesIO(self.original)) as image:
            self.assertEqual(image.size, (1600, 900))
        with Image.open(BytesIO(self.preview(_png("red", (24, 16)))[0])) as image:
            self.assertEqual(image.size, (24, 16))

    def test_animation_poster_keeps_full_content_identity_and_original_frames(self):
        animations = []
        for color in ("blue", "green"):
            output = BytesIO()
            Image.new("RGB", (640, 360), "red").save(output, format="GIF", save_all=True,
                append_images=[Image.new("RGB", (640, 360), color)], duration=50, loop=0)
            animations.append(output.getvalue())
        for original in animations:
            poster, mime = self.preview(original)
            with Image.open(BytesIO(poster)) as image:
                self.assertEqual(image.size, (480, 270))
                self.assertEqual(getattr(image, "n_frames", 1), 1)
            preserved, original_mime = self.preview(original, variant="original")
            self.assertEqual((preserved, original_mime), (original, "image/gif"))
        self.assertNotEqual(sha256(animations[0]).hexdigest(), sha256(animations[1]).hexdigest())
        self.assertEqual(len(list(self.cache.rglob("*.preview"))), 2)

    def test_corrupt_derivative_rebuilds_and_a_changed_source_never_hits_old_cache(self):
        expected = self.preview()
        cached = next(self.cache.rglob("*.preview"))
        cached.write_bytes(b"broken cached thumbnail")
        with patch.object(image_preview, "_encode_thumbnail", wraps=image_preview._encode_thumbnail) as encode:
            self.assertEqual(self.preview(), expected)
            self.assertEqual(encode.call_count, 1)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.preview(_png("blue"), content_sha256=self.digest)

    def test_concurrent_same_identity_encodes_once_and_uses_atomic_cache_writes(self):
        with patch.object(image_preview, "_encode_thumbnail", wraps=image_preview._encode_thumbnail) as encode:
            with ThreadPoolExecutor(max_workers=8) as executor:
                previews = list(executor.map(lambda _: self.preview(), range(8)))
            self.assertEqual(encode.call_count, 1)
        self.assertTrue(all(value == previews[0] for value in previews))
        self.assertEqual(list(self.cache.rglob(".preview-*")), [])

    def test_read_only_cache_and_unavailable_webp_fall_back_without_blocking_preview(self):
        self.cache.write_bytes(b"a regular file cannot be a cache directory")
        with patch.object(image_preview.features, "check", return_value=False):
            content, mime = self.preview()
        self.assertEqual(mime, "image/png")
        with Image.open(BytesIO(content)) as image:
            self.assertEqual(image.size, (480, 270))
        self.assertEqual(self.cache.read_bytes(), b"a regular file cannot be a cache directory")

    def test_encoder_revision_invalidates_derivative_without_changing_source_identity(self):
        self.preview()
        with patch.object(image_preview, "_thumbnail_spec", return_value="future-encoder-revision"):
            self.preview()
        self.assertEqual(len(list(self.cache.rglob("*.preview"))), 2)


if __name__ == "__main__":
    unittest.main()
