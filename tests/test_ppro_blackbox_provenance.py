from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from emuflow.common import json_io
from emuflow_tools import document_io

from emuflow_tools.ppro_blackbox_provenance import (
    RUNNER_SOURCE_BUNDLE_SCHEMA,
    RUNNER_SOURCE_MEMBERS,
    runner_source_bundle,
)


class PProBlackboxProvenanceTest(unittest.TestCase):
    def test_revision_is_path_redacted_and_covers_runtime_sources(self):
        bundle = runner_source_bundle()
        self.assertEqual(bundle["schema"], RUNNER_SOURCE_BUNDLE_SCHEMA)
        self.assertEqual(
            [member["path"] for member in bundle["members"]],
            list(RUNNER_SOURCE_MEMBERS),
        )
        self.assertEqual(len(bundle["runner_revision"]), 64)
        text = json.dumps(bundle, sort_keys=True)
        self.assertNotIn(str(Path.cwd()), text)
        self.assertNotIn("/Users/", text)

    def test_any_runtime_source_change_changes_revision(self):
        source = Path(__file__).resolve().parents[1] / "tools" / "emuflow_tools"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            io_sources = {"document_io.py": Path(document_io.__file__),
                          "json_io.py": Path(json_io.__file__)}
            for relative in RUNNER_SOURCE_MEMBERS:
                shutil.copyfile(io_sources.get(relative, source / relative), root / relative)
            first = runner_source_bundle(root)
            for relative in RUNNER_SOURCE_MEMBERS:
                with self.subTest(member=relative):
                    member = root / relative
                    original = member.read_bytes()
                    member.write_bytes(original + b"\n# mutation\n")
                    second = runner_source_bundle(root)
                    self.assertNotEqual(first["runner_revision"], second["runner_revision"])
                    member.write_bytes(original)
                    self.assertEqual(first, runner_source_bundle(root))


if __name__ == "__main__":
    unittest.main()
