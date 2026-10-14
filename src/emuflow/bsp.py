from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .netlist import VIRTUAL_IO_ANCHORS_SCHEMA
from .phase6 import PHASE6_REPORT_SCHEMA
from .platform import BoardLink, Platform
from .release import RELEASE_MANIFEST_SCHEMA


BSP_REQUIREMENTS_SCHEMA = "emuflow.bsp-requirements/v1"
PHASE8A_REPORT_SCHEMA = "emuflow.phase8a-report/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_directions(link: BoardLink) -> Sequence[Tuple[str, str]]:
    first, second = link.endpoints
    if link.direction == "full_duplex":
        return ((first, second), (second, first))
    if link.direction == "unidirectional":
        return ((first, second),)
    return ()


def _physical_data_lanes(platform: Platform) -> List[Dict[str, Any]]:
    if any(link.bank_count is not None for link in platform.links):
        raise ValidationError(
            "a shared-bank platform requires a schedule-aware physical BSP; "
            "independent per-direction package-pin requirements are unsupported"
        )
    records: List[Dict[str, Any]] = []
    for link in platform.links:
        if link.direction == "half_duplex":
            for fpga, peer in (
                (link.endpoints[0], link.endpoints[1]),
                (link.endpoints[1], link.endpoints[0]),
            ):
                for lane in range(link.data_lanes_per_direction):
                    if link.mode == "serial":
                        required_fields = [
                            "tx_package_pin_p",
                            "tx_package_pin_n",
                            "rx_package_pin_p",
                            "rx_package_pin_n",
                            "connector",
                            "transceiver_site",
                        ]
                        lane_kind = "serial_transceiver"
                    else:
                        required_fields = [
                            "package_pin",
                            "bank",
                            "iostandard",
                        ]
                        lane_kind = "parallel_data"
                    records.append(
                        _physical_lane_record(
                            link,
                            fpga,
                            peer,
                            "inout",
                            lane,
                            required_fields,
                            lane_kind,
                        )
                    )
            continue
        for source, sink in _link_directions(link):
            for lane in range(link.data_lanes_per_direction):
                for fpga, peer, direction in (
                    (source, sink, "tx"),
                    (sink, source, "rx"),
                ):
                    if link.mode == "serial":
                        required_fields = [
                            "package_pin_p",
                            "package_pin_n",
                            "connector",
                            "transceiver_site",
                        ]
                        lane_kind = "serial_transceiver"
                    else:
                        required_fields = [
                            "package_pin",
                            "bank",
                            "iostandard",
                        ]
                        lane_kind = "parallel_data"
                    records.append(
                        _physical_lane_record(
                            link,
                            fpga,
                            peer,
                            direction,
                            lane,
                            required_fields,
                            lane_kind,
                        )
                    )
    return sorted(records, key=lambda item: item["id"])


def _physical_lane_record(
    link: BoardLink,
    fpga: str,
    peer: str,
    direction: str,
    lane: int,
    required_fields: Sequence[str],
    lane_kind: str,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "id": f"{link.id}:{fpga}:{direction}:{lane}",
        "link": link.id,
        "fpga": fpga,
        "peer": peer,
        "direction": direction,
        "logical_lane": lane,
        "physical_lane": lane,
        "lane_kind": lane_kind,
        "payload_bits_per_lane_per_cycle": (
            link.payload_bits_per_lane_per_cycle
        ),
        "required_binding_fields": list(required_fields),
    }
    if link.mode != "serial":
        record["binding_status"] = "unbound"
        return record
    endpoint = link.endpoint_binding(fpga)
    if endpoint is None:
        record["binding_status"] = "unbound"
        record["unresolved_binding_fields"] = list(required_fields)
        return record
    serial_lane = endpoint.lanes[lane]
    if direction == "rx":
        package_pin_p = serial_lane.rx_package_pin_p
        package_pin_n = serial_lane.rx_package_pin_n
    elif direction == "tx":
        package_pin_p = serial_lane.tx_package_pin_p
        package_pin_n = serial_lane.tx_package_pin_n
    else:
        record["tx_package_pin_p"] = serial_lane.tx_package_pin_p
        record["tx_package_pin_n"] = serial_lane.tx_package_pin_n
        record["rx_package_pin_p"] = serial_lane.rx_package_pin_p
        record["rx_package_pin_n"] = serial_lane.rx_package_pin_n
        package_pin_p = None
        package_pin_n = None
        resolved_pin_fields = [
            "tx_package_pin_p",
            "tx_package_pin_n",
            "rx_package_pin_p",
            "rx_package_pin_n",
        ]
    if direction in {"tx", "rx"}:
        resolved_pin_fields = ["package_pin_p", "package_pin_n"]
    record.update(
        {
            "binding_status": "partially_bound",
            "connector": endpoint.connector,
            "mgt_group": endpoint.mgt,
            "resolved_binding_fields": [*resolved_pin_fields, "connector"],
            "unresolved_binding_fields": ["transceiver_site"],
        }
    )
    if package_pin_p is not None:
        record["package_pin_p"] = package_pin_p
        record["package_pin_n"] = package_pin_n
    return record


def _link_channels(platform: Platform) -> List[Dict[str, Any]]:
    records = []
    for link in platform.links:
        directions = _link_directions(link)
        if link.direction == "half_duplex":
            directions = (link.endpoints,)
        for source, sink in directions:
            channel = f"{source}-to-{sink}"
            source_binding = link.endpoint_binding(source)
            sink_binding = link.endpoint_binding(sink)
            if link.mode == "serial":
                required_fields = [
                    "connector",
                    "transceiver_profile",
                    "line_rate_gbps_per_lane",
                    "encoding",
                    "training_protocol",
                ]
            else:
                required_fields = [
                    "connector",
                    "forwarded_clock_binding",
                    "input_delay_ns",
                    "output_delay_ns",
                    "electrical_standard",
                    "training_protocol",
                ]
            record = {
                    "id": f"{link.id}:{channel}",
                    "link": link.id,
                    "source": source,
                    "sink": sink,
                    "mode": link.mode,
                    "data_lanes": link.data_lanes_per_direction,
                    "payload_bits_per_lane_per_cycle": (
                        link.payload_bits_per_lane_per_cycle
                    ),
                    "transport_bits_per_cycle": (
                        link.transport_bits_per_cycle_per_direction
                    ),
                    "fabric_clock_mhz": link.fabric_clock_mhz,
                    "required_binding_fields": required_fields,
            }
            if (
                link.mode == "serial"
                and source_binding is not None
                and sink_binding is not None
            ):
                record.update(
                    {
                        "binding_status": "partially_bound",
                        "source_connector": source_binding.connector,
                        "sink_connector": sink_binding.connector,
                        "source_mgt_group": source_binding.mgt,
                        "sink_mgt_group": sink_binding.mgt,
                        "configured_line_rate_gbps_per_lane": (
                            link.fabric_clock_mhz
                            * link.payload_bits_per_lane_per_cycle
                            / 1000.0
                        ),
                        "maximum_line_rate_gbps_per_lane": (
                            link.max_line_rate_gbps_per_lane
                        ),
                        "resolved_binding_fields": [
                            "connector",
                            "line_rate_gbps_per_lane",
                        ],
                        "unresolved_binding_fields": [
                            "transceiver_profile",
                            "encoding",
                            "training_protocol",
                        ],
                    }
                )
            else:
                record["binding_status"] = "unbound"
            records.append(record)
    return sorted(records, key=lambda item: item["id"])


def _validate_release(
    release: Mapping[str, Any],
    phase6: Mapping[str, Any],
    platform: Platform,
) -> str:
    if release.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise ValidationError("release manifest has the wrong schema")
    if release.get("status") != "pass":
        raise ValidationError("release manifest did not pass")
    if release.get("release_scope") != "board-independent-g0-g9":
        raise ValidationError("release manifest does not cover G0-G9")
    gates = release.get("gates")
    if (
        not isinstance(gates, dict)
        or set(gates) != {f"G{index}" for index in range(10)}
        or any(
            not isinstance(gate, dict) or gate.get("status") != "pass"
            for gate in gates.values()
        )
    ):
        raise ValidationError("release manifest G0-G9 evidence is incomplete")
    if release.get("platform") != platform.name:
        raise ValidationError("release platform does not match BoardDB")
    release_binding = release.get("board_binding")
    if (
        not isinstance(release_binding, dict)
        or release_binding.get("status") != "virtual"
    ):
        raise ValidationError(
            "Phase 8A requirements generation expects a virtual release"
        )
    source_commit = release.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
    ):
        raise ValidationError("release source commit is not an exact SHA-1")

    if phase6.get("schema") != PHASE6_REPORT_SCHEMA:
        raise ValidationError("Phase 6 report has the wrong schema")
    if phase6.get("status") != "pass":
        raise ValidationError("Phase 6 report did not pass")
    if (
        phase6.get("design") != release.get("design")
        or phase6.get("platform") != platform.name
    ):
        raise ValidationError("Phase 6 report does not match release")
    phase6_binding = phase6.get("board_binding")
    if (
        not isinstance(phase6_binding, dict)
        or phase6_binding.get("status") != "virtual"
    ):
        raise ValidationError("Phase 6 report incorrectly claims a hardware BSP")
    return source_commit


def _validate_anchors(
    anchor_documents: Mapping[str, Mapping[str, Any]],
    phase6: Mapping[str, Any],
    platform: Platform,
    physical_lanes: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    fpga_parts = {fpga.id: fpga.part for fpga in platform.fpgas}
    if set(anchor_documents) != set(fpga_parts):
        raise ValidationError("virtual anchors must cover every FPGA exactly once")
    lanes_by_key = {
        (
            item["link"],
            item["fpga"],
            item["direction"],
            item["physical_lane"],
        ): item
        for item in physical_lanes
    }
    half_duplex_links = {
        link.id for link in platform.links if link.direction == "half_duplex"
    }
    links_by_id = {link.id: link for link in platform.links}
    records: List[Dict[str, Any]] = []
    ids = set()
    for fpga_id in sorted(fpga_parts):
        document = anchor_documents[fpga_id]
        if document.get("schema") != VIRTUAL_IO_ANCHORS_SCHEMA:
            raise ValidationError(f"{fpga_id} virtual anchors have wrong schema")
        if (
            document.get("platform") != platform.name
            or document.get("fpga") != fpga_id
            or document.get("part") != fpga_parts[fpga_id]
        ):
            raise ValidationError(
                f"{fpga_id} virtual anchors do not match BoardDB"
            )
        required_fields = document.get("required_hardware_binding_fields")
        if not isinstance(required_fields, list) or any(
            not isinstance(field, str) or not field for field in required_fields
        ):
            raise ValidationError(
                f"{fpga_id} virtual anchors omit hardware binding fields"
            )
        anchors = document.get("anchors")
        if not isinstance(anchors, list):
            raise ValidationError(f"{fpga_id} anchors must be an array")
        for index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                raise ValidationError(
                    f"{fpga_id} anchor {index} must be an object"
                )
            anchor_id = anchor.get("id")
            if not isinstance(anchor_id, str) or not anchor_id:
                raise ValidationError(f"{fpga_id} anchor {index} has no id")
            if anchor_id in ids:
                raise ValidationError(f"duplicate virtual anchor {anchor_id!r}")
            ids.add(anchor_id)
            if anchor.get("binding_status") != "unbound":
                raise ValidationError(
                    f"virtual anchor {anchor_id!r} is not explicitly unbound"
                )
            link_id = anchor.get("link")
            board_link = links_by_id.get(link_id)
            logical_lane = anchor.get("logical_lane")
            if (
                board_link is None
                or isinstance(logical_lane, bool)
                or not isinstance(logical_lane, int)
                or logical_lane < 0
                or logical_lane
                >= board_link.transport_bits_per_cycle_per_direction
            ):
                raise ValidationError(
                    f"virtual anchor {anchor_id!r} is not a legal BoardDB lane"
                )
            physical_lane = (
                logical_lane // board_link.payload_bits_per_lane_per_cycle
            )
            bit_within_physical_lane = (
                logical_lane % board_link.payload_bits_per_lane_per_cycle
            )
            key = (
                link_id,
                fpga_id,
                anchor.get("direction"),
                physical_lane,
            )
            physical = lanes_by_key.get(key)
            if (
                physical is None
                and anchor.get("link") in half_duplex_links
                and anchor.get("direction") in {"tx", "rx"}
            ):
                physical = lanes_by_key.get(
                    (
                        link_id,
                        fpga_id,
                        "inout",
                        physical_lane,
                    )
                )
            if physical is None or anchor.get("peer") != physical["peer"]:
                raise ValidationError(
                    f"virtual anchor {anchor_id!r} is not a legal BoardDB lane"
                )
            expected_fields = sorted(physical["required_binding_fields"])
            anchor_fields = anchor.get(
                "required_hardware_binding_fields", expected_fields
            )
            if (
                not isinstance(anchor_fields, list)
                or any(
                    not isinstance(field, str) or not field
                    for field in anchor_fields
                )
                or sorted(anchor_fields) != expected_fields
                or not set(expected_fields).issubset(required_fields)
            ):
                raise ValidationError(
                    f"virtual anchor {anchor_id!r} has invalid binding fields"
                )
            physical_projection = (
                anchor.get("physical_lane", physical_lane),
                anchor.get(
                    "bit_within_physical_lane", bit_within_physical_lane
                ),
            )
            if physical_projection != (
                physical_lane,
                bit_within_physical_lane,
            ):
                raise ValidationError(
                    f"virtual anchor {anchor_id!r} has invalid serial lane projection"
                )
            records.append(
                {
                    "id": anchor_id,
                    "fpga": fpga_id,
                    "link": anchor["link"],
                    "peer": anchor["peer"],
                    "direction": anchor["direction"],
                    "logical_lane": logical_lane,
                    "physical_lane": physical_lane,
                    "bit_within_physical_lane": bit_within_physical_lane,
                    "required_binding_fields": expected_fields,
                }
            )
    validation = phase6.get("validation")
    if not isinstance(validation, dict):
        raise ValidationError("Phase 6 report has no validation record")
    expected = validation.get("virtual_anchors")
    unbound = validation.get("unbound_package_pins")
    if len(records) != expected or len(records) != unbound:
        raise ValidationError(
            "virtual anchor count does not match Phase 6 package-pin boundary"
        )
    return sorted(records, key=lambda item: item["id"])


def build_bsp_requirements(
    release: Mapping[str, Any],
    release_manifest_sha256: str,
    phase6: Mapping[str, Any],
    platform: Platform,
    anchor_documents: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    source_commit = _validate_release(release, phase6, platform)
    if re.fullmatch(r"[0-9a-f]{64}", release_manifest_sha256) is None:
        raise ValidationError("release manifest SHA-256 is invalid")
    physical_lanes = _physical_data_lanes(platform)
    logical_anchors = _validate_anchors(
        anchor_documents, phase6, platform, physical_lanes
    )
    release_artifacts = release.get("artifacts")
    if not isinstance(release_artifacts, list):
        raise ValidationError("release artifact inventory is missing")
    artifact_labels = {
        item.get("label")
        for item in release_artifacts
        if isinstance(item, dict)
    }
    bitstreams = []
    for fpga in sorted(platform.fpgas, key=lambda item: item.id):
        routed_label = f"{fpga.id}.routed_dcp"
        if routed_label not in artifact_labels:
            raise ValidationError(
                f"release is missing routed checkpoint {routed_label!r}"
            )
        bitstreams.append(
            {
                "fpga": fpga.id,
                "part": fpga.part,
                "routed_checkpoint": routed_label,
                "required_binding_fields": [
                    "bitstream_path",
                    "bitstream_sha256",
                    "vivado_version",
                ],
            }
        )
    fabric_clocks = []
    for fpga in sorted(platform.fpgas, key=lambda item: item.id):
        frequencies = [
            link.fabric_clock_mhz
            for link in platform.links
            if fpga.id in link.endpoints
        ]
        if not frequencies:
            raise ValidationError(
                f"{fpga.id} has no BoardDB link for a fabric-clock contract"
            )
        fabric_clocks.append(
            {
                "id": f"{fpga.id}:fabric_clock",
                "fpga": fpga.id,
                "frequency_mhz": max(frequencies),
                "required_binding_fields": [
                    "package_pin",
                    "iostandard",
                    "clock_buffer",
                    "clock_constraint",
                ],
            }
        )
    link_channels = _link_channels(platform)
    pending_checks = [
        {
            "gate": "G10a",
            "name": "bsp-electrical-and-pin-binding",
            "status": "pending_hardware_bsp",
        },
        {
            "gate": "G10b",
            "name": "board-io-timing-and-drc",
            "status": "pending_hardware_bsp",
        },
        {
            "gate": "G10c",
            "name": "bitstream-generation-and-verification",
            "status": "pending_hardware_bsp",
        },
        {
            "gate": "G10d",
            "name": "link-prbs-training-and-deskew",
            "status": "pending_hardware",
        },
        {
            "gate": "G10e",
            "name": "golden-workload-execution",
            "status": "pending_hardware",
        },
    ]
    return {
        "schema": BSP_REQUIREMENTS_SCHEMA,
        "status": "awaiting_hardware_bsp",
        "design": release["design"],
        "platform": platform.name,
        "release": {
            "source_commit": source_commit,
            "manifest_sha256": release_manifest_sha256,
            "gates_closed": [f"G{index}" for index in range(10)],
        },
        "target": {
            "required_boarddb_kind": "hardware",
            "fpgas": [
                {"id": fpga.id, "part": fpga.part}
                for fpga in sorted(platform.fpgas, key=lambda item: item.id)
            ],
            "links": [
                {
                    "id": link.id,
                    "endpoints": list(link.endpoints),
                    "direction": link.direction,
                    "mode": link.mode,
                    "data_lanes_per_direction": (
                        link.data_lanes_per_direction
                    ),
                    "fabric_clock_mhz": link.fabric_clock_mhz,
                    "latency_cycles": link.latency_cycles,
                    "payload_bits_per_lane_per_cycle": (
                        link.payload_bits_per_lane_per_cycle
                    ),
                    "transport_bits_per_cycle_per_direction": (
                        link.transport_bits_per_cycle_per_direction
                    ),
                    "max_line_rate_gbps_per_lane": (
                        link.max_line_rate_gbps_per_lane
                    ),
                }
                for link in sorted(platform.links, key=lambda item: item.id)
            ],
        },
        "requirements": {
            "logical_anchor_bindings": logical_anchors,
            "physical_data_lane_bindings": physical_lanes,
            "fabric_clock_bindings": fabric_clocks,
            "link_channel_bindings": link_channels,
            "bitstreams": bitstreams,
            "board_shell": {
                "required_binding_fields": [
                    "board_name",
                    "board_revision",
                    "host_interface",
                    "reset_binding",
                    "programming_interface",
                ]
            },
            "bringup_checks": pending_checks,
        },
        "metrics": {
            "fpgas": len(platform.fpgas),
            "links": len(platform.links),
            "logical_anchors": len(logical_anchors),
            "physical_data_lane_endpoints": len(physical_lanes),
            "fabric_clock_bindings": len(fabric_clocks),
            "link_channel_bindings": len(link_channels),
            "bitstreams": len(bitstreams),
            "pending_g10_checks": len(pending_checks),
        },
    }


def run_phase8a(
    release_manifest_path: Path,
    phase6_report_path: Path,
    platform_path: Path,
    anchor_paths: Mapping[str, Path],
    output_dir: Path,
) -> Dict[str, Any]:
    release = read_json(release_manifest_path)
    phase6 = read_json(phase6_report_path)
    platform = Platform.load(platform_path)
    anchors = {
        fpga: read_json(path) for fpga, path in anchor_paths.items()
    }
    requirements = build_bsp_requirements(
        release,
        _sha256(release_manifest_path),
        phase6,
        platform,
        anchors,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "bsp_requirements.json", requirements)
    if read_json(output_dir / "bsp_requirements.json") != build_bsp_requirements(
        release,
        _sha256(release_manifest_path),
        phase6,
        platform,
        anchors,
    ):
        raise ValidationError(
            "written BSP requirements do not match independent reconstruction"
        )
    report = {
        "schema": PHASE8A_REPORT_SCHEMA,
        "phase": "8A",
        "increment": "hardware-bsp-readiness-contract",
        "status": "pass",
        "design": requirements["design"],
        "platform": requirements["platform"],
        "board_binding_status": requirements["status"],
        "g10_status": "not_run",
        "validation": dict(requirements["metrics"]),
        "artifacts": {
            "bsp_requirements": "bsp_requirements.json",
            "report": "phase8a_report.json",
        },
    }
    write_json(output_dir / "phase8a_report.json", report)
    return report
