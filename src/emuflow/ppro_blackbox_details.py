"""Compact, path-preserving facts from ordinary PPro calibration reports.

These are observations, not a fitted platform.  In particular ``channelNum``
is retained as a reported quantity, not relabelled as a bank's capacity, and a
run-wide maximum TDM ratio is never attached to a timing path.  Until a report
exposes an unambiguous cross-FPGA segment and its ratio, single-hop delay is
explicitly not identifiable.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping

from .errors import ValidationError


DETAILS_SCHEMA = "emuflow.ppro-calibration-details/v1"
_FPGA = re.compile(r"^F[0-9]+$")
_SUMMARY = re.compile(
    r"^(Path[0-9]+)\s+(.+?)\s{2,}([0-9]+)\s+"
    r"(-?[0-9]+(?:\.[0-9]+)?)\s{2,}"
)
_PATH = re.compile(r"^(Path[0-9]+)\s*$")
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_ARRIVAL = re.compile(rf"^\s*({_NUMBER})\s+data arrival time\s*$", re.I)
_POINT = re.compile(rf"^\s*({_NUMBER})\s+({_NUMBER})\s+(.+?)\s*$")
_RATIO = re.compile(r"\bTDM\s+(?:ratio|Ratio)\s*[:=]\s*([0-9]+)\b", re.I)
_FPGA_IN_NAME = re.compile(r"(?<![A-Za-z0-9_])(?:FPGA_([0-9]+)|F([0-9]+))(?=/|\b)")
_BANK_HEADER = [
    "FPGAID", "targetFPGAID", "BankID", "targetBankID", "dir",
    "eringDevID", "lineRate", "channelNum", "maxRatio",
]


def _token(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _number(value: Any, context: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValidationError(f"{context}: invalid finite number")
    return result


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context}: expected a nonnegative integer")
    return value


def _keys(value: Any, keys: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValidationError(f"{context}: unexpected or missing fields")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{context}: expected a list")
    return value


def _logical(value: str, aliases: Mapping[str, str]) -> str:
    raw = value.upper()
    if raw.isdigit():
        raw = f"F{raw}"
    if raw not in aliases:
        raise ValidationError("PPro bank report: unmapped physical FPGA alias")
    return aliases[raw]


def _descriptor(value: str, aliases: Mapping[str, str]) -> tuple[str, str | None]:
    """Keep short design/cell labels, replace physical FPGA identities.

    ``FPGA_n`` is not assumed to mean the physical route label F<n>.  Its
    opaque token and a null logical FPGA preserve that ambiguity.  Relative
    design hierarchy is useful evidence; absolute paths are never retained.
    """
    value = re.sub(r"^[\^v]\s+", "", value.strip())
    if len(value) > 256 or not value or any(c in value for c in "\n\r\x00"):
        raise ValidationError("PPro SSTA point: invalid descriptor")
    if value.startswith(("/", "~")) or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", value):
        raise ValidationError("PPro SSTA point: absolute paths are forbidden")
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        logical = aliases.get(raw) if match.group(2) else None
        if logical is not None:
            found.add(logical)
            return logical
        return "unmapped-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    sanitized = _FPGA_IN_NAME.sub(replace, value)
    return sanitized, next(iter(found)) if len(found) == 1 else None


def _parse_banks(text: str, aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    in_section = False
    header_seen = False
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("2.4 tdm_info report"):
            in_section = True
            continue
        if in_section and re.match(r"2\.[0-9]+\s", stripped):
            break
        if not in_section or not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells == _BANK_HEADER:
            header_seen = True
            continue
        if not cells or not _FPGA.fullmatch(cells[0].upper()):
            continue
        if not header_seen or len(cells) != len(_BANK_HEADER):
            raise ValidationError("PPro bank report: unsupported tdm_info columns")
        local, peer = _logical(cells[0], aliases), _logical(cells[1], aliases)
        direction = cells[4].lower()
        if local == peer or direction not in {"input", "output"}:
            raise ValidationError("PPro bank report: invalid endpoints or direction")
        if not all(re.fullmatch(r"[0-9]+", cells[i]) for i in (2, 3, 7, 8)):
            raise ValidationError("PPro bank report: invalid count or bank identifier")
        try:
            line_rate = float(cells[6])
        except ValueError as error:
            raise ValidationError("PPro bank report: invalid reported line rate") from error
        endpoints = sorted(((local, cells[2]), (peer, cells[3])))
        rows.append({
            "bank_pair_id": _token("bank", repr(endpoints)),
            "local_fpga": local,
            "peer_fpga": peer,
            "direction": direction,
            "reported_channel_count": int(cells[7]),
            "maximum_tdm_ratio": int(cells[8]),
            "reported_line_rate": _number(line_rate, "reported line rate"),
        })
    # Reciprocal endpoint rows remain separate facts.  Their channel counts
    # must never be summed as two independent banks by a capacity consumer.
    return sorted(rows, key=lambda row: (row["bank_pair_id"], row["local_fpga"], row["direction"]))


def _section(line: str, current: str) -> str:
    match = re.match(r"^\s*##\s*(Setup|Hold)\s*##", line, re.I)
    return match.group(1).lower() if match else current


def _parse_paths(text: str, aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    summaries: dict[tuple[str, str], tuple[bool, int]] = {}
    section = "unspecified"
    for line in text.splitlines():
        section = _section(line, section)
        match = _SUMMARY.match(line.strip())
        if match is None:
            continue
        key = (section, match.group(1))
        value = ("unconstrained" not in match.group(2).lower(), int(match.group(3)))
        if key in summaries and summaries[key] != value:
            raise ValidationError("PPro SSTA report: ambiguous duplicate path identifier")
        summaries[key] = value

    paths: dict[tuple[str, str], dict[str, Any]] = {}
    section = "unspecified"
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        section = _section(line, section)
        start = _PATH.fullmatch(line.strip())
        if start is not None:
            key = (section, start.group(1))
            current = None
            if key not in summaries:
                continue
            if key in paths:
                raise ValidationError("PPro SSTA report: repeated path details")
            constrained, crossings = summaries[key]
            current = {
                "id": f"{section}-{start.group(1).lower()}",
                "constrained": constrained,
                "reported_cross_fpga_count": crossings,
                "arrival_ns": None,
                "startpoint_token": None,
                "endpoint_token": None,
                "points": [],
                "crossings": [],
            }
            paths[key] = current
            continue
        if current is None or current["arrival_ns"] is not None:
            # Ignore the later negative arrival operand in the slack summary.
            continue
        for marker, field in (("Startpoint:", "startpoint_token"), ("Endpoint:", "endpoint_token")):
            if line.strip().startswith(marker):
                descriptor, _ = _descriptor(line.strip()[len(marker):], aliases)
                current[field] = _token("pin", descriptor)
        arrival = _ARRIVAL.match(line)
        if arrival is not None:
            current["arrival_ns"] = _number(float(arrival.group(1)), "SSTA arrival")
            continue
        point = _POINT.match(line)
        if point is None:
            continue
        descriptor, fpga = _descriptor(point.group(3), aliases)
        ratio = _RATIO.search(descriptor)
        current["points"].append({
            "id": f"point{len(current['points'])}",
            "pin_token": _token("pin", descriptor),
            "descriptor": descriptor,
            "fpga": fpga,
            "increment_ns": _number(float(point.group(1)), "SSTA increment", nonnegative=False),
            "cumulative_ns": _number(float(point.group(2)), "SSTA cumulative", nonnegative=False),
            "reported_tdm_ratio": int(ratio.group(1)) if ratio else None,
            "kind": "pin" if "/" in descriptor else "other",
        })
    return sorted(paths.values(), key=lambda path: path["id"])


def parse_calibration_details(
    route_text: str, timing_text: str, aliases: Mapping[str, str]
) -> dict[str, Any]:
    """Extract evidence without claiming bank inventory or segment timing."""
    banks = _parse_banks(route_text, aliases)
    paths = _parse_paths(timing_text, aliases)
    reasons = ["bank-inventory-not-reported", "crossing-boundary-and-ratio-not-bound"]
    if not banks:
        reasons.append("no-bank-allocation-rows")
    if not paths:
        reasons.append("no-path-specific-timing")
    if any(point["fpga"] is None and "unmapped-" in point["descriptor"]
           for path in paths for point in path["points"]):
        reasons.append("unmapped-timing-fpga-label")
    return validate_calibration_details({
        "schema": DETAILS_SCHEMA,
        "bank_rows": banks,
        "timing_paths": paths,
        "identification": {
            "bank_inventory": "not_identifiable",
            "single_hop_delay": "not_identifiable",
            "reasons": sorted(reasons),
        },
    })


def validate_calibration_details(value: Any) -> dict[str, Any]:
    """Validate the observation-only contract; no inferred fit eligibility."""
    root = _keys(value, {"schema", "bank_rows", "timing_paths", "identification"}, "calibration details")
    if root["schema"] != DETAILS_SCHEMA:
        raise ValidationError("calibration details: unsupported schema")
    banks = []
    bank_seen: set[tuple[str, str, str]] = set()
    for raw in _list(root["bank_rows"], "bank rows"):
        row = _keys(raw, {"bank_pair_id", "local_fpga", "peer_fpga", "direction", "reported_channel_count", "maximum_tdm_ratio", "reported_line_rate"}, "bank row")
        if not isinstance(row["bank_pair_id"], str) or not re.fullmatch(r"bank-[0-9a-f]{64}", row["bank_pair_id"]):
            raise ValidationError("bank row: invalid opaque bank identifier")
        if any(not isinstance(row[key], str) or not _FPGA.fullmatch(row[key]) for key in ("local_fpga", "peer_fpga")):
            raise ValidationError("bank row: invalid logical FPGA")
        key = (row["bank_pair_id"], row["local_fpga"], row["direction"])
        if row["local_fpga"] == row["peer_fpga"] or row["direction"] not in {"input", "output"} or key in bank_seen:
            raise ValidationError("bank row: duplicate or invalid directional endpoint")
        bank_seen.add(key)
        banks.append({**row,
            "reported_channel_count": _integer(row["reported_channel_count"], "bank channels"),
            "maximum_tdm_ratio": _integer(row["maximum_tdm_ratio"], "bank ratio"),
            "reported_line_rate": _number(row["reported_line_rate"], "bank line rate"),
        })
    paths = []
    path_seen: set[str] = set()
    for raw in _list(root["timing_paths"], "timing paths"):
        path = _keys(raw, {"id", "constrained", "reported_cross_fpga_count", "arrival_ns", "startpoint_token", "endpoint_token", "points", "crossings"}, "timing path")
        if not isinstance(path["id"], str) or not re.fullmatch(r"(?:setup|hold|unspecified)-path[0-9]+", path["id"]) or path["id"] in path_seen:
            raise ValidationError("timing path: invalid or duplicate identity")
        path_seen.add(path["id"])
        if type(path["constrained"]) is not bool:
            raise ValidationError("timing path: invalid constrained flag")
        for field in ("startpoint_token", "endpoint_token"):
            if path[field] is not None and (not isinstance(path[field], str) or not re.fullmatch(r"pin-[0-9a-f]{64}", path[field])):
                raise ValidationError("timing path: invalid endpoint token")
        if path["crossings"] != []:
            raise ValidationError("timing path: crossing extraction is not yet qualified")
        points = []
        for index, raw_point in enumerate(_list(path["points"], "timing points")):
            point = _keys(raw_point, {"id", "pin_token", "descriptor", "fpga", "increment_ns", "cumulative_ns", "reported_tdm_ratio", "kind"}, "timing point")
            if point["id"] != f"point{index}" or not isinstance(point["pin_token"], str) or not re.fullmatch(r"pin-[0-9a-f]{64}", point["pin_token"]):
                raise ValidationError("timing point: invalid identity")
            descriptor = point["descriptor"]
            if not isinstance(descriptor, str) or not descriptor or len(descriptor) > 256 or any(c in descriptor for c in "\n\r\x00") or descriptor.startswith(("/", "~")) or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", descriptor) or re.search(r"\bFPGA_[0-9]+\b", descriptor):
                raise ValidationError("timing point: invalid compact descriptor")
            if point["pin_token"] != _token("pin", descriptor):
                raise ValidationError("timing point: descriptor token mismatch")
            if point["fpga"] is not None and (not isinstance(point["fpga"], str) or not _FPGA.fullmatch(point["fpga"])):
                raise ValidationError("timing point: invalid logical FPGA")
            if point["kind"] not in {"pin", "other"}:
                raise ValidationError("timing point: invalid kind")
            points.append({**point,
                "increment_ns": _number(point["increment_ns"], "point increment", nonnegative=False),
                "cumulative_ns": _number(point["cumulative_ns"], "point cumulative", nonnegative=False),
                "reported_tdm_ratio": None if point["reported_tdm_ratio"] is None else _integer(point["reported_tdm_ratio"], "point ratio"),
            })
        paths.append({**path,
            "reported_cross_fpga_count": _integer(path["reported_cross_fpga_count"], "cross FPGA count"),
            "arrival_ns": None if path["arrival_ns"] is None else _number(path["arrival_ns"], "path arrival"),
            "points": points,
        })
    identification = _keys(root["identification"], {"bank_inventory", "single_hop_delay", "reasons"}, "identification")
    if any(identification[key] != "not_identifiable" for key in ("bank_inventory", "single_hop_delay")):
        raise ValidationError("calibration details: report facts cannot assert identified models")
    reasons = _list(identification["reasons"], "identification reasons")
    allowed = {"bank-inventory-not-reported", "crossing-boundary-and-ratio-not-bound", "no-bank-allocation-rows", "no-path-specific-timing", "unmapped-timing-fpga-label"}
    if not reasons or any(not isinstance(reason, str) or reason not in allowed for reason in reasons) or len(set(reasons)) != len(reasons):
        raise ValidationError("calibration details: invalid identification reason")
    if not {"bank-inventory-not-reported", "crossing-boundary-and-ratio-not-bound"} <= set(reasons):
        raise ValidationError("calibration details: missing unidentifiability reasons")
    return {"schema": DETAILS_SCHEMA, "bank_rows": banks, "timing_paths": paths, "identification": dict(identification)}
