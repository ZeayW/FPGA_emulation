"""Versioned timing bounds for directed inter-FPGA board links."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Tuple

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform


BOARD_LINK_TIMING_SCHEMA = "emuflow.board-link-timing/v1"
_QUALIFICATIONS = {
    "model-only",
    "characterized-upper-bound",
    "measured-upper-bound",
}


def directed_board_links(
    platform: Platform,
) -> Dict[Tuple[str, str, str], Any]:
    """Return every legal ``(link, source, sink)`` BoardDB arc."""
    result = {}
    for link in platform.links:
        left, right = link.endpoints
        directions = [(left, right)]
        if link.direction in {"full_duplex", "half_duplex"}:
            directions.append((right, left))
        for source, sink in directions:
            result[(link.id, source, sink)] = link
    return result


def build_board_link_timing_model(
    platform: Platform,
    *,
    reference: str = "BoardDB declared interconnect model",
) -> Dict[str, Any]:
    """Materialize each link at the minimum supported TDM ratio."""
    if not reference:
        raise ValidationError("board link timing reference is empty")
    records = []
    for (link_id, source, sink), link in sorted(
        directed_board_links(platform).items()
    ):
        slot_ns = 1000.0 / link.fabric_clock_mhz
        records.append(
            {
                "link": link_id,
                "from": source,
                "to": sink,
                "fabric_clock_mhz": link.fabric_clock_mhz,
                "latency_cycles": link.latency_cycles,
                "delay_bound_ns": (link.tdm_delay_model.delay_ns(
                                       link.tdm_delay_model.min_ratio)
                                   if link.tdm_delay_model is not None
                                   else link.latency_cycles * slot_ns),
                **({"tdm_delay_model": link.tdm_delay_model.to_dict(),
                    "reference_tdm_ratio": link.tdm_delay_model.min_ratio}
                   if link.tdm_delay_model is not None else {}),
                "qualification": "model-only",
                "source": {
                    "kind": "boarddb-model",
                    "reference": reference,
                },
            }
        )
    database = {
        "schema": BOARD_LINK_TIMING_SCHEMA,
        "status": "pass",
        "platform": platform.name,
        "measurement_scope": "tx-transport-stage-to-rx-transport-stage",
        "final_link_timing_signoff": False,
        "links": records,
    }
    validate_board_link_timing(database, platform)
    return database


def validate_board_link_timing(
    database: Mapping[str, Any], platform: Platform
) -> Dict[str, Any]:
    if database.get("schema") != BOARD_LINK_TIMING_SCHEMA:
        raise ValidationError("board link timing schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("board link timing did not pass")
    if database.get("platform") != platform.name:
        raise ValidationError("board link timing platform disagrees")
    if database.get("measurement_scope") != (
        "tx-transport-stage-to-rx-transport-stage"
    ):
        raise ValidationError("board link timing scope is invalid")
    expected = directed_board_links(platform)
    records = database.get("links")
    if not isinstance(records, list):
        raise ValidationError("board link timing records are invalid")
    actual = {}
    qualifications = []
    for index, record in enumerate(records):
        context = f"board link timing links[{index}]"
        if not isinstance(record, dict):
            raise ValidationError(f"{context} is invalid")
        key = (record.get("link"), record.get("from"), record.get("to"))
        link = expected.get(key)
        if link is None or key in actual:
            raise ValidationError(f"{context} identity is invalid")
        frequency = record.get("fabric_clock_mhz")
        delay = record.get("delay_bound_ns")
        latency = record.get("latency_cycles")
        qualification = record.get("qualification")
        source = record.get("source")
        if (
            isinstance(frequency, bool)
            or not isinstance(frequency, (int, float))
            or not math.isclose(
                float(frequency),
                link.fabric_clock_mhz,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
            or isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
            or isinstance(latency, bool)
            or not isinstance(latency, int)
            or latency != link.latency_cycles
            or qualification not in _QUALIFICATIONS
            or not isinstance(source, dict)
            or source.get("kind") not in {
                "boarddb-model",
                "vendor-characterization",
                "hardware-measurement",
            }
            or not isinstance(source.get("reference"), str)
            or not source["reference"]
        ):
            raise ValidationError(f"{context} contract is invalid")
        expected_source_kind = {
            "model-only": "boarddb-model",
            "characterized-upper-bound": "vendor-characterization",
            "measured-upper-bound": "hardware-measurement",
        }[qualification]
        if link.tdm_delay_model is not None:
            if (record.get("tdm_delay_model") != link.tdm_delay_model.to_dict()
                    or record.get("reference_tdm_ratio")
                    != link.tdm_delay_model.min_ratio
                    or not math.isclose(
                        delay,
                        link.tdm_delay_model.delay_ns(
                            link.tdm_delay_model.min_ratio
                        ),
                        abs_tol=1e-9,
                    )
                    or qualification != "model-only"):
                raise ValidationError(f"{context}: TDM ratio model disagrees with BoardDB")
        elif "tdm_delay_model" in record:
            raise ValidationError(f"{context}: unexpected TDM ratio model")
        if source["kind"] != expected_source_kind:
            raise ValidationError(f"{context} measurement claim is invalid")
        observations = source.get("observations")
        if source["kind"] == "hardware-measurement" and (
            isinstance(observations, bool)
            or not isinstance(observations, int)
            or observations <= 0
        ):
            raise ValidationError(f"{context} observations are invalid")
        actual[key] = float(delay)
        qualifications.append(qualification)
    if set(actual) != set(expected):
        raise ValidationError("board link timing coverage disagrees")
    final_signoff = bool(records) and all(
        item == "measured-upper-bound" for item in qualifications
    )
    if database.get("final_link_timing_signoff") is not final_signoff:
        raise ValidationError("board link timing signoff claim is invalid")
    return {
        "status": "pass",
        "platform": platform.name,
        "directed_links": len(actual),
        "maximum_delay_bound_ns": max(actual.values(), default=0.0),
        "model_only_links": sum(
            item == "model-only" for item in qualifications
        ),
        "characterized_links": sum(
            item == "characterized-upper-bound" for item in qualifications
        ),
        "measured_links": sum(
            item == "measured-upper-bound" for item in qualifications
        ),
        "final_link_timing_signoff": final_signoff,
    }


def directed_route_link_delays(
    database: Mapping[str, Any], platform: Platform
) -> Tuple[Dict[str, Dict[str, Dict[str, float]]], Dict[str, Any]]:
    """Project BoardLinkTimingDB into direction-exact route constraints."""
    validation = validate_board_link_timing(database, platform)
    projected: Dict[str, Dict[str, Dict[str, float]]] = {}
    for record in sorted(
        database["links"],
        key=lambda item: (item["link"], item["from"], item["to"]),
    ):
        projected.setdefault(record["link"], {}).setdefault(
            record["from"], {}
        )[record["to"]] = float(record["delay_bound_ns"])
    return projected, {
        "status": "pass",
        "projection": "direction-exact-link-upper-bounds",
        "directed_links": validation["directed_links"],
        "maximum_route_link_delay_ns": max(
            (
                delay
                for by_source in projected.values()
                for by_sink in by_source.values()
                for delay in by_sink.values()
            ),
            default=0.0,
        ),
    }
