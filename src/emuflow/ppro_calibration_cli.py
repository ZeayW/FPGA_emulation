"""Command-line entry point for redacted PPro calibration artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .io import read_json, write_json
from .open_transport_characterization import (
    fit_open_transport_cost_model,
    generate_open_transport_matrix,
    read_open_transport_observations,
    run_open_transport_matrix,
    validate_open_transport_matrix,
)
from .ppro_blackbox_application import generate_application_holdout_bundle
from .ppro_blackbox_calibration import EVALUATED_OUTCOMES, validate_redacted_artifact
from .ppro_blackbox_campaign import (
    PProCampaignRuntime,
    discover_generated_bundles,
    execute_generated_campaign,
    prune_empty_bundle_tree,
)
from .ppro_blackbox_ppro_adapter import PPRO_2026_REPORT_PROFILE
from .ppro_blackbox_constraints import parse_logical_targets
from .ppro_blackbox_communication import (
    generate_communication_matrix,
    generate_communication_probe_bundle,
)
from .ppro_blackbox_microbench import CAPACITY_AXES, generate_capacity_matrix
from .ppro_blackbox_runner import execute_blackbox_case, validate_run_spec
from .ppro_blackbox_runtime import (
    PProRuntimeConfig,
    parse_fpga_aliases,
    render_ppro_runtime_binding,
)
from .ppro_blackbox_smoke import generate_connected_smoke_bundle
from .ppro_blackbox_stage3 import fit_capacity_intervals, fit_effective_topology
from .ppro_blackbox_stage4 import (
    fit_latency_model,
    fit_payload_intervals,
    fit_transport_cost_model,
)
from .ppro_blackbox_topology import generate_ordered_pair_matrix
from .ppro_calibrated_platform import (
    generate_calibrated_platform_profiles,
    validate_calibrated_platform_bundle,
    write_calibrated_platform_profiles,
)
from .ppro_holdout_validation import evaluate_holdout_promotion


def _read_many(paths: Sequence[Path]) -> list[Any]:
    return [read_json(path.resolve()) for path in paths]


def _write_result(path: Path, value: Any) -> None:
    write_json(path.resolve(), value, compact=True)


def _campaign_status(outcomes: dict[str, int], case_count: int) -> str:
    evaluated_count = sum(
        count for outcome, count in outcomes.items() if outcome in EVALUATED_OUTCOMES
    )
    return "pass" if case_count > 0 and evaluated_count == case_count else "failed"


def _ordered_pair(value: str) -> tuple[int, int]:
    source, separator, sink = value.partition(":")
    if not separator or not source.isdigit() or not sink.isdigit():
        raise argparse.ArgumentTypeError("ordered pair must use SOURCE:SINK integers")
    return int(source), int(sink)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emuflow-ppro-calibration",
        description="Generate, fit, and validate redacted PPro black-box calibration artifacts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate_artifact = commands.add_parser("validate-artifact")
    validate_artifact.add_argument("artifact", type=Path)

    validate_spec = commands.add_parser("validate-run-spec")
    validate_spec.add_argument("run_spec", type=Path)

    smoke = commands.add_parser("generate-smoke")
    smoke.add_argument("--out", type=Path, required=True)
    smoke.add_argument("--campaign-id", required=True)
    smoke.add_argument("--case-id", default="connected-smoke-w32-d8")
    smoke.add_argument("--public-prior-id", default="lx2-public-prior-v1")
    smoke.add_argument("--configuration-id", required=True)
    smoke.add_argument("--tool-release", required=True)
    smoke.add_argument("--runner-revision", required=True)
    smoke.add_argument("--seed", type=int, default=1)
    smoke.add_argument("--width", type=int, default=32)
    smoke.add_argument("--pipeline-stages", type=int, default=8)
    smoke.add_argument(
        "--adapter-profile",
        choices=["mock-ordinary-reports-v1", PPRO_2026_REPORT_PROFILE],
        default=PPRO_2026_REPORT_PROFILE,
    )

    def add_generation_identity(command: argparse.ArgumentParser) -> None:
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--campaign-id", required=True)
        command.add_argument("--public-prior-id", default="lx2-public-prior-v1")
        command.add_argument("--configuration-id", required=True)
        command.add_argument("--tool-release", required=True)
        command.add_argument("--runner-revision", required=True)
        command.add_argument("--seed-base", type=int, default=1)
        command.add_argument(
            "--adapter-profile",
            choices=["mock-ordinary-reports-v1", PPRO_2026_REPORT_PROFILE],
            default=PPRO_2026_REPORT_PROFILE,
        )

    capacity_matrix = commands.add_parser("generate-capacity-matrix")
    add_generation_identity(capacity_matrix)
    capacity_matrix.add_argument("--axes", nargs="+", choices=sorted(CAPACITY_AXES), required=True)
    capacity_matrix.add_argument("--fit-units", nargs="+", type=int, required=True)
    capacity_matrix.add_argument("--holdout-units", nargs="+", type=int, required=True)
    capacity_matrix.add_argument("--repeats", type=int, default=2)

    topology_matrix = commands.add_parser("generate-topology-matrix")
    add_generation_identity(topology_matrix)
    topology_matrix.add_argument("--fpga-count", type=int, required=True)
    topology_matrix.add_argument(
        "--holdout-pair",
        action="append",
        type=_ordered_pair,
        required=True,
        help="directed pair SOURCE:SINK receiving independent holdout repeats",
    )
    topology_matrix.add_argument("--repeats", type=int, default=2)
    topology_matrix.add_argument("--width", type=int, default=32)
    topology_matrix.add_argument("--pipeline-stages", type=int, default=8)

    communication = commands.add_parser("generate-communication-probe")
    add_generation_identity(communication)
    communication.add_argument(
        "--kind", choices=["payload_capacity", "latency", "transport_cost"], required=True
    )
    communication.add_argument("--fpga-count", type=int, required=True)
    communication.add_argument("--source-index", type=int, required=True)
    communication.add_argument("--sink-indices", nargs="+", type=int, required=True)
    communication.add_argument("--width", type=int, required=True)
    communication.add_argument("--flow-count", type=int, default=1)
    communication.add_argument("--bidirectional", action="store_true")
    communication.add_argument("--local-baseline", action="store_true")
    communication.add_argument("--forced-tdm-ratio", type=int, default=0)
    communication.add_argument("--repeat", type=int, default=0)
    communication.add_argument("--role", choices=["fit", "holdout"], required=True)

    communication_matrix = commands.add_parser("generate-communication-matrix")
    add_generation_identity(communication_matrix)
    communication_matrix.add_argument(
        "--kind", choices=["payload_capacity", "latency", "transport_cost"], required=True
    )
    communication_matrix.add_argument("--fpga-count", type=int, required=True)
    communication_matrix.add_argument("--source-index", type=int, required=True)
    communication_matrix.add_argument("--sink-indices", nargs="+", type=int, required=True)
    communication_matrix.add_argument("--fit-widths", nargs="+", type=int, required=True)
    communication_matrix.add_argument("--holdout-widths", nargs="+", type=int, required=True)
    communication_matrix.add_argument("--flow-counts", nargs="+", type=int, required=True)
    communication_matrix.add_argument("--bidirectional", action="store_true")
    communication_matrix.add_argument("--repeats", type=int, default=2)

    application = commands.add_parser("generate-application-holdout")
    add_generation_identity(application)
    application.add_argument("--benchmark-run", type=Path, required=True)
    application.add_argument("--source-root", type=Path, required=True)

    run_case = commands.add_parser("run-ppro-case")
    run_case.add_argument("--run-spec", type=Path, required=True)
    run_case.add_argument("--filelist", type=Path, required=True)
    run_case.add_argument("--case-dir", type=Path, required=True)
    run_case.add_argument("--install-root", type=Path, required=True)
    run_case.add_argument("--platform-reference", type=Path, required=True)
    run_case.add_argument("--documented-constraints", type=Path, required=True)
    run_case.add_argument("--fpga-alias", action="append", default=[], required=True)
    run_case.add_argument("--logical-target", action="append", default=[], required=True)
    run_case.add_argument("--max-processes", type=int, default=4)
    run_case.add_argument("--utilization-limit-percent", type=int, default=75)
    run_case.add_argument("--timeout-seconds", type=float, default=21600.0)
    run_case.add_argument("--keep-raw-project", action="store_true")

    run_campaign = commands.add_parser("run-ppro-campaign")
    run_campaign.add_argument("--bundle-root", type=Path, required=True)
    run_campaign.add_argument("--result-root", type=Path, required=True)
    run_campaign.add_argument("--install-root", type=Path, required=True)
    run_campaign.add_argument("--platform-reference", type=Path, required=True)
    run_campaign.add_argument("--fpga-alias", action="append", default=[], required=True)
    run_campaign.add_argument("--logical-target", action="append", default=[], required=True)
    run_campaign.add_argument("--max-workers", type=int, default=1)
    run_campaign.add_argument("--max-processes-per-case", type=int, default=4)
    run_campaign.add_argument("--maximum-cases", type=int, default=1000)
    run_campaign.add_argument("--utilization-limit-percent", type=int, default=75)
    run_campaign.add_argument("--timeout-seconds", type=float, default=21600.0)

    for name in ("fit-capacity", "fit-topology", "fit-payload", "fit-transport"):
        command = commands.add_parser(name)
        command.add_argument("--observations", nargs="+", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
    latency = commands.add_parser("fit-latency")
    latency.add_argument("--observations", nargs="+", type=Path, required=True)
    latency.add_argument("--out", type=Path, required=True)

    open_matrix = commands.add_parser("generate-open-transport-matrix")
    open_matrix.add_argument("--out", type=Path, required=True)
    run_open_matrix = commands.add_parser("run-open-transport-matrix")
    run_open_matrix.add_argument("--matrix", type=Path, required=True)
    run_open_matrix.add_argument("--out", type=Path, required=True)
    run_open_matrix.add_argument("--work-root", type=Path, required=True)
    run_open_matrix.add_argument("--yosys", required=True)
    fit_open_transport = commands.add_parser("fit-open-transport")
    fit_open_transport.add_argument("--observations", nargs="+", type=Path, required=True)
    fit_open_transport.add_argument("--out", type=Path, required=True)

    generate = commands.add_parser("generate-platform")
    generate.add_argument("--prior", type=Path, required=True)
    generate.add_argument("--configuration-id", required=True)
    generate.add_argument("--capacity-fit", type=Path, required=True)
    generate.add_argument("--topology-fit", type=Path, required=True)
    generate.add_argument("--payload-fit", type=Path, required=True)
    generate.add_argument("--latency-fit", type=Path, required=True)
    generate.add_argument("--transport-fit", type=Path, required=True)
    generate.add_argument("--aggressive-fabric-clock-mhz", type=float, required=True)
    generate.add_argument("--nominal-fabric-clock-mhz", type=float, required=True)
    generate.add_argument("--conservative-fabric-clock-mhz", type=float, required=True)
    generate.add_argument("--out", type=Path, required=True)

    validate_bundle = commands.add_parser("validate-platform")
    validate_bundle.add_argument("bundle", type=Path)
    holdouts = commands.add_parser("evaluate-holdouts")
    holdouts.add_argument("--results", nargs="+", type=Path, required=True)
    holdouts.add_argument("--out", type=Path, required=True)
    return parser


def _dispatch(args: argparse.Namespace) -> Any:
    if args.command == "validate-artifact":
        return validate_redacted_artifact(read_json(args.artifact.resolve()))
    if args.command == "validate-run-spec":
        return validate_run_spec(read_json(args.run_spec.resolve()))
    if args.command == "generate-smoke":
        bundle = generate_connected_smoke_bundle(
            args.out,
            campaign_id=args.campaign_id,
            case_id=args.case_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed=args.seed,
            width=args.width,
            pipeline_stages=args.pipeline_stages,
            adapter_profile=args.adapter_profile,
        )
        return {"status": "pass", "run_spec": bundle.run_spec}
    if args.command == "generate-capacity-matrix":
        bundles = generate_capacity_matrix(
            args.out,
            axes=args.axes,
            fit_units=args.fit_units,
            holdout_units=args.holdout_units,
            repeats=args.repeats,
            campaign_id=args.campaign_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed_base=args.seed_base,
            adapter_profile=args.adapter_profile,
        )
        return {"status": "pass", "case_count": len(bundles)}
    if args.command == "generate-topology-matrix":
        bundles = generate_ordered_pair_matrix(
            args.out,
            fpga_count=args.fpga_count,
            holdout_pairs=args.holdout_pair,
            repeats=args.repeats,
            width=args.width,
            pipeline_stages=args.pipeline_stages,
            campaign_id=args.campaign_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed_base=args.seed_base,
            adapter_profile=args.adapter_profile,
        )
        return {"status": "pass", "case_count": len(bundles)}
    if args.command == "generate-communication-probe":
        bundle = generate_communication_probe_bundle(
            args.out,
            kind=args.kind,
            fpga_count=args.fpga_count,
            source_index=args.source_index,
            sink_indices=args.sink_indices,
            width=args.width,
            flow_count=args.flow_count,
            bidirectional=args.bidirectional,
            local_baseline=args.local_baseline,
            forced_tdm_ratio=args.forced_tdm_ratio,
            repeat=args.repeat,
            role=args.role,
            campaign_id=args.campaign_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed=args.seed_base + args.repeat,
            adapter_profile=args.adapter_profile,
        )
        return {"status": "pass", "case_id": bundle.run_spec["identity"]["id"]}
    if args.command == "generate-communication-matrix":
        bundles = generate_communication_matrix(
            args.out,
            kind=args.kind,
            fpga_count=args.fpga_count,
            source_index=args.source_index,
            sink_indices=args.sink_indices,
            fit_widths=args.fit_widths,
            holdout_widths=args.holdout_widths,
            flow_counts=args.flow_counts,
            bidirectional=args.bidirectional,
            repeats=args.repeats,
            campaign_id=args.campaign_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed_base=args.seed_base,
            adapter_profile=args.adapter_profile,
        )
        return {"status": "pass", "case_count": len(bundles)}
    if args.command == "generate-application-holdout":
        bundle = generate_application_holdout_bundle(
            args.out,
            benchmark_run_path=args.benchmark_run,
            source_root=args.source_root,
            campaign_id=args.campaign_id,
            public_prior_id=args.public_prior_id,
            configuration_id=args.configuration_id,
            tool_release=args.tool_release,
            runner_revision=args.runner_revision,
            seed=args.seed_base,
        )
        return {"status": "pass", "case_id": bundle.run_spec["identity"]["id"]}
    if args.command == "run-ppro-case":
        spec = read_json(args.run_spec.resolve())
        binding = render_ppro_runtime_binding(
            spec,
            source_filelist=args.filelist.resolve(),
            config=PProRuntimeConfig(
                case_dir=args.case_dir,
                install_root=args.install_root,
                platform_reference=args.platform_reference,
                documented_constraints=args.documented_constraints,
                fpga_aliases=parse_fpga_aliases(args.fpga_alias),
                logical_targets=parse_logical_targets(args.logical_target),
                max_processes=args.max_processes,
                utilization_limit_percent=args.utilization_limit_percent,
                timeout_seconds=args.timeout_seconds,
                keep_raw_project=args.keep_raw_project,
            ),
        )
        result = execute_blackbox_case(spec, binding)
        return {
            "status": "pass" if result["execution"]["outcome"] == "pass" else "failed",
            "case_id": result["identity"]["id"],
            "outcome": result["execution"]["outcome"],
            "observation": binding.output_path.name,
        }
    if args.command == "run-ppro-campaign":
        bundles = discover_generated_bundles(
            args.bundle_root, maximum_cases=args.maximum_cases
        )
        results = execute_generated_campaign(
            bundles,
            runtime=PProCampaignRuntime(
                result_root=args.result_root,
                install_root=args.install_root,
                platform_reference=args.platform_reference,
                fpga_aliases=parse_fpga_aliases(args.fpga_alias),
                logical_targets=parse_logical_targets(args.logical_target),
                max_processes_per_case=args.max_processes_per_case,
                utilization_limit_percent=args.utilization_limit_percent,
                timeout_seconds=args.timeout_seconds,
            ),
            max_workers=args.max_workers,
        )
        prune_empty_bundle_tree(args.bundle_root)
        outcomes: dict[str, int] = {}
        for result in results:
            outcome = str(result["execution"]["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "status": _campaign_status(outcomes, len(results)),
            "case_count": len(results),
            "outcomes": outcomes,
        }
    if args.command == "fit-capacity":
        result = fit_capacity_intervals(_read_many(args.observations))
        _write_result(args.out, result)
        return {"status": "pass", "output": args.out.name, "schema": result["schema"]}
    if args.command == "fit-topology":
        result = fit_effective_topology(_read_many(args.observations))
        _write_result(args.out, result)
        return {"status": "pass", "output": args.out.name, "schema": result["schema"]}
    if args.command == "fit-payload":
        result = fit_payload_intervals(_read_many(args.observations))
        _write_result(args.out, result)
        return {"status": "pass", "output": args.out.name, "schema": result["schema"]}
    if args.command == "fit-latency":
        result = fit_latency_model(_read_many(args.observations))
        _write_result(args.out, result)
        return {"status": "pass", "output": args.out.name, "schema": result["schema"]}
    if args.command == "generate-open-transport-matrix":
        result = generate_open_transport_matrix()
        _write_result(args.out, result)
        return {
            "status": "pass",
            "output": args.out.name,
            "case_count": len(result["cases"]),
            "schema": result["schema"],
        }
    if args.command == "run-open-transport-matrix":
        matrix = read_json(args.matrix.resolve())
        validate_open_transport_matrix(matrix)
        return run_open_transport_matrix(
            matrix,
            output_root=args.out.resolve(),
            work_root=args.work_root.resolve(),
            yosys_executable=args.yosys,
        )
    if args.command == "fit-open-transport":
        result = fit_open_transport_cost_model(
            read_open_transport_observations(args.observations)
        )
        _write_result(args.out, result)
        return {
            "status": "pass" if result["all_resources_identifiable"] else "failed",
            "output": args.out.name,
            "schema": result["schema"],
        }
    if args.command == "fit-transport":
        result = fit_transport_cost_model(_read_many(args.observations))
        _write_result(args.out, result)
        return {"status": "pass", "output": args.out.name, "schema": result["schema"]}
    if args.command == "generate-platform":
        bundle = generate_calibrated_platform_profiles(
            prior=read_json(args.prior.resolve()),
            configuration_id=args.configuration_id,
            capacity_fit=read_json(args.capacity_fit.resolve()),
            topology_fit=read_json(args.topology_fit.resolve()),
            payload_fit=read_json(args.payload_fit.resolve()),
            latency_fit=read_json(args.latency_fit.resolve()),
            transport_fit=read_json(args.transport_fit.resolve()),
            fabric_clock_mhz={
                "aggressive": args.aggressive_fabric_clock_mhz,
                "nominal": args.nominal_fabric_clock_mhz,
                "conservative": args.conservative_fabric_clock_mhz,
            },
        )
        write_calibrated_platform_profiles(args.out, bundle)
        return validate_calibrated_platform_bundle(args.out)
    if args.command == "validate-platform":
        return validate_calibrated_platform_bundle(args.bundle.resolve())
    if args.command == "evaluate-holdouts":
        result = evaluate_holdout_promotion(_read_many(args.results))
        _write_result(args.out, result)
        return {
            "status": result["status"],
            "promoted": result["promoted"],
            "output": args.out.name,
        }
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = _dispatch(args)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 1 if result.get("status") in {"failed", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
