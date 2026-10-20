"""Continuous author demand prices must not inherit ratio-quantum plateaus."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_author_routing import author_router


class AuthorCostPolicyTest(unittest.TestCase):
    def _run(self, arcs, demands, policy=None, *, nodes=2, frame=16, quantum=4, minimum=4):
        with tempfile.TemporaryDirectory(prefix="emuflow-author-cost-") as temporary:
            root = Path(temporary)
            source, output = root / "input", root / "output"
            source.write_text(
                "EMUFLOW_TLR_INPUT_V7_BANKS_V1\n"
                f"PARAM {nodes} 0 20 8 2 4 1 0.1 {quantum} {frame} "
                f"1 1 1 0 {minimum} 0 0 1\n"
                + "\n".join(arcs + demands) + "\n"
            )
            command = [author_router(), str(source), str(output)]
            if policy is not None:
                command += ["--cost-policy", policy]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            text = output.read_text() if output.exists() else None
            return result, text

    @staticmethod
    def _trees(text):
        return {
            int(fields[1]): fields[2]
            for line in text.splitlines()
            if (fields := line.split()) and fields[0] == "ROUTE"
        }

    def test_quantum_plateau_regression_and_explicit_legacy_policy(self):
        # Both demands fit in the same ratio-4 bucket. Pricing only that bucket
        # leaves the two links tied and packs both demands onto arc 0. Eq. (2)
        # sees its existing 1/10 load and sends the second demand to arc 1.
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 160 10 1.5 1 0 0 0",
            "ARC 1 1 0 1 1 -1 -1 160 10 1.5 1 0 0 0",
        ]
        demands = ["DEMAND 0 0 1 1 0 0", "DEMAND 1 0 1 1 0 0"]
        result, default = self._run(arcs, demands)
        self.assertEqual(result.returncode, 0, result.stderr)
        result, explicit = self._run(arcs, demands, "author-pressure-v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(default, explicit)
        self.assertEqual(self._trees(default), {0: "0", 1: "1"})
        result, legacy = self._run(arcs, demands, "quantized-ratio-v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(legacy), {0: "0", 1: "0"})

    def test_continuous_cost_uses_preinsertion_load(self):
        # Empty links tie under the paper's pre-insertion cost even when their
        # capacities differ. Projecting this demand into the optimization cost
        # would choose the wide arc first; that is a different algorithm.
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 16 1 1.5 1 0 0 0",
            "ARC 1 1 0 1 1 -1 -1 1600 100 1.5 1 0 0 0",
        ]
        result, text = self._run(arcs, ["DEMAND 0 0 1 2 0 0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(text), {0: "0"})

    def test_quantized_capacity_is_hard_under_both_cost_policies(self):
        # Five signals need ratio 8, beyond the six available slots. A smoother
        # optimization price must not turn this infeasible input into a pass.
        for policy in ("author-pressure-v1", "quantized-ratio-v1"):
            with self.subTest(policy=policy):
                result, text = self._run(
                    ["ARC 0 0 0 1 0 -1 -1 6 1 1.5 1 0 0 0"],
                    ["DEMAND 0 0 1 5 0 0"], policy, frame=6,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIsNone(text)
                self.assertIn("no legal author-maze route", result.stderr)

    def test_sll_board_delay_is_identical_under_both_policies(self):
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 2 2 7 0 1 0 0",
            "ARC 1 1 0 2 1 -1 -1 2 2 2 0 1 0 0",
            "ARC 2 2 2 1 2 -1 -1 2 2 2 0 1 0 0",
        ]
        for policy in ("author-pressure-v1", "quantized-ratio-v1"):
            result, text = self._run(arcs, ["DEMAND 0 0 1 1 0 0"], policy, nodes=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._trees(text), {0: "1,2"})

    def test_policy_cli_rejects_unknown_and_documents_default(self):
        result, text = self._run([], [], "unknown-policy")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(text)
        self.assertIn("unsupported cost policy", result.stderr)
        result = subprocess.run([author_router(), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("default: author-pressure-v1", result.stdout)
        self.assertIn("quantized-ratio-v1", result.stdout)
        self.assertIn("author-sll-pressure-v1", result.stdout)

    def test_sll_scarcity_prices_new_claims_before_hard_saturation(self):
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 2 2 1 0 1 0 0",
            "ARC 1 1 0 1 1 -1 -1 2 2 1 0 1 0 0",
        ]
        demands = ["DEMAND 0 0 1 1 0 0", "DEMAND 1 0 1 1 0 0"]
        result, original = self._run(arcs, demands, "author-pressure-v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(original), {0: "0", 1: "0"})
        result, diagnostic = self._run(arcs, demands, "author-sll-pressure-v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(diagnostic), {0: "0", 1: "1"})
        result, default = self._run(arcs, demands)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(default, original)

    def test_sll_scarcity_keeps_last_wire_legal_and_zero_delay_zero(self):
        demands = ["DEMAND 0 0 1 1 0 0", "DEMAND 1 0 1 1 0 0"]
        for delay in (0, 1):
            with self.subTest(delay=delay):
                arcs = [f"ARC 0 0 0 1 0 -1 -1 2 2 {delay} 0 1 0 0"]
                result, text = self._run(arcs, demands, "author-sll-pressure-v1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._trees(text), {0: "0", 1: "0"})
                result, text = self._run(arcs, demands + ["DEMAND 2 0 1 1 0 0"], "author-sll-pressure-v1")
                self.assertEqual(result.returncode, 1)
                self.assertIsNone(text)

    def test_sll_shared_prefix_claims_no_additional_saturated_wire(self):
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 1 1 1 0 1 0 0",
            "ARC 1 1 1 2 1 -1 -1 1 1 1 0 1 0 0",
            "ARC 2 2 1 3 2 -1 -1 1 1 1 0 1 0 0",
        ]
        result, text = self._run(arcs, ["DEMAND 0 0 2,3 1 0 0"], "author-sll-pressure-v1", nodes=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(text), {0: "0,1,2"})

    def test_sll_scarcity_uses_physical_usage_across_round_layers(self):
        arcs = [
            "ARC 0 0 0 1 0 -1 -1 2 2 1 0 1 0 0",
            "ARC 1 1 0 1 1 -1 -1 2 2 1.5 0 1 0 0",
            "ARC 2 0 2 3 2 -1 -1 2 2 1 0 1 0 0",
            "ARC 3 1 2 3 3 -1 -1 2 2 1.5 0 1 0 0",
        ]
        demands = ["DEMAND 0 0 1 1 0 0", "DEMAND 1 2 3 1 0 0"]
        result, baseline = self._run(arcs, demands, "author-pressure-v1", nodes=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(baseline), {0: "0", 1: "2"})
        result, diagnostic = self._run(arcs, demands, "author-sll-pressure-v1", nodes=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._trees(diagnostic), {0: "0", 1: "3"})


if __name__ == "__main__":
    unittest.main()
