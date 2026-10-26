"""Independent small-fixture exact-binary oracle; never a production route."""
from array import array
from pathlib import Path
from typing import Mapping, Any
from emuflow.errors import ValidationError
from emuflow.phase3_exact_records import STATIC_EXACT_CAPTURE_BIT_MAX_BITS
from emuflow.phase3_exact_binary import (_load_manifest, _read_nets, _SectionReader, _section, _U32,
    _iter_captures, _iter_segments, _number, _MISSING, iter_phase3_exact_node_sources)


def _validate_node_source_population(
    path: Path, manifest: Mapping[str, Any]
) -> None:
    """Prove the nested source count before count-sized validation buffers."""

    section = _section(manifest, "node_sources")
    expected = manifest["counts"]["source_segments"]
    observed = 0
    reader = _SectionReader(path, section)
    try:
        for _ in range(int(section["records"])):
            count = _U32.unpack(reader.read(_U32.size))[0]
            if count > reader.remaining // _U32.size:
                raise ValidationError(
                    "Static Exact binary source count is invalid"
                )
            observed += count
            if observed > expected:
                raise ValidationError(
                    "Static Exact binary source population disagrees"
                )
            for _ in range(count):
                reader.read(_U32.size)
        reader.finish()
    finally:
        reader.close()
    if observed != expected:
        raise ValidationError("Static Exact binary source population disagrees")



def validate_reference(
    path: Path, descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    """Independently scan every section with bounded integer state."""

    manifest = _load_manifest(path, descriptor)
    nets = _read_nets(path, manifest)
    net_index = {net: index for index, net in enumerate(nets)}
    # Source population is nested inside one record per cut.  Scan it before
    # allocating any buffer sized by manifest population counts.
    _validate_node_source_population(path, manifest)
    counts = manifest.get("counts", {})
    segment_count = counts.get("logic_segments")
    capture_count = counts.get("capture_requirements")
    if (
        isinstance(segment_count, bool)
        or not isinstance(segment_count, int)
        or isinstance(capture_count, bool)
        or not isinstance(capture_count, int)
        or segment_count < 0
        or capture_count < 0
        or counts.get("cut_nodes") != len(nets)
    ):
        raise ValidationError("Static Exact binary population counts are invalid")

    capture_seen = bytearray(capture_count)
    capture_parts = bytearray(capture_count)
    capture_nets = array("I", [_MISSING]) * capture_count
    observed_captures = 0
    for part, fpga in enumerate(manifest["parts"]):
        if part >= 255 or not isinstance(fpga, str) or not fpga:
            raise ValidationError("Static Exact binary part is invalid")
        for capture in _iter_captures(path, manifest, part, nets):
            number = _number(capture["id"], "capture")
            if number >= capture_count or capture_seen[number]:
                raise ValidationError("Static Exact binary capture identity repeats")
            capture_seen[number] = 1
            capture_parts[number] = part
            capture_nets[number] = net_index[capture["cut_net"]]
            observed_captures += 1
    if observed_captures != capture_count or (capture_count and not all(capture_seen)):
        raise ValidationError("Static Exact binary capture coverage disagrees")

    segment_seen = bytearray(segment_count)
    segment_target_nets = array("I", [_MISSING]) * segment_count
    capture_bound = bytearray(capture_count)
    dependency_edges = 0
    consumer_segments = 0
    observed_segments = 0
    for part in range(len(manifest["parts"])):
        for segment in _iter_segments(path, manifest, part, nets):
            number = _number(segment["id"], "segment")
            if number >= segment_count or segment_seen[number]:
                raise ValidationError("Static Exact binary segment identity repeats")
            segment_seen[number] = 1
            observed_segments += 1
            if segment["kind"] == "rx_to_tx":
                dependency_edges += 1
                consumer_segments += 1
                segment_target_nets[number] = net_index[
                    segment["sink_cut_net"]
                ]
            elif segment["kind"] == "rx_to_capture":
                consumer_segments += 1
                capture = _number(segment["capture_requirement"], "capture")
                if (
                    capture >= capture_count
                    or capture_bound[capture]
                    or capture_parts[capture] != part
                    or nets[capture_nets[capture]] != segment["source_cut_net"]
                ):
                    raise ValidationError("Static Exact capture binding disagrees")
                capture_bound[capture] = 1
            else:
                segment_target_nets[number] = net_index[
                    segment["sink_cut_net"]
                ]
    if observed_segments != segment_count or (segment_count and not all(segment_seen)):
        raise ValidationError("Static Exact binary segment coverage disagrees")
    if capture_count and not all(capture_bound):
        raise ValidationError("Static Exact binary capture binding is incomplete")
    if consumer_segments != counts.get("consumer_segments"):
        raise ValidationError("Static Exact consumer population disagrees")

    source_seen = bytearray(segment_count)
    source_count = 0
    node_count = 0
    for net, source_numbers in iter_phase3_exact_node_sources(path, descriptor):
        node_count += 1
        target_net = net_index[net]
        for number in source_numbers:
            if (
                number >= segment_count
                or source_seen[number]
                or segment_target_nets[number] != target_net
            ):
                raise ValidationError("Static Exact source segment is invalid")
            source_seen[number] = 1
            source_count += 1
    if node_count != len(nets) or source_count != counts.get("source_segments"):
        raise ValidationError("Static Exact source population disagrees")
    if any(
        target != _MISSING and not source_seen[number]
        for number, target in enumerate(segment_target_nets)
    ):
        raise ValidationError("Static Exact source coverage is incomplete")

    metrics = manifest.get("contract_header", {}).get("metrics", {})
    if (
        metrics.get("transported_cut_nets") != len(nets)
        or metrics.get("logic_segments") != segment_count
        or metrics.get("capture_requirements") != capture_count
        or metrics.get("dependency_edges") != dependency_edges
    ):
        raise ValidationError("Static Exact binary metrics disagree")
    return {
        "status": "pass",
        "schema": manifest["schema"],
        **(
            {
                "capture_bit_max_bits": STATIC_EXACT_CAPTURE_BIT_MAX_BITS
            }
            if manifest["_format_version"] == 2
            else {}
        ),
        "semantic_contract_sha256": manifest["semantic_contract_sha256"],
        "cut_nodes": len(nets),
        "source_segments": source_count,
        "logic_segments": segment_count,
        "capture_requirements": capture_count,
        "dependency_edges": dependency_edges,
    }
