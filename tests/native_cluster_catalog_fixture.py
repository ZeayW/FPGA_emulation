"""Bounded request driver for native catalog record and corruption tests.

This has no stage entry, selector transaction, graph construction or search.
The current complete native stage owns production cluster publication.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile

from emuflow.errors import ValidationError
from emuflow.native_protocol import CheckedRequestWriter
from emuflow.native_tools import resolve_native_executable
from emuflow.store_publication import StoreSource, require_store_sources_unchanged
from emuflow.tool_runtime import NativeToolProcess


def catalog_sources(clusters, eligible):
    if len(clusters) > 65536:
        raise ValidationError("catalog qualification fixture exceeds bound")
    if getattr(clusters, "native_artifact", None) is None:
        if getattr(clusters, "implementation_id", None) == "native-primary-clusters-v1":
            raise ValidationError("native cluster catalog ownership is missing")
        return ()
    sources = (
        StoreSource(
            Path(clusters.native_source_path).resolve(),
            "emuir-compact-v2",
            clusters.native_source_generation,
        ),
        StoreSource(
            Path(clusters.native_artifact).resolve(),
            "partition-cluster-columns-v2",
            clusters.native_generation,
        ),
    )
    candidate = (
        getattr(eligible, "_source", None)
        if not getattr(eligible, "_materialized", False)
        else None
    )
    if getattr(candidate, "implementation_id", None) == "native-candidate-ids-v1":
        if (
            candidate.native_source_generation != clusters.native_source_generation
            or Path(candidate.native_source_path).resolve() != sources[0].path
        ):
            raise ValidationError(
                "cluster eligibility belongs to another source artifact"
            )
        sources += (
            StoreSource(
                Path(candidate.native_artifact).resolve(),
                "candidate-id-columns-v1",
                candidate.native_generation,
            ),
        )
    require_store_sources_unchanged(sources)
    return sources


def write_catalog_request(request, value, document, eligible, eligible_present, sources):
    clusters = value["clusters"]
    if len(clusters) > 65536 or len(eligible) > 65536:
        raise ValidationError("catalog qualification fixture exceeds bound")
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QI", 0x31514343464D5545, 1)
        for source in sources[:2]:
            writer.put("3Q4q", *source.generation)
        writer.text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        candidate = (
            getattr(eligible, "_source", None)
            if not getattr(eligible, "_materialized", False)
            else None
        )
        bits = getattr(candidate, "_ordinal_bits", None)
        mode = 0 if len(sources) == 3 else 1 if bits is not None else 2
        writer.put("II", int(eligible_present), mode)
        if mode == 0:
            writer.put("3Q4q", *sources[2].generation)
        elif mode == 1:
            writer.put("Q", len(bits))
            writer.bytes(bits)
        else:
            writer.put("Q", len(eligible))
            for name in eligible:
                writer.text(name)
        fixed, groups = clusters._fixed_by_cluster, clusters._groups_by_cluster
        writer.put("Q", len(fixed))
        for cluster in sorted(fixed):
            writer.put("I", cluster)
            writer.text(fixed[cluster])
        writer.put("Q", len(groups))
        for cluster in sorted(groups):
            writer.put("II", cluster, len(groups[cluster]))
            for name in groups[cluster]:
                writer.text(name)
        writer.finish()
    finally:
        writer.close()


def build_native_cluster_catalog(
    path, value, document, eligible, eligible_present, sources
):
    if sys.byteorder != "little":
        raise ValidationError(
            "native cluster publication requires little-endian storage"
        )
    clusters = value["clusters"]
    require_store_sources_unchanged(sources)
    with tempfile.TemporaryDirectory(
        prefix=".cluster-catalog-", dir=path.parent
    ) as temporary:
        request = Path(temporary) / "request.bin"
        write_catalog_request(request, value, document, eligible, eligible_present, sources)
        require_store_sources_unchanged(sources)
        executable = resolve_native_executable("emuflow-native")
        command = [
            executable,
            "phase3",
            "publish-cluster-catalog",
            str(sources[0].path),
            str(sources[1].path),
            str(request),
        ]
        if len(sources) == 3:
            command.append(str(sources[2].path))
        command.append(str(path))
        with NativeToolProcess(
            executable,
            command,
            environment=dict(os.environ),
            timeout_ms=60000,
            scratch_parent=Path(temporary),
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native cluster publication failed: " + completed.stdout
            )
        require_store_sources_unchanged(sources)
    return {
        "clusters": len(clusters),
        "instances": len(clusters.source_cluster_indices),
        "eligible_nets": len(eligible),
    }
