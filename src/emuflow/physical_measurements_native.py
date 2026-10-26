"""Native measurement import and bounded publication of current timing owners."""
import importlib
import json
import math
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .io import file_sha256
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import (
    bind_store_source, commit_store_selector, publish_sqlite_store,
    require_store_sources_unchanged, validate_store_destination,
)
from .tool_runtime import NativeToolProcess

_KINDS = {
    2: ('boundary_timing', 'BOUNDARY_TIMING', 'import-boundary-timing', 'boundary-identity-store-v1'),
    3: ('logic_segment_timing', 'LOGIC_SEGMENT_TIMING', 'import-logic-timing', 'logic-segment-identity-store-v1'),
    4: ('local_path_timing', 'LOCAL_PATH_TIMING', 'import-local-timing', 'local-path-timing-store-v1'),
}



def import_physical_measurements(input_path, identity_path, output_path, *, kind, header,
                                 sources, source=None, allow_missing=False, allow_legacy=False):
    name, prefix, command, identity_schema = _KINDS[kind]
    module = importlib.import_module('emuflow.' + name + '_storage')
    input_path, identity_path, output_path = map(Path, (input_path, identity_path, output_path))
    source_bindings = validate_store_destination(output_path, sources)
    identities = next((s for s in source_bindings if s.path == identity_path.resolve() and s.schema == identity_schema), None)
    measurement = next((s for s in source_bindings if s.path == input_path.resolve() and s.schema == 'physical-timing-measurements-v1'), None)
    if identities is None or measurement is None:
        raise ValidationError('native physical timing source ownership is missing')
    payload = [json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
               for value in (header, source)]
    if any(len(text.encode()) > size for text, size in zip(payload, (4*1024*1024, 65536))):
        raise ValidationError('native physical timing metadata exceeds bound')
    executable = resolve_native_executable('emuflow-native')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lease = module._Lease(output_path, exclusive=True)
    staged = None
    try:
        with tempfile.TemporaryDirectory(prefix='.physical-measurements.', dir=output_path.parent) as temporary:
            root = Path(temporary)
            request, summary = root/'request.bin', root/'summary.json'
            descriptor, filename = tempfile.mkstemp(prefix='.'+output_path.stem+'.', suffix='.native.sqlite', dir=output_path.parent)
            os.close(descriptor)
            staged = store = Path(filename)
            writer = CheckedRequestWriter(request)
            try:
                writer.put('QIII', 0x314d4950464d5545, 1, kind, int(allow_missing) | (int(allow_legacy) << 1))
                for binding in (identities, measurement):writer.put('3Q4q', *binding.generation)
                for value in payload:writer.text(value)
                writer.finish()
            finally:
                writer.close()
            require_store_sources_unchanged(source_bindings)
            with NativeToolProcess(executable, [executable, 'physical', command, str(identity_path.resolve()),
                                               str(input_path.resolve()), str(request), str(store), str(summary)],
                                   environment=dict(os.environ), scratch_parent=root) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError('native physical timing import failed: ' + completed.stdout)
            require_store_sources_unchanged(source_bindings)
            if summary.stat().st_size > 8*1024*1024:
                raise ValidationError('native physical timing summary exceeds bound')
            result = json.loads(summary.read_text())
            if (not isinstance(result, dict) or set(result) != {
                    'schema', 'status', 'implementation_id', 'kind', 'header_json', 'counts_json', 'source_json',
                    'logical_sha256', 'maximum_delay_ns', 'measurement_counts_json', 'missing_segments'}
                    or result['schema'] != 'emuflow.physical-measurement-import/v1' or result['status'] != 'pass'
                    or result['implementation_id'] != 'native-physical-measurement-import-v1' or result['kind'] != kind
                    or type(result['maximum_delay_ns']) not in (int, float)
                    or not math.isfinite(result['maximum_delay_ns']) or result['maximum_delay_ns'] < 0
                    or type(result['missing_segments']) is not int or result['missing_segments'] < 0):
                raise ValidationError('native physical timing summary disagrees')
            return publish_physical_projection(store, output_path, kind=kind, result=result,
                                               sources=source_bindings)

    finally:
        if staged is not None:staged.unlink(missing_ok=True)
        lease.close()


def publish_physical_projection(store, output_path, *, kind, result, sources):
    """Publish one native sealed projection while its caller holds the owner lease.

    Measurement import and native routed-graph production share this bounded
    selector path. Neither reconstructs typed records or revalidates the graph.
    """
    store, output_path = Path(store), Path(output_path)
    if store.parent.resolve() == output_path.parent.resolve():
        return _publish_closed_projection(store, output_path, kind=kind, result=result, sources=sources)
    # Move the closed private owner once into the publisher's same-directory
    # staging contract. No payload copy, row transfer or second graph pass.
    descriptor, name = tempfile.mkstemp(prefix="." + output_path.stem + ".", suffix=".native.sqlite", dir=output_path.parent)
    os.close(descriptor)
    staged = Path(name)
    try:
        os.replace(store, staged)
        return _publish_closed_projection(staged, output_path, kind=kind, result=result, sources=sources)
    finally:
        staged.unlink(missing_ok=True)


def _publish_closed_projection(store, output_path, *, kind, result, sources):
    name, prefix, _command, _identity_schema = _KINDS[kind]
    module = importlib.import_module("emuflow." + name + "_storage")
    store, output_path = Path(store), Path(output_path)
    sources = validate_store_destination(output_path, sources)
    if (result.get("kind") != kind or type(result.get("maximum_delay_ns")) not in (int, float)
            or not math.isfinite(result["maximum_delay_ns"]) or result["maximum_delay_ns"] < 0
            or type(result.get("missing_segments")) is not int or result["missing_segments"] < 0):
        raise ValidationError("native physical timing projection summary disagrees")
    final_header, counts = json.loads(result["header_json"]), json.loads(result["counts_json"])
    final_source = json.loads(result["source_json"])
    digest = file_sha256(store)
    published = output_path.with_name(module._store_name(output_path, digest))
    created = committed = False
    try:
        if published.is_symlink():
            raise ValidationError("physical timing generation is a symlink")
        if published.exists():
            if file_sha256(published) != digest:
                raise ValidationError("physical timing generation seal disagrees")
        else:
            publish_sqlite_store(store, published, name.replace("_", "-") + "-store-v1", sources=sources)
            created = True
        selector = {
            "schema": getattr(module, prefix + "_STORAGE_SCHEMA"),
            "logical_schema": getattr(module, prefix + "_SCHEMA"),
            "application_id": getattr(module, prefix + "_APPLICATION_ID"),
            "user_version": getattr(module, prefix + "_USER_VERSION"),
            "header": final_header, "counts": counts, "logical_sha256": result["logical_sha256"],
            "artifact": published.name, "store_bytes": published.stat().st_size, "store_sha256": digest,
        }
        if kind == 3:
            selector["source"] = final_source
        module._validate_selector(selector)
        require_store_sources_unchanged(sources)
        commit_store_selector(output_path, selector, sources, published, created)
        committed = True
        try:
            module._collect_stale(output_path, published)
        except OSError:
            pass
        validation = {"status": "pass", "fpga": final_header["fpga"],
                      "maximum_delay_ns": result["maximum_delay_ns"], "output": str(output_path)}
        if kind == 2:
            validation.update(counts)
        elif kind == 3:
            validation.update({key: counts.get(key, 0) for key in
                               ("segments", "endpoint_exact_segments", "cone_bound_segments", "endpoint_collection_bound_segments")})
            validation["missing_segments"] = result["missing_segments"]
        else:
            validation.update(local_paths=counts["local_paths"],
                              measurement_counts=json.loads(result["measurement_counts_json"]))
        return validation
    finally:
        if created and not committed:
            published.unlink(missing_ok=True)
