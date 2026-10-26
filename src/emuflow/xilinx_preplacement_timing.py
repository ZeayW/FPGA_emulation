"""Compact RapidWright UltraScale+ pre-placement timing contract.

The source timing files stay outside the repository under their upstream
license.  This module extracts only conservative scalar bounds needed by the
partition-independent OpenSTA pass and seals the exact source identities.
Routed Phase 7 timing continues to use exact per-sink RWRoute delays.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, Mapping

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_rwroute import (
    RAPIDWRIGHT_TIMING_DATA_REVISION,
    RAPIDWRIGHT_TIMING_DATA_SHA256,
)


XILINX_PREPLACEMENT_TIMING_SCHEMA = (
    "emuflow.xilinx-preplacement-timing-db/v1"
)
_REQUIRED_DELAYS = {
    "lut1",
    "lut2",
    "lut3",
    "lut4",
    "lut5",
    "lut6",
    "lut6_2",
    "muxf7",
    "muxf8",
    "muxf9",
    "carry8",
    "ff_setup",
    "ff_clock_to_q",
    "bram_setup",
    "bram_clock_to_q",
    "uram_setup",
    "uram_clock_to_q",
    "dsp48e2",
    "sink_interconnect",
}

_FF_CONTROLS = {"FDRE": ("R", "setup"), "FDSE": ("S", "setup"),
                "FDCE": ("CLR", "recovery"), "FDPE": ("PRE", "recovery")}
_FF_DELAY_FIELDS = [f"DELAY_{corner}{bound}_{edge}" for corner in ("FAST_", "SLOW_", "")
                    for bound in ("MAX", "MIN") for edge in ("RISE", "FALL")]


def import_vivado_ff_control_timing(raw_path: Path, output_path: Path) -> Dict[str, Any]:
    """Reduce all 32 device checks to a conservative, source-sealed envelope."""
    from .opensta import _runtime_data_path
    from .store_publication import bind_store_source, require_store_sources_unchanged
    recipe = _runtime_data_path(Path('scripts/vivado/export_ff_control_checks.tcl'))
    parents = tuple(bind_store_source(p, 'sta-source-document-v1') for p in (raw_path, recipe))
    if output_path.resolve() in {parent.path for parent in parents}:
        raise ValidationError('device control output aliases a source input')
    with raw_path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or not raw.endswith(b'\n'):
        raise ValidationError('device control checks are oversized or truncated')
    try:
        lines = [line.split('\t') for line in raw.decode('utf-8').splitlines()]
    except UnicodeError as error:
        raise ValidationError('invalid device control check encoding') from error
    if (len(lines) != 38 or lines[0] != ['schema', 'emuflow.vivado-ff-control-raw/v2']
            or any(len(lines[i]) != 2 for i in (1, 2, 3))
            or lines[1][0] != 'part' or lines[2][0] != 'vivado_version'
            or not lines[1][1] or not lines[2][1]
            or lines[3] != ['unit', 'ns'] or lines[4] != ['transition_frame', 'library-pin'] or lines[5] !=
            ['cell_type', 'clock_inverted', 'reset_inverted', 'pin', 'kind', 'transitions', *_FF_DELAY_FIELDS]):
        raise ValidationError('device control check schema/coverage is invalid')
    checks = {cell: {pin: dict(kind=kind, margin_ps=0.) for pin, kind in [('CE', 'setup'), pair]}
              for cell, pair in _FF_CONTROLS.items()}
    seen = set()
    for row in lines[6:]:
        if len(row) != 18:
            raise ValidationError('device control check row is invalid')
        cell, clock, reset, pin, kind, transitions = row[:6]
        key = (cell, clock, reset, pin)
        if (cell not in checks or clock not in {'0', '1'} or reset not in {'0', '1'}
                or pin not in checks[cell] or kind != checks[cell][pin]['kind'] or key in seen):
            raise ValidationError('device control check identity/mode is invalid')
        edges = transitions.split(',')
        # Vivado timing_arc transitions/delay suffixes use canonical library
        # pins, even when the cell's IS_*_INVERTED properties are set. The
        # producer verifies those properties; do not interpret library-frame
        # delay columns as external, inversion-adjusted pin transitions.
        allowed = {'rr', 'rf'}
        if (not edges or len(set(edges)) != len(edges) or not set(edges) <= allowed
                or (kind == 'setup' and set(edges) != allowed)
                or (kind == 'recovery' and set(edges) != {'rf'})):
            raise ValidationError('device control check transition coverage is invalid')
        try:
            values = []
            for field, text in zip(_FF_DELAY_FIELDS, row[6:]):
                edge = 'r' if field.endswith('_RISE') else 'f'
                if any(transition[1] == edge for transition in edges):
                    values.append(float(text))
                elif text != '':
                    raise ValidationError('device check has a value for an inapplicable transition')
        except ValueError as error:
            raise ValidationError('invalid device control check value') from error
        if not all(math.isfinite(v) for v in values):
            raise ValidationError('non-finite device control check value')
        checks[cell][pin]['margin_ps'] = max(checks[cell][pin]['margin_ps'], 1000. * max(values))
        seen.add(key)
    value = dict(schema='emuflow.xilinx-ff-control-timing/v1', provider='vivado-device-check-envelope-v1',
                 part=lines[1][1], vivado_version=lines[2][1], recipe_sha256=_sha256(recipe),
                 raw_sha256=hashlib.sha256(raw).hexdigest(), recovery_transitions='both', checks=checks)
    require_store_sources_unchanged(parents)
    write_json(output_path, value)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _bel_arcs(path: Path) -> Dict[str, list[tuple[str, str, float]]]:
    result: DefaultDict[str, list[tuple[str, str, float]]] = defaultdict(list)
    active: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if fields[0] == "bel":
            active = []
            for field in fields[1:]:
                if field == "in" or ":" in field:
                    break
                active.extend(name for name in field.split(",") if name)
            continue
        if fields[0] in {"site", "intersite"}:
            active = []
            continue
        if not active or len(fields) < 3:
            continue
        delay = _float(fields[2])
        if delay is None:
            continue
        for bel in active:
            result[bel].append((fields[0], fields[1], delay))
    return dict(result)


def _terms(path: Path) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        fields = raw_line.split("#", 1)[0].split()
        if len(fields) != 2:
            continue
        value = _float(fields[1])
        if value is not None:
            result[fields[0]] = value
    return result


def _maximum(values: Iterable[float], context: str) -> float:
    candidates = [float(value) for value in values]
    if not candidates:
        raise ValidationError(
            f"RapidWright timing data has no usable {context} delay"
        )
    return max(candidates)


def _clock_bounds(
    arcs: Iterable[tuple[str, str, float]], context: str
) -> tuple[float, float]:
    setup = []
    clock_to_q = []
    for source, sink, delay in arcs:
        source_clock = "CLK" in source.upper()
        sink_clock = "CLK" in sink.upper()
        if sink_clock and not source_clock and delay >= 0.0:
            setup.append(delay)
        if source_clock and not sink_clock and delay >= 0.0:
            clock_to_q.append(delay)
    return (
        _maximum(setup, f"{context} setup"),
        _maximum(clock_to_q, f"{context} clock-to-Q"),
    )


def _uram_clock_bounds(
    arcs: Iterable[tuple[str, str, float]],
) -> tuple[float, float]:
    """Interpret the source-first convention used by DelayModel v0.5 URAM.

    Unlike the RAMB section, the URAM table encodes both setup bounds and
    clock-to-output bounds as ``CLK -> pin`` rows.  Output identities are
    explicit DOUT/CAS_OUT pins; the remaining CLK rows are input setup terms.
    """

    setup = []
    clock_to_q = []
    for source, sink, delay in arcs:
        if "CLK" not in source.upper() or delay < 0.0:
            continue
        upper_sink = sink.upper()
        if "DOUT" in upper_sink or "CAS_OUT" in upper_sink:
            clock_to_q.append(delay)
        else:
            setup.append(delay)
    return (
        _maximum(setup, "URAM288 setup"),
        _maximum(clock_to_q, "URAM288 clock-to-Q"),
    )


def build_xilinx_preplacement_timing_db(
    timing_data_dir: Path,
    output_path: Path,
    *, ff_control_timing_path: Path | None = None,
) -> Dict[str, Any]:
    """Extract a compact, source-sealed pre-placement timing database."""

    paths = {
        name: timing_data_dir / name
        for name in RAPIDWRIGHT_TIMING_DATA_SHA256
    }
    observed = {
        name: _sha256(path) if path.is_file() else "missing"
        for name, path in paths.items()
    }
    if observed != RAPIDWRIGHT_TIMING_DATA_SHA256:
        raise ValidationError(
            "RapidWright timing data is missing or does not match the pinned "
            f"revision {RAPIDWRIGHT_TIMING_DATA_REVISION}"
        )

    arcs = _bel_arcs(paths["intrasite_delay_terms.txt"])
    terms = _terms(paths["intersite_delay_terms.txt"])
    lut6 = _maximum(
        (
            delay for bel in (f"{name}6LUT" for name in "ABCDEFGH")
            for source, sink, delay in arcs.get(bel, [])
            if source.startswith("A") and sink == "O6" and delay >= 0.0
        ),
        "LUT6",
    )
    lut5 = _maximum(
        (
            delay for bel in (f"{name}5LUT" for name in "ABCDEFGH")
            for source, sink, delay in arcs.get(bel, [])
            if source.startswith("A") and sink == "O5" and delay >= 0.0
        ),
        "LUT5",
    )
    muxf7 = _maximum(
        (
            delay
            for bel in ("F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH")
            for _source, _sink, delay in arcs.get(bel, []) if delay >= 0.0
        ),
        "MUXF7",
    )
    carry = _maximum(
        (delay for _source, _sink, delay in arcs.get("CARRY8", [])
         if delay >= 0.0),
        "CARRY8",
    )
    ff_arcs = [
        arc for bel in (
            "AFF", "BFF", "CFF", "DFF", "EFF", "FFF", "GFF", "HFF",
            "AFF2", "BFF2", "CFF2", "DFF2", "EFF2", "FFF2", "GFF2", "HFF2",
        ) for arc in arcs.get(bel, [])
    ]
    ff_setup = _maximum(
        (-delay for source, sink, delay in ff_arcs
         if "CLK" in source.upper() and sink.upper() == "D" and delay < 0.0),
        "flip-flop setup",
    )
    ff_clock_to_q = _maximum(
        (delay for source, sink, delay in ff_arcs
         if "CLK" in source.upper() and sink.upper() == "Q" and delay >= 0.0),
        "flip-flop clock-to-Q",
    )
    bram_setup, bram_clock_to_q = _clock_bounds(
        arcs.get("RAMB36E2", []), "RAMB36E2"
    )
    uram_setup, uram_clock_to_q = _uram_clock_bounds(
        arcs.get("URAM288", [])
    )
    site_pin = _maximum(
        (value for name, value in terms.items() if name.startswith("SITEPIN_")),
        "site-pin",
    )
    route_class = _maximum(
        (value for name, value in terms.items()
         if name.endswith(("_SINGLE", "_DOUBLE", "_QUAD", "_LONG"))),
        "intersite route-class",
    )

    # DelayModel v0.5 contains no DSP48E2 logic arcs.  Preserve the existing
    # explicit research upper bound rather than silently assigning zero.  The
    # qualification records this separately from extracted RapidWright terms.
    dsp48e2_ps = 5000.0
    mux_upper_ps = max(muxf7, lut5, lut6)
    delays_ps = {
        "lut1": lut6, "lut2": lut6, "lut3": lut6,
        "lut4": lut6, "lut5": lut5, "lut6": lut6,
        "lut6_2": max(lut5, lut6),
        "muxf7": muxf7, "muxf8": mux_upper_ps, "muxf9": mux_upper_ps,
        "carry8": carry,
        "ff_setup": ff_setup, "ff_clock_to_q": ff_clock_to_q,
        "bram_setup": bram_setup, "bram_clock_to_q": bram_clock_to_q,
        "uram_setup": uram_setup, "uram_clock_to_q": uram_clock_to_q,
        "dsp48e2": dsp48e2_ps,
        "sink_interconnect": site_pin + route_class,
    }
    value = {
        "schema": XILINX_PREPLACEMENT_TIMING_SCHEMA,
        "family": "UltraScale+",
        "mapping_profile": "xilinx-ultrascaleplus-open-v1",
        "source": {
            "provider": "rapidwright-delay-model-v0.5",
            "revision": RAPIDWRIGHT_TIMING_DATA_REVISION,
            "qualification": "analytical_uncharacterized",
            "files_sha256": observed,
        },
        "delays_ps": delays_ps,
        "limitations": {
            "purpose": "pre-placement timing optimization only",
            "muxf8_muxf9": "conservative scalar surrogate from LUT/F7 bounds",
            "dsp48e2": "explicit 5 ns research upper bound; upstream model has no arcs",
            "hold_analysis": "unavailable",
            "authoritative_setup": "Phase 7 routed timing plus global OpenSTA",
        },
    }
    if ff_control_timing_path is not None:
        value["ff_control_timing"] = read_json(ff_control_timing_path)
    validate_xilinx_preplacement_timing_db(value)
    write_json(output_path, value, compact=True)
    return validate_xilinx_preplacement_timing_db(value)


def validate_xilinx_preplacement_timing_db(
    value_or_path: Mapping[str, Any] | Path,
) -> Dict[str, Any]:
    value = (
        read_json(value_or_path)
        if isinstance(value_or_path, Path)
        else dict(value_or_path)
    )
    if value.get("schema") != XILINX_PREPLACEMENT_TIMING_SCHEMA:
        raise ValidationError("Xilinx pre-placement TimingDB schema is invalid")
    if value.get("family") != "UltraScale+" or value.get(
        "mapping_profile"
    ) != "xilinx-ultrascaleplus-open-v1":
        raise ValidationError("Xilinx pre-placement TimingDB target is invalid")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValidationError("Xilinx pre-placement TimingDB source is invalid")
    expected_source = {
        "provider": "rapidwright-delay-model-v0.5",
        "revision": RAPIDWRIGHT_TIMING_DATA_REVISION,
        "qualification": "analytical_uncharacterized",
        "files_sha256": RAPIDWRIGHT_TIMING_DATA_SHA256,
    }
    for name, expected in expected_source.items():
        if source.get(name) != expected:
            raise ValidationError(
                f"Xilinx pre-placement TimingDB source.{name} is invalid"
            )
    delays = value.get("delays_ps")
    if not isinstance(delays, dict) or set(delays) != _REQUIRED_DELAYS:
        raise ValidationError("Xilinx pre-placement TimingDB delays are incomplete")
    for name, delay in delays.items():
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) <= 0.0
        ):
            raise ValidationError(
                f"Xilinx pre-placement TimingDB delay {name!r} is invalid"
            )
    limitations = value.get("limitations")
    supplement = value.get("ff_control_timing")
    if supplement is not None:
        if (not isinstance(supplement, dict)
                or supplement.get("schema") != "emuflow.xilinx-ff-control-timing/v1"
                or supplement.get("provider") != "vivado-device-check-envelope-v1"
                or not isinstance(supplement.get("part"), str) or not supplement["part"]
                or not isinstance(supplement.get("vivado_version"), str) or not supplement["vivado_version"]
                or supplement.get("recovery_transitions") != "both"
                or any(not isinstance(supplement.get(key), str)
                       or len(supplement[key]) != 64
                       or any(c not in '0123456789abcdef' for c in supplement[key])
                       for key in ("recipe_sha256", "raw_sha256"))):
            raise ValidationError("FF control timing device provenance is invalid")
        kinds = {"FDRE": {"CE": "setup", "R": "setup"},
                 "FDSE": {"CE": "setup", "S": "setup"},
                 "FDCE": {"CE": "setup", "CLR": "recovery"},
                 "FDPE": {"CE": "setup", "PRE": "recovery"}}
        checks = supplement.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(kinds):
            raise ValidationError("FF control timing primitive coverage is incomplete")
        for cell, pins in kinds.items():
            if not isinstance(checks[cell], dict) or set(checks[cell]) != set(pins):
                raise ValidationError("FF control timing pin coverage is incomplete")
            for pin, kind in pins.items():
                check = checks[cell][pin]
                if (not isinstance(check, dict) or set(check) != {"kind", "margin_ps"}
                        or check["kind"] != kind or isinstance(check["margin_ps"], bool)
                        or not isinstance(check["margin_ps"], (int, float))
                        or not math.isfinite(check["margin_ps"]) or check["margin_ps"] < 0):
                    raise ValidationError("FF control timing check is invalid")
    if not isinstance(limitations, dict) or limitations.get(
        "authoritative_setup"
    ) != "Phase 7 routed timing plus global OpenSTA":
        raise ValidationError(
            "Xilinx pre-placement TimingDB limitations are invalid"
        )
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-preplacement-timing-validation/v1",
        "family": value["family"],
        "mapping_profile": value["mapping_profile"],
        "delay_terms": len(delays),
        "qualification": source["qualification"],
    }
