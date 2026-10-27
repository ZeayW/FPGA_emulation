"""Fixed-event timing binding for an external OpenSTA engine.

This is a timing abstraction, not a synthesizable transport implementation.
An event cutpoint has an absolute launch time, a chain of measured arcs, and
an explicit capture deadline. OpenSTA propagates the arcs and checks the
deadline. TX readiness checks MUST accompany terminal path observations:
observing the last TX alone cannot prove that it transported the current value.
No Python-computed arrival, slack, or TDM wait enters the exported circuit.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .global_event_store import (
    EventChecks as _IndexedEventChecks, EventBuilder as _EventCheckBuilder,
    PathMetadata as _IndexedPathMetadata, MetadataBuilder as _PathMetadataBuilder,
    export_event_model, event_model_sources,
)
from .native_tools import resolve_native_executable
from .opensta import require_opensta_engine

@dataclass(frozen=True)
class EventCheck:
    path: str
    role: str
    event: str
    launch_ns: float
    arcs_ns: tuple[float, ...]
    required_ns: float


def validate_checks(checks: _IndexedEventChecks) -> _IndexedEventChecks:
    """Require the current native-validated event owner."""
    if not isinstance(checks, _IndexedEventChecks):
        raise ValidationError("global STA requires native event ownership; inline checks are retired")
    checks.require_valid()
    return checks

def export_event_checks(checks: _IndexedEventChecks, directory: Path) -> _IndexedEventChecks:
    """Export the raw event chains from the validated native owner."""
    rows = validate_checks(checks)
    export_event_model(rows, directory)
    return rows


def run_event_checks(checks: Iterable[EventCheck], directory: Path,
                     executable: str | None = None, *, verify_arcs=True
                     ) -> Sequence[dict]:
    if not isinstance(checks, _IndexedEventChecks):
        raise ValidationError("global OpenSTA requires native event ownership; inline execution is retired")
    rows = validate_checks(checks)
    representatives, _ = rows.compressed()
    export_event_checks(representatives, directory)
    model_directory, model_sources = event_model_sources(directory)
    tool = resolve_native_executable("sta", executable)
    engine = require_opensta_engine(tool)
    with (directory / "opensta.log").open("w") as log:
        # Some packaged OpenSTA builds report ``GITDIR-NOT`` because their
        # source archive had no .git directory. Seal the exact executable in
        # the process log so engine identity remains independently replayable.
        log.write(
            "EmuFlow OpenSTA executable SHA256 "
            f"{engine['executable_sha256']}\n"
        )
        if "launcher_sha256" in engine:
            log.write(f"EmuFlow OpenSTA launcher SHA256 {engine['launcher_sha256']}\n")
        log.flush()
        from .store_publication import require_store_sources_unchanged
        from .tool_runtime import NativeToolProcess
        rows.require_valid()
        require_store_sources_unchanged(model_sources)
        runner = resolve_native_executable("emuflow-native")
        with NativeToolProcess(
            runner, [tool, "-exit", "analyze.tcl"], cwd=model_directory,
            environment=dict(os.environ), forward_stdout=True,
            scratch_parent=rows._owner.root,
        ) as process:
            for line in process.stdout:
                log.write(line.decode("utf-8", errors="replace"))
            result = process.finish()
        rows.require_valid()
        require_store_sources_unchanged(model_sources)
    engine_output = model_directory / "measurements.tsv"
    if result.returncode or not engine_output.is_file():
        raise ValidationError("global OpenSTA failed; see opensta.log")
    from .global_measurements import import_measurements
    expanded_values = import_measurements(
        engine_output, representatives, verify_arcs=verify_arcs, directory=directory
    )
    engine_output.unlink()
    with (directory / "opensta.log").open("a") as log:
        log.write(
            "EmuFlow exact path-bundle compression "
            f"original_checks={len(rows)} "
            f"representative_checks={len(representatives)}\n"
        )
    return expanded_values

def read_engine_identity(log_path: Path) -> dict:
    """Read the existing process banner, without another tool invocation."""
    executable_sha256 = None
    launcher_sha256 = None
    banner = None
    with log_path.open() as stream:
        for _ in range(32):
            line = stream.readline(4096)
            if not line:
                break
            seal = re.fullmatch(
                r"EmuFlow OpenSTA executable SHA256 ([0-9a-f]{64})\n?", line
            )
            if seal:
                executable_sha256 = seal[1]
                continue
            launcher_seal = re.fullmatch(
                r"EmuFlow OpenSTA launcher SHA256 ([0-9a-f]{64})\n?", line
            )
            if launcher_seal:
                launcher_sha256 = launcher_seal[1]
                continue
            match = re.match(r"OpenSTA\s+(\S+)\s+(\S+)", line)
            if match:
                banner = (match[1], match[2])
    if banner is not None:
        version, reported_revision = banner
        if re.fullmatch(r"[0-9a-f]{7,40}", reported_revision):
            result = {
                "name": "OpenSTA",
                "version": version,
                "revision": reported_revision,
            }
            if executable_sha256 is not None:
                result["executable_sha256"] = executable_sha256
            if launcher_sha256 is not None:
                result["launcher_sha256"] = launcher_sha256
            return result
        if executable_sha256 is not None:
            return {
                "name": "OpenSTA",
                "version": version,
                "revision": f"binary-sha256:{executable_sha256}",
                "reported_revision": reported_revision,
                "executable_sha256": executable_sha256,
                **({"launcher_sha256": launcher_sha256} if launcher_sha256 is not None else {}),
            }
    raise ValidationError("global OpenSTA log lacks engine version/revision")

def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    """Bind only current typed physical facts through the shared native owner."""
    from .global_binding_native import has_native_physical_inputs, bind_native_physical_checks
    if not has_native_physical_inputs(routes, schedule, physical):
        raise ValidationError("global STA requires current typed physical owners; inline binding is retired")
    return bind_native_physical_checks(
        runtime, routes, schedule, physical, platform, metadata=metadata,
        routes_artifact_sha256=routes_artifact_sha256,
    )

def build_opensta_timing(runtime, metadata, measurements):
    """Publish the native complete-original-path reduction of OpenSTA scalars."""
    from .global_measurements import NativeMeasurements
    if not isinstance(measurements, NativeMeasurements):
        raise ValidationError("global STA requires native measurement ownership; inline reduction is retired")
    return _build_native_opensta_timing(runtime, metadata, measurements)

def _build_native_opensta_timing(runtime, metadata, measurements):
    from .global_measurements import NativeTimingPaths
    measurements.require()
    paths = NativeTimingPaths(measurements, metadata["paths"])
    reduced = measurements.header['reduction']
    source = metadata['source_binding']
    count, bounds, failures = reduced['paths'], reduced['bounds_paths'], reduced['event_failures']
    scope = 'whole-original-design' if source is not None else 'cross-fpga-path-subset'
    status = 'fail' if failures or reduced['runtime']['wns_ns'] < 0 else 'pass'
    metrics = {role: {key: reduced[role][key] for key in ('wns_ns','original_path_tns_ns','negative_paths')}
               for role in ('target','runtime')}
    timing = {'schema':'emuflow.system-timing/v2','status':status,
              'qualification':'opensta-fixed-event-physical-bounds','timing_scope':scope,
              'source_binding':source,'paths':paths,
              'path_exactness': {'physical_model':'routed-staging-chain-upper-bounds',
                  'endpoint_exact_logic_paths':count-bounds,'cone_bound_logic_paths':bounds,
                  'fallback_logic_paths':0,'discontinuous_compressed_paths':0,
                  'physical_logic_segment_bounds':True,'fixed_slot_event_failures':failures},
              'summary': {'timing_paths':count,'original_paths':source['original_paths'] if source else None,
                  'original_local_paths':reduced['local_paths'],'original_cross_fpga_paths':count-reduced['local_paths'],
                  'compressed_representative_paths':metadata['compressed_representative_paths'],
                  'original_path_coverage':1.0 if source else None,
                  'original_path_ids_sha256':source['original_path_ids_sha256'] if source else None,
                  'maximum_system_delay_bound_ns':reduced['maximum_delay_ns']}}
    for role in ('target','runtime'):
        values=reduced[role];tns=values['original_path_tns_ns']
        timing[role+'_clock']={'closure_gate':role=='runtime','worst_path':values['worst_path'],
            'worst_slack_bound_ns':values['wns_ns'],'tns_bound_ns':tns,
            'total_negative_slack_bound_ns':tns,'negative_slack_paths':values['negative_paths']}
    periods={clock:float(record['nominal_period_ns']) for clock,record in runtime['virtual_dut_clocks'].items()}
    period=min(periods.values());maximum=reduced['maximum_delay_ns']
    timing['runtime_clock'].update(period_ns=period,frequency_mhz=1000.0/period,
        period_scope='fastest-domain-summary',clock_periods_ns=periods,
        minimum_safe_period_bound_ns=maximum,maximum_safe_frequency_bound_mhz=1000.0/maximum if maximum>0 else None)
    timing['global_opensta']={'schema':'emuflow.global-opensta-check/v1','authority':'opensta','execution':'standalone',
        'status':status,'timing_scope':scope,'original_paths':count,'checks':reduced['checks'],
        'event_failures':failures,'metrics':metrics,'tns_definition':'sum-negative-slack-once-per-original-TimingPathDB-path',
        'implementation_id':'native-global-measurements-v1',
        'measurement_logical_sha256':reduced['measurements_sha256']}
    return timing
