import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.native_build import emuflow_native, native_pipeline_api_test


class NativeStageRuntimeTest(unittest.TestCase):
    def test_runtime_probe_checks_sqlite_without_creating_data_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "probe.json"
            result = subprocess.run([str(emuflow_native()), "--report", str(report), "check-runtime"],
                                    capture_output=True, text=True, cwd=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(report.read_text())
            self.assertEqual(status["stage"], "native.runtime-check")
            self.assertEqual(status["status"], "pass")
            self.assertEqual(status["inputs"], [])
            self.assertEqual(status["outputs"], [])
            self.assertEqual(sorted(p.name for p in Path(temporary).iterdir()), ["probe.json"])

    def test_cpp_pipeline_retains_producer_artifact_generations(self):
        result = subprocess.run([str(native_pipeline_api_test())], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_registered_operations_reject_missing_input_without_replacing_results(self):
        binary = emuflow_native()
        schema = json.loads((Path(__file__).parents[1] / "schemas/native-stage-result-v1.schema.json").read_text())
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = str(root / 'missing"input\n.bin')
            output, report = root / "previous-result", root / "status.json"
            cases = [
                ["phase3", "analyze", missing],
                ["phase3", "reduce-loads", missing, missing, missing],
                ["phase3", "analyze", "--indexed-input", missing, missing],
                ["phase3", "finalize-semantics", missing, missing],
                ["phase3", "resolve-captures", missing, missing, missing, missing],
                ["phase3", "build-captures", missing, missing, missing, missing],
                ["phase3", "validate-captures", missing, missing, missing, missing, missing],
                ["phase3", "hash-semantics", missing, missing, missing],
                ["phase3", "write-exact-sections", missing, missing, missing],
                ["phase3", "refine-patron", "--typed-input", missing],
                ["phase4", "route", "--typed-output", missing],
                ["phase5", "schedule", missing],
                ["phase5", "schedule-from-store", missing, missing],
                ["phase5", "build-schedule", missing, missing, missing],
                ["phase5", "publish-schedule", missing, missing, missing, missing],
                ["phase6", "assign-banks", "--typed-input", missing],
                ["phase6", "group-signals", "--typed-input", missing],
                ["phase6", "refine-groups", "--typed-input", missing],
                ["phase6", "rudy", missing],
            ]
            for args in cases:
                with self.subTest(operation=args[:2]):
                    output.write_bytes(b"previous-generation")
                    command = [str(binary), "--report", str(report), *args, str(output)]
                    if args[:2] in (["phase6", "group-signals"], ["phase6", "refine-groups"]):
                        command.append("0" * 64)
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    status = json.loads(report.read_text())
                    stderr = json.loads(result.stderr.removeprefix("emuflow-native: "))
                    self.assertEqual(status, stderr)
                    self.assertEqual(status["status"], "fail")
                    self.assertEqual(status["error"]["code"], "missing-artifact")
                    self.assertIn(missing, status["error"]["message"])
                    self.assertEqual(status["outputs"], [])
                    self.assertEqual(output.read_bytes(), b"previous-generation")
                    if jsonschema is not None:
                        jsonschema.validate(status, schema)
            self.assertFalse(list(root.glob("*.tmp.*")))

    def test_invalid_report_alias_and_bad_envelope_preserve_inputs(self):
        binary = emuflow_native()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, report = root / "input.bin", root / "output.bin", root / "result.json"
            source.write_bytes(b"invalid-envelope")
            output.write_bytes(b"previous-output")
            base = ["phase5", "schedule", str(source), str(output)]
            alias = subprocess.run([str(binary), "--report", str(source), *base], capture_output=True, text=True)
            self.assertEqual(alias.returncode, 2)
            self.assertEqual(source.read_bytes(), b"invalid-envelope")
            failed = subprocess.run([str(binary), "--report", str(report), *base], capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(json.loads(report.read_text())["error"]["code"], "invalid-artifact")
            self.assertEqual(output.read_bytes(), b"previous-output")


if __name__ == "__main__":
    unittest.main()
