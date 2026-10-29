"""Independent small-fixture clock/resource report checks; never installed."""
from collections import Counter
import json
from pathlib import Path
from typing import Any, Dict
from emuflow.ir.emuir_storage import open_emuir_sqlite_read_only
from emuflow.ir.resources import ResourceVector


def _small_fixture(ir_path):
    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        assert connection.execute('SELECT COUNT(*) FROM instances').fetchone()[0] <= 4096
        assert connection.execute('SELECT COUNT(*) FROM nets').fetchone()[0] <= 8192
        assert connection.execute('SELECT COUNT(*) FROM endpoints').fetchone()[0] <= 32768
    finally:
        connection.close()


def _clock_pin(instance_type: str, port: str) -> bool:
    if instance_type.startswith("FD"):
        return port == "C"
    if instance_type.startswith("$_DFF_"):
        return port == "C"
    return instance_type in {"VTR_SP_RAM", "VTR_DP_RAM"} and port == "clk"


def _fabric_logic(instance_type: str) -> bool:
    return instance_type.startswith("LUT") or instance_type == "$lut"


def analyze_indexed_clock_topology(ir_path: Path) -> Dict[str, Any]:
    """Compute the Phase 1 clock audit without materializing the graph."""

    _small_fixture(ir_path)
    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        sink_rows = connection.execute(
            """
            SELECT n.seq AS net_seq, n.id AS net_id, e.port, i.type
              FROM nets AS n
              JOIN endpoints AS e
                ON e.net_seq = n.seq AND e.role = 1
              JOIN instances AS i ON i.id = e.instance_id
             ORDER BY n.seq, e.ordinal
            """
        )
        current_net = None
        current_id = None
        clock_sinks = 0
        clock_nets = 0
        fabric_clock_nets = []

        def finish_net() -> None:
            nonlocal clock_nets
            if current_net is None or clock_sinks == 0:
                return
            clock_nets += 1
            lut_drivers = sorted(
                row["instance_id"]
                for row in connection.execute(
                    """
                    SELECT e.instance_id, i.type
                      FROM endpoints AS e
                      JOIN instances AS i ON i.id = e.instance_id
                     WHERE e.net_seq = ? AND e.role = 0
                     ORDER BY e.ordinal
                    """,
                    (current_net,),
                )
                if _fabric_logic(row["type"])
            )
            if lut_drivers:
                fabric_clock_nets.append(
                    {
                        "net": current_id,
                        "lut_drivers": lut_drivers,
                        "clocked_ffs": clock_sinks,
                    }
                )

        for row in sink_rows:
            net_seq = row["net_seq"]
            if current_net is not None and net_seq != current_net:
                finish_net()
                clock_sinks = 0
            current_net = net_seq
            current_id = row["net_id"]
            if _clock_pin(row["type"], row["port"]):
                clock_sinks += 1
        finish_net()
        clocked_ffs = sum(item["clocked_ffs"] for item in fabric_clock_nets)
        return {
            "status": "pass" if not fabric_clock_nets else "unsafe",
            "ff_clock_nets": clock_nets,
            "fabric_logic_clock_nets": len(fabric_clock_nets),
            "fabric_logic_clocked_ffs": clocked_ffs,
            "maximum_fabric_clock_fanout": max(
                (item["clocked_ffs"] for item in fabric_clock_nets),
                default=0,
            ),
            "fabric_clock_details": fabric_clock_nets,
        }
    finally:
        connection.close()


def indexed_phase1_facts(
    ir_path: Path,
) -> tuple[str, list[str], ResourceVector, Counter[str]]:
    """Read only the bounded summaries needed by the Phase 1 report."""

    _small_fixture(ir_path)
    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?)",
                ("design_json", "warnings_json"),
            )
        }
        design = json.loads(metadata["design_json"])
        warnings = json.loads(metadata["warnings_json"])
        resource_query = """SELECT resource, SUM(amount) AS amount
                            FROM instance_resources GROUP BY resource"""
        if connection.execute("PRAGMA user_version").fetchone()[0] == 2:
            resource_query = """SELECT r.key AS resource,SUM(r.value*g.amount) AS amount
                FROM (SELECT resources_seq,COUNT(*) AS amount FROM cell_records GROUP BY resources_seq) g
                JOIN payloads p ON p.seq=g.resources_seq,json_each(p.value) r GROUP BY r.key"""
        totals = ResourceVector.from_mapping(
            {
                row["resource"]: row["amount"]
                for row in connection.execute(resource_query)
            },
            context="indexed EmuIR resource totals",
        )
        cut_classes: Counter[str] = Counter(
            {
                row["cut_class"]: row["amount"]
                for row in connection.execute(
                    """
                    SELECT cut_class, COUNT(*) AS amount
                      FROM nets
                     GROUP BY cut_class
                    """
                )
            }
        )
        return design["name"], warnings, totals, cut_classes
    finally:
        connection.close()
