from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from mvp_v2.video_frames import (
    discover_project_videos,
    evenly_spaced_timecodes,
    frame_records_to_manifest,
)
from mvp_v2.image_retrieval import _source_binding_score, search_images_for_slot


class VideoFrameAssetTests(unittest.TestCase):
    def test_evenly_spaced_timecodes_cover_short_clip_without_using_exact_edges(self):
        values = evenly_spaced_timecodes(3.826, 8)

        self.assertEqual(len(values), 8)
        self.assertGreater(values[0], 0)
        self.assertLess(values[-1], 3.826)
        self.assertEqual(values, sorted(values))

    def test_discover_project_videos_includes_media_root_and_matching_finished_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            launch_root = Path(tmp) / "发布会类"
            media_root = launch_root / "美的智美万象发布会"
            finished = launch_root / "成片"
            other = launch_root / "林氏家居"
            for directory in (media_root, finished, other):
                directory.mkdir(parents=True)
            project_video = media_root / "美的发布会.mp4"
            related_clip = finished / "美的园区启动仪式.MP4"
            unrelated = finished / "林氏家居启动仪式.mp4"
            other_video = other / "林氏家居发布会.mp4"
            for path in (project_video, related_clip, unrelated, other_video):
                path.write_bytes(b"video")

            videos = discover_project_videos(
                media_root,
                launch_root,
                aliases=("美的智美万象发布会", "美的"),
            )

        self.assertEqual(videos, [project_video, related_clip])

    def test_frame_records_create_traceable_video_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "launch.mp4"
            frame = root / "launch__t000001500__f0001.png"
            video.write_bytes(b"video")
            Image.new("RGB", (1920, 1080), color="white").save(frame)

            rows = frame_records_to_manifest(
                video,
                [{"frame_path": str(frame), "timecode": "00:00:01.500", "frame_index": 1}],
                project_name="测试发布会",
            )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["是否来自视频抽帧"])
        self.assertEqual(rows[0]["来源文件"], str(video))
        self.assertEqual(rows[0]["来源 sheet"], "video_keyframe")
        self.assertIn("00:00:01.500", rows[0]["可回查锚点"])
        self.assertTrue(rows[0]["image_id"].startswith("img_"))

    def test_project_video_frame_receives_project_source_weight_for_auto_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "launch.mp4"
            frame = root / "startup.png"
            video.write_bytes(b"video")
            Image.new("RGB", (1920, 1080), color="white").save(frame)
            manifest = frame_records_to_manifest(
                video,
                [{"frame_path": str(frame), "timecode": "00:00:24.500", "frame_index": 1}],
                project_name="美的发布会",
            )
            analysis = [{
                "image_id": manifest[0]["image_id"],
                "caption": "美的发布会多位嘉宾共同按下启动装置，完成启动仪式",
                "tags": ["美的", "嘉宾", "共同启动", "启动装置", "启动仪式"],
                "brand_names": ["美的"],
                "source": "minimax",
            }]

            result = search_images_for_slot(
                {"slot_index": 16, "画面描述": "高管团完成启动仪式，形成发布高潮"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertGreaterEqual(result["candidates"][0]["score"], 80)

    def test_project_video_frame_has_same_source_weight_as_project_image_pool(self):
        score = _source_binding_score(
            {"是否来自项目图片池": False, "是否来自视频抽帧": True},
            {"slot_index": 16},
        )

        self.assertEqual(score, 20)

    def test_brand_slogan_video_frame_can_fill_brand_transition_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "launch.mp4"
            frame = root / "brand-slogan.png"
            video.write_bytes(b"video")
            Image.new("RGB", (1920, 1080), color="white").save(frame)
            manifest = frame_records_to_manifest(
                video,
                [{"frame_path": str(frame), "timecode": "00:03:19.840", "frame_index": 12}],
                project_name="林氏家居",
            )
            analysis = [{
                "image_id": manifest[0]["image_id"],
                "caption": "黑色背景上以白色艺术字体展示林氏家居品牌标语",
                "tags": ["林氏家居", "品牌标语", "黑色背景", "艺术字体"],
                "brand_names": ["林氏家居"],
                "source": "minimax",
            }]

            result = search_images_for_slot(
                {"slot_index": 3, "画面描述": "品牌logo快闪后切入视觉转场"},
                {"项目名称": "林氏家居发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")


if __name__ == "__main__":
    unittest.main()
