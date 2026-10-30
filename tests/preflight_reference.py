"""Independent finite compile-admission contract, outside production entries."""
from __future__ import annotations
import os
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
_CompileContext=SimpleNamespace
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.io import read_json
from emuflow.common.native_tools import resolve_native_executable, native_file_generation
from emuflow.platform.platform import Platform
from emuflow.platform.board_link_timing import validate_board_link_timing
from emuflow.transport.transport_cost import validate_transport_cost_database
from emuflow.physical.openparf.openparf import validate_openparf_runtime
from emuflow.timing.opensta import check_opensta_engine_version
from emuflow.frontend.policy import VALID_XILINX_MAPPING_STRATEGIES
from emuflow.physical.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE
from emuflow.scheduling.tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from emuflow.partition.partition import CUT_MODE_STATIC_EXACT, CUT_MODE_SEQUENTIAL_ONLY
from emuflow.scheduling.timing_routing import GLOBAL_CANDIDATE_PROVIDER
from emuflow.timing.xilinx_preplacement_timing import XILINX_PREPLACEMENT_TIMING_SCHEMA
from emuflow.flow.multi_fpga_physical_flow import _backend_sources
import json
MULTI_FPGA_MAPPING_PROFILES=(XILINX_ULTRASCALEPLUS_OPEN_PROFILE,)
MULTI_FPGA_PHASE6_PROVIDERS=('chimew','baseline')


def _validate_mapping_timing_contract(
    mapping_profile: str,
    timing_db_path: Optional[Path],
) -> None:
    """Require the maintained Xilinx pre-placement contract before synthesis."""
    if mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
        raise EmuFlowError(f"unsupported multi-FPGA mapping profile {mapping_profile!r}")
    if timing_db_path is None:
        raise EmuFlowError(
            "xilinx-ultrascaleplus-open-v1 requires a source-sealed "
            "Xilinx pre-placement --architecture-timing-db"
        )
    expected_schema = XILINX_PREPLACEMENT_TIMING_SCHEMA

    observed_schema = read_json(timing_db_path).get("schema")
    if observed_schema != expected_schema:
        raise EmuFlowError(
            f"mapping profile {mapping_profile!r} requires Architecture "
            f"TimingDB schema {expected_schema!r}, got {observed_schema!r}"
        )


def _resolve_partition_openroad(
    partition_provider: str,
    executable: Optional[str],
    solution_input: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a required partition engine before producing flow artifacts."""

    if solution_input is not None:
        return executable
    if partition_provider in {"tritonpart", "patron"}:
        return resolve_native_executable("openroad", executable)
    return executable


def _compile_preflight(ctx: _CompileContext):
    if (
        isinstance(ctx.patron_algorithm_version, bool)
        or not isinstance(ctx.patron_algorithm_version, int)
        or ctx.patron_algorithm_version != 14
    ):
        raise EmuFlowError("only PATRON v14 is supported")
    if ctx.partition_provider not in {"patron", "tritonpart"}:
        raise EmuFlowError(f"unsupported Phase 3 provider {ctx.partition_provider!r}")
    if getattr(ctx, "source_checksums", None) is not None and (ctx.yosys_json is not None or ctx.prepared_frontend is not None or ctx.prepared_timing is not None):
        raise EmuFlowError("source checksums apply only to original RTL compilation")
    if ctx.optimize_frame_slots:
        raise EmuFlowError("native compile does not yet support frame search")
    if ctx.output_dir is None:
        raise EmuFlowError("multi-FPGA compilation requires an output directory")

    if ctx.global_timing_engine != "opensta":
        raise EmuFlowError("only the OpenSTA global timing engine is supported")
    if ctx.global_sta_executable is not None and not ctx.physical:
        raise EmuFlowError("global OpenSTA qualification requires --physical")
    if ctx.physical_backend not in {"rapidwright", "vivado"}:
        raise EmuFlowError(f"unsupported physical backend {ctx.physical_backend!r}")
    if ctx.mapping_profile not in MULTI_FPGA_MAPPING_PROFILES:
        raise EmuFlowError(
            "unsupported multi-FPGA mapping profile "
            f"{ctx.mapping_profile!r}; expected one of "
            f"{', '.join(MULTI_FPGA_MAPPING_PROFILES)}"
        )
    if ctx.xilinx_mapping_strategy is not None:
        if ctx.xilinx_mapping_strategy not in VALID_XILINX_MAPPING_STRATEGIES:
            raise EmuFlowError("unsupported Xilinx mapping strategy")
        if ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE or ctx.yosys_json is not None:
            raise EmuFlowError(
                "Xilinx mapping strategy requires native Xilinx RTL synthesis"
            )
    if (
        ctx.physical
        and ctx.physical_backend == "rapidwright"
        and ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE
    ):
        raise EmuFlowError(
            "physical-backend=rapidwright requires "
            "mapping-profile=xilinx-ultrascaleplus-open-v1"
        )
    if ctx.tdm_provider not in {None, TDM_EXACT_DEPENDENCY_PROVIDER}:
        raise EmuFlowError(f"unsupported Phase 5 provider {ctx.tdm_provider!r}")
    if ctx.cut_mode != CUT_MODE_STATIC_EXACT:
        raise EmuFlowError("multi-FPGA compilation requires static-exact-combinational")
    if ctx.route_provider not in {None, GLOBAL_CANDIDATE_PROVIDER}:
        raise EmuFlowError("unsupported Phase 4 routing provider; use " + GLOBAL_CANDIDATE_PROVIDER)
    if ctx.phase6_provider not in MULTI_FPGA_PHASE6_PROVIDERS:
        raise EmuFlowError(
            "unsupported Phase 6 provider "
            f"{ctx.phase6_provider!r}; expected one of "
            f"{', '.join(MULTI_FPGA_PHASE6_PROVIDERS)}"
        )
    if ctx.cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise EmuFlowError("unsupported combinational cut mode")
    ctx.exact_cut_mode = ctx.cut_mode == CUT_MODE_STATIC_EXACT
    ctx.clocks = tuple(ctx.clocks)
    if (
        ctx.partition_provider == "patron"
        and ctx.patron_algorithm_version == 14
        and not ctx.exact_cut_mode
    ):
        raise EmuFlowError(
            "PATRON v14 requires generalized Static Exact mode"
        )
    if not 2 <= ctx.phase6_chimew_region_count <= 31:
        raise EmuFlowError("--phase6-chimew-region-count must be in [2, 31]")
    if ctx.phase6_provider == "chimew":
        if (not ctx.physical or ctx.physical_backend not in {"rapidwright", "vivado"}
                or ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE):
            raise EmuFlowError("Chimew requires --physical and the Xilinx mapping with RapidWright or Vivado")
        if ctx.physical_architecture is None:
            raise EmuFlowError("Chimew requires the source-qualified Xilinx ArchitectureDB package geometry")
        from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
        from emuflow.transport.chimew.chimew_fixed_wiring import FixedWiringChannels
        board = FixedWiringChannels(Platform.load(ctx.platform_path))
        parts = {fpga.part for fpga in Platform.load(ctx.platform_path).fpgas}
        if len(parts) != 1:
            raise EmuFlowError("Chimew requires a single Xilinx device model")
        ctx.chimew_package_geometry = XilinxPackageGeometry.open(ctx.physical_architecture, next(iter(parts)))
        pins = ctx.chimew_package_geometry.pins
        for wire in board.board["wires"]:
            for end in wire["endpoints"]:
                for polarity in ("p", "n"):
                    pin = end[polarity]
                    if pins.get(pin["package_pin"], (None,))[0] != pin["site"]:
                        raise ValidationError("Chimew native package geometry does not cover the fixed P/N wiring")
    _validate_mapping_timing_contract(
        ctx.mapping_profile, ctx.architecture_timing_db
    )
    ctx.internal_timing_database = ctx.timing_paths is None
    if ctx.partition_provider == "patron" and not ctx.internal_timing_database:
        raise EmuFlowError(
            "--partition-provider patron requires the internally generated "
            "complete TimingPathDB"
        )
    if ctx.timing_paths is not None and (
        ctx.timing_driven or ctx.architecture_timing_db is not None
    ):
        raise EmuFlowError(
            "--timing-driven/--architecture-timing-db cannot be combined "
            "with externally projected --timing-paths"
        )
    if ctx.physical and ctx.timing_paths is not None:
        raise EmuFlowError(
            "physical Phase 7C requires an internally generated complete "
            "TimingPathDB; externally projected --timing-paths are only "
            "supported by non-physical algorithm tests"
        )
    if ctx.optimize_frame_slots and not ctx.timing_driven:
        raise EmuFlowError(
            "--optimize-frame-slots requires --timing-driven"
        )
    if ctx.internal_timing_database and not ctx.clock_periods:
        raise EmuFlowError(
            "multi-FPGA timing analysis requires at least one "
            "--clock-period CLOCK=PERIOD_NS; TimingPathDB remains mandatory "
            "for physical Phase 7C even with --no-timing-driven"
        )

    # These small board contracts can be checked without a synthesized graph.
    # Reject a renamed stress platform paired with the original timing/cost
    # databases before spending hours on synthesis and timing extraction.
    ctx.checked_link_timing = None
    ctx.checked_link_timing_validation = None
    if ctx.board_link_timing_db is not None or ctx.transport_cost_db is not None:
        board_contract_platform = Platform.load(ctx.platform_path)
        if ctx.board_link_timing_db is not None:
            ctx.checked_link_timing = read_json(ctx.board_link_timing_db.resolve())
            ctx.checked_link_timing_validation = validate_board_link_timing(
                ctx.checked_link_timing, board_contract_platform
            )
        if ctx.transport_cost_db is not None:
            validate_transport_cost_database(
                read_json(ctx.transport_cost_db.resolve()),
                expected_platform=board_contract_platform.name,
            )

    # Phase 7 runs after every board-independent stage, so validating its
    # interpreter inside the physical-flow helper is much too late for a
    # one-shot Phase 1--7 invocation.  Reject an unusable OpenPARF/PyTorch
    # binding before creating any frontend artifact or doing synthesis.
    physical_uses_openparf = ctx.physical and ctx.physical_backend == "rapidwright"
    if physical_uses_openparf:
        from emuflow.common.native_tools import require_native_schema
        require_native_schema("rwroute-export-request-v2")
        require_native_schema("routed-segment-request-v1")
        require_native_schema("routed-endpoint-request-v1")
        require_native_schema("routed-opensta-request-v1")
        validate_openparf_runtime(
            install_root=ctx.physical_openparf_install,
            python_executable=ctx.physical_openparf_python,
        )
    if ctx.physical and ctx.physical_backend == "vivado":
        from emuflow.common.native_tools import require_native_schema
        require_native_schema("vivado-lineage-store-v2")
        require_native_schema("vivado-export-request-v1")
    if ctx.internal_timing_database:
        from emuflow.common.native_tools import require_native_schema
        require_native_schema("opensta-input-export-request-v1")
        require_native_schema("sta-path-request-v1")
        require_native_schema("sta-weight-request-v1")
        ctx.opensta = resolve_native_executable("sta", ctx.opensta)
        check_opensta_engine_version(ctx.opensta)
    if ctx.physical:
        from emuflow.common.native_tools import require_native_schema
        require_native_schema("placement-lowering-request-v1")
        require_native_schema("transport-synthesis-request-v1")
        ctx.global_sta_executable = resolve_native_executable("sta", ctx.global_sta_executable)
        from emuflow.timing.opensta import _runtime_data_path
        ctx.global_sta_launcher_reference = _runtime_data_path(Path("scripts/opensta/container_runtime.sh")).resolve()
        if not Path(ctx.global_sta_executable).is_file() or not os.access(ctx.global_sta_executable, os.X_OK):
            raise EmuFlowError("global OpenSTA executable is unavailable or not executable")
        if ctx.global_sta_executable != ctx.opensta:
            check_opensta_engine_version(ctx.global_sta_executable)

    ctx.output_dir = ctx.output_dir.resolve()
    if ctx.output_dir.exists():
        if not ctx.output_dir.is_dir() or any(ctx.output_dir.iterdir()):
            raise EmuFlowError(
                "multi-FPGA output path must be an empty directory: "
                f"{ctx.output_dir}"
            )
    ctx.openroad = _resolve_partition_openroad(
        ctx.partition_provider, ctx.openroad, ctx.tritonpart_solution
    )
    from emuflow.common.native_tools import require_native_schema

    if ctx.prepared_frontend is None and ctx.yosys_json is None:
        require_native_schema("yosys-emuir-import-request-v1")
        require_native_schema("external-tool-request-v2")
    require_native_schema("partition-initialization-request-v1")
    require_native_schema("partition-decision-request-v1")
    require_native_schema("partition-configuration-request-v1")
    require_native_schema("partition-hop-request-v1")
    require_native_schema("phase3-stage-request-v1")
    require_native_schema("partition-load-request-v2")
    require_native_schema("partition-finalization-request-v1")
    require_native_schema("assignment-publication-request-v3")
    require_native_schema("assignment-descriptor-request-v1")
    require_native_schema("exact-binary-publication-request-v1")
    require_native_schema("exact-binary-validation-request-v1")
    require_native_schema("partition-publication-request-v1")
    require_native_schema("phase5-stage-request-v1")
    require_native_schema("split-netlists-v3")
    require_native_schema("transport-stage-request-v1")
    require_native_schema("phase6-stage-request-v1")
    require_native_schema("phase6-compile-policy-v1")
    require_native_schema("boarddb-v1")
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    if ctx.physical and ctx.phase6_provider == "baseline":
        prepare_physical_policy_reference(ctx)
    if ctx.prepared_frontend is None and ctx.yosys_json is None and ctx.sources:
        from emuflow.frontend.frontend_native import prepare_frontend
        prepare_frontend(ctx)
    elif ctx.prepared_frontend is not None or ctx.prepared_timing is not None or ctx.yosys_json is not None:
        from emuflow.frontend.inputs import prepare_inputs
        prepare_inputs(ctx)
    if ctx.internal_timing_database and ctx.prepared_timing is None:
        from emuflow.timing.phase2_native import prepare_configuration_request
        from emuflow.common.native_tools import native_file_generation
        ctx.native_phase2_configuration = prepare_configuration_request(
            ctx.output_dir / ".native-phase2-configuration",
            clocks=ctx.clock_periods, timing_io=ctx.timing_io,
            timing_model_path=ctx.timing_model,
            architecture_timing_db_path=ctx.architecture_timing_db,
            executable=ctx.opensta, max_paths=ctx.sta_max_paths,
            optimization_enabled=ctx.timing_driven,
            criticality_scale=ctx.timing_criticality_scale,
            criticality_exponent=ctx.timing_criticality_exponent)
        ctx.native_phase2_configuration_generation = tuple(str(value) for value in native_file_generation(ctx.native_phase2_configuration))

    from emuflow.partition.phase3_native import prepare_configuration_request as prepare_partition_configuration
    from emuflow.common.native_tools import native_file_generation
    ctx.native_phase3_configuration = prepare_partition_configuration(
        ctx.output_dir / ".native-phase3-configuration", ctx.platform_path,
        constraints_path=ctx.partition_constraints, seed=ctx.seed,
        min_used_fpgas=ctx.min_used_fpgas, balance_tolerance=ctx.balance_tolerance,
        provider=ctx.partition_provider, openroad=ctx.openroad,
        tritonpart_solution=ctx.tritonpart_solution,
        tritonpart_timeout_seconds=ctx.partition_timeout_seconds,
        tritonpart_seed_attempts=ctx.partition_seed_attempts,
        tritonpart_num_initial_solutions=ctx.partition_num_initial_solutions,
        tritonpart_num_best_initial_solutions=ctx.partition_num_best_initial_solutions,
        tritonpart_repair_min_used_fpgas=ctx.partition_repair_min_used_fpgas,
        route_constraints_path=ctx.route_constraints,
        max_cross_fpga_dependency_depth=ctx.max_cross_fpga_dependency_depth,
        static_exact_candidate_policy=ctx.static_exact_candidate_policy,
        patron_max_moves=ctx.patron_max_moves, board_link_timing_db=ctx.board_link_timing_db,
        timing_driven=ctx.timing_driven)
    ctx.native_phase3_configuration_generation = tuple(str(value) for value in native_file_generation(ctx.native_phase3_configuration))


    from emuflow.routing.phase4_native import prepare_configuration_request as prepare_routing
    route_settings = ctx.output_dir / '.native-phase4-configuration'
    route_settings.mkdir()
    ctx.native_phase4_configuration = prepare_routing(route_settings/'request.json', ctx.platform_path,
        constraints_path=ctx.route_constraints, frame_slots=ctx.frame_slots,
        max_iterations=ctx.route_max_iterations, candidate_workers=ctx.route_candidate_workers,
        timing_driven=ctx.timing_driven, external_timing=ctx.timing_paths,
        timing_model_path=ctx.timing_model if ctx.internal_timing_database else None,
        architecture_timing_db_path=ctx.architecture_timing_db)
    ctx.native_phase4_configuration_generation = tuple(str(v) for v in native_file_generation(ctx.native_phase4_configuration))
    if ctx.phase6_provider == 'baseline':
        from emuflow.transport.phase6_native import prepare_configuration_request as prepare_transport
        ctx.native_phase6_configuration = prepare_transport(ctx.output_dir/'.native-phase6-configuration',ctx.platform_path,
            clock_periods=ctx.clock_periods,transport_cost_path=ctx.transport_cost_db,
            equivalence_cycles=ctx.equivalence_cycles,equivalence_seed=ctx.equivalence_seed)
        ctx.native_phase6_configuration_generation = tuple(str(v) for v in native_file_generation(ctx.native_phase6_configuration))


def _initial_context(request):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    from tests.native_compile_state_fixture import CompileContext as _CompileContext

    options = request["options"]
    if type(options) is not dict:
        raise EmuFlowError("native compile options must be an object")
    signature = inspect.signature(run_multi_fpga_flow)
    # Signature binding is the single current-default contract during this
    # transition; no duplicated or silently inferred native defaults.
    bound = signature.bind(**options)
    bound.apply_defaults()
    for name, value in list(bound.arguments.items()):
        annotation = str(signature.parameters[name].annotation)
        if value is not None and "Path" in annotation:
            def path(item):
                if not isinstance(item, (str, Path)) or not Path(item).is_absolute():
                    raise EmuFlowError(f"native compile {name} requires absolute paths")
                return Path(item)
            bound.arguments[name] = tuple(path(item) for item in value) if "Iterable" in annotation else path(value)
    return _CompileContext(dict(bound.arguments))

def prepare_physical_policy_reference(ctx):
    """Freeze only backend tool/package configuration before graph stages run."""
    sources,settings=_backend_sources(ctx.physical_backend,
        architecture=ctx.physical_architecture,yosys=ctx.yosys,
        openparf_install=ctx.physical_openparf_install,openparf_python=ctx.physical_openparf_python,
        vivado=ctx.physical_vivado,vivado_place_directive=ctx.physical_vivado_place_directive,
        vivado_route_directive=ctx.physical_vivado_route_directive,
        rapidwright_jar=ctx.physical_rapidwright_jar,rapidwright_java=ctx.physical_rapidwright_java,
        rapidwright_java_source=ctx.physical_rapidwright_java_source,
        rapidwright_device_data=ctx.physical_rapidwright_device_data,
        rapidwright_timing_data=ctx.physical_rapidwright_timing_data,
        rapidwright_opensta=ctx.physical_rapidwright_opensta,
        rapidwright_native_constraints=ctx.physical_rapidwright_native_constraints,
        rapidwright_provider_manifest=ctx.physical_rapidwright_provider_manifest)
    value={'schema':'emuflow.physical-flow-policy/v1','sources':sources,'backend':ctx.physical_backend,
        'backend_options':settings,'workers':ctx.physical_workers,'seed':ctx.physical_seed}
    root=ctx.output_dir/'.native-physical-request';root.mkdir(exist_ok=False)
    target=root/'physical-policy.json';text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(text.encode())>8*1024*1024:raise ValidationError('physical compile policy exceeds bound')
    target.write_text(text+'\n',encoding='utf-8')
    for source in sources:
        if source['generation']!=list(native_file_generation(source['path'])):raise ValidationError('physical policy source changed')
    ctx.native_physical_policy=target
    ctx.native_physical_generation=tuple(str(v) for v in native_file_generation(target))
