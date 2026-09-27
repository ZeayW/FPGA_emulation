#!/usr/bin/env python3
"""Deterministic legalization for typed hard-block groups and cascades.

The device-specific producer supplies exact legal site windows.  This operator
runs inside OpenPARF after global placement and chooses conflict-free windows
using the global-placement displacement as its cost.  It intentionally does
not infer adjacency from coordinates and it never repairs a placement after
OpenPARF has returned.
"""

import json
import math
from pathlib import Path
import sqlite3

import torch


CONSTRAINT_SCHEMA = "openparf.physical-macro-groups/v2"
SITE_DATABASE_SCHEMA = "emuflow.openparf-atomic-site-database/v1"
SUPPORTED_RESOURCES = {"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}
SUPPORTED_SITE_RESOURCES = {"LUT", "CARRY8", "MUXF7", "MUXF8", "MUXF9"}


def _string(value, context):
    if not isinstance(value, str) or not value:
        raise ValueError("{} must be a non-empty string".format(context))
    return value


def _site(value, resource, context):
    if not isinstance(value, dict):
        raise ValueError("{} must be an object".format(context))
    entry_resource = value.get("resource")
    if resource == "SLICE_MACRO":
        if entry_resource not in SUPPORTED_SITE_RESOURCES:
            raise ValueError("{} has an unsupported slice resource".format(context))
    elif entry_resource != resource:
        raise ValueError("{} has the wrong resource".format(context))
    name = _string(value.get("site"), context + ".site")
    raw_claims = value.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("{}.claims must be non-empty".format(context))
    claims = [_string(claim, context + ".claims") for claim in raw_claims]
    if len(claims) != len(set(claims)):
        raise ValueError("{}.claims must be unique".format(context))
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
        "resource": entry_resource,
        "x": coordinates[0],
        "y": coordinates[1],
        "z": coordinates[2],
        "claims": claims,
    }


class TypedHardblockLegalizer(object):
    """Place complete chains and typed singletons on certified site windows."""

    def __init__(self, constraint_file, placedb, data_cls):
        constraint_path = Path(constraint_file).resolve()
        with constraint_path.open("r") as stream:
            value = json.load(stream)
        if value.get("schema") != CONSTRAINT_SCHEMA or value.get("status") != "pass":
            raise ValueError("typed hardblock chain constraint header is invalid")

        self.site_database = None
        database_descriptor = value.get("site_database")
        if database_descriptor is not None:
            if not isinstance(database_descriptor, dict):
                raise ValueError("site database descriptor must be an object")
            relative = database_descriptor.get("file")
            site_count = database_descriptor.get("sites")
            if (
                database_descriptor.get("schema") != SITE_DATABASE_SCHEMA
                or not isinstance(relative, str)
                or not relative
                or Path(relative).name != relative
                or isinstance(site_count, bool)
                or not isinstance(site_count, int)
                or site_count <= 0
            ):
                raise ValueError("site database descriptor is invalid")
            database_path = constraint_path.parent / relative
            if not database_path.is_file():
                raise ValueError("site database is missing")
            uri = "file:{}?mode=ro&immutable=1".format(database_path)
            with sqlite3.connect(uri, uri=True) as database:
                metadata = dict(database.execute("SELECT key, value FROM metadata"))
            if (
                metadata.get("schema") != SITE_DATABASE_SCHEMA
                or metadata.get("site_count") != str(site_count)
            ):
                raise ValueError("site database metadata is invalid")
            self.site_database = database_path

        self.data_cls = data_cls
        self.groups = []
        instance_names = set()
        for index, raw in enumerate(value.get("groups", [])):
            context = "groups[{}]".format(index)
            if not isinstance(raw, dict):
                raise ValueError("{} must be an object".format(context))
            resource = _string(raw.get("resource"), context + ".resource")
            kind = _string(raw.get("kind"), context + ".kind")
            if resource not in SUPPORTED_RESOURCES | {"SLICE_MACRO"}:
                raise ValueError("{} uses unsupported resource {}".format(context, resource))
            if resource == "SLICE_MACRO" and kind != "site_macro":
                raise ValueError("{} has an invalid slice-macro kind".format(context))
            if resource != "SLICE_MACRO" and kind not in {"singleton", "cascade"}:
                raise ValueError("{} has an invalid hardblock kind".format(context))
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
            window_template = raw.get("window_template")
            if window_template is not None:
                if resource != "SLICE_MACRO" or kind != "site_macro":
                    raise ValueError(
                        "{}.window_template is only valid for a site macro".format(
                            context
                        )
                    )
                if self.site_database is None:
                    raise ValueError(
                        "{}.window_template requires the site database".format(context)
                    )
                members = (
                    window_template.get("members")
                    if isinstance(window_template, dict) else None
                )
                if (
                    not isinstance(window_template, dict)
                    or window_template.get("kind") != "same-site-slice/v1"
                    or window_template.get("site_resource") != "LUT"
                    or not isinstance(members, list)
                    or len(members) != len(names)
                ):
                    raise ValueError("{}.window_template is invalid".format(context))
                normalized_members = []
                for member_index, member in enumerate(members):
                    member_context = "{}.window_template.members[{}]".format(
                        context, member_index
                    )
                    if not isinstance(member, dict):
                        raise ValueError("{} must be an object".format(member_context))
                    member_resource = member.get("resource")
                    coordinate = member.get("z")
                    if (
                        member_resource not in SUPPORTED_SITE_RESOURCES
                        or isinstance(coordinate, bool)
                        or not isinstance(coordinate, (int, float))
                        or not math.isfinite(float(coordinate))
                    ):
                        raise ValueError("{} is invalid".format(member_context))
                    normalized_members.append({
                        "resource": member_resource,
                        "z": float(coordinate),
                    })
                window_count = raw.get("window_count")
                if (
                    isinstance(window_count, bool)
                    or not isinstance(window_count, int)
                    or window_count <= 0
                ):
                    raise ValueError("{}.window_count is invalid".format(context))
                window_template = {
                    "site_resource": "LUT", "members": normalized_members,
                }
            else:
                for window_index, raw_window in enumerate(raw.get("windows", [])):
                    window_context = "{}.windows[{}]".format(context, window_index)
                    if not isinstance(raw_window, list) or len(raw_window) != len(names):
                        raise ValueError("{} has the wrong length".format(window_context))
                    window = [
                        _site(site, resource, "{}[{}]".format(window_context, site_index))
                        for site_index, site in enumerate(raw_window)
                    ]
                    claims = [claim for site in window for claim in site["claims"]]
                    if resource == "SLICE_MACRO":
                        site_names = {site["site"] for site in window}
                        unique_claims = set(claims)
                        if (
                            len(site_names) != 1
                            or unique_claims != {"site:" + next(iter(site_names))}
                        ):
                            raise ValueError(
                                "{} is not one exclusive same-site window".format(
                                    window_context
                                )
                            )
                    elif len(claims) != len(set(claims)):
                        raise ValueError(
                            "{} has internally conflicting occupancy claims".format(
                                window_context
                            )
                        )
                    windows.append(window)
                if not windows:
                    raise ValueError("{} has no legal windows".format(context))
                window_count = len(windows)
            owned_resources = (
                sorted({_string(item, context + ".owned_resources")
                        for item in raw.get("owned_resources", [])})
                if resource == "SLICE_MACRO" else [resource]
            )
            if (
                resource == "SLICE_MACRO"
                and (
                    not owned_resources
                    or not set(owned_resources).issubset(
                        SUPPORTED_SITE_RESOURCES - {"LUT"}
                    )
                )
            ):
                raise ValueError(
                    "{}.owned_resources must name dedicated slice area types".format(
                        context
                    )
                )
            self.groups.append({
                "id": _string(raw.get("id"), context + ".id"),
                "kind": kind,
                "resource": resource,
                "owned_resources": owned_resources,
                "names": names,
                "ids": [int(placedb.nameToInst(name)) for name in names],
                "windows": windows,
                "window_template": window_template,
                "window_count": window_count,
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
        self.site_macro_ids = torch.tensor(sorted(
            inst_id for group in self.groups if group["kind"] == "site_macro"
            for inst_id in group["ids"]
        ), dtype=torch.int64)
        self.hardblock_ids = torch.tensor(sorted(
            inst_id for group in self.groups if group["kind"] != "site_macro"
            for inst_id in group["ids"]
        ), dtype=torch.int64)
        self.last_assignment = None

    def _iter_windows(self, group):
        template = group["window_template"]
        if template is None:
            for window in group["windows"]:
                yield window
            return
        uri = "file:{}?mode=ro&immutable=1".format(self.site_database)
        query = (
            "SELECT p.physical_site, s.placement_x, s.placement_y "
            "FROM physical_sites p JOIN sites s USING(dense_x, dense_y) "
            "WHERE p.resource = ? "
            "ORDER BY s.dense_x, s.dense_y, p.slot, p.physical_site"
        )
        emitted = 0
        with sqlite3.connect(uri, uri=True) as database:
            for site, x, y in database.execute(
                query, (template["site_resource"],)
            ):
                emitted += 1
                yield [
                    {
                        "site": site,
                        "resource": member["resource"],
                        "x": float(x), "y": float(y), "z": member["z"],
                        "claims": ["site:" + site],
                    }
                    for member in template["members"]
                ]
        if emitted != group["window_count"]:
            raise RuntimeError(
                "compact site-window count disagrees with the sealed contract"
            )

    def _cost(self, group, window, pos_xyz):
        result = 0.0
        for inst_id, site in zip(group["ids"], window):
            dx = float(pos_xyz[inst_id, 0]) - site["x"]
            dy = float(pos_xyz[inst_id, 1]) - site["y"]
            result += dx * dx + dy * dy
        return result

    def _legalize(self, pos_xyz, kinds):
        local = pos_xyz.cpu() if pos_xyz.is_cuda else pos_xyz
        occupied = set()
        assignment = []
        # Most constrained and longest chains go first.  The selection is a
        # deterministic conflict-aware heuristic, not an exact optimizer.
        ordered = sorted(
            [group for group in self.groups if group["kind"] in kinds],
            key=lambda group: (
                group["window_count"], -len(group["ids"]), group["id"]
            ),
        )
        with torch.no_grad():
            for group in ordered:
                best = None
                for window in self._iter_windows(group):
                    claims = tuple(sorted(set(
                        claim for site in window for claim in site["claims"]
                    )))
                    if occupied.isdisjoint(claims):
                        site_names = tuple(site["site"] for site in window)
                        candidate = (
                            self._cost(group, window, local), site_names,
                            claims, window,
                        )
                        if best is None or candidate[:3] < best[:3]:
                            best = candidate
                if best is None:
                    raise RuntimeError(
                        "typed hardblock group legalization has no conflict-free window for {}"
                        .format(group["id"])
                    )
                _cost, _site_names, claims, selected = best
                occupied.update(claims)
                for inst_id, name, site in zip(group["ids"], group["names"], selected):
                    local[inst_id, 0] = site["x"]
                    local[inst_id, 1] = site["y"]
                    local[inst_id, 2] = site["z"]
                    assignment.append({
                        "group": group["id"], "instance": name,
                        "resource": site["resource"], "site": site["site"],
                    })
            selected_ids = sorted(
                inst_id for group in ordered for inst_id in group["ids"]
            )
            if selected_ids:
                lock_ids = torch.tensor(
                    selected_ids, dtype=torch.int64,
                    device=self.data_cls.inst_lock_mask.device,
                )
                self.data_cls.inst_lock_mask[lock_ids] = 1
            if local is not pos_xyz:
                pos_xyz.data.copy_(local)
        self.last_assignment = sorted(assignment, key=lambda item: item["instance"])
        return pos_xyz

    def legalize_site_macros(self, pos_xyz):
        return self._legalize(pos_xyz, {"site_macro"})

    def legalize_hardblocks(self, pos_xyz):
        return self._legalize(pos_xyz, {"singleton", "cascade"})

    def __call__(self, pos_xyz):
        return self._legalize(pos_xyz, {"site_macro", "singleton", "cascade"})
