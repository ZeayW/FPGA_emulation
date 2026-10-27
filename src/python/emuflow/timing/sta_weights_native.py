"""Bounded hosts for the canonical timing-weight producer and source auditor."""
from contextlib import closing, contextmanager
import math
import os
from pathlib import Path
import re
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.timing.indexed_timing import IndexedTimingView
from emuflow.common.io import read_json
from emuflow.common.native_tools import require_native_schema
from emuflow.timing.sta_paths_native import ValidationProof, _execute
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged, validate_store_destination


def _selector(path, schema):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512*1024:
        raise ValidationError("timing-weight selector is missing, symbolic, or exceeds bound")
    source = bind_store_source(path, schema)
    value = read_json(path)
    require_store_sources_unchanged((source,))
    if not isinstance(value, dict):
        raise ValidationError("timing-weight selector must be an object")
    return value, source


def _parameters(scale, exponent):
    for name, value in (("criticality_scale", scale), ("criticality_exponent", exponent)):
        try:
            valid = type(value) in {int, float} and math.isfinite(float(value)) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValidationError(f"{name} must be positive and finite")
    return {"criticality_scale": float(scale), "criticality_exponent": float(exponent)}


@contextmanager
def _paths(database_path, ir_path, *, validation=None):
    from emuflow.timing import sta
    value, selector = _selector(database_path, "sta-path-selector-v1")
    if value.get("schema") != sta.STA_PATH_DATABASE_INDEX_SCHEMA:
        raise ValidationError("timing weights require current indexed TimingPathDB")
    path, _digest, _count, kind = sta._sta_path_payload(Path(database_path), value)
    if kind != sta.STA_PATH_DATABASE_SQLITE_FORMAT or path.is_symlink():
        raise ValidationError("timing weights require the current SQLite path owner")
    with closing(IndexedTimingView(Path(ir_path))) as view:
        sources = (view.source, selector, bind_store_source(path, "timing-path-store-v1"))
        if validation is None:
            sta.validate_sta_path_database_path_value(Path(database_path), value, view)
        else:
            if not isinstance(validation, ValidationProof):
                raise ValidationError("timing weights require the original typed path validation proof")
            validation.require(Path(database_path), value, view)
        require_store_sources_unchanged(sources)
        yield value, sources
        require_store_sources_unchanged(sources)


def _run(inputs, operation, output, summary, root, parameters):
    return _execute(inputs, operation, output, summary, root, parameters,
                    request_schema="sta-weight-request-v1", magic=0x31574754534D5545)


def build_weights(database_path, ir_path, output_path, *, criticality_scale=9.0,
                  criticality_exponent=2.0, validation=None):
    from emuflow.timing import sta
    database_path, ir_path, output_path = map(Path, (database_path, ir_path, output_path))
    parameters = _parameters(criticality_scale, criticality_exponent)
    require_native_schema("sta-weight-request-v1")
    if output_path.is_symlink():
        raise ValidationError("STA selector is a symbolic link")
    with _paths(database_path, ir_path, validation=validation) as (database, sources):
        validate_store_destination(output_path, sources)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".sta-weight-build.", dir=output_path.parent) as temporary:
            root = Path(temporary)
            result, metrics = _run(sources, "build-sta-weights", root/"weights.sqlite", root/"summary.json", root, parameters)
            if (result.get("schema") != "emuflow.sta-net-weights-production/v1"
                    or result.get("implementation_id") != "native-sta-net-weights-v1"
                    or result.get("design") != database.get("design")):
                raise ValidationError("native timing-weight summary disagrees")
            descriptor, name = tempfile.mkstemp(prefix=".sta-weight-payload.", suffix=".tmp", dir=output_path.parent)
            os.close(descriptor)
            sibling = Path(name)
            try:
                os.replace(root/"weights.sqlite", sibling)
                payload, digest, created = sta._adopt_sta_payload(sibling, output_path,
                    "weights", "net-weight-store-v1", sources)
            finally:
                sibling.unlink(missing_ok=True)
            summary = {key: result[key] for key in ("paths", "timed_nets", "weighted_nets", "minimum_weight", "maximum_weight")}
            manifest = {"schema": sta.PARTITION_NET_WEIGHTS_INDEX_SCHEMA, "design": database["design"],
                "source": {"provider": "sta-max-criticality-power-law-v2-indexed",
                           "path_database": database_path.name, "path_database_provider": database["source"]["provider"]},
                "parameters": {**parameters, "criticality_definition": "clamp(1-slack/period,0,1)", "path_reduction": "maximum"},
                "summary": summary,
                "payloads": {"weights": {"format": sta.PARTITION_NET_WEIGHTS_SQLITE_FORMAT,
                    "path": payload.name, "sha256": digest, "records": summary["timed_nets"]}}}
            sta.commit_store_selector(output_path, manifest, sources, payload, created)
            return {"status": "pass", "design": database["design"], **summary, "output": str(output_path),
                    "implementation_id": result["implementation_id"], "native_metrics": metrics}


def validate_weights(weights_path, database_path, ir_path):
    from emuflow.timing import sta
    weights_path, database_path, ir_path = map(Path, (weights_path, database_path, ir_path))
    require_native_schema("sta-weight-request-v1")
    weights, selector = _selector(weights_path, "net-weight-selector-v2")
    if weights.get("schema") != sta.PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        raise ValidationError("partition net weights schema is invalid")
    parameters = weights.get("parameters")
    if not isinstance(parameters, dict):
        raise ValidationError("partition net weights parameters are invalid")
    parameters = _parameters(parameters.get("criticality_scale"), parameters.get("criticality_exponent"))
    payload = sta.partition_net_weights_payload_path(weights_path, weights)
    descriptor = weights["payloads"]["weights"]
    digest = descriptor.get("sha256")
    if (payload.is_symlink() or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise ValidationError("partition net weights payload seal is invalid")
    with _paths(database_path, ir_path) as (_database, sources):
        sources += (selector, bind_store_source(payload, "net-weight-store-v1"))
        sta._verify_file_digest(payload, digest, "partition net weights payload")
        require_store_sources_unchanged(sources)
        with tempfile.TemporaryDirectory(prefix=".sta-weight-audit.", dir=weights_path.parent) as temporary:
            root = Path(temporary)
            result, _metrics = _run(sources, "validate-sta-weights", None, root/"summary.json", root, parameters)
        if result.get("schema") != "emuflow.sta-net-weights-validation/v1":
            raise ValidationError("native timing-weight validation summary disagrees")
        require_store_sources_unchanged(sources)
        return {key: result[key] for key in ("status", "paths", "timed_nets", "weighted_nets", "minimum_weight", "maximum_weight")}
