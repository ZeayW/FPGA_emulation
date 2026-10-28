"""Source-bound native endpoint binding and bounded routed-timing views."""
from collections.abc import Sequence
from contextlib import closing
from itertools import islice
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, bind_view_sources, publish_derived_outputs, require_store_sources_unchanged, validate_store_destination
from emuflow.common.streaming_json import StreamingJSONObjectReader
from emuflow.common.tool_runtime import NativeToolProcess


class _TimingMetadataArray(Sequence):
    """Borrow one immutable manifest array; do not copy its records into Python."""
    def __init__(self, key, count, sources):
        self.key, self.count, self.sources = key, count, sources

    def __len__(self):
        return self.count

    def __iter__(self):
        require_store_sources_unchanged(self.sources)
        try:
            if not self.count:
                return
            with self.sources[0].path.open("r", encoding="utf-8") as stream:
                reader = StreamingJSONObjectReader(stream)
                for key in reader.object_keys():
                    if key != self.key:
                        reader.skip_value()
                        continue
                    count = 0
                    for record in reader.array_values():
                        count += 1
                        yield record
                    if count != self.count:
                        raise ValidationError("routed timing metadata array count disagrees")
                    # The immutable producer already sealed the whole document.
                    # A small array query must not rescan a later large array.
                    return
                raise ValidationError("routed timing metadata array is absent")
        except (ValueError, UnicodeError, OSError) as error:
            raise ValidationError("routed timing metadata is invalid") from error
        finally:
            require_store_sources_unchanged(self.sources)

    def __getitem__(self, index):
        if isinstance(index, slice):
            positions = range(*index.indices(self.count))
            if not positions:
                return []
            start, stop, step = positions.start, positions.stop, positions.step
            with closing(iter(self)) as records:
                if step > 0:
                    return list(islice(records, start, stop, step))
                return list(islice(records, positions[-1], start+1, -step))[::-1]
        if not isinstance(index, int):
            raise TypeError("metadata array index must be an integer")
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        with closing(iter(self)) as records:
            for ordinal, record in enumerate(records):
                if ordinal == index:
                    return record
        raise ValidationError("routed timing metadata member is absent")


def bind_routed_timing_sources(path, value):
    from tests.routed_timing_adapter import _timing_payload_path
    payload, _sha, _count = _timing_payload_path(path, value)
    metadata = value.get("virtual_io")
    if isinstance(metadata, _TimingMetadataArray):
        sources = metadata.sources
        if (len(sources) != 2 or sources[0].path != Path(path).resolve()
                or sources[1].path != payload.resolve()):
            raise ValidationError("routed timing view belongs to another artifact")
        require_store_sources_unchanged(sources)
        return sources
    return (bind_store_source(path, "xilinx-routed-timing-v2"),
            bind_store_source(payload, "xilinx-routed-endpoints-v1"))


def build_native_routed_endpoints(mapped, packed, placement, route_path, route,
                                  output_path, source, route_validation, timing_value_sink):
    from emuflow.physical.rapidwright.xilinx_rwroute import _route_payload_path, XILINX_ROUTE_NET_PAYLOAD_FORMAT
    require_native_schema("routed-endpoint-request-v1")
    executable = resolve_native_executable("emuflow-native")
    physical_sources = []
    for value in (packed, placement):
        bind = getattr(value.get("clusters"), "bound_store_sources", None)
        if not callable(bind):
            raise ValidationError("routed endpoint binding requires the typed physical handoff")
        physical_sources.append(bind()[0])
    if physical_sources[0] != physical_sources[1]:
        raise ValidationError("packing and placement belong to different handoff generations")
    mapped_sources = bind_view_sources((mapped, "xilinx-mapped-index-v2"))
    if len(mapped_sources) != 1:
        raise ValidationError("routed endpoint binding requires the current mapped owner")
    nets, _, _ = _route_payload_path(route_path, route["payloads"]["nets"], field="nets", expected_format=XILINX_ROUTE_NET_PAYLOAD_FORMAT)
    generations = route_validation.get("source_generations")
    if (not isinstance(generations, dict) or set(generations) != {"route", "nets", "pips"}
            or any(not isinstance(g, (tuple, list)) or len(g) != 7 or any(type(v) is not int for v in g)
                   for g in generations.values())):
        raise ValidationError("routed endpoint binding requires the current route generation seal")
    route_source = StoreSource(Path(route_path).resolve(), "xilinx-route-manifest-v2", tuple(generations["route"]))
    net_source = StoreSource(nets.resolve(), "xilinx-route-nets-v1", tuple(generations["nets"]))
    inputs = (*mapped_sources, physical_sources[0], route_source, net_source)
    # PIP legality belongs to the completed independent route qualification.
    # This consumer uses its seal and the net stream, not the PIP payload. Do
    # not keep a new file-lifetime dependency after that payload's last reader.
    sources = inputs
    output_path = Path(output_path)
    endpoint_path = output_path.with_name(output_path.name + ".endpoints.jsonl")
    for path in (output_path, endpoint_path):
        validate_store_destination(path, sources)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        with tempfile.TemporaryDirectory(prefix=".routed-endpoints.", dir=output_path.parent) as temporary:
            root = Path(temporary)
            for name in (endpoint_path.name, output_path.name):
                descriptor, filename = tempfile.mkstemp(prefix="."+name+".", suffix=".tmp", dir=output_path.parent)
                os.close(descriptor)
                staged.append(Path(filename))
            endpoint, manifest = staged
            request, summary = root/"request.bin", root/"summary.json"
            writer = CheckedRequestWriter(request)
            try:
                writer.put("QI", 0x31505452464D5545, 1)
                for item in inputs:
                    writer.put("3Q4q", *item.generation)
                writer.text(json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False))
                writer.text(endpoint_path.name)
                writer.finish()
            finally:
                writer.close()
            require_store_sources_unchanged(sources)
            with NativeToolProcess(executable, [executable, "physical", "build-routed-endpoints",
                                               *(str(item.path) for item in inputs), str(request),
                                               str(endpoint), str(manifest), str(summary)],
                                   environment=dict(os.environ), scratch_parent=root) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError("native routed endpoint binding failed: " + completed.stdout)
            require_store_sources_unchanged(sources)
            if summary.stat().st_size > 65536:
                raise ValidationError("native routed endpoint summary exceeds bound")
            result = json.loads(summary.read_text())
            if (result.get("schema") != "emuflow.routed-endpoint-summary/v1" or result.get("status") != "pass"
                    or result.get("implementation_id") != "native-routed-endpoints-v1"
                    or any(type(result.get(key)) is not int or result[key] < 0 for key in ("clock_gates", "virtual_io"))
                    or not isinstance(result.get("timing_sha256"), str) or len(result["timing_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in result["timing_sha256"])):
                raise ValidationError("native routed endpoint summary disagrees")
            header = json.loads(result["header_json"])
            if (header.get("source") != source or header.get("schema") != "emuflow.xilinx-routed-timing/v2"
                    or header.get("status") != "pass"
                    or header.get("payloads", {}).get("endpoints", {}).get("path") != endpoint_path.name):
                raise ValidationError("native routed timing header disagrees")
            publish_derived_outputs(((endpoint, endpoint_path, None), (manifest, output_path, None)), sources=sources)
            timing_sources = (bind_store_source(output_path, "xilinx-routed-timing-v2"),
                              bind_store_source(endpoint_path, "xilinx-routed-endpoints-v1"))
            header["clock_gates"] = _TimingMetadataArray("clock_gates", result["clock_gates"], timing_sources)
            header["virtual_io"] = _TimingMetadataArray("virtual_io", result["virtual_io"], timing_sources)
            if timing_value_sink is not None:
                timing_value_sink.clear()
                timing_value_sink.update(header)
            return {"status": "pass", "schema": "emuflow.xilinx-routed-timing-validation/v1", **header["summary"],
                    "timing_sha256": result["timing_sha256"], "implementation_id": result["implementation_id"],
                    "native_metrics": completed.native_metrics}
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
