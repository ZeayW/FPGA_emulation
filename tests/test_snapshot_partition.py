import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.partition import build_partition_assignment
from emuflow.snapshot_partition import partition_snapshot_pair, ulx3s_partition_resources
from test_snapshot_netlist import fixture


class SnapshotPartitionTests(unittest.TestCase):
    def test_capacity_view_has_no_invented_link_timing(self):
        view = ulx3s_partition_resources()
        self.assertFalse(hasattr(view, "links"))
        self.assertFalse(hasattr(view, "to_dict"))
        self.assertEqual([f.id for f in view.fpgas], ["board0", "board1"])
        for fpga in view.fpgas:
            self.assertEqual(fpga.utilization_limit, .75)
            self.assertEqual(fpga.effective_capacity, {"lut": 62730, "ff": 62730})

    def test_existing_triton_provider_and_generalized_clusters(self):
        ir=fixture(); ir.value["clocks"]=[{"port":"clk"}]
        def provider(ir, platform, clusters, constraints, directory, seed, **options):
            self.assertEqual(constraints["fixed"], [])
            self.assertEqual(clusters["policy"]["cut_mode"], "static-exact-combinational")
            self.assertTrue(options["defer_semantic_contract"])
            self.assertFalse(options["persist_input_manifest"])
            assignment={c["id"]: ("board0" if "a" in c["instances"] else "board1")
                        for c in clusters["clusters"]}
            return build_partition_assignment(ir,platform,clusters,constraints,assignment,
                provider="tritonpart",seed=seed,_include_semantic_contract=False)
        with tempfile.TemporaryDirectory() as d, patch("emuflow.snapshot_partition.run_tritonpart",side_effect=provider) as call:
            result, check=partition_snapshot_pair(ir,output_dir=Path(d),executable="/tools/openroad")
        call.assert_called_once()
        self.assertEqual(check["status"],"pass")
        self.assertFalse(result["snapshot_platform_scope"]["fixed_latency_model"])

    def test_incompatible_mapping_fails_before_provider(self):
        for kind, resources in (("LUT6",{"lut":1}),("$_DFF_P_",{"ff":2}),("DP16KD",{"bram":1})):
            ir=fixture(); ir.value["instances"][0].update(type=kind,resources=resources)
            with patch("emuflow.snapshot_partition.run_tritonpart") as call:
                with self.assertRaises((ValidationError, ValueError)):
                    partition_snapshot_pair(ir,output_dir=Path("unused"),executable="/tools/openroad")
                call.assert_not_called()
