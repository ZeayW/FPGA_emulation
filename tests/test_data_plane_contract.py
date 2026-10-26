"""Negative boundary tests for production dependency and retirement enforcement."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('data_plane_gate', Path(__file__).resolve().parents[1] / 'scripts/check_data_plane_contract.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class DataPlaneContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = dict(version=1, entrypoints={'emuflow': 'emuflow.cli:main'},
                             native_manifest='src/native/emuflow_core/sources.txt',
                             native_exceptions=[], existing_python_debt=[], retired_paths=[])
        self.write('pyproject.toml', '[project.scripts]\nemuflow = "emuflow.cli:main"\n')
        self.write('src/emuflow/cli.py', 'def main(): pass\n')
        self.write('src/native/emuflow_core/sources.txt', 'emuflow_core/store.cpp\n')
        self.write('src/native/emuflow_core/store.cpp', '// canonical owner\n')

    def write(self, path, data):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def check(self):
        self.write('config/data_plane_contract.json', json.dumps(self.registry))
        return GATE.check(self.root)

    def retire(self):
        self.registry['retired_paths'] = [dict(path='src/emuflow/old.py', replacement='src/native/emuflow_core/store.cpp', reason='Producer replaced', source_revision='a' * 40)]

    def test_control_loops_and_test_oracles_are_allowed(self):
        self.write('src/emuflow/control.py', 'def summary():\n return [x for x in range(3)]\n')
        self.write('tests/oracle.py', 'import tests.reference\n')
        self.assertEqual(self.check(), [])

    def test_production_oracle_imports_rejected(self):
        for statement in ('import tests.oracle', 'from tests import oracle', '__import__("tests.oracle")', 'importlib.import_module("tests.oracle")'):
            with self.subTest(statement=statement):
                self.write('src/emuflow/control.py', statement)
                self.assertTrue(any('production dependency' in e for e in self.check()))

    def test_relative_retired_dependencies_rejected(self):
        self.retire()
        for statement in ('from . import old', 'from .old import producer', 'from emuflow import old'):
            self.write('src/emuflow/control.py', statement)
            self.assertTrue(any('emuflow.old' in e for e in self.check()))

    def test_retired_path_and_missing_replacement_rejected(self):
        self.retire()
        self.assertEqual(self.check(), [])
        self.write('src/emuflow/old.py', '')
        (self.root / 'src/native/emuflow_core/store.cpp').unlink()
        errors = self.check()
        self.assertTrue(any('retired path restored' in e for e in errors))
        self.assertTrue(any('replacement missing' in e for e in errors))

    def test_entrypoint_drift_and_missing_target_rejected(self):
        self.write('pyproject.toml', '[project.scripts]\nemuflow = "emuflow.old:main"\n')
        self.write('src/emuflow/cli.py', 'def wrong(): pass\n')
        errors = self.check()
        self.assertTrue(any('entrypoint registry' in e for e in errors))
        self.assertTrue(any('missing function' in e for e in errors))

    def test_duplicate_missing_and_unowned_native_sources_rejected(self):
        self.write('src/native/emuflow_core/sources.txt', 'emuflow_core/store.cpp\nemuflow_core/store.cpp\nmissing.cpp\n')
        self.write('src/native/backend_duplicate.cpp', '// duplicated owner\n')
        errors = self.check()
        for message in ('duplicate shared', 'missing shared', 'unowned native'):
            self.assertTrue(any(message in e for e in errors))

    def test_new_exception_requires_exact_scope_and_provenance(self):
        self.registry['native_exceptions'] = [dict(scope=['src/native/*.cpp'], reason='legacy', owner='core', retirement='switch producer', source_revision='a'*40)]
        with self.assertRaises(ValueError):
            self.check()
        self.registry['native_exceptions'][0]['scope'] = ['src/native/tool.cpp']
        self.registry['native_exceptions'][0]['source_revision'] = 'HEAD'
        with self.assertRaises(ValueError):
            self.check()

    def test_native_exception_cannot_hide_duplicate_or_stale_ownership(self):
        self.registry['native_exceptions'] = [dict(scope=['src/native/emuflow_core/store.cpp', 'src/native/gone.cpp'], reason='existing tool', owner='core', retirement='switch producer', source_revision='a'*40)]
        errors = self.check()
        self.assertTrue(any('core-owned source still exempted' in e for e in errors))
        self.assertTrue(any('stale native exception' in e for e in errors))

    def test_malformed_registry_fails_closed(self):
        self.registry['version'] = 999
        with self.assertRaises(ValueError):
            self.check()


if __name__ == '__main__':
    unittest.main()
