from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from emuflow.common.errors import ValidationError


BOARDDB_SCHEMA = "emuflow.boarddb/v1"

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
    hp_io_bank_count: Optional[int] = None
    hd_io_bank_count: Optional[int] = None

    @property
    def effective_capacity(self) -> Dict[str, int]:
        return {
            resource: math.floor(count * self.utilization_limit)
            for resource, count in self.capacity.items()
        }

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "part": self.part,
            "utilization_limit": self.utilization_limit,
            "capacity": dict(sorted(self.capacity.items())),
            "effective_capacity": dict(sorted(self.effective_capacity.items())),
        }
        for key in ("hp_io_bank_count", "hd_io_bank_count"):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        return result


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
class RatioDelayModel:
    """Complete single-hop net delay, including its TDM ratio contribution."""

    model: str
    intercept_ns: float
    ratio_coefficient_ns: float
    min_ratio: int
    max_ratio: int
    ratio_quantum: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RatioDelayModel":
        if not isinstance(value, dict) or value.get("model") != "linear-ratio-v1":
            raise ValidationError("tdm_delay_model: expected linear-ratio-v1")
        if set(value) != {
            "model",
            "intercept_ns",
            "ratio_coefficient_ns",
            "min_ratio",
            "max_ratio",
            "ratio_quantum",
        }:
            raise ValidationError("tdm_delay_model: unexpected or missing fields")
        for key in ("intercept_ns", "ratio_coefficient_ns"):
            number = value[key]
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or number < 0):
                raise ValidationError(f"tdm_delay_model.{key}: expected finite non-negative ns")
        for key in ("min_ratio", "max_ratio", "ratio_quantum"):
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise ValidationError(
                    f"tdm_delay_model.{key}: expected a positive integer"
                )
        if (
            value["min_ratio"] > value["max_ratio"]
            or value["min_ratio"] % value["ratio_quantum"]
            or value["max_ratio"] % value["ratio_quantum"]
        ):
            raise ValidationError(
                "tdm_delay_model: ratio bounds must be ordered multiples of "
                "ratio_quantum"
            )
        return cls(
            value["model"],
            float(value["intercept_ns"]),
            float(value["ratio_coefficient_ns"]),
            value["min_ratio"],
            value["max_ratio"],
            value["ratio_quantum"],
        )

    def delay_ns(self, ratio: float) -> float:
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or ratio < self.min_ratio
            or ratio > self.max_ratio
            or ratio % self.ratio_quantum
        ):
            raise ValidationError(
                "net TDM ratio: expected a supported ratio in "
                f"[{self.min_ratio}, {self.max_ratio}] at quantum "
                f"{self.ratio_quantum}"
            )
        return self.intercept_ns + self.ratio_coefficient_ns * ratio

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "intercept_ns": self.intercept_ns,
            "ratio_coefficient_ns": self.ratio_coefficient_ns,
            "min_ratio": self.min_ratio,
            "max_ratio": self.max_ratio,
            "ratio_quantum": self.ratio_quantum,
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
    bank_count: Optional[int] = None
    wires_per_bank: Optional[int] = None
    tdm_delay_model: Optional[RatioDelayModel] = None
    cable_count: Optional[int] = None
    hp_banks_per_cable: Optional[int] = None

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
        if self.bank_count is not None:
            result["bank_count"] = self.bank_count
            result["wires_per_bank"] = self.wires_per_bank
        if self.cable_count is not None:
            result["cable_count"] = self.cable_count
            result["hp_banks_per_cable"] = self.hp_banks_per_cable
        if self.tdm_delay_model is not None:
            result["tdm_delay_model"] = self.tdm_delay_model.to_dict()
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
    virtual_board_model: Optional[str] = None
    virtual_board_wiring: Optional[str] = None

    @classmethod
    def _from_normalized(cls, value):
        """Construct the bounded optional API from the native owner's records."""
        metadata = value["platform"]
        fpgas = tuple(FpgaNode(id=row["id"], part=row["part"],
            utilization_limit=row["utilization_limit"], capacity=row["capacity"],
            hp_io_bank_count=row.get("hp_io_bank_count"),
            hd_io_bank_count=row.get("hd_io_bank_count")) for row in value["fpgas"])
        links = []
        for row in value["links"]:
            bindings = tuple(LinkEndpointBinding(fpga=b["fpga"], connector=b["connector"], mgt=b["mgt"],
                lanes=tuple(SerialLaneBinding(lane=l["lane"],
                    tx_package_pin_p=l["tx_package_pins"]["p"], tx_package_pin_n=l["tx_package_pins"]["n"],
                    rx_package_pin_p=l["rx_package_pins"]["p"], rx_package_pin_n=l["rx_package_pins"]["n"])
                    for l in b["lanes"])) for b in row.get("endpoint_bindings", []))
            fields = {key: row[key] for key in ("id", "direction", "mode", "data_lanes_per_direction",
                "fabric_clock_mhz", "latency_cycles", "capacity_sharing")}
            for key in ("payload_bits_per_lane_per_cycle", "max_line_rate_gbps_per_lane", "bank_count",
                    "wires_per_bank", "cable_count", "hp_banks_per_cable"):
                if key in row: fields[key] = row[key]
            links.append(BoardLink(**fields, endpoints=tuple(row["endpoints"]), endpoint_bindings=bindings,
                tdm_delay_model=None if "tdm_delay_model" not in row else RatioDelayModel(**row["tdm_delay_model"])))
        services = value.get("board_services", {})
        return cls(name=metadata["name"], kind=metadata["kind"], description=metadata["description"],
            fpgas=fpgas, links=tuple(links), clocks=tuple(BoardClockContract(**row) for row in services.get("clocks", [])),
            resets=tuple(BoardResetContract(**row) for row in services.get("resets", [])),
            virtual_board_model=metadata.get("virtual_board_model"), virtual_board_wiring=metadata.get("virtual_board_wiring"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Platform":
        from emuflow.platform.board_native import normalize_board
        return cls._from_normalized(normalize_board(value))

    @classmethod
    def load(cls, path: Path) -> "Platform":
        from emuflow.platform.board_native import normalize_board
        return cls._from_normalized(normalize_board(path=path))

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
        if self.virtual_board_model is not None:
            result["platform"]["virtual_board_model"] = self.virtual_board_model
        if self.virtual_board_wiring is not None:
            result["platform"]["virtual_board_wiring"] = self.virtual_board_wiring
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

    def with_utilization_limit(
        self, utilization_limit: float, *, name: Optional[str] = None
    ) -> "Platform":
        """Return an immutable capacity-headroom profile of this platform.

        The device capacities, topology, board services, and link contracts are
        preserved exactly.  Only the per-FPGA admissible utilization is changed,
        which keeps controlled capacity experiments separate from the source
        BoardDB instead of encouraging in-place edits.
        """

        if (
            isinstance(utilization_limit, bool)
            or not isinstance(utilization_limit, (int, float))
            or not 0.0 < float(utilization_limit) <= 1.0
        ):
            raise ValidationError(
                "utilization_limit: expected a number in the interval (0, 1]"
            )
        derived_name = self.name if name is None else _require_nonempty_string(
            name, "platform.name"
        )
        return replace(
            self,
            name=derived_name,
            description=(
                f"{self.description} Derived capacity-headroom profile with "
                f"a uniform {float(utilization_limit):g} utilization limit."
            ).strip(),
            fpgas=tuple(
                replace(fpga, utilization_limit=float(utilization_limit))
                for fpga in self.fpgas
            ),
        )
