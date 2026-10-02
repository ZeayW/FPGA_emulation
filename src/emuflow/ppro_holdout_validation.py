"""Blind application and complete-flow promotion gate for calibrated platforms."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .io import read_json
from .ir import EmuIR
from .multi_fpga_flow import validate_multi_fpga_flow_bundle
from .platform import Platform
from .ppro_blackbox_application import benchmark_rtl_identity
from .ppro_blackbox_calibration import validate_blackbox_observation
from .ppro_calibrated_platform import validate_calibrated_platform_bundle


HOLDOUT_RESULT_SCHEMA = "emuflow.ppro-holdout-result/v3"
PROMOTION_REPORT_SCHEMA = "emuflow.ppro-platform-promotion/v1"
_TIERS = {"medium", "diversity", "large", "large_primary", "very_large_final"}
_BENCHMARK_CLASS_TIERS = {
    "secworks_aes": "medium",
    "open_cpu": "diversity",
    "koios_compute": "large",
    "koios_dla": "large_primary",
    "nvdla": "very_large_final",
}
_PROFILES = {"aggressive", "nominal", "conservative"}
_PHYSICAL_MEMORY_POLICY = "physically-implementable-shared-memory-model-v1"
_RESOURCE_NAMES = {
    "lut": "lut",
    "ff": "ff",
    "bram18k": "bram36k",
    "dsp48": "dsp48",
    "uram288": "uram288",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _digest(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{context}: expected a lowercase SHA-256 digest")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{context}: expected a finite number")
    return result


def _nonnegative(value: Any, context: str) -> float:
    result = _number(value, context)
    if result < 0:
        raise ValidationError(f"{context}: expected a non-negative number")
    return result


def _nonnegative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a non-negative integer")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        raise ValidationError(f"{context}: expected a non-negative integer")
    return int(numeric)


def _validate_holdout_preparation(
    benchmark_class: str, preparation: Any
) -> None:
    if benchmark_class != "nvdla":
        return
    required = {
        "schema",
        "generator_id",
        "upstream_revision",
        "upstream_archive_sha256",
        "memory_policy",
        "partition_directive_replacements",
        "ram_wrapper_count",
        "ram_modeled_count",
        "generated_files",
        "source_list_sha256",
    }
    if not isinstance(preparation, Mapping) or set(preparation) != required:
        raise ValidationError(
            "NVDLA final holdout requires a complete shared-frontend preparation certificate"
        )
    if (
        preparation.get("schema") != "emuflow.nvdla-preparation/v1"
        or preparation.get("generator_id") != "nvdla-shared-frontend-v2"
        or preparation.get("memory_policy") != _PHYSICAL_MEMORY_POLICY
    ):
        raise ValidationError(
            "NVDLA final holdout requires a shared physically implementable "
            "memory model; black-box scale abstractions are not closure evidence"
        )
    revision = preparation["upstream_revision"]
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise ValidationError("NVDLA preparation has an invalid upstream revision")
    _digest(preparation["upstream_archive_sha256"], "NVDLA upstream archive")
    _digest(preparation["source_list_sha256"], "NVDLA source list")
    replacements = _nonnegative_integer(
        preparation["partition_directive_replacements"],
        "NVDLA partition directive replacements",
    )
    wrapper_count = _nonnegative_integer(
        preparation["ram_wrapper_count"], "NVDLA RAM wrapper count"
    )
    modeled_count = _nonnegative_integer(
        preparation["ram_modeled_count"], "NVDLA modeled RAM count"
    )
    if replacements == 0 or wrapper_count == 0 or modeled_count != wrapper_count:
        raise ValidationError("NVDLA preparation has incomplete physical-memory coverage")
    generated = preparation["generated_files"]
    if not isinstance(generated, list) or len(generated) != 3:
        raise ValidationError("NVDLA preparation must seal exactly three generated files")
    generated_paths = set()
    for index, record in enumerate(generated):
        if not isinstance(record, Mapping) or set(record) != {"path", "sha256"}:
            raise ValidationError(f"NVDLA generated file {index} is invalid")
        path = record["path"]
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path in generated_paths
        ):
            raise ValidationError(f"NVDLA generated file {index} path is unsafe")
        generated_paths.add(path)
        _digest(record["sha256"], f"NVDLA generated file {index}")
    required_names = {
        "NV_NVDLA_partition_o.v",
        "nvdla_ram_wrappers.v",
        "nvdla_compat.v",
    }
    if {Path(path).name for path in generated_paths} != required_names:
        raise ValidationError("NVDLA preparation generated-file set is incomplete")


def validate_holdout_result(value: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema",
        "id",
        "workload_id",
        "benchmark_class",
        "tier",
        "algorithm_id",
        "evidence",
        "ppro",
        "emuflow",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValidationError("holdout result fields are invalid")
    if value.get("schema") != HOLDOUT_RESULT_SCHEMA:
        raise ValidationError("holdout result schema is invalid")
    for name in ("id", "workload_id", "algorithm_id"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValidationError(f"holdout result {name} is invalid")
    if value["tier"] not in _TIERS:
        raise ValidationError("holdout result tier is invalid")
    benchmark_class = value["benchmark_class"]
    if (
        benchmark_class not in _BENCHMARK_CLASS_TIERS
        or value["tier"] != _BENCHMARK_CLASS_TIERS[benchmark_class]
    ):
        raise ValidationError("holdout result benchmark class and tier disagree")
    evidence = value["evidence"]
    evidence_required = {
        "producer",
        "benchmark_run_sha256",
        "platform_manifest_sha256",
        "platform_boarddb_sha256",
        "flow_report_sha256",
        "schedule_sha256",
        "physical_flow_report_sha256",
        "qor_report_sha256",
    }
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != evidence_required
        or evidence.get("producer") != "independent-flow-bundle-assembler-v1"
    ):
        raise ValidationError("holdout result evidence is invalid")
    normalized_evidence = {
        "producer": evidence["producer"],
        **{
            name: _digest(evidence[name], f"holdout evidence {name}")
            for name in sorted(evidence_required - {"producer"})
        },
    }
    ppro = validate_blackbox_observation(value["ppro"])
    if (
        ppro["identity"]["role"] != "holdout"
        or ppro["experiment"]["kind"] != "application_holdout"
        or ppro["execution"]["outcome"] != "pass"
    ):
        raise ValidationError("holdout result requires a passing PPro application holdout")
    if (
        not ppro["reports"]["route_summary"]
        or not ppro["reports"]["system_timing"]
        or not ppro["metrics"]["routes"]
    ):
        raise ValidationError("PPro holdout lacks route or system-timing evidence")
    emuflow = value["emuflow"]
    if not isinstance(emuflow, Mapping):
        raise ValidationError("holdout EmuFlow summary is invalid")
    emuflow_required = {
        "status",
        "configuration_id",
        "rtl_sha256",
        "physical_seed",
        "resource_utilization",
        "maximum_tdm_ratio",
        "worst_cross_fpga_delay_ns",
        "busiest_pairs",
        "global_wns_ns",
        "global_tns_ns",
        "global_timing_engine",
        "phase1_7_complete",
        "macro_cycle_equivalence",
        "schedule_legality",
        "zero_unrouted_nets",
        "zero_drc_violations",
        "original_path_coverage",
    }
    if set(emuflow) != emuflow_required or emuflow.get("status") != "pass":
        raise ValidationError("holdout EmuFlow summary fields or status are invalid")
    if emuflow["configuration_id"] != ppro["identity"]["configuration_id"]:
        configuration_match = False
    else:
        configuration_match = True
    rtl_sha256 = emuflow["rtl_sha256"]
    if (
        not isinstance(rtl_sha256, str)
        or len(rtl_sha256) != 64
        or any(character not in "0123456789abcdef" for character in rtl_sha256)
        or rtl_sha256 != ppro["workload"]["rtl_sha256"]
    ):
        raise ValidationError("holdout PPro and EmuFlow RTL identities disagree")
    if emuflow["physical_seed"] != 1:
        raise ValidationError("holdout validation requires the single default physical seed 1")
    if emuflow["global_timing_engine"] != "opensta":
        raise ValidationError("holdout validation requires authoritative OpenSTA global timing")
    booleans = (
        "phase1_7_complete",
        "macro_cycle_equivalence",
        "schedule_legality",
        "zero_unrouted_nets",
        "zero_drc_violations",
    )
    if any(type(emuflow[name]) is not bool for name in booleans):
        raise ValidationError("holdout EmuFlow completion check is not boolean")
    coverage = _number(emuflow["original_path_coverage"], "holdout path coverage")
    if not 0.0 <= coverage <= 1.0:
        raise ValidationError("holdout path coverage is outside [0,1]")
    utilization = emuflow["resource_utilization"]
    if not isinstance(utilization, Mapping) or not utilization:
        raise ValidationError("holdout EmuFlow resource utilization is invalid")
    normalized_utilization = {
        str(name): _nonnegative(amount, f"holdout utilization {name}")
        for name, amount in sorted(utilization.items())
    }
    if any(amount > 1.0 for amount in normalized_utilization.values()):
        raise ValidationError("holdout EmuFlow utilization exceeds one")
    busiest_pairs = emuflow["busiest_pairs"]
    if not isinstance(busiest_pairs, list) or any(
        not isinstance(item, str) or "->" not in item for item in busiest_pairs
    ):
        raise ValidationError("holdout EmuFlow busiest-pair ordering is invalid")
    if len(busiest_pairs) != len(set(busiest_pairs)):
        raise ValidationError("holdout EmuFlow busiest-pair ordering contains duplicates")
    global_tns = _number(emuflow["global_tns_ns"], "holdout global TNS")
    if global_tns > 0:
        raise ValidationError("holdout global TNS must use the non-positive STA convention")
    normalized_emuflow = {
        **dict(emuflow),
        "configuration_match": configuration_match,
        "resource_utilization": normalized_utilization,
        "maximum_tdm_ratio": _nonnegative_integer(
            emuflow["maximum_tdm_ratio"], "holdout maximum TDM ratio"
        ),
        "worst_cross_fpga_delay_ns": _nonnegative(
            emuflow["worst_cross_fpga_delay_ns"], "holdout cross-FPGA delay"
        ),
        "global_wns_ns": _number(emuflow["global_wns_ns"], "holdout global WNS"),
        "global_tns_ns": global_tns,
        "original_path_coverage": coverage,
    }
    return {
        "schema": HOLDOUT_RESULT_SCHEMA,
        "id": value["id"],
        "workload_id": value["workload_id"],
        "benchmark_class": benchmark_class,
        "tier": value["tier"],
        "algorithm_id": value["algorithm_id"],
        "evidence": normalized_evidence,
        "ppro": ppro,
        "emuflow": normalized_emuflow,
    }


def _resource_utilization(
    resources_by_fpga: Mapping[str, Any], platform: Platform
) -> Dict[str, float]:
    if not isinstance(resources_by_fpga, Mapping) or not resources_by_fpga:
        raise ValidationError("flow partition report lacks per-FPGA resources")
    by_id = {fpga.id: fpga for fpga in platform.fpgas}
    if set(resources_by_fpga) != set(by_id):
        raise ValidationError("flow partition resource coverage disagrees with BoardDB")
    result: Dict[str, float] = {}
    for resource, observation_name in _RESOURCE_NAMES.items():
        ratios = []
        for fpga_id, usage in resources_by_fpga.items():
            if not isinstance(usage, Mapping):
                raise ValidationError("flow partition FPGA resources are invalid")
            capacity = by_id[fpga_id].capacity.get(resource, 0)
            if capacity <= 0:
                continue
            amount = _nonnegative(
                usage.get(resource, 0), f"flow {fpga_id} {resource} usage"
            )
            if amount > capacity:
                raise ValidationError("flow partition usage exceeds physical capacity")
            ratios.append(amount / capacity)
        if ratios:
            result[observation_name] = max(ratios)
    if not result:
        raise ValidationError("flow partition has no comparable calibrated resources")
    return dict(sorted(result.items()))


def _busiest_pairs(schedule: Mapping[str, Any]) -> list[str]:
    entries = schedule.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("flow schedule entries are invalid")
    loads: Dict[str, int] = defaultdict(int)
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(f"flow schedule entry {index} is invalid")
        source = entry.get("from")
        sink = entry.get("to")
        if not isinstance(source, str) or not isinstance(sink, str):
            raise ValidationError(f"flow schedule entry {index} lacks endpoints")
        loads[f"{source}->{sink}"] += 1
    return [name for name, _ in sorted(loads.items(), key=lambda item: (-item[1], item[0]))]


def _maximum_tdm_ratio(schedule: Mapping[str, Any]) -> int:
    entries = schedule.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("flow schedule entries are invalid")
    ratios = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(f"flow schedule entry {index} is invalid")
        ratios.append(
            _nonnegative_integer(
                entry.get("tdm_ratio", 1), f"flow schedule entry {index} TDM ratio"
            )
        )
    return max(ratios, default=1)


def assemble_holdout_result(
    *,
    result_id: str,
    workload_id: str,
    algorithm_id: str,
    ppro_observation_path: Path,
    flow_root: Path,
    benchmark_run_path: Path,
    source_root: Path,
    platform_bundle_root: Path,
    profile: str,
) -> Dict[str, Any]:
    """Assemble a blind result exclusively from independently checked artifacts."""

    if profile not in _PROFILES:
        raise ValidationError("holdout calibrated-platform profile is invalid")
    for name, value in (
        ("result id", result_id),
        ("workload id", workload_id),
        ("algorithm id", algorithm_id),
    ):
        if not isinstance(value, str) or not value:
            raise ValidationError(f"holdout {name} is invalid")

    ppro_path = ppro_observation_path.resolve()
    ppro = validate_blackbox_observation(read_json(ppro_path))
    identity = benchmark_rtl_identity(benchmark_run_path, source_root)
    benchmark_class = identity["calibration_holdout_class"]
    if benchmark_class not in _BENCHMARK_CLASS_TIERS:
        raise ValidationError(
            "benchmark contract lacks a valid calibration_holdout_class"
        )
    _validate_holdout_preparation(benchmark_class, identity["preparation"])
    if (
        ppro["workload"]["rtl_sha256"] != identity["rtl_sha256"]
        or ppro["workload"]["parameters_sha256"]
        != identity["parameters_sha256"]
        or ppro["workload"]["top_module"] != identity["top_module"]
    ):
        raise ValidationError("PPro observation disagrees with benchmark RTL identity")

    bundle_root = platform_bundle_root.resolve()
    bundle_validation = validate_calibrated_platform_bundle(bundle_root)
    if ppro["identity"]["configuration_id"] != bundle_validation["configuration_id"]:
        raise ValidationError("PPro observation disagrees with calibrated platform")
    manifest_path = bundle_root / "manifest.json"
    manifest = read_json(manifest_path)
    profile_record = manifest["profiles"].get(profile)
    if not isinstance(profile_record, Mapping):
        raise ValidationError("calibrated platform manifest lacks selected profile")
    boarddb_path = bundle_root / profile / "boarddb.json"
    boarddb = read_json(boarddb_path)
    if profile_record.get("boarddb") != _sha256(boarddb):
        raise ValidationError("selected calibrated BoardDB disagrees with manifest")
    platform = Platform.from_dict(boarddb)

    root = flow_root.resolve()
    validate_multi_fpga_flow_bundle(root, require_physical=True)
    flow_report_path = root / "multi-fpga-flow-report.json"
    flow_report = read_json(flow_report_path)
    artifacts = flow_report["artifacts"]
    for label in ("schedule", "physical_flow_report", "qor_report"):
        if label not in artifacts:
            raise ValidationError(f"sealed flow lacks {label} evidence")
    normalized_platform = read_json(root / artifacts["platform"]["path"])
    if Platform.from_dict(normalized_platform).to_dict() != platform.to_dict():
        raise ValidationError("flow BoardDB is not the selected calibrated profile")

    synthesis = flow_report["stages"]["frontend"].get("synthesis")
    raw_sources = synthesis.get("sources") if isinstance(synthesis, Mapping) else None
    if not isinstance(raw_sources, list) or any(
        not isinstance(path, str) for path in raw_sources
    ):
        raise ValidationError("flow frontend source identity is missing")
    flow_sources = [Path(path).resolve() for path in raw_sources]
    if flow_sources != identity["sources"]:
        raise ValidationError("flow frontend sources disagree with benchmark contract")
    raw_include_dirs = synthesis.get("include_dirs", [])
    raw_defines = synthesis.get("defines", [])
    if (
        not isinstance(raw_include_dirs, list)
        or any(not isinstance(path, str) for path in raw_include_dirs)
        or [Path(path).resolve() for path in raw_include_dirs]
        != identity["include_dirs"]
        or raw_defines != identity["defines"]
    ):
        raise ValidationError(
            "flow frontend compilation context disagrees with benchmark contract"
        )
    ir = EmuIR.load(root / artifacts["emuir"]["path"])
    if ir.value["design"]["top"] != identity["top_module"] or sorted(
        clock["id"] for clock in ir.value["clocks"]
    ) != sorted(identity["clocks"]):
        raise ValidationError("flow EmuIR top or clocks disagree with benchmark contract")

    phase3 = read_json(root / "partition/phase3_report.json")
    schedule = read_json(root / artifacts["schedule"]["path"])
    physical = read_json(root / artifacts["physical_flow_report"]["path"])
    qor = read_json(root / artifacts["qor_report"]["path"])
    execution = physical.get("execution")
    if not isinstance(execution, Mapping) or execution.get("seed") != 1:
        raise ValidationError("holdout flow requires recorded physical seed 1")

    timing = qor.get("timing")
    if not isinstance(timing, Mapping):
        raise ValidationError("holdout flow lacks global timing")
    opensta = timing.get("global_opensta")
    if (
        timing.get("timing_scope") != "whole-original-design"
        or not isinstance(opensta, Mapping)
        or opensta.get("authority") != "opensta"
        or opensta.get("execution") != "standalone"
    ):
        raise ValidationError("holdout flow lacks standalone authoritative whole-design OpenSTA")
    summary = timing.get("summary")
    target = timing.get("target_clock")
    paths = timing.get("paths")
    if not isinstance(summary, Mapping) or not isinstance(target, Mapping) or not isinstance(paths, list):
        raise ValidationError("holdout flow timing population is incomplete")
    cross_delays = [
        _nonnegative(path.get("system_delay_bound_ns"), "cross-FPGA path delay")
        for path in paths
        if isinstance(path, Mapping) and path.get("path_scope") == "cross-fpga"
    ]
    if not cross_delays:
        raise ValidationError("holdout flow has no cross-FPGA timing paths")

    runtime = flow_report.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValidationError("holdout flow runtime report is missing")
    equivalence = runtime.get("functional_equivalence")
    schedule_legality = runtime.get("schedule_legality")
    physical_records = physical.get("fpgas")
    if not isinstance(physical_records, list) or not physical_records:
        raise ValidationError("holdout flow physical FPGA evidence is missing")
    closures = [
        record.get("physical_result", {}).get("closure", {})
        for record in physical_records
        if isinstance(record, Mapping)
    ]
    if len(closures) != len(physical_records):
        raise ValidationError("holdout flow physical closure evidence is incomplete")

    result = {
        "schema": HOLDOUT_RESULT_SCHEMA,
        "id": result_id,
        "workload_id": workload_id,
        "benchmark_class": benchmark_class,
        "tier": _BENCHMARK_CLASS_TIERS[benchmark_class],
        "algorithm_id": algorithm_id,
        "evidence": {
            "producer": "independent-flow-bundle-assembler-v1",
            "benchmark_run_sha256": identity["benchmark_run_sha256"],
            "platform_manifest_sha256": _sha256_file(manifest_path),
            "platform_boarddb_sha256": profile_record["boarddb"],
            "flow_report_sha256": _sha256_file(flow_report_path),
            "schedule_sha256": artifacts["schedule"]["sha256"],
            "physical_flow_report_sha256": artifacts["physical_flow_report"]["sha256"],
            "qor_report_sha256": artifacts["qor_report"]["sha256"],
        },
        "ppro": ppro,
        "emuflow": {
            "status": "pass",
            "configuration_id": bundle_validation["configuration_id"],
            "rtl_sha256": identity["rtl_sha256"],
            "physical_seed": execution["seed"],
            "resource_utilization": _resource_utilization(
                phase3["validation"]["resources_by_fpga"], platform
            ),
            "maximum_tdm_ratio": _maximum_tdm_ratio(schedule),
            "worst_cross_fpga_delay_ns": max(cross_delays),
            "busiest_pairs": _busiest_pairs(schedule),
            "global_wns_ns": target["worst_slack_bound_ns"],
            "global_tns_ns": target["total_negative_slack_bound_ns"],
            "global_timing_engine": "opensta",
            "phase1_7_complete": flow_report.get("status") == "pass",
            "macro_cycle_equivalence": (
                isinstance(equivalence, Mapping) and equivalence.get("status") == "pass"
            ),
            "schedule_legality": (
                isinstance(schedule_legality, Mapping)
                and schedule_legality.get("status") == "pass"
                and schedule_legality.get("collisions") == 0
            ),
            "zero_unrouted_nets": all(
                closure.get("unrouted_nets") == 0 for closure in closures
            ),
            "zero_drc_violations": all(
                closure.get("drc_violations") == 0 for closure in closures
            ),
            "original_path_coverage": summary["original_path_coverage"],
        },
    }
    return validate_holdout_result(result)


def _ppro_resource_utilization(observation: Mapping[str, Any]) -> Dict[str, float]:
    records = observation["metrics"]["fpga_utilization"]
    names = sorted(set().union(*(record["resources"] for record in records))) if records else []
    return {
        name: max(record["resources"].get(name, 0.0) for record in records)
        for name in names
    }


def _ppro_pair_order(observation: Mapping[str, Any]) -> list[str]:
    loads: Dict[str, float] = defaultdict(float)
    for route in observation["metrics"]["routes"]:
        for sink in route["sinks"]:
            loads[f"{route['source']}->{sink}"] += float(route["signal_count"])
    return [name for name, _ in sorted(loads.items(), key=lambda item: (-item[1], item[0]))]


def _major_order_agreement(left: Sequence[str], right: Sequence[str]) -> bool:
    if not left or not right:
        return not left and not right
    if left[0] != right[0]:
        return False
    common = [item for item in left if item in set(right)][:3]
    right_common = [item for item in right if item in set(common)]
    return common == right_common


def evaluate_holdout_promotion(results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    normalized = [validate_holdout_result(item) for item in results]
    if not normalized:
        raise ValidationError("holdout promotion requires results")
    identities = [item["id"] for item in normalized]
    if len(identities) != len(set(identities)):
        raise ValidationError("holdout promotion contains duplicate result identities")
    workload_algorithms = [
        (item["workload_id"], item["algorithm_id"]) for item in normalized
    ]
    if len(workload_algorithms) != len(set(workload_algorithms)):
        raise ValidationError("holdout promotion contains a duplicate workload algorithm")
    cases = []
    for item in normalized:
        ppro = item["ppro"]
        emuflow = item["emuflow"]
        ppro_utilization = _ppro_resource_utilization(ppro)
        common_resources = sorted(set(ppro_utilization) & set(emuflow["resource_utilization"]))
        utilization_errors = {
            name: abs(ppro_utilization[name] - emuflow["resource_utilization"][name])
            for name in common_resources
        }
        if "maximum_tdm_ratio" not in ppro["metrics"]["communication"]:
            raise ValidationError("PPro holdout lacks maximum TDM ratio")
        ppro_tdm = _nonnegative_integer(
            ppro["metrics"]["communication"]["maximum_tdm_ratio"],
            "PPro holdout maximum TDM ratio",
        )
        ppro_delay = float(
            ppro["metrics"]["timing"].get("sr0_worst_cross_fpga_delay_ns", -1.0)
        )
        if ppro_delay < 0:
            raise ValidationError("PPro holdout lacks worst cross-FPGA delay")
        if ppro_delay:
            delay_error = abs(emuflow["worst_cross_fpga_delay_ns"] - ppro_delay) / ppro_delay
        else:
            delay_error = 0.0 if emuflow["worst_cross_fpga_delay_ns"] == 0.0 else 1.0
        complete = all(
            emuflow[name]
            for name in (
                "phase1_7_complete",
                "macro_cycle_equivalence",
                "schedule_legality",
                "zero_unrouted_nets",
                "zero_drc_violations",
            )
        ) and emuflow["original_path_coverage"] == 1.0
        ppro_pairs = _ppro_pair_order(ppro)
        case = {
            "id": item["id"],
            "workload_id": item["workload_id"],
            "benchmark_class": item["benchmark_class"],
            "tier": item["tier"],
            "algorithm_id": item["algorithm_id"],
            "configuration_match": emuflow["configuration_match"],
            "maximum_resource_utilization_error": max(utilization_errors.values(), default=None),
            "resource_comparison_available": bool(common_resources),
            "tdm_ratio_difference": abs(emuflow["maximum_tdm_ratio"] - ppro_tdm),
            "cross_fpga_delay_relative_error": delay_error,
            "busiest_pair_order_agrees": _major_order_agreement(
                ppro_pairs, emuflow["busiest_pairs"]
            ),
            "complete_phase1_7_gate": complete,
            "global_wns_ns": emuflow["global_wns_ns"],
            "global_tns_ns": emuflow["global_tns_ns"],
        }
        case["passes"] = (
            case["configuration_match"]
            and case["resource_comparison_available"]
            and case["maximum_resource_utilization_error"] <= 0.10
            and case["tdm_ratio_difference"] <= 1
            and case["cross_fpga_delay_relative_error"] <= 0.15
            and case["busiest_pair_order_agrees"]
            and case["complete_phase1_7_gate"]
        )
        cases.append(case)

    ranking_checks = []
    by_workload: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for case, item in zip(cases, normalized):
        by_workload[case["workload_id"]].append(
            {
                "algorithm_id": case["algorithm_id"],
                "ppro": item["ppro"]["metrics"]["timing"]["sr0_worst_cross_fpga_delay_ns"],
                "emuflow": item["emuflow"]["worst_cross_fpga_delay_ns"],
            }
        )
    for workload, items in sorted(by_workload.items()):
        if len(items) < 2:
            continue
        ppro_order = [item["algorithm_id"] for item in sorted(items, key=lambda entry: (entry["ppro"], entry["algorithm_id"]))]
        emuflow_order = [item["algorithm_id"] for item in sorted(items, key=lambda entry: (entry["emuflow"], entry["algorithm_id"]))]
        ranking_checks.append(
            {"workload_id": workload, "ppro_order": ppro_order, "emuflow_order": emuflow_order, "matches": ppro_order == emuflow_order}
        )
    tiers = {case["tier"] for case in cases if case["passes"]}
    benchmark_classes = {
        case["benchmark_class"] for case in cases if case["passes"]
    }
    promotion = (
        all(case["passes"] for case in cases)
        and _TIERS <= tiers
        and set(_BENCHMARK_CLASS_TIERS) <= benchmark_classes
        and bool(ranking_checks)
        and all(item["matches"] for item in ranking_checks)
    )
    return {
        "schema": PROMOTION_REPORT_SCHEMA,
        "status": "pass" if promotion else "fail",
        "promoted": promotion,
        "cases": cases,
        "ranking_checks": ranking_checks,
        "covered_passing_tiers": sorted(tiers),
        "required_tiers": sorted(_TIERS),
        "covered_passing_benchmark_classes": sorted(benchmark_classes),
        "required_benchmark_classes": sorted(_BENCHMARK_CLASS_TIERS),
    }
