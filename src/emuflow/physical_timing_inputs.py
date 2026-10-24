"""Validated physical arc readers shared by the OpenSTA event binder.

These readers check identities, coverage and finite measured values. They do
not propagate event arrivals, compute path slack or implement a timing engine.
"""
from __future__ import annotations

import heapq
import math
from typing import Any, Dict, List, Mapping, Optional
from .board_link_timing import validate_board_link_timing
from .boundary_timing import validate_boundary_timing_database
from .logic_segment_timing import validate_logic_segment_timing
from .errors import ValidationError
from .platform import Platform

def _finite_number(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{context} must be a finite number")
    return float(value)

def _endpoint_delay_database(
    physical_summary: Mapping[str, Any],
) -> Optional[Dict[str, float]]:
    raw_timing = physical_summary.get("boundary_timing")
    if raw_timing is None:
        return None
    identities = physical_summary.get("boundary_identities")
    if not isinstance(raw_timing, dict) or not isinstance(identities, dict):
        raise ValidationError("physical boundary timing/identity maps are invalid")
    if set(raw_timing) != set(identities):
        raise ValidationError("physical boundary timing FPGA coverage disagrees")
    result: Dict[str, float] = {}
    for fpga, database in raw_timing.items():
        validate_boundary_timing_database(database, identities[fpga])
        for endpoint in database["endpoints"]:
            endpoint_id = endpoint["id"]
            if endpoint_id in result:
                raise ValidationError(
                    f"duplicate physical boundary timing endpoint {endpoint_id!r}"
                )
            result[endpoint_id] = _finite_number(
                endpoint["delay_ns"],
                f"physical boundary endpoint {endpoint_id}",
            )
    return result

class _IndexedLogicSegmentMembers:
    def __init__(self, owner: "_IndexedLogicSegmentLookup", system_path: str):
        self.owner = owner
        self.system_path = system_path

    def get(self, member_path: str, default: Any = None) -> Any:
        records = self.owner.segments_for(self.system_path, member_path)
        return records if records else default

    def __getitem__(self, member_path: str) -> List[Mapping[str, Any]]:
        records = self.owner.segments_for(self.system_path, member_path)
        if not records:
            raise KeyError(member_path)
        return records

class _IndexedLogicSegmentLookup:
    """Small store set with indexed path/member lookup and no graph mirror."""

    def __init__(self, stores: Sequence[Mapping[str, Any]]):
        self.stores = stores

    def get(self, system_path: str, default: Any = None) -> Any:
        if not any(
            store.contains_system_path(system_path)  # type: ignore[attr-defined]
            for store in self.stores
        ):
            return default
        return _IndexedLogicSegmentMembers(self, system_path)

    def segments_for(
        self, system_path: str, member_path: str
    ) -> List[Mapping[str, Any]]:
        records = [
            segment
            for store in self.stores
            for segment in store.iter_segments_for(  # type: ignore[attr-defined]
                system_path, member_path
            )
        ]
        records.sort(key=lambda item: (item["cut_index"], item["id"]))
        return records

def _logic_segment_database(
    physical_summary: Mapping[str, Any],
) -> Optional[Any]:
    raw = physical_summary.get("logic_segment_timing")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("physical logic segment timing map is invalid")
    cached = getattr(raw, "_logic_collection_cache", None)
    if cached is not None:
        return cached
    from .logic_segment_timing_storage import (
        LogicSegmentTimingCollection,
        LogicSegmentTimingView,
    )

    result: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {}
    segment_ids = set()
    stores = []
    representations = set()
    source_identity = None
    for fpga, database in raw.items():
        indexed_view = isinstance(database, LogicSegmentTimingView)
        if indexed_view:
            validation_fpga = database.selector["source"]["fpga"]
        else:
            validation_fpga = validate_logic_segment_timing(database)["fpga"]
        if validation_fpga != fpga:
            raise ValidationError(
                "physical logic segment timing FPGA identity disagrees"
            )
        identity = (
            database.get("design"),
            database.get("platform"),
            database.get("semantic_contract_sha256"),
        )
        if source_identity is None:
            source_identity = identity
        elif identity != source_identity:
            raise ValidationError(
                "physical logic segment timing source seals disagree"
            )
        indexed = indexed_view or callable(
            getattr(database, "iter_segments_for", None)
        )
        representations.add(indexed)
        if indexed:
            stores.append(database)
            continue
        for segment in database["segments"]:
            segment_id = segment["id"]
            if segment_id in segment_ids:
                raise ValidationError(
                    f"duplicate physical logic segment {segment_id!r}"
                )
            segment_ids.add(segment_id)
            result.setdefault(segment["system_path"], {}).setdefault(
                segment["member_path"], []
            ).append(segment)
    if len(representations) > 1:
        raise ValidationError(
            "physical logic segment timing mixes indexed and legacy evidence"
        )
    if stores:
        if all(isinstance(store, LogicSegmentTimingView) for store in stores):
            collection = LogicSegmentTimingCollection(stores)
            try:
                raw._logic_collection_cache = collection
            except AttributeError:
                pass
            return collection
        previous = None
        for segment_id in heapq.merge(
            *(store.iter_segment_ids() for store in stores)  # type: ignore[attr-defined]
        ):
            if segment_id == previous:
                raise ValidationError(
                    f"duplicate physical logic segment {segment_id!r}"
                )
            previous = segment_id
        return _IndexedLogicSegmentLookup(stores)
    return result

def _board_link_delay_database(
    physical_summary: Mapping[str, Any], platform: Platform
) -> Optional[Dict[tuple[str, str, str], float]]:
    raw = physical_summary.get("board_link_timing")
    if raw is None:
        return None
    validate_board_link_timing(raw, platform)
    return {
        (item["link"], item["from"], item["to"]): float(
            item["delay_bound_ns"]
        )
        for item in raw["links"]
    }
