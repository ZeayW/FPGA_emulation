"""Source-bound route/schedule identities for the current native pipeline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform
from emuflow.routing.system_route_storage import SystemRoutesView, pretty_system_routes_sha256
from emuflow.scheduling.tdm_schedule_storage import TDMScheduleView, pretty_tdm_schedule_sha256

CROSS_LAYER_TIMING_CONTRACT_SCHEMA = "emuflow.cross-layer-timing/v3"
CROSS_LAYER_PHYSICAL_BINDING_SCHEMA = "emuflow.cross-layer-physical-binding/v1"
CROSS_LAYER_PHYSICAL_BINDING_PROVIDER = "provider-neutral-routed-physical-evidence-v1"


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    if isinstance(value, TDMScheduleView):
        return pretty_tdm_schedule_sha256(value)
    if isinstance(value, SystemRoutesView):
        return pretty_system_routes_sha256(value)
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def build_cross_layer_timing_contract(
    routes: Mapping[str, Any],
    schedule: Optional[Mapping[str, Any]] = None,
    semantic_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project only current typed owners; legacy Python construction is retired."""
    from emuflow.scheduling.phase5_timing_native import has_native_timing_inputs, project_native_schedule_timing
    if not has_native_timing_inputs(routes, schedule) or not (
        isinstance(semantic_contract, dict) and semantic_contract.get("indexed") is True
    ):
        raise ValidationError("cross-layer timing requires current typed owners and indexed semantics")
    if "semantic_contract" in routes:
        raise ValidationError("cross-layer routes may not duplicate the Phase 3 semantic contract")
    if semantic_contract.get("schema") != routes.get("semantic_contract_schema"):
        raise ValidationError("cross-layer semantic binding is incomplete")
    return project_native_schedule_timing(routes, schedule)["contract"]


def validate_cross_layer_timing_contract(
    routes: Mapping[str, Any], contract: Mapping[str, Any],
    schedule: Optional[Mapping[str, Any]] = None,
    semantic_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Independently recompute current source-bound relations in the native core."""
    if contract.get("schema") != CROSS_LAYER_TIMING_CONTRACT_SCHEMA:
        raise ValidationError("current typed cross-layer timing requires v3")
    try:
        expected = build_cross_layer_timing_contract(routes, schedule, semantic_contract)
    except ValidationError as error:
        raise ValidationError("cross-layer timing contract does not match canonical typed inputs: "+str(error)) from error
    if contract != expected:
        raise ValidationError("cross-layer timing contract does not match canonical inputs")
    return {"status": "pass", **expected["metrics"]}


def build_cross_layer_physical_binding(
    contract: Mapping[str, Any], schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any], platform: Platform,
    assignment_index_path: Optional[Path] = None, *, output_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Publish the source-bound native coverage owner; JSON is only a selector."""
    from emuflow.physical.physical_coverage import build_physical_coverage
    return build_physical_coverage(contract,schedule,physical_summary,platform,
        assignment_index_path,output_path=output_path)


def validate_cross_layer_physical_binding(
    contract: Mapping[str, Any], schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any], platform: Platform,
    binding: Mapping[str, Any], assignment_index_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently join original facts against the published typed relations."""
    from emuflow.physical.physical_coverage import audit_physical_coverage
    return audit_physical_coverage(contract,schedule,physical_summary,platform,
                                  binding,assignment_index_path)
