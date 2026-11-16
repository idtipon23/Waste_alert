import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_site import build_site


class BuildSiteTests(unittest.TestCase):
    def test_build_writes_key_only_to_output_and_omits_local_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "site"
            with patch.dict(os.environ, {"MAPS_KEY": "test-key"}, clear=False):
                build_site(output)

            generated = (output / "config.js").read_text(encoding="utf-8")
            self.assertIn('"test-key"', generated)
            self.assertFalse((output / "config.js.example").exists())

    def test_build_requires_maps_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "site"
            with patch.dict(os.environ, {"MAPS_KEY": ""}, clear=False):
                with self.assertRaisesRegex(ValueError, "MAPS_KEY is required"):
                    build_site(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
