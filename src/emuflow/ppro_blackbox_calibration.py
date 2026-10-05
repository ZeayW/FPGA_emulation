"""Strict public-prior and normalized-observation contracts for PPro calibration.

This module deliberately models PPro as an executable black box.  It accepts
public product facts and compact values from ordinary tool reports; it never
accepts vendor database paths, raw report bodies, commands, credentials, or
undocumented configuration.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Dict, Mapping, Sequence
from urllib.parse import urlparse

from .errors import ValidationError


PUBLIC_PRIOR_SCHEMA = "emuflow.ppro-public-platform-prior/v1"
OBSERVATION_SCHEMA = "emuflow.ppro-blackbox-observation/v1"

PROVENANCE_CLASSES = {
    "public_spec",
    "black_box_observation",
    "black_box_fitted",
    "research_assumption",
    "not_identifiable",
}
FIT_ROLES = {"fit", "holdout"}
EXPERIMENT_KINDS = {
    "reproducibility",
    "resource_capacity",
    "topology_reachability",
    "payload_capacity",
    "latency",
    "transport_cost",
    "application_holdout",
}
CONTROL_MODES = {
    "fixed_assignment",
    "fixed_communication",
    "free_optimization",
    "none",
}
DOCUMENTED_ACTIONS = {
    "partition_constraint",
    "net_route_constraint",
    "tdm_ratio_constraint",
    "random_seed",
}
OUTCOMES = {
    "pass",
    "capacity_infeasible",
    "link_capacity_infeasible",
    "routing_infeasible",
    "tool_failure",
    "infrastructure_failure",
    "license_failure",
    "missing_report",
    "report_parse_failure",
}
EVALUATED_OUTCOMES = {
    "pass",
    "capacity_infeasible",
    "link_capacity_infeasible",
    "routing_infeasible",
}
REPORT_NAMES = {
    "resource_summary",
    "partition_summary",
    "route_summary",
    "system_timing",
}
RESOURCE_NAMES = {
    "clb_lut",
    "clb_ff",
    "bram_kib",
    "uram_kib",
    "dsp",
    "user_io",
    "gty",
}
_OFFICIAL_SOURCE_HOSTS = {
    "amd.com",
    "www.amd.com",
    "docs.amd.com",
    "s2ceda.com",
    "www.s2ceda.com",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HDL_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FPGA_RE = re.compile(r"^F[0-9]+$")
_PARTITION_RE = re.compile(r"^P[0-9]+$")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PRIVATE_NETWORK_RE = re.compile(
    r"(?:^|[^0-9])(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)"
)
_FORBIDDEN_KEYS = {
    "authorization_id",
    "boarddb",
    "command",
    "credential",
    "install_path",
    "license_server",
    "password",
    "pin_map",
    "raw_report",
    "server_path",
    "stf",
    "timing_table",
    "token",
}


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context}: expected an object")
    return value


def _sequence(value: Any, context: str, *, nonempty: bool = False) -> Sequence[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = "non-empty array" if nonempty else "array"
        raise ValidationError(f"{context}: expected a {suffix}")
    return value


def _strict_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], context: str
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValidationError(f"{context}: missing fields {sorted(missing)}")
    if unknown:
        raise ValidationError(f"{context}: unknown fields {sorted(unknown)}")


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: invalid stable identifier")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value.strip()


def _hdl_identifier(value: Any, context: str) -> str:
    text = _string(value, context)
    if not _HDL_ID_RE.fullmatch(text):
        raise ValidationError(f"{context}: invalid HDL identifier")
    return text


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValidationError(f"{context}: expected a lowercase SHA-256 digest")
    return value


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{context}: expected an integer >= {minimum}")
    return value


def _number(value: Any, context: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValidationError(f"{context}: expected a finite number >= {minimum}")
    return result


def _date(value: Any, context: str) -> str:
    text = _string(value, context)
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise ValidationError(f"{context}: expected YYYY-MM-DD") from error
    return text


def _official_uri(value: Any, context: str) -> str:
    uri = _string(value, context)
    parsed = urlparse(uri)
    if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_SOURCE_HOSTS:
        raise ValidationError(f"{context}: expected an allowlisted official HTTPS URI")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValidationError(f"{context}: URI contains disallowed components")
    return uri


def _reject_sensitive_content(value: Any, context: str = "artifact") -> None:
    """Defense-in-depth scan after strict structural validation."""
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if key in _FORBIDDEN_KEYS or key.endswith("_path"):
                raise ValidationError(f"{context}: forbidden field {raw_key!r}")
            _reject_sensitive_content(child, f"{context}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_content(child, f"{context}[{index}]")
    elif isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if (
            stripped.startswith(("/", "~", "file://", "ssh://"))
            or _WINDOWS_PATH_RE.match(stripped)
            or _PRIVATE_NETWORK_RE.search(stripped)
            or "/data/" in lowered
            or "/research/" in lowered
            or "license@" in lowered
            or "bearer " in lowered
        ):
            raise ValidationError(f"{context}: sensitive path, endpoint, or secret")


def _provenance(
    value: Any,
    context: str,
    *,
    allowed: set[str],
    known_sources: set[str] | None = None,
) -> Dict[str, Any]:
    item = _mapping(value, context)
    _strict_keys(item, {"class"}, {"source_refs", "note"}, context)
    provenance_class = _string(item.get("class"), f"{context}.class")
    if provenance_class not in PROVENANCE_CLASSES or provenance_class not in allowed:
        raise ValidationError(f"{context}.class: invalid provenance claim")
    source_refs = []
    if "source_refs" in item:
        source_refs = [
            _identifier(source, f"{context}.source_refs[{index}]")
            for index, source in enumerate(
                _sequence(item["source_refs"], f"{context}.source_refs", nonempty=True)
            )
        ]
    if provenance_class == "public_spec":
        if not source_refs:
            raise ValidationError(f"{context}: public_spec requires source_refs")
        if known_sources is not None and not set(source_refs) <= known_sources:
            raise ValidationError(f"{context}: unknown public source reference")
    elif source_refs:
        raise ValidationError(f"{context}: only public_spec may cite public sources")
    normalized: Dict[str, Any] = {"class": provenance_class}
    if source_refs:
        normalized["source_refs"] = sorted(set(source_refs))
    if "note" in item:
        normalized["note"] = _string(item["note"], f"{context}.note")
    return normalized


def validate_public_platform_prior(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "prior")
    _strict_keys(
        root,
        {
            "schema",
            "prior",
            "sources",
            "device",
            "configurations",
            "interconnect_capabilities",
            "not_identifiable",
        },
        set(),
        "prior",
    )
    if root.get("schema") != PUBLIC_PRIOR_SCHEMA:
        raise ValidationError(f"prior.schema: expected {PUBLIC_PRIOR_SCHEMA!r}")

    identity = _mapping(root["prior"], "prior.prior")
    _strict_keys(
        identity,
        {"id", "family", "publication_scope", "claim_scope"},
        set(),
        "prior.prior",
    )
    normalized_identity = {
        "id": _identifier(identity["id"], "prior.prior.id"),
        "family": _string(identity["family"], "prior.prior.family"),
        "publication_scope": _string(
            identity["publication_scope"], "prior.prior.publication_scope"
        ),
        "claim_scope": _string(identity["claim_scope"], "prior.prior.claim_scope"),
    }
    if normalized_identity["publication_scope"] != "public-spec-only":
        raise ValidationError("prior.prior.publication_scope must be public-spec-only")
    if normalized_identity["claim_scope"] != "published-upper-bounds-not-boarddb":
        raise ValidationError("prior.prior.claim_scope overstates the public evidence")

    sources = []
    source_ids: set[str] = set()
    for index, raw_source in enumerate(
        _sequence(root["sources"], "prior.sources", nonempty=True)
    ):
        context = f"prior.sources[{index}]"
        source = _mapping(raw_source, context)
        _strict_keys(
            source,
            {"id", "publisher", "title", "uri", "locator", "accessed_on"},
            set(),
            context,
        )
        source_id = _identifier(source["id"], f"{context}.id")
        if source_id in source_ids:
            raise ValidationError(f"{context}.id: duplicate source")
        source_ids.add(source_id)
        sources.append(
            {
                "id": source_id,
                "publisher": _string(source["publisher"], f"{context}.publisher"),
                "title": _string(source["title"], f"{context}.title"),
                "uri": _official_uri(source["uri"], f"{context}.uri"),
                "locator": _string(source["locator"], f"{context}.locator"),
                "accessed_on": _date(source["accessed_on"], f"{context}.accessed_on"),
            }
        )

    device = _mapping(root["device"], "prior.device")
    _strict_keys(device, {"part", "resources", "provenance"}, set(), "prior.device")
    resources = []
    resource_names: set[str] = set()
    for index, raw_resource in enumerate(
        _sequence(device["resources"], "prior.device.resources", nonempty=True)
    ):
        context = f"prior.device.resources[{index}]"
        resource = _mapping(raw_resource, context)
        _strict_keys(resource, {"name", "value", "unit", "provenance"}, set(), context)
        name = _string(resource["name"], f"{context}.name")
        if name not in RESOURCE_NAMES or name in resource_names:
            raise ValidationError(f"{context}.name: unsupported or duplicate resource")
        resource_names.add(name)
        resources.append(
            {
                "name": name,
                "value": _number(resource["value"], f"{context}.value"),
                "unit": _string(resource["unit"], f"{context}.unit"),
                "provenance": _provenance(
                    resource["provenance"],
                    f"{context}.provenance",
                    allowed={"public_spec"},
                    known_sources=source_ids,
                ),
            }
        )
    normalized_device = {
        "part": _string(device["part"], "prior.device.part"),
        "resources": sorted(resources, key=lambda item: item["name"]),
        "provenance": _provenance(
            device["provenance"],
            "prior.device.provenance",
            allowed={"public_spec"},
            known_sources=source_ids,
        ),
    }

    configurations = []
    configuration_ids: set[str] = set()
    for index, raw_configuration in enumerate(
        _sequence(root["configurations"], "prior.configurations", nonempty=True)
    ):
        context = f"prior.configurations[{index}]"
        configuration = _mapping(raw_configuration, context)
        _strict_keys(
            configuration,
            {"id", "fpga_count", "published_totals", "connectors", "provenance"},
            set(),
            context,
        )
        config_id = _identifier(configuration["id"], f"{context}.id")
        if config_id in configuration_ids:
            raise ValidationError(f"{context}.id: duplicate configuration")
        configuration_ids.add(config_id)
        totals = _mapping(configuration["published_totals"], f"{context}.published_totals")
        connectors = _mapping(configuration["connectors"], f"{context}.connectors")
        if not totals or not connectors:
            raise ValidationError(f"{context}: totals and connectors must be non-empty")
        configurations.append(
            {
                "id": config_id,
                "fpga_count": _integer(configuration["fpga_count"], f"{context}.fpga_count", minimum=1),
                "published_totals": {
                    _identifier(name, f"{context}.published_totals key"): _number(
                        amount, f"{context}.published_totals.{name}"
                    )
                    for name, amount in sorted(totals.items())
                },
                "connectors": {
                    _identifier(name, f"{context}.connectors key"): _integer(
                        amount, f"{context}.connectors.{name}"
                    )
                    for name, amount in sorted(connectors.items())
                },
                "provenance": _provenance(
                    configuration["provenance"],
                    f"{context}.provenance",
                    allowed={"public_spec"},
                    known_sources=source_ids,
                ),
            }
        )
    counts = [item["fpga_count"] for item in configurations]
    if len(counts) != len(set(counts)) or counts != sorted(counts):
        raise ValidationError("prior.configurations: FPGA counts must be unique and sorted")

    capabilities = []
    capability_names: set[str] = set()
    for index, raw_capability in enumerate(
        _sequence(root["interconnect_capabilities"], "prior.interconnect_capabilities", nonempty=True)
    ):
        context = f"prior.interconnect_capabilities[{index}]"
        capability = _mapping(raw_capability, context)
        _strict_keys(
            capability,
            {"name", "statement", "published_bounds", "provenance"},
            set(),
            context,
        )
        name = _identifier(capability["name"], f"{context}.name")
        if name in capability_names:
            raise ValidationError(f"{context}.name: duplicate capability")
        capability_names.add(name)
        bounds = _mapping(capability["published_bounds"], f"{context}.published_bounds")
        if not bounds:
            raise ValidationError(f"{context}.published_bounds: expected a non-empty object")
        capabilities.append(
            {
                "name": name,
                "statement": _string(capability["statement"], f"{context}.statement"),
                "published_bounds": {
                    _identifier(bound, f"{context}.published_bounds key"): _number(
                        amount, f"{context}.published_bounds.{bound}"
                    )
                    for bound, amount in sorted(bounds.items())
                },
                "provenance": _provenance(
                    capability["provenance"],
                    f"{context}.provenance",
                    allowed={"public_spec"},
                    known_sources=source_ids,
                ),
            }
        )

    not_identifiable = [
        _identifier(item, f"prior.not_identifiable[{index}]")
        for index, item in enumerate(
            _sequence(root["not_identifiable"], "prior.not_identifiable", nonempty=True)
        )
    ]
    if len(not_identifiable) != len(set(not_identifiable)):
        raise ValidationError("prior.not_identifiable: duplicate entries")

    normalized = {
        "schema": PUBLIC_PRIOR_SCHEMA,
        "prior": normalized_identity,
        "sources": sorted(sources, key=lambda item: item["id"]),
        "device": normalized_device,
        "configurations": configurations,
        "interconnect_capabilities": sorted(capabilities, key=lambda item: item["name"]),
        "not_identifiable": sorted(not_identifiable),
    }
    _reject_sensitive_content(normalized)
    return normalized


def _normalized_metric_map(value: Any, context: str) -> Dict[str, float]:
    metrics = _mapping(value, context)
    return {
        _identifier(name, f"{context} key"): _number(amount, f"{context}.{name}")
        for name, amount in sorted(metrics.items())
    }


def validate_blackbox_observation(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "observation")
    _strict_keys(
        root,
        {
            "schema",
            "identity",
            "tool",
            "workload",
            "experiment",
            "execution",
            "reports",
            "metrics",
            "provenance",
            "derived",
        },
        set(),
        "observation",
    )
    if root.get("schema") != OBSERVATION_SCHEMA:
        raise ValidationError(f"observation.schema: expected {OBSERVATION_SCHEMA!r}")

    identity = _mapping(root["identity"], "observation.identity")
    _strict_keys(
        identity,
        {
            "id",
            "campaign_id",
            "case_id",
            "role",
            "public_prior_id",
            "configuration_id",
        },
        set(),
        "observation.identity",
    )
    role = _string(identity["role"], "observation.identity.role")
    if role not in FIT_ROLES:
        raise ValidationError("observation.identity.role: unsupported role")
    normalized_identity = {
        "id": _identifier(identity["id"], "observation.identity.id"),
        "campaign_id": _identifier(identity["campaign_id"], "observation.identity.campaign_id"),
        "case_id": _identifier(identity["case_id"], "observation.identity.case_id"),
        "role": role,
        "public_prior_id": _identifier(identity["public_prior_id"], "observation.identity.public_prior_id"),
        "configuration_id": _identifier(
            identity["configuration_id"], "observation.identity.configuration_id"
        ),
    }

    tool = _mapping(root["tool"], "observation.tool")
    _strict_keys(
        tool, {"name", "release", "runner_revision"}, set(), "observation.tool"
    )
    normalized_tool = {
        "name": _string(tool["name"], "observation.tool.name"),
        "release": _string(tool["release"], "observation.tool.release"),
        "runner_revision": _sha256(
            tool["runner_revision"], "observation.tool.runner_revision"
        ),
    }

    workload = _mapping(root["workload"], "observation.workload")
    _strict_keys(
        workload,
        {
            "generator_id",
            "generator_revision",
            "rtl_sha256",
            "parameters_sha256",
            "top_module",
        },
        set(),
        "observation.workload",
    )
    normalized_workload = {
        "generator_id": _identifier(workload["generator_id"], "observation.workload.generator_id"),
        "generator_revision": _sha256(workload["generator_revision"], "observation.workload.generator_revision"),
        "rtl_sha256": _sha256(workload["rtl_sha256"], "observation.workload.rtl_sha256"),
        "parameters_sha256": _sha256(workload["parameters_sha256"], "observation.workload.parameters_sha256"),
        "top_module": _hdl_identifier(
            workload["top_module"], "observation.workload.top_module"
        ),
    }

    experiment = _mapping(root["experiment"], "observation.experiment")
    _strict_keys(
        experiment,
        {"kind", "control_mode", "documented_actions", "constraints_sha256"},
        set(),
        "observation.experiment",
    )
    kind = _string(experiment["kind"], "observation.experiment.kind")
    control_mode = _string(experiment["control_mode"], "observation.experiment.control_mode")
    if kind not in EXPERIMENT_KINDS or control_mode not in CONTROL_MODES:
        raise ValidationError("observation.experiment: unsupported kind or control mode")
    actions = [
        _string(action, f"observation.experiment.documented_actions[{index}]")
        for index, action in enumerate(
            _sequence(experiment["documented_actions"], "observation.experiment.documented_actions")
        )
    ]
    if not set(actions) <= DOCUMENTED_ACTIONS or len(actions) != len(set(actions)):
        raise ValidationError("observation.experiment.documented_actions: unsupported or duplicate action")
    normalized_experiment = {
        "kind": kind,
        "control_mode": control_mode,
        "documented_actions": sorted(actions),
        "constraints_sha256": _sha256(
            experiment["constraints_sha256"], "observation.experiment.constraints_sha256"
        ),
    }

    execution = _mapping(root["execution"], "observation.execution")
    _strict_keys(
        execution,
        {"seed", "outcome", "failure_code", "runtime_seconds"},
        set(),
        "observation.execution",
    )
    outcome = _string(execution["outcome"], "observation.execution.outcome")
    if outcome not in OUTCOMES:
        raise ValidationError("observation.execution.outcome: unsupported outcome")
    failure_code = execution["failure_code"]
    if outcome == "pass":
        if failure_code is not None:
            raise ValidationError("observation.execution.failure_code: pass requires null")
    else:
        failure_code = _identifier(failure_code, "observation.execution.failure_code")
    normalized_execution = {
        "seed": _integer(execution["seed"], "observation.execution.seed"),
        "outcome": outcome,
        "failure_code": failure_code,
        "runtime_seconds": (
            None
            if execution["runtime_seconds"] is None
            else _number(
                execution["runtime_seconds"], "observation.execution.runtime_seconds"
            )
        ),
    }
    if outcome in EVALUATED_OUTCOMES and normalized_execution["runtime_seconds"] is None:
        raise ValidationError("observation.execution.runtime_seconds: evaluated run requires runtime")

    reports = _mapping(root["reports"], "observation.reports")
    if set(reports) != REPORT_NAMES or any(type(value) is not bool for value in reports.values()):
        raise ValidationError("observation.reports: expected exact boolean report-presence map")
    normalized_reports = {name: reports[name] for name in sorted(REPORT_NAMES)}

    metrics = _mapping(root["metrics"], "observation.metrics")
    _strict_keys(
        metrics,
        {"design", "resource_demand", "fpga_utilization", "assignments", "routes", "communication", "timing"},
        set(),
        "observation.metrics",
    )
    design = _normalized_metric_map(metrics["design"], "observation.metrics.design")
    resource_demand = _normalized_metric_map(
        metrics["resource_demand"], "observation.metrics.resource_demand"
    )
    fpga_utilization = []
    fpga_ids: set[str] = set()
    for index, raw_item in enumerate(_sequence(metrics["fpga_utilization"], "observation.metrics.fpga_utilization")):
        context = f"observation.metrics.fpga_utilization[{index}]"
        item = _mapping(raw_item, context)
        _strict_keys(item, {"fpga", "resources"}, set(), context)
        fpga = _string(item["fpga"], f"{context}.fpga")
        if not _FPGA_RE.fullmatch(fpga) or fpga in fpga_ids:
            raise ValidationError(f"{context}.fpga: invalid or duplicate logical FPGA")
        fpga_ids.add(fpga)
        resources = _normalized_metric_map(item["resources"], f"{context}.resources")
        if any(amount > 1.0 for amount in resources.values()):
            raise ValidationError(f"{context}.resources: utilization must be in [0, 1]")
        fpga_utilization.append({"fpga": fpga, "resources": resources})

    assignments = []
    partition_ids: set[str] = set()
    for index, raw_item in enumerate(_sequence(metrics["assignments"], "observation.metrics.assignments")):
        context = f"observation.metrics.assignments[{index}]"
        item = _mapping(raw_item, context)
        _strict_keys(item, {"partition", "fpga"}, set(), context)
        partition = _string(item["partition"], f"{context}.partition")
        fpga = _string(item["fpga"], f"{context}.fpga")
        if not _PARTITION_RE.fullmatch(partition) or partition in partition_ids:
            raise ValidationError(f"{context}.partition: invalid or duplicate logical partition")
        if not _FPGA_RE.fullmatch(fpga):
            raise ValidationError(f"{context}.fpga: invalid logical FPGA")
        partition_ids.add(partition)
        assignments.append({"partition": partition, "fpga": fpga})

    routes = []
    route_ids: set[str] = set()
    for index, raw_item in enumerate(_sequence(metrics["routes"], "observation.metrics.routes")):
        context = f"observation.metrics.routes[{index}]"
        item = _mapping(raw_item, context)
        _strict_keys(
            item,
            {"id", "source", "sinks", "effective_hops", "signal_count"},
            set(),
            context,
        )
        route_id = _identifier(item["id"], f"{context}.id")
        source = _string(item["source"], f"{context}.source")
        sinks = [
            _string(sink, f"{context}.sinks[{sink_index}]")
            for sink_index, sink in enumerate(_sequence(item["sinks"], f"{context}.sinks", nonempty=True))
        ]
        if route_id in route_ids or not _FPGA_RE.fullmatch(source) or any(
            not _FPGA_RE.fullmatch(sink) for sink in sinks
        ) or source in sinks or len(sinks) != len(set(sinks)):
            raise ValidationError(f"{context}: invalid vendor-neutral route")
        route_ids.add(route_id)
        routes.append(
            {
                "id": route_id,
                "source": source,
                "sinks": sorted(sinks),
                "effective_hops": _integer(item["effective_hops"], f"{context}.effective_hops", minimum=1),
                "signal_count": _integer(item["signal_count"], f"{context}.signal_count", minimum=1),
            }
        )
    communication = _normalized_metric_map(
        metrics["communication"], "observation.metrics.communication"
    )
    timing = _normalized_metric_map(metrics["timing"], "observation.metrics.timing")
    normalized_metrics = {
        "design": design,
        "resource_demand": resource_demand,
        "fpga_utilization": sorted(fpga_utilization, key=lambda item: item["fpga"]),
        "assignments": sorted(assignments, key=lambda item: item["partition"]),
        "routes": sorted(routes, key=lambda item: item["id"]),
        "communication": communication,
        "timing": timing,
    }

    provenance = _provenance(
        root["provenance"],
        "observation.provenance",
        allowed={"black_box_observation"},
    )
    derived = _mapping(root["derived"], "observation.derived")
    _strict_keys(derived, {"fit_eligible", "reason"}, set(), "observation.derived")
    if type(derived["fit_eligible"]) is not bool:
        raise ValidationError("observation.derived.fit_eligible: expected boolean")

    is_controlled = control_mode in {"fixed_assignment", "fixed_communication"}
    calculated_eligible = role == "fit" and is_controlled and outcome in EVALUATED_OUTCOMES
    expected_reason = (
        "controlled-evaluated-observation"
        if calculated_eligible
        else "holdout-not-fit"
        if role == "holdout"
        else "uncontrolled-not-fit"
        if not is_controlled
        else "non-hardware-failure"
    )
    if derived["fit_eligible"] is not calculated_eligible or derived["reason"] != expected_reason:
        raise ValidationError("observation.derived: fit eligibility claim is invalid")

    if outcome not in EVALUATED_OUTCOMES:
        if any(normalized_reports.values()) or any(
            normalized_metrics[name]
            for name in normalized_metrics
        ):
            raise ValidationError("observation: tool/provider failures cannot carry hardware metrics")
    elif outcome == "pass":
        if not normalized_reports["resource_summary"] or not normalized_reports["partition_summary"]:
            raise ValidationError("observation: passing run lacks mandatory normal reports")
        if not design or not resource_demand:
            raise ValidationError("observation: passing run lacks compact design/resource metrics")
        if kind in {"topology_reachability", "payload_capacity", "latency"}:
            if not normalized_reports["route_summary"] or not normalized_metrics["routes"]:
                raise ValidationError("observation: communication experiment lacks route evidence")
        if kind == "latency":
            if not normalized_reports["system_timing"] or "sr0_worst_cross_fpga_delay_ns" not in timing:
                raise ValidationError("observation: latency experiment lacks sr0 timing evidence")
        if kind == "application_holdout":
            if not assignments:
                raise ValidationError(
                    "observation: application holdout lacks partition assignments"
                )
            uses_multiple_fpgas = len({item["fpga"] for item in assignments}) > 1
            if uses_multiple_fpgas and (
                not normalized_reports["route_summary"]
                or not routes
                or "maximum_tdm_ratio" not in communication
            ):
                raise ValidationError(
                    "observation: multi-FPGA application holdout lacks route evidence"
                )
    else:
        expected_boundary_code = {
            "capacity_infeasible": "capacity-boundary",
            "link_capacity_infeasible": "link-capacity-boundary",
            "routing_infeasible": "routing-boundary",
        }[outcome]
        if failure_code != expected_boundary_code:
            raise ValidationError("observation: evaluated infeasibility requires its exact boundary code")

    normalized = {
        "schema": OBSERVATION_SCHEMA,
        "identity": normalized_identity,
        "tool": normalized_tool,
        "workload": normalized_workload,
        "experiment": normalized_experiment,
        "execution": normalized_execution,
        "reports": normalized_reports,
        "metrics": normalized_metrics,
        "provenance": provenance,
        "derived": {"fit_eligible": calculated_eligible, "reason": expected_reason},
    }
    _reject_sensitive_content(normalized)
    return normalized


def validate_redacted_artifact(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize one publishable Stage-1 calibration artifact."""
    schema = value.get("schema") if isinstance(value, Mapping) else None
    if schema == PUBLIC_PRIOR_SCHEMA:
        return validate_public_platform_prior(value)
    if schema == OBSERVATION_SCHEMA:
        return validate_blackbox_observation(value)
    raise ValidationError("artifact.schema: unsupported PPro calibration artifact")
