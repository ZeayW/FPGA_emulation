"""Validate board-specific bindings layered on top of a public BoardDB."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform


BOARD_SUPPORT_OVERLAY_SCHEMA = "emuflow.board-support-overlay/v1"
VALID_OVERLAY_QUALIFICATIONS = {
    "source_backed_hardware_definition",
    "user_supplied_unverified",
}


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _records(value: Any, context: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise ValidationError(f"{context}: expected an array of objects")
    return value


def _selected_clock_index(signal: str, service_signal: str) -> int:
    match = re.fullmatch(r"(.+)\[(\d+):(\d+)\]", service_signal)
    if match is None:
        if signal != service_signal:
            raise ValidationError("selected reference clock is outside its service")
        return 0
    selected = re.fullmatch(re.escape(match.group(1)) + r"\[(\d+)\]", signal)
    if selected is None:
        raise ValidationError("selected reference clock is outside its service")
    index = int(selected.group(1))
    high = int(match.group(2))
    low = int(match.group(3))
    if not min(high, low) <= index <= max(high, low):
        raise ValidationError("selected clock index is out of range")
    return index


def validate_board_support_overlay(
    overlay: Mapping[str, Any], platform: Platform
) -> Dict[str, Any]:
    if overlay.get("schema") != BOARD_SUPPORT_OVERLAY_SCHEMA:
        raise ValidationError(
            f"board support overlay schema must be {BOARD_SUPPORT_OVERLAY_SCHEMA!r}"
        )
    if overlay.get("platform") != platform.name:
        raise ValidationError("board support overlay does not match BoardDB")
    qualification = overlay.get("qualification")
    if qualification not in VALID_OVERLAY_QUALIFICATIONS:
        raise ValidationError("board support overlay qualification is invalid")
    provenance = overlay.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("board support overlay provenance is missing")
    sources = _records(provenance.get("sources"), "provenance.sources")
    if not sources:
        raise ValidationError("board support overlay must cite at least one source")
    for index, source in enumerate(sources):
        _string(source.get("title"), f"provenance.sources[{index}].title")
        _string(source.get("uri"), f"provenance.sources[{index}].uri")
        _string(source.get("locator"), f"provenance.sources[{index}].locator")

    fpga_ids = {fpga.id for fpga in platform.fpgas}
    clock_services = {clock.id: clock for clock in platform.clocks}
    reset_services = {reset.id: reset for reset in platform.resets}
    used_ids = set()
    used_pins = {
        (endpoint.fpga, pin)
        for link in platform.links
        for endpoint in link.endpoint_bindings
        for lane in endpoint.lanes
        for pin in (
            lane.tx_package_pin_p,
            lane.tx_package_pin_n,
            lane.rx_package_pin_p,
            lane.rx_package_pin_n,
        )
    }
    clock_bindings: Dict[str, Mapping[str, Any]] = {}
    normalized_clocks = []
    for index, raw in enumerate(
        _records(overlay.get("reference_clocks"), "reference_clocks")
    ):
        context = f"reference_clocks[{index}]"
        binding_id = _string(raw.get("id"), f"{context}.id")
        fpga = _string(raw.get("fpga"), f"{context}.fpga")
        service_id = _string(
            raw.get("board_service"), f"{context}.board_service"
        )
        selected_signal = _string(
            raw.get("selected_signal"), f"{context}.selected_signal"
        )
        pins = raw.get("package_pins")
        if not isinstance(pins, dict):
            raise ValidationError(f"{context}.package_pins: expected an object")
        p = _string(pins.get("p"), f"{context}.package_pins.p")
        n = _string(pins.get("n"), f"{context}.package_pins.n")
        frequency = raw.get("frequency_mhz")
        if (
            binding_id in used_ids
            or fpga not in fpga_ids
            or service_id not in clock_services
            or isinstance(frequency, bool)
            or not isinstance(frequency, (int, float))
            or float(frequency) <= 0.0
            or p == n
        ):
            raise ValidationError(f"{context}: invalid reference-clock binding")
        service = clock_services[service_id]
        selected_index = _selected_clock_index(selected_signal, service.signal)
        if selected_index >= service.count:
            raise ValidationError(f"{context}: selected clock index is out of range")
        frequency_basis = _string(
            raw.get("frequency_basis"), f"{context}.frequency_basis"
        )
        if frequency_basis not in {"documented", "configured"}:
            raise ValidationError(f"{context}: invalid frequency basis")
        if (
            frequency_basis == "documented"
            and abs(float(frequency) - service.frequency_mhz) > 1e-9
        ):
            raise ValidationError(
                f"{context}: documented frequency disagrees with BoardDB"
            )
        for pin in (p, n):
            if (fpga, pin) in used_pins:
                raise ValidationError("board support overlay package-pin collision")
            used_pins.add((fpga, pin))
        used_ids.add(binding_id)
        normalized = {
            "id": binding_id,
            "fpga": fpga,
            "board_service": service_id,
            "selected_signal": selected_signal,
            "package_pins": {"p": p, "n": n},
            "frequency_mhz": float(frequency),
            "frequency_basis": frequency_basis,
        }
        normalized_clocks.append(normalized)
        clock_bindings[binding_id] = normalized

    normalized_resets = []
    reset_bindings: Dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(_records(overlay.get("resets"), "resets")):
        context = f"resets[{index}]"
        binding_id = _string(raw.get("id"), f"{context}.id")
        fpga = _string(raw.get("fpga"), f"{context}.fpga")
        service_id = _string(
            raw.get("board_service"), f"{context}.board_service"
        )
        package_pin = _string(
            raw.get("package_pin"), f"{context}.package_pin"
        )
        iostandard = _string(
            raw.get("iostandard"), f"{context}.iostandard"
        )
        if (
            binding_id in used_ids
            or fpga not in fpga_ids
            or service_id not in reset_services
            or (fpga, package_pin) in used_pins
        ):
            raise ValidationError(f"{context}: invalid reset binding")
        used_ids.add(binding_id)
        used_pins.add((fpga, package_pin))
        normalized = {
            "id": binding_id,
            "fpga": fpga,
            "board_service": service_id,
            "package_pin": package_pin,
            "iostandard": iostandard,
            "polarity": reset_services[service_id].polarity,
        }
        normalized_resets.append(normalized)
        reset_bindings[binding_id] = normalized

    links = {link.id: link for link in platform.links}
    used_endpoint_lanes = set()
    used_sites = set()
    normalized_sites = []
    for index, raw in enumerate(
        _records(overlay.get("transceiver_sites"), "transceiver_sites")
    ):
        context = f"transceiver_sites[{index}]"
        fpga = _string(raw.get("fpga"), f"{context}.fpga")
        link_id = _string(raw.get("link"), f"{context}.link")
        connector = _string(raw.get("connector"), f"{context}.connector")
        mgt_group = _string(raw.get("mgt_group"), f"{context}.mgt_group")
        site = _string(raw.get("site"), f"{context}.site")
        refclk = _string(
            raw.get("reference_clock_binding"),
            f"{context}.reference_clock_binding",
        )
        reset_binding = _string(
            raw.get("reset_binding"), f"{context}.reset_binding"
        )
        lane = raw.get("physical_lane")
        link = links.get(link_id)
        endpoint = link.endpoint_binding(fpga) if link is not None else None
        endpoint_key = (fpga, link_id, lane)
        if (
            fpga not in fpga_ids
            or link is None
            or endpoint is None
            or isinstance(lane, bool)
            or not isinstance(lane, int)
            or not 0 <= lane < link.data_lanes_per_direction
            or endpoint.connector != connector
            or endpoint.mgt != mgt_group
            or refclk not in clock_bindings
            or clock_bindings[refclk]["fpga"] != fpga
            or reset_binding not in reset_bindings
            or reset_bindings[reset_binding]["fpga"] != fpga
            or endpoint_key in used_endpoint_lanes
            or (fpga, site) in used_sites
        ):
            raise ValidationError(f"{context}: invalid transceiver-site binding")
        used_endpoint_lanes.add(endpoint_key)
        used_sites.add((fpga, site))
        normalized_sites.append(
            {
                "fpga": fpga,
                "link": link_id,
                "connector": connector,
                "mgt_group": mgt_group,
                "physical_lane": lane,
                "site": site,
                "reference_clock_binding": refclk,
                "reset_binding": reset_binding,
            }
        )

    static_outputs = []
    for raw in _records(overlay.get("static_outputs", []), "static_outputs"):
        binding_id = _string(raw.get("id"), "static_outputs.id")
        fpga = _string(raw.get("fpga"), "static_outputs.fpga")
        pin = _string(raw.get("package_pin"), "static_outputs.package_pin")
        standard = _string(raw.get("iostandard"), "static_outputs.iostandard")
        value = raw.get("value")
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding_id)
                or not re.fullmatch(r"[A-Z]+[0-9]+", pin)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", standard)
                or binding_id in used_ids or fpga not in fpga_ids
                or (fpga, pin) in used_pins or type(value) is not int
                or value not in (0, 1)):
            raise ValidationError("invalid static board output or package-pin collision")
        used_ids.add(binding_id)
        used_pins.add((fpga, pin))
        static_outputs.append({"id": binding_id, "fpga": fpga,
                               "package_pin": pin, "iostandard": standard,
                               "value": value})

    normalized = {
        "schema": BOARD_SUPPORT_OVERLAY_SCHEMA,
        "platform": platform.name,
        "qualification": qualification,
        "provenance": {"sources": [dict(item) for item in sources]},
        "reference_clocks": sorted(normalized_clocks, key=lambda item: item["id"]),
        "resets": sorted(normalized_resets, key=lambda item: item["id"]),
        **({"static_outputs": sorted(static_outputs, key=lambda item: item["id"])}
           if static_outputs else {}),
        "transceiver_sites": sorted(
            normalized_sites,
            key=lambda item: (item["fpga"], item["link"], item["physical_lane"]),
        ),
    }
    return {
        "status": "pass",
        "hardware_qualification": (
            "source_backed"
            if qualification == "source_backed_hardware_definition"
            else "unverified"
        ),
        "reference_clock_bindings": len(normalized_clocks),
        "reset_bindings": len(normalized_resets),
        "transceiver_site_bindings": len(normalized_sites),
        "normalized": normalized,
    }


def validate_board_support_overlay_file(
    platform_path: Path,
    overlay_path: Path,
    normalized_out: Optional[Path] = None,
) -> Dict[str, Any]:
    platform = Platform.load(platform_path)
    result = validate_board_support_overlay(read_json(overlay_path), platform)
    if normalized_out is not None:
        write_json(normalized_out, result["normalized"])
    return {key: value for key, value in result.items() if key != "normalized"}
