import copy
import json
import unittest
from pathlib import Path

from scripts.check_source_complete import _audit_architecture_source


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = (
    ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"
)


class SourceAuditTest(unittest.TestCase):
    def test_rapidwright_provider_is_a_valid_external_architecture_source(self):
        provider = json.loads(PROVIDER.read_text(encoding="utf-8"))
        errors = []
        _audit_architecture_source(PROVIDER.name, provider, errors)
        self.assertEqual(errors, [])

    def test_rapidwright_provider_provenance_failures_are_not_hidden(self):
        base = json.loads(PROVIDER.read_text(encoding="utf-8"))
        cases = [
            ("rapidwright_device_database", "md5", "0"),
            ("license", "generated_device_data_committable", True),
            ("resource_evidence", "url", "file:///private/device.pdf"),
        ]
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                provider = copy.deepcopy(base)
                provider[section][field] = value
                errors = []
                _audit_architecture_source(PROVIDER.name, provider, errors)
                self.assertTrue(errors)

    def test_open_pinned_architecture_source_remains_supported(self):
        errors = []
        _audit_architecture_source(
            "fixture.json",
            {
                "schema": "emuflow.pinned-architecture-source/v1",
                "upstream": "https://example.invalid/architecture.xml",
                "commit": "revision",
                "sha256": "digest",
                "license": "BSD-3-Clause",
            },
            errors,
        )
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
