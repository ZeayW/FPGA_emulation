"""Small current-schema SQL facts for reader/kernel tests, never a flow producer.

Production ownership, legalization, source validation and atomic publication are
exercised through the native certificate producer. This helper only encodes at
most 4096 already specified clusters for independent downstream fixtures.
"""

import hashlib
import json
from pathlib import Path
import sqlite3

_DDL = (
    Path(__file__).resolve().parents[1]
    / "src/python/emuflow/data/schema/xilinx_physical_handoff.sql"
).read_text()


def write_handoff_fixture(path, packed, placement, *, canonical=False, repairs=None):
    clusters = list(packed["clusters"])
    if len(clusters) > 4096:
        raise ValueError("small handoff fixture exceeds cluster bound")
    placed = {row["cluster"]: row for row in placement["clusters"]}
    if len(placed) != len(clusters) or set(placed) != {row["id"] for row in clusters}:
        raise ValueError("fixture cluster facts disagree")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    if repairs is not None:
        repairs = list(repairs)
        if len(repairs) > 4096:
            raise ValueError("small handoff fixture exceeds repair bound")
        canonical = True
        descriptor = {
            "schema": "emuflow.xilinx-site-pin-repairs/v1",
            "records": len(repairs),
            "sha256": hashlib.sha256(
                b"".join(
                    (
                        json.dumps(
                            row,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                    for row in repairs
                )
            ).hexdigest(),
        }
        packed = dict(
            packed,
            policy=dict(
                packed["policy"],
                site_pin_legalization={
                    "repairs": descriptor,
                    "moved_ffs": len(repairs),
                    "sites": len({row["cluster"] for row in repairs}),
                },
            ),
        )
    with sqlite3.connect(path) as db:
        db.executescript(_DDL)
        if repairs is None:
            db.execute("DROP TABLE site_pin_repair")
        if canonical:
            db.execute("CREATE TABLE feature(name TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("INSERT INTO feature VALUES('canonical-cluster-id-order-v1')")
        if repairs is not None:
            db.execute("INSERT INTO feature VALUES('typed-site-pin-repairs-v1')")
        for name, source, excluded in (
            ("packed", packed, {"clusters", "cascade_chains", "unplaced_constants"}),
            ("placement", placement, {"clusters"}),
        ):
            document = {
                key: value for key, value in source.items() if key not in excluded
            }
            if name == "placement":
                document["source"] = {
                    key: value
                    for key, value in source.get("source", {}).items()
                    if key != "packed_sha256"
                }
            db.execute("INSERT INTO document VALUES(?,?)", (name, encode(document)))
        assignments = 0
        for ordinal, cluster in enumerate(clusters):
            position = placed[cluster["id"]]
            db.execute(
                "INSERT INTO cluster VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ordinal,
                    cluster["id"],
                    cluster["kind"],
                    encode(cluster["site_templates"]),
                    encode(cluster.get("control_set")),
                    encode(cluster["control_sets"])
                    if "control_sets" in cluster
                    else None,
                    cluster.get("site_mode"),
                    position["site"],
                    position["site_type"],
                    position["x"],
                    position["y"],
                    int(position.get("fixed", False)),
                    encode(position.get("physical_region")),
                ),
            )
            records = {
                row["instance"]: (index, row)
                for index, row in enumerate(position["assignments"])
            }
            if set(records) != {row["instance"] for row in cluster["assignments"]}:
                raise ValueError("fixture assignment facts disagree")
            for packed_ordinal, row in enumerate(cluster["assignments"]):
                placement_ordinal, physical = records[row["instance"]]
                if (
                    physical["cell_type"] != row["cell_type"]
                    or physical["bel"] != row["bel"]
                ):
                    raise ValueError("fixture packing and placement facts disagree")
                db.execute(
                    "INSERT INTO assignment VALUES(?,?,?,?,?,?,?,?)",
                    (
                        ordinal,
                        row["instance"],
                        row["cell_type"],
                        row["bel"],
                        physical["placement_mode"],
                        physical["site"],
                        packed_ordinal,
                        placement_ordinal,
                    ),
                )
                assignments += 1
        if repairs is not None:
            db.executemany(
                "INSERT INTO site_pin_repair VALUES(?,?,?,?,?)",
                (
                    (i, row["cluster"], row["instance"], row["from_bel"], row["to_bel"])
                    for i, row in enumerate(repairs)
                ),
            )
        cascades = packed.get("cascade_chains", [])
        constants = packed.get("unplaced_constants", [])
        db.executemany(
            "INSERT INTO cascade VALUES(?,?)",
            ((i, encode(row)) for i, row in enumerate(cascades)),
        )
        db.executemany("INSERT INTO constant VALUES(?,?)", enumerate(constants))
        db.execute(
            "INSERT INTO manifest VALUES(1,?,?,?,?,?)",
            (
                "emuflow.xilinx-physical-handoff-sqlite/v1",
                len(clusters),
                assignments,
                len(cascades),
                len(constants),
            ),
        )
    return path


def write_bridge_cluster_fixture(path, cluster, packed, placement):
    """Encode one explicitly supplied cluster fact used by Chimew fixtures."""
    position = cluster["_placement"]
    packed = dict(
        packed,
        clusters=[
            {key: value for key, value in cluster.items() if key != "_placement"}
        ],
        cascade_chains=[],
        unplaced_constants=[],
    )
    packed["clusters"][0]["assignments"] = [
        {key: row[key] for key in ("instance", "cell_type", "bel")}
        for row in cluster["assignments"]
    ]
    placement = dict(
        placement,
        clusters=[
            dict(
                position,
                cluster=cluster["id"],
                physical_region=position.get("physical_region"),
                assignments=[
                    dict(
                        instance=row["instance"],
                        cell_type=row["cell_type"],
                        bel=row["bel"],
                        placement_mode=row["_placement_mode"],
                        site=row["_physical_site"],
                    )
                    for row in cluster["assignments"]
                ],
            )
        ],
    )
    return write_handoff_fixture(path, packed, placement, canonical=True)
