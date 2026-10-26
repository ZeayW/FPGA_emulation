"""Small source-bound requests for complete native physical evidence validation."""
import json
import math
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import require_store_sources_unchanged
from .tool_runtime import NativeToolProcess

_KINDS = {
    "boundary-identity-store-v1": 1,
    "boundary-timing-store-v1": 2,
    "logic-segment-timing-store-v1": 3,
    "local-path-timing-store-v1": 4,
    "logic-segment-identity-store-v1": 5,
}


def validate_physical_evidence(store_path, selector, sources, schema):
    kind = _KINDS[schema]
    store_path = Path(store_path).resolve()
    source = next((value for value in sources if value.path == store_path), None)
    if source is None or source.schema != schema:
        raise ValidationError("native physical evidence owner is missing")
    encoded = [json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")) for value in (
        selector["header"], selector["counts"], selector.get("source"),
    )]
    if any(len(value.encode()) > limit for value, limit in zip(encoded, (4*1024*1024, 65536, 65536))):
        raise ValidationError("native physical evidence metadata exceeds bound")
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix="emuflow-physical-evidence-") as temporary:
        root = Path(temporary)
        request, summary = root / "request.bin", root / "summary.json"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QII", 0x31564550464D5545, 1, kind)
            writer.put("3Q4q", *source.generation)
            for value in (*encoded, selector["logical_sha256"]):
                writer.text(value)
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        with NativeToolProcess(executable, [executable, "physical", "validate-evidence",
                                           str(store_path), str(request), str(summary)],
                               environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError("native physical evidence validation failed: " + completed.stdout)
        require_store_sources_unchanged(sources)
        if summary.stat().st_size > 65536:
            raise ValidationError("native physical evidence summary exceeds bound")
        value = json.loads(summary.read_text())
    if (not isinstance(value, dict) or set(value) != {
            "schema", "status", "implementation_id", "kind", "counts", "logical_sha256",
            "maximum_delay_ns", "measurement_counts"}
            or value["schema"] != "emuflow.physical-evidence-validation/v1"
            or value["status"] != "pass"
            or value["implementation_id"] != "native-physical-evidence-validation-v1"
            or value["kind"] != kind or value["counts"] != selector["counts"]
            or value["logical_sha256"] != selector["logical_sha256"]
            or type(value["maximum_delay_ns"]) not in (int, float)
            or not math.isfinite(value["maximum_delay_ns"]) or value["maximum_delay_ns"] < 0
            or not isinstance(value["measurement_counts"], dict)):
        raise ValidationError("native physical evidence validation result disagrees")
    expected_measurements = (
        {"explicit-routed-path-chain", "endpoint-longest-path-fallback"}
        if kind == 4 and selector["header"]["identity_schema"] == "emuflow.local-path-identity/v2"
        else set()
    )
    if (set(value["measurement_counts"]) != expected_measurements
            or any(type(count) is not int or count < 0 for count in value["measurement_counts"].values())
            or (expected_measurements and sum(value["measurement_counts"].values()) != selector["counts"]["local_paths"])):
        raise ValidationError("native physical evidence measurement coverage disagrees")
    require_store_sources_unchanged(sources)
    return value
