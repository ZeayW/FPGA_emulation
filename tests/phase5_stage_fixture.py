"""Exercise actual native publication failures, not a Python commit substitute."""
from pathlib import Path
import subprocess
import tempfile

from emuflow.scheduling.phase5 import _load_phase3_semantics
from emuflow.scheduling.phase5_native import _write_inputs
from emuflow.platform.platform import Platform
from emuflow.routing.system_route_storage import read_system_routes
from emuflow.scheduling.tdm import build_native_exact_schedule_request
from tests.native_build import native_pipeline_api_test


def interrupt_native_phase5(routes_path, platform_path, assignment_path, output_dir, point, mode="throw"):
    persisted = read_system_routes(routes_path)
    routes, _ = _load_phase3_semantics(persisted, assignment_path)
    producer = build_native_exact_schedule_request(routes, Platform.load(platform_path), source_bindings=persisted.bound_store_sources())
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="phase5-fault-inputs-", dir=output_dir.parent) as temporary:
        inputs = _write_inputs(producer, Path(temporary))
        return subprocess.run(
            [str(native_pipeline_api_test()), "phase5-fault", mode, str(point), *inputs,
             str(output_dir / "phase5_report.json")], capture_output=True, text=True,
        )
