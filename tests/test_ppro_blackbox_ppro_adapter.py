from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_details import parse_calibration_details, validate_calibration_details
from emuflow.ppro_blackbox_ppro_adapter import parse_ppro_2026_ordinary_reports


PARTITION_REPORT = """
2.Device resource:
| Resource Type | PIO | INT | LUT | FF | BRAM | LUTRAM | DSP | URAM | MSGPORT | STATICPROBE | BOUNDARY |
| F11           | 10  | 20  | 600 | 900 | 8 | 0 | 12 | 1 | 0 | 0 | 0 |
| F33           | 11  | 21  | 400 | 700 | 4 | 0 | 8  | 0 | 0 | 0 | 0 |
| Total Resource | 21 | 41 | 1000 | 1600 | 12 | 0 | 20 | 1 | 0 | 0 | 0 |
3.Device resource utilization:
| Resource Type | PIO | INT | LUT | FF | BRAM | LUTRAM | DSP | URAM | MSGPORT | STATICPROBE | BOUNDARY |
| F11           | 1% | 2% | 3% | 4% | 5% | 0% | 6% | 7% | 0% | 0% | 0% |
| F33           | 2% | 3% | 4% | 5% | 6% | 0% | 7% | 0% | 0% | 0% | 0% |
| Total Util    | 2% | 3% | 4% | 5% | 6% | 0% | 7% | 4% | 0% | 0% | 0% |
"""

ROUTE_REPORT = """
2.1 fpga tdm connect net num
| src_fpga | dst_fpga | tdm_net_num |
| 11 | 33 | 388 |
| 33 | 11 | 39 |
2.2 fpga untdm connect net num
| src_fpga | dst_fpga | untdm_net_num |
| 33 | 11 | 42 |
2.3 tdm cable info report
| srcFPGA | dstFPGA | srcJconn | dstJconn |
| 11 | 33 | J26 | J26 |
2.4 tdm_info report
| FPGAID | targetFPGAID | BankID | targetBankID | dir | eringDevID | lineRate | channelNum | maxRatio |
| F11 | F33 | 703 | 703 | INPUT | 1 | 1600 | 5 | 8 |
| F11 | F33 | 704 | 704 | OUTPUT | 2 | 1600 | 26 | 16 |
| F33 | F11 | 703 | 703 | OUTPUT | 1 | 1600 | 5 | 8 |
2.5 fpga tdm detailed info file path
"""

TIMING_REPORT = """
Critical Path Report
  60.00 60.00 FPGA_1 16 endpoint
  60.10 data arrival time ( normalized delay 60.10 )
  42.00 data arrival time ( normalized delay 42.00 )
"""

POST_PARTITION_SSTA_REPORT = """
## Setup ## : (Max Frequency is infered by setup slack or datapath delay)
|         PathGroup          | Constrained Period(ns) | Illegal/Total | worst slack/data path(ns) | Max Freq(MHz) | CrossFpga  |
| None to clk(unconstrained) |           --           |   0/551256    |           6.960           |    143.678    |     1      |
Setup:(Path is sorted by slack from small to large)
Setup-PathGroup: None to clk(unconstrained)
PathName  PathGroup                   CrossFpga  Slack(ns)   Startpoint  Endpoint
Path1     None to clk(unconstrained)          1      6.960   i_ready    FPGA_4/u0/d

Path1
Startpoint: i_ready (input port)
Endpoint: FPGA_4/u0 (rising edge-triggered flip-flop)
Path Group: unconstrained
Path Type: max
   0.000    0.000 ^ input external delay
   6.960    6.960 ^ FPGA_4/u0/d (S2C_DFFRS)
            6.960   data arrival time
(Path is unconstrained)
"""

MIXED_POST_PARTITION_SSTA_REPORT = POST_PARTITION_SSTA_REPORT + """
Path2     clk to clk                           1     -1.250   FPGA_1/u0/q FPGA_4/u1/d

Path2
Startpoint: FPGA_1/u0 (rising edge-triggered flip-flop)
Endpoint: FPGA_4/u1 (rising edge-triggered flip-flop)
Path Group: clk
Path Type: max
   0.000    0.000 ^ FPGA_1/u0/q (S2C_DFFRS)
   8.250    8.250 ^ FPGA_4/u1/d (S2C_DFFRS)
            8.250   data arrival time
"""

SIGNED_SLACK_SUMMARY_SSTA_REPORT = """
Path1     clk to clk                           1    -41.660   FPGA_1/u0/q FPGA_4/u1/d

Path1
Startpoint: FPGA_1/u0 (rising edge-triggered flip-flop)
Endpoint: FPGA_4/u1 (rising edge-triggered flip-flop)
Path Group: clk
Path Type: max
   0.000    0.000 ^ FPGA_1/u0/q (S2C_DFFRS)
  51.660   51.660 ^ FPGA_4/u1/d (S2C_DFFRS)
           51.660   data arrival time

  10.000   10.000   data required time
          -51.660   data arrival time
          -41.660   slack (VIOLATED)
"""


class PProBlackboxPProAdapterTest(unittest.TestCase):
    def _reports(self, root: Path):
        partition = root / "pa0.rpt"
        route = root / "sr0.rpt"
        timing = root / "sr0_time.rpt"
        partition.write_text(PARTITION_REPORT, encoding="utf-8")
        route.write_text(ROUTE_REPORT, encoding="utf-8")
        timing.write_text(TIMING_REPORT, encoding="utf-8")
        return {
            "resource_summary": partition,
            "partition_summary": partition,
            "route_summary": route,
            "system_timing": timing,
        }

    def test_parses_compact_vendor_neutral_metrics(self):
        with tempfile.TemporaryDirectory() as raw:
            metrics = parse_ppro_2026_ordinary_reports(
                self._reports(Path(raw)),
                {"instances": 1234},
                {"F11": "F0", "F33": "F1"},
            )
        self.assertEqual(metrics["resource_demand"]["lut"], 1000.0)
        self.assertEqual(metrics["resource_demand"]["bram36k"], 12.0)
        self.assertEqual(metrics["resource_demand"]["dsp48"], 20.0)
        self.assertEqual(metrics["fpga_utilization"][0]["resources"]["lut"], 0.03)
        self.assertEqual(metrics["assignments"], [{"partition": "P0", "fpga": "F0"}, {"partition": "P1", "fpga": "F1"}])
        self.assertEqual(metrics["routes"][0]["signal_count"], 388)
        self.assertEqual(metrics["routes"][1]["signal_count"], 81)
        self.assertEqual(metrics["communication"]["cross_fpga_path_count"], 469.0)
        self.assertEqual(metrics["communication"]["maximum_tdm_ratio"], 16.0)
        self.assertEqual(metrics["timing"]["sr0_worst_cross_fpga_delay_ns"], 60.1)
        self.assertNotIn("F11", repr(metrics))
        self.assertNotIn("F33", repr(metrics))

    def test_unmapped_physical_fpga_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValidationError):
                parse_ppro_2026_ordinary_reports(
                    self._reports(Path(raw)),
                    {"instances": 1234},
                    {"F11": "F0"},
                )

    def test_empty_normal_timing_report_preserves_partition_and_route_metrics(self):
        with tempfile.TemporaryDirectory() as raw:
            reports = self._reports(Path(raw))
            reports["system_timing"].write_text("", encoding="utf-8")
            metrics = parse_ppro_2026_ordinary_reports(
                reports,
                {"instances": 1234},
                {"F11": "F0", "F33": "F1"},
            )
        self.assertEqual(metrics["resource_demand"]["lut"], 1000.0)
        self.assertEqual(metrics["communication"]["maximum_tdm_ratio"], 16.0)
        self.assertEqual(metrics["timing"], {})

    def test_post_partition_ssta_preserves_delay_and_constraint_qualification(self):
        with tempfile.TemporaryDirectory() as raw:
            reports = self._reports(Path(raw))
            reports["system_timing"].write_text(
                POST_PARTITION_SSTA_REPORT, encoding="utf-8"
            )
            metrics = parse_ppro_2026_ordinary_reports(
                reports,
                {"instances": 1234},
                {"F11": "F0", "F33": "F1"},
            )
        self.assertEqual(
            metrics["timing"]["sr0_worst_cross_fpga_delay_ns"], 6.96
        )
        self.assertEqual(
            metrics["timing"]["sr0_reported_cross_fpga_path_count"], 1.0
        )
        self.assertEqual(
            metrics["timing"]["sr0_constrained_cross_fpga_path_count"], 0.0
        )
        self.assertEqual(
            metrics["timing"]["sr0_unconstrained_cross_fpga_path_count"], 1.0
        )
        self.assertEqual(
            metrics["timing"]["sr0_all_cross_fpga_paths_constrained"], 0.0
        )

    def test_post_partition_ssta_counts_mixed_constraint_coverage(self):
        with tempfile.TemporaryDirectory() as raw:
            reports = self._reports(Path(raw))
            reports["system_timing"].write_text(
                MIXED_POST_PARTITION_SSTA_REPORT, encoding="utf-8"
            )
            metrics = parse_ppro_2026_ordinary_reports(
                reports,
                {"instances": 1234},
                {"F11": "F0", "F33": "F1"},
            )
        self.assertEqual(
            metrics["timing"]["sr0_reported_cross_fpga_path_count"], 2.0
        )
        self.assertEqual(
            metrics["timing"]["sr0_constrained_cross_fpga_path_count"], 1.0
        )
        self.assertEqual(
            metrics["timing"]["sr0_unconstrained_cross_fpga_path_count"], 1.0
        )
        self.assertEqual(metrics["timing"]["sr0_worst_cross_fpga_delay_ns"], 8.25)

    def test_post_partition_ssta_ignores_signed_slack_summary_operand(self):
        with tempfile.TemporaryDirectory() as raw:
            reports = self._reports(Path(raw))
            reports["system_timing"].write_text(
                SIGNED_SLACK_SUMMARY_SSTA_REPORT, encoding="utf-8"
            )
            metrics = parse_ppro_2026_ordinary_reports(
                reports,
                {"instances": 1234},
                {"F11": "F0", "F33": "F1"},
            )
        self.assertEqual(
            metrics["timing"]["sr0_worst_cross_fpga_delay_ns"], 51.66
        )

    def test_bank_records_preserve_direction_and_reciprocal_identity(self):
        details = parse_calibration_details(ROUTE_REPORT, "", {"F11": "F0", "F33": "F1"})
        rows = details["bank_rows"]
        self.assertEqual(len(rows), 3)
        reciprocal = [row for row in rows if row["reported_channel_count"] == 5]
        self.assertEqual(len(reciprocal), 2)
        self.assertEqual(reciprocal[0]["bank_pair_id"], reciprocal[1]["bank_pair_id"])
        self.assertEqual({row["direction"] for row in reciprocal}, {"input", "output"})
        self.assertEqual({row["maximum_tdm_ratio"] for row in rows}, {8, 16})
        self.assertEqual(details["identification"]["bank_inventory"], "not_identifiable")
        self.assertNotIn("bank_capacity", repr(details))
        self.assertNotIn("703", repr(details["bank_rows"][0].get("local_bank", "")))

    def test_paths_preserve_arrival_identity_without_global_ratio_substitution(self):
        details = parse_calibration_details(ROUTE_REPORT, MIXED_POST_PARTITION_SSTA_REPORT, {"F11": "F0", "F33": "F1"})
        paths = details["timing_paths"]
        self.assertEqual([path["id"] for path in paths], ["setup-path1", "setup-path2"])
        self.assertEqual([path["arrival_ns"] for path in paths], [6.96, 8.25])
        self.assertEqual([path["constrained"] for path in paths], [False, True])
        self.assertTrue(all(path["crossings"] == [] for path in paths))
        self.assertTrue(all(point["reported_tdm_ratio"] is None for path in paths for point in path["points"]))
        self.assertEqual(paths[1]["points"][1]["increment_ns"], 8.25)
        self.assertIn("unmapped-timing-fpga-label", details["identification"]["reasons"])
        self.assertNotIn("FPGA_1", repr(details))
        self.assertNotIn("FPGA_4", repr(details))

    def test_timing_fpga_prefix_is_not_assumed_to_be_route_fpga_id(self):
        details = parse_calibration_details("", SIGNED_SLACK_SUMMARY_SSTA_REPORT, {"F1": "F0", "F4": "F1"})
        path = details["timing_paths"][0]
        self.assertEqual(path["arrival_ns"], 51.66)
        self.assertEqual(len(path["points"]), 2)
        self.assertTrue(all(point["fpga"] is None for point in path["points"]))
        self.assertNotEqual(path["points"][0]["pin_token"], path["points"][1]["pin_token"])

    def test_missing_segment_and_arrival_are_unavailable_not_zero(self):
        text = SIGNED_SLACK_SUMMARY_SSTA_REPORT.split("           51.660   data arrival time")[0]
        details = parse_calibration_details("", text, {"F1": "F0", "F4": "F1"})
        self.assertIsNone(details["timing_paths"][0]["arrival_ns"])
        self.assertEqual(details["timing_paths"][0]["crossings"], [])
        self.assertEqual(details["identification"]["single_hop_delay"], "not_identifiable")

    def test_details_do_not_promote_normalized_whole_path_delay(self):
        details = parse_calibration_details("", TIMING_REPORT, {"F11": "F0", "F33": "F1"})
        self.assertEqual(details["timing_paths"], [])
        self.assertIn("no-path-specific-timing", details["identification"]["reasons"])

    def test_point_identity_and_unqualified_crossing_claims_fail_closed(self):
        original = parse_calibration_details(ROUTE_REPORT, SIGNED_SLACK_SUMMARY_SSTA_REPORT, {"F11": "F0", "F33": "F1"})
        for mutation in ("token", "crossing", "identified", "nan", "negative-arrival"):
            with self.subTest(mutation=mutation):
                details = deepcopy(original)
                path = details["timing_paths"][0]
                if mutation == "token":
                    path["points"][0]["descriptor"] = "other/pin"
                elif mutation == "crossing":
                    path["crossings"] = [{"delay_ns": 7.2, "tdm_ratio": 16}]
                elif mutation == "identified":
                    details["identification"]["single_hop_delay"] = "identified"
                elif mutation == "nan":
                    path["points"][0]["increment_ns"] = float("nan")
                else:
                    path["arrival_ns"] = -1.0
                with self.assertRaises(ValidationError):
                    validate_calibration_details(details)

    def test_setup_and_hold_path_names_are_separate(self):
        text = "## Setup ##\n" + SIGNED_SLACK_SUMMARY_SSTA_REPORT
        text += "\n## Hold ##\n" + SIGNED_SLACK_SUMMARY_SSTA_REPORT
        details = parse_calibration_details("", text, {"F1": "F0", "F4": "F1"})
        self.assertEqual({path["id"] for path in details["timing_paths"]}, {"setup-path1", "hold-path1"})

    def test_duplicate_path_and_bank_records_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, "repeated path"):
            parse_calibration_details("", SIGNED_SLACK_SUMMARY_SSTA_REPORT * 2, {"F1": "F0", "F4": "F1"})
        row = "| F11 | F33 | 703 | 703 | INPUT | 1 | 1600 | 5 | 8 |\n"
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            parse_calibration_details(ROUTE_REPORT.replace("2.5 fpga", row + "2.5 fpga"), "", {"F11": "F0", "F33": "F1"})

    def test_only_explicit_path_point_ratio_is_retained(self):
        text = SIGNED_SLACK_SUMMARY_SSTA_REPORT.replace("FPGA_4/u1/d (S2C_DFFRS)", "F33/u1/d (S2C_DFFRS) TDM ratio: 8")
        details = parse_calibration_details(ROUTE_REPORT, text, {"F11": "F0", "F33": "F1"})
        point = details["timing_paths"][0]["points"][1]
        self.assertEqual(point["reported_tdm_ratio"], 8)
        self.assertEqual(point["fpga"], "F1")
        self.assertEqual(details["timing_paths"][0]["crossings"], [])

    def test_bank_identity_stays_stable_when_direction_changes(self):
        aliases = {"F11": "F0", "F33": "F1"}
        left = parse_calibration_details(ROUTE_REPORT, "", aliases)["bank_rows"]
        switched = ROUTE_REPORT.replace("INPUT", "SWAP").replace("OUTPUT", "INPUT").replace("SWAP", "OUTPUT")
        right = parse_calibration_details(switched, "", aliases)["bank_rows"]
        self.assertEqual([row["bank_pair_id"] for row in left], [row["bank_pair_id"] for row in right])
        self.assertTrue(all(a["direction"] != b["direction"] for a, b in zip(left, right)))

    def test_unsupported_bank_columns_are_not_silently_reinterpreted(self):
        changed = ROUTE_REPORT.replace("channelNum", "physical_capacity")
        with self.assertRaisesRegex(ValidationError, "unsupported"):
            parse_calibration_details(changed, "", {"F11": "F0", "F33": "F1"})


if __name__ == "__main__":
    unittest.main()
