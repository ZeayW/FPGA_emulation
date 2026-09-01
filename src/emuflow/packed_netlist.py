"""Versioned VPR packed-netlist contract for the OpenPARF handoff."""

from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .native_tools import resolve_native_executable


PACKED_NETLIST_SCHEMA = "emuflow.vpr-packed-netlist/v1"
VPR_PACKED_NETLIST_FORMAT = "vpr-packed-net-xml/v1"
_EXTRACT_HEADER = "EMUFLOW_VPR_PACKED_NETLIST_EXTRACT_V1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(value: str, context: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"{context}: invalid UTF-8 hex") from error


def _source_id(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("SHA256:")
        or len(value) != 71
        or any(
            character not in "0123456789abcdef"
            for character in value[7:]
        )
    ):
        raise ValidationError(f"{context} must be SHA256:<64 lowercase hex>")
    return value


def _parse_extract(text: str, source_path: Path) -> Dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != _EXTRACT_HEADER:
        raise ValidationError("VPR packed-netlist importer header is invalid")
    root: Optional[Dict[str, str]] = None
    clusters: Dict[str, Dict[str, Any]] = {}
    nets: Dict[str, Dict[str, Any]] = {}
    for line_number, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        context = f"native extract line {line_number}"
        record = fields[0]
        if record == "ROOT" and len(fields) == 5:
            if root is not None:
                raise ValidationError(f"{context}: duplicate ROOT")
            root = {
                "name": _decode(fields[1], context),
                "instance": _decode(fields[2], context),
                "architecture_id": _decode(fields[3], context),
                "atom_netlist_id": _decode(fields[4], context),
            }
        elif record == "CLUSTER" and len(fields) == 6:
            cluster_id = _decode(fields[1], context)
            if cluster_id in clusters:
                raise ValidationError(
                    f"{context}: duplicate cluster {cluster_id!r}"
                )
            clusters[cluster_id] = {
                "id": cluster_id,
                "name": _decode(fields[2], context),
                "instance": _decode(fields[3], context),
                "block_type": _decode(fields[4], context),
                "mode": _decode(fields[5], context),
                "pb_blocks": [],
                "atoms": [],
            }
        elif record == "PB" and len(fields) == 7:
            cluster_id = _decode(fields[1], context)
            if cluster_id not in clusters:
                raise ValidationError(
                    f"{context}: PB references unknown cluster {cluster_id!r}"
                )
            leaf = fields[6]
            if leaf not in {"0", "1"}:
                raise ValidationError(f"{context}: invalid leaf flag")
            block = {
                "path": _decode(fields[2], context),
                "name": _decode(fields[3], context),
                "instance": _decode(fields[4], context),
                "mode": _decode(fields[5], context),
                "leaf": leaf == "1",
            }
            clusters[cluster_id]["pb_blocks"].append(block)
            if block["leaf"]:
                clusters[cluster_id]["atoms"].append(block["name"])
        elif record == "NET" and len(fields) == 3:
            net_id = _decode(fields[1], context)
            if net_id in nets:
                raise ValidationError(
                    f"{context}: duplicate net {net_id!r}"
                )
            nets[net_id] = {
                "id": net_id,
                "driver": _decode(fields[2], context),
                "sinks": [],
            }
        elif record == "SINK" and len(fields) == 3:
            net_id = _decode(fields[1], context)
            if net_id not in nets:
                raise ValidationError(
                    f"{context}: sink references unknown net {net_id!r}"
                )
            nets[net_id]["sinks"].append(_decode(fields[2], context))
        else:
            raise ValidationError(
                f"{context}: malformed {record!r} record"
            )
    if root is None:
        raise ValidationError("native extract has no ROOT record")
    design = root["name"]
    if design.endswith(".net"):
        design = design[:-4]
    return {
        "schema": PACKED_NETLIST_SCHEMA,
        "design": design,
        "source": {
            "format": VPR_PACKED_NETLIST_FORMAT,
            "path": str(source_path.resolve()),
            "sha256": _sha256(source_path),
            "root_instance": root["instance"],
            "architecture_id": root["architecture_id"],
            "atom_netlist_id": root["atom_netlist_id"],
        },
        "clusters": [
            clusters[cluster_id] for cluster_id in sorted(clusters)
        ],
        "nets": [nets[net_id] for net_id in sorted(nets)],
    }


def validate_packed_netlist_contract(
    value: Mapping[str, Any],
    *,
    expected_architecture_sha256: Optional[str] = None,
    expected_atom_netlist_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    if value.get("schema") != PACKED_NETLIST_SCHEMA:
        raise ValidationError("packed-netlist schema is invalid")
    design = value.get("design")
    if not isinstance(design, str) or not design:
        raise ValidationError("packed-netlist design is invalid")
    source = value.get("source")
    if not isinstance(source, Mapping):
        raise ValidationError("packed-netlist source is invalid")
    if source.get("format") != VPR_PACKED_NETLIST_FORMAT:
        raise ValidationError("packed-netlist source format is invalid")
    root_instance = source.get("root_instance")
    if (
        not isinstance(root_instance, str)
        or not root_instance.startswith("FPGA_packed_netlist[")
    ):
        raise ValidationError("packed-netlist root instance is invalid")
    source_hash = source.get("sha256")
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source_hash
        )
    ):
        raise ValidationError("packed-netlist source SHA-256 is invalid")
    architecture_id = _source_id(
        source.get("architecture_id"), "source.architecture_id"
    )
    atom_netlist_id = _source_id(
        source.get("atom_netlist_id"), "source.atom_netlist_id"
    )
    if (
        expected_architecture_sha256 is not None
        and architecture_id != f"SHA256:{expected_architecture_sha256}"
    ):
        raise ValidationError(
            "packed-netlist architecture_id does not match architecture XML"
        )
    if (
        expected_atom_netlist_sha256 is not None
        and atom_netlist_id != f"SHA256:{expected_atom_netlist_sha256}"
    ):
        raise ValidationError(
            "packed-netlist atom_netlist_id does not match eBLIF"
        )

    raw_clusters = value.get("clusters")
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise ValidationError("packed-netlist clusters must be non-empty")
    cluster_ids = set()
    cluster_names = set()
    block_numbers = set()
    block_types: Counter[str] = Counter()
    atoms = 0
    pb_blocks = 0
    for index, cluster in enumerate(raw_clusters):
        context = f"clusters[{index}]"
        if not isinstance(cluster, Mapping):
            raise ValidationError(f"{context} is invalid")
        cluster_id = cluster.get("id")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValidationError(f"{context}.id is invalid")
        if cluster_id in cluster_ids:
            raise ValidationError(f"duplicate cluster {cluster_id!r}")
        cluster_ids.add(cluster_id)
        if cluster.get("instance") != cluster_id:
            raise ValidationError(
                f"{context}.instance must equal its stable cluster id"
            )
        cluster_name = cluster.get("name")
        if not isinstance(cluster_name, str) or not cluster_name:
            raise ValidationError(f"{context}.name is invalid")
        if cluster_name in cluster_names:
            raise ValidationError(
                f"duplicate packed block name {cluster_name!r}"
            )
        cluster_names.add(cluster_name)
        bracket = cluster_id.rfind("[")
        if (
            bracket <= 0
            or not cluster_id.endswith("]")
            or not cluster_id[bracket + 1 : -1].isdigit()
        ):
            raise ValidationError(f"{context}.id has no VPR block number")
        block_number = int(cluster_id[bracket + 1 : -1])
        if block_number in block_numbers:
            raise ValidationError(
                f"duplicate VPR block number {block_number}"
            )
        block_numbers.add(block_number)
        block_type = cluster.get("block_type")
        if not isinstance(block_type, str) or not block_type:
            raise ValidationError(f"{context}.block_type is invalid")
        block_types[block_type] += 1
        blocks = cluster.get("pb_blocks")
        cluster_atoms = cluster.get("atoms")
        if not isinstance(blocks, list) or not isinstance(
            cluster_atoms, list
        ):
            raise ValidationError(f"{context} hierarchy is invalid")
        paths = set()
        leaf_names = []
        for block_index, block in enumerate(blocks):
            block_context = f"{context}.pb_blocks[{block_index}]"
            if not isinstance(block, Mapping):
                raise ValidationError(f"{block_context} is invalid")
            path = block.get("path")
            if not isinstance(path, str) or not path.startswith(
                cluster_id + "/"
            ):
                raise ValidationError(f"{block_context}.path is invalid")
            if path in paths:
                raise ValidationError(
                    f"{block_context}.path is duplicated"
                )
            paths.add(path)
            if block.get("leaf") is True:
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    raise ValidationError(
                        f"{block_context} leaf name is invalid"
                    )
                leaf_names.append(name)
        if cluster_atoms != leaf_names:
            raise ValidationError(
                f"{context}.atoms does not match hierarchy leaves"
            )
        atoms += len(cluster_atoms)
        pb_blocks += len(blocks)

    raw_nets = value.get("nets")
    if not isinstance(raw_nets, list):
        raise ValidationError("packed-netlist nets is invalid")
    net_ids = set()
    endpoint_count = 0
    for index, net in enumerate(raw_nets):
        context = f"nets[{index}]"
        if not isinstance(net, Mapping):
            raise ValidationError(f"{context} is invalid")
        net_id = net.get("id")
        if not isinstance(net_id, str) or not net_id:
            raise ValidationError(f"{context}.id is invalid")
        if net_id in net_ids:
            raise ValidationError(f"duplicate net {net_id!r}")
        net_ids.add(net_id)
        driver = net.get("driver")
        sinks = net.get("sinks")
        if driver not in cluster_ids:
            raise ValidationError(f"{context}.driver is unknown")
        if (
            not isinstance(sinks, list)
            or not sinks
            or len(sinks) != len(set(sinks))
            or driver in sinks
            or any(sink not in cluster_ids for sink in sinks)
        ):
            raise ValidationError(f"{context}.sinks is invalid")
        endpoint_count += 1 + len(sinks)

    return {
        "status": "pass",
        "schema": PACKED_NETLIST_SCHEMA,
        "design": design,
        "clusters": len(cluster_ids),
        "block_types": dict(sorted(block_types.items())),
        "pb_blocks": pb_blocks,
        "atoms": atoms,
        "cross_cluster_nets": len(net_ids),
        "net_endpoints": endpoint_count,
        "architecture_id": architecture_id,
        "atom_netlist_id": atom_netlist_id,
    }


def validate_packed_netlist_file(
    path: Path,
    *,
    architecture_path: Optional[Path] = None,
    circuit_path: Optional[Path] = None,
) -> Dict[str, Any]:
    return validate_packed_netlist_contract(
        read_json(path),
        expected_architecture_sha256=(
            _sha256(architecture_path)
            if architecture_path is not None
            else None
        ),
        expected_atom_netlist_sha256=(
            _sha256(circuit_path) if circuit_path is not None else None
        ),
    )


def run_packed_netlist_import(
    packed_netlist_path: Path,
    output_path: Path,
    *,
    architecture_path: Optional[Path] = None,
    circuit_path: Optional[Path] = None,
    executable: Optional[str] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    packed_netlist_path = packed_netlist_path.resolve()
    if not packed_netlist_path.is_file():
        raise EmuFlowError(
            f"VPR packed netlist does not exist: {packed_netlist_path}"
        )
    command = [
        resolve_native_executable(
            "emuflow_vpr_packed_netlist_importer", executable
        ),
        str(packed_netlist_path),
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EmuFlowError(
            "VPR packed-netlist importer failed with exit code "
            f"{completed.returncode}: {detail}"
        )
    value = _parse_extract(completed.stdout, packed_netlist_path)
    report = validate_packed_netlist_contract(
        value,
        expected_architecture_sha256=(
            _sha256(architecture_path)
            if architecture_path is not None
            else None
        ),
        expected_atom_netlist_sha256=(
            _sha256(circuit_path) if circuit_path is not None else None
        ),
    )
    if resume and (output_path.exists() or output_path.is_symlink()):
        if output_path.is_symlink() or not output_path.is_file():
            raise ValidationError("packed-netlist resume contract is not a regular file")
        existing = read_json(output_path)
        # Re-extract from the current native inputs before trusting a retained
        # contract. Its absolute source path records original provenance, not
        # runtime identity: relocating identical bytes must not rewrite the
        # contract sealed by an already completed route/checker certificate.
        source = existing.get("source") if isinstance(existing, dict) else None
        old_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(old_path, str) or not Path(old_path).is_absolute():
            raise ValidationError("packed-netlist resume source path is invalid")
        expected = {**value, "source": {**value["source"], "path": old_path}}
        if existing != expected:
            raise ValidationError("packed-netlist resume contract disagrees with native inputs")
        # Keep the original bytes, including formatting; the downstream route
        # gate independently verifies their original SHA-256 certificate.
    else:
        write_json(output_path, value)
    return {
        **report,
        "provider": "emuflow-cpp-vpr-packed-netlist-importer",
        "output": str(output_path.resolve()),
        "source_sha256": value["source"]["sha256"],
    }
