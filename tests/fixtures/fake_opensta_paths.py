#!/usr/bin/env python3
"""Deterministic OpenSTA protocol fixture for multi-FPGA flow tests."""

import os
from pathlib import Path


rows = Path(os.environ["EMUFLOW_STA_NET_MAP"]).read_text().splitlines()[1:]
output = Path(os.environ["EMUFLOW_STA_OUTPUT"])
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
