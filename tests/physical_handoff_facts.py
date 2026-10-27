"""Small explicit typed handoff facts, without production publication or packing."""
import json
from pathlib import Path
import sqlite3

from emuflow.xilinx_physical_handoff import _HANDOFF_DDL, XILINX_PHYSICAL_HANDOFF_SCHEMA


def physical_handoff_facts(path, packed, placement):
    clusters = packed["clusters"]
    assert len(clusters) <= 4096
    assert sum(len(c["assignments"]) for c in clusters) <= 8192
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    placed = {row["cluster"]: row for row in placement["clusters"]}
    with sqlite3.connect(path) as db:
        db.executescript(_HANDOFF_DDL)
        document = {k:v for k,v in packed.items() if k not in {
            "clusters", "cascade_chains", "unplaced_constants"}}
        placement_header = {k:v for k,v in placement.items() if k != "clusters"}
        placement_header["source"] = {k:v for k,v in placement_header.get("source", {}).items()
                                      if k != "packed_sha256"}
        db.executemany("INSERT INTO document VALUES (?,?)", (
            ("packed", encode(document)), ("placement", encode(placement_header))))
        count = 0
        for seq, cluster in enumerate(clusters):
            p = placed[cluster["id"]]
            db.execute("INSERT INTO cluster VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                seq, cluster["id"], cluster["kind"], encode(cluster.get("site_templates")),
                encode(cluster.get("control_set")),
                encode(cluster["control_sets"]) if "control_sets" in cluster else None,
                cluster.get("site_mode"),p["site"],p["site_type"],p["x"],p["y"],
                int(bool(p.get("fixed"))),encode(p.get("physical_region"))))
            placement_assignments = {a["instance"]:(i,a) for i,a in enumerate(p["assignments"])}
            for ordinal, assignment in enumerate(cluster["assignments"]):
                order, target = placement_assignments[assignment["instance"]]
                db.execute("INSERT INTO assignment VALUES (?,?,?,?,?,?,?,?)", (
                    seq, assignment["instance"],assignment["cell_type"],assignment["bel"],
                    target.get("placement_mode"),target.get("site"),ordinal,order))
                count += 1
        cascades, constants = packed.get("cascade_chains", []), packed.get("unplaced_constants", [])
        db.executemany("INSERT INTO cascade VALUES (?,?)", ((i,encode(v)) for i,v in enumerate(cascades)))
        db.executemany("INSERT INTO constant VALUES (?,?)", enumerate(constants))
        db.execute("INSERT INTO manifest VALUES (1,?,?,?,?,?)", (
            XILINX_PHYSICAL_HANDOFF_SCHEMA,len(clusters),count,len(cascades),len(constants)))
