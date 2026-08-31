"""STA extraction adapters with stable EmuIR cut-net identity."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .timing_routing import STA_PATHS_SCHEMA, compress_sta_paths


VIVADO_STA_TSV_HEADER = (
    "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\t"
    "fixed_delay_ns\tcut_nets_hex"
)
VIVADO_CUT_NET_MAP_HEADER = "vivado_net_hex\tcut_net_hex"
STA_PATH_DATABASE_SCHEMA = "emuflow.sta-path-database/v1"
PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"
STA_PATH_DATABASE_PROVIDERS = {
    "opensta-fpga-path-database-v1",
    "vivado-get-timing-path-database-v1",
}
EMUIR_NET_MAP_HEADER = "mapped_net_hex\temuir_net_hex"
VIVADO_NET_MAP_HEADER = "vivado_net_hex\temuir_net_hex"
STA_PATH_DATABASE_TSV_HEADER = (
    "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\t"
    "fixed_delay_ns\tpath_nets_hex"
)
VIVADO_PATH_DATABASE_TSV_HEADER = STA_PATH_DATABASE_TSV_HEADER


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _instance_pin_inventory(ir: EmuIR) -> Dict[str, set[tuple[str, int]]]:
    result: Dict[str, set[tuple[str, int]]] = {
        instance["id"]: set() for instance in ir.value["instances"]
    }
    for net in ir.value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                instance = endpoint["instance"]
                if instance is not None:
                    result[instance].add((endpoint["port"], endpoint["bit"]))
    for instance in ir.value["instances"]:
        for endpoint in instance.get("constant_connections", []):
            result[instance["id"]].add((endpoint["port"], endpoint["bit"]))
    return result


def _scalar_pin(port: str, bit: int, pins: set[tuple[str, int]]) -> str:
    width = max(
        (
            candidate_bit + 1
            for candidate_port, candidate_bit in pins
            if candidate_port == port
        ),
        default=0,
    )
    return port if width <= 1 else f"{port}__{bit}"


def sta_object_index(ir: EmuIR) -> Dict[str, Dict[str, Any]]:
    """Map provider object names to stable EmuIR endpoint identities."""
    pins = _instance_pin_inventory(ir)
    canonical: Dict[str, Dict[str, Any]] = {}
    for instance in ir.value["instances"]:
        instance_id = instance["id"]
        for port, bit in pins[instance_id]:
            name = f"{instance_id}/{_scalar_pin(port, bit, pins[instance_id])}"
            canonical[name] = {
                "object": name,
                "instance": instance_id,
                "port": port,
                "bit": bit,
            }
    for port in ir.value["ports"]:
        for bit in range(port["width"]):
            names = [port["id"]]
            if port["width"] > 1:
                names = [f"{port['id']}[{bit}]", f"{port['id']}__{bit}"]
            for name in names:
                canonical[name] = {
                    "object": name,
                    "instance": None,
                    "port": port["id"],
                    "bit": bit,
                }

    # OpenSTA reports Verilog escaped identifiers through Tcl.  A literal
    # backslash in the mapped-Verilog object name is therefore escaped one
    # additional time in ``get_full_name`` and in the exported path ID.  Add
    # that exact provider spelling as an O(1) alias while retaining the
    # provider-neutral EmuIR spelling in the endpoint certificate.
    #
    # Do not guess through an ambiguous name.  It is possible (although very
    # unusual) for one canonical object to contain one literal backslash and
    # another to contain two.  Such an IR cannot be mapped bijectively through
    # OpenSTA's textual path identity, so reject it instead of silently binding
    # the path to the wrong endpoint.
    result = dict(canonical)
    for name, endpoint in canonical.items():
        alias = name.replace("\\", "\\\\")
        if alias == name:
            continue
        existing = result.get(alias)
        if existing is not None and existing != endpoint:
            raise ValidationError(
                "STA provider object alias is ambiguous between "
                f"{existing['object']!r} and {name!r}"
            )
        result[alias] = endpoint
    return result


def sta_path_endpoints(
    path: Mapping[str, Any],
    object_index: Mapping[str, Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve structured or legacy provider path endpoints."""
    raw_start = path.get("startpoint")
    raw_end = path.get("endpoint")
    if isinstance(raw_start, dict) and isinstance(raw_end, dict):
        return dict(raw_start), dict(raw_end)
    path_id = path.get("id")
    match = (
        re.fullmatch(r"(.+)->(.+)#\d{8}", path_id)
        if isinstance(path_id, str)
        else None
    )
    if match is None:
        raise ValidationError(
            f"STA path {path_id!r} lacks provider-neutral endpoint identity"
        )
    missing = [
        name for name in match.groups() if name not in object_index
    ]
    if missing:
        raise ValidationError(
            f"STA path {path_id!r} has unmapped endpoint objects {missing}"
        )
    return (
        dict(object_index[match.group(1)]),
        dict(object_index[match.group(2)]),
    )


def _vivado_hard_macro_path_endpoints(
    path: Mapping[str, Any],
    object_index: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    nets: Mapping[str, Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Recover a logical RAM launch hidden behind a Vivado RAMB endpoint.

    Vivado reports a synchronous block-RAM launch at the physical RAMB clock
    pin (for example ``.../memory_reg_bram_0/CLKBWRCLK``), not at the
    source-level VTR RAM output.  The stable EmuIR net identities on that path
    identify the exact logical output bit.  Preserve the physical object for
    the routed timing query while exposing the logical instance/port/bit to
    partition projection.
    """
    path_id = path.get("id")
    match = (
        re.fullmatch(r"(.+)->(.+)#\d{8}", path_id)
        if isinstance(path_id, str)
        else None
    )
    if match is None:
        raise ValidationError("Vivado hard-macro path identity is invalid")
    start_name, end_name = match.groups()
    if end_name not in object_index:
        raise ValidationError("Vivado hard-macro capture endpoint is unmapped")
    marker = "/memory_reg_bram_"
    if marker not in start_name:
        raise ValidationError("Vivado hard-macro launch endpoint is unmapped")
    instance_id = start_name.split(marker, 1)[0]
    instance = instances.get(instance_id)
    if instance is None or instance.get("type") not in {
        "VTR_SP_RAM",
        "VTR_DP_RAM",
    }:
        raise ValidationError("Vivado RAMB launch has no logical RAM instance")
    candidates = []
    for net_id in path.get("path_nets", []):
        net = nets.get(net_id)
        if net is None:
            continue
        candidates.extend(
            endpoint
            for endpoint in net["drivers"]
            if endpoint.get("instance") == instance_id
            and endpoint.get("port") in {"out", "out1", "out2"}
        )
    identities = {
        (item["instance"], item["port"], item["bit"])
        for item in candidates
    }
    if len(identities) != 1:
        raise ValidationError(
            "Vivado RAMB launch does not identify one logical output bit"
        )
    _, port, bit = next(iter(identities))
    return (
        {
            "object": start_name,
            "instance": instance_id,
            "port": port,
            "bit": bit,
        },
        dict(object_index[end_name]),
    )


def _validate_endpoint_identity(
    endpoint: Any,
    known_instances: set[str],
    context: str,
) -> None:
    if not isinstance(endpoint, dict) or set(endpoint) != {
        "object",
        "instance",
        "port",
        "bit",
    }:
        raise ValidationError(f"{context} is invalid")
    if (
        not isinstance(endpoint["object"], str)
        or not endpoint["object"]
        or not isinstance(endpoint["port"], str)
        or not endpoint["port"]
        or isinstance(endpoint["bit"], bool)
        or not isinstance(endpoint["bit"], int)
        or endpoint["bit"] < 0
    ):
        raise ValidationError(f"{context} is invalid")
    instance = endpoint["instance"]
    if instance is not None and (
        not isinstance(instance, str)
        or instance not in known_instances
    ):
        raise ValidationError(f"{context}.instance is invalid")


def _hex_encode(value: str) -> str:
    return value.encode("utf-8").hex()


def _hex_decode(value: str, context: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"{context}: invalid UTF-8 hex") from error


def _database_normalization(paths: list[Dict[str, Any]]) -> Dict[str, float]:
    positive_scale = max(
        (path["slack_ns"] for path in paths if path["slack_ns"] >= 0.0),
        default=1.0,
    )
    if positive_scale == 0.0:
        positive_scale = 1.0
    negative_scale = abs(
        min(
            (path["slack_ns"] for path in paths if path["slack_ns"] < 0.0),
            default=-1.0,
        )
    )
    max_period = max(path["clock_period_ns"] for path in paths)
    return {
        "positive_slack_scale_ns": positive_scale,
        "negative_slack_scale_ns": negative_scale,
        "max_clock_period_ns": max_period,
    }


def _validate_database_normalization(
    value: Any,
) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise ValidationError("STA path database normalization is invalid")
    expected_keys = {
        "positive_slack_scale_ns",
        "negative_slack_scale_ns",
        "max_clock_period_ns",
    }
    if set(value) != expected_keys:
        raise ValidationError("STA path database normalization is invalid")
    result = {}
    for key in sorted(expected_keys):
        item = value[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or float(item) <= 0.0
        ):
            raise ValidationError(
                f"STA path database normalization.{key} is invalid"
            )
        result[key] = float(item)
    return result


def _normalized_slack(
    period: float,
    slack: float,
    normalization: Mapping[str, float],
) -> float:
    if slack >= 0.0:
        return (
            slack
            * period
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
        )
    return (
        slack
        / (
            normalization["negative_slack_scale_ns"]
            * period
        )
    )


def write_vivado_cut_net_map(
    ir_path: Path,
    assignment_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    assignment = read_json(assignment_path)
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    if assignment.get("design") != ir.value["design"]["name"]:
        raise ValidationError("assignment.design does not match EmuIR")
    net_index = {
        net["id"]: index for index, net in enumerate(ir.value["nets"])
    }
    cut_nets = sorted(
        {
            cut["net"]
            for cut in assignment.get("cut_nets", [])
            if isinstance(cut, dict) and isinstance(cut.get("net"), str)
        }
    )
    unknown = sorted(set(cut_nets) - set(net_index))
    if unknown:
        raise ValidationError(
            f"assignment cut nets are absent from EmuIR: {unknown[:10]}"
        )
    lines = [VIVADO_CUT_NET_MAP_HEADER]
    for cut_net in cut_nets:
        vivado_name = f"__emuflow_net_{net_index[cut_net]}"
        lines.append(
            f"{_hex_encode(vivado_name)}\t{_hex_encode(cut_net)}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "pass",
        "design": assignment["design"],
        "cut_nets": len(cut_nets),
        "output": str(output_path),
    }


def _write_net_map(
    ir_path: Path,
    output_path: Path,
    header: str,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        output.write(header + "\n")
        for index, net in enumerate(ir.value["nets"]):
            output.write(
                f"{_hex_encode(f'__emuflow_net_{index}')}\t"
                f"{_hex_encode(net['id'])}\n"
            )
    return {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "nets": len(ir.value["nets"]),
        "output": str(output_path),
    }


def write_emuir_net_map(
    ir_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    return _write_net_map(ir_path, output_path, EMUIR_NET_MAP_HEADER)


def write_vivado_net_map(
    ir_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    return _write_net_map(ir_path, output_path, VIVADO_NET_MAP_HEADER)


def import_sta_path_database_tsv(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    provider: str,
    source: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    known_nets = {net["id"] for net in ir.value["nets"]}
    object_index = sta_object_index(ir)
    instances_by_id = (
        {item["id"]: item for item in ir.value["instances"]}
        if provider == "vivado-get-timing-path-database-v1"
        else {}
    )
    nets_by_id = (
        {item["id"]: item for item in ir.value["nets"]}
        if provider == "vivado-get-timing-path-database-v1"
        else {}
    )
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != STA_PATH_DATABASE_TSV_HEADER:
        raise ValidationError("STA path database TSV: invalid header")
    if provider not in STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
    paths = []
    path_ids = set()
    for index, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "expected six fields"
            )
        path_id = _hex_decode(
            fields[0],
            f"STA path database TSV line {index} path",
        )
        clock_domain = _hex_decode(
            fields[1],
            f"STA path database TSV line {index} clock",
        )
        if not path_id or path_id in path_ids:
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "invalid or duplicate path"
            )
        path_ids.add(path_id)
        try:
            clock_period = float(fields[2])
            slack = float(fields[3])
            fixed_delay = float(fields[4])
        except ValueError as error:
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "invalid numeric field"
            ) from error
        if (
            not all(
                math.isfinite(value)
                for value in (clock_period, slack, fixed_delay)
            )
            or clock_period <= 0.0
            or fixed_delay < 0.0
        ):
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "invalid period/delay"
            )
        raw_nets = fields[5].split(",")
        if not raw_nets or any(not item for item in raw_nets):
            raise ValidationError(
                f"STA path database TSV line {index}: "
                "empty path-net list"
            )
        path_nets = [
            _hex_decode(
                item,
                f"STA path database TSV line {index} path_nets_hex",
            )
            for item in raw_nets
        ]
        if len(set(path_nets)) != len(path_nets):
            raise ValidationError(
                f"STA path database TSV line {index}: duplicate net"
            )
        unknown = sorted(set(path_nets) - known_nets)
        if unknown:
            raise ValidationError(
                f"STA path database TSV line {index}: "
                f"unknown EmuIR nets {unknown}"
            )
        record = {
            "id": path_id,
            "clock_domain": clock_domain,
            "clock_period_ns": clock_period,
            "slack_ns": slack,
            "fixed_delay_ns": fixed_delay,
            "path_nets": path_nets,
        }
        try:
            startpoint, endpoint = sta_path_endpoints(record, object_index)
        except ValidationError:
            if provider == "vivado-get-timing-path-database-v1":
                try:
                    startpoint, endpoint = _vivado_hard_macro_path_endpoints(
                        record,
                        object_index,
                        instances_by_id,
                        nets_by_id,
                    )
                except ValidationError:
                    pass
                else:
                    record["startpoint"] = startpoint
                    record["endpoint"] = endpoint
        else:
            record["startpoint"] = startpoint
            record["endpoint"] = endpoint
        paths.append(record)
    if not paths:
        raise ValidationError(
            "STA path database TSV contains no mapped timing paths"
        )
    normalization = _database_normalization(paths)
    for path in paths:
        path["normalized_slack"] = _normalized_slack(
            path["clock_period_ns"],
            path["slack_ns"],
            normalization,
        )
    source_value = dict(source) if source is not None else {}
    source_value.update(
        {
            "provider": provider,
            "input": str(input_path),
        }
    )
    artifact = {
        "schema": STA_PATH_DATABASE_SCHEMA,
        "design": ir.value["design"]["name"],
        "source": source_value,
        "normalization": normalization,
        "paths": paths,
    }
    write_json(output_path, artifact)
    return {
        "status": "pass",
        "design": artifact["design"],
        "paths": len(paths),
        "structured_endpoint_paths": sum(
            "startpoint" in path and "endpoint" in path for path in paths
        ),
        "unique_path_nets": len(
            {net for path in paths for net in path["path_nets"]}
        ),
        "output": str(output_path),
    }


def import_vivado_path_database_tsv(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    return import_sta_path_database_tsv(
        input_path,
        ir_path,
        output_path,
        provider="vivado-get-timing-path-database-v1",
    )


def validate_sta_path_database_value(
    database: Mapping[str, Any],
    ir: EmuIR,
) -> Dict[str, Any]:
    if database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("STA path database schema is invalid")
    if database.get("design") != ir.value["design"]["name"]:
        raise ValidationError("STA path database design does not match EmuIR")
    source = database.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provider") not in STA_PATH_DATABASE_PROVIDERS
    ):
        raise ValidationError("STA path database source is invalid")
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    raw_paths = database.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("STA path database paths are invalid")
    known_nets = {net["id"] for net in ir.value["nets"]}
    known_instances = {
        instance["id"] for instance in ir.value["instances"]
    }
    path_ids = set()
    clock_domains = set()
    path_nets_union = set()
    worst_slack: Optional[float] = None
    structured_endpoint_paths = 0
    for index, path in enumerate(raw_paths):
        context = f"STA path database paths[{index}]"
        if not isinstance(path, dict):
            raise ValidationError(f"{context} is invalid")
        required_keys = {
            "id",
            "clock_domain",
            "clock_period_ns",
            "slack_ns",
            "fixed_delay_ns",
            "path_nets",
            "normalized_slack",
        }
        endpoint_keys = {"startpoint", "endpoint"}
        if not (
            set(path) == required_keys
            or set(path) == required_keys | endpoint_keys
        ):
            raise ValidationError(f"{context} fields are invalid")
        if endpoint_keys <= set(path):
            structured_endpoint_paths += 1
            _validate_endpoint_identity(
                path["startpoint"], known_instances, f"{context}.startpoint"
            )
            _validate_endpoint_identity(
                path["endpoint"], known_instances, f"{context}.endpoint"
            )
        path_id = path["id"]
        clock_domain = path["clock_domain"]
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(f"{context} identity is invalid")
        path_ids.add(path_id)
        clock_domains.add(clock_domain)
        numeric = {}
        for name in (
            "clock_period_ns",
            "slack_ns",
            "fixed_delay_ns",
            "normalized_slack",
        ):
            value = path[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(f"{context}.{name} is invalid")
            numeric[name] = float(value)
        if (
            numeric["clock_period_ns"] <= 0.0
            or numeric["fixed_delay_ns"] < 0.0
        ):
            raise ValidationError(f"{context} timing is invalid")
        path_nets = path["path_nets"]
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(f"{context}.path_nets is invalid")
        unknown = sorted(set(path_nets) - known_nets)
        if unknown:
            raise ValidationError(
                f"{context}.path_nets contains unknown EmuIR nets {unknown}"
            )
        expected_normalized = _normalized_slack(
            numeric["clock_period_ns"],
            numeric["slack_ns"],
            normalization,
        )
        if (
            abs(numeric["normalized_slack"] - expected_normalized)
            > 1.0e-12
        ):
            raise ValidationError(
                f"{context}.normalized_slack is inconsistent"
            )
        path_nets_union.update(path_nets)
        if worst_slack is None or numeric["slack_ns"] < worst_slack:
            worst_slack = numeric["slack_ns"]
    return {
        "status": "pass",
        "design": database["design"],
        "provider": source["provider"],
        "paths": len(raw_paths),
        "structured_endpoint_paths": structured_endpoint_paths,
        "unresolved_endpoint_paths": (
            len(raw_paths) - structured_endpoint_paths
        ),
        "clock_domains": sorted(clock_domains),
        "unique_path_nets": len(path_nets_union),
        "worst_slack_ns": worst_slack,
    }


def validate_sta_path_database(
    database_path: Path,
    ir_path: Path,
) -> Dict[str, Any]:
    return validate_sta_path_database_value(
        read_json(database_path), EmuIR.load(ir_path)
    )


def derive_partition_net_weights(
    database_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    criticality_scale: float = 9.0,
    criticality_exponent: float = 2.0,
) -> Dict[str, Any]:
    """Project path criticality onto hyperedges before partitioning.

    Net criticality is the maximum ``clamp(1 - slack / period, 0, 1)`` over
    all extracted paths containing the net. The power-law edge weight is the
    standard timing-driven partitioning form ``1 + scale * criticality^p``.
    """
    for name, value in (
        ("criticality_scale", criticality_scale),
        ("criticality_exponent", criticality_exponent),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValidationError(f"{name} must be positive")
    checked = validate_sta_path_database(database_path, ir_path)
    database = read_json(database_path)
    criticality_by_net: Dict[str, float] = {}
    path_count_by_net: Dict[str, int] = {}
    for path in database["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - float(path["slack_ns"])
                / float(path["clock_period_ns"]),
            ),
        )
        for net in path["path_nets"]:
            criticality_by_net[net] = max(
                criticality_by_net.get(net, 0.0),
                criticality,
            )
            path_count_by_net[net] = path_count_by_net.get(net, 0) + 1
    weights = {
        net: 1.0
        + float(criticality_scale)
        * criticality ** float(criticality_exponent)
        for net, criticality in sorted(criticality_by_net.items())
        if criticality > 0.0
    }
    artifact = {
        "schema": PARTITION_NET_WEIGHTS_SCHEMA,
        "design": database["design"],
        "source": {
            "provider": "sta-max-criticality-power-law-v1",
            "path_database": str(database_path),
            "path_database_provider": database["source"]["provider"],
        },
        "parameters": {
            "criticality_scale": float(criticality_scale),
            "criticality_exponent": float(criticality_exponent),
            "criticality_definition": "clamp(1-slack/period,0,1)",
            "path_reduction": "maximum",
        },
        "criticality": dict(sorted(criticality_by_net.items())),
        "path_count": dict(sorted(path_count_by_net.items())),
        "weights": weights,
    }
    write_json(output_path, artifact)
    return {
        "status": "pass",
        "design": database["design"],
        "paths": checked["paths"],
        "timed_nets": len(criticality_by_net),
        "weighted_nets": len(weights),
        "minimum_weight": min(weights.values(), default=1.0),
        "maximum_weight": max(weights.values(), default=1.0),
        "output": str(output_path),
    }


def project_sta_path_database(
    database_path: Path,
    assignment_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    database = read_json(database_path)
    assignment = read_json(assignment_path)
    if database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("STA path database schema is invalid")
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    if database.get("design") != assignment.get("design"):
        raise ValidationError(
            "STA path database design does not match assignment"
        )
    cut_nets = {
        cut["net"]
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict) and isinstance(cut.get("net"), str)
    }
    if not cut_nets:
        raise ValidationError(
            "STA path projection requires partition cut nets"
        )
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    cut_by_net = {
        cut["net"]: cut
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict)
        and isinstance(cut.get("net"), str)
        and cut["net"] in cut_nets
    }
    if set(cut_by_net) != cut_nets:
        raise ValidationError("partition cut-net records are invalid")
    cut_signature_by_net = {}
    for net in sorted(cut_by_net):
        cut = cut_by_net[net]
        sources = cut.get("source_fpgas", [])
        sinks = cut.get("sink_fpgas", [])
        if (
            not isinstance(sources, list)
            or not all(isinstance(item, str) and item for item in sources)
            or not isinstance(sinks, list)
            or not all(isinstance(item, str) and item for item in sinks)
        ):
            raise ValidationError(
                f"partition cut-net {net!r} endpoint lists are invalid"
            )
        cut_signature_by_net[net] = (
            f"{','.join(sources)}->{','.join(sinks)}"
        )
    instance_assignment = assignment.get("instance_assignment")
    if not isinstance(instance_assignment, dict):
        instance_assignment = {}

    def path_transitions(
        path: Mapping[str, Any], candidates: list[str]
    ) -> tuple[list[str], Optional[list[Dict[str, str]]]]:
        startpoint = path.get("startpoint")
        endpoint = path.get("endpoint")
        if not isinstance(startpoint, dict) or not isinstance(endpoint, dict):
            return candidates, None
        end_instance = endpoint.get("instance")
        end_partition = instance_assignment.get(end_instance)
        if not isinstance(end_partition, str) or not end_partition:
            return candidates, None
        target = end_partition
        reverse_transitions = []
        for net in reversed(candidates):
            raw_sources = cut_by_net[net].get("source_fpgas")
            raw_sinks = cut_by_net[net].get("sink_fpgas")
            if not isinstance(raw_sources, list) or len(raw_sources) != 1:
                return candidates, None
            source = raw_sources[0]
            if source == target:
                # This timing path follows a local fanout of a net whose
                # other sinks make the net globally cross-partition.
                continue
            if not isinstance(raw_sinks, list) or target not in raw_sinks:
                raise ValidationError(
                    f"STA path {path['id']!r} partition {target!r} cannot "
                    f"be reached through cut net {net!r}"
                )
            reverse_transitions.append(
                {"net": net, "from": source, "to": target}
            )
            target = source
        start_instance = startpoint.get("instance")
        start_partition = instance_assignment.get(start_instance)
        if (
            isinstance(start_partition, str)
            and start_partition != target
        ):
            raise ValidationError(
                f"STA path {path['id']!r} launch/capture partition chain "
                "is inconsistent"
            )
        transitions = list(reversed(reverse_transitions))
        return [item["net"] for item in transitions], transitions

    paths = []
    database_records = []
    covered_cut_nets = set()
    path_ids = set()
    raw_paths = database.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("STA path database paths are invalid")
    for index, path in enumerate(raw_paths):
        if not isinstance(path, dict):
            raise ValidationError(
                f"STA path database paths[{index}] is invalid"
            )
        path_id = path.get("id")
        clock_domain = path.get("clock_domain")
        clock_period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        fixed_delay = path.get("fixed_delay_ns")
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(
                f"STA path database paths[{index}] identity is invalid"
            )
        path_ids.add(path_id)
        for name, value in (
            ("clock_period_ns", clock_period),
            ("slack_ns", slack),
            ("fixed_delay_ns", fixed_delay),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"STA path database paths[{index}].{name} is invalid"
                )
        if float(clock_period) <= 0.0 or float(fixed_delay) < 0.0:
            raise ValidationError(
                f"STA path database paths[{index}] timing is invalid"
            )
        path_nets = path.get("path_nets")
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(
                f"STA path database paths[{index}].path_nets is invalid"
            )
        expected_normalized = _normalized_slack(
            float(clock_period),
            float(slack),
            normalization,
        )
        normalized = path.get("normalized_slack")
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, (int, float))
            or not math.isfinite(float(normalized))
            or abs(float(normalized) - expected_normalized) > 1.0e-12
        ):
            raise ValidationError(
                f"STA path database paths[{index}].normalized_slack "
                "is invalid"
            )
        database_records.append(
            {
                "clock_period_ns": float(clock_period),
                "slack_ns": float(slack),
            }
        )
        candidates = [net for net in path_nets if net in cut_nets]
        projected, transitions = path_transitions(path, candidates)
        if not projected:
            continue
        covered_cut_nets.update(projected)
        record = {
            "id": path_id,
            "clock_domain": clock_domain,
            "clock_period_ns": float(clock_period),
            "slack_ns": float(slack),
            "fixed_delay_ns": float(fixed_delay),
            "cut_nets": projected,
            "cut_signature": [
                cut_signature_by_net[net]
                for net in projected
            ],
            "normalized_slack": expected_normalized,
            "compressed_path_ids": [path_id],
        }
        if transitions is not None:
            record["cut_transitions"] = transitions
        paths.append(record)
    if not paths:
        raise ValidationError(
            "STA path database has no path crossing this partition"
        )
    expected_normalization = _database_normalization(database_records)
    if any(
        abs(expected_normalization[key] - normalization[key]) > 1.0e-12
        for key in expected_normalization
    ):
        raise ValidationError(
            "STA path database normalization does not match its paths"
        )
    artifact = compress_sta_paths({
        "schema": STA_PATHS_SCHEMA,
        "design": assignment["design"],
        "source": {
            "provider": "partition-projected-sta-paths-v1",
            # Checkpoints are first built below a staging directory and then
            # atomically moved into the content-addressed object store.  A
            # producer-local absolute path would make an otherwise identical
            # projection fail independent reconstruction after that move.
            "input_sha256": _file_sha256(database_path),
        },
        "normalization": normalization,
        "paths": paths,
    })
    write_json(output_path, artifact)
    return {
        "status": "pass",
        "design": assignment["design"],
        "database_paths": len(raw_paths),
        "projected_paths": len(paths),
        "compressed_paths": len(artifact["paths"]),
        "cut_nets": len(cut_nets),
        "covered_cut_nets": len(covered_cut_nets),
        "uncovered_cut_nets": len(cut_nets - covered_cut_nets),
        "output": str(output_path),
    }


def import_vivado_sta_tsv(
    input_path: Path,
    assignment_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    assignment = read_json(assignment_path)
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    valid_cut_nets = {
        cut["net"]
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict) and isinstance(cut.get("net"), str)
    }
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != VIVADO_STA_TSV_HEADER:
        raise ValidationError("Vivado STA TSV: invalid header")
    paths = []
    path_ids = set()
    for index, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            raise ValidationError(
                f"Vivado STA TSV line {index}: expected six fields"
            )
        path_id = _hex_decode(fields[0], f"Vivado STA TSV line {index} path")
        clock_domain = _hex_decode(
            fields[1], f"Vivado STA TSV line {index} clock"
        )
        if not path_id or path_id in path_ids:
            raise ValidationError(
                f"Vivado STA TSV line {index}: invalid or duplicate path"
            )
        path_ids.add(path_id)
        try:
            clock_period = float(fields[2])
            slack = float(fields[3])
            fixed_delay = float(fields[4])
        except ValueError as error:
            raise ValidationError(
                f"Vivado STA TSV line {index}: invalid numeric field"
            ) from error
        if clock_period <= 0.0 or fixed_delay < 0.0:
            raise ValidationError(
                f"Vivado STA TSV line {index}: invalid period/delay"
            )
        raw_cut_nets = fields[5].split(",")
        if not raw_cut_nets or any(not item for item in raw_cut_nets):
            raise ValidationError(
                f"Vivado STA TSV line {index}: empty cut-net list"
            )
        cut_nets = [
            _hex_decode(
                item, f"Vivado STA TSV line {index} cut_nets_hex"
            )
            for item in raw_cut_nets
        ]
        if len(set(cut_nets)) != len(cut_nets):
            raise ValidationError(
                f"Vivado STA TSV line {index}: duplicate cut net"
            )
        unknown = sorted(set(cut_nets) - valid_cut_nets)
        if unknown:
            raise ValidationError(
                f"Vivado STA TSV line {index}: unknown cut nets {unknown}"
            )
        paths.append(
            {
                "id": path_id,
                "clock_domain": clock_domain,
                "clock_period_ns": clock_period,
                "slack_ns": slack,
                "fixed_delay_ns": fixed_delay,
                "cut_nets": cut_nets,
            }
        )
    if not paths:
        raise ValidationError("Vivado STA TSV contains no cut timing paths")
    artifact = {
        "schema": STA_PATHS_SCHEMA,
        "design": assignment["design"],
        "source": {
            "provider": "vivado-get-timing-paths-v1",
            "input": str(input_path),
        },
        "paths": paths,
    }
    write_json(output_path, artifact)
    return {
        "status": "pass",
        "design": assignment["design"],
        "paths": len(paths),
        "unique_cut_nets": len(
            {net for path in paths for net in path["cut_nets"]}
        ),
        "output": str(output_path),
    }
