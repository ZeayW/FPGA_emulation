"""Source-sealed native Xilinx dedicated-wire and region-capacity facts.

The artifact validated here is deliberately smaller than a second device
database.  Dedicated CARRY8 edges are encoded as chains of ArchitectureDB
site names; every consecutive pair was proven by the exporter through the
primitive BEL pins, their dedicated SitePins, and one identical canonical
RapidWright Node.  The digest of those native proofs is retained without
copying the complete native routing graph into the repository.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json
from .rapidwright_provider import (
    RAPIDWRIGHT_ROUTE_BACKEND,
    load_rapidwright_provider_manifest,
    validate_rapidwright_provider_manifest,
)


XILINX_NATIVE_DEVICE_CONSTRAINTS_SCHEMA = (
    "emuflow.xilinx-native-device-constraints/v1"
)
NATIVE_DEDICATED_NODE_PROOF = (
    "rapidwright-primitive-bel-sitepin-same-canonical-node-v1"
)
CAPABILITY_STATUSES = {
    "native_supported",
    "adapter_required",
    "core_missing",
    "unverified",
}
_DEDICATED_KINDS = (
    "BRAM_CASCADE",
    "CARRY_NEXT",
    "DSP_CASCADE",
    "URAM_CASCADE",
)
_REQUIRED_CAPABILITIES = (
    "clock_region_site_capacity",
    "half_column_clock_capacity",
    "slr_site_capacity",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _nonnegative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context}: expected a non-negative integer")
    return value


def _hex(value: Any, length: int, context: str) -> str:
    text = _string(value, context).lower()
    if len(text) != length or any(char not in "0123456789abcdef" for char in text):
        raise ValidationError(f"{context}: expected {length}-digit lowercase hex")
    return text


def _site_bels(architecture: ArchitectureDB, site: Mapping[str, Any]) -> set[str]:
    bels = site.get("bels")
    if bels is None:
        templates = architecture.value.get("site_templates")
        template = (
            templates.get(site.get("template"), {})
            if isinstance(templates, dict)
            else {}
        )
        bels = template.get("bels", []) if isinstance(template, dict) else []
    if not isinstance(bels, list):
        return set()
    return {
        str(bel.get("name"))
        for bel in bels
        if isinstance(bel, dict) and isinstance(bel.get("name"), str)
    }


def _architecture_capacity(
    architecture: ArchitectureDB,
) -> Tuple[Counter[Tuple[str, str, str]], Dict[str, Mapping[str, Any]]]:
    capacity: Counter[Tuple[str, str, str]] = Counter()
    sites: Dict[str, Mapping[str, Any]] = {}
    for index, site in enumerate(architecture.value.get("sites", [])):
        if not isinstance(site, dict):
            raise ValidationError(f"arch.sites[{index}]: expected an object")
        name = _string(site.get("name"), f"arch.sites[{index}].name")
        if name in sites:
            raise ValidationError(f"ArchitectureDB has duplicate site {name!r}")
        region = site.get("physical_region")
        if not isinstance(region, dict):
            raise ValidationError(
                "native device constraints require complete ArchitectureDB "
                "SLR/clock-region membership"
            )
        slr = _string(region.get("slr"), f"arch site {name}.slr")
        clock_region = _string(
            region.get("clock_region"), f"arch site {name}.clock_region"
        )
        site_type = _string(site.get("type"), f"arch site {name}.type")
        capacity[(slr, clock_region, site_type)] += 1
        sites[name] = site
    if not sites:
        raise ValidationError("ArchitectureDB contains no sites")
    return capacity, sites


def _validate_endpoint(value: Any, *, source: bool, context: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected an object")
    expected = {
        "bel": "CARRY8",
        "bel_pin": "CO7" if source else "CIN",
        "logical_port": "CO" if source else "CI",
        "selection": {"kind": "bit", "index": 7}
        if source
        else {"kind": "all"},
        "site_pin": "COUT" if source else "CIN",
    }
    if value != expected:
        raise ValidationError(
            f"{context}: expected the authoritative CARRY8 "
            f"{'source' if source else 'target'} endpoint {expected!r}"
        )


def _validate_capabilities(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError("native constraints capabilities are missing")
    expected_keys = set(_REQUIRED_CAPABILITIES) | {
        f"dedicated_adjacency.{kind}" for kind in _DEDICATED_KINDS
    }
    if set(value) != expected_keys:
        raise ValidationError("native constraints capability matrix is incomplete")
    checked: Dict[str, str] = {}
    for name in sorted(expected_keys):
        status = value[name]
        if status not in CAPABILITY_STATUSES:
            raise ValidationError(f"capabilities.{name}: invalid status")
        checked[name] = status
    if checked["dedicated_adjacency.CARRY_NEXT"] not in {
        "native_supported",
        "core_missing",
    }:
        raise ValidationError(
            "CARRY_NEXT must be native-supported or explicitly core-missing"
        )
    if checked["clock_region_site_capacity"] != "native_supported":
        raise ValidationError("clock-region capacity must be native-supported")
    if checked["slr_site_capacity"] != "native_supported":
        raise ValidationError("SLR capacity must be native-supported")
    if checked["half_column_clock_capacity"] != "unverified":
        raise ValidationError(
            "half-column clock capacity must remain fail-closed/unverified"
        )
    for kind in ("DSP_CASCADE", "BRAM_CASCADE", "URAM_CASCADE"):
        if checked[f"dedicated_adjacency.{kind}"] != "unverified":
            raise ValidationError(
                f"{kind} cannot be claimed without a native endpoint proof"
            )
    return checked


def validate_xilinx_native_device_constraints(
    value: Mapping[str, Any],
    architecture: ArchitectureDB,
    provider_manifest: Mapping[str, Any],
    *,
    architecture_path: Path,
    provider_manifest_path: Path,
) -> Dict[str, Any]:
    """Validate schema, source seals, exact capacities, and compact chains."""
    if value.get("schema") != XILINX_NATIVE_DEVICE_CONSTRAINTS_SCHEMA:
        raise ValidationError("Xilinx native device constraints schema is invalid")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValidationError("Xilinx native device constraints payload is missing")
    payload_sha256 = _hex(
        value.get("payload_sha256"), 64, "native constraints payload_sha256"
    )
    if payload_sha256 != _canonical_sha256(payload):
        raise ValidationError("Xilinx native device constraints payload seal is invalid")

    manifest = validate_rapidwright_provider_manifest(provider_manifest)
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValidationError("native constraints source seal is missing")
    expected_source = {
        "architecture_sha256": _sha256(architecture_path),
        "device": manifest["device_identity"]["device"],
        "device_database_md5": manifest["device_database_md5"],
        "full_part": manifest["part"],
        "generator": {
            "revision": manifest["revision"],
            "version": manifest["version"],
        },
        "provider_manifest_sha256": _sha256(provider_manifest_path),
        "route_backend": RAPIDWRIGHT_ROUTE_BACKEND,
    }
    if source != expected_source:
        raise ValidationError(
            "native constraints source seal does not match ArchitectureDB/provider"
        )
    if architecture.part.lower() != manifest["part"].lower():
        raise ValidationError(
            "native constraints ArchitectureDB part does not match provider"
        )

    capabilities = _validate_capabilities(payload.get("capabilities"))
    if "half_columns" in payload or "half_column_capacity" in payload:
        raise ValidationError(
            "half-column facts are not available from the pinned native provider"
        )

    expected_capacity, architecture_sites = _architecture_capacity(architecture)
    raw_capacity = payload.get("site_capacity")
    if not isinstance(raw_capacity, list) or not raw_capacity:
        raise ValidationError("native constraints site_capacity is missing")
    observed_capacity: Counter[Tuple[str, str, str]] = Counter()
    capacity_keys = []
    for index, entry in enumerate(raw_capacity):
        context = f"site_capacity[{index}]"
        if not isinstance(entry, dict) or set(entry) != {
            "clock_region", "site_type", "sites", "slr"
        }:
            raise ValidationError(f"{context}: invalid capacity entry")
        key = (
            _string(entry.get("slr"), f"{context}.slr"),
            _string(entry.get("clock_region"), f"{context}.clock_region"),
            _string(entry.get("site_type"), f"{context}.site_type"),
        )
        count = _nonnegative_integer(entry.get("sites"), f"{context}.sites")
        if count == 0 or key in observed_capacity:
            raise ValidationError(f"{context}: zero or duplicate capacity bucket")
        observed_capacity[key] = count
        capacity_keys.append(key)
    if capacity_keys != sorted(capacity_keys):
        raise ValidationError("native constraints site_capacity is not canonical")
    if observed_capacity != expected_capacity:
        raise ValidationError(
            "native constraints site capacity does not match ArchitectureDB"
        )

    raw_adjacency = payload.get("dedicated_adjacency")
    if not isinstance(raw_adjacency, list):
        raise ValidationError("native constraints dedicated_adjacency is invalid")
    carry_status = capabilities["dedicated_adjacency.CARRY_NEXT"]
    if carry_status == "core_missing":
        if raw_adjacency:
            raise ValidationError(
                "core-missing CARRY_NEXT cannot contain claimed native edges"
            )
        adjacency = None
        edge_count = 0
    elif len(raw_adjacency) != 1:
        raise ValidationError("native constraints must contain one CARRY_NEXT family")
    else:
        adjacency = raw_adjacency[0]

    if adjacency is not None:
        if not isinstance(adjacency, dict) or set(adjacency) != {
            "chains",
            "edge_count",
            "kind",
            "native_proof_sha256",
            "proof_method",
            "source_endpoint",
            "target_endpoint",
        }:
            raise ValidationError("native constraints CARRY_NEXT family is invalid")
        if adjacency["kind"] != "CARRY_NEXT":
            raise ValidationError("native constraints adjacency kind is not CARRY_NEXT")
        if adjacency["proof_method"] != NATIVE_DEDICATED_NODE_PROOF:
            raise ValidationError("native constraints CARRY_NEXT proof method is invalid")
        _hex(adjacency["native_proof_sha256"], 64, "CARRY_NEXT native proof")
        _validate_endpoint(
            adjacency["source_endpoint"], source=True, context="source_endpoint"
        )
        _validate_endpoint(
            adjacency["target_endpoint"], source=False, context="target_endpoint"
        )
        chains = adjacency["chains"]
        if not isinstance(chains, list) or not chains:
            raise ValidationError("native constraints CARRY_NEXT chains are missing")
        canonical_chains = []
        seen_sites = set()
        edge_count = 0
        for chain_index, chain in enumerate(chains):
            context = f"CARRY_NEXT.chains[{chain_index}]"
            if (
                not isinstance(chain, list)
                or len(chain) < 2
                or not all(isinstance(site, str) and site for site in chain)
            ):
                raise ValidationError(f"{context}: expected at least two site names")
            if len(set(chain)) != len(chain):
                raise ValidationError(f"{context}: repeated site")
            chain_slrs = set()
            for site_name in chain:
                site = architecture_sites.get(site_name)
                if site is None:
                    raise ValidationError(f"{context}: unknown site {site_name!r}")
                if "CARRY8" not in _site_bels(architecture, site):
                    raise ValidationError(
                        f"{context}: site {site_name!r} has no CARRY8 BEL"
                    )
                if site_name in seen_sites:
                    raise ValidationError(
                        f"{context}: site {site_name!r} belongs to multiple chains"
                    )
                seen_sites.add(site_name)
                chain_slrs.add(site["physical_region"]["slr"])
            if len(chain_slrs) != 1:
                raise ValidationError(f"{context}: dedicated carry chain crosses SLRs")
            canonical_chains.append(tuple(chain))
            edge_count += len(chain) - 1
        if canonical_chains != sorted(canonical_chains):
            raise ValidationError(
                "native constraints CARRY_NEXT chains are not canonical"
            )
        if (
            _nonnegative_integer(adjacency["edge_count"], "CARRY_NEXT.edge_count")
            != edge_count
        ):
            raise ValidationError(
                "native constraints CARRY_NEXT edge count is inconsistent"
            )
        if edge_count == 0:
            raise ValidationError("native constraints contain no CARRY_NEXT edge")

    summary = payload.get("summary")
    expected_summary = {
        "capacity_buckets": len(expected_capacity),
        "clock_regions": len({key[1] for key in expected_capacity}),
        "dedicated_edges": edge_count,
        "sites": len(architecture_sites),
        "slrs": len({key[0] for key in expected_capacity}),
    }
    if summary != expected_summary:
        raise ValidationError("native constraints summary is inconsistent")
    return {
        "status": "pass",
        "schema": XILINX_NATIVE_DEVICE_CONSTRAINTS_SCHEMA,
        "payload_sha256": payload_sha256,
        "capabilities": capabilities,
        **expected_summary,
    }


def load_xilinx_native_device_constraints(
    path: Path,
    *,
    architecture_path: Path,
    provider_manifest_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("Xilinx native device constraints must be an object")
    architecture = ArchitectureDB.load(architecture_path)
    manifest = load_rapidwright_provider_manifest(provider_manifest_path)
    report = validate_xilinx_native_device_constraints(
        value,
        architecture,
        manifest,
        architecture_path=architecture_path,
        provider_manifest_path=provider_manifest_path,
    )
    return value, report


def require_xilinx_native_constraint_capability(
    report: Mapping[str, Any], capability: str
) -> None:
    """Fail closed unless the requested native fact is actually certified."""
    capabilities = report.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValidationError("native constraints capability report is missing")
    status = capabilities.get(capability)
    if status != "native_supported":
        raise ValidationError(
            f"native device capability {capability!r} is {status or 'missing'}, "
            "not native_supported"
        )
