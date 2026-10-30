"""Fail-fast audit of the driven source required by native implementation."""
from __future__ import annotations

from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow_tools.ir.indexed_physical_ir import IndexedPhysicalIR
from emuflow.common.json_io import write_json


def validate_vivado_source(ir_path: Path, report_path: Path | None = None) -> dict:
    count = 0
    declared = 0
    examples = []
    with IndexedPhysicalIR(ir_path) as ir:
        # One SQL pass; only a bounded diagnostic sample crosses into Python.
        for row in ir.connection.execute("""
            SELECT n.id,n.cut_class FROM net_records n
            WHERE NOT EXISTS (SELECT 1 FROM endpoint_records d
                              WHERE d.net_seq=n.seq AND d.role=0)
              AND EXISTS (SELECT 1 FROM endpoint_records s
                          WHERE s.net_seq=n.seq AND s.role=1)
            ORDER BY n.seq
        """):
            count += 1
            declared += row[1] == "undriven"
            if len(examples) < 8:
                examples.append({"net": str(row[0]), "cut_class": str(row[1])})
    report = {"schema": "emuflow.vivado-source-check/v1",
              "status": "pass" if count == 0 else "fail",
              "policy": "reject-used-undriven-nets",
              "used_undriven_nets": count, "source_declared_undriven_nets": declared,
              "examples": examples}
    if report_path is not None:
        write_json(report_path, report, compact=True)
    if count:
        raise ValidationError(
            f"Vivado input has {count} used undriven nets; example: {examples[0]['net']}. "
            "Upstream source binding or an explicitly qualified OOC input model "
            "is required; no constant driver was invented."
        )
    return report
