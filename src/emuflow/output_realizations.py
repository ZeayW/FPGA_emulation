"""Physical output copies retain one source TimingPathDB population."""

from __future__ import annotations

from typing import Any, Mapping
import math

from .errors import ValidationError


def output_realization_index(routes: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    timing = routes.get("timing")
    if timing is None:
        return {}
    if not isinstance(timing, Mapping):
        raise ValidationError("route timing metadata is invalid")
    registry = timing.get("output_realizations")
    if registry is None:
        return {}
    return validate_output_realizations(registry)


def validate_output_realizations(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if (not isinstance(registry, Mapping)
            or registry.get("schema") != "emuflow.output-realizations/v1"
            or not isinstance(registry.get("realizations"), list)):
        raise ValidationError("output realization registry is invalid")
    result = {}
    owners = set()
    original_constraints = {}
    population = registry.get("original_population")
    if (not isinstance(population, Mapping)
            or not isinstance(population.get("paths"), int)
            or isinstance(population["paths"], bool) or population["paths"] < 0
            or not isinstance(population.get("path_ids_sha256"), str)
            or len(population["path_ids_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in population["path_ids_sha256"])):
        raise ValidationError("output realization original population is invalid")
    for item in registry["realizations"]:
        if (not isinstance(item, Mapping)
                or any(not isinstance(item.get(key), str) or not item[key]
                       for key in ("id", "original_path", "fpga", "terminal_net"))
                or not isinstance(item.get("local"), bool)
                or not isinstance(item.get("cut_nets"), list)
                or not isinstance(item.get("cut_transitions"), list)
                or len(item["cut_nets"]) != len(item["cut_transitions"])
                or item["local"] != (not item["cut_nets"])):
            raise ValidationError("output realization record is invalid")
        endpoint = item.get("endpoint")
        if (not isinstance(endpoint, Mapping)
                or not isinstance(endpoint.get("port"), str)
                or not endpoint["port"]
                or not isinstance(endpoint.get("bit"), int)
                or isinstance(endpoint["bit"], bool) or endpoint["bit"] < 0):
            raise ValidationError("output realization endpoint is invalid")
        owner = (item["original_path"], item["fpga"], endpoint["port"], endpoint["bit"])
        if item["id"] in result or owner in owners:
            raise ValidationError("duplicate output realization identity")
        owners.add(owner)
        for field in ("clock_period_ns", "required_time_ns", "fixed_delay_ns"):
            value = item.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or (field == "clock_period_ns" and value <= 0)
                    or (field == "fixed_delay_ns" and value < 0)):
                raise ValidationError("output realization timing is invalid")
        if not isinstance(item.get("clock_domain"), str) or not item["clock_domain"]:
            raise ValidationError("output realization clock is invalid")
        constraint = (item["clock_domain"], item["clock_period_ns"], item["required_time_ns"],
                      item["fixed_delay_ns"], item["terminal_net"], endpoint["port"], endpoint["bit"])
        if original_constraints.setdefault(item["original_path"], constraint) != constraint:
            raise ValidationError("output realizations disagree on source timing path")
        if not item["local"]:
            for position, transition in enumerate(item["cut_transitions"]):
                if (not isinstance(transition, Mapping)
                        or transition.get("net") != item["cut_nets"][position]
                        or any(not isinstance(transition.get(key), str) or not transition[key]
                               for key in ("from", "to"))
                        or (position and item["cut_transitions"][position-1]["to"] != transition["from"])):
                    raise ValidationError("output realization transition chain is invalid")
            if item["cut_transitions"][-1]["to"] != item["fpga"]:
                raise ValidationError("output realization capture owner is invalid")
        result[item["id"]] = item
    return result


def original_path_id(member: str, index: Mapping[str, Mapping[str, Any]]) -> str:
    return index[member]["original_path"] if member in index else member


def aggregate_output_realizations(
    records: list[dict[str, Any]],
    routes: Mapping[str, Any],
    *,
    delay_field: str,
    physical: bool = False,
) -> list[dict[str, Any]]:
    """Select the measured worst copy, counting each original path once.

    The full records remain available to callers for physical completeness and
    exactness gates. A projection-time longest-copy decision is never reused.
    """
    index = output_realization_index(routes)
    if not index:
        return records
    seen = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    originals = {item["original_path"] for item in index.values()}
    for record in records:
        member = record["path"]
        if member in seen:
            raise ValidationError("duplicate measured output realization")
        seen.add(member)
        if member in originals:
            raise ValidationError("original output path bypasses copy realization")
        item = index.get(member)
        if physical and item is not None:
            sequence = record["logical_fpga_sequence"]
            if (not sequence or sequence[-1] != item["fpga"]
                    or (record["path_scope"] == "same-fpga-local") != item["local"]):
                raise ValidationError("physical output realization owner disagrees")
        groups.setdefault(original_path_id(member, index), []).append(record)
    missing = set(index) - seen
    if missing:
        raise ValidationError(f"unmeasured output realizations: {sorted(missing)[:10]}")
    result = []
    for original, copies in sorted(groups.items()):
        if original in originals:
            # Copies represent one logical timing constraint. Different periods
            # would make max-delay and min-slack inconsistent.
            declared = physical and any("declared_target_required_time_ns" in copy for copy in copies)
            fields = (("clock_domain", "target_period_ns", "declared_target_required_time_ns",
                       "declared_runtime_required_time_ns", "virtual_period_ns") if declared else
                      (("clock_domain", "target_period_ns", "target_required_time_ns", "virtual_period_ns")
                       if physical else ("clock_domain", "clock_period_ns", "required_time_ns")))
            if declared:
                for copy in copies:
                    item = index[copy["path"]]
                    if (any(key not in copy for key in fields)
                            or copy["clock_domain"] != item["clock_domain"]
                            or copy["target_period_ns"] != item["clock_period_ns"]
                            or copy["declared_target_required_time_ns"] != item["required_time_ns"]
                            or copy["declared_runtime_required_time_ns"] != copy["virtual_period_ns"] - (
                                item["clock_period_ns"] - item["required_time_ns"])):
                        raise ValidationError("output copy declared source timing constraint disagrees")
            if any(any(copy.get(key) != copies[0].get(key) for key in fields)
                   for copy in copies[1:]):
                raise ValidationError("output copies have different timing constraints")
        worst = max(copies, key=lambda copy: (copy[delay_field], copy["path"]))
        selected = dict(worst)
        selected["path"] = original
        if original in originals:
            selected["worst_realization"] = worst["path"]
            selected["realization_ids"] = sorted(copy["path"] for copy in copies)
            if declared:
                # Engine-rounded deadlines can differ by ulps. Retain the
                # maximum-arrival structural representative, but each clock
                # role's observed slack must cover every physical copy.
                for role in ("target", "runtime"):
                    field = f"{role}_clock_slack_bound_ns"
                    role_worst = min(copies, key=lambda copy: (copy[field], copy["path"]))
                    selected[field] = role_worst[field]
                    selected[f"{role}_required_time_ns"] = role_worst[f"{role}_required_time_ns"]
                    selected[f"{role}_worst_realization"] = role_worst["path"]
                    selected[f"{role}_measured_arrival_ns"] = role_worst.get(
                        f"{role}_measured_arrival_ns", role_worst[delay_field])
        result.append(selected)
    return result
