#!/usr/bin/env python3
"""Deterministic OpenSTA protocol fixture for multi-FPGA flow tests."""

import json
import os
from pathlib import Path


rows = Path(os.environ["EMUFLOW_STA_NET_MAP"]).read_text().splitlines()[1:]
output = Path(os.environ["EMUFLOW_STA_OUTPUT"])
if not os.environ.get("EMUFLOW_STA_THROUGH_NETS"):
    pin_rows = Path(os.environ["EMUFLOW_STA_PIN_MAP"]).read_text().splitlines()[1:]
    pin_by_net = {}
    for row in pin_rows:
        pin_hex, net_hex = row.split("\t")
        pin_by_net.setdefault(net_hex, bytes.fromhex(pin_hex).decode())
    checks = []
    for index, row in enumerate(rows):
        _, emuir_hex = row.split("\t")
        pin = pin_by_net[emuir_hex]
        checks.append({
            "type": "setup",
            "path_group": "clk",
            "path_type": "max",
            # Keep the historical fixture's intentionally unstructured
            # endpoint identity.  Its purpose is net-path projection, not
            # validating OpenSTA's real instance/pin endpoint binding.
            "startpoint": f"fixture-start-{index}",
            "endpoint": f"fixture-end-{index}",
            "source_clock": "clk",
            "source_clock_edge": "rise",
            "source_path": [{"pin": pin, "arrival": 0.5, "slew": 0.0}],
            "target_clock": "clk",
            "target_clock_edge": "rise",
            "data_arrival_time": 0.5,
            "required_time": 10.0,
            "slack": 9.5,
        })
    output.write_text(json.dumps({"checks": checks}) + "\n")
    raise SystemExit(0)

header = (
    "path_id_hex\tclock_domain_hex\tclock_period_ns\t"
    "slack_ns\tfixed_delay_ns\tpath_nets_hex"
)
records = [header]
clock = "clk".encode().hex()
for index, row in enumerate(rows):
    _, emuir_hex = row.split("\t")
    path_id = f"path-{index}".encode().hex()
    records.append(
        f"{path_id}\t{clock}\t10\t9.5\t0.5\t{emuir_hex}"
    )
output.write_text("\n".join(records) + "\n")
if os.environ.get("EMUFLOW_STA_THROUGH_NETS"):
    requested = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()[1:]
    coverage = ["emuir_net_hex\tdriver_count\tqueried_paths\temitted_paths"]
    for row in requested:
        _, emuir_hex = row.split("\t")
        coverage.append(f"{emuir_hex}\t1\t1\t1")
    Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
        "\n".join(coverage) + "\n"
    )
