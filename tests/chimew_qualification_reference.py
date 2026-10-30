"""Independent test-only reference for bank assignment semantic checks."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional, Sequence

from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_ALGORITHM2_INTERPRETATION, CHIMEW_BANK_CHANNEL_INTEGRATION_STATUS, CHIMEW_BANK_CHANNEL_NUMERIC_POLICY, CHIMEW_BANK_CHANNEL_PAPER_SCOPE, CHIMEW_BANK_CHANNEL_PROVIDER, CHIMEW_BANK_CHANNEL_REPORT_SCHEMA, CHIMEW_BANK_CHANNEL_STATUS, _candidate_cost, _raw_cost, validate_chimew_bank_channel_input
from tests.chimew_grouping_fixture import _domain, _popcount, _tdm_ratio, validate_chimew_crossings
from tests.chimew_refinement_fixture import CHIMEW_REFINED_GROUPING_SCHEMA, CHIMEW_REFINEMENT_PROVIDER, _pairwise_objective, _validate_initial_groups, validate_chimew_positions
from tests.chimew_rudy_fixture import CHIMEW_RUDY_PROVIDER, CHIMEW_RUDY_REPORT_SCHEMA, _oracle as _rudy_oracle, validate_chimew_rudy_input
from emuflow.common.errors import ValidationError


from emuflow.transport.chimew.chimew_qualification import canonical_sha256

def _close(lhs, rhs):
    if isinstance(lhs, bool) or isinstance(rhs, bool): return lhs == rhs
    if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
        return math.isclose(float(lhs), float(rhs), rel_tol=1e-10, abs_tol=1e-9)
    return lhs == rhs

def validate_chimew_bank_channel_report_artifact(
    bank_input: Mapping[str, Any], report: Mapping[str, Any]
) -> Dict[str, Any]:
    problem = validate_chimew_bank_channel_input(bank_input)
    expected_report_keys = {
        "schema",
        "status",
        "integration_status",
        "design",
        "platform",
        "provider",
        "paper_scope",
        "algorithm2_interpretation",
        "numeric_policy",
        "provenance",
        "input_sha256",
        "metrics",
        "direction_priority",
        "assignments",
    }
    if (
        set(report) != expected_report_keys
        or report.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
        or report.get("status") != CHIMEW_BANK_CHANNEL_STATUS
        or report.get("integration_status")
        != CHIMEW_BANK_CHANNEL_INTEGRATION_STATUS
        or report.get("provider") != CHIMEW_BANK_CHANNEL_PROVIDER
        or report.get("paper_scope") != CHIMEW_BANK_CHANNEL_PAPER_SCOPE
        or report.get("algorithm2_interpretation")
        != CHIMEW_BANK_CHANNEL_ALGORITHM2_INTERPRETATION
        or report.get("numeric_policy") != CHIMEW_BANK_CHANNEL_NUMERIC_POLICY
        or report.get("design") != problem["design"]
        or report.get("platform") != problem["platform"]
        or report.get("provenance") != problem["provenance"]
        or report.get("input_sha256") != canonical_sha256(bank_input)
    ):
        raise ValidationError("Chimew bank/channel report provenance does not agree")
    assignments = report.get("assignments")
    if not isinstance(assignments, Sequence):
        raise ValidationError("Chimew bank/channel assignments are missing")
    if len(assignments) != len(problem["groups"]):
        raise ValidationError("Chimew bank/channel assignment coverage is invalid")
    from emuflow.transport.chimew.chimew_bank_channel_report_storage import _normalize_assignment

    lookup = sqlite3.connect("")
    lookup.executescript(
        """
        PRAGMA temp_store=FILE;
        CREATE TABLE banks(
          bank_id TEXT PRIMARY KEY,
          bank_seq INTEGER NOT NULL UNIQUE,
          domain_id INTEGER NOT NULL,
          ax REAL NOT NULL, ay REAL NOT NULL,
          bx REAL NOT NULL, by REAL NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE channels(
          channel_id TEXT PRIMARY KEY,
          bank_id TEXT NOT NULL,
          local_position INTEGER NOT NULL,
          pin_ax REAL NOT NULL, pin_ay REAL NOT NULL,
          pin_bx REAL NOT NULL, pin_by REAL NOT NULL,
          UNIQUE(bank_id,local_position)
        ) WITHOUT ROWID;
        CREATE TABLE report_assignments(
          seq INTEGER PRIMARY KEY,
          group_id TEXT NOT NULL UNIQUE,
          bank_id TEXT NOT NULL,
          channel_id TEXT NOT NULL UNIQUE,
          bank_cost_rank INTEGER NOT NULL,
          channel_cost_rank INTEGER NOT NULL,
          bank_cost REAL NOT NULL,
          channel_cost REAL NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE group_order(
          seq INTEGER PRIMARY KEY,
          group_id TEXT NOT NULL UNIQUE,
          domain_id INTEGER NOT NULL,
          kind INTEGER NOT NULL,
          direction INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
    )
    try:
        bank_rows = []
        channel_rows = []
        for bank_seq, bank in enumerate(problem["banks"]):
            bank_rows.append(
                (
                    bank["id"],
                    bank_seq,
                    bank["domain"],
                    bank["bank_a"]["point"][0],
                    bank["bank_a"]["point"][1],
                    bank["bank_b"]["point"][0],
                    bank["bank_b"]["point"][1],
                )
            )
            for local_position, channel in enumerate(bank["channels"]):
                channel_rows.append(
                    (
                        channel["id"],
                        bank["id"],
                        local_position,
                        channel["pin_a"][0],
                        channel["pin_a"][1],
                        channel["pin_b"][0],
                        channel["pin_b"][1],
                    )
                )
                if len(channel_rows) >= 4096:
                    lookup.executemany(
                        "INSERT INTO channels VALUES (?,?,?,?,?,?,?)",
                        channel_rows,
                    )
                    channel_rows.clear()
            if len(bank_rows) >= 4096:
                lookup.executemany(
                    "INSERT INTO banks VALUES (?,?,?,?,?,?,?)", bank_rows
                )
                bank_rows.clear()
        if bank_rows:
            lookup.executemany(
                "INSERT INTO banks VALUES (?,?,?,?,?,?,?)", bank_rows
            )
        if channel_rows:
            lookup.executemany(
                "INSERT INTO channels VALUES (?,?,?,?,?,?,?)", channel_rows
            )
        assignment_rows = []
        for seq, raw in enumerate(assignments):
            record = _normalize_assignment(raw, seq)
            assignment_rows.append(
                (
                    seq,
                    record["group"],
                    record["bank_pair"],
                    record["channel"],
                    record["bank_cost_rank"],
                    record["channel_cost_rank"],
                    record["bank_cost"],
                    record["channel_cost"],
                )
            )
            if len(assignment_rows) >= 4096:
                lookup.executemany(
                    "INSERT INTO report_assignments VALUES (?,?,?,?,?,?,?,?)",
                    assignment_rows,
                )
                assignment_rows.clear()
        if assignment_rows:
            lookup.executemany(
                "INSERT INTO report_assignments VALUES (?,?,?,?,?,?,?,?)",
                assignment_rows,
            )
        group_rows = []
        for seq, group in enumerate(problem["groups"]):
            group_rows.append(
                (seq, group["id"], group["domain"], group["kind"], group["direction"])
            )
            if len(group_rows) >= 4096:
                lookup.executemany(
                    "INSERT INTO group_order VALUES (?,?,?,?,?)", group_rows
                )
                group_rows.clear()
        if group_rows:
            lookup.executemany(
                "INSERT INTO group_order VALUES (?,?,?,?,?)", group_rows
            )
    except (sqlite3.IntegrityError, ValidationError) as error:
        lookup.close()
        raise ValidationError(
            "Chimew bank/channel assignment coverage is invalid"
        ) from error

    stage1_rank = stage2_rank = 0
    stage1_cost = stage2_cost = 0.0
    joined = lookup.execute(
        """
        SELECT assignment.group_id,assignment.bank_id,assignment.channel_id,
               assignment.bank_cost_rank,assignment.channel_cost_rank,
               assignment.bank_cost,assignment.channel_cost,
               bank.domain_id,bank.ax,bank.ay,bank.bx,bank.by,
               channel.bank_id,channel.pin_ax,channel.pin_ay,
               channel.pin_bx,channel.pin_by
        FROM group_order AS ordering
        LEFT JOIN report_assignments AS assignment
          ON assignment.group_id=ordering.group_id
        LEFT JOIN banks AS bank ON bank.bank_id=assignment.bank_id
        LEFT JOIN channels AS channel ON channel.channel_id=assignment.channel_id
        ORDER BY ordering.seq
        """
    )
    try:
        for group, row in zip(problem["groups"], joined):
            if (
                row[0] != group["id"]
                or row[1] is None
                or row[2] is None
                or row[12] != row[1]
                or row[7] != group["domain"]
            ):
                raise ValidationError(
                    "Chimew bank/channel assignment coverage is invalid"
                )
            bank_a = (row[8], row[9])
            bank_b = (row[10], row[11])
            pin_a = (row[13], row[14])
            pin_b = (row[15], row[16])
            raw_bank = _raw_cost(group, bank_a, bank_b)
            raw_channel = _raw_cost(group, pin_a, pin_b)
            ranked_bank = _candidate_cost(
                group, bank_a, bank_b, problem["cost_scale"]
            )
            ranked_channel = _candidate_cost(
                group, pin_a, pin_b, problem["cost_scale"]
            )
            expected = (ranked_bank, ranked_channel, raw_bank, raw_channel)
            if any(not _close(actual, value) for actual, value in zip(row[3:7], expected)):
                raise ValidationError(
                    "Chimew bank/channel assignment cost does not agree"
                )
            stage1_rank += ranked_bank
            stage2_rank += ranked_channel
            stage1_cost += raw_bank
            stage2_cost += raw_channel
    finally:
        joined.close()

    expected_priorities: Dict[str, str] = {}
    for bank_id, direction0, direction1, direction2, common in lookup.execute(
        """
        SELECT assignment.bank_id,
               sum(grouping.kind=0 AND grouping.direction=0),
               sum(grouping.kind=0 AND grouping.direction=1),
               sum(grouping.kind=0 AND grouping.direction=2),
               sum(grouping.kind=1)
        FROM report_assignments AS assignment
        JOIN group_order AS grouping ON grouping.group_id=assignment.group_id
        GROUP BY assignment.bank_id ORDER BY assignment.bank_id
        """
    ):
        counts = (int(direction0), int(direction1), int(direction2))
        common_count = int(common)

        def compatible(priority: int) -> bool:
            first, second = priority, 1 - priority
            for local_position, kind, direction in lookup.execute(
                """
                SELECT channel.local_position,grouping.kind,grouping.direction
                FROM report_assignments AS assignment
                JOIN channels AS channel
                  ON channel.channel_id=assignment.channel_id
                JOIN group_order AS grouping
                  ON grouping.group_id=assignment.group_id
                WHERE assignment.bank_id=? ORDER BY grouping.seq
                """,
                (bank_id,),
            ):
                if common_count == 0 and sum(count > 0 for count in counts) == 1:
                    eligible = kind == 0 and direction == next(
                        index for index, count in enumerate(counts) if count > 0
                    )
                elif local_position < counts[first]:
                    eligible = kind == 0 and direction == first
                elif local_position < counts[first] + counts[second]:
                    eligible = kind == 0 and direction == second
                elif local_position < sum(counts):
                    eligible = kind == 0 and direction == 2
                else:
                    eligible = kind == 1
                if not eligible:
                    return False
            return True

        compatible_priorities = [
            priority for priority in (0, 1) if compatible(priority)
        ]
        if not compatible_priorities:
            lookup.close()
            raise ValidationError("Chimew direction priority does not agree")
        expected_priority = (
            compatible_priorities[-1]
            if len(compatible_priorities) == 1
            else 0
        )
        expected_priorities[str(bank_id)] = (
            "a_to_b" if expected_priority == 0 else "b_to_a"
        )
    lookup.close()
    if report.get("direction_priority") != expected_priorities:
        raise ValidationError("Chimew direction priority does not agree")
    metrics = report.get("metrics")
    expected_metrics = {
        **problem["metrics"],
        "stage1_cost_rank": stage1_rank,
        "stage2_cost_rank": stage2_rank,
        "stage1_physical_site_cost": stage1_cost,
        "stage2_physical_site_cost": stage2_cost,
        "certified_matchings": 1 + 2 * len(expected_priorities),
        "certificate_disagreements": 0,
    }
    def metric_agrees(actual: Any, expected: Any) -> bool:
        if isinstance(expected, int):
            return (
                not isinstance(actual, bool)
                and isinstance(actual, int)
                and actual == expected
            )
        return (
            not isinstance(actual, bool)
            and isinstance(actual, (int, float))
            and math.isfinite(float(actual))
            and _close(actual, expected)
        )

    if (
        not isinstance(metrics, dict)
        or set(metrics) != set(expected_metrics)
        or any(
            not metric_agrees(metrics.get(field), value)
            for field, value in expected_metrics.items()
        )
    ):
        raise ValidationError("Chimew bank/channel report metrics do not agree")
    return problem
