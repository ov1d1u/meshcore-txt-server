import tempfile
import unittest
from pathlib import Path

from teletext.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "config.ini"

    def tearDown(self):
        self.temporary.cleanup()

    def test_adds_suffix_once(self):
        self.path.write_text("[server]\nnode_name = Teletext\n", encoding="utf-8")
        self.assertEqual(load_config(self.path).node_name, "Teletext-txt")
        self.path.write_text("[server]\nnode_name = Teletext-txt\n", encoding="utf-8")
        self.assertEqual(load_config(self.path).node_name, "Teletext-txt")

    def test_rejects_missing_empty_or_oversized_name(self):
        with self.assertRaises(ConfigError):
            load_config(self.path)
        for value in ("", "  ", "é" * 10):
            with self.subTest(value=value):
                self.path.write_text(f"[server]\nnode_name = {value}\n", encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.path)


if __name__ == "__main__":
    unittest.main()
