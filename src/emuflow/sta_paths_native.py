"""Bounded hosts for native TimingPathDB production and independent audit."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .indexed_timing import IndexedTimingView
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import (bind_store_source, require_store_sources_unchanged,
                                validate_store_destination)
from .tool_runtime import NativeToolProcess


class ValidationProof:
    def __init__(self, selector, manifest, source, payload, result):
        self.selector = bind_store_source(selector, "sta-path-selector-v1")
        self.source = source
        self.sources = (source, payload, self.selector)
        self.manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.result = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def require(self, selector, manifest, view):
        if (Path(selector).resolve() != self.selector.path
                or json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) != self.manifest
                or not isinstance(view, IndexedTimingView) or view.source != self.source):
            raise ValidationError("native STA validation proof belongs to another owner")
        require_store_sources_unchanged(self.sources)
        return json.loads(self.result)


class ImportedPaths(dict):
    """Ordinary JSON-compatible result with an invocation-scoped native lease."""
    validation = None


def _execute(inputs, operation, output, summary, root, configuration):
    require_native_schema("sta-path-request-v1")
    request = root/"request.bin"
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QI", 0x31505453464D5545, 1)
        for source in inputs:
            writer.put("3Q4q", *source.generation)
        writer.text(json.dumps(configuration, sort_keys=True, separators=(",", ":"), allow_nan=False))
        writer.finish()
    finally:
        writer.close()
    require_store_sources_unchanged(inputs)
    executable = resolve_native_executable("emuflow-native")
    argv = [executable, "physical", operation, *(str(s.path) for s in inputs), str(request)]
    argv += [str(output), str(summary)] if output is not None else [str(summary)]
    with NativeToolProcess(executable, argv, environment=dict(os.environ), scratch_parent=root) as process:
        completed = process.finish()
    if completed.returncode:
        raise ValidationError("native STA path operation failed: " + completed.stdout)
    require_store_sources_unchanged(inputs)
    if summary.stat().st_size > 128*1024:
        raise ValidationError("native STA path summary exceeds bound")
    value = json.loads(summary.read_text())
    if value.get("status") != "pass":
        raise ValidationError("native STA path operation has no passing result")
    return value, completed.native_metrics


def import_paths(input_path, ir_path, output_path, *, provider, source=None, view=None):
    from . import sta
    if provider not in sta.STA_PATH_DATABASE_PROVIDERS:
        raise ValidationError("STA path database provider is invalid")
    if output_path.is_symlink():
        raise ValidationError("STA selector is a symbolic link")
    with ExitStack() as lifetime:
        if not isinstance(view, IndexedTimingView):
            from .emuir_storage import is_emuir_sqlite
            from .emuir_compact import is_compact_emuir
            if not is_emuir_sqlite(ir_path) or not is_compact_emuir(ir_path):
                raise ValidationError("streamed STA import requires compact EmuIR v2")
            view = IndexedTimingView(ir_path)
            lifetime.callback(view.close)
        if view.source.path != Path(ir_path).resolve():
            raise ValidationError("STA import belongs to another EmuIR source")
        measured = bind_store_source(input_path, "sta-source-document-v1")
        sources = (view.source, measured)
        validate_store_destination(output_path, sources)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        root = Path(lifetime.enter_context(tempfile.TemporaryDirectory(prefix=".sta-path-import.", dir=output_path.parent)))
        payload, summary = root/"paths.sqlite", root/"summary.json"
        result, metrics = _execute(sources, "import-sta-paths", payload, summary, root, {})
        if (result.get("schema") != "emuflow.sta-path-import/v1"
                or result.get("implementation_id") != "native-sta-path-import-v1"
                or result.get("design") != view.value["design"]["name"]):
            raise ValidationError("native STA import summary disagrees")
        fd, name = tempfile.mkstemp(prefix=".sta-path-payload.", suffix=".tmp", dir=output_path.parent)
        os.close(fd)
        sibling = Path(name)
        lifetime.callback(sibling.unlink, missing_ok=True)
        os.replace(payload, sibling)
        published, digest, created = sta._adopt_sta_payload(sibling, output_path, "paths", "timing-path-store-v1", sources)
        source_value = dict(source) if source is not None else {}
        source_value.update(provider=provider, input=str(input_path))
        count = result["summary"]["paths"]
        artifact = {"schema": sta.STA_PATH_DATABASE_INDEX_SCHEMA, "design": result["design"],
                    "source": source_value, "normalization": result["normalization"], "summary": result["summary"],
                    "payloads": {"paths": {"format": sta.STA_PATH_DATABASE_SQLITE_FORMAT,
                                            "path": published.name, "sha256": digest, "records": count}}}
        sta.commit_store_selector(output_path, artifact, sources, published, created)
        report = ImportedPaths(status="pass", design=result["design"], paths=count,
                               structured_endpoint_paths=result["summary"]["structured_endpoint_paths"],
                               unique_path_nets=result["summary"]["unique_path_nets"],
                               path_qor=result["summary"]["qor"], output=str(output_path),
                               implementation_id=result["implementation_id"], native_metrics=metrics)
        report.validation = ValidationProof(output_path, artifact, view.source,
                                            bind_store_source(published, "timing-path-store-v1"), result["validation"])
        return report


def validate_paths(selector, manifest, view):
    from . import sta
    source = manifest.get("source")
    if (manifest.get("schema") != sta.STA_PATH_DATABASE_INDEX_SCHEMA or not isinstance(source, dict)
            or source.get("provider") not in sta.STA_PATH_DATABASE_PROVIDERS
            or manifest.get("design") != view.value["design"]["name"]):
        raise ValidationError("STA path database source/design/schema is invalid")
    normalization = sta._validate_database_normalization(manifest.get("normalization"))
    payload, digest, records, kind = sta._sta_path_payload(selector, manifest)
    if kind != sta.STA_PATH_DATABASE_SQLITE_FORMAT:
        raise ValidationError("native STA validation requires the indexed path owner")
    sources = (view.source, bind_store_source(payload, "timing-path-store-v1"))
    selector_source = bind_store_source(selector, "sta-path-selector-v1")
    sta._verify_file_digest(payload, digest, "STA path database payload")
    require_store_sources_unchanged((*sources, selector_source))
    with tempfile.TemporaryDirectory(prefix=".sta-path-validate.", dir=Path(selector).parent) as temporary:
        root = Path(temporary)
        result, _metrics = _execute(sources, "validate-sta-paths", None, root/"summary.json", root,
                                   {"normalization": normalization, "summary": manifest.get("summary"),
                                    "design": manifest["design"], "provider": source["provider"]})
    if result.get("paths") != records:
        raise ValidationError("STA path database payload count disagrees")
    require_store_sources_unchanged((*sources, selector_source))
    return result
