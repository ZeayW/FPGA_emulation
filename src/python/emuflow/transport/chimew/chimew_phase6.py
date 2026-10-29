"""Bridge certified Chimew assignments to EmuFlow lane/electrical contracts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_PROVIDER, CHIMEW_BANK_CHANNEL_REPORT_SCHEMA, evaluate_chimew_bank_channel_assignment, validate_chimew_bank_channel_input
from emuflow.transport.chimew.chimew_bank_channel_storage import BankChannelInputView, read_chimew_bank_channel_input
from emuflow.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView, read_chimew_bank_channel_report, copy_chimew_bank_channel_report_storage
from emuflow.transport.chimew.chimew_grouping import _tdm_ratio
from emuflow.storage.store_publication import bind_view_sources
from emuflow.common.native_tools import require_native_schema
from emuflow.transport.chimew.chimew_binding_storage import CHIMEW_BINDING_STORAGE_SCHEMA, ChimewBindingView, read_chimew_binding, write_chimew_binding_storage
from emuflow.transport.chimew.chimew_qualification import CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA, CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER, CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER, canonical_sha256, validate_chimew_bank_channel_report_artifact, validate_chimew_qualification_binding, validate_chimew_qualification_seal
from emuflow.common.errors import ValidationError
from emuflow.transport.chimew.chimew_fixed_wiring import FIXED_WIRING_MAP_SCHEMA, FIXED_WIRING_BINDING_SCHEMA, FIXED_WIRING_CLASS, FixedWiringChannels, electrical_channel_pin_keys
from emuflow.common.io import read_json, write_json
from emuflow.transport.pin_planning import CHIMEW_PHYSICAL_POSITION_PROVIDER, CHIMEW_PIN_PLAN_PROVIDER, PIN_PLAN_SCHEMA, SIGNAL_POSITION_HINTS_SCHEMA, _assignment_metrics, _baseline_assignment, _install_indexed_schedule_entry_relation, _validate_bank_pin_rows, validate_bank_pin_assignment, validate_pin_plan
from emuflow.transport.pin_plan_storage import PIN_PLAN_STORAGE_SCHEMA, read_pin_plan, write_pin_plan_storage
from emuflow.platform.platform import Platform
from emuflow.transport.position_hints_storage import POSITION_HINTS_STORAGE_SCHEMA, read_position_hints, write_position_hints_storage
from emuflow.scheduling.tdm_schedule_storage import canonical_tdm_schedule_sha256, read_tdm_schedule


CHIMEW_ELECTRICAL_MAP_SCHEMA_V1 = "emuflow.chimew-electrical-channel-map/v1"
CHIMEW_ELECTRICAL_MAP_SCHEMA = "emuflow.chimew-electrical-channel-map/v2"
CHIMEW_ELECTRICAL_MAP_PROVIDER = (
    "source-qualified-boarddb-electrical-channel-map-v1"
)
CHIMEW_PHASE6_BINDING_SCHEMA_V1 = "emuflow.chimew-phase6-electrical-binding/v1"
CHIMEW_PHASE6_BINDING_SCHEMA = "emuflow.chimew-phase6-electrical-binding/v2"
CHIMEW_PHASE6_BINDING_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V1 = (
    "emuflow.chimew-phase6-adapter-report/v1"
)
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V2 = (
    "emuflow.chimew-phase6-adapter-report/v2"
)
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA = "emuflow.chimew-phase6-adapter-report/v3"
_IOSTANDARD_VOLTAGES = {
    "LVCMOS12": 1.2,
    "LVCMOS15": 1.5,
    "LVCMOS18": 1.8,
    "LVCMOS25": 2.5,
    "LVCMOS33": 3.3,
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_chimew_adapter_file(source: Path, destination: Path) -> None:
    """Atomically publish one already-sealed adapter artifact."""

    os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


_ADAPTER_GENERATION_PATTERN = re.compile(
    r"^(adapter_report|bank_channel_report|qualification_certificate|"
    r"position_hints|pin_plan|electrical_binding)\.([0-9a-f]{64})"
    r"\.(json|sqlite)$"
)
_ADAPTER_LEGACY_FIXED_NAMES = {
    "adapter_report.json",
    "bank_channel_report.json",
    "qualification_certificate.json",
    "position_hints.json",
    "pin_plan.json",
    "electrical_binding.json",
}
_ADAPTER_ARTIFACT_LAYOUT = {
    "bank_channel_report": ("bank_channel_report", "json"),
    "bank_channel_report_store": ("bank_channel_report", "sqlite"),
    "qualification_certificate": ("qualification_certificate", "json"),
    "position_hints": ("position_hints", "json"),
    "position_hints_store": ("position_hints", "sqlite"),
    "pin_plan": ("pin_plan", "json"),
    "pin_plan_store": ("pin_plan", "sqlite"),
    "electrical_binding": ("electrical_binding", "json"),
    "electrical_binding_store": ("electrical_binding", "sqlite"),
}


def chimew_adapter_report_generation_path(
    output_dir: Path, report: Mapping[str, Any]
) -> Path:
    """Return the immutable report-generation path for ``report``."""

    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return Path(output_dir) / f"adapter_report.{digest}.json"


def validate_chimew_adapter_generation(report_path: Path) -> set[Path]:
    """Validate one complete immutable adapter generation as a group."""

    report_path = Path(report_path)
    match = _ADAPTER_GENERATION_PATTERN.fullmatch(report_path.name)
    if (
        match is None
        or match.group(1) != "adapter_report"
        or match.group(3) != "json"
        or not report_path.is_file()
        or _file_sha256(report_path) != match.group(2)
    ):
        raise ValidationError("Chimew adapter report generation is invalid")
    report = read_json(report_path)
    artifacts = report.get("artifacts")
    schema = report.get("schema")
    required_labels = set(_ADAPTER_ARTIFACT_LAYOUT) - {
        "qualification_certificate"
    }
    if schema != CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA:
        required_labels.remove("bank_channel_report_store")
    supplied_labels = set(artifacts) if isinstance(artifacts, dict) else set()
    if (
        schema
        not in {
            CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V2,
            CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
        }
        or report.get("status") != "pass"
        or report.get("provider") != CHIMEW_PHASE6_BINDING_PROVIDER
        or not isinstance(report.get("design"), str)
        or not isinstance(report.get("platform"), str)
        or not isinstance(artifacts, dict)
        or not required_labels <= supplied_labels
        or not supplied_labels <= set(_ADAPTER_ARTIFACT_LAYOUT)
    ):
        raise ValidationError("Chimew adapter report generation is malformed")

    paths = {"adapter_report": report_path.resolve()}
    for label, name in artifacts.items():
        artifact_match = (
            _ADAPTER_GENERATION_PATTERN.fullmatch(name)
            if isinstance(name, str) and Path(name).name == name
            else None
        )
        if (
            artifact_match is None
            or (artifact_match.group(1), artifact_match.group(3))
            != _ADAPTER_ARTIFACT_LAYOUT[label]
        ):
            raise ValidationError("Chimew adapter generation path is invalid")
        artifact_path = report_path.with_name(name)
        if (
            not artifact_path.is_file()
            or _file_sha256(artifact_path) != artifact_match.group(2)
        ):
            raise ValidationError("Chimew adapter generation seal differs")
        paths[label] = artifact_path.resolve()

    try:
        position_selector = read_json(paths["position_hints"])
        pin_selector = read_json(paths["pin_plan"])
        binding_selector = read_json(paths["electrical_binding"])
    except (OSError, ValueError) as error:
        raise ValidationError(
            "Chimew adapter selector generation is malformed"
        ) from error
    if (
        position_selector.get("schema") != POSITION_HINTS_STORAGE_SCHEMA
        or position_selector.get("artifact")
        != Path(paths["position_hints_store"]).name
        or pin_selector.get("schema") != PIN_PLAN_STORAGE_SCHEMA
        or pin_selector.get("artifact") != Path(paths["pin_plan_store"]).name
        or binding_selector.get("schema") != CHIMEW_BINDING_STORAGE_SCHEMA
        or binding_selector.get("artifact")
        != Path(paths["electrical_binding_store"]).name
    ):
        raise ValidationError("Chimew adapter selector/store pairing differs")
    try:
        read_position_hints(paths["position_hints"])
        read_pin_plan(paths["pin_plan"])
        read_chimew_binding(paths["electrical_binding"])
    except (OSError, ValueError, sqlite3.Error) as error:
        raise ValidationError(
            "Chimew adapter indexed generation is malformed"
        ) from error

    try:
        bank_report = read_chimew_bank_channel_report(
            paths["bank_channel_report"],
        )
    except (OSError, ValueError, ValidationError) as error:
        raise ValidationError(
            "Chimew bank/channel report generation is malformed"
        ) from error
    if (
        bank_report.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
        or bank_report.get("provider") != CHIMEW_BANK_CHANNEL_PROVIDER
        or bank_report.get("design") != report["design"]
        or bank_report.get("platform") != report["platform"]
        or not isinstance(bank_report.get("input_sha256"), str)
        or len(bank_report["input_sha256"]) != 64
        or not isinstance(bank_report.get("assignments"), Sequence)
        or not isinstance(bank_report.get("metrics"), dict)
        or not isinstance(bank_report.get("provenance"), dict)
    ):
        raise ValidationError("Chimew bank/channel report generation is malformed")
    if "bank_channel_report_store" in paths and (
        not isinstance(bank_report, BankChannelReportView)
        or bank_report.store_path != paths["bank_channel_report_store"]
    ):
        raise ValidationError("Chimew bank/channel report selector/store pairing differs")
    if isinstance(bank_report, BankChannelReportView):
        bank_report.close()

    if "qualification_certificate" in paths:
        try:
            certificate = read_json(paths["qualification_certificate"])
        except (OSError, ValueError) as error:
            raise ValidationError(
                "Chimew qualification generation is malformed"
            ) from error
        identity = (certificate.get("schema"), certificate.get("provider"))
        if (
            identity
            not in {
                (CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER),
                (
                    CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA,
                    CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER,
                ),
            }
            or certificate.get("status") != "pass"
            or certificate.get("design") != report["design"]
            or certificate.get("platform") != report["platform"]
        ):
            raise ValidationError("Chimew qualification generation is malformed")
        unsigned = dict(certificate)
        supplied_sha = unsigned.pop("qualification_sha256", None)
        if supplied_sha != canonical_sha256(unsigned):
            raise ValidationError("Chimew qualification generation seal differs")
    return set(paths.values())


def garbage_collect_chimew_adapter_generations(
    output_dir: Path,
    referenced_names: Sequence[str],
    *,
    additionally_retired_names: Sequence[str] = (),
) -> None:
    """Remove only unreferenced, validated immutable adapter generations.

    ``additionally_retired_names`` is reserved for names previously sealed by
    an outer pipeline report.  It permits removal of legacy fixed adapter
    artifacts only after that outer report has committed a new inventory;
    arbitrary extra files are never removed here.
    """

    output_dir = Path(output_dir)
    retained = {
        name
        for name in referenced_names
        if isinstance(name, str) and Path(name).name == name
    }
    retired = {
        name
        for name in additionally_retired_names
        if isinstance(name, str) and Path(name).name == name
    }
    if not output_dir.is_dir():
        return
    removable: set[Path] = set()
    for report_path in output_dir.glob("adapter_report.*.json"):
        try:
            group = validate_chimew_adapter_generation(report_path)
        except (OSError, ValueError, sqlite3.Error, ValidationError):
            continue
        if report_path.name not in retained:
            removable.update(
                path for path in group if path.name not in retained
            )
    for name in retired & _ADAPTER_LEGACY_FIXED_NAMES:
        path = output_dir / name
        if path.is_file() and name not in retained:
            removable.add(path)
    for path in removable:
        try:
            path.unlink()
        except OSError:
            # GC follows a complete commit and does not define its validity.
            pass


class _IndexedPinPlanRecords(Sequence[Mapping[str, Any]]):
    """Bounded view over Chimew's temporary member/schedule join."""

    def __init__(self, connection: sqlite3.Connection, count: int):
        self._connection = connection
        self._count = count

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schedule_entry": str(row[0]),
            "group": int(row[1]),
            "physical_lane": int(row[2]),
            "logical_lane": int(row[3]),
        }

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        for row in self._connection.execute(
            """
            SELECT member.entry_id,member.group_index,member.physical_lane,
                   schedule.lane
            FROM chimew_build_members AS member
            JOIN indexed_schedule_entries AS schedule
              ON schedule.identity=member.entry_id
            ORDER BY member.entry_id
            """
        ):
            yield self._record(row)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        row = self._connection.execute(
            """
            SELECT member.entry_id,member.group_index,member.physical_lane,
                   schedule.lane
            FROM chimew_build_members AS member
            JOIN indexed_schedule_entries AS schedule
              ON schedule.identity=member.entry_id
            ORDER BY member.entry_id LIMIT 1 OFFSET ?
            """,
            (index,),
        ).fetchone()
        if row is None:
            raise ValidationError("Chimew build member sequence is not contiguous")
        return self._record(row)


def _open_chimew_build_index(
    schedule: Mapping[str, Any],
) -> tuple[sqlite3.Connection, int]:
    """Expose schedule entries through a bounded file-backed relation."""

    connection = sqlite3.connect("", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=FILE")
    try:
        store_path = getattr(schedule, "store_path", None)
        if isinstance(store_path, Path):
            connection.execute(
                "ATTACH DATABASE ? AS schedule_store",
                (immutable_sqlite_uri(store_path),),
            )
            schedule_count = _install_indexed_schedule_entry_relation(connection)
        else:
            connection.execute(
                """
                CREATE TABLE indexed_schedule_entries(
                  identity TEXT PRIMARY KEY,
                  link_id TEXT NOT NULL,
                  source_fpga TEXT NOT NULL,
                  sink_fpga TEXT NOT NULL,
                  slot INTEGER NOT NULL,
                  lane INTEGER NOT NULL,
                  tdm_ratio INTEGER NOT NULL,
                  well_typed INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )
            raw_entries = schedule.get("entries")
            if not isinstance(raw_entries, Sequence) or isinstance(
                raw_entries, (str, bytes)
            ):
                raise ValidationError("schedule entries are missing")
            pending: list[tuple[Any, ...]] = []
            try:
                for entry in raw_entries:
                    if not isinstance(entry, Mapping):
                        raise ValidationError("schedule entry is malformed")
                    identity = entry.get("id")
                    link = entry.get("link")
                    source = entry.get("from")
                    sink = entry.get("to")
                    slot = entry.get("slot")
                    lane = entry.get("lane")
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or not isinstance(link, str)
                        or not link
                        or not isinstance(source, str)
                        or not source
                        or not isinstance(sink, str)
                        or not sink
                        or isinstance(slot, bool)
                        or not isinstance(slot, int)
                        or slot < 0
                        or isinstance(lane, bool)
                        or not isinstance(lane, int)
                        or lane < 0
                    ):
                        raise ValidationError(
                            f"schedule entry {identity!r} is malformed"
                        )
                    pending.append(
                        (identity, link, source, sink, slot, lane, _tdm_ratio(entry), 1)
                    )
                    if len(pending) >= 4096:
                        connection.executemany(
                            "INSERT INTO indexed_schedule_entries VALUES "
                            "(?,?,?,?,?,?,?,?)",
                            pending,
                        )
                        pending.clear()
                if pending:
                    connection.executemany(
                        "INSERT INTO indexed_schedule_entries VALUES "
                        "(?,?,?,?,?,?,?,?)",
                        pending,
                    )
            except sqlite3.IntegrityError as error:
                raise ValidationError("schedule entry identities are duplicated") from error
            schedule_count = int(
                connection.execute(
                    "SELECT count(*) FROM indexed_schedule_entries"
                ).fetchone()[0]
            )
        connection.executescript(
            """
            CREATE TABLE chimew_build_members(
              entry_id TEXT PRIMARY KEY,
              group_index INTEGER NOT NULL,
              member_seq INTEGER NOT NULL,
              member_direction TEXT NOT NULL,
              expected_link TEXT NOT NULL,
              expected_source TEXT NOT NULL,
              expected_sink TEXT NOT NULL,
              physical_lane INTEGER NOT NULL,
              source_y REAL NOT NULL,
              sink_y REAL NOT NULL,
              source_region INTEGER NOT NULL,
              sink_region INTEGER NOT NULL,
              UNIQUE(group_index,member_seq)
            ) WITHOUT ROWID;
            CREATE INDEX chimew_build_members_group
              ON chimew_build_members(group_index,member_seq);
            CREATE TABLE chimew_build_package_pins(
              fpga TEXT NOT NULL,
              package_pin TEXT NOT NULL,
              PRIMARY KEY(fpga,package_pin)
            ) WITHOUT ROWID;
            """
        )
        return connection, schedule_count
    except BaseException:
        connection.close()
        raise


def _iter_chimew_position_records(
    connection: sqlite3.Connection,
) -> Iterator[Mapping[str, Any]]:
    for row in connection.execute(
        """
        SELECT entry_id,source_y,sink_y,source_region,sink_region
        FROM chimew_build_members ORDER BY entry_id
        """
    ):
        yield {
            "schedule_entry": str(row[0]),
            "source_y": float(row[1]),
            "sink_y": float(row[2]),
            "source_region": int(row[3]),
            "sink_region": int(row[4]),
            "source_fallback": False,
            "sink_fallback": False,
        }


def _indexed_assignment_metrics(
    connection: sqlite3.Connection,
    platform: Platform,
    *,
    baseline: bool,
) -> Dict[str, float]:
    """Reproduce pin-plan metrics without a hop-sized Python assignment."""

    link_width = {
        link.id: link.transport_bits_per_cycle_per_direction
        for link in platform.links
    }
    group_stats: Dict[Any, list[float | int]] = {}
    pin_distance = 0.0
    for row in connection.execute(
        """
        SELECT member.entry_id,member.group_index,member.physical_lane,
               member.source_y,member.sink_y,member.source_region,
               member.sink_region,schedule.link_id,schedule.source_fpga,
               schedule.sink_fpga,schedule.lane
        FROM chimew_build_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.entry_id
        ORDER BY member.entry_id
        """
    ):
        if baseline:
            group_key: Any = (str(row[7]), str(row[8]), str(row[9]), int(row[10]))
            pin = int(row[10])
        else:
            group_key = int(row[1])
            pin = int(row[2])
        stats = group_stats.setdefault(group_key, [0, 0, 0.0, 0.0])
        stats[0] = int(stats[0]) | (
            (1 << int(row[5])) | (1 << (32 + int(row[6])))
        )
        for value in (float(row[3]), float(row[4])):
            count = int(stats[1]) + 1
            delta = value - float(stats[2])
            mean = float(stats[2]) + delta / count
            stats[3] = float(stats[3]) + delta * (value - mean)
            stats[1] = count
            stats[2] = mean
        lanes = link_width[str(row[7])]
        pin_y = 0.5 if lanes == 1 else pin / (lanes - 1)
        pin_distance += abs(float(row[3]) - pin_y)
        pin_distance += abs(float(row[4]) - pin_y)
    crossing_bits = sum(
        bin(int(stats[0])).count("1") for stats in group_stats.values()
    )
    position_sse = sum(float(stats[3]) for stats in group_stats.values())
    return {
        "objective": float(crossing_bits) + position_sse,
        "crossing_bits": float(crossing_bits),
        "position_sse": position_sse,
        "pin_distance": pin_distance,
    }


def _indexed_chimew_validation_available(
    schedule: Mapping[str, Any], pin_plan: Mapping[str, Any]
) -> bool:
    return all(
        isinstance(getattr(value, "store_path", None), Path)
        for value in (schedule, pin_plan)
    )


def _open_indexed_chimew_validation(
    schedule: Mapping[str, Any],
    pin_plan: Mapping[str, Any],
    platform: Platform,
    binding: Mapping[str, Any],
) -> tuple[sqlite3.Connection, int]:
    """Open sealed schedule/plan relations and validate exact coverage."""

    connection = sqlite3.connect("", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=FILE")
    try:
        connection.execute(
            "ATTACH DATABASE ? AS schedule_store",
            (
                immutable_sqlite_uri(schedule.store_path),
            ),
        )
        connection.execute(
            "ATTACH DATABASE ? AS pin_store",
            (
                immutable_sqlite_uri(pin_plan.store_path),
            ),
        )
        schedule_count = _install_indexed_schedule_entry_relation(connection)
        plan_count = connection.execute(
            "SELECT count(*) FROM pin_store.entries"
        ).fetchone()[0]
        joined_count = connection.execute(
            """
            SELECT count(*)
            FROM indexed_schedule_entries AS schedule
            JOIN pin_store.entries AS pin
              ON pin.schedule_entry=schedule.identity
            """
        ).fetchone()[0]
        if schedule_count != plan_count or joined_count != schedule_count:
            raise ValidationError(
                "Chimew Phase 6 electrical schedule coverage is invalid"
            )
        mismatch = connection.execute(
            """
            SELECT schedule.identity
            FROM indexed_schedule_entries AS schedule
            JOIN pin_store.entries AS pin
              ON pin.schedule_entry=schedule.identity
            WHERE (
                pin.schedule_entry IS NOT json_extract(
                  pin.record_json,'$.schedule_entry'
                )
                OR pin.physical_lane IS NOT json_extract(
                  pin.record_json,'$.physical_lane'
                )
              )
            LIMIT 1
            """
        ).fetchone()
        if mismatch is not None:
            raise ValidationError(
                "Chimew Phase 6 electrical schedule coverage is invalid"
            )
        _validate_bank_pin_rows(
            schedule,
            platform,
            connection.execute(
                """
                SELECT schedule.link_id,
                       schedule.source_fpga,
                       schedule.sink_fpga,
                       schedule.lane,
                       pin.physical_lane
                FROM indexed_schedule_entries AS schedule
                JOIN pin_store.entries AS pin
                  ON pin.schedule_entry=schedule.identity
                ORDER BY schedule.identity
                """
            ),
        )
        if isinstance(binding, ChimewBindingView):
            connection.execute(
                "ATTACH DATABASE ? AS binding_store",
                (
                    immutable_sqlite_uri(binding.store_path),
                ),
            )
            connection.execute(
                """
                CREATE TEMP VIEW binding_members AS
                SELECT member.member AS member,
                       member.group_seq AS group_index,
                       groups.direction AS declared_direction,
                       groups.link_id AS link_id,
                       groups.fpga_a AS fpga_a,
                       groups.fpga_b AS fpga_b,
                       groups.physical_lane AS physical_lane
                FROM binding_store.members AS member
                JOIN binding_store.groups AS groups
                  ON groups.seq=member.group_seq
                """
            )
        else:
            connection.execute(
                """
                CREATE TABLE binding_members(
                  member TEXT PRIMARY KEY,
                  group_index INTEGER NOT NULL,
                  declared_direction TEXT NOT NULL,
                  link_id TEXT NOT NULL,
                  fpga_a TEXT NOT NULL,
                  fpga_b TEXT NOT NULL,
                  physical_lane INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )
        return connection, int(schedule_count)
    except BaseException:
        connection.close()
        raise


def _stage_indexed_chimew_members(
    connection: sqlite3.Connection,
    *,
    group_index: int,
    members: list[str],
    direction: str,
    link: str,
    fpga_a: str,
    fpga_b: str,
    physical_lane: int,
) -> None:
    try:
        connection.executemany(
            "INSERT INTO binding_members VALUES (?,?,?,?,?,?,?)",
            (
                (
                    member,
                    group_index,
                    direction,
                    link,
                    fpga_a,
                    fpga_b,
                    physical_lane,
                )
                for member in members
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ValidationError(
            "Chimew electrical binding duplicates schedule entries"
        ) from error


def _validate_indexed_chimew_members(
    connection: sqlite3.Connection, schedule_count: int
) -> None:
    """Validate all members with ordered SQL joins and bounded state."""

    member_count = connection.execute(
        "SELECT count(*) FROM binding_members"
    ).fetchone()[0]
    unknown_member = connection.execute(
        """
        SELECT member.member
        FROM binding_members AS member
        LEFT JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        LEFT JOIN pin_store.entries AS pin
          ON pin.schedule_entry=member.member
        WHERE schedule.identity IS NULL OR pin.schedule_entry IS NULL
        LIMIT 1
        """
    ).fetchone()
    if unknown_member is not None:
        raise ValidationError(
            "Chimew electrical binding conflicts with pin plan"
        )
    if member_count != schedule_count:
        raise ValidationError("Chimew electrical binding coverage is incomplete")
    invalid_direction = connection.execute(
        """
        SELECT member.member
        FROM binding_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        JOIN pin_store.entries AS pin
          ON pin.schedule_entry=member.member
        WHERE NOT (
             (
               schedule.source_fpga=member.fpga_a
               AND schedule.sink_fpga=member.fpga_b
             ) OR (
               schedule.source_fpga=member.fpga_b
               AND schedule.sink_fpga=member.fpga_a
             )
           )
        LIMIT 1
        """
    ).fetchone()
    if invalid_direction is not None:
        raise ValidationError(
            "Chimew electrical binding direction is invalid"
        )
    conflict = connection.execute(
        """
        SELECT member.member
        FROM binding_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        JOIN pin_store.entries AS pin
          ON pin.schedule_entry=member.member
        WHERE schedule.link_id != member.link_id
           OR pin.physical_lane != member.physical_lane
           OR schedule.well_typed != 1
        LIMIT 1
        """
    ).fetchone()
    if conflict is not None:
        raise ValidationError(
            "Chimew electrical binding conflicts with pin plan"
        )
    duplicate_slot = connection.execute(
        """
        SELECT member.group_index
        FROM binding_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        GROUP BY member.group_index,
                 schedule.slot
        HAVING count(*) > 1 LIMIT 1
        """
    ).fetchone()
    if duplicate_slot is not None:
        raise ValidationError(
            "Chimew electrical binding conflicts with pin plan"
        )
    invalid_ratio = connection.execute(
        """
        SELECT member.group_index
        FROM binding_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        GROUP BY member.group_index,
          CASE
            WHEN schedule.source_fpga=member.fpga_a
             AND schedule.sink_fpga=member.fpga_b
            THEN 'a_to_b' ELSE 'b_to_a'
          END
        HAVING min(schedule.tdm_ratio) != max(schedule.tdm_ratio)
            OR min(schedule.tdm_ratio) < 1
            OR count(*) > min(schedule.tdm_ratio)
        LIMIT 1
        """
    ).fetchone()
    if invalid_ratio is not None:
        raise ValidationError("Chimew electrical binding TDM bundle is invalid")
    invalid_direction = connection.execute(
        """
        SELECT member.group_index
        FROM binding_members AS member
        JOIN indexed_schedule_entries AS schedule
          ON schedule.identity=member.member
        GROUP BY member.group_index,member.declared_direction
        HAVING (
          member.declared_direction='bidirectional'
          AND (
            count(DISTINCT CASE
              WHEN schedule.source_fpga=member.fpga_a
               AND schedule.sink_fpga=member.fpga_b
              THEN 'a_to_b' ELSE 'b_to_a' END
            ) != 2
          )
        ) OR (
          member.declared_direction!='bidirectional'
          AND (
            count(DISTINCT CASE
              WHEN schedule.source_fpga=member.fpga_a
               AND schedule.sink_fpga=member.fpga_b
              THEN 'a_to_b' ELSE 'b_to_a' END
            ) != 1
            OR min(CASE
              WHEN schedule.source_fpga=member.fpga_a
               AND schedule.sink_fpga=member.fpga_b
              THEN 'a_to_b' ELSE 'b_to_a' END
            ) != member.declared_direction
          )
        )
        LIMIT 1
        """
    ).fetchone()
    if invalid_direction is not None:
        raise ValidationError("Chimew electrical binding TDM bundle is invalid")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}: expected a non-empty string")
    return value


def _digest(value: Any, label: str) -> str:
    digest = _string(value, label).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValidationError(f"{label}: expected a SHA-256 digest")
    return digest


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValidationError(f"{label}: expected a finite number") from error
    if not math.isfinite(number):
        raise ValidationError(f"{label}: expected a finite number")
    return number


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_chimew_electrical_map(
    document: Mapping[str, Any],
    platform: Platform,
    problem: Mapping[str, Any],
) -> Dict[str, Any]:
    """Check concrete lanes, banks, package pins, and electrical identity."""

    schema = document.get("schema")
    if schema not in {CHIMEW_ELECTRICAL_MAP_SCHEMA_V1, CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}:
        raise ValidationError("Chimew electrical channel-map schema is invalid")
    fixed = FixedWiringChannels(platform) if schema == FIXED_WIRING_MAP_SCHEMA else None
    if platform.virtual_board_model is not None and fixed is None:
        raise ValidationError("fixed LVDS BoardDB requires a differential Chimew electrical map")
    if fixed is not None:
        fixed.validate_source(document.get("fixed_wiring"))
    if document.get("provider") != CHIMEW_ELECTRICAL_MAP_PROVIDER:
        raise ValidationError("Chimew electrical channel map is not source-qualified")
    if (
        document.get("design") != problem["design"]
        or document.get("platform") != platform.name
        or problem["platform"] != platform.name
    ):
        raise ValidationError("Chimew electrical channel-map identity does not agree")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew electrical channel-map provenance is missing")
    normalized_provenance = {
        "producer": _string(provenance.get("producer"), "electrical.producer"),
        "producer_version": _string(
            provenance.get("producer_version"), "electrical.producer_version"
        ),
        "boarddb_sha256": _digest(
            provenance.get("boarddb_sha256"), "electrical.boarddb_sha256"
        ),
        "package_pin_inventory_sha256": _digest(
            provenance.get("package_pin_inventory_sha256"),
            "electrical.package_pin_inventory_sha256",
        ),
    }

    raw_bounds = document.get("fpga_y_bounds")
    if not isinstance(raw_bounds, list):
        raise ValidationError("Chimew electrical FPGA bounds are missing")
    bounds: Dict[str, Tuple[float, float]] = {}
    for index, record in enumerate(raw_bounds):
        if not isinstance(record, dict):
            raise ValidationError(f"electrical.fpga_y_bounds[{index}] is invalid")
        fpga = _string(record.get("fpga"), f"electrical.bounds[{index}].fpga")
        low = _number(record.get("y_min"), f"electrical.bounds[{fpga}].y_min")
        high = _number(record.get("y_max"), f"electrical.bounds[{fpga}].y_max")
        if fpga in bounds or high <= low:
            raise ValidationError("Chimew electrical FPGA bounds are invalid")
        bounds[fpga] = (low, high)
    expected_fpgas = {fpga.id for fpga in platform.fpgas}
    used_fpgas = {
        endpoint
        for domain in problem["domains"]
        for endpoint in (domain["fpga_a"], domain["fpga_b"])
    }
    if set(bounds) != used_fpgas or not used_fpgas <= expected_fpgas:
        raise ValidationError("Chimew electrical FPGA bounds do not cover domains")

    link_by_id = {link.id: link for link in platform.links}
    bank_by_channel = {
        channel["id"]: (bank, channel)
        for bank in problem["banks"]
        for channel in bank["channels"]
    }
    raw_channels = document.get("channels")
    if not isinstance(raw_channels, list):
        raise ValidationError("Chimew electrical channel records are missing")
    channels = {}
    lane_uses: Dict[Tuple[str, int], list[str]] = {}
    used_pins = set()
    for index, record in enumerate(raw_channels):
        if not isinstance(record, dict):
            raise ValidationError(f"electrical.channels[{index}] is invalid")
        channel_id = _string(
            record.get("chimew_channel"), f"electrical.channels[{index}].id"
        )
        if channel_id in channels or channel_id not in bank_by_channel:
            raise ValidationError("Chimew electrical channel identity is invalid")
        bank, assignment_channel = bank_by_channel[channel_id]
        domain = problem["domains"][bank["domain"]]
        link_id = _string(record.get("link"), f"electrical.{channel_id}.link")
        link = link_by_id.get(link_id)
        if link is None or set(link.endpoints) != {
            domain["fpga_a"], domain["fpga_b"]
        }:
            raise ValidationError("Chimew electrical channel link/domain mismatch")
        lane = record.get("physical_lane")
        raw_direction = record.get("direction")
        if schema in {CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}:
            if raw_direction not in {"a_to_b", "b_to_a", "either"}:
                raise ValidationError(
                    "Chimew electrical channel direction is invalid"
                )
            direction = raw_direction
        else:
            if raw_direction is not None:
                raise ValidationError(
                    "legacy Chimew electrical maps cannot declare a direction"
                )
            direction = "either"
        lane_key = (link_id, lane)
        if (
            isinstance(lane, bool)
            or not isinstance(lane, int)
            or not 0 <= lane < link.transport_bits_per_cycle_per_direction
        ):
            raise ValidationError("Chimew electrical concrete lane is invalid")
        uses = lane_uses.setdefault(lane_key, [])
        direction_qualified_reuse = (
            schema in {CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}
            and link.direction == "full_duplex"
            and link.capacity_sharing == "per_direction"
        )
        if direction == "either":
            maximum_either_uses = 2 if direction_qualified_reuse else 1
            if (
                any(value != "either" for value in uses)
                or len(uses) >= maximum_either_uses
            ):
                raise ValidationError("Chimew electrical concrete lane is invalid")
        elif (
            "either" in uses
            or direction in uses
            or (uses and not direction_qualified_reuse)
        ):
            raise ValidationError("Chimew electrical concrete lane is invalid")
        uses.append(direction)
        bank_a = _string(record.get("bank_a"), f"electrical.{channel_id}.bank_a")
        bank_b = _string(record.get("bank_b"), f"electrical.{channel_id}.bank_b")
        if bank_a != bank["bank_a"]["id"] or bank_b != bank["bank_b"]["id"]:
            raise ValidationError("Chimew electrical bank identity does not agree")
        pin_a = _string(
            record.get("package_pin_a"), f"electrical.{channel_id}.package_pin_a"
        )
        pin_b = _string(
            record.get("package_pin_b"), f"electrical.{channel_id}.package_pin_b"
        )
        differential = (fixed.validate_channel(record, domain["fpga_a"], domain["fpga_b"])
                        if fixed is not None else {})
        pin_keys = electrical_channel_pin_keys({**record, "fpga_a": domain["fpga_a"],
                                               "fpga_b": domain["fpga_b"]}, fixed)
        if any(key in used_pins for key in pin_keys):
            raise ValidationError("Chimew electrical package-pin collision")
        used_pins.update(pin_keys)
        iostandard = _string(
            record.get("iostandard"), f"electrical.{channel_id}.iostandard"
        )
        supported = record.get("supported_iostandards")
        if (
            not isinstance(supported, list)
            or not supported
            or not all(isinstance(value, str) and value for value in supported)
            or len(set(supported)) != len(supported)
            or iostandard not in supported
        ):
            raise ValidationError("Chimew electrical IOSTANDARD support is invalid")
        if record.get("reserved") is not False:
            raise ValidationError("Chimew electrical channel is reserved")
        placement_anchor = record.get("placement_anchor", True)
        if not isinstance(placement_anchor, bool):
            raise ValidationError("Chimew electrical placement anchor is invalid")
        if fixed is None and record.get("electrical_class") != "single_ended_parallel":
            raise ValidationError(
                "Chimew electrical maps require single-ended parallel channels"
            )
        voltage = _number(
            record.get("bank_voltage"), f"electrical.{channel_id}.bank_voltage"
        )
        expected_voltage = 1.8 if fixed is not None else _IOSTANDARD_VOLTAGES.get(iostandard)
        if expected_voltage is None or abs(voltage - expected_voltage) > 1.0e-9:
            raise ValidationError("Chimew electrical bank voltage/IOSTANDARD mismatch")
        channels[channel_id] = {
            "link": link_id,
            "physical_lane": lane,
            "direction": direction,
            "fpga_a": domain["fpga_a"],
            "fpga_b": domain["fpga_b"],
            "bank_a": bank_a,
            "bank_b": bank_b,
            "package_pin_a": pin_a,
            "package_pin_b": pin_b,
            "iostandard": iostandard,
            "supported_iostandards": sorted(supported),
            "bank_voltage": voltage,
            "electrical_class": FIXED_WIRING_CLASS if fixed is not None else "single_ended_parallel",
            "placement_anchor": placement_anchor,
            **differential,
            # Preserve the optimizer's physical site coordinates all the way
            # into the sealed Phase-6 binding.  The concrete lane number is
            # an electrical identity, not a device-placement coordinate.
            "pin_a_point": {
                "x": assignment_channel["pin_a"][0],
                "y": assignment_channel["pin_a"][1],
            },
            "pin_b_point": {
                "x": assignment_channel["pin_b"][0],
                "y": assignment_channel["pin_b"][1],
            },
        }
    if set(channels) != set(bank_by_channel):
        raise ValidationError("Chimew electrical channel coverage is incomplete")
    metrics = document.get("metrics")
    expected_metrics = {
        "channels": len(channels),
        "package_pins": len(used_pins),
        "concrete_lanes": sum(len(uses) for uses in lane_uses.values()),
    }
    if not isinstance(metrics, dict) or any(
        metrics.get(field) != value for field, value in expected_metrics.items()
    ):
        raise ValidationError("Chimew electrical channel-map metrics do not agree")
    return {
        "provenance": normalized_provenance,
        "bounds": bounds,
        "channels": channels,
        "metrics": expected_metrics,
        "map_sha256": _canonical_sha256(document),
        "fixed_wiring": fixed.source if fixed is not None else None,
    }


def _normalized_y(y: float, bounds: Tuple[float, float], label: str) -> float:
    low, high = bounds
    tolerance = 1.0e-9 * max(1.0, abs(low), abs(high))
    if y < low - tolerance or y > high + tolerance:
        raise ValidationError(f"{label}: physical y lies outside FPGA bounds")
    return min(1.0, max(0.0, (y - low) / (high - low)))


def build_chimew_phase6_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    bank_channel_input: Mapping[str, Any],
    electrical_map: Mapping[str, Any],
    *,
    qualification_document: Optional[Mapping[str, Any]] = None,
    bank_channel_report_document: Optional[Mapping[str, Any]] = None,
    bank_channel_report_output_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
    electrical_binding_output_path: Optional[Path] = None,
    position_hints_output_path: Optional[Path] = None,
    pin_plan_output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run certified Chimew assignment and bind it to concrete EmuFlow lanes.

    Production callers may supply all three output paths.  In that mode the
    schedule/member join and the position/pin/binding records remain indexed;
    no hop-sized schedule dictionary, coverage set, or output list is built.
    Omitting paths preserves the logical in-memory documents for legacy APIs.
    """

    if not isinstance(bank_channel_input, BankChannelInputView):
        raise ValidationError("Chimew pin planning requires a typed bank input")
    if not 1 <= region_count <= 31:
        raise ValueError("region_count must be in [1, 31]")
    if pin_plan_output_path is not None or electrical_binding_output_path is not None:
        require_native_schema("sqlite-publication-request-v1")
    publication_sources = bind_view_sources(
        (schedule, "tdm-schedule-store-v4"),
        (bank_channel_input, "chimew-bank-input-store-v1"),
    )
    problem = validate_chimew_bank_channel_input(bank_channel_input)
    qualification_validation = None
    if qualification_document is not None:
        qualification_validation = validate_chimew_qualification_binding(
            qualification_document, schedule, bank_channel_input
        )
    electrical = validate_chimew_electrical_map(
        electrical_map, platform, problem
    )
    fixed = FixedWiringChannels(platform) if electrical["fixed_wiring"] is not None else None
    if (
        qualification_validation is not None
        and qualification_validation["qualification_scope"]
        == "byte-bound-source-artifacts"
    ):
        source_binding = qualification_document.get("source_binding")
        if (
            not isinstance(source_binding, dict)
            or not isinstance(source_binding.get("digests"), dict)
            or source_binding["digests"].get("package_pins")
            != electrical["provenance"]["package_pin_inventory_sha256"]
        ):
            raise ValidationError(
                "Chimew byte-bound package-pin provenance does not agree"
            )
    if bank_channel_report_document is not None:
        if not isinstance(bank_channel_report_document, BankChannelReportView):
            raise ValidationError("Chimew pin planning requires a typed assignment report")
        if qualification_document is None:
            raise ValidationError(
                "a precomputed Chimew assignment report requires qualification"
            )
        validate_chimew_bank_channel_report_artifact(
            bank_channel_input, bank_channel_report_document
        )
        report = bank_channel_report_document
    else:
        report = evaluate_chimew_bank_channel_assignment(
            bank_channel_input,
            executable=executable,
            output_path=bank_channel_report_output_path,
        )
    try:
        if qualification_document is not None and qualification_document[
            "artifacts"
        ].get("bank_channel_report") != canonical_sha256(report):
            raise ValidationError(
                "Chimew assignment report does not match qualification certificate"
            )
        if report["provider"] != CHIMEW_BANK_CHANNEL_PROVIDER:
            raise ValidationError("Chimew assignment provider is invalid")
        if (
            schedule.get("design") != problem["design"]
            or schedule.get("platform") != platform.name
        ):
            raise ValidationError("Chimew assignment does not match schedule identity")
        publication_sources += bind_view_sources((report, "chimew-bank-report-store-v1"))
        assignment_cursor = iter(report["assignments"])

        build_connection, schedule_count = _open_chimew_build_index(schedule)
        try:
            pending_members: list[tuple[Any, ...]] = []
            pending_package_pins: list[tuple[str, str]] = []
            for group_index, group in enumerate(problem["groups"]):
                assignment = (
                    next(assignment_cursor, None)
                )
                if assignment is None or assignment.get("group") != group["id"]:
                    raise ValidationError("Chimew assignment does not cover all groups")
                channel = electrical["channels"].get(assignment["channel"])
                if channel is None:
                    raise ValidationError("Chimew assignment has no electrical channel")
                domain = problem["domains"][group["domain"]]
                group_direction = (
                    "a_to_b"
                    if group["direction"] == 0
                    else "b_to_a" if group["direction"] == 1 else "bidirectional"
                )
                if (
                    group_direction == "bidirectional"
                    and channel["direction"] != "either"
                ) or (
                    group_direction != "bidirectional"
                    and channel["direction"] not in {"either", group_direction}
                ):
                    raise ValidationError("Chimew assignment electrical direction mismatch")
                if (channel["fpga_a"], channel["fpga_b"]) != (
                    domain["fpga_a"], domain["fpga_b"]
                ):
                    raise ValidationError("Chimew assignment electrical domain mismatch")
                member_directions = set()
                for member_seq, member in enumerate(group["members"]):
                    entry_id = member["id"]
                    member_direction = (
                        "a_to_b" if member["direction"] == 0 else "b_to_a"
                    )
                    member_directions.add(member_direction)
                    source = (
                        domain["fpga_a"]
                        if member_direction == "a_to_b"
                        else domain["fpga_b"]
                    )
                    sink = (
                        domain["fpga_b"]
                        if member_direction == "a_to_b"
                        else domain["fpga_a"]
                    )
                    source_y = _normalized_y(
                        member["fanout"][1],
                        electrical["bounds"][source],
                        f"{entry_id}.source_y",
                    )
                    sink_physical_y = sum(
                        point[1] for point in member["fanins"]
                    ) / len(member["fanins"])
                    sink_y = _normalized_y(
                        sink_physical_y,
                        electrical["bounds"][sink],
                        f"{entry_id}.sink_y",
                    )
                    pending_members.append(
                        (
                            entry_id,
                            group_index,
                            member_seq,
                            member_direction,
                            channel["link"],
                            source,
                            sink,
                            channel["physical_lane"],
                            source_y,
                            sink_y,
                            min(region_count - 1, int(source_y * region_count)),
                            min(region_count - 1, int(sink_y * region_count)),
                        )
                    )
                    if len(pending_members) >= 4096:
                        try:
                            build_connection.executemany(
                                "INSERT INTO chimew_build_members VALUES "
                                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                                pending_members,
                            )
                        except sqlite3.IntegrityError as error:
                            raise ValidationError(
                                "Chimew members do not cover unique schedule entries"
                            ) from error
                        pending_members.clear()
                if (
                    group_direction == "bidirectional"
                    and member_directions != {"a_to_b", "b_to_a"}
                ) or (
                    group_direction != "bidirectional"
                    and member_directions != {group_direction}
                ):
                    raise ValidationError("Chimew group violates schedule TDM capacity")
                package_pin_keys = electrical_channel_pin_keys(channel, fixed)
                pending_package_pins.extend(package_pin_keys)
                if len(pending_package_pins) >= 4096:
                    try:
                        build_connection.executemany(
                            "INSERT INTO chimew_build_package_pins VALUES (?,?)",
                            pending_package_pins,
                        )
                    except sqlite3.IntegrityError as error:
                        raise ValidationError(
                            "selected Chimew groups collide on package pins"
                        ) from error
                    pending_package_pins.clear()
            if next(assignment_cursor, None) is not None:
                raise ValidationError("Chimew assignment does not cover all groups")
            if pending_members:
                try:
                    build_connection.executemany(
                        "INSERT INTO chimew_build_members VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        pending_members,
                    )
                except sqlite3.IntegrityError as error:
                    raise ValidationError(
                        "Chimew members do not cover unique schedule entries"
                    ) from error
            if pending_package_pins:
                try:
                    build_connection.executemany(
                        "INSERT INTO chimew_build_package_pins VALUES (?,?)",
                        pending_package_pins,
                    )
                except sqlite3.IntegrityError as error:
                    raise ValidationError(
                        "selected Chimew groups collide on package pins"
                    ) from error

            member_count, joined_count = build_connection.execute(
                """
                SELECT count(*),count(schedule.identity)
                FROM chimew_build_members AS member
                LEFT JOIN indexed_schedule_entries AS schedule
                  ON schedule.identity=member.entry_id
                """
            ).fetchone()
            member_count = int(member_count)
            package_pin_count = int(
                build_connection.execute(
                    "SELECT count(*) FROM chimew_build_package_pins"
                ).fetchone()[0]
            )
            if member_count != schedule_count or joined_count != schedule_count:
                raise ValidationError("Chimew groups do not cover the schedule exactly")
            mismatch = build_connection.execute(
                """
                SELECT member.entry_id
                FROM chimew_build_members AS member
                JOIN indexed_schedule_entries AS schedule
                  ON schedule.identity=member.entry_id
                WHERE schedule.link_id!=member.expected_link
                   OR schedule.source_fpga!=member.expected_source
                   OR schedule.sink_fpga!=member.expected_sink
                LIMIT 1
                """
            ).fetchone()
            if mismatch is not None:
                raise ValidationError("Chimew member schedule domain does not agree")
            collision = build_connection.execute(
                """
                SELECT member.group_index,member.entry_id,schedule.slot
                FROM chimew_build_members AS member
                JOIN indexed_schedule_entries AS schedule
                  ON schedule.identity=member.entry_id
                JOIN (
                  SELECT member.group_index,schedule.slot
                  FROM chimew_build_members AS member
                  JOIN indexed_schedule_entries AS schedule
                    ON schedule.identity=member.entry_id
                  GROUP BY member.group_index,schedule.slot HAVING count(*)>1
                ) AS duplicate
                  ON duplicate.group_index=member.group_index
                 AND duplicate.slot=schedule.slot
                ORDER BY member.group_index,member.member_seq LIMIT 1
                """
            ).fetchone()
            if collision is not None:
                raise ValidationError(
                    "Chimew group has a TDM slot collision: "
                    f"group_index={collision[0]!r}, entry={collision[1]!r}, "
                    f"slot={collision[2]!r}"
                )
            invalid_capacity = build_connection.execute(
                """
                SELECT member.group_index
                FROM chimew_build_members AS member
                JOIN indexed_schedule_entries AS schedule
                  ON schedule.identity=member.entry_id
                GROUP BY member.group_index,member.member_direction
                HAVING min(schedule.tdm_ratio)!=max(schedule.tdm_ratio)
                    OR count(*)>min(schedule.tdm_ratio)
                LIMIT 1
                """
            ).fetchone()
            if invalid_capacity is not None:
                raise ValidationError("Chimew group violates schedule TDM capacity")

            position_header = {
                "schema": SIGNAL_POSITION_HINTS_SCHEMA,
                "design": schedule["design"],
                "platform": platform.name,
                "provider": CHIMEW_PHYSICAL_POSITION_PROVIDER,
                "source_coordinate_system": "physical-site-xy",
                "region_count": region_count,
                "provenance": {
                    "producer": problem["provenance"]["producer"],
                    "producer_version": problem["provenance"]["producer_version"],
                    "assignment_input_sha256": report["input_sha256"],
                    "placement_sha256": problem["provenance"]["placement_sha256"],
                    "architecture_sha256": problem["provenance"]["architecture_sha256"],
                },
                "metrics": {
                    "signals": member_count,
                    "endpoint_centroid_fallbacks": 0,
                },
            }
            positions_document = {
                **position_header,
                "entries": _iter_chimew_position_records(build_connection),
            }
            positions = (
                write_position_hints_storage(
                    position_hints_output_path,
                    positions_document,
                    source_bindings=publication_sources,
                )
                if position_hints_output_path is not None
                else {
                    **position_header,
                    "entries": list(_iter_chimew_position_records(build_connection)),
                }
            )
            weights = {"crossing": 1.0, "position": 1.0}
            schedule_sha256 = canonical_tdm_schedule_sha256(schedule)
            metrics = _indexed_assignment_metrics(
                build_connection, platform, baseline=False
            )
            baseline = _indexed_assignment_metrics(
                build_connection, platform, baseline=True
            )
            plan_records = _IndexedPinPlanRecords(build_connection, member_count)
            plan_document = {
                "schema": PIN_PLAN_SCHEMA,
                "design": schedule["design"],
                "platform": platform.name,
                "provider": CHIMEW_PIN_PLAN_PROVIDER,
                "configuration": {
                    "refinement_iterations": 0,
                    "paper_assignment_provider": report["provider"],
                    "electrical_map_sha256": electrical["map_sha256"],
                    "schedule_sha256": schedule_sha256,
                    "lookahead_qualification": (
                        "complete-artifact-chain"
                        if qualification_validation is not None
                        else "bank-electrical-only"
                    ),
                    **(
                        {
                            "qualification_sha256": qualification_validation[
                                "qualification_sha256"
                            ],
                            "qualification_scope": qualification_validation[
                                "qualification_scope"
                            ],
                        }
                        if qualification_validation is not None
                        else {}
                    ),
                },
                "weights": weights,
                "metrics": {
                    "signals": member_count,
                    "groups": len(problem["groups"]),
                    **metrics,
                    **{
                        f"baseline_logical_lane_{field}": value
                        for field, value in baseline.items()
                    },
                },
                "entries": plan_records,
            }
            plan = (
                write_pin_plan_storage(
                    pin_plan_output_path, plan_document, source_bindings=publication_sources
                )
                if pin_plan_output_path is not None
                else {**plan_document, "entries": list(plan_records)}
            )
            validation = validate_pin_plan(schedule, platform, positions, plan)

            def iter_binding_entries() -> Iterator[Mapping[str, Any]]:
                # Both source relations have independent ordered cursors.
                groups = bank_channel_input.iter_normalized_groups_by_id()
                assignments = report.iter_assignments_by_group()
                for group, assignment in zip(groups, assignments):
                    if assignment.get("group") != group["id"]:
                        raise ValidationError(
                            "Chimew assignment does not cover all groups"
                        )
                    channel = electrical["channels"][assignment["channel"]]
                    yield {
                        "group": group["id"],
                        "schedule_entries": sorted(
                            member["id"] for member in group["members"]
                        ),
                        "bank_pair": assignment["bank_pair"],
                        "channel": assignment["channel"],
                        **channel,
                        "direction": (
                            "a_to_b"
                            if group["direction"] == 0
                            else "b_to_a"
                            if group["direction"] == 1
                            else "bidirectional"
                        ),
                    }

            binding_document = {
                "schema": (FIXED_WIRING_BINDING_SCHEMA if electrical["fixed_wiring"] is not None
                           else CHIMEW_PHASE6_BINDING_SCHEMA),
                **({"fixed_wiring": electrical["fixed_wiring"]} if electrical["fixed_wiring"] is not None else {}),
                "status": "pass",
                "integration_status": "phase6-pin-plan",
                "design": schedule["design"],
                "platform": platform.name,
                "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
                "paper_provider": report["provider"],
                "extension_scope": (
                    "EmuFlow concrete lane, bank voltage, IOSTANDARD, and package-pin legality"
                ),
                "fpga_y_bounds": [
                    {"fpga": fpga, "y_min": bounds[0], "y_max": bounds[1]}
                    for fpga, bounds in sorted(electrical["bounds"].items())
                ],
                "provenance": {
                    **electrical["provenance"],
                    "electrical_map_sha256": electrical["map_sha256"],
                    "assignment_input_sha256": report["input_sha256"],
                    "schedule_sha256": schedule_sha256,
                    **(
                        {
                            "qualification_sha256": qualification_validation[
                                "qualification_sha256"
                            ],
                            "qualification_scope": qualification_validation[
                                "qualification_scope"
                            ],
                        }
                        if qualification_validation is not None
                        else {}
                    ),
                },
                **(
                    {"lookahead_qualification": dict(qualification_document)}
                    if qualification_document is not None
                    else {}
                ),
                "metrics": {
                    "signals": member_count,
                    "groups": len(problem["groups"]),
                    "channels": len(problem["groups"]),
                    "package_pins": package_pin_count,
                    "lane_slot_collisions": validation["physical_lane_slot_collisions"],
                    "package_pin_collisions": 0,
                },
                "entries": iter_binding_entries(),
            }
            binding = (
                write_chimew_binding_storage(
                    electrical_binding_output_path,
                    binding_document,
                    source_bindings=publication_sources
                    + bind_view_sources((plan, "pin-plan-store-v1")),
                )
                if electrical_binding_output_path is not None
                else {**binding_document, "entries": list(iter_binding_entries())}
            )
            # Independently rejoin all three sealed typed relations before return.
            validate_chimew_phase6_binding(schedule, platform, plan, binding)
            return {
                "status": "pass",
                "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
                "bank_channel_report": report,
                "position_hints": positions,
                "pin_plan": plan,
                "electrical_binding": binding,
                "qualification_validation": qualification_validation,
                "validation": validation,
            }
        finally:
            try:
                assignment_cursor.close()
            finally:
                build_connection.close()
    except BaseException:
        if bank_channel_report_document is None:
            report.close()
        raise


def _validate_chimew_phase6_binding(
    schedule: Mapping[str, Any],
    platform: Platform,
    pin_plan: Mapping[str, Any],
    binding: Mapping[str, Any],
    connection_owner: list[sqlite3.Connection],
) -> Dict[str, Any]:
    """Independently bind a Chimew pin plan to its electrical certificate."""

    binding_schema = binding.get("schema")
    if binding_schema not in {
        CHIMEW_PHASE6_BINDING_SCHEMA_V1,
        CHIMEW_PHASE6_BINDING_SCHEMA,
        FIXED_WIRING_BINDING_SCHEMA,
    }:
        raise ValidationError("Chimew Phase 6 electrical binding schema is invalid")
    fixed = FixedWiringChannels(platform) if binding_schema == FIXED_WIRING_BINDING_SCHEMA else None
    if platform.virtual_board_model is not None and fixed is None:
        raise ValidationError("fixed LVDS BoardDB requires a differential Chimew binding")
    if fixed is not None:
        fixed.validate_source(binding.get("fixed_wiring"))
    if (
        binding.get("status") != "pass"
        or binding.get("integration_status") != "phase6-pin-plan"
        or binding.get("provider") != CHIMEW_PHASE6_BINDING_PROVIDER
        or pin_plan.get("provider") != CHIMEW_PIN_PLAN_PROVIDER
    ):
        raise ValidationError("Chimew Phase 6 electrical binding provider is invalid")
    if (
        binding.get("design") != schedule.get("design")
        or binding.get("platform") != platform.name
        or pin_plan.get("design") != schedule.get("design")
        or pin_plan.get("platform") != platform.name
    ):
        raise ValidationError("Chimew Phase 6 electrical binding identity differs")
    provenance = binding.get("provenance")
    configuration = pin_plan.get("configuration")
    if not isinstance(provenance, dict) or not isinstance(configuration, dict):
        raise ValidationError("Chimew Phase 6 electrical provenance is missing")
    _string(binding.get("paper_provider"), "electrical_binding.paper_provider")
    _string(provenance.get("producer"), "electrical_binding.producer")
    _string(
        provenance.get("producer_version"),
        "electrical_binding.producer_version",
    )
    for field in (
        "boarddb_sha256",
        "package_pin_inventory_sha256",
        "electrical_map_sha256",
        "assignment_input_sha256",
        "schedule_sha256",
    ):
        _digest(provenance.get(field), f"electrical_binding.{field}")
    schedule_sha256 = canonical_tdm_schedule_sha256(schedule)
    if (
        provenance.get("schedule_sha256") != schedule_sha256
        or configuration.get("schedule_sha256") != schedule_sha256
        or provenance.get("electrical_map_sha256")
        != configuration.get("electrical_map_sha256")
    ):
        raise ValidationError("Chimew Phase 6 electrical provenance does not agree")
    qualification_status = configuration.get(
        "lookahead_qualification", "bank-electrical-only"
    )
    if qualification_status == "complete-artifact-chain":
        qualification_sha = _digest(
            configuration.get("qualification_sha256"),
            "pin_plan.qualification_sha256",
        )
        if provenance.get("qualification_sha256") != qualification_sha:
            raise ValidationError("Chimew qualification binding does not agree")
        qualification = binding.get("lookahead_qualification")
        if not isinstance(qualification, dict):
            raise ValidationError("Chimew qualification certificate is missing")
        qualification_validation = validate_chimew_qualification_seal(
            qualification, schedule
        )
        qualification_scope = configuration.get("qualification_scope")
        provenance_scope = provenance.get("qualification_scope")
        expected_scope = qualification_validation["qualification_scope"]
        legacy_scope_omitted = (
            expected_scope == "declared-digest-artifact-chain"
            and qualification_scope is None
            and provenance_scope is None
        )
        if qualification_validation["qualification_sha256"] != qualification_sha or (
            not legacy_scope_omitted
            and (
                qualification_scope != expected_scope
                or provenance_scope != expected_scope
            )
        ):
            raise ValidationError("embedded Chimew qualification does not agree")
        if expected_scope == "byte-bound-source-artifacts":
            source_binding = qualification.get("source_binding")
            if (
                not isinstance(source_binding, dict)
                or not isinstance(source_binding.get("digests"), dict)
                or source_binding["digests"].get("package_pins")
                != provenance.get("package_pin_inventory_sha256")
            ):
                raise ValidationError(
                    "Chimew byte-bound package-pin provenance does not agree"
                )
    elif qualification_status == "bank-electrical-only":
        if (
            "qualification_sha256" in configuration
            or "qualification_sha256" in provenance
            or "qualification_scope" in configuration
            or "qualification_scope" in provenance
            or "lookahead_qualification" in binding
        ):
            raise ValidationError("partial Chimew binding contains a qualification seal")
    else:
        raise ValidationError("Chimew lookahead qualification status is invalid")

    indexed_connection = None
    indexed_schedule_count = 0
    schedule_by_id = None
    plan_by_id = None
    if _indexed_chimew_validation_available(schedule, pin_plan):
        indexed_connection, indexed_schedule_count = (
            _open_indexed_chimew_validation(
                schedule, pin_plan, platform, binding
            )
        )
        connection_owner.append(indexed_connection)
    else:
        schedule_by_id = {
            entry["id"]: entry for entry in schedule.get("entries", [])
        }
        plan_by_id = {
            entry["schedule_entry"]: entry
            for entry in pin_plan.get("entries", [])
        }
        if (
            len(schedule_by_id) != len(schedule.get("entries", []))
            or set(plan_by_id) != set(schedule_by_id)
        ):
            raise ValidationError(
                "Chimew Phase 6 electrical schedule coverage is invalid"
            )
        validate_bank_pin_assignment(
            schedule,
            platform,
            {
                entry_id: item.get("physical_lane")
                for entry_id, item in plan_by_id.items()
            },
        )
    indexed_binding = isinstance(binding, ChimewBindingView)
    raw_entries = (
        binding.iter_group_records()
        if indexed_binding and indexed_connection is not None
        else binding.get("entries")
    )
    if not (
        isinstance(raw_entries, Sequence)
        and not isinstance(raw_entries, (str, bytes))
    ) and not (indexed_binding and indexed_connection is not None):
        raise ValidationError("Chimew Phase 6 electrical entries are missing")
    raw_entry_count = len(binding["entries"])
    covered = set()
    pins = set()
    lanes = set()
    physical_lane_uses: Dict[Tuple[str, int], set[str]] = {}
    groups = set()
    link_by_id = {link.id: link for link in platform.links}
    binding_bounds = {}
    if binding_schema in {CHIMEW_PHASE6_BINDING_SCHEMA, FIXED_WIRING_BINDING_SCHEMA}:
        raw_bounds = binding.get("fpga_y_bounds")
        if not isinstance(raw_bounds, list):
            raise ValidationError("Chimew electrical binding FPGA bounds are missing")
        for item in raw_bounds:
            if not isinstance(item, dict):
                raise ValidationError("Chimew electrical binding FPGA bounds are invalid")
            fpga = _string(item.get("fpga"), "electrical_binding.bounds.fpga")
            low = _number(item.get("y_min"), f"electrical_binding.bounds.{fpga}.low")
            high = _number(item.get("y_max"), f"electrical_binding.bounds.{fpga}.high")
            if fpga in binding_bounds or high <= low:
                raise ValidationError("Chimew electrical binding FPGA bounds are invalid")
            binding_bounds[fpga] = (low, high)
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"Chimew electrical binding entry {index} is invalid")
        group = _string(record.get("group"), f"electrical_binding[{index}].group")
        if group in groups:
            raise ValidationError("Chimew electrical binding duplicates a group")
        groups.add(group)
        members = record.get("schedule_entries")
        if indexed_binding and indexed_connection is not None:
            members = None
        elif not isinstance(members, list) or not members or not all(
            isinstance(member, str) and member for member in members
        ):
            raise ValidationError("Chimew electrical binding group is empty")
        if indexed_connection is None:
            if len(set(members)) != len(members) or any(
                member in covered for member in members
            ):
                raise ValidationError(
                    "Chimew electrical binding duplicates schedule entries"
                )
        link = _string(record.get("link"), f"electrical_binding[{index}].link")
        fpga_a = _string(record.get("fpga_a"), f"electrical_binding[{index}].fpga_a")
        fpga_b = _string(record.get("fpga_b"), f"electrical_binding[{index}].fpga_b")
        platform_link = link_by_id.get(link)
        if platform_link is None or set(platform_link.endpoints) != {fpga_a, fpga_b}:
            raise ValidationError("Chimew electrical binding link identity is invalid")
        physical_lane = record.get("physical_lane")
        if (
            isinstance(physical_lane, bool)
            or not isinstance(physical_lane, int)
            or not 0
            <= physical_lane
            < platform_link.transport_bits_per_cycle_per_direction
        ):
            raise ValidationError("Chimew electrical binding physical lane is invalid")
        direction = record.get("direction")
        if direction not in {"a_to_b", "b_to_a", "bidirectional"}:
            raise ValidationError("Chimew electrical binding direction is invalid")
        if direction == "bidirectional" and platform_link.bank_count is not None:
            raise ValidationError("a shared HP-bank group must have one direction for the schedule")
        if direction == "bidirectional" and (
            platform_link.direction != "full_duplex"
            or platform_link.capacity_sharing != "shared_bidirectional"
        ):
            raise ValidationError(
                "a bidirectional Chimew bundle requires shared full-duplex capacity"
            )
        lane_key = (link, direction, physical_lane)
        if lane_key in lanes:
            raise ValidationError("Chimew electrical binding reuses a concrete lane")
        physical_key = (link, physical_lane)
        existing_directions = physical_lane_uses.setdefault(physical_key, set())
        if existing_directions and (
            direction == "bidirectional"
            or "bidirectional" in existing_directions
            or platform_link.direction != "full_duplex"
            or platform_link.capacity_sharing != "per_direction"
        ):
            raise ValidationError("Chimew electrical binding reuses a concrete lane")
        existing_directions.add(direction)
        lanes.add(lane_key)
        if indexed_connection is not None and not indexed_binding:
            _stage_indexed_chimew_members(
                indexed_connection,
                group_index=index,
                members=members,
                direction=direction,
                link=link,
                fpga_a=fpga_a,
                fpga_b=fpga_b,
                physical_lane=physical_lane,
            )
        elif indexed_connection is None:
            member_directions = set()
            slots = set()
            ratios_by_direction = {"a_to_b": set(), "b_to_a": set()}
            count_by_direction = {"a_to_b": 0, "b_to_a": 0}
            for member in members:
                scheduled = schedule_by_id.get(member)
                planned = plan_by_id.get(member)
                if scheduled is None:
                    raise ValidationError(
                        "Chimew electrical binding conflicts with pin plan"
                    )
                if (scheduled.get("from"), scheduled.get("to")) == (
                    fpga_a,
                    fpga_b,
                ):
                    member_direction = "a_to_b"
                elif (scheduled.get("from"), scheduled.get("to")) == (
                    fpga_b,
                    fpga_a,
                ):
                    member_direction = "b_to_a"
                else:
                    raise ValidationError(
                        "Chimew electrical binding direction is invalid"
                    )
                ratio = _tdm_ratio(scheduled)
                slot = scheduled.get("slot")
                if (
                    planned is None
                    or scheduled.get("link") != link
                    or planned.get("physical_lane") != physical_lane
                    or isinstance(slot, bool)
                    or not isinstance(slot, int)
                    or slot < 0
                    or slot in slots
                ):
                    raise ValidationError(
                        "Chimew electrical binding conflicts with pin plan"
                    )
                member_directions.add(member_direction)
                ratios_by_direction[member_direction].add(ratio)
                count_by_direction[member_direction] += 1
                slots.add(slot)
            if (
                any(
                    len(ratios_by_direction[member_direction]) != 1
                    or count_by_direction[member_direction]
                    > next(iter(ratios_by_direction[member_direction]))
                    for member_direction in member_directions
                )
                or (
                    direction == "bidirectional"
                    and member_directions != {"a_to_b", "b_to_a"}
                )
                or (
                    direction != "bidirectional"
                    and member_directions != {direction}
                )
            ):
                raise ValidationError(
                    "Chimew electrical binding TDM bundle is invalid"
                )
        _string(record.get("package_pin_a"), "package_pin_a")
        _string(record.get("package_pin_b"), "package_pin_b")
        if fixed is not None:
            fixed.validate_channel(record, fpga_a, fpga_b)
        pin_keys = electrical_channel_pin_keys(record, fixed)
        if any(key in pins for key in pin_keys):
            raise ValidationError("Chimew electrical binding reuses a package pin")
        pins.update(pin_keys)
        _string(record.get("bank_pair"), "electrical_binding.bank_pair")
        _string(record.get("channel"), "electrical_binding.channel")
        _string(record.get("bank_a"), "electrical_binding.bank_a")
        _string(record.get("bank_b"), "electrical_binding.bank_b")
        iostandard = _string(
            record.get("iostandard"), "electrical_binding.iostandard"
        )
        supported = record.get("supported_iostandards")
        if (
            not isinstance(supported, list)
            or iostandard not in supported
            or len(supported) != len(set(supported))
        ):
            raise ValidationError("Chimew electrical binding IOSTANDARD is invalid")
        voltage = _number(
            record.get("bank_voltage"), "electrical_binding.bank_voltage"
        )
        if (
            (fixed is None and record.get("electrical_class") != "single_ended_parallel")
            or (fixed is None and iostandard not in _IOSTANDARD_VOLTAGES)
            or abs(voltage - (1.8 if fixed is not None else _IOSTANDARD_VOLTAGES[iostandard])) > 1.0e-9
        ):
            raise ValidationError("Chimew electrical binding voltage is invalid")
        if binding_schema in {CHIMEW_PHASE6_BINDING_SCHEMA, FIXED_WIRING_BINDING_SCHEMA}:
            for endpoint, fpga in (("a", fpga_a), ("b", fpga_b)):
                point = record.get(f"pin_{endpoint}_point")
                if not isinstance(point, dict) or fpga not in binding_bounds:
                    raise ValidationError(
                        "Chimew electrical binding physical pin point is missing"
                    )
                _number(point.get("x"), f"pin_{endpoint}_point.x")
                y = _number(point.get("y"), f"pin_{endpoint}_point.y")
                low, high = binding_bounds[fpga]
                if y < low - 1.0e-9 or y > high + 1.0e-9:
                    raise ValidationError(
                        "Chimew electrical binding pin lies outside FPGA bounds"
                    )
        if not isinstance(record.get("placement_anchor", True), bool):
            raise ValidationError("Chimew electrical placement anchor is invalid")
        if indexed_connection is None:
            covered.update(members)
    if indexed_connection is not None:
        _validate_indexed_chimew_members(
            indexed_connection, indexed_schedule_count
        )
        signal_count = indexed_schedule_count
    else:
        if covered != set(schedule_by_id):
            raise ValidationError(
                "Chimew electrical binding coverage is incomplete"
            )
        signal_count = len(covered)
    metrics = binding.get("metrics")
    expected = {
        "signals": signal_count,
        "groups": len(groups),
        "channels": raw_entry_count,
        "package_pins": len(pins),
        "lane_slot_collisions": 0,
        "package_pin_collisions": 0,
    }
    if not isinstance(metrics, dict) or any(
        metrics.get(key) != value for key, value in expected.items()
    ):
        raise ValidationError("Chimew Phase 6 electrical metrics do not agree")
    return {
        "status": "pass",
        "signals": signal_count,
        "groups": len(groups),
        "concrete_lanes": len(lanes),
        "package_pins": len(pins),
        "binding_sha256": (
            binding.canonical_sha256
            if isinstance(binding, ChimewBindingView)
            else _canonical_sha256(binding)
        ),
    }


def validate_chimew_phase6_binding(
    schedule: Mapping[str, Any],
    platform: Platform,
    pin_plan: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate the electrical certificate and close every indexed scratch DB."""

    connections: list[sqlite3.Connection] = []
    try:
        return _validate_chimew_phase6_binding(
            schedule,
            platform,
            pin_plan,
            binding,
            connections,
        )
    finally:
        for connection in reversed(connections):
            connection.close()


def _run_chimew_phase6_adapter_with_input(
    schedule_path: Path,
    platform_path: Path,
    assignment_input: Mapping[str, Any],
    electrical_map_path: Path,
    output_dir: Path,
    *,
    qualification_path: Optional[Path] = None,
    bank_channel_report_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
    defer_generation_gc: bool = False,
) -> Dict[str, Any]:
    """Publish one crash-safe, report-committed Phase 6 adapter bundle."""

    schedule = read_tdm_schedule(schedule_path)
    electrical_map = read_json(electrical_map_path)
    qualification = read_json(qualification_path) if qualification_path else None
    assignment_report = (
        read_chimew_bank_channel_report(
            bank_channel_report_path
        )
        if bank_channel_report_path
        else None
    )
    platform_sha256 = hashlib.sha256(platform_path.read_bytes()).hexdigest()
    provenance = electrical_map.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("boarddb_sha256") != platform_sha256
    ):
        raise ValidationError(
            "Chimew electrical map BoardDB SHA-256 does not match platform"
        )
    platform = Platform.load(platform_path)
    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.adapter-",
            suffix=".tmp",
            dir=output_dir.parent,
        )
    )
    installed: list[Path] = []
    report_committed = False
    previous_report: Mapping[str, Any] | None = None
    previous_names: tuple[Any, ...] = ()
    previous_report_path = output_dir / "adapter_report.json"
    if previous_report_path.is_file():
        try:
            candidate = read_json(previous_report_path)
            if isinstance(candidate.get("artifacts"), dict):
                previous_report = candidate
        except (OSError, ValueError):
            previous_report = None
    if not defer_generation_gc:
        previous_names = (
            (
                *previous_report["artifacts"].values(),
                chimew_adapter_report_generation_path(
                    output_dir, previous_report
                ).name,
            )
            if previous_report is not None
            else ()
        )
        garbage_collect_chimew_adapter_generations(
            output_dir,
            (*previous_names, "adapter_report.json"),
        )
    indexed_bank_report: BankChannelReportView | None = None
    result_bank_report: Mapping[str, Any] | None = None
    try:
        result = build_chimew_phase6_pin_plan(
            schedule,
            platform,
            assignment_input,
            electrical_map,
            qualification_document=qualification,
            bank_channel_report_document=assignment_report,
            bank_channel_report_output_path=(
                staging_root / "bank_channel_report.json"
            ),
            executable=executable,
            region_count=region_count,
            electrical_binding_output_path=(
                staging_root / "electrical_binding.json"
            ),
            position_hints_output_path=(staging_root / "position_hints.json"),
            pin_plan_output_path=(staging_root / "pin_plan.json"),
        )
        indexed_binding = result["electrical_binding"]
        indexed_positions = result["position_hints"]
        indexed_plan = result["pin_plan"]
        if not isinstance(indexed_binding, ChimewBindingView) or not all(
            isinstance(getattr(value, "store_path", None), Path)
            for value in (indexed_positions, indexed_plan)
        ):
            raise ValidationError(
                "Chimew adapter did not publish indexed artifacts"
            )

        report_selector_path = staging_root / "bank_channel_report.json"
        result_bank_report = result["bank_channel_report"]
        if (
            isinstance(result_bank_report, BankChannelReportView)
            and result_bank_report.manifest_path == report_selector_path.resolve()
        ):
            indexed_bank_report = result_bank_report
        else:
            if not isinstance(result_bank_report, BankChannelReportView):
                raise ValidationError("Chimew adapter requires a typed assignment report")
            copy_chimew_bank_channel_report_storage(result_bank_report.manifest_path, report_selector_path)
            indexed_bank_report = read_chimew_bank_channel_report(report_selector_path)
        if qualification is not None:
            write_json(staging_root / "qualification_certificate.json", qualification)

        sources: Dict[str, Path] = {
            "bank_channel_report": staging_root / "bank_channel_report.json",
            "bank_channel_report_store": indexed_bank_report.store_path,
            "position_hints": indexed_positions.manifest_path,
            "position_hints_store": indexed_positions.store_path,
            "pin_plan": indexed_plan.manifest_path,
            "pin_plan_store": indexed_plan.store_path,
            "electrical_binding": indexed_binding.manifest_path,
            "electrical_binding_store": indexed_binding.store_path,
        }
        if qualification is not None:
            sources["qualification_certificate"] = (
                staging_root / "qualification_certificate.json"
            )

        sidecar_labels = {
            "bank_channel_report_store",
            "position_hints_store",
            "pin_plan_store",
            "electrical_binding_store",
        }
        artifact_names: Dict[str, str] = {}
        for label, source in sources.items():
            digest = _file_sha256(source)
            artifact_names[label] = (
                source.name
                if label in sidecar_labels
                else f"{source.stem}.{digest}{source.suffix}"
            )

        report = {
            "schema": CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
            "status": "pass",
            "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
            "design": result["pin_plan"]["design"],
            "platform": result["pin_plan"]["platform"],
            "paper_provider": result["bank_channel_report"]["provider"],
            "validation": result["validation"],
            "electrical_metrics": result["electrical_binding"]["metrics"],
            "lookahead_qualification": (
                result["pin_plan"]["configuration"]["lookahead_qualification"]
            ),
            "qualification_validation": result["qualification_validation"],
            "artifacts": artifact_names,
        }
        staged_report = staging_root / "adapter_report.json"
        write_json(staged_report, report)

        output_dir.mkdir(parents=True, exist_ok=True)

        # Sidecars and selectors are immutable, content-addressed generations.
        # The adapter report is published after every referenced artifact.
        publish_order = [
            *sorted(sidecar_labels & sources.keys()),
            *sorted(set(sources) - sidecar_labels),
        ]
        for label in publish_order:
            source = sources[label]
            _fsync_file(source)
            destination = output_dir / artifact_names[label]
            if destination.is_file():
                if _file_sha256(destination) != _file_sha256(source):
                    raise ValidationError(
                        "Chimew adapter content-addressed artifact collision"
                    )
                source.unlink()
                continue
            _publish_chimew_adapter_file(source, destination)
            installed.append(destination)
        _fsync_directory(output_dir)

        # Reopen from final paths before committing the report.  This checks
        # relocation, all store seals and the independent schedule/pin/binding
        # joins while the previous report still names the previous generation.
        final_positions = read_position_hints(
            output_dir / artifact_names["position_hints"]
        )
        final_plan = read_pin_plan(output_dir / artifact_names["pin_plan"])
        final_binding = read_chimew_binding(
            output_dir / artifact_names["electrical_binding"]
        )
        final_validation = validate_pin_plan(
            schedule, platform, final_positions, final_plan
        )
        if final_validation != result["validation"]:
            raise ValidationError(
                "relocated Chimew pin-plan validation differs"
            )
        validate_chimew_phase6_binding(
            schedule, platform, final_plan, final_binding
        )

        _fsync_file(staged_report)
        report_destination = chimew_adapter_report_generation_path(
            output_dir, report
        )
        staged_fixed_report = staging_root / "adapter_report.fixed.json"
        if not defer_generation_gc:
            shutil.copy2(staged_report, staged_fixed_report)
            _fsync_file(staged_fixed_report)
        if report_destination.is_file():
            if _file_sha256(report_destination) != _file_sha256(staged_report):
                raise ValidationError(
                    "Chimew adapter report-generation collision"
                )
            staged_report.unlink()
        else:
            _publish_chimew_adapter_file(staged_report, report_destination)
            installed.append(report_destination)
        if not defer_generation_gc:
            _publish_chimew_adapter_file(
                staged_fixed_report, previous_report_path
            )
        report_committed = True
        _fsync_directory(output_dir)

        # Standalone adapters own their report commit and may collect every
        # older generation.  Pipeline-managed adapters defer collection until
        # the outer pipeline_report has committed its matching inventory.
        if not defer_generation_gc:
            garbage_collect_chimew_adapter_generations(
                output_dir,
                (
                    *artifact_names.values(),
                    report_destination.name,
                    "adapter_report.json",
                ),
                additionally_retired_names=previous_names,
            )
        return report
    except BaseException:
        if not report_committed:
            for path in reversed(installed):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        if isinstance(indexed_bank_report, BankChannelReportView):
            indexed_bank_report.close()
        if (
            isinstance(result_bank_report, BankChannelReportView)
            and result_bank_report is not indexed_bank_report
        ):
            result_bank_report.close()
        if isinstance(assignment_report, BankChannelReportView):
            assignment_report.close()
        shutil.rmtree(staging_root, ignore_errors=True)


def run_chimew_phase6_adapter(
    schedule_path: Path,
    platform_path: Path,
    bank_channel_input_path: Path,
    electrical_map_path: Path,
    output_dir: Path,
    *,
    qualification_path: Optional[Path] = None,
    bank_channel_report_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
    defer_generation_gc: bool = False,
) -> Dict[str, Any]:
    """Lease typed bank/group storage through the adapter's final cursor."""

    assignment_input = read_chimew_bank_channel_input(bank_channel_input_path)
    if not hasattr(assignment_input, "__enter__"):
        raise ValidationError("Phase 6 production requires indexed bank storage")
    with assignment_input:
        return _run_chimew_phase6_adapter_with_input(
            schedule_path,
            platform_path,
            assignment_input,
            electrical_map_path,
            output_dir,
            qualification_path=qualification_path,
            bank_channel_report_path=bank_channel_report_path,
            executable=executable,
            region_count=region_count,
            defer_generation_gc=defer_generation_gc,
        )
