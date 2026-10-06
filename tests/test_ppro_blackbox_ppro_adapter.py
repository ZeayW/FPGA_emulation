from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
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
            metrics["timing"]["sr0_all_cross_fpga_paths_constrained"], 0.0
        )


if __name__ == "__main__":
    unittest.main()
