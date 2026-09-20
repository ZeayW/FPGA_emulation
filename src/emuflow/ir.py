from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Set

from .errors import ValidationError
from .io import read_json
from .resources import ResourceVector


EMUIR_SCHEMA = "emuflow.emuir/v1"
VALID_DIRECTIONS = {"input", "output", "inout", "unknown"}
VALID_CUT_CLASSES = {
    "clock",
    "reset",
    "primary_input",
    "register_output",
    "register_input",
    "combinational",
    "multi_driver",
    "undriven",
}


class EmuIR:
    def __init__(self, value: Mapping[str, Any]):
        self.value = dict(value)
        self.validate()

    @classmethod
    def load(cls, path: Path) -> "EmuIR":
        return cls(read_json(path))

    def validate(self) -> None:
        value = self.value
        if value.get("schema") != EMUIR_SCHEMA:
            raise ValidationError(
                f"ir.schema: expected {EMUIR_SCHEMA!r}, got {value.get('schema')!r}"
            )
        design = value.get("design")
        if not isinstance(design, dict):
            raise ValidationError("ir.design: expected an object")
        for key in ("name", "top", "source_format"):
            if not isinstance(design.get(key), str) or not design[key]:
                raise ValidationError(f"ir.design.{key}: expected a non-empty string")

        ports = value.get("ports")
        instances = value.get("instances")
        nets = value.get("nets")
        clocks = value.get("clocks")
        if not isinstance(ports, list):
            raise ValidationError("ir.ports: expected an array")
        if not isinstance(instances, list):
            raise ValidationError("ir.instances: expected an array")
        if not isinstance(nets, list):
            raise ValidationError("ir.nets: expected an array")
        if not isinstance(clocks, list):
            raise ValidationError("ir.clocks: expected an array")

        port_ids = self._validate_unique_ids(ports, "ports")
        instance_ids = self._validate_unique_ids(instances, "instances")
        self._validate_unique_ids(nets, "nets")
        self._validate_unique_ids(clocks, "clocks")

        for index, port in enumerate(ports):
            direction = port.get("direction")
            if direction not in VALID_DIRECTIONS:
                raise ValidationError(
                    f"ports[{index}].direction: expected one of "
                    f"{sorted(VALID_DIRECTIONS)}"
                )
            width = port.get("width")
            if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
                raise ValidationError(
                    f"ports[{index}].width: expected a positive integer"
                )
            constant_connections = port.get("constant_connections", [])
            if not isinstance(constant_connections, list):
                raise ValidationError(
                    f"ports[{index}].constant_connections: expected an array"
                )
            seen_constant_bits = set()
            for connection_index, connection in enumerate(
                constant_connections
            ):
                context = (
                    f"ports[{index}].constant_connections"
                    f"[{connection_index}]"
                )
                if not isinstance(connection, dict):
                    raise ValidationError(f"{context}: expected an object")
                bit = connection.get("bit")
                if (
                    isinstance(bit, bool)
                    or not isinstance(bit, int)
                    or bit < 0
                    or bit >= width
                    or bit in seen_constant_bits
                ):
                    raise ValidationError(f"{context}.bit: invalid port bit")
                seen_constant_bits.add(bit)
                if connection.get("value") not in {"0", "1", "x", "z"}:
                    raise ValidationError(
                        f"{context}.value: expected 0, 1, x, or z"
                    )
            if constant_connections and direction not in {"output", "inout"}:
                raise ValidationError(
                    f"ports[{index}]: only output ports may be constant"
                )

        for index, instance in enumerate(instances):
            if not isinstance(instance.get("type"), str) or not instance["type"]:
                raise ValidationError(
                    f"instances[{index}].type: expected a non-empty string"
                )
            resources = instance.get("resources")
            if not isinstance(resources, dict):
                raise ValidationError(
                    f"instances[{index}].resources: expected an object"
                )
            ResourceVector.from_mapping(resources, f"instances[{index}].resources")
            constant_connections = instance.get("constant_connections", [])
            if not isinstance(constant_connections, list):
                raise ValidationError(
                    f"instances[{index}].constant_connections: "
                    "expected an array"
                )
            for connection_index, connection in enumerate(
                constant_connections
            ):
                context = (
                    f"instances[{index}].constant_connections"
                    f"[{connection_index}]"
                )
                if not isinstance(connection, dict):
                    raise ValidationError(f"{context}: expected an object")
                if (
                    not isinstance(connection.get("port"), str)
                    or not connection["port"]
                ):
                    raise ValidationError(
                        f"{context}.port: expected a non-empty string"
                    )
                bit = connection.get("bit")
                if isinstance(bit, bool) or not isinstance(bit, int) or bit < 0:
                    raise ValidationError(
                        f"{context}.bit: expected a non-negative integer"
                    )
                if connection.get("value") not in {"0", "1", "x", "z"}:
                    raise ValidationError(
                        f"{context}.value: expected 0, 1, x, or z"
                    )

        for index, net in enumerate(nets):
            cut_class = net.get("cut_class")
            if cut_class not in VALID_CUT_CLASSES:
                raise ValidationError(
                    f"nets[{index}].cut_class: expected one of "
                    f"{sorted(VALID_CUT_CLASSES)}"
                )
            for collection in ("drivers", "sinks"):
                endpoints = net.get(collection)
                if not isinstance(endpoints, list):
                    raise ValidationError(
                        f"nets[{index}].{collection}: expected an array"
                    )
                for endpoint_index, endpoint in enumerate(endpoints):
                    self._validate_endpoint(
                        endpoint,
                        instance_ids,
                        port_ids,
                        f"nets[{index}].{collection}[{endpoint_index}]",
                    )

        warnings = value.get("warnings", [])
        if not isinstance(warnings, list) or not all(
            isinstance(warning, str) for warning in warnings
        ):
            raise ValidationError("ir.warnings: expected an array of strings")

    @staticmethod
    def _validate_unique_ids(
        items: Iterable[Mapping[str, Any]], context: str
    ) -> Set[str]:
        seen: Set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"{context}[{index}]: expected an object")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValidationError(
                    f"{context}[{index}].id: expected a non-empty string"
                )
            if item_id in seen:
                raise ValidationError(
                    f"{context}[{index}].id: duplicate identifier {item_id!r}"
                )
            seen.add(item_id)
        return seen

    @staticmethod
    def _validate_endpoint(
        endpoint: Any,
        instance_ids: Set[str],
        port_ids: Set[str],
        context: str,
    ) -> None:
        if not isinstance(endpoint, dict):
            raise ValidationError(f"{context}: expected an object")
        instance = endpoint.get("instance")
        port = endpoint.get("port")
        bit = endpoint.get("bit")
        if instance is None:
            if port not in port_ids:
                raise ValidationError(
                    f"{context}: top-level port {port!r} does not exist"
                )
        elif instance not in instance_ids:
            raise ValidationError(
                f"{context}: instance {instance!r} does not exist"
            )
        if not isinstance(port, str) or not port:
            raise ValidationError(f"{context}.port: expected a non-empty string")
        if isinstance(bit, bool) or not isinstance(bit, int) or bit < 0:
            raise ValidationError(
                f"{context}.bit: expected a non-negative integer"
            )

    def resource_totals(self) -> ResourceVector:
        return ResourceVector.sum(
            ResourceVector.from_mapping(instance["resources"])
            for instance in self.value["instances"]
        )

    def stats(self) -> Dict[str, Any]:
        cut_classes = Counter(net["cut_class"] for net in self.value["nets"])
        cell_types = Counter(
            instance["type"] for instance in self.value["instances"]
        )
        return {
            "design": self.value["design"]["name"],
            "top": self.value["design"]["top"],
            "ports": len(self.value["ports"]),
            "instances": len(self.value["instances"]),
            "nets": len(self.value["nets"]),
            "clocks": len(self.value["clocks"]),
            "resource_totals": self.resource_totals().to_dict(include_zeros=False),
            "cut_classes": dict(sorted(cut_classes.items())),
            "cell_types": dict(sorted(cell_types.items())),
            "warnings": list(self.value.get("warnings", [])),
        }

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.value)
