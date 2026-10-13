from __future__ import annotations

import tempfile
import unittest
import hashlib
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

# Minimal ordinary-report grammar observed in the fresh 32-bit, single-hop
# pilot. This is a parser fixture, not a substitute for a sealed tool run.
SYSTEM_ROUTE_TIMING_REPORT = """
Critical Path Report (Top 10 records will be listed at most, sorting by 'normalized delay' in descending order)
Startpoint: FPGA_1_ins/P0/payload_reg/i1/q
            (clocked by clk)
Endpoint  : FPGA_2_ins/P1/received_payload_reg/i1/d
            (clocked by clk)
Delay(ns) Time(ns) Die                                      TDM_ratio  pin_name
     0.00     0.00 FPGA_1                                              P0/payload_reg/i1/q/CK (DFF_X1)
     0.00     0.00 FPGA_1                                              P0/payload_reg/i1/q/Q (DFF_X1)
     0.00     0.00 FPGA_1                                              P0.payload[0]_buf/Z (BUF_X1)
     7.00     7.00 FPGA_2                                   2          P0.payload[0]_buf/Z (BUF_X1)
     0.10     7.10 FPGA_2                                              P1/received_payload_reg/i1/d/D (DFF_X1)
              7.10 data arrival time     ( normalized delay  7.10      )
"""

SYSTEM_ROUTE_NET_TDM_REPORT = """
1. tdm_set report
| port name | src FPGA | dst FPGA | untdm/tdm |
2. untdm report
| net name | FPGA Pair | is clock |
""" + "\n".join(f"| P0.payload[{bit}] | (F1,F2) | 0 |" for bit in range(32)) + """
| reset_n_pio_0.o | (F2,F1) | 0 |
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

    def _route_details(self, timing=SYSTEM_ROUTE_TIMING_REPORT, nets=SYSTEM_ROUTE_NET_TDM_REPORT):
        return parse_calibration_details("", "", {"F1": "F0", "F2": "F1"},
            route_timing_text=timing, net_tdm_text=nets,
            report_sha256={"route_timing": hashlib.sha256(timing.encode()).hexdigest(), "net_tdm": hashlib.sha256(nets.encode()).hexdigest()})

    def test_real_route_grammar_binds_one_crossing_and_preserves_ratio_encoding(self):
        details = self._route_details()
        self.assertEqual(details["schema"], "emuflow.ppro-calibration-details/v2")
        self.assertEqual(details["route_timing_path_limit"], 10)
        self.assertEqual(details["net_tdm_capture_status"], "complete")
        self.assertEqual(len(details["net_tdm_rows"]), 33)
        path = details["route_timing_paths"][0]
        self.assertEqual(path["source_clock"], "clk")
        self.assertEqual(path["sink_clock"], "clk")
        self.assertEqual(path["arrival_ns"], 7.1)
        self.assertEqual(path["points"][-1]["increment_ns"], 0.1)
        self.assertEqual(path["unbound_crossing_count"], 0)
        self.assertEqual(len(path["crossings"]), 1)
        crossing = path["crossings"][0]
        self.assertEqual(crossing["delay_ns"], 7.0)
        self.assertEqual(crossing["reported_tdm_ratio"], 2)
        self.assertEqual(crossing["transport_kind"], "untdm")
        self.assertEqual(crossing["net_name"], "P0.payload[0]")
        self.assertEqual((crossing["source_fpga"], crossing["sink_fpga"]), ("F0", "F1"))
        self.assertNotIn("FPGA_", repr(details))

    def test_unknown_net_or_wrong_direction_does_not_inherit_untdm_status(self):
        for nets in ("", SYSTEM_ROUTE_NET_TDM_REPORT.replace("(F1,F2)", "(F2,F1)")):
            with self.subTest(nets=bool(nets)):
                crossing = self._route_details(nets=nets)["route_timing_paths"][0]["crossings"][0]
                self.assertEqual(crossing["transport_kind"], "unbound")
                self.assertIsNone(crossing["net_token"])
                self.assertEqual(crossing["reported_tdm_ratio"], 2)

    def test_unknown_net_table_is_partial_and_not_guessed_as_tdm(self):
        nets = SYSTEM_ROUTE_NET_TDM_REPORT + "\n3. new_transport report\n| opaque field | unknown value |\n"
        details = self._route_details(nets=nets)
        self.assertEqual(details["net_tdm_capture_status"], "partial")
        self.assertEqual(len(details["net_tdm_rows"]), 33)

    def test_unknown_section_before_untdm_stays_partial_without_pipe_rows(self):
        for unknown_section in (
            "0. tdm report\nP0.return_payload[0] F2 F1 ratio 8\n",
            "0.tdm report\nP0.return_payload[0] F2 F1 ratio 8\n",
            "0. tdm report\n",
        ):
            with self.subTest(section=unknown_section):
                details = self._route_details(nets=unknown_section + SYSTEM_ROUTE_NET_TDM_REPORT)
                self.assertEqual(details["net_tdm_capture_status"], "partial")
                self.assertEqual(len(details["net_tdm_rows"]), 33)
                crossing = details["route_timing_paths"][0]["crossings"][0]
                self.assertEqual(crossing["transport_kind"], "untdm")
                self.assertEqual(crossing["net_name"], "P0.payload[0]")
        self.assertEqual(self._route_details()["net_tdm_capture_status"], "complete")

    def test_crossing_requires_same_pin_and_explicit_ratio(self):
        for text in (
            SYSTEM_ROUTE_TIMING_REPORT.replace("2          P0.payload[0]_buf/Z", "2          other_net_buf/Z"),
            SYSTEM_ROUTE_TIMING_REPORT.replace("2          P0.payload[0]_buf/Z", "           P0.payload[0]_buf/Z"),
        ):
            path = self._route_details(timing=text)["route_timing_paths"][0]
            self.assertEqual(path["crossings"], [])
            self.assertEqual(path["unbound_crossing_count"], 1)

    def test_route_report_columns_and_time_arithmetic_fail_closed(self):
        for text in (
            SYSTEM_ROUTE_TIMING_REPORT.replace("TDM_ratio", "unknown_column"),
            SYSTEM_ROUTE_TIMING_REPORT.replace("7.00     7.00", "7.00     8.00"),
            SYSTEM_ROUTE_TIMING_REPORT.replace("7.00     7.00 FPGA_2", "7.00     7.00 UNKNOWN_2"),
        ):
            with self.assertRaises(ValidationError):
                self._route_details(timing=text)

    def test_route_crossing_validator_rebuilds_binding_from_points(self):
        for field, value in (("delay_ns", 7.1), ("reported_tdm_ratio", 1), ("sink_point_id", "point4"), ("transport_kind", "tdm")):
            with self.subTest(field=field):
                details = self._route_details()
                details["route_timing_paths"][0]["crossings"][0][field] = value
                with self.assertRaisesRegex(ValidationError, "binding mismatch"):
                    validate_calibration_details(details)

    def test_legacy_details_remain_readable(self):
        details = self._route_details()
        legacy = {key: details[key] for key in ("schema", "bank_rows", "timing_paths", "identification")}
        legacy["schema"] = "emuflow.ppro-calibration-details/v1"
        self.assertEqual(validate_calibration_details(legacy)["schema"], legacy["schema"])

    def test_allowlisted_route_reports_have_byte_hashes_and_replace_aggregate_source(self):
        with tempfile.TemporaryDirectory() as raw:
            reports = self._reports(Path(raw))
            reports["system_timing"].unlink()
            reports["system_timing"] = Path(raw) / "gr0_ssta_post_part.rpt"
            reports["route_timing"] = Path(raw) / "sr0_time.rpt"
            reports["net_tdm"] = Path(raw) / "sr0_tdm.rpt"
            route_bytes = SYSTEM_ROUTE_TIMING_REPORT.replace("FPGA_1", "FPGA_11").replace("FPGA_2", "FPGA_33").replace("\n", "\r\n").encode()
            reports["route_timing"].write_bytes(route_bytes)
            reports["net_tdm"].write_text(SYSTEM_ROUTE_NET_TDM_REPORT.replace("F1", "F11").replace("F2", "F33"))
            metrics = parse_ppro_2026_ordinary_reports(reports, {"instances": 1234}, {"F11": "F0", "F33": "F1"})
        self.assertEqual(metrics["timing"]["sr0_worst_cross_fpga_delay_ns"], 7.1)
        details = metrics["calibration_details"]
        self.assertEqual(details["report_sha256"]["route_timing"], hashlib.sha256(route_bytes).hexdigest())
        self.assertNotIn("system_timing", details["report_sha256"])
        self.assertEqual(details["timing_paths"], [])
        self.assertEqual(details["route_timing_paths"][0]["crossings"][0]["delay_ns"], 7.0)


if __name__ == "__main__":
    unittest.main()
