from dataclasses import asdict
import unittest

from mvp_v2.project_image_authority import _source_location, _employee_source
from mvp_v2.workspace_store import WorkspaceServiceError


class ImageSourcePositionTests(unittest.TestCase):
    def test_office_extraction_positions_survive_authority_readback(self):
        for filename, position in (
            ("anonymous.xlsx", {"sheet": "Brief", "row": 14, "column": 3, "image_index": 2, "part": "xl/drawings/drawing1.xml"}),
            ("anonymous.docx", {"paragraph": 4, "image_index": 1, "part": "word/document.xml"}),
            ("anonymous.pptx", {"slide": 3, "image_index": 2}),
        ):
            with self.subTest(filename=filename):
                source = _source_location(filename, position, "anonymous-document")
                stored = asdict(source)
                for key, value in position.items():
                    self.assertEqual(stored["image" if key == "image_index" else key], value)
                self.assertNotIn("part", _employee_source(source))

    def test_invalid_extended_positions_are_rejected(self):
        for position in (
            {"image_index": 1, "column": 0}, {"image_index": 1, "paragraph": True},
            {"image_index": 1, "part": "../outside.xml"},
            {"image_index": 1, "part": "/private/file.xml"},
            {"image_index": 1, "arbitrary": "value"},
        ):
            with self.subTest(position=position):
                with self.assertRaises(WorkspaceServiceError):
                    _source_location("anonymous.xlsx", position, "anonymous-document")
