"""Independent previous transport transformation oracle; never installed."""
from __future__ import annotations
import hashlib,json,os,re,sqlite3,uuid
from pathlib import Path
from collections.abc import Iterator,Mapping,Sequence
from typing import Any
from emuflow.transport_storage import (
    _canonical,_manifest,_record_section,_signal_section,_anchor_relation_section,
    _canonical_document_digest,TransportView,LaneMapView,AnchorView,VirtualBindingView,
    TRANSPORT_STORAGE_SCHEMA,LANE_MAP_STORAGE_SCHEMA,ANCHOR_STORAGE_SCHEMA,
    VIRTUAL_BINDING_STORAGE_SCHEMA,VIRTUAL_BINDING_LOGICAL_SCHEMA,TRANSPORT_STORE_NAME,
)
from emuflow.netlist import (LOGICAL_LANE_MAP_SCHEMA,TRANSPORT_ENDPOINTS_SCHEMA,VIRTUAL_IO_ANCHORS_SCHEMA,
    _endpoint_id,_sv_name,transport_endpoint_port)
from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.phase3_assignment_index import open_phase3_assignment_index
from emuflow.platform import Platform
from emuflow.tdm import TDM_SCHEDULE_SCHEMA,is_sampled_virtual_wire_schedule
from emuflow.tdm_schedule_storage import canonical_tdm_schedule_sha256
from emuflow.sqlite_artifacts import immutable_sqlite_uri
from emuflow.store_publication import (StoreSource,VerifiedStoreFile,bind_view_sources,publish_sqlite_store)
from emuflow.native_tools import native_file_generation
TRANSPORT_APPLICATION_ID=0x454D5558
TRANSPORT_USER_VERSION=1
DDL="CREATE TABLE metadata(\n  key TEXT PRIMARY KEY,\n  value TEXT NOT NULL\n) WITHOUT ROWID;\nCREATE TABLE parts(\n  part_index INTEGER PRIMARY KEY,\n  fpga TEXT NOT NULL UNIQUE\n);\nCREATE TABLE endpoints(\n  id TEXT PRIMARY KEY,\n  fpga TEXT NOT NULL,\n  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),\n  peer TEXT NOT NULL,\n  signal TEXT NOT NULL,\n  schedule_entry TEXT NOT NULL,\n  demand TEXT NOT NULL,\n  net TEXT NOT NULL,\n  link TEXT NOT NULL,\n  slot INTEGER NOT NULL,\n  arrival_slot INTEGER NOT NULL,\n  lane INTEGER NOT NULL,\n  logical_lane INTEGER NOT NULL,\n  record_json TEXT NOT NULL\n) WITHOUT ROWID;\nCREATE INDEX endpoints_fpga_identity ON endpoints(fpga,id);\nCREATE INDEX endpoints_fpga_kind_signal\n  ON endpoints(fpga,kind,signal);\nCREATE INDEX endpoints_anchor\n  ON endpoints(fpga,link,peer,kind,lane,id);\nCREATE TABLE signals(\n  fpga TEXT NOT NULL,\n  kind TEXT NOT NULL CHECK(kind IN ('source','shadow')),\n  seq INTEGER NOT NULL,\n  signal TEXT NOT NULL,\n  PRIMARY KEY(fpga,kind,seq),\n  UNIQUE(fpga,kind,signal)\n) WITHOUT ROWID;\nCREATE TABLE lanes(\n  id TEXT PRIMARY KEY,\n  schedule_entry TEXT NOT NULL UNIQUE,\n  demand TEXT NOT NULL,\n  net TEXT NOT NULL,\n  link TEXT NOT NULL,\n  source_fpga TEXT NOT NULL,\n  sink_fpga TEXT NOT NULL,\n  slot INTEGER NOT NULL,\n  lane INTEGER NOT NULL,\n  logical_lane INTEGER NOT NULL,\n  tx_endpoint TEXT NOT NULL UNIQUE,\n  rx_endpoint TEXT NOT NULL UNIQUE,\n  record_json TEXT NOT NULL\n) WITHOUT ROWID;\nCREATE INDEX lanes_link_slot ON lanes(link,slot,lane);\nCREATE TABLE anchors(\n  id TEXT PRIMARY KEY,\n  fpga TEXT NOT NULL,\n  link TEXT NOT NULL,\n  peer TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  lane INTEGER NOT NULL,\n  record_json TEXT NOT NULL,\n  UNIQUE(fpga,link,peer,kind,lane)\n) WITHOUT ROWID;\nCREATE INDEX anchors_fpga_identity ON anchors(fpga,id);\nCREATE TABLE binding_entries(\n  schedule_entry TEXT PRIMARY KEY,\n  link TEXT NOT NULL,\n  slot INTEGER NOT NULL,\n  wire INTEGER NOT NULL,\n  source_fpga TEXT NOT NULL,\n  sink_fpga TEXT NOT NULL,\n  record_json TEXT NOT NULL,\n  UNIQUE(link,slot,wire)\n) WITHOUT ROWID;\nCREATE INDEX binding_source ON binding_entries(source_fpga,schedule_entry);\nCREATE INDEX binding_sink ON binding_entries(sink_fpga,schedule_entry);\nCREATE TABLE binding_adapter_ports(\n  fpga TEXT NOT NULL,\n  p_port TEXT NOT NULL,\n  n_port TEXT NOT NULL,\n  role TEXT NOT NULL CHECK(role IN ('source','sink')),\n  logical_port TEXT NOT NULL,\n  logical_port_width INTEGER NOT NULL,\n  package_pin_p TEXT NOT NULL,\n  package_pin_n TEXT NOT NULL,\n  iostandard TEXT NOT NULL,\n  PRIMARY KEY(fpga,p_port),\n  UNIQUE(fpga,n_port)\n) WITHOUT ROWID;\nCREATE TABLE binding_physical_ports(\n  fpga TEXT NOT NULL,\n  p_port TEXT NOT NULL,\n  record_json TEXT NOT NULL,\n  schedule_entry_count INTEGER NOT NULL,\n  schedule_entries_sha256 TEXT NOT NULL,\n  PRIMARY KEY(fpga,p_port)\n) WITHOUT ROWID;\n"

def write_transport_storage(
    output_dir: Path,
    assignment_index_path: Path,
    schedule: Mapping[str, Any],
    platform: Platform,
    *,
    design: Mapping[str, Any],
    clocks: Sequence[Mapping[str, Any]],
    pin_plan: Mapping[str, Any] | None = None,
    virtual_board: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish Phase 6 transport relations without hop-sized Python lists."""

    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError("schedule has an unsupported schema")
    design_name = design.get("name")
    if schedule.get("design") != design_name:
        raise ValidationError("schedule design disagrees with EmuIR")
    if schedule.get("platform") != platform.name:
        raise ValidationError("schedule platform disagrees with BoardDB")

    assignment_generation = native_file_generation(assignment_index_path)
    source_bindings = bind_view_sources(
        (schedule, "tdm-schedule-store-v4"),
        (pin_plan, "pin-plan-store-v1"),
    )
    assignment = open_phase3_assignment_index(assignment_index_path)
    try:
        assignment_metadata = {
            row["key"]: json.loads(row["value"])
            for row in assignment.execute("SELECT key,value FROM metadata")
        }
        parts = list(
            assignment.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        )
    finally:
        assignment.close()
    source_bindings += (
        StoreSource(
            Path(assignment_index_path).resolve(),
            {
                "emuflow.phase3-assignment-sqlite/v3": "assignment-index-v3",
                "emuflow.phase3-assignment-sqlite/v4": "assignment-index-v4",
            }[assignment_metadata["schema"]],
            assignment_generation,
        ),
    )
    if assignment_metadata.get("design") != design_name:
        raise ValidationError("assignment index design disagrees with EmuIR")
    if assignment_metadata.get("platform") != platform.name:
        raise ValidationError("assignment index platform disagrees with BoardDB")
    fpga_ids = [str(row["fpga"]) for row in parts]
    if set(fpga_ids) != {fpga.id for fpga in platform.fpgas}:
        raise ValidationError("assignment partitions disagree with BoardDB")

    exact_digest = assignment_metadata.get("semantic_contract_sha256")
    exact_mode = exact_digest is not None
    if exact_mode and (
        not is_sampled_virtual_wire_schedule(schedule)
        or schedule.get("semantic_contract_sha256") != exact_digest
    ):
        raise ValidationError(
            "exact transport inputs do not share one semantic contract"
        )
    exact_storage = assignment_metadata.get("semantic_contract_binary")

    physical_lane_by_entry: dict[str, int] = {}
    pin_lane_iterator: Iterator[tuple[str, int, int]] | None = None
    if pin_plan is not None:
        if pin_plan.get("schema") != "emuflow.placement-aware-pin-plan/v1":
            raise ValidationError("pin plan has an unsupported schema")
        if pin_plan.get("provider") != "chimew-paper-plus-emuflow-electrical-slot-v1":
            raise ValidationError("pin plan has a retired or unsupported provider")
        lane_records = getattr(pin_plan, "iter_lane_records", None)
        if lane_records is not None:
            pin_lane_iterator = iter(lane_records())
        else:
            physical_lane_by_entry = {
                item["schedule_entry"]: item["physical_lane"]
                for item in pin_plan.get("entries", [])
            }
            if len(physical_lane_by_entry) != len(pin_plan.get("entries", [])):
                raise ValidationError("pin plan repeats a schedule entry")

    binding_wire_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    binding_links = {link.id: link for link in platform.links}
    binding_direction_by_wire: dict[str, tuple[str, str]] = {}
    binding_pin_ports: dict[tuple[str, str], str] = {}
    binding_port_pins: dict[tuple[str, str], tuple[str, str]] = {}
    if virtual_board is not None:
        if (
            virtual_board.get("schema") != "emuflow.virtual-board-wiring/v1"
            or virtual_board.get("platform") != platform.name
            or not isinstance(virtual_board.get("wires"), Sequence)
        ):
            raise ValidationError("virtual board wiring is invalid")
        binding_wire_by_key = {
            (wire["link"], wire["wire"]): wire
            for wire in virtual_board["wires"]
        }

    def pin_port_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", value)

    output_dir.mkdir(parents=True, exist_ok=True)
    store_path = output_dir / TRANSPORT_STORE_NAME
    temporary = store_path.with_name(
        f".{store_path.name}.{uuid.uuid4().hex}.tmp"
    )
    # The assignment sidecar is attached below through a read-only immutable
    # ``file:`` URI.  URI handling is a per-connection SQLite flag; without it
    # older production builds interpret the complete URI as a literal filename
    # and fail after Phase 5 even though the sidecar exists.
    connection = sqlite3.connect(temporary, uri=True)
    connection.row_factory = sqlite3.Row
    seen_pin_entries: set[str] = set()
    entry_count = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-131072")
        connection.execute(f"PRAGMA application_id={TRANSPORT_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={TRANSPORT_USER_VERSION}")
        connection.executescript(DDL)
        metadata = {
            "design": design_name,
            "platform": platform.name,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "virtual_clock_order": sorted(item["id"] for item in clocks) or ["clk"],
            "semantic_contract_sha256": exact_digest,
            "semantic_contract_storage": exact_storage,
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            ((key, _canonical(value)) for key, value in sorted(metadata.items())),
        )
        connection.executemany(
            "INSERT INTO parts(part_index,fpga) VALUES (?,?)",
            ((int(row["part_index"]), str(row["fpga"])) for row in parts),
        )
        connection.execute(
            "ATTACH DATABASE ? AS assignment",
            (immutable_sqlite_uri(assignment_index_path),),
        )

        typed_records = getattr(schedule, "iter_transport_records", None)
        joined_records = getattr(schedule, "iter_entry_route_records", None)
        if typed_records is not None:
            entry_routes = (
                (record, None)
                for record in typed_records(
                    identity_order=pin_lane_iterator is not None
                )
            )
        elif joined_records is None:
            routes = schedule.get("routes")
            entries = schedule.get("entries")
            if not isinstance(routes, Sequence) or not isinstance(entries, Sequence):
                raise ValidationError("Phase 6 schedule lacks route metadata")
            route_by_demand = {route["id"]: route for route in routes}
            entry_routes = (
                (entry, route_by_demand.get(entry["demand"]))
                for entry in sorted(
                    entries,
                    key=lambda item: (
                        len(item["id"]), item["id"]
                    ) if pin_lane_iterator is not None else item["id"],
                )
            )
        else:
            entry_routes = joined_records()

        endpoint_rows: list[tuple[Any, ...]] = []
        lane_rows: list[tuple[Any, ...]] = []
        binding_rows: list[tuple[Any, ...]] = []
        binding_adapter_rows: list[tuple[Any, ...]] = []
        seen_binding_adapter_ports: set[tuple[str, str]] = set()
        binding_contract_records: dict[
            tuple[str, str], Mapping[str, Any]
        ] = {}
        binding_contract_lookup: dict[
            tuple[str, str, int, str], tuple[str, str]
        ] = {}

        def flush() -> None:
            if endpoint_rows:
                connection.executemany(
                    "INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    endpoint_rows,
                )
                endpoint_rows.clear()
            if lane_rows:
                connection.executemany(
                    "INSERT INTO lanes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    lane_rows,
                )
                lane_rows.clear()
            if binding_rows:
                connection.executemany(
                    "INSERT INTO binding_entries VALUES (?,?,?,?,?,?,?)",
                    binding_rows,
                )
                binding_rows.clear()
            if binding_adapter_rows:
                connection.executemany(
                    "INSERT INTO binding_adapter_ports VALUES (?,?,?,?,?,?,?,?,?)",
                    binding_adapter_rows,
                )
                binding_adapter_rows.clear()

        for entry, route in entry_routes:
            if typed_records is not None:
                entry_id = entry.entry_id
                demand = entry.demand
                net = entry.net
                link = entry.link
                source = entry.source
                sink = entry.sink
                slot = entry.slot
                arrival_slot = entry.arrival_slot
                logical_lane = entry.lane
                tdm_ratio = entry.tdm_ratio
                route_source = entry.route_source
            elif route is None:
                raise ValidationError(
                    f"schedule entry {entry['id']!r} has no route metadata"
                )
            else:
                entry_id = entry["id"]
                demand = entry["demand"]
                net = entry["net"]
                link = entry["link"]
                source = entry.get("from")
                sink = entry.get("to")
                slot = entry["slot"]
                arrival_slot = entry["arrival_slot"]
                logical_lane = entry["lane"]
                tdm_ratio = entry.get("tdm_ratio")
                route_source = route["source"]
            if source not in fpga_ids or sink not in fpga_ids:
                raise ValidationError("schedule entry references an unknown FPGA")
            if pin_lane_iterator is not None:
                pin_record = next(pin_lane_iterator, None)
                if (
                    pin_record is None
                    or pin_record[0] != entry_id
                    or pin_record[2] != logical_lane
                ):
                    raise ValidationError(
                        "pin plan must cover schedule entries exactly"
                    )
                lane = pin_record[1]
            else:
                lane = physical_lane_by_entry.get(entry_id, logical_lane)
            if pin_plan is not None and pin_lane_iterator is None:
                if entry_id not in physical_lane_by_entry:
                    raise ValidationError(
                        "pin plan must cover every schedule entry exactly"
                    )
                seen_pin_entries.add(entry_id)
            tx_id = _endpoint_id("tx", entry_id)
            rx_id = _endpoint_id("rx", entry_id)
            tx_signal = (
                f"net:{net}"
                if source == route_source
                else f"shadow:{demand}:{source}"
            )
            rx_signal = f"shadow:{demand}:{sink}"
            common = {
                "schedule_entry": entry_id,
                "demand": demand,
                "net": net,
                "link": link,
                "slot": slot,
                "arrival_slot": arrival_slot,
                "lane": lane,
                "logical_lane": logical_lane,
            }
            tx_exact: dict[str, Any] = {}
            rx_exact: dict[str, Any] = {}
            if exact_mode:
                tx_exact = {
                    "boundary_identity": f"static_exact:tx:{entry_id}",
                    "semantic_contract_sha256": exact_digest,
                    "source_role": (
                        "cut_source"
                        if source == route_source
                        else "route_tree_relay"
                    ),
                    "preserve": True,
                }
                rx_exact = {
                    "boundary_identity": f"static_exact:rx:{entry_id}",
                    "semantic_contract_sha256": exact_digest,
                    "preserve": True,
                }
            tx = {
                "id": tx_id,
                "kind": "tx",
                "fpga": source,
                "peer": sink,
                "signal": tx_signal,
                **common,
                **tx_exact,
            }
            rx = {
                "id": rx_id,
                "kind": "rx",
                "fpga": sink,
                "peer": source,
                "signal": rx_signal,
                **common,
                **rx_exact,
            }
            for endpoint in (tx, rx):
                endpoint_rows.append(
                    (
                        endpoint["id"], endpoint["fpga"], endpoint["kind"],
                        endpoint["peer"], endpoint["signal"], entry_id,
                        demand, net, link, slot, arrival_slot, lane,
                        logical_lane, _canonical(endpoint),
                    )
                )
            lane_record = {
                "id": f"lane_{entry_id}",
                "schedule_entry": entry_id,
                "demand": demand,
                "net": net,
                "link": link,
                "from": source,
                "to": sink,
                "slot": slot,
                "lane": lane,
                "logical_lane": logical_lane,
                "tx_endpoint": tx_id,
                "rx_endpoint": rx_id,
            }
            lane_rows.append(
                (
                    lane_record["id"], entry_id, demand, net,
                    link, source, sink, slot, lane,
                    logical_lane, tx_id, rx_id, _canonical(lane_record),
                )
            )
            if virtual_board is not None:
                wire = binding_wire_by_key.get((link, lane))
                if wire is None:
                    raise ValidationError(
                        "scheduled wire has no virtual board connection"
                    )
                direction = (source, sink)
                if (
                    binding_direction_by_wire.setdefault(wire["id"], direction)
                    != direction
                ):
                    raise ValidationError(
                        "one physical wire cannot change direction within "
                        "the schedule"
                    )
                pins = {pin["fpga"]: pin for pin in wire["endpoints"]}
                bound: dict[str, Any] = {}
                for role, endpoint in (("source", tx), ("sink", rx)):
                    kind = endpoint["kind"]
                    endpoint_fpga = endpoint["fpga"]
                    logical_port = (
                        f"{transport_endpoint_port(endpoint)}[{lane}]"
                    )
                    pair = pins.get(endpoint_fpga)
                    if pair is None:
                        raise ValidationError(
                            "virtual board wire omits a scheduled FPGA"
                        )
                    bound_end = {
                        key: pair[key] for key in ("fpga", "bank", "pair_id")
                    }
                    bound_end["logical_port"] = logical_port
                    bound_end["logical_port_width"] = binding_links[
                        link
                    ].transport_bits_per_cycle_per_direction
                    for polarity in ("p", "n"):
                        pin = pair[polarity]
                        port = (
                            f"emuflow_io_{kind}_{pin_port_name(link)}_"
                            f"wire{wire['wire']}_{polarity}"
                        )
                        identity = (endpoint_fpga, pin["package_pin"])
                        if binding_pin_ports.setdefault(identity, port) != port:
                            raise ValidationError(
                                "package pin is assigned to more than one RTL port"
                            )
                        if (
                            binding_port_pins.setdefault(
                                (endpoint_fpga, port), identity
                            )
                            != identity
                        ):
                            raise ValidationError(
                                "physical RTL port name collides after "
                                "identifier normalization"
                            )
                        bound_end[polarity] = {**pin, "port": port}
                    bound[role] = bound_end
                    adapter_identity = (endpoint_fpga, bound_end["p"]["port"])
                    if adapter_identity not in seen_binding_adapter_ports:
                        seen_binding_adapter_ports.add(adapter_identity)
                        binding_adapter_rows.append(
                            (
                                endpoint_fpga,
                                bound_end["p"]["port"],
                                bound_end["n"]["port"],
                                role,
                                logical_port,
                                bound_end["logical_port_width"],
                                bound_end["p"]["package_pin"],
                                bound_end["n"]["package_pin"],
                                virtual_board["assumptions"]["iostandard"],
                            )
                        )
                    logical_match = re.fullmatch(
                        r"([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]", logical_port
                    )
                    if logical_match is None:
                        raise ValidationError(
                            "virtual binding logical port is invalid"
                        )
                    direction_name = "tx" if role == "source" else "rx"
                    contract_record = {
                        "id": f"{direction_name}:{bound_end['p']['port']}",
                        "direction": direction_name,
                        "link": wire["link"],
                        "wire": wire["wire"],
                        "wire_id": wire["id"],
                        "bank": bound_end["bank"],
                        "pair_id": bound_end["pair_id"],
                        "iostandard": virtual_board["assumptions"][
                            "iostandard"
                        ],
                        "logical_port": logical_match.group(1),
                        "logical_bit": int(logical_match.group(2)),
                        "logical_port_width": bound_end[
                            "logical_port_width"
                        ],
                        "p": dict(bound_end["p"]),
                        "n": dict(bound_end["n"]),
                    }
                    previous_contract = binding_contract_records.setdefault(
                        adapter_identity, contract_record
                    )
                    if previous_contract != contract_record:
                        raise ValidationError(
                            "one physical differential pair has conflicting bindings"
                        )
                    lookup_key = (
                        endpoint_fpga,
                        link,
                        int(wire["wire"]),
                        role,
                    )
                    previous_identity = binding_contract_lookup.setdefault(
                        lookup_key, adapter_identity
                    )
                    if previous_identity != adapter_identity:
                        raise ValidationError(
                            "one virtual wire has conflicting physical ports"
                        )
                binding_record = {
                    "schedule_entry": entry_id,
                    "net": net,
                    "slot": slot,
                    "arrival_slot": arrival_slot,
                    "tdm_ratio": tdm_ratio,
                    "wire_id": wire["id"],
                    "wire": wire["wire"],
                    "link": wire["link"],
                    "bank_group": wire["bank_group"],
                    "cable": wire["cable"],
                    "iostandard": virtual_board["assumptions"]["iostandard"],
                    **bound,
                }
                binding_rows.append(
                    (
                        entry_id, link, slot, wire["wire"],
                        source, sink, _canonical(binding_record),
                    )
                )
            entry_count += 1
            if len(lane_rows) >= 4096:
                flush()
        flush()
        if pin_lane_iterator is not None and next(pin_lane_iterator, None) is not None:
            raise ValidationError("pin plan must cover schedule entries exactly")
        if (
            pin_plan is not None
            and pin_lane_iterator is None
            and set(physical_lane_by_entry) != seen_pin_entries
        ):
            raise ValidationError("pin plan must cover every schedule entry exactly")

        if virtual_board is not None:
            membership_counts = {
                identity: 0 for identity in binding_contract_records
            }
            membership_digests = {
                identity: hashlib.sha256()
                for identity in binding_contract_records
            }
            for row in connection.execute(
                "SELECT schedule_entry,link,wire,source_fpga,sink_fpga "
                "FROM binding_entries ORDER BY schedule_entry"
            ):
                for role, fpga_column in (
                    ("source", "source_fpga"),
                    ("sink", "sink_fpga"),
                ):
                    lookup_key = (
                        str(row[fpga_column]),
                        str(row["link"]),
                        int(row["wire"]),
                        role,
                    )
                    identity = binding_contract_lookup.get(lookup_key)
                    if identity is None:
                        raise ValidationError(
                            "virtual binding physical-port relation is incomplete"
                        )
                    membership_counts[identity] += 1
                    membership_digests[identity].update(
                        _canonical(str(row["schedule_entry"])).encode("utf-8")
                    )
                    membership_digests[identity].update(b"\n")
            connection.executemany(
                "INSERT INTO binding_physical_ports VALUES (?,?,?,?,?)",
                (
                    (
                        identity[0],
                        identity[1],
                        _canonical(record),
                        membership_counts[identity],
                        membership_digests[identity].hexdigest(),
                    )
                    for identity, record in sorted(
                        binding_contract_records.items()
                    )
                ),
            )

        if exact_mode:
            missing = connection.execute(
                "SELECT e.net FROM endpoints e "
                "LEFT JOIN assignment.cut_sinks c ON c.net_id=e.net "
                "WHERE c.net_id IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise ValidationError("exact schedule references an unknown cut node")

        for fpga in fpga_ids:
            for kind, endpoint_kind, predicate in (
                ("source", "tx", "AND substr(signal,1,4)='net:'"),
                ("shadow", "rx", ""),
            ):
                rows = connection.execute(
                    "SELECT DISTINCT signal FROM endpoints "
                    "WHERE fpga=? AND kind=? " + predicate + " ORDER BY signal",
                    (fpga, endpoint_kind),
                )
                connection.executemany(
                    "INSERT INTO signals(fpga,kind,seq,signal) VALUES (?,?,?,?)",
                    (
                        (fpga, kind, index, str(row[0]))
                        for index, row in enumerate(rows)
                    ),
                )

        links = {link.id: link for link in platform.links}
        for row in connection.execute(
            "SELECT fpga,link,peer,kind,lane FROM endpoints "
            "GROUP BY fpga,link,peer,kind,lane "
            "ORDER BY fpga,link,peer,kind,lane"
        ):
            board_link = links.get(row["link"])
            if board_link is None:
                raise ValidationError("transport endpoint references an unknown link")
            lane = int(row["lane"])
            if board_link.mode == "serial":
                required_fields = [
                    "package_pin_p", "package_pin_n", "connector",
                    "transceiver_site",
                ]
                physical_lane = lane // board_link.payload_bits_per_lane_per_cycle
                bit_within_physical_lane = (
                    lane % board_link.payload_bits_per_lane_per_cycle
                )
            else:
                required_fields = ["package_pin", "bank", "iostandard"]
                physical_lane = lane
                bit_within_physical_lane = 0
            record = {
                "id": f"{row['link']}:{row['fpga']}:{row['kind']}:{lane}",
                "link": row["link"],
                "peer": row["peer"],
                "direction": row["kind"],
                "logical_lane": lane,
                "physical_lane": physical_lane,
                "bit_within_physical_lane": bit_within_physical_lane,
                "placement_class": "virtual_link_io_region",
                "binding_status": "unbound",
                "required_hardware_binding_fields": required_fields,
            }
            connection.execute(
                "INSERT INTO anchors VALUES (?,?,?,?,?,?,?)",
                (
                    record["id"], row["fpga"], row["link"], row["peer"],
                    row["kind"], lane, _canonical(record),
                ),
            )
        connection.commit()

        lane_header = {
            "schema": LOGICAL_LANE_MAP_SCHEMA,
            "design": design_name,
            "platform": platform.name,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "binding_status": "logical_only",
            **(
                {
                    "cut_mode": "static-exact-combinational",
                    "semantic_contract_sha256": exact_digest,
                }
                if exact_mode else {}
            ),
        }
        lane_sections = {
            "entries": _record_section(
                connection, "SELECT record_json FROM lanes ORDER BY id", ()
            )
        }
        lane_canonical_sha256 = _canonical_document_digest(
            lane_header,
            {
                "entries": (
                    str(row[0])
                    for row in connection.execute(
                        "SELECT record_json FROM lanes ORDER BY id"
                    )
                )
            },
        )
        binding_header = None
        binding_sections = None
        binding_canonical_sha256 = None
        if virtual_board is not None:
            binding_header = {
                "schema": VIRTUAL_BINDING_LOGICAL_SCHEMA,
                "platform": platform.name,
                "design": design_name,
                "qualification": "virtual_board_real_device_pins",
                "status": "pass",
                "integration_status": (
                    "differential_data_constraints_and_adapter_generated"
                ),
                "physical_backend_status": "phase7_consumption_required",
                "board_services_status": "clock_reset_and_control_pins_not_bound",
                "io_adapter_status": "phase7_top_level_integration_required",
                "board_sha256": hashlib.sha256(
                    _canonical(virtual_board).encode("utf-8")
                ).hexdigest(),
                "schedule_sha256": canonical_tdm_schedule_sha256(schedule),
                "lane_map_sha256": lane_canonical_sha256,
                "metrics": {
                    "scheduled_hops": entry_count,
                    "used_differential_pairs": len(binding_direction_by_wire),
                    "bound_data_ports": len(binding_pin_ports),
                    "total_differential_pairs": len(virtual_board["wires"]),
                },
            }
            binding_sections = {
                "entries": _record_section(
                    connection,
                    "SELECT record_json FROM binding_entries "
                    "ORDER BY schedule_entry",
                    (),
                )
            }
            binding_canonical_sha256 = _canonical_document_digest(
                binding_header,
                {
                    "entries": (
                        str(row[0])
                        for row in connection.execute(
                            "SELECT record_json FROM binding_entries "
                            "ORDER BY schedule_entry"
                        )
                    )
                },
            )
        transport_headers: dict[str, dict[str, Any]] = {}
        transport_sections: dict[str, dict[str, dict[str, Any]]] = {}
        anchor_headers: dict[str, dict[str, Any]] = {}
        anchor_sections: dict[str, dict[str, dict[str, Any]]] = {}
        clock_order = sorted(item["id"] for item in clocks) or ["clk"]
        parts_by_id = {fpga.id: fpga for fpga in platform.fpgas}
        for fpga in fpga_ids:
            transport_headers[fpga] = {
                "schema": TRANSPORT_ENDPOINTS_SCHEMA,
                "design": design_name,
                "platform": platform.name,
                "fpga": fpga,
                "frame_slots": schedule["metrics"]["frame_slots"],
                "virtual_clock_order": clock_order,
                **(
                    {
                        "cut_mode": "static-exact-combinational",
                        "semantic_contract_sha256": exact_digest,
                        **(
                            {"semantic_contract_storage": exact_storage}
                            if isinstance(exact_storage, Mapping) else {}
                        ),
                        "shadow_reset_value": 0,
                        "startup_policy": (
                            "first-post-reset-frame-current-arrivals"
                        ),
                    }
                    if exact_mode else {}
                ),
            }
            transport_sections[fpga] = {
                "source_signals": _signal_section(connection, fpga, "tx"),
                "shadow_signals": _signal_section(connection, fpga, "rx"),
                "endpoints": _record_section(
                    connection,
                    "SELECT record_json FROM endpoints WHERE fpga=? ORDER BY id",
                    (fpga,),
                ),
            }
            required_fields = sorted(
                {
                    field
                    for record in connection.execute(
                        "SELECT record_json FROM anchors WHERE fpga=? ORDER BY id",
                        (fpga,),
                    )
                    for field in json.loads(record[0])[
                        "required_hardware_binding_fields"
                    ]
                }
            )
            anchor_headers[fpga] = {
                "schema": VIRTUAL_IO_ANCHORS_SCHEMA,
                "platform": platform.name,
                "fpga": fpga,
                "part": parts_by_id[fpga].part,
                "required_hardware_binding_fields": required_fields,
            }
            anchor_sections[fpga] = {
                "anchors": _anchor_relation_section(connection, fpga)
            }
        connection.close()
        publish_sqlite_store(
            temporary, store_path, "transport-store-v1", sources=source_bindings
        )
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "transport storage contains duplicate identities"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise

    store_owner = VerifiedStoreFile(store_path, "transport-store-v1")
    lane_manifest = _manifest(
        schema=LANE_MAP_STORAGE_SCHEMA,
        logical_schema=LOGICAL_LANE_MAP_SCHEMA,
        artifact=TRANSPORT_STORE_NAME,
        header=lane_header,
        sections=lane_sections,
        store_owner=store_owner,
        canonical_sha256=lane_canonical_sha256,
    )
    write_json(output_dir / "lane_map.json", lane_manifest, compact=True)
    binding_manifest = None
    if binding_header is not None and binding_sections is not None:
        binding_manifest = _manifest(
            schema=VIRTUAL_BINDING_STORAGE_SCHEMA,
            logical_schema=VIRTUAL_BINDING_LOGICAL_SCHEMA,
            artifact=TRANSPORT_STORE_NAME,
            header=binding_header,
            sections=binding_sections,
            store_owner=store_owner,
            canonical_sha256=binding_canonical_sha256,
        )
        write_json(
            output_dir / "virtual_board_binding.json",
            binding_manifest,
            compact=True,
        )
    for fpga in fpga_ids:
        root = output_dir / fpga
        write_json(
            root / "transport.json",
            _manifest(
                schema=TRANSPORT_STORAGE_SCHEMA,
                logical_schema=TRANSPORT_ENDPOINTS_SCHEMA,
                artifact=f"../{TRANSPORT_STORE_NAME}",
                header=transport_headers[fpga],
                sections=transport_sections[fpga],
                store_owner=store_owner,
            ),
            compact=True,
        )
        write_json(
            root / "virtual_anchors.json",
            _manifest(
                schema=ANCHOR_STORAGE_SCHEMA,
                logical_schema=VIRTUAL_IO_ANCHORS_SCHEMA,
                artifact=f"../{TRANSPORT_STORE_NAME}",
                header=anchor_headers[fpga],
                sections=anchor_sections[fpga],
                store_owner=store_owner,
            ),
            compact=True,
        )

    transports = {
        fpga: TransportView(
            output_dir / fpga / "transport.json", store_owner=store_owner
        )
        for fpga in fpga_ids
    }
    anchors = {
        fpga: AnchorView(
            output_dir / fpga / "virtual_anchors.json", store_owner=store_owner
        )
        for fpga in fpga_ids
    }
    return {
        "lane_map": LaneMapView(output_dir / "lane_map.json", store_owner=store_owner),
        "transports": transports,
        "anchors": anchors,
        "binding": (
            VirtualBindingView(
                output_dir / "virtual_board_binding.json", store_owner=store_owner
            )
            if binding_manifest is not None
            else None
        ),
        "summary": {
            "schema": TRANSPORT_STORAGE_SCHEMA,
            "artifact": TRANSPORT_STORE_NAME,
            "bytes": store_owner.bytes,
            "sha256": store_owner.sha256,
            "scheduled_hops": entry_count,
            "transport_endpoints": 2 * entry_count,
            "lane_entries": entry_count,
            "virtual_binding_entries": (
                entry_count if virtual_board is not None else 0
            ),
            "anchors": sum(len(value["anchors"]) for value in anchors.values()),
            "dataflow": "indexed-lazy-sqlite-v1",
        },
    }


def write_transport_systemverilog(
    path: Path,
    transport: Mapping[str, Any],
    platform: Platform,
) -> None:
    """Stream production transport RTL from indexed endpoint cursors."""

    rtl_rows = getattr(transport, "iter_rtl_endpoints", None)
    endpoint_groups = getattr(transport, "endpoint_groups", None)
    if rtl_rows is None or endpoint_groups is None:
        from emuflow.netlist import transport_to_systemverilog

        value = transport_to_systemverilog(transport, platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    fpga = transport["fpga"]
    links = {link.id: link for link in platform.links}
    groups = endpoint_groups()
    virtual_clocks = transport.get("virtual_clock_order", ["clk"])
    if (
        not isinstance(virtual_clocks, list)
        or not virtual_clocks
        or any(not isinstance(clock, str) or not clock for clock in virtual_clocks)
        or len(set(virtual_clocks)) != len(virtual_clocks)
    ):
        raise ValidationError("transport virtual clock order is invalid")
    ports = [
        "  input  logic fabric_clk",
        "  input  logic reset",
        "  input  logic links_ready",
        "  input  logic [SOURCE_COUNT-1:0] source_values",
        "  output logic [SHADOW_COUNT-1:0] shadow_values",
        f"  output logic [{len(virtual_clocks) - 1}:0] virtual_clock_enable",
        "  output logic [SLOT_BITS-1:0] slot_debug",
    ]
    bus_name: dict[tuple[str, str, str], str] = {}
    for link_id, peer, kinds in groups:
        width = links[link_id].transport_bits_per_cycle_per_direction
        for kind, direction in (("rx", "input  "), ("tx", "output ")):
            if kind not in kinds:
                continue
            name = transport_endpoint_port(
                {"kind": kind, "link": link_id, "peer": peer}
            )
            bus_name[(link_id, peer, kind)] = name
            ports.append(
                f"  {direction}logic [{width - 1}:0] {name}"
            )

    source_count = max(1, len(transport["source_signals"]))
    shadow_count = max(1, len(transport["shadow_signals"]))
    frame_slots = int(transport["frame_slots"])
    slot_bits = max(1, (frame_slots - 1).bit_length())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

    def line(stream: Any, value: str = "") -> None:
        stream.write(value)
        stream.write("\n")

    try:
        with temporary.open("w", encoding="utf-8") as stream:
            line(stream, f"module emuflow_transport_{_sv_name(fpga)} #(")
            line(stream, f"  parameter integer FRAME_SLOTS = {frame_slots},")
            line(stream, f"  parameter integer SLOT_BITS = {slot_bits},")
            line(stream, f"  parameter integer SOURCE_COUNT = {source_count},")
            line(stream, f"  parameter integer SHADOW_COUNT = {shadow_count}")
            line(stream, ") (")
            line(stream, ",\n".join(ports))
            line(stream, ");")
            line(stream)
            line(stream, "  logic [SLOT_BITS-1:0] slot;")
            if transport.get("cut_mode") == "static-exact-combinational":
                line(stream, '  (* KEEP = "yes", DONT_TOUCH = "yes" *)')
            line(stream, "  logic [SHADOW_COUNT-1:0] shadow_regs;")
            line(stream, "  emuflow_virtual_runtime_controller #(")
            line(stream, "    .FRAME_SLOTS(FRAME_SLOTS),")
            line(stream, "    .SLOT_BITS(SLOT_BITS)")
            line(stream, "  ) runtime_controller (")
            line(stream, "    .fabric_clk(fabric_clk),")
            line(stream, "    .reset(reset),")
            line(stream, "    .links_ready(links_ready),")
            line(stream, "    .virtual_clock_enable(virtual_clock_enable),")
            line(stream, "    .event_index_debug(),")
            line(stream, "    .slot(slot)")
            line(stream, "  );")
            line(stream, "  assign slot_debug = slot;")
            line(stream, "  assign shadow_values = shadow_regs;")
            line(stream)

            tx_groups = [
                (link_id, peer)
                for link_id, peer, kinds in groups
                if "tx" in kinds
            ]
            if tx_groups:
                line(stream, "  always @* begin")
                for link_id, peer in tx_groups:
                    line(
                        stream,
                        f"    {bus_name[(link_id, peer, 'tx')]} = '0;",
                    )
                line(stream, "    case (slot)")
                active_slot = None
                for slot, endpoint, signal_index in rtl_rows("tx"):
                    if slot != active_slot:
                        if active_slot is not None:
                            line(stream, "      end")
                        active_slot = slot
                        line(stream, f"      {slot}: begin")
                    expression = (
                        f"source_values[{signal_index}]"
                        if endpoint["signal"].startswith("net:")
                        else f"shadow_regs[{signal_index}]"
                    )
                    name = bus_name[
                        (endpoint["link"], endpoint["peer"], "tx")
                    ]
                    line(
                        stream,
                        f"        {name}[{endpoint['lane']}] = {expression};",
                    )
                if active_slot is not None:
                    line(stream, "      end")
                line(stream, "      default: begin end")
                line(stream, "    endcase")
                line(stream, "  end")
                line(stream)

            line(stream, "  always_ff @(posedge fabric_clk) begin")
            line(stream, "    if (reset) begin")
            line(stream, "      shadow_regs <= '0;")
            line(stream, "    end else begin")
            line(stream, "      case (slot)")
            active_slot = None
            for slot, endpoint, signal_index in rtl_rows("rx"):
                if slot != active_slot:
                    if active_slot is not None:
                        line(stream, "        end")
                    active_slot = slot
                    line(stream, f"        {slot}: begin")
                name = bus_name[(endpoint["link"], endpoint["peer"], "rx")]
                line(
                    stream,
                    f"          shadow_regs[{signal_index}] "
                    f"<= {name}[{endpoint['lane']}];",
                )
            if active_slot is not None:
                line(stream, "        end")
            line(stream, "        default: begin end")
            line(stream, "      endcase")
            line(stream, "    end")
            line(stream, "  end")
            line(stream, "endmodule")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
