from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image

from mvp_v2.alpha_image_delivery import (
    AlphaImageDeliveryError,
    create_alpha_image_delivery,
    make_alpha_isolated_root,
)


class AlphaImageDeliveryTests(unittest.TestCase):
    def test_confirmed_single_image_per_shot_writes_reopenable_excel(self):
        root = make_alpha_isolated_root()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        first = _image(root / "first.png", "navy")
        second = _image(root / "second.png", "teal")

        delivery = create_alpha_image_delivery(
            root,
            script_rows=[
                {"镜号": "1", "结构": "开场", "画面描述": "匿名主体一"},
                {"镜号": "2", "结构": "收束", "画面描述": "匿名主体二"},
            ],
            image_decisions=[
                _decision("1", "image-alpha-1", first),
                _decision("2", "image-alpha-2", second),
            ],
            run_id="alpha-run-001",
            version_id="alpha-version-001",
        )

        self.assertTrue(delivery.workbook_path.is_file())
        self.assertFalse((root / "CURRENT").exists())
        self.assertEqual(delivery.run_id, "alpha-run-001")
        self.assertEqual(delivery.version_id, "alpha-version-001")
        self.assertEqual([item["shot_no"] for item in delivery.bindings], ["1", "2"])
        self.assertEqual(
            delivery.bindings[0]["source_document_id"],
            "anonymous-source-document-001",
        )
        self.assertEqual(delivery.bindings[0]["source_sheet"], "脚本")
        self.assertEqual(delivery.bindings[0]["source_row"], "2")
        reopened = load_workbook(delivery.workbook_path, read_only=False, data_only=True)
        try:
            self.assertEqual(len(reopened["脚本"]._images), 2)
            self.assertEqual(reopened["脚本"]["B2"].value, "1")
            self.assertEqual(reopened["脚本"]["B3"].value, "2")
            detail_headers = {
                str(cell.value): cell.column
                for cell in reopened["抽调明细"][1]
            }
            self.assertEqual(
                reopened["抽调明细"].cell(
                    2, detail_headers["采用来源文件"]
                ).value,
                "anonymous-source-document-001",
            )
            self.assertEqual(
                reopened["抽调明细"].cell(
                    2, detail_headers["采用来源 sheet"]
                ).value,
                "脚本",
            )
            self.assertEqual(
                reopened["抽调明细"].cell(
                    2, detail_headers["采用来源行"]
                ).value,
                "2",
            )
        finally:
            reopened.close()

    def test_problem_shot_replacement_requires_new_confirmed_decision(self):
        root = make_alpha_isolated_root()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        original = _image(root / "original.png", "navy")
        replacement = _image(root / "replacement.png", "orange")
        rows = [{"镜号": "1", "结构": "开场", "画面描述": "匿名主体"}]

        first = create_alpha_image_delivery(root, script_rows=rows, image_decisions=[
            _decision("1", "image-original", original),
        ], run_id="alpha-run-001", version_id="alpha-version-001")
        second = create_alpha_image_delivery(root, script_rows=rows, image_decisions=[
            _decision("1", "image-replacement", replacement),
        ], run_id="alpha-run-001", version_id="alpha-version-002")

        self.assertNotEqual(first.bindings[0]["image_id"], second.bindings[0]["image_id"])
        self.assertEqual(second.bindings[0]["image_id"], "image-replacement")
        self.assertNotEqual(first.workbook_path, second.workbook_path)

    def test_rejects_missing_confirmation_or_non_isolated_image(self):
        root = make_alpha_isolated_root()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        image = _image(root / "image.png", "green")
        with self.assertRaisesRegex(AlphaImageDeliveryError, "人工图片确认"):
            create_alpha_image_delivery(
                root, script_rows=[{"镜号": "1"}], image_decisions=[],
                run_id="alpha-run-001", version_id="alpha-version-001",
            )
        with self.assertRaisesRegex(AlphaImageDeliveryError, "同一隔离根"):
            create_alpha_image_delivery(root, script_rows=[{"镜号": "1"}], image_decisions=[{
                **_decision("1", "image-outside", image),
                "image_path": str(Path(tempfile.gettempdir()) / "outside.png"),
            }], run_id="alpha-run-001", version_id="alpha-version-001")
        with self.assertRaisesRegex(AlphaImageDeliveryError, "不允许 text_only_approved"):
            create_alpha_image_delivery(root, script_rows=[{"镜号": "1"}], image_decisions=[{
                **_decision("1", "image-text-only", image),
                "resolution_status": "text_only_approved",
            }], run_id="alpha-run-001", version_id="alpha-version-001")

    def test_rejects_duplicate_script_or_decision_shot_numbers(self):
        root = make_alpha_isolated_root()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        image = _image(root / "image.png", "green")
        with self.assertRaisesRegex(AlphaImageDeliveryError, "脚本镜号必须唯一"):
            create_alpha_image_delivery(
                root,
                script_rows=[{"镜号": "1"}, {"镜号": "1"}],
                image_decisions=[_decision("1", "image-alpha-1", image)],
                run_id="alpha-run-001",
                version_id="alpha-version-001",
            )
        with self.assertRaisesRegex(AlphaImageDeliveryError, "每镜必须且只能有一条"):
            create_alpha_image_delivery(
                root,
                script_rows=[{"镜号": "1"}],
                image_decisions=[
                    _decision("1", "image-alpha-1", image),
                    _decision("1", "image-alpha-2", image),
                ],
                run_id="alpha-run-001",
                version_id="alpha-version-001",
            )

    def test_rejects_missing_anonymous_logical_source(self):
        root = make_alpha_isolated_root()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        image = _image(root / "image.png", "green")
        for missing_field in ("source_document_id", "source_sheet", "source_row"):
            decision = _decision("1", "image-alpha-1", image)
            decision.pop(missing_field)
            with self.subTest(missing_field=missing_field):
                with self.assertRaisesRegex(AlphaImageDeliveryError, "匿名来源三元组"):
                    create_alpha_image_delivery(
                        root,
                        script_rows=[{"镜号": "1"}],
                        image_decisions=[decision],
                        run_id="alpha-run-001",
                        version_id="alpha-version-001",
                    )


def _image(path: Path, color: str) -> Path:
    Image.new("RGB", (640, 360), color).save(path)
    return path


def _decision(shot_no: str, image_id: str, image_path: Path) -> dict:
    return {
        "shot_no": shot_no,
        "image_id": image_id,
        "image_path": str(image_path),
        "confirmed_by": "alpha-project-manager",
        "confirmed_at": "2026-08-12T16:30:00Z",
        "source_document_id": "anonymous-source-document-001",
        "source_sheet": "脚本",
        "source_row": "2",
    }
