"""Independent native validation of published current assignment cuts."""
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def validate_native_assignment_cuts(path):
    """Validate published cut rows independently through the shared view."""
    path = Path(path).resolve()
    generation = native_file_generation(path)
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix="emuflow-assignment-cuts-") as temporary:
        root = Path(temporary)
        output = root / "summary.json"
        command = [executable, "phase3", "validate-cuts", str(path), str(output)]
        with NativeToolProcess(
            executable,
            command,
            environment=dict(os.environ),
            timeout_ms=21600000,
            scratch_parent=root,
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native assignment cut validation failed: " + completed.stdout
            )
        if native_file_generation(path) != generation:
            raise ValidationError("assignment cut validation source generation changed")
        try:
            if output.stat().st_size > 1024:
                raise ValidationError("assignment cut validation summary exceeds bound")
            summary = json.loads(output.read_text())
        except (OSError, ValueError) as error:
            raise ValidationError(
                "assignment cut validation summary is invalid"
            ) from error
    if (
        not isinstance(summary, dict)
        or set(summary) != {"schema", "status", "records", "sinks", "sha256"}
        or summary["schema"] != "emuflow.assignment-cut-validation/v1"
        or summary["status"] != "pass"
        or any(
            type(summary[key]) is not int or summary[key] < 0
            for key in ("records", "sinks")
        )
        or summary["sinks"] < summary["records"]
        or not isinstance(summary["sha256"], str)
        or len(summary["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in summary["sha256"])
    ):
        raise ValidationError("assignment cut validation summary disagrees")
    return summary
