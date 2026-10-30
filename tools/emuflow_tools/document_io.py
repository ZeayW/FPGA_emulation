"""Explicit inspection of typed artifact selectors for independent tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from emuflow.common.json_io import read_json as read_json_object
from emuflow.common.json_io import json_writes_are_durable, json_write_policy, write_json, _sha256_for_identity, file_sha256


def read_json(path: Path) -> Dict[str, Any]:
    value = read_json_object(path)
    schema = value.get("schema")
    if schema == "emuflow.managed-json-storage/v1":
        from emuflow_tools.storage.managed_json_storage import expand_managed_json

        return expand_managed_json(value, path)
    if isinstance(schema, str) and schema.startswith("emuflow.tdm-schedule-storage/"):
        from emuflow.scheduling.tdm_schedule_storage import expand_tdm_schedule_storage

        return expand_tdm_schedule_storage(value, path)
    if schema == "emuflow.system-route-storage/v1":
        from emuflow_tools.routing.system_route_storage import expand_system_route_storage

        return expand_system_route_storage(value, path)
    if schema == "emuflow.qor-report-storage/v1":
        from emuflow_tools.timing.qor_storage import expand_qor_report_storage

        return expand_qor_report_storage(value, path)
    if schema in {
        "emuflow.transport-storage/v1",
        "emuflow.logical-lane-map-storage/v1",
        "emuflow.virtual-io-anchors-storage/v1",
        "emuflow.virtual-board-binding-storage/v1",
    }:
        from emuflow_tools.transport.transport_storage import expand_transport_storage

        return expand_transport_storage(value, path)
    if isinstance(schema, str) and schema.startswith('emuflow.phase3-clusters-storage/'):
        from emuflow.partition.phase3_storage import INDEXED_CLUSTERS_SCHEMA, read_phase3_clusters_selector
        if schema != INDEXED_CLUSTERS_SCHEMA:
            raise ValueError('retired or unsupported Phase 3 clusters storage schema')
        return read_phase3_clusters_selector(path, value)
    if isinstance(schema, str) and schema.startswith('emuflow.phase3-assignment-storage/'):
        from emuflow.partition.phase3_storage import PACKED_ASSIGNMENT_SCHEMA, read_phase3_assignment_view
        if schema != PACKED_ASSIGNMENT_SCHEMA:
            raise ValueError('retired or unsupported Phase 3 assignment storage schema')
        return read_phase3_assignment_view(path)
    return value
