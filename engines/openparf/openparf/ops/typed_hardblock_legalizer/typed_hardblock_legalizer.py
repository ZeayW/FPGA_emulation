#!/usr/bin/env python3
"""Deterministic legalization for typed hardblock groups and cascades.

The device-specific producer supplies exact legal site windows.  This operator
runs inside OpenPARF after global placement and chooses conflict-free windows
using the global-placement displacement as its cost.  It intentionally does
not infer adjacency from coordinates and it never repairs a placement after
OpenPARF has returned.
"""

import json
import math

import torch


CONSTRAINT_SCHEMA = "openparf.typed-hardblock-groups/v2"
SUPPORTED_RESOURCES = {"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}


def _string(value, context):
    if not isinstance(value, str) or not value:
        raise ValueError("{} must be a non-empty string".format(context))
    return value


def _site(value, resource, context):
    if not isinstance(value, dict):
        raise ValueError("{} must be an object".format(context))
    if value.get("resource") != resource:
        raise ValueError("{} has the wrong resource".format(context))
    name = _string(value.get("site"), context + ".site")
    raw_claims = value.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("{}.claims must be non-empty and unique".format(context))
    claims = [
        _string(claim, context + ".claims") for claim in raw_claims
    ]
    if len(claims) != len(set(claims)):
        raise ValueError("{}.claims must be non-empty and unique".format(context))
    coordinates = []
    for axis in ("x", "y", "z"):
        coordinate = value.get(axis)
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise ValueError("{}.{} must be numeric".format(context, axis))
        coordinate = float(coordinate)
        if not math.isfinite(coordinate):
            raise ValueError("{}.{} must be finite".format(context, axis))
        coordinates.append(coordinate)
    return {
        "site": name,
        "resource": resource,
        "x": coordinates[0],
        "y": coordinates[1],
        "z": coordinates[2],
        "claims": claims,
    }


class TypedHardblockLegalizer(object):
    """Place complete chains and typed singletons on certified site windows."""

    def __init__(self, constraint_file, placedb, data_cls):
        with open(constraint_file, "r") as stream:
            value = json.load(stream)
        if value.get("schema") != CONSTRAINT_SCHEMA or value.get("status") != "pass":
            raise ValueError("typed hardblock chain constraint header is invalid")

        self.data_cls = data_cls
        self.groups = []
        instance_names = set()
        for index, raw in enumerate(value.get("groups", [])):
            context = "groups[{}]".format(index)
            if not isinstance(raw, dict):
                raise ValueError("{} must be an object".format(context))
            resource = _string(raw.get("resource"), context + ".resource")
            if resource not in SUPPORTED_RESOURCES:
                raise ValueError("{} uses unsupported resource {}".format(context, resource))
            names = raw.get("instances")
            if not isinstance(names, list) or not names:
                raise ValueError("{}.instances must be non-empty".format(context))
            names = [_string(name, context + ".instances") for name in names]
            if len(names) != len(set(names)):
                raise ValueError("{} repeats an instance".format(context))
            overlap = instance_names.intersection(names)
            if overlap:
                raise ValueError("typed hardblock instance appears twice: {}".format(sorted(overlap)))
            instance_names.update(names)
            windows = []
            for window_index, raw_window in enumerate(raw.get("windows", [])):
                window_context = "{}.windows[{}]".format(context, window_index)
                if not isinstance(raw_window, list) or len(raw_window) != len(names):
                    raise ValueError("{} has the wrong length".format(window_context))
                window = [
                    _site(site, resource, "{}[{}]".format(window_context, site_index))
                    for site_index, site in enumerate(raw_window)
                ]
                claims = [claim for site in window for claim in site["claims"]]
                if len(claims) != len(set(claims)):
                    raise ValueError(
                        "{} has internally conflicting occupancy claims".format(
                            window_context
                        )
                    )
                windows.append(window)
            if not windows:
                raise ValueError("{} has no legal windows".format(context))
            self.groups.append({
                "id": _string(raw.get("id"), context + ".id"),
                "resource": resource,
                "names": names,
                "ids": [int(placedb.nameToInst(name)) for name in names],
                "windows": windows,
            })

        if not self.groups:
            raise ValueError("typed hardblock chain constraints contain no groups")
        all_ids = [inst_id for group in self.groups for inst_id in group["ids"]]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("typed hardblock name-to-instance mapping is not injective")
        movable_begin, movable_end = data_cls.movable_range
        if any(inst_id < movable_begin or inst_id >= movable_end for inst_id in all_ids):
            raise ValueError("typed hardblock constraints reference a non-movable instance")
        self.inst_ids = torch.tensor(sorted(all_ids), dtype=torch.int64)
        self.last_assignment = None

    def _cost(self, group, window, pos_xyz):
        result = 0.0
        for inst_id, site in zip(group["ids"], window):
            dx = float(pos_xyz[inst_id, 0]) - site["x"]
            dy = float(pos_xyz[inst_id, 1]) - site["y"]
            result += dx * dx + dy * dy
        return result

    def __call__(self, pos_xyz):
        local = pos_xyz.cpu() if pos_xyz.is_cuda else pos_xyz
        occupied = set()
        assignment = []
        # Most constrained and longest chains go first.  The selection is a
        # deterministic conflict-aware heuristic, not an exact optimizer.
        ordered = sorted(
            self.groups,
            key=lambda group: (len(group["windows"]), -len(group["ids"]), group["id"]),
        )
        with torch.no_grad():
            for group in ordered:
                candidates = []
                for window in group["windows"]:
                    claims = tuple(
                        sorted(claim for site in window for claim in site["claims"])
                    )
                    if occupied.isdisjoint(claims):
                        site_names = tuple(site["site"] for site in window)
                        candidates.append((
                            self._cost(group, window, local), site_names,
                            claims, window,
                        ))
                if not candidates:
                    raise RuntimeError(
                        "typed hardblock group legalization has no conflict-free window for {}"
                        .format(group["id"])
                    )
                _cost, _site_names, claims, selected = min(
                    candidates, key=lambda item: (item[0], item[1], item[2])
                )
                occupied.update(claims)
                for inst_id, name, site in zip(group["ids"], group["names"], selected):
                    local[inst_id, 0] = site["x"]
                    local[inst_id, 1] = site["y"]
                    local[inst_id, 2] = site["z"]
                    assignment.append({
                        "group": group["id"], "instance": name,
                        "resource": group["resource"], "site": site["site"],
                    })
            lock_ids = self.inst_ids.to(self.data_cls.inst_lock_mask.device)
            self.data_cls.inst_lock_mask[lock_ids] = 1
            if local is not pos_xyz:
                pos_xyz.data.copy_(local)
        self.last_assignment = sorted(assignment, key=lambda item: item["instance"])
        return pos_xyz
