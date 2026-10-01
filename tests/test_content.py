import tempfile
import unittest
from pathlib import Path

from teletext.content import ContentNotFound, ContentStore


class ContentTests(unittest.TestCase):
    def test_index_is_curated_but_unlisted_file_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pages").mkdir()
            (root / "index.md").write_text("[101](page:101)", encoding="utf-8")
            (root / "pages" / "101.md").write_text("visible", encoding="utf-8")
            (root / "pages" / "999.md").write_text("unlisted", encoding="utf-8")
            store = ContentStore(root)
            with store.snapshot(None) as index:
                self.assertNotIn(b"999", index.read())
            (root / "pages" / "100.md").write_text("not the index", encoding="utf-8")
            with store.snapshot(100) as index:
                self.assertEqual(index.read(), b"[101](page:101)")
            with store.snapshot(999) as page:
                self.assertEqual(page.read(), b"unlisted")
            with self.assertRaises(ContentNotFound):
                store.snapshot(500)
            with self.assertRaises(ValueError):
                store.snapshot(99)

    def test_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pages").mkdir()
            outside = root.parent / "outside-teletext.md"
            (root / "pages" / "101.md").symlink_to(outside)
            with self.assertRaises(ContentNotFound):
                ContentStore(root).snapshot(101)


if __name__ == "__main__":
    unittest.main()
