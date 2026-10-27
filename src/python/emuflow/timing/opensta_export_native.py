"""File-reference host for shared-core compact EmuIR timing export."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import publish_derived_outputs, require_store_sources_unchanged, validate_store_destination
from emuflow.common.tool_runtime import NativeToolProcess


def export_opensta_inputs(view, *, verilog=None, net_map=None, timing_cell_types=None):
    from emuflow.timing.indexed_timing import _TimingTypes
    require_native_schema("opensta-input-export-request-v1")
    source = view.source
    mode = 0
    if timing_cell_types is not None:
        if not isinstance(timing_cell_types, _TimingTypes) or timing_cell_types.view is not view:
            raise ValidationError("indexed OpenSTA export requires its source-bound timing-type policy")
        mode = 1
    destinations = [(label, Path(path)) for label, path in (("verilog", verilog), ("net_map", net_map)) if path is not None]
    if not destinations:
        raise ValidationError("OpenSTA export requires a text destination")
    if len({path.parent.resolve() for _label, path in destinations}) != 1:
        raise ValidationError("combined OpenSTA outputs require the same directory")
    for _label, destination in destinations:
        validate_store_destination(destination, (source,))
    if len(destinations) == 2 and (destinations[0][1].resolve() == destinations[1][1].resolve()
            or (all(p.exists() for _label, p in destinations) and os.path.samefile(destinations[0][1], destinations[1][1]))):
        raise ValidationError("OpenSTA text destinations alias each other")
    parent = destinations[0][1].parent
    parent.mkdir(parents=True, exist_ok=True)
    executable = resolve_native_executable("emuflow-native")
    mask = (1 if verilog is not None else 0) | (2 if net_map is not None else 0)
    with ExitStack() as lifetime:
        root = Path(lifetime.enter_context(tempfile.TemporaryDirectory(prefix=".opensta-export.", dir=parent)))
        request, summary = root/"request.bin", root/"summary.json"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x31455853464D5545, 1)
            writer.put("3Q4q", *source.generation)
            writer.put("II", mode, mask)
            writer.finish()
        finally:
            writer.close()
        outputs = {label: root/label for label, _path in destinations}
        require_store_sources_unchanged((source,))
        with NativeToolProcess(executable, [executable, "physical", "export-opensta-inputs",
                str(source.path), str(request), str(outputs.get("verilog", "-")),
                str(outputs.get("net_map", "-")), str(summary)],
                environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError("native OpenSTA input export failed: " + completed.stdout)
        require_store_sources_unchanged((source,))
        if summary.stat().st_size > 16384:
            raise ValidationError("OpenSTA input export summary exceeds bound")
        report = json.loads(summary.read_text())
        if (report.get("schema") != "emuflow.opensta-input-export/v1" or report.get("status") != "pass"
                or report.get("implementation_id") != "native-opensta-export-v1"
                or report.get("outputs") != mask or report.get("mode") != mode
                or report.get("nets") != view.counts["net_records"]
                or (verilog is not None and (report.get("cells") != view.counts["cell_records"]
                                            or report.get("endpoints") != view.counts["endpoint_records"]))):
            raise ValidationError("native OpenSTA export coverage disagrees")
        # Closed native text files are adopted without copying their payloads.
        staged = []
        for label, destination in destinations:
            fd, name = tempfile.mkstemp(prefix="."+destination.name+".", suffix=".tmp", dir=parent)
            os.close(fd)
            path = Path(name)
            lifetime.callback(path.unlink, missing_ok=True)
            os.replace(outputs[label], path)
            staged.append((path, destination, None))
        publish_derived_outputs(staged, sources=(source,))
    report["native_metrics"] = completed.native_metrics
    return report
