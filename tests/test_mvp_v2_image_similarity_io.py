import builtins
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from mvp_v2.formal_image_candidates import _image_similarity


class ImageSimilarityIOTests(unittest.TestCase):
    def test_source_is_read_in_one_pass_before_pixel_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'anonymous.png'
            Image.new('RGB', (96, 64), (50, 80, 120)).save(path)
            reads = []
            original_builtin, original_io = builtins.open, io.open

            class Reader:
                def __init__(self, handle): self.handle = handle
                def __getattr__(self, key): return getattr(self.handle, key)
                def __enter__(self): return self
                def __exit__(self, *args): self.handle.close()
                def read(self, *args):
                    reads.append(args)
                    return self.handle.read(*args)

            def tracked(original):
                def open_file(name, *args, **kwargs):
                    handle = original(name, *args, **kwargs)
                    return Reader(handle) if Path(name).resolve() == path.resolve() else handle
                return open_file

            with patch('builtins.open', side_effect=tracked(original_builtin)), \
                    patch('io.open', side_effect=tracked(original_io)):
                first = _image_similarity({'path': str(path)})
            self.assertGreater(len(reads), 0)
            self.assertLessEqual(len(reads), 2, 'pixel decoder repeatedly reads the source file')
            self.assertTrue(first['static_key'])
            self.assertEqual(first['color'], (50.0, 80.0, 120.0))

    def test_replaced_content_with_same_size_and_mtime_gets_new_pixel_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'anonymous.bmp'
            Image.new('RGB', (96, 64), (180, 40, 70)).save(path)
            first = _image_similarity({'path': str(path)})
            old = path.stat()
            Image.new('RGB', (96, 64), (30, 120, 200)).save(path)
            self.assertEqual(path.stat().st_size, old.st_size)
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
            second = _image_similarity({'path': str(path)})
            self.assertNotEqual(first['static_key'], second['static_key'])
            self.assertEqual(second['color'], (30.0, 120.0, 200.0))
