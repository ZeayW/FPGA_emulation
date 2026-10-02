import json
import tempfile
import unittest
from pathlib import Path

from emuflow.benchmark import BenchmarkRun
from emuflow.errors import EmuFlowError, ValidationError


ROOT = Path(__file__).resolve().parents[1]
SERV_SPEC = ROOT / "benchmarks" / "runs" / "serv_l1.json"
PICORV32_SPEC = ROOT / "benchmarks" / "runs" / "picorv32_l2.json"
SECWORKS_AES_SPEC = ROOT / "benchmarks" / "runs" / "secworks_aes_l3.json"
KOIOS_DLA_MEDIUM_SPEC = (
    ROOT / "benchmarks" / "runs" / "koios_dla_medium_l5.json"
)
KOIOS_DLA_SMALL_SPEC = (
    ROOT / "benchmarks" / "runs" / "koios_dla_small_l5.json"
)


class BenchmarkRunTest(unittest.TestCase):
    def test_serv_l1_spec_and_sources(self) -> None:
        spec = BenchmarkRun.load(SERV_SPEC)
        self.assertEqual(spec.value["top"], "serv_synth_wrapper")
        source_root = ROOT / "third_party" / "rtl" / "serv"
        if source_root.is_dir():
            sources = spec.resolve_sources(source_root)
            self.assertGreaterEqual(len(sources), 18)
            self.assertTrue(all(path.suffix == ".v" for path in sources))

    def test_picorv32_l2_spec_and_source(self) -> None:
        spec = BenchmarkRun.load(PICORV32_SPEC)
        self.assertEqual(spec.value["top"], "picorv32")
        self.assertEqual(spec.value["synthesis"]["policy"], "logic-only")
        source_root = ROOT / "third_party" / "rtl" / "picorv32"
        if source_root.is_dir():
            sources = spec.resolve_sources(source_root)
            self.assertEqual(
                [path.name for path in sources],
                ["picorv32.v"],
            )

    def test_secworks_aes_l3_spec_and_sources(self) -> None:
        spec = BenchmarkRun.load(SECWORKS_AES_SPEC)
        self.assertEqual(spec.value["design_id"], "secworks_aes")
        self.assertEqual(spec.value["top"], "aes")
        self.assertEqual(spec.value["synthesis"]["policy"], "logic-only")
        source_root = ROOT / "third_party" / "rtl" / "secworks_aes"
        if source_root.is_dir():
            sources = spec.resolve_sources(source_root)
            self.assertEqual(
                [path.name for path in sources],
                [
                    "aes.v",
                    "aes_core.v",
                    "aes_decipher_block.v",
                    "aes_encipher_block.v",
                    "aes_inv_sbox.v",
                    "aes_key_mem.v",
                    "aes_sbox.v",
                ],
            )

    def test_koios_dla_medium_spec_and_source(self) -> None:
        spec = BenchmarkRun.load(KOIOS_DLA_MEDIUM_SPEC)
        self.assertEqual(spec.value["top"], "DLA")
        self.assertEqual(spec.value["synthesis"]["policy"], "logic-only")
        self.assertEqual(spec.value["clock_periods_ns"], {"clk": 10.0})
        self.assertEqual(
            spec.value["physical_mapping_profile"], "vtr-hard-blocks"
        )
        source_root = (
            ROOT
            / "third_party"
            / "rtl"
            / "koios"
            / "vtr_flow"
            / "benchmarks"
            / "verilog"
            / "koios"
        )
        if source_root.is_dir():
            sources = spec.resolve_sources(source_root)
            self.assertEqual(
                [path.name for path in sources],
                ["dla_like.medium.v"],
            )

    def test_koios_dla_small_spec_and_source(self) -> None:
        spec = BenchmarkRun.load(KOIOS_DLA_SMALL_SPEC)
        self.assertEqual(spec.value["top"], "DLA")
        self.assertEqual(spec.value["synthesis"]["policy"], "logic-only")
        source_root = (
            ROOT
            / "third_party"
            / "rtl"
            / "koios"
            / "vtr_flow"
            / "benchmarks"
            / "verilog"
            / "koios"
        )
        if source_root.is_dir():
            sources = spec.resolve_sources(source_root)
            self.assertEqual(
                [path.name for path in sources],
                ["dla_like.small.v"],
            )

    def test_missing_source_pattern_is_rejected(self) -> None:
        spec = BenchmarkRun.load(SERV_SPEC)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(EmuFlowError, "matched no files"):
                spec.resolve_sources(Path(temporary))

    def test_unknown_policy_is_rejected(self) -> None:
        value = json.loads(SERV_SPEC.read_text(encoding="utf-8"))
        value["synthesis"]["policy"] = "unknown"
        with self.assertRaisesRegex(ValidationError, "unsupported value"):
            BenchmarkRun(value)

    def test_clock_period_contract_must_cover_declared_clocks(self) -> None:
        value = json.loads(KOIOS_DLA_MEDIUM_SPEC.read_text(encoding="utf-8"))
        value["clock_periods_ns"] = {"other": 10.0}
        with self.assertRaisesRegex(ValidationError, "clock_periods_ns"):
            BenchmarkRun(value)

    def test_unknown_physical_mapping_profile_is_rejected(self) -> None:
        value = json.loads(KOIOS_DLA_MEDIUM_SPEC.read_text(encoding="utf-8"))
        value["physical_mapping_profile"] = "implicit"
        with self.assertRaisesRegex(ValidationError, "physical_mapping_profile"):
            BenchmarkRun(value)

    def test_compilation_context_is_relative_validated_and_resolved(self) -> None:
        value = json.loads(SECWORKS_AES_SPEC.read_text(encoding="utf-8"))
        value["synthesis"]["include_dirs"] = ["include"]
        value["synthesis"]["defines"] = ["SYNTHESIS", "WIDTH=32"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "include").mkdir()
            spec = BenchmarkRun(value)
            self.assertEqual(
                spec.resolve_include_dirs(root), [(root / "include").resolve()]
            )
            self.assertEqual(
                spec.compilation_context(root),
                {
                    "include_dirs": ["include"],
                    "defines": ["SYNTHESIS", "WIDTH=32"],
                },
            )

    def test_compilation_context_rejects_escape_and_unsafe_define(self) -> None:
        value = json.loads(SECWORKS_AES_SPEC.read_text(encoding="utf-8"))
        value["synthesis"]["include_dirs"] = ["../outside"]
        with self.assertRaisesRegex(ValidationError, "contained relative path"):
            BenchmarkRun(value)
        value["synthesis"]["include_dirs"] = []
        value["synthesis"]["defines"] = ["SAFE; delete"]
        with self.assertRaisesRegex(ValidationError, "NAME or NAME=VALUE"):
            BenchmarkRun(value)


if __name__ == "__main__":
    unittest.main()
