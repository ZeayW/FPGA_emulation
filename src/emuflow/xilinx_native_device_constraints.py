"""Source-sealed native Xilinx dedicated-wire and region-capacity facts.

The artifact validated here is deliberately smaller than a second device
database. Dedicated carry, DSP, BRAM, and URAM edges are encoded as chains of
ArchitectureDB site names. Every consecutive pair was proven by the exporter
through the complete primitive-family vector of dedicated SitePins and
identical canonical RapidWright Nodes. The digest of those native proofs is
retained without copying the complete native routing graph into the repository.
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
    "emuflow.xilinx-native-device-constraints/v2"
)
NATIVE_DEDICATED_NODE_PROOF = (
    "rapidwright-dedicated-sitepin-vector-same-canonical-node-v1"
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
_FAMILY_CONTRACTS = {
    "BRAM_CASCADE": ("ramb36e2-all-cascade-sitepins-v1", {"RAMB36E2"}),
    "CARRY_NEXT": ("carry8-co7-ci-all-v1", {"CARRY8"}),
    "DSP_CASCADE": ("dsp48e2-all-cascade-sitepins-v1", {"DSP_ALU"}),
    "URAM_CASCADE": ("uram288-all-cascade-sitepins-v1", {"URAM_288K_INST"}),
}


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
    for kind in _DEDICATED_KINDS:
        if checked[f"dedicated_adjacency.{kind}"] not in {
            "native_supported", "core_missing"
        }:
            raise ValidationError(
                f"{kind} must be native-supported or explicitly core-missing"
            )
    if checked["clock_region_site_capacity"] != "native_supported":
        raise ValidationError("clock-region capacity must be native-supported")
    if checked["slr_site_capacity"] != "native_supported":
        raise ValidationError("SLR capacity must be native-supported")
    if checked["half_column_clock_capacity"] != "unverified":
        raise ValidationError(
            "half-column clock capacity must remain fail-closed/unverified"
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
    adjacency_kinds = []
    edge_counts = {kind: 0 for kind in _DEDICATED_KINDS}
    for family_index, adjacency in enumerate(raw_adjacency):
        context = f"dedicated_adjacency[{family_index}]"
        if not isinstance(adjacency, dict) or set(adjacency) != {
            "chains", "edge_count", "endpoint_contract", "kind",
            "native_proof_sha256", "proof_method",
        }:
            raise ValidationError(f"{context}: invalid native family")
        kind = _string(adjacency.get("kind"), f"{context}.kind")
        if kind not in _FAMILY_CONTRACTS or kind in adjacency_kinds:
            raise ValidationError(f"{context}: unknown or duplicate kind")
        adjacency_kinds.append(kind)
        expected_contract, required_bels = _FAMILY_CONTRACTS[kind]
        if adjacency["endpoint_contract"] != expected_contract:
            raise ValidationError(f"{context}: endpoint contract is invalid")
        if adjacency["proof_method"] != NATIVE_DEDICATED_NODE_PROOF:
            raise ValidationError(f"{context}: proof method is invalid")
        _hex(adjacency["native_proof_sha256"], 64, f"{kind} native proof")
        if capabilities[f"dedicated_adjacency.{kind}"] != "native_supported":
            raise ValidationError(f"{context}: core-missing family contains edges")
        chains = adjacency["chains"]
        if not isinstance(chains, list) or not chains:
            raise ValidationError(f"{context}: chains are missing")
        canonical_chains = []
        seen_sites = set()
        observed_edges = 0
        for chain_index, chain in enumerate(chains):
            chain_context = f"{kind}.chains[{chain_index}]"
            if (
                not isinstance(chain, list)
                or len(chain) < 2
                or not all(isinstance(site, str) and site for site in chain)
            ):
                raise ValidationError(
                    f"{chain_context}: expected at least two site names"
                )
            if len(set(chain)) != len(chain):
                raise ValidationError(f"{chain_context}: repeated site")
            chain_slrs = set()
            for site_name in chain:
                site = architecture_sites.get(site_name)
                if site is None:
                    raise ValidationError(
                        f"{chain_context}: unknown site {site_name!r}"
                    )
                if not required_bels.issubset(_site_bels(architecture, site)):
                    raise ValidationError(
                        f"{chain_context}: site {site_name!r} lacks the "
                        f"required {kind} BEL contract"
                    )
                if site_name in seen_sites:
                    raise ValidationError(
                        f"{chain_context}: site belongs to multiple chains"
                    )
                seen_sites.add(site_name)
                chain_slrs.add(site["physical_region"]["slr"])
            if len(chain_slrs) != 1:
                raise ValidationError(f"{chain_context}: chain crosses SLRs")
            canonical_chains.append(tuple(chain))
            observed_edges += len(chain) - 1
        if canonical_chains != sorted(canonical_chains):
            raise ValidationError(f"{kind} chains are not canonical")
        if _nonnegative_integer(
            adjacency["edge_count"], f"{kind}.edge_count"
        ) != observed_edges:
            raise ValidationError(f"{kind} edge count is inconsistent")
        edge_counts[kind] = observed_edges
    if adjacency_kinds != sorted(adjacency_kinds):
        raise ValidationError("native constraint families are not canonical")
    for kind in _DEDICATED_KINDS:
        status = capabilities[f"dedicated_adjacency.{kind}"]
        if status == "native_supported" and kind not in adjacency_kinds:
            raise ValidationError(f"native-supported {kind} has no edge family")
        if status == "core_missing" and edge_counts[kind] != 0:
            raise ValidationError(f"core-missing {kind} contains native edges")
    edge_count = sum(edge_counts.values())

    summary = payload.get("summary")
    expected_summary = {
        "capacity_buckets": len(expected_capacity),
        "clock_regions": len({key[1] for key in expected_capacity}),
        "dedicated_edges": edge_count,
        "dedicated_edges_by_kind": edge_counts,
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
