from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .io import read_json
from .resources import RESOURCE_FIELDS


BOARDDB_SCHEMA = "emuflow.boarddb/v1"
VALID_PLATFORM_KINDS = {"virtual", "hardware"}
VALID_DIRECTIONS = {"full_duplex", "half_duplex", "unidirectional"}
VALID_LINK_MODES = {"source_synchronous", "parallel", "serial", "abstract"}
VALID_CAPACITY_SHARING = {"per_direction", "shared_bidirectional"}
VALID_CLOCK_FREQUENCY_QUALIFICATIONS = {
    "fixed",
    "documented_default",
    "configured",
}
VALID_BOARD_BINDING_STATUS = {
    "logical_source_without_package_pins",
    "package_bound",
}
VALID_RESET_POLARITIES = {"active_low", "active_high"}


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected an object")
    return value


def _require_nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


@dataclass(frozen=True)
class FpgaNode:
    id: str
    part: str
    utilization_limit: float
    capacity: Dict[str, int]

    @property
    def effective_capacity(self) -> Dict[str, int]:
        return {
            resource: math.floor(count * self.utilization_limit)
            for resource, count in self.capacity.items()
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "part": self.part,
            "utilization_limit": self.utilization_limit,
            "capacity": dict(sorted(self.capacity.items())),
            "effective_capacity": dict(sorted(self.effective_capacity.items())),
        }


@dataclass(frozen=True)
class SerialLaneBinding:
    lane: int
    tx_package_pin_p: str
    tx_package_pin_n: str
    rx_package_pin_p: str
    rx_package_pin_n: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "tx_package_pins": {
                "p": self.tx_package_pin_p,
                "n": self.tx_package_pin_n,
            },
            "rx_package_pins": {
                "p": self.rx_package_pin_p,
                "n": self.rx_package_pin_n,
            },
        }


@dataclass(frozen=True)
class LinkEndpointBinding:
    fpga: str
    connector: str
    mgt: str
    lanes: Tuple[SerialLaneBinding, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fpga": self.fpga,
            "connector": self.connector,
            "mgt": self.mgt,
            "lanes": [lane.to_dict() for lane in self.lanes],
        }


@dataclass(frozen=True)
class BoardClockContract:
    id: str
    signal: str
    kind: str
    frequency_mhz: float
    frequency_qualification: str
    count: int
    destination: str
    binding_status: str
    qualification: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signal": self.signal,
            "kind": self.kind,
            "frequency_mhz": self.frequency_mhz,
            "frequency_qualification": self.frequency_qualification,
            "count": self.count,
            "destination": self.destination,
            "binding_status": self.binding_status,
            "qualification": self.qualification,
        }


@dataclass(frozen=True)
class BoardResetContract:
    id: str
    signal: str
    polarity: str
    purpose: str
    binding_status: str
    qualification: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signal": self.signal,
            "polarity": self.polarity,
            "purpose": self.purpose,
            "binding_status": self.binding_status,
            "qualification": self.qualification,
        }


@dataclass(frozen=True)
class BoardLink:
    id: str
    endpoints: Tuple[str, str]
    direction: str
    mode: str
    data_lanes_per_direction: int
    fabric_clock_mhz: float
    latency_cycles: int
    capacity_sharing: str = "per_direction"
    payload_bits_per_lane_per_cycle: int = 1
    max_line_rate_gbps_per_lane: Optional[float] = None
    endpoint_bindings: Tuple[LinkEndpointBinding, ...] = ()

    @property
    def transport_bits_per_cycle_per_direction(self) -> int:
        """User-side bit capacity exposed to routing and TDM.

        Parallel links carry one bit per physical lane and cycle.  A serial
        transceiver presents a wider user-side word for every physical lane;
        the serializer itself remains a board-support-package concern.
        """
        return (
            self.data_lanes_per_direction
            * self.payload_bits_per_lane_per_cycle
        )

    @property
    def raw_bits_per_second_per_direction(self) -> float:
        return (
            self.transport_bits_per_cycle_per_direction
            * self.fabric_clock_mhz
            * 1_000_000.0
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "endpoints": list(self.endpoints),
            "direction": self.direction,
            "mode": self.mode,
            "data_lanes_per_direction": self.data_lanes_per_direction,
            "fabric_clock_mhz": self.fabric_clock_mhz,
            "latency_cycles": self.latency_cycles,
            "capacity_sharing": self.capacity_sharing,
            "raw_bits_per_second_per_direction": (
                self.raw_bits_per_second_per_direction
            ),
        }
        if self.payload_bits_per_lane_per_cycle != 1:
            result["payload_bits_per_lane_per_cycle"] = (
                self.payload_bits_per_lane_per_cycle
            )
            result["transport_bits_per_cycle_per_direction"] = (
                self.transport_bits_per_cycle_per_direction
            )
        if self.max_line_rate_gbps_per_lane is not None:
            result["max_line_rate_gbps_per_lane"] = (
                self.max_line_rate_gbps_per_lane
            )
        if self.endpoint_bindings:
            result["endpoint_bindings"] = [
                binding.to_dict() for binding in self.endpoint_bindings
            ]
        return result

    def endpoint_binding(self, fpga: str) -> Optional[LinkEndpointBinding]:
        return next(
            (
                binding
                for binding in self.endpoint_bindings
                if binding.fpga == fpga
            ),
            None,
        )


@dataclass(frozen=True)
class Platform:
    name: str
    kind: str
    description: str
    fpgas: Tuple[FpgaNode, ...]
    links: Tuple[BoardLink, ...]
    clocks: Tuple[BoardClockContract, ...] = ()
    resets: Tuple[BoardResetContract, ...] = ()
    physical_device: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Platform":
        if value.get("schema") != BOARDDB_SCHEMA:
            raise ValidationError(
                f"platform.schema: expected {BOARDDB_SCHEMA!r}, "
                f"got {value.get('schema')!r}"
            )

        metadata = _require_mapping(value.get("platform"), "platform.platform")
        name = _require_nonempty_string(metadata.get("name"), "platform.name")
        kind = _require_nonempty_string(metadata.get("kind"), "platform.kind")
        if kind not in VALID_PLATFORM_KINDS:
            raise ValidationError(
                f"platform.kind: expected one of {sorted(VALID_PLATFORM_KINDS)}"
            )
        description = metadata.get("description", "")
        if not isinstance(description, str):
            raise ValidationError("platform.description: expected a string")

        raw_fpgas = value.get("fpgas")
        if not isinstance(raw_fpgas, list) or not raw_fpgas:
            raise ValidationError("platform.fpgas: expected a non-empty array")

        fpgas: List[FpgaNode] = []
        fpga_ids = set()
        for index, raw in enumerate(raw_fpgas):
            item = _require_mapping(raw, f"fpgas[{index}]")
            fpga_id = _require_nonempty_string(item.get("id"), f"fpgas[{index}].id")
            if fpga_id in fpga_ids:
                raise ValidationError(f"fpgas[{index}].id: duplicate {fpga_id!r}")
            fpga_ids.add(fpga_id)
            part = _require_nonempty_string(
                item.get("part"), f"fpgas[{index}].part"
            )
            utilization_limit = item.get("utilization_limit")
            if (
                isinstance(utilization_limit, bool)
                or not isinstance(utilization_limit, (int, float))
                or not 0.0 < float(utilization_limit) <= 1.0
            ):
                raise ValidationError(
                    f"fpgas[{index}].utilization_limit: expected 0 < value <= 1"
                )
            raw_capacity = _require_mapping(
                item.get("capacity"), f"fpgas[{index}].capacity"
            )
            capacity: Dict[str, int] = {}
            for resource, raw_count in raw_capacity.items():
                if resource not in RESOURCE_FIELDS:
                    raise ValidationError(
                        f"fpgas[{index}].capacity: unknown resource {resource!r}"
                    )
                if (
                    isinstance(raw_count, bool)
                    or not isinstance(raw_count, int)
                    or raw_count < 0
                ):
                    raise ValidationError(
                        f"fpgas[{index}].capacity.{resource}: "
                        "expected a non-negative integer"
                    )
                capacity[resource] = raw_count
            if not capacity:
                raise ValidationError(
                    f"fpgas[{index}].capacity: expected at least one resource"
                )
            fpgas.append(
                FpgaNode(
                    id=fpga_id,
                    part=part,
                    utilization_limit=float(utilization_limit),
                    capacity=capacity,
                )
            )

        raw_links = value.get("links")
        if not isinstance(raw_links, list):
            raise ValidationError("platform.links: expected an array")
        links: List[BoardLink] = []
        link_ids = set()
        used_serial_connectors = set()
        used_serial_package_pins = set()
        for index, raw in enumerate(raw_links):
            item = _require_mapping(raw, f"links[{index}]")
            link_id = _require_nonempty_string(item.get("id"), f"links[{index}].id")
            if link_id in link_ids:
                raise ValidationError(f"links[{index}].id: duplicate {link_id!r}")
            link_ids.add(link_id)

            endpoints = item.get("endpoints")
            if (
                not isinstance(endpoints, list)
                or len(endpoints) != 2
                or not all(isinstance(endpoint, str) for endpoint in endpoints)
            ):
                raise ValidationError(
                    f"links[{index}].endpoints: expected two FPGA IDs"
                )
            if endpoints[0] == endpoints[1]:
                raise ValidationError(
                    f"links[{index}].endpoints: self-links are not allowed"
                )
            missing = [endpoint for endpoint in endpoints if endpoint not in fpga_ids]
            if missing:
                raise ValidationError(
                    f"links[{index}].endpoints: unknown FPGA IDs {missing}"
                )

            direction = _require_nonempty_string(
                item.get("direction"), f"links[{index}].direction"
            )
            if direction not in VALID_DIRECTIONS:
                raise ValidationError(
                    f"links[{index}].direction: expected one of "
                    f"{sorted(VALID_DIRECTIONS)}"
                )
            mode = _require_nonempty_string(
                item.get("mode"), f"links[{index}].mode"
            )
            if mode not in VALID_LINK_MODES:
                raise ValidationError(
                    f"links[{index}].mode: expected one of "
                    f"{sorted(VALID_LINK_MODES)}"
                )
            capacity_sharing = item.get(
                "capacity_sharing", "per_direction"
            )
            if capacity_sharing not in VALID_CAPACITY_SHARING:
                raise ValidationError(
                    f"links[{index}].capacity_sharing: expected one of "
                    f"{sorted(VALID_CAPACITY_SHARING)}"
                )
            if (
                capacity_sharing == "shared_bidirectional"
                and direction != "full_duplex"
            ):
                raise ValidationError(
                    f"links[{index}].capacity_sharing: "
                    "shared_bidirectional requires full_duplex direction"
                )

            lanes = item.get("data_lanes_per_direction")
            if isinstance(lanes, bool) or not isinstance(lanes, int) or lanes <= 0:
                raise ValidationError(
                    f"links[{index}].data_lanes_per_direction: "
                    "expected a positive integer"
                )
            frequency = item.get("fabric_clock_mhz")
            if (
                isinstance(frequency, bool)
                or not isinstance(frequency, (int, float))
                or frequency <= 0
            ):
                raise ValidationError(
                    f"links[{index}].fabric_clock_mhz: expected a positive number"
                )
            latency = item.get("latency_cycles")
            if (
                isinstance(latency, bool)
                or not isinstance(latency, int)
                or latency < 0
            ):
                raise ValidationError(
                    f"links[{index}].latency_cycles: "
                    "expected a non-negative integer"
                )
            payload_width = item.get("payload_bits_per_lane_per_cycle", 1)
            if (
                isinstance(payload_width, bool)
                or not isinstance(payload_width, int)
                or payload_width <= 0
            ):
                raise ValidationError(
                    f"links[{index}].payload_bits_per_lane_per_cycle: "
                    "expected a positive integer"
                )
            if mode != "serial" and payload_width != 1:
                raise ValidationError(
                    f"links[{index}].payload_bits_per_lane_per_cycle: "
                    "values greater than one require serial mode"
                )
            line_rate = item.get("max_line_rate_gbps_per_lane")
            if line_rate is not None:
                if (
                    mode != "serial"
                    or isinstance(line_rate, bool)
                    or not isinstance(line_rate, (int, float))
                    or float(line_rate) <= 0.0
                ):
                    raise ValidationError(
                        f"links[{index}].max_line_rate_gbps_per_lane: "
                        "expected a positive number for a serial link"
                    )
                configured_rate = float(frequency) * payload_width / 1000.0
                if configured_rate > float(line_rate) * (1.0 + 1e-9):
                    raise ValidationError(
                        f"links[{index}]: configured user-side rate "
                        f"{configured_rate:g} Gbps/lane exceeds maximum "
                        f"line rate {float(line_rate):g} Gbps/lane"
                    )
            raw_endpoint_bindings = item.get("endpoint_bindings", [])
            if not isinstance(raw_endpoint_bindings, list):
                raise ValidationError(
                    f"links[{index}].endpoint_bindings: expected an array"
                )
            if raw_endpoint_bindings and mode != "serial":
                raise ValidationError(
                    f"links[{index}].endpoint_bindings: require serial mode"
                )
            endpoint_bindings: List[LinkEndpointBinding] = []
            bound_fpgas = set()
            for binding_index, raw_binding in enumerate(raw_endpoint_bindings):
                binding_context = (
                    f"links[{index}].endpoint_bindings[{binding_index}]"
                )
                binding = _require_mapping(
                    raw_binding,
                    binding_context,
                )
                fpga = _require_nonempty_string(
                    binding.get("fpga"),
                    f"{binding_context}.fpga",
                )
                connector = _require_nonempty_string(
                    binding.get("connector"),
                    f"{binding_context}.connector",
                )
                mgt = _require_nonempty_string(
                    binding.get("mgt"),
                    f"{binding_context}.mgt",
                )
                if fpga not in endpoints or fpga in bound_fpgas:
                    raise ValidationError(
                        f"{binding_context}: "
                        "FPGA must cover one link endpoint exactly once"
                    )
                connector_key = (fpga, connector)
                if connector_key in used_serial_connectors:
                    raise ValidationError(
                        f"{binding_context}: "
                        "connector is already used by another link"
                    )
                raw_lane_bindings = binding.get("lanes")
                if not isinstance(raw_lane_bindings, list):
                    raise ValidationError(
                        f"{binding_context}.lanes: "
                        "expected an array"
                    )
                lane_bindings: List[SerialLaneBinding] = []
                lane_ids = set()
                binding_package_pins = set()
                for lane_index, raw_lane in enumerate(raw_lane_bindings):
                    lane_context = f"{binding_context}.lanes[{lane_index}]"
                    lane = _require_mapping(
                        raw_lane,
                        lane_context,
                    )
                    lane_id = lane.get("lane")
                    if (
                        isinstance(lane_id, bool)
                        or not isinstance(lane_id, int)
                        or lane_id < 0
                        or lane_id in lane_ids
                    ):
                        raise ValidationError(
                            f"{lane_context}.lane: "
                            "expected a unique non-negative integer"
                        )
                    tx = _require_mapping(
                        lane.get("tx_package_pins"),
                        f"{lane_context}.tx_package_pins",
                    )
                    rx = _require_mapping(
                        lane.get("rx_package_pins"),
                        f"{lane_context}.rx_package_pins",
                    )
                    pins = tuple(
                        _require_nonempty_string(
                            pair.get(polarity),
                            f"{lane_context}.{direction}_package_pins."
                            f"{polarity}",
                        )
                        for direction, pair in (("tx", tx), ("rx", rx))
                        for polarity in ("p", "n")
                    )
                    if len(set(pins)) != len(pins):
                        raise ValidationError(
                            f"{lane_context}: "
                            "differential package pins must be distinct"
                        )
                    package_keys = {(fpga, pin) for pin in pins}
                    if (
                        package_keys & used_serial_package_pins
                        or set(pins) & binding_package_pins
                    ):
                        raise ValidationError(
                            f"{lane_context}: "
                            "package pin is already used"
                        )
                    lane_ids.add(lane_id)
                    binding_package_pins.update(pins)
                    lane_bindings.append(
                        SerialLaneBinding(
                            lane=lane_id,
                            tx_package_pin_p=pins[0],
                            tx_package_pin_n=pins[1],
                            rx_package_pin_p=pins[2],
                            rx_package_pin_n=pins[3],
                        )
                    )
                if lane_ids != set(range(lanes)):
                    raise ValidationError(
                        f"{binding_context}.lanes: "
                        "must cover every physical lane exactly once"
                    )
                bound_fpgas.add(fpga)
                used_serial_connectors.add(connector_key)
                used_serial_package_pins.update(
                    (fpga, pin) for pin in binding_package_pins
                )
                endpoint_bindings.append(
                    LinkEndpointBinding(
                        fpga=fpga,
                        connector=connector,
                        mgt=mgt,
                        lanes=tuple(
                            sorted(lane_bindings, key=lambda entry: entry.lane)
                        ),
                    )
                )
            if endpoint_bindings and bound_fpgas != set(endpoints):
                raise ValidationError(
                    f"links[{index}].endpoint_bindings: "
                    "must cover both link endpoints exactly"
                )
            links.append(
                BoardLink(
                    id=link_id,
                    endpoints=(endpoints[0], endpoints[1]),
                    direction=direction,
                    mode=mode,
                    data_lanes_per_direction=lanes,
                    fabric_clock_mhz=float(frequency),
                    latency_cycles=latency,
                    capacity_sharing=capacity_sharing,
                    payload_bits_per_lane_per_cycle=payload_width,
                    max_line_rate_gbps_per_lane=(
                        None if line_rate is None else float(line_rate)
                    ),
                    endpoint_bindings=tuple(
                        sorted(endpoint_bindings, key=lambda entry: entry.fpga)
                    ),
                )
            )

        raw_services = value.get("board_services", {})
        services = _require_mapping(raw_services, "board_services")
        raw_clocks = services.get("clocks", [])
        raw_resets = services.get("resets", [])
        if not isinstance(raw_clocks, list) or not isinstance(raw_resets, list):
            raise ValidationError("board_services clocks/resets must be arrays")
        clocks: List[BoardClockContract] = []
        service_ids = set()
        for index, raw_clock in enumerate(raw_clocks):
            context = f"board_services.clocks[{index}]"
            clock = _require_mapping(raw_clock, context)
            clock_id = _require_nonempty_string(clock.get("id"), f"{context}.id")
            signal = _require_nonempty_string(
                clock.get("signal"), f"{context}.signal"
            )
            clock_kind = _require_nonempty_string(
                clock.get("kind"), f"{context}.kind"
            )
            frequency = clock.get("frequency_mhz")
            frequency_qualification = clock.get("frequency_qualification")
            count = clock.get("count")
            destination = _require_nonempty_string(
                clock.get("destination"), f"{context}.destination"
            )
            binding_status = clock.get("binding_status")
            qualification = _require_nonempty_string(
                clock.get("qualification"), f"{context}.qualification"
            )
            if clock_id in service_ids:
                raise ValidationError(f"{context}.id: duplicate {clock_id!r}")
            if (
                isinstance(frequency, bool)
                or not isinstance(frequency, (int, float))
                or float(frequency) <= 0.0
                or frequency_qualification
                not in VALID_CLOCK_FREQUENCY_QUALIFICATIONS
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or binding_status not in VALID_BOARD_BINDING_STATUS
            ):
                raise ValidationError(f"{context}: invalid clock contract")
            service_ids.add(clock_id)
            clocks.append(
                BoardClockContract(
                    id=clock_id,
                    signal=signal,
                    kind=clock_kind,
                    frequency_mhz=float(frequency),
                    frequency_qualification=frequency_qualification,
                    count=count,
                    destination=destination,
                    binding_status=binding_status,
                    qualification=qualification,
                )
            )
        resets: List[BoardResetContract] = []
        for index, raw_reset in enumerate(raw_resets):
            context = f"board_services.resets[{index}]"
            reset = _require_mapping(raw_reset, context)
            reset_id = _require_nonempty_string(reset.get("id"), f"{context}.id")
            signal = _require_nonempty_string(
                reset.get("signal"), f"{context}.signal"
            )
            polarity = reset.get("polarity")
            purpose = _require_nonempty_string(
                reset.get("purpose"), f"{context}.purpose"
            )
            binding_status = reset.get("binding_status")
            qualification = _require_nonempty_string(
                reset.get("qualification"), f"{context}.qualification"
            )
            if reset_id in service_ids:
                raise ValidationError(f"{context}.id: duplicate {reset_id!r}")
            if (
                polarity not in VALID_RESET_POLARITIES
                or binding_status not in VALID_BOARD_BINDING_STATUS
            ):
                raise ValidationError(f"{context}: invalid reset contract")
            service_ids.add(reset_id)
            resets.append(
                BoardResetContract(
                    id=reset_id,
                    signal=signal,
                    polarity=polarity,
                    purpose=purpose,
                    binding_status=binding_status,
                    qualification=qualification,
                )
            )

        return cls(
            name=name,
            kind=kind,
            description=description,
            fpgas=tuple(fpgas),
            links=tuple(links),
            clocks=tuple(sorted(clocks, key=lambda item: item.id)),
            resets=tuple(sorted(resets, key=lambda item: item.id)),
            physical_device=value.get("physical_device"),
        )

    @classmethod
    def load(cls, path: Path) -> "Platform":
        return cls.from_dict(read_json(path))

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "schema": BOARDDB_SCHEMA,
            "platform": {
                "name": self.name,
                "kind": self.kind,
                "description": self.description,
            },
            "fpgas": [fpga.to_dict() for fpga in self.fpgas],
            "links": [link.to_dict() for link in self.links],
        }
        if self.clocks or self.resets:
            result["board_services"] = {
                "clocks": [clock.to_dict() for clock in self.clocks],
                "resets": [reset.to_dict() for reset in self.resets],
            }
        if self.physical_device is not None:
            result["physical_device"] = self.physical_device
        return result

    def summary(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "fpga_count": len(self.fpgas),
            "link_count": len(self.links),
            "parts": sorted({fpga.part for fpga in self.fpgas}),
            "total_raw_link_gbps_per_direction": sum(
                link.raw_bits_per_second_per_direction for link in self.links
            )
            / 1_000_000_000.0,
        }
