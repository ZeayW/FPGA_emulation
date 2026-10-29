"""Small independent projection oracle; never a production execution path."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Iterator
import math,re
from emuflow.common.errors import ValidationError
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.scheduling.timing_routing import STA_PATHS_SCHEMA, compress_sta_paths
from emuflow.timing.sta import STA_PATH_DATABASE_SCHEMA,STA_PATH_DATABASE_STREAM_SCHEMA,STA_PATH_DATABASE_INDEX_SCHEMA,_validate_database_normalization,_normalized_slack,iter_sta_path_database_paths

def project_sta_path_database_value(
    database: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    source_input_sha256: str,
    output_path: str | None = None,
    database_path: Optional[Path] = None,
    verify_payload: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Project one loaded TimingPathDB onto one loaded assignment.

    ``source_input_sha256`` is supplied by the managed dependency certificate
    or computed once by the standalone wrapper.  This keeps provenance stable
    without re-reading an immutable multi-gigabyte input merely to hash it.
    """

    count=database.get('summary',{}).get('paths',len(database.get('paths',())))
    if not isinstance(count,int) or count>4096:
        raise AssertionError('projection oracle is restricted to small fixtures')
    if re.fullmatch(r"[0-9a-f]{64}", source_input_sha256) is None:
        raise ValidationError("STA path projection input digest is invalid")
    if database.get("schema") not in {
        STA_PATH_DATABASE_SCHEMA,
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
    }:
        raise ValidationError("STA path database schema is invalid")
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    if database.get("design") != assignment.get("design"):
        raise ValidationError(
            "STA path database design does not match assignment"
        )
    cut_nets = {
        cut["net"]
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict) and isinstance(cut.get("net"), str)
    }
    if not cut_nets:
        raise ValidationError(
            "STA path projection requires partition cut nets"
        )
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    cut_by_net = {
        cut["net"]: cut
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict)
        and isinstance(cut.get("net"), str)
        and cut["net"] in cut_nets
    }
    if set(cut_by_net) != cut_nets:
        raise ValidationError("partition cut-net records are invalid")
    cut_signature_by_net = {}
    for net in sorted(cut_by_net):
        cut = cut_by_net[net]
        sources = cut.get("source_fpgas", [])
        sinks = cut.get("sink_fpgas", [])
        if (
            not isinstance(sources, list)
            or not all(isinstance(item, str) and item for item in sources)
            or not isinstance(sinks, list)
            or not all(isinstance(item, str) and item for item in sinks)
        ):
            raise ValidationError(
                f"partition cut-net {net!r} endpoint lists are invalid"
            )
        cut_signature_by_net[net] = (
            f"{','.join(sources)}->{','.join(sinks)}"
        )
    instance_assignment = assignment.get("instance_assignment")
    if not isinstance(instance_assignment, Mapping):
        instance_assignment = {}

    def path_transitions(
        path: Mapping[str, Any], candidates: list[str]
    ) -> tuple[list[str], Optional[list[Dict[str, str]]]]:
        startpoint = path.get("startpoint")
        endpoint = path.get("endpoint")
        if not isinstance(startpoint, dict) or not isinstance(endpoint, dict):
            return candidates, None
        end_instance = endpoint.get("instance")
        end_partition = instance_assignment.get(end_instance)
        if not isinstance(end_partition, str) or not end_partition:
            return candidates, None
        target = end_partition
        reverse_transitions = []
        for net in reversed(candidates):
            raw_sources = cut_by_net[net].get("source_fpgas")
            raw_sinks = cut_by_net[net].get("sink_fpgas")
            if not isinstance(raw_sources, list) or len(raw_sources) != 1:
                return candidates, None
            source = raw_sources[0]
            if source == target:
                # This timing path follows a local fanout of a net whose
                # other sinks make the net globally cross-partition.
                continue
            if not isinstance(raw_sinks, list) or target not in raw_sinks:
                raise ValidationError(
                    f"STA path {path['id']!r} partition {target!r} cannot "
                    f"be reached through cut net {net!r}"
                )
            reverse_transitions.append(
                {"net": net, "from": source, "to": target}
            )
            target = source
        start_instance = startpoint.get("instance")
        start_partition = instance_assignment.get(start_instance)
        if (
            isinstance(start_partition, str)
            and start_partition != target
        ):
            raise ValidationError(
                f"STA path {path['id']!r} launch/capture partition chain "
                "is inconsistent"
            )
        transitions = list(reversed(reverse_transitions))
        return [item["net"] for item in transitions], transitions

    paths = []
    positive_scale_value: Optional[float] = None
    most_negative_value: Optional[float] = None
    max_period = 0.0
    covered_cut_nets = set()
    path_ids = set()
    if database.get("schema") == STA_PATH_DATABASE_SCHEMA:
        raw_paths = database.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValidationError("STA path database paths are invalid")
        path_records: Iterator[Mapping[str, Any]] = iter(raw_paths)
    else:
        if database_path is None:
            raise ValidationError(
                "indexed STA projection requires its manifest path"
            )
        path_records = iter_sta_path_database_paths(
            database_path, database, verify_payload=verify_payload
        )
    database_path_count = 0
    for index, path in enumerate(path_records):
        database_path_count += 1
        if not isinstance(path, dict):
            raise ValidationError(
                f"STA path database paths[{index}] is invalid"
            )
        path_id = path.get("id")
        clock_domain = path.get("clock_domain")
        clock_period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        fixed_delay = path.get("fixed_delay_ns")
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
            or not isinstance(clock_domain, str)
            or not clock_domain
        ):
            raise ValidationError(
                f"STA path database paths[{index}] identity is invalid"
            )
        path_ids.add(path_id)
        for name, value in (
            ("clock_period_ns", clock_period),
            ("slack_ns", slack),
            ("fixed_delay_ns", fixed_delay),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"STA path database paths[{index}].{name} is invalid"
                )
        if float(clock_period) <= 0.0 or float(fixed_delay) < 0.0:
            raise ValidationError(
                f"STA path database paths[{index}] timing is invalid"
            )
        path_nets = path.get("path_nets")
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(
                f"STA path database paths[{index}].path_nets is invalid"
            )
        expected_normalized = _normalized_slack(
            float(clock_period),
            float(slack),
            normalization,
        )
        normalized = path.get("normalized_slack")
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, (int, float))
            or not math.isfinite(float(normalized))
            or abs(float(normalized) - expected_normalized) > 1.0e-12
        ):
            raise ValidationError(
                f"STA path database paths[{index}].normalized_slack "
                "is invalid"
            )
        numeric_period = float(clock_period)
        numeric_slack = float(slack)
        max_period = max(max_period, numeric_period)
        if numeric_slack >= 0.0:
            positive_scale_value = (
                numeric_slack
                if positive_scale_value is None
                else max(positive_scale_value, numeric_slack)
            )
        else:
            most_negative_value = (
                numeric_slack
                if most_negative_value is None
                else min(most_negative_value, numeric_slack)
            )
        candidates = [net for net in path_nets if net in cut_nets]
        projected, transitions = path_transitions(path, candidates)
        if not projected:
            continue
        covered_cut_nets.update(projected)
        record = {
            "id": path_id,
            "clock_domain": clock_domain,
            "clock_period_ns": float(clock_period),
            "slack_ns": float(slack),
            "fixed_delay_ns": float(fixed_delay),
            "cut_nets": projected,
            "cut_signature": [
                cut_signature_by_net[net]
                for net in projected
            ],
            "normalized_slack": expected_normalized,
            "compressed_path_ids": [path_id],
        }
        if transitions is not None:
            record["cut_transitions"] = transitions
        paths.append(record)
    if not paths:
        raise ValidationError(
            "STA path database has no path crossing this partition"
        )
    expected_normalization = {
        "positive_slack_scale_ns": (
            1.0
            if positive_scale_value in {None, 0.0}
            else float(positive_scale_value)
        ),
        "negative_slack_scale_ns": abs(
            -1.0
            if most_negative_value is None
            else float(most_negative_value)
        ),
        "max_clock_period_ns": max_period,
    }
    if any(
        abs(expected_normalization[key] - normalization[key]) > 1.0e-12
        for key in expected_normalization
    ):
        raise ValidationError(
            "STA path database normalization does not match its paths"
        )
    artifact = compress_sta_paths({
        "schema": STA_PATHS_SCHEMA,
        "design": assignment["design"],
        "source": {
            "provider": "partition-projected-sta-paths-v1",
            # Checkpoints are first built below a staging directory and then
            # atomically moved into the content-addressed object store.  A
            # producer-local absolute path would make an otherwise identical
            # projection fail independent reconstruction after that move.
            "input_sha256": source_input_sha256,
        },
        "normalization": normalization,
        "paths": paths,
    })
    return artifact, {
        "status": "pass",
        "design": assignment["design"],
        "database_paths": database_path_count,
        "projected_paths": len(paths),
        "compressed_paths": len(artifact["paths"]),
        "cut_nets": len(cut_nets),
        "covered_cut_nets": len(covered_cut_nets),
        "uncovered_cut_nets": len(cut_nets - covered_cut_nets),
        **({"output": output_path} if output_path is not None else {}),
    }
