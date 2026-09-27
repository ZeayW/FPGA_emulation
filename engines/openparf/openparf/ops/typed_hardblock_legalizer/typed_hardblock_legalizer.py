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
from bisect import bisect_left
from pathlib import Path
import sqlite3

import torch


CONSTRAINT_SCHEMA = "openparf.physical-macro-groups/v3"
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
        shared_window_sets = {}
        for set_index, raw_set in enumerate(value.get("window_sets", [])):
            context = "window_sets[{}]".format(set_index)
            if not isinstance(raw_set, dict):
                raise ValueError("{} must be an object".format(context))
            set_id = _string(raw_set.get("id"), context + ".id")
            resource = _string(raw_set.get("resource"), context + ".resource")
            if resource not in SUPPORTED_RESOURCES:
                raise ValueError("{} uses unsupported resource {}".format(context, resource))
            if set_id in shared_window_sets:
                raise ValueError("shared hardblock window-set id appears twice")
            windows = []
            for window_index, raw_window in enumerate(raw_set.get("windows", [])):
                window_context = "{}.windows[{}]".format(context, window_index)
                if not isinstance(raw_window, list) or len(raw_window) != 1:
                    raise ValueError("{} must contain one singleton site".format(window_context))
                windows.append([
                    _site(raw_window[0], resource, window_context + "[0]")
                ])
            if not windows:
                raise ValueError("{} has no legal windows".format(context))
            shared_window_sets[set_id] = {
                "resource": resource, "windows": windows,
            }
        self._site_chain_sets = {}
        for set_index, raw_set in enumerate(value.get("site_chain_sets", [])):
            context = "site_chain_sets[{}]".format(set_index)
            if not isinstance(raw_set, dict):
                raise ValueError("{} must be an object".format(context))
            set_id = _string(raw_set.get("id"), context + ".id")
            if set_id in self._site_chain_sets:
                raise ValueError("directed site-chain set id appears twice")
            if raw_set.get("site_resource") != "LUT":
                raise ValueError("{} must use LUT sites".format(context))
            chains = raw_set.get("chains")
            if not isinstance(chains, list) or not chains:
                raise ValueError("{}.chains must be non-empty".format(context))
            normalized_chains = []
            owned_sites = set()
            for chain_index, chain in enumerate(chains):
                chain_context = "{}.chains[{}]".format(context, chain_index)
                if (
                    not isinstance(chain, list)
                    or len(chain) < 2
                    or not all(isinstance(site, str) and site for site in chain)
                    or len(chain) != len(set(chain))
                ):
                    raise ValueError("{} is invalid".format(chain_context))
                overlap = owned_sites.intersection(chain)
                if overlap:
                    raise ValueError(
                        "directed site-chain sets overlap at {}".format(
                            sorted(overlap)
                        )
                    )
                owned_sites.update(chain)
                normalized_chains.append(tuple(chain))
            self._site_chain_sets[set_id] = {
                "site_resource": "LUT", "chains": normalized_chains,
            }
        referenced_window_sets = set()
        referenced_site_chain_sets = set()
        instance_names = set()
        for index, raw in enumerate(value.get("groups", [])):
            context = "groups[{}]".format(index)
            if not isinstance(raw, dict):
                raise ValueError("{} must be an object".format(context))
            resource = _string(raw.get("resource"), context + ".resource")
            kind = _string(raw.get("kind"), context + ".kind")
            if resource not in SUPPORTED_RESOURCES | {"SLICE_MACRO"}:
                raise ValueError("{} uses unsupported resource {}".format(context, resource))
            if resource == "SLICE_MACRO" and kind not in {
                "site_macro", "site_cascade",
            }:
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
            window_set_id = raw.get("window_set")
            chain_template = raw.get("chain_template")
            if sum(
                item is not None
                for item in (window_template, window_set_id, chain_template)
            ) > 1:
                raise ValueError(
                    "{} has conflicting compact window contracts".format(context)
                )
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
            elif window_set_id is not None:
                window_set_id = _string(window_set_id, context + ".window_set")
                window_set = shared_window_sets.get(window_set_id)
                if (
                    kind != "singleton"
                    or len(names) != 1
                    or window_set is None
                    or window_set["resource"] != resource
                    or "windows" in raw
                    or raw.get("window_count") != len(window_set["windows"])
                ):
                    raise ValueError("{}.window_set is invalid".format(context))
                windows = window_set["windows"]
                window_count = len(windows)
                referenced_window_sets.add(window_set_id)
            elif chain_template is not None:
                if resource != "SLICE_MACRO" or kind != "site_cascade":
                    raise ValueError(
                        "{}.chain_template is only valid for a site cascade".format(
                            context
                        )
                    )
                if self.site_database is None:
                    raise ValueError(
                        "{}.chain_template requires the site database".format(context)
                    )
                members = (
                    chain_template.get("unit_members")
                    if isinstance(chain_template, dict) else None
                )
                chain_set_id = (
                    chain_template.get("chain_set")
                    if isinstance(chain_template, dict) else None
                )
                chain_length = (
                    chain_template.get("chain_length")
                    if isinstance(chain_template, dict) else None
                )
                if (
                    not isinstance(chain_template, dict)
                    or chain_template.get("kind") != "directed-site-chain/v1"
                    or not isinstance(chain_set_id, str)
                    or chain_set_id not in self._site_chain_sets
                    or isinstance(chain_length, bool)
                    or not isinstance(chain_length, int)
                    or chain_length < 2
                    or not isinstance(members, list)
                    or not members
                    or len(names) != chain_length * len(members)
                ):
                    raise ValueError("{}.chain_template is invalid".format(context))
                normalized_members = []
                for member_index, member in enumerate(members):
                    member_context = "{}.chain_template.unit_members[{}]".format(
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
                        "resource": member_resource, "z": float(coordinate),
                    })
                expected_count = sum(
                    max(0, len(chain) - chain_length + 1)
                    for chain in self._site_chain_sets[chain_set_id]["chains"]
                )
                if raw.get("window_count") != expected_count or expected_count <= 0:
                    raise ValueError("{}.window_count is invalid".format(context))
                chain_template = {
                    "chain_set": chain_set_id,
                    "chain_length": chain_length,
                    "unit_members": normalized_members,
                }
                window_count = expected_count
                referenced_site_chain_sets.add(chain_set_id)
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
                "chain_template": chain_template,
                "window_count": window_count,
            })

        if referenced_window_sets != set(shared_window_sets):
            raise ValueError("typed hardblock constraints contain an unused window set")
        if referenced_site_chain_sets != set(self._site_chain_sets):
            raise ValueError(
                "typed hardblock constraints contain an unused site-chain set"
            )

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
            inst_id for group in self.groups
            if group["kind"] in {"site_macro", "site_cascade"}
            for inst_id in group["ids"]
        ), dtype=torch.int64)
        self.hardblock_ids = torch.tensor(sorted(
            inst_id for group in self.groups
            if group["kind"] not in {"site_macro", "site_cascade"}
            for inst_id in group["ids"]
        ), dtype=torch.int64)
        # Compact same-site templates all refer to the same immutable device
        # database.  Loading that database once is important: a realistic
        # device has hundreds of thousands of LUT sites and thousands of
        # CARRY/MUX macros.  Reopening and scanning it once per macro turns a
        # small legalization problem into an O(macros * device-sites) pass.
        self._compact_site_indexes = {}
        self._compact_chain_indexes = {}
        self.last_assignment = None

    def _compact_site_index(self, site_resource, expected_count):
        """Return a cached exact spatial index for compact template sites.

        The index groups candidates into x columns and keeps every column
        ordered by y.  Nearest-site lookup can then prune columns and rows by
        an exact squared-distance lower bound without an optional spatial
        library or an approximate search.
        """
        cached = self._compact_site_indexes.get(site_resource)
        if cached is None:
            uri = "file:{}?mode=ro&immutable=1".format(self.site_database)
            query = (
                # All in-core placement operators use the geometric center of
                # the complete Bookshelf site bounding box.  The dense lower-
                # left coordinate is only the serialized site-map identity.
                "SELECT p.physical_site, s.placement_x, s.placement_y "
                "FROM physical_sites p JOIN sites s USING(dense_x, dense_y) "
                "WHERE p.resource = ? "
                "ORDER BY s.dense_x, s.dense_y, p.slot, p.physical_site"
            )
            columns = {}
            with sqlite3.connect(uri, uri=True) as database:
                for site, x, y in database.execute(query, (site_resource,)):
                    x = float(x)
                    columns.setdefault(x, []).append((float(y), str(site)))
            xs = sorted(columns)
            ordered_columns = []
            count = 0
            for x in xs:
                rows = sorted(columns[x])
                count += len(rows)
                ordered_columns.append((rows, [row[0] for row in rows]))
            cached = {
                "count": count,
                "xs": xs,
                "columns": ordered_columns,
            }
            self._compact_site_indexes[site_resource] = cached
        if cached["count"] != expected_count:
            raise RuntimeError(
                "compact site-window count disagrees with the sealed contract"
            )
        return cached

    @staticmethod
    def _template_window(template, site, x, y):
        return [
            {
                "site": site,
                "resource": member["resource"],
                "x": x, "y": y, "z": member["z"],
                "claims": ["site:" + site],
            }
            for member in template["members"]
        ]

    def _nearest_template_window(self, group, pos_xyz, occupied):
        """Choose exactly the same minimum-displacement site without a scan.

        For a same-site macro, the sum of squared member displacement equals
        ``N * distance(site, member-centroid)^2 + constant``.  Therefore the
        original exhaustive objective is minimized by the nearest unoccupied
        site to the centroid.  The two-level x/y search below is exact: it
        stops only once the next coordinate's lower bound is strictly worse
        than the best candidate already evaluated.  Site-name tie breaking is
        retained from the exhaustive implementation.
        """
        template = group["window_template"]
        index = self._compact_site_index(
            template["site_resource"], group["window_count"]
        )
        count = len(group["ids"])
        mean_x = sum(float(pos_xyz[inst_id, 0]) for inst_id in group["ids"]) / count
        mean_y = sum(float(pos_xyz[inst_id, 1]) for inst_id in group["ids"]) / count
        xs = index["xs"]
        columns = index["columns"]
        right = bisect_left(xs, mean_x)
        left = right - 1
        best = None
        best_distance = None

        while left >= 0 or right < len(xs):
            left_distance = (
                (xs[left] - mean_x) ** 2 if left >= 0 else float("inf")
            )
            right_distance = (
                (xs[right] - mean_x) ** 2 if right < len(xs) else float("inf")
            )
            if left_distance <= right_distance:
                column_index = left
                x_distance = left_distance
                left -= 1
            else:
                column_index = right
                x_distance = right_distance
                right += 1
            if best_distance is not None and x_distance > best_distance:
                break

            x = xs[column_index]
            rows, ys = columns[column_index]
            upper = bisect_left(ys, mean_y)
            lower = upper - 1
            while lower >= 0 or upper < len(rows):
                lower_distance = (
                    (rows[lower][0] - mean_y) ** 2
                    if lower >= 0 else float("inf")
                )
                upper_distance = (
                    (rows[upper][0] - mean_y) ** 2
                    if upper < len(rows) else float("inf")
                )
                if lower_distance <= upper_distance:
                    row_index = lower
                    y_distance = lower_distance
                    lower -= 1
                else:
                    row_index = upper
                    y_distance = upper_distance
                    upper += 1
                distance = x_distance + y_distance
                if best_distance is not None and distance > best_distance:
                    break
                y, site = rows[row_index]
                claims = ("site:" + site,)
                if occupied.isdisjoint(claims):
                    window = self._template_window(template, site, x, y)
                    site_names = tuple(item["site"] for item in window)
                    candidate = (
                        self._cost(group, window, pos_xyz), site_names,
                        claims, window,
                    )
                    if best is None or candidate[:3] < best[:3]:
                        best = candidate
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
        return best

    def _compact_chain_index(self, template, expected_count):
        """Index certified directed chain windows without expanding JSON.

        The source contract stores every native site chain once.  A length-
        specific index is built lazily and cached because logical carry chains
        usually use only a few distinct lengths.  Candidate rows retain the
        exact source site order; no adjacency is inferred from coordinates.
        """
        chain_set_id = template["chain_set"]
        chain_length = template["chain_length"]
        cache_key = (chain_set_id, chain_length)
        cached = self._compact_chain_indexes.get(cache_key)
        if cached is None:
            chains = self._site_chain_sets[chain_set_id]["chains"]
            wanted = {site for chain in chains for site in chain}
            uri = "file:{}?mode=ro&immutable=1".format(self.site_database)
            query = (
                "SELECT p.physical_site, s.placement_x, s.placement_y "
                "FROM physical_sites p JOIN sites s USING(dense_x, dense_y) "
                "WHERE p.resource = 'LUT'"
            )
            coordinates = {}
            with sqlite3.connect(uri, uri=True) as database:
                for site, x, y in database.execute(query):
                    if site in wanted:
                        coordinates[str(site)] = (float(x), float(y))
            if set(coordinates) != wanted:
                raise RuntimeError(
                    "directed site-chain contract references an unknown LUT site"
                )

            columns = {}
            count = 0
            for chain in chains:
                for start in range(len(chain) - chain_length + 1):
                    sites = chain[start:start + chain_length]
                    site_coordinates = [coordinates[site] for site in sites]
                    x, y = site_coordinates[0]
                    if any(
                        candidate_x != x or candidate_y != y + offset
                        for offset, (candidate_x, candidate_y)
                        in enumerate(site_coordinates)
                    ):
                        raise RuntimeError(
                            "directed site-chain coordinates are not a unit-stride "
                            "placement column"
                        )
                    columns.setdefault(x, []).append((y, sites))
                    count += 1
            xs = sorted(columns)
            ordered_columns = []
            for x in xs:
                rows = sorted(columns[x], key=lambda item: (item[0], item[1]))
                ordered_columns.append((rows, [row[0] for row in rows]))
            cached = {"count": count, "xs": xs, "columns": ordered_columns}
            self._compact_chain_indexes[cache_key] = cached
        if cached["count"] != expected_count:
            raise RuntimeError(
                "compact directed-chain window count disagrees with the sealed contract"
            )
        return cached

    @staticmethod
    def _chain_template_window(template, sites, x, y):
        return [
            {
                "site": site,
                "resource": member["resource"],
                "x": x, "y": y + unit_index, "z": member["z"],
                "claims": ["site:" + site],
            }
            for unit_index, site in enumerate(sites)
            for member in template["unit_members"]
        ]

    def _nearest_chain_window(self, group, pos_xyz, occupied):
        """Select the exact nearest conflict-free certified chain window."""
        template = group["chain_template"]
        index = self._compact_chain_index(template, group["window_count"])
        unit_size = len(template["unit_members"])
        count = len(group["ids"])
        mean_x = sum(float(pos_xyz[inst_id, 0]) for inst_id in group["ids"]) / count
        mean_y = sum(
            float(pos_xyz[inst_id, 1]) - (index_in_group // unit_size)
            for index_in_group, inst_id in enumerate(group["ids"])
        ) / count
        xs = index["xs"]
        columns = index["columns"]
        right = bisect_left(xs, mean_x)
        left = right - 1
        best = None
        best_distance = None

        while left >= 0 or right < len(xs):
            left_distance = (
                (xs[left] - mean_x) ** 2 if left >= 0 else float("inf")
            )
            right_distance = (
                (xs[right] - mean_x) ** 2 if right < len(xs) else float("inf")
            )
            if left_distance <= right_distance:
                column_index = left
                x_distance = left_distance
                left -= 1
            else:
                column_index = right
                x_distance = right_distance
                right += 1
            if best_distance is not None and x_distance > best_distance:
                break

            x = xs[column_index]
            rows, ys = columns[column_index]
            upper = bisect_left(ys, mean_y)
            lower = upper - 1
            while lower >= 0 or upper < len(rows):
                lower_distance = (
                    (rows[lower][0] - mean_y) ** 2
                    if lower >= 0 else float("inf")
                )
                upper_distance = (
                    (rows[upper][0] - mean_y) ** 2
                    if upper < len(rows) else float("inf")
                )
                if lower_distance <= upper_distance:
                    row_index = lower
                    y_distance = lower_distance
                    lower -= 1
                else:
                    row_index = upper
                    y_distance = upper_distance
                    upper += 1
                distance = x_distance + y_distance
                if best_distance is not None and distance > best_distance:
                    break
                y, sites = rows[row_index]
                claims = tuple("site:" + site for site in sites)
                if occupied.isdisjoint(claims):
                    window = self._chain_template_window(
                        template, sites, x, y
                    )
                    site_names = tuple(item["site"] for item in window)
                    candidate = (
                        self._cost(group, window, pos_xyz), site_names,
                        claims, window,
                    )
                    if best is None or candidate[:3] < best[:3]:
                        best = candidate
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
        return best

    def _iter_windows(self, group):
        chain_template = group["chain_template"]
        if chain_template is not None:
            index = self._compact_chain_index(
                chain_template, group["window_count"]
            )
            for x, (rows, _ys) in zip(index["xs"], index["columns"]):
                for y, sites in rows:
                    yield self._chain_template_window(
                        chain_template, sites, x, y
                    )
            return
        template = group["window_template"]
        if template is None:
            for window in group["windows"]:
                yield window
            return
        index = self._compact_site_index(
            template["site_resource"], group["window_count"]
        )
        for x, (rows, _ys) in zip(index["xs"], index["columns"]):
            for y, site in rows:
                yield self._template_window(template, site, x, y)

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
                if group["chain_template"] is not None:
                    best = self._nearest_chain_window(group, local, occupied)
                elif group["window_template"] is not None:
                    best = self._nearest_template_window(group, local, occupied)
                else:
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
        return self._legalize(pos_xyz, {"site_macro", "site_cascade"})

    def legalize_hardblocks(self, pos_xyz):
        return self._legalize(pos_xyz, {"singleton", "cascade"})

    def __call__(self, pos_xyz):
        return self._legalize(
            pos_xyz, {"site_macro", "site_cascade", "singleton", "cascade"}
        )
