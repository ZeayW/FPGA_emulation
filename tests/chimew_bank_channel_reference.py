"""Test-only prior wire-protocol producer and independent certificate oracle."""
from __future__ import annotations

import io
import math
import sqlite3
import struct
import subprocess
import tempfile
import zlib
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from emuflow.common.errors import EmuFlowError, ValidationError
from tests.native_build import chimew_bank_oracle, emuflow_native


from emuflow_tools.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1, CHIMEW_BANK_CHANNEL_INPUT_SCHEMA, CHIMEW_BANK_CHANNEL_INPUT_PROVIDER, CHIMEW_BANK_CHANNEL_REPORT_SCHEMA, CHIMEW_BANK_CHANNEL_PROVIDER, CHIMEW_BANK_CHANNEL_STATUS, CHIMEW_BANK_CHANNEL_INTEGRATION_STATUS, CHIMEW_BANK_CHANNEL_PAPER_SCOPE, CHIMEW_BANK_CHANNEL_ALGORITHM2_INTERPRETATION, CHIMEW_BANK_CHANNEL_NUMERIC_POLICY, _string, _integer, _number, _digest, _point, _normalize_chimew_group, _NormalizedDocumentGroups, _normalize_chimew_bank, _normalize_chimew_problem_header, validate_chimew_bank_channel_input, _distance, _raw_cost, _ranked_cost, _candidate_cost

_TYPED_INPUT_MAGIC = b"EMCHBC3\0"
_TYPED_INPUT_END_MAGIC = b"EMCHEND\0"
_TYPED_INPUT_VERSION = 1
_TYPED_INPUT_ENDIAN_MARKER = 0x01020304


def _serialize_typed(
    problem: Mapping[str, Any], stream: Any, logical_sha256: str
) -> Mapping[str, int]:
    """Stream the sealed v1 native protocol without a graph-sized text file.

    The logical digest binds both ends of the stream to the already validated
    input artifact.  A stream checksum and exact counts catch truncation,
    concatenation and mixed-generation header/trailer pairs before the kernel
    starts either matching stage.
    """

    if (
        not isinstance(logical_sha256, str)
        or len(logical_sha256) != 64
        or any(character not in "0123456789abcdef" for character in logical_sha256)
    ):
        raise ValidationError("Chimew typed input logical digest is invalid")
    metrics = problem["metrics"]
    counts = tuple(
        _integer(metrics.get(field), f"chimew.assignment.metrics.{field}")
        for field in ("bank_pairs", "channels", "groups", "signals", "fanins")
    )
    digest = bytes.fromhex(logical_sha256)
    header = struct.pack(
        "<8sIIQQQQQd32s",
        _TYPED_INPUT_MAGIC,
        _TYPED_INPUT_VERSION,
        _TYPED_INPUT_ENDIAN_MARKER,
        *counts,
        float(problem["cost_scale"]),
        digest,
    )
    stream.write(header)
    # The checksum seals the complete semantic header (including protocol
    # identity, counts, cost scale and source digest) followed by every record.
    # The trailer carrying the checksum is intentionally outside that scope.
    checksum = zlib.crc32(header)
    observed_banks = observed_channels = 0
    observed_groups = observed_members = observed_fanins = 0

    def emit(fmt: str, *values: Any) -> None:
        nonlocal checksum
        payload = struct.pack(fmt, *values)
        stream.write(payload)
        checksum = zlib.crc32(payload, checksum)

    for bank_index, bank in enumerate(problem["banks"]):
        point_a = bank["bank_a"]["point"]
        point_b = bank["bank_b"]["point"]
        channels = bank["channels"]
        emit(
            "<Bii4dQ",
            ord("B"),
            bank_index,
            bank["domain"],
            point_a[0],
            point_a[1],
            point_b[0],
            point_b[1],
            len(channels),
        )
        observed_banks += 1
        for channel in channels:
            pin_a, pin_b = channel["pin_a"], channel["pin_b"]
            emit(
                "<Biii4d",
                ord("C"),
                channel["index"],
                bank_index,
                channel["order"],
                pin_a[0],
                pin_a[1],
                pin_b[0],
                pin_b[1],
            )
            observed_channels += 1
    for group_index, group in enumerate(problem["groups"]):
        members = group["members"]
        emit(
            "<BiiiiQ",
            ord("G"),
            group_index,
            group["domain"],
            group["kind"],
            group["direction"],
            len(members),
        )
        observed_groups += 1
        for member_index, member in enumerate(members):
            fanout = member["fanout"]
            fanins = member["fanins"]
            emit(
                "<Bii3dQ",
                ord("M"),
                member_index,
                member["direction"],
                member["timing_weight"],
                fanout[0],
                fanout[1],
                len(fanins),
            )
            observed_members += 1
            for point in fanins:
                emit("<B2d", ord("F"), point[0], point[1])
                observed_fanins += 1
    observed = (
        observed_banks,
        observed_channels,
        observed_groups,
        observed_members,
        observed_fanins,
    )
    if observed != counts:
        raise ValidationError("Chimew typed input record counts disagree")
    stream.write(
        struct.pack("<8sQ32s", _TYPED_INPUT_END_MAGIC, checksum, digest)
    )
    return {
        "records": sum(observed),
        "stream_checksum": checksum,
    }
class _NativeAssignmentResult:
    """Disk-backed view of the native primal and residual-dual certificate."""

    def __init__(
        self,
        owner: tempfile.TemporaryDirectory[str],
        connection: sqlite3.Connection,
        metrics: Tuple[int, int, int],
    ):
        self._owner = owner
        self.connection = connection
        self.metrics = metrics

    def potential(self, label: str) -> array:
        row = self.connection.execute(
            "SELECT size FROM certificates WHERE label=?", (label,)
        ).fetchone()
        if row is None:
            return array("q")
        size = _native_integer(row[0], "certificate size", nonnegative=True)
        result = array("q")
        expected = 0
        for index, value in self.connection.execute(
            "SELECT node_index,potential FROM potentials "
            "WHERE label=? ORDER BY node_index",
            (label,),
        ):
            node_index = _native_integer(
                index, "certificate node index", nonnegative=True
            )
            potential = _native_integer(value, "certificate potential")
            if node_index != expected:
                raise EmuFlowError(
                    f"Chimew certificate {label!r} is incomplete"
                )
            result.append(potential)
            expected += 1
        if expected != size:
            raise EmuFlowError(f"Chimew certificate {label!r} is incomplete")
        return result

    def iter_bank_assignments(self) -> Iterator[Tuple[int, int, int]]:
        for group_index, bank_index, cost in self.connection.execute(
            "SELECT group_index,bank_index,cost FROM bank_assignments "
            "ORDER BY group_index"
        ):
            yield (
                _native_integer(group_index, "bank assignment group", nonnegative=True),
                _native_integer(bank_index, "bank assignment bank", nonnegative=True),
                _native_integer(cost, "bank assignment cost", nonnegative=True),
            )

    def group_indices_for_bank(self, bank_index: int) -> array:
        return array(
            "q",
            (
                _native_integer(
                    row[0], "bank assignment group", nonnegative=True
                )
                for row in self.connection.execute(
                    "SELECT group_index FROM bank_assignments "
                    "WHERE bank_index=? ORDER BY group_index",
                    (bank_index,),
                )
            ),
        )

    def alternative_cost(self, bank_index: int, priority: int) -> int:
        row = self.connection.execute(
            "SELECT cost FROM alternatives WHERE bank_index=? AND priority=?",
            (bank_index, priority),
        ).fetchone()
        if row is None:
            raise EmuFlowError("Chimew package-pin alternative is missing")
        return _native_integer(row[0], "alternative cost", nonnegative=True)

    def chosen_priority(self, bank_index: int) -> int | None:
        row = self.connection.execute(
            "SELECT priority FROM chosen WHERE bank_index=?", (bank_index,)
        ).fetchone()
        return (
            None
            if row is None
            else _native_integer(row[0], "chosen priority", nonnegative=True)
        )

    def channel_assignments(
        self, bank_index: int, priority: int
    ) -> Iterator[Tuple[int, int, int]]:
        for group_index, channel_index, cost in self.connection.execute(
            "SELECT group_index,channel_index,cost FROM channel_assignments "
            "WHERE bank_index=? AND priority=? ORDER BY group_index",
            (bank_index, priority),
        ):
            yield (
                _native_integer(group_index, "channel assignment group", nonnegative=True),
                _native_integer(channel_index, "channel assignment channel", nonnegative=True),
                _native_integer(cost, "channel assignment cost", nonnegative=True),
            )

    def final_assignments(self) -> Iterator[Tuple[int, int, int, int, int]]:
        for row in self.connection.execute(
            """
            SELECT bank_assignment.group_index,bank_assignment.bank_index,
                   bank_assignment.cost,channel_assignment.channel_index,
                   channel_assignment.cost
            FROM bank_assignments AS bank_assignment
            JOIN channel_assignments AS channel_assignment
              ON channel_assignment.group_index=bank_assignment.group_index
             AND channel_assignment.bank_index=bank_assignment.bank_index
            JOIN chosen
              ON chosen.bank_index=channel_assignment.bank_index
             AND chosen.priority=channel_assignment.priority
            ORDER BY bank_assignment.group_index
            """
        ):
            yield tuple(
                _native_integer(value, "final assignment field", nonnegative=True)
                for value in row
            )

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None  # type: ignore[assignment]
            self._owner.cleanup()

    def __enter__(self) -> "_NativeAssignmentResult":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _native_integer(value: Any, label: str, *, nonnegative: bool = False) -> int:
    """Read a typed native-certificate field without SQLite coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise EmuFlowError(f"Chimew {label} is not an integer")
    if nonnegative and value < 0:
        raise EmuFlowError(f"Chimew {label} is negative")
    return value


def _run_native(
    problem: Mapping[str, Any],
    executable: Optional[str],
    *,
    logical_sha256: str,
) -> _NativeAssignmentResult:
    owner = tempfile.TemporaryDirectory(prefix="emuflow-chimew-assignment-")
    root = Path(owner.name)
    stderr_path = root / "stderr.txt"
    database_path = root / "certificate.sqlite"
    connection: sqlite3.Connection | None = None
    process: subprocess.Popen[str] | None = None
    stderr_stream: Any = None
    try:
        reference_driver = executable
        if executable is None or Path(executable).samefile(emuflow_native()):
            reference_driver = str(chimew_bank_oracle())
        native_command = [str(reference_driver)]
        stderr_stream = stderr_path.open("w+", encoding="utf-8")
        process = subprocess.Popen(
            [*native_command, "--typed-input", "-", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_stream,
        )
        if process.stdin is None:
            raise EmuFlowError("Chimew bank/channel input stream is unavailable")
        input_stream = process.stdin
        try:
            _serialize_typed(problem, input_stream, logical_sha256)
            input_stream.close()
            process.stdin = None
        except BrokenPipeError as error:
            try:
                input_stream.close()
            except (BrokenPipeError, OSError):
                pass
            process.stdin = None
            returncode = process.wait()
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            stderr_stream.flush()
            stderr_stream.seek(0)
            detail = stderr_stream.read().strip()
            raise EmuFlowError(
                "Chimew bank/channel kernel rejected typed input "
                f"with exit status {returncode}"
                + (f": {detail}" if detail else "")
            ) from error
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE bank_assignments(
              group_index INTEGER PRIMARY KEY,
              bank_index INTEGER NOT NULL,
              cost INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE INDEX bank_assignments_bank_order
              ON bank_assignments(bank_index,group_index);
            CREATE TABLE certificates(
              label TEXT PRIMARY KEY,
              size INTEGER NOT NULL CHECK(size>=0)
            ) WITHOUT ROWID;
            CREATE TABLE potentials(
              label TEXT NOT NULL,
              node_index INTEGER NOT NULL,
              potential INTEGER NOT NULL,
              PRIMARY KEY(label,node_index)
            ) WITHOUT ROWID;
            CREATE TABLE chosen(
              bank_index INTEGER PRIMARY KEY,
              priority INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE alternatives(
              bank_index INTEGER NOT NULL,
              priority INTEGER NOT NULL,
              cost INTEGER NOT NULL,
              PRIMARY KEY(bank_index,priority)
            ) WITHOUT ROWID;
            CREATE TABLE channel_assignments(
              bank_index INTEGER NOT NULL,
              priority INTEGER NOT NULL,
              group_index INTEGER NOT NULL,
              channel_index INTEGER NOT NULL,
              cost INTEGER NOT NULL,
              PRIMARY KEY(bank_index,priority,group_index),
              FOREIGN KEY(bank_index,priority)
                REFERENCES alternatives(bank_index,priority)
            ) WITHOUT ROWID;
            """
        )
        metrics: Tuple[int, int, int] | None = None
        bank_rows: list[Tuple[int, int, int]] = []
        potential_rows: list[Tuple[str, int, int]] = []
        channel_rows: list[Tuple[int, int, int, int, int]] = []

        def flush_bank_rows() -> None:
            if bank_rows:
                connection.executemany(
                    "INSERT INTO bank_assignments VALUES (?,?,?)", bank_rows
                )
                bank_rows.clear()

        def flush_potential_rows() -> None:
            if potential_rows:
                connection.executemany(
                    "INSERT INTO potentials VALUES (?,?,?)", potential_rows
                )
                potential_rows.clear()

        def flush_channel_rows() -> None:
            if channel_rows:
                connection.executemany(
                    "INSERT INTO channel_assignments VALUES (?,?,?,?,?)",
                    channel_rows,
                )
                channel_rows.clear()

        if process.stdout is None:
            raise EmuFlowError("Chimew bank/channel output stream is unavailable")
        with io.TextIOWrapper(process.stdout, encoding="utf-8") as stream:
            if stream.readline().rstrip("\n") != "EMUFLOW_CHIMEW_BANK_CHANNEL_OUTPUT_V1":
                returncode = process.wait()
                stderr_stream.flush()
                stderr_stream.seek(0)
                stderr_text = stderr_stream.read().strip()
                stderr_stream.close()
                if returncode != 0:
                    raise EmuFlowError(
                        "Chimew bank/channel kernel failed: "
                        + (stderr_text or f"exit status {returncode}")
                    )
                raise EmuFlowError("Chimew bank/channel output header is invalid")
            def fields_for(tag: str, size: int) -> list[str]:
                fields = stream.readline().split()
                if len(fields) != size or fields[0] != tag:
                    raise EmuFlowError("Chimew bank/channel output is malformed")
                return fields

            metric_fields = fields_for("METRIC", 4)
            metrics = tuple(map(int, metric_fields[1:]))
            for expected_group in range(len(problem["groups"])):
                fields = fields_for("BANK_ASSIGN", 4)
                values = tuple(map(int, fields[1:]))
                if values[0] != expected_group:
                    raise EmuFlowError("Chimew bank assignment is incomplete")
                bank_rows.append(values)
                if len(bank_rows) >= 4096:
                    flush_bank_rows()
            flush_bank_rows()

            def read_certificate(label: str, expected_size: int) -> None:
                fields = fields_for("CERT", 3)
                if fields[1] != label:
                    raise EmuFlowError("Chimew bank/channel output is malformed")
                size = int(fields[2])
                if size != expected_size:
                    raise EmuFlowError(
                        "Chimew assignment certificate dimensions are invalid"
                    )
                connection.execute(
                    "INSERT INTO certificates VALUES (?,?)", (label, size)
                )
                for expected_node in range(size):
                    potential = fields_for("POT", 4)
                    if potential[1] != label or int(potential[2]) != expected_node:
                        raise EmuFlowError(
                            f"Chimew certificate {label!r} is incomplete"
                        )
                    potential_rows.append(
                        (label, expected_node, int(potential[3]))
                    )
                    if len(potential_rows) >= 4096:
                        flush_potential_rows()

            read_certificate(
                "STAGE1", 2 + len(problem["banks"]) + len(problem["groups"])
            )
            active_banks = tuple(
                (int(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT bank_index,count(*) FROM bank_assignments "
                    "GROUP BY bank_index ORDER BY bank_index"
                )
            )
            for bank_index, bank_group_count in active_banks:
                if not 0 <= bank_index < len(problem["banks"]):
                    raise EmuFlowError("Chimew bank assignment is incomplete")
                chosen_fields = fields_for("CHOSEN", 3)
                chosen_bank, chosen_priority = map(int, chosen_fields[1:])
                if chosen_bank != bank_index:
                    raise EmuFlowError("Chimew bank/channel output is malformed")
                connection.execute(
                    "INSERT INTO chosen VALUES (?,?)",
                    (chosen_bank, chosen_priority),
                )
                for priority in (0, 1):
                    alternative = fields_for("ALTERNATIVE", 4)
                    alternative_bank = int(alternative[1])
                    alternative_priority = int(alternative[2])
                    alternative_cost = int(alternative[3])
                    if (
                        alternative_bank != bank_index
                        or alternative_priority != priority
                    ):
                        raise EmuFlowError(
                            "Chimew bank/channel output is malformed"
                        )
                    connection.execute(
                        "INSERT INTO alternatives VALUES (?,?,?)",
                        (bank_index, priority, alternative_cost),
                    )
                    for _ in range(bank_group_count):
                        assignment = fields_for("CHANNEL_ASSIGN", 6)
                        values = tuple(map(int, assignment[1:]))
                        if values[:2] != (bank_index, priority):
                            raise EmuFlowError(
                                "Chimew bank/channel output is malformed"
                            )
                        channel_rows.append(values)
                        if len(channel_rows) >= 4096:
                            flush_channel_rows()
                    read_certificate(
                        f"BANK{bank_index}P{priority}",
                        2
                        + len(problem["banks"][bank_index]["channels"])
                        + bank_group_count,
                    )
            if stream.readline() != "":
                raise EmuFlowError("Chimew bank/channel output is malformed")
        returncode = process.wait()
        stderr_stream.flush()
        stderr_stream.seek(0)
        stderr_text = stderr_stream.read().strip()
        stderr_stream.close()
        if returncode != 0:
            raise EmuFlowError(
                "Chimew bank/channel kernel failed: "
                + (stderr_text or f"exit status {returncode}")
            )
        if metrics is None:
            raise EmuFlowError("Chimew bank/channel metrics are missing")
        flush_bank_rows()
        flush_potential_rows()
        flush_channel_rows()
        connection.commit()
        return _NativeAssignmentResult(owner, connection, metrics)
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        if connection is not None:
            connection.close()
        if stderr_stream is not None and not stderr_stream.closed:
            stderr_stream.close()
        owner.cleanup()
        if isinstance(error, EmuFlowError):
            raise
        raise EmuFlowError("Chimew bank/channel output is malformed") from error
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        if connection is not None:
            connection.close()
        if stderr_stream is not None and not stderr_stream.closed:
            stderr_stream.close()
        owner.cleanup()
        raise


def _group_cost_signature(group: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Return an exact, identifier-free signature for one Algorithm 2 row."""

    members = []
    for member in group["members"]:
        members.append(
            (
                member.get("timing_weight", 1.0),
                member.get("direction", group["direction"]),
                member["fanout"],
                tuple(sorted(member["fanins"])),
            )
        )
    return group["kind"], group["direction"], tuple(sorted(members))


def _verify_indexed_stage1_certificate(
    problem: Mapping[str, Any],
    native: _NativeAssignmentResult,
    expected_total: int,
) -> None:
    """Check Stage 1 from ordered cursors without candidate/assignment maps."""

    banks = problem["banks"]
    groups = problem["groups"]
    right_count = len(banks)
    left_count = len(groups)
    source = 0
    first_right = 1
    first_left = first_right + right_count
    sink = first_left + left_count
    potentials = native.potential("STAGE1")
    if len(potentials) != sink + 1:
        raise EmuFlowError("Chimew assignment certificate dimensions are invalid")
    used = array("q", [0]) * right_count
    total = 0
    observed = 0
    assignment_cursor = native.iter_bank_assignments()
    for group, assignment in zip(groups, assignment_cursor):
        group_index, selected_bank, selected_cost = assignment
        if group_index != observed or not 0 <= selected_bank < right_count:
            raise EmuFlowError("Chimew bank assignment is incomplete")
        if banks[selected_bank]["domain"] != group["domain"]:
            raise EmuFlowError("Chimew assignment selects an ineligible bank")
        used[selected_bank] += 1
        total += selected_cost
        left_node = first_left + group_index
        for bank_index, bank in enumerate(banks):
            if bank["domain"] != group["domain"]:
                continue
            cost = _candidate_cost(
                group,
                bank["bank_a"]["point"],
                bank["bank_b"]["point"],
                problem["cost_scale"],
            )
            if bank_index == selected_bank and selected_cost != cost:
                raise EmuFlowError("Chimew assignment selects a wrong-cost edge")
            reduced = (
                -cost + potentials[left_node] - potentials[first_right + bank_index]
                if bank_index == selected_bank
                else cost + potentials[first_right + bank_index] - potentials[left_node]
            )
            if reduced < 0:
                raise EmuFlowError(
                    "Chimew residual-dual certificate has a negative reduced cost"
                )
        if potentials[sink] - potentials[left_node] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )
        observed += 1
    if (
        observed != left_count
        or next(assignment_cursor, None) is not None
        or total != expected_total
    ):
        raise EmuFlowError("Chimew assignment primal certificate is invalid")
    for bank_index, (use, bank) in enumerate(zip(used, banks)):
        if use > len(bank["channels"]):
            raise EmuFlowError("Chimew assignment primal certificate is invalid")
        start, end = (
            (first_right + bank_index, source)
            if use
            else (source, first_right + bank_index)
        )
        if potentials[start] - potentials[end] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )


def _verify_indexed_stage2_certificate(
    bank: Mapping[str, Any],
    all_groups: Sequence[Mapping[str, Any]],
    groups: Sequence[int],
    priority: int,
    assignments: Iterable[Tuple[int, int, int]],
    potentials: Sequence[int],
    expected_total: int,
    cost_scale: int,
) -> None:
    """Check one package-pin alternative with bounded arrays and cursors."""

    right_count = len(bank["channels"])
    left_count = len(groups)
    source = 0
    first_right = 1
    first_left = first_right + right_count
    sink = first_left + left_count
    if len(potentials) != sink + 1:
        raise EmuFlowError("Chimew assignment certificate dimensions are invalid")

    direction_counts = [0, 0, 0]
    common_count = 0
    for group_index in groups:
        group = all_groups[group_index]
        if group["kind"] == 0:
            direction_counts[group["direction"]] += 1
        else:
            common_count += 1
    dedicated_direction = None
    if common_count == 0:
        active = [
            direction
            for direction, count in enumerate(direction_counts)
            if count > 0
        ]
        if len(active) == 1:
            dedicated_direction = active[0]

    def eligible(group: Mapping[str, Any], right: int) -> bool:
        if dedicated_direction is not None:
            return group["kind"] == 0 and group["direction"] == dedicated_direction
        first, second = priority, 1 - priority
        if right < direction_counts[first]:
            return group["kind"] == 0 and group["direction"] == first
        if right < direction_counts[first] + direction_counts[second]:
            return group["kind"] == 0 and group["direction"] == second
        if right < sum(direction_counts):
            return group["kind"] == 0 and group["direction"] == 2
        return group["kind"] == 1

    channel_position = {
        channel["index"]: position
        for position, channel in enumerate(bank["channels"])
    }
    used = array("q", [0]) * right_count
    cost_cache: OrderedDict[Tuple[Any, ...], array] = OrderedDict()
    total = 0
    observed = 0
    assignment_cursor = iter(assignments)
    for left, (expected_group_index, assignment) in enumerate(
        zip(groups, assignment_cursor)
    ):
        group_index, channel_index, selected_cost = assignment
        if group_index != expected_group_index or channel_index not in channel_position:
            raise EmuFlowError("Chimew package-pin assignment is incomplete")
        right = channel_position[channel_index]
        group = all_groups[group_index]
        if not eligible(group, right):
            raise EmuFlowError("Chimew assignment selects an ineligible channel")
        left_node = first_left + left
        signature = _group_cost_signature(group)
        costs = cost_cache.get(signature)
        if costs is None:
            costs = array("q")
            for candidate_right, channel in enumerate(bank["channels"]):
                costs.append(
                    _candidate_cost(
                        group, channel["pin_a"], channel["pin_b"], cost_scale
                    )
                    if eligible(group, candidate_right)
                    else -1
                )
            cost_cache[signature] = costs
            if len(cost_cache) > 256:
                cost_cache.popitem(last=False)
        else:
            cost_cache.move_to_end(signature)
        for candidate_right, cost in enumerate(costs):
            if not eligible(group, candidate_right):
                continue
            if candidate_right == right:
                if selected_cost != cost:
                    raise EmuFlowError(
                        "Chimew assignment selects a wrong-cost edge"
                    )
                reduced = (
                    -cost
                    + potentials[left_node]
                    - potentials[first_right + candidate_right]
                )
            else:
                reduced = (
                    cost
                    + potentials[first_right + candidate_right]
                    - potentials[left_node]
                )
            if reduced < 0:
                raise EmuFlowError(
                    "Chimew residual-dual certificate has a negative reduced cost"
                )
        if potentials[sink] - potentials[left_node] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )
        used[right] += 1
        total += selected_cost
        observed += 1
    if (
        observed != left_count
        or next(assignment_cursor, None) is not None
        or total != expected_total
        or any(use > 1 for use in used)
    ):
        raise EmuFlowError("Chimew assignment primal certificate is invalid")
    for right, use in enumerate(used):
        start, end = (
            (first_right + right, source)
            if use
            else (source, first_right + right)
        )
        if potentials[start] - potentials[end] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )


def evaluate_chimew_bank_channel_assignment(
    document: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Run both paper stages and check exact optimality without replaying them."""

    from emuflow.storage.store_publication import bind_view_sources

    source_bindings = bind_view_sources((document, "chimew-bank-input-store-v1"))
    from emuflow_tools.transport.chimew.chimew_bank_channel_storage import canonical_bank_channel_input_sha256

    input_sha256 = canonical_bank_channel_input_sha256(document)
    problem = validate_chimew_bank_channel_input(document)
    banks = problem["banks"]
    with _run_native(
        problem, executable, logical_sha256=input_sha256
    ) as native:
        group_count, stage1_cost, stage2_cost = native.metrics
        if group_count != len(problem["groups"]):
            raise EmuFlowError("Chimew bank/channel metrics do not agree")
        _verify_indexed_stage1_certificate(problem, native, stage1_cost)

        checked_alternatives = 0
        computed_stage2_cost = 0
        priorities = {}
        nonempty_banks = 0
        active_bank_indices = []
        for bank_index, bank in enumerate(banks):
            groups = native.group_indices_for_bank(bank_index)
            if not groups:
                continue
            nonempty_banks += 1
            active_bank_indices.append(bank_index)
            alternatives = []
            for priority in (0, 1):
                alternative_cost = native.alternative_cost(bank_index, priority)
                _verify_indexed_stage2_certificate(
                    bank,
                    problem["groups"],
                    groups,
                    priority,
                    native.channel_assignments(bank_index, priority),
                    native.potential(f"BANK{bank_index}P{priority}"),
                    alternative_cost,
                    problem["cost_scale"],
                )
                alternatives.append(alternative_cost)
                checked_alternatives += 1
            expected_priority = 1 if alternatives[1] < alternatives[0] else 0
            if native.chosen_priority(bank_index) != expected_priority:
                raise EmuFlowError(
                    "Chimew package-pin priority choice is not minimum-cost"
                )
            priorities[bank["id"]] = (
                "a_to_b" if expected_priority == 0 else "b_to_a"
            )
            computed_stage2_cost += alternatives[expected_priority]
        if (
            computed_stage2_cost != stage2_cost
            or native.connection.execute("SELECT count(*) FROM chosen").fetchone()[0]
            != nonempty_banks
            or native.connection.execute(
                "SELECT count(*) FROM alternatives"
            ).fetchone()[0]
            != 2 * nonempty_banks
            or {
                str(row[0])
                for row in native.connection.execute(
                    "SELECT label FROM certificates"
                )
            }
            != {
                "STAGE1",
                *(
                    f"BANK{bank_index}P{priority}"
                    for bank_index in active_bank_indices
                    for priority in (0, 1)
                ),
            }
        ):
            raise EmuFlowError("Chimew selected package-pin cost does not agree")

        def iter_assignments() -> Iterator[Mapping[str, Any]]:
            observed = 0
            assignment_cursor = native.final_assignments()
            for group, row in zip(problem["groups"], assignment_cursor):
                (
                    group_index,
                    bank_index,
                    bank_cost,
                    channel_index,
                    channel_cost,
                ) = row
                if group_index != observed or not 0 <= bank_index < len(banks):
                    raise EmuFlowError("Chimew bank/channel assignment is incomplete")
                bank = banks[bank_index]
                channels = bank["channels"]
                first_channel = channels[0]["index"] if channels else -1
                local_channel = channel_index - first_channel
                if (
                    not 0 <= local_channel < len(channels)
                    or channels[local_channel]["index"] != channel_index
                ):
                    raise EmuFlowError("Chimew bank/channel stages disagree")
                channel = channels[local_channel]
                bank_raw_cost = _raw_cost(
                    group, bank["bank_a"]["point"], bank["bank_b"]["point"]
                )
                channel_raw_cost = _raw_cost(
                    group, channel["pin_a"], channel["pin_b"]
                )
                yield {
                    "group": group["id"],
                    "bank_pair": bank["id"],
                    "channel": channel["id"],
                    "bank_cost_rank": bank_cost,
                    "channel_cost_rank": channel_cost,
                    "bank_cost": bank_raw_cost,
                    "channel_cost": channel_raw_cost,
                }
                observed += 1
            if observed != group_count or next(assignment_cursor, None) is not None:
                raise EmuFlowError("Chimew bank/channel assignment is incomplete")

        header = {
            "schema": CHIMEW_BANK_CHANNEL_REPORT_SCHEMA,
            "status": CHIMEW_BANK_CHANNEL_STATUS,
            "integration_status": CHIMEW_BANK_CHANNEL_INTEGRATION_STATUS,
            "design": problem["design"],
            "platform": problem["platform"],
            "provider": CHIMEW_BANK_CHANNEL_PROVIDER,
            "paper_scope": CHIMEW_BANK_CHANNEL_PAPER_SCOPE,
            "algorithm2_interpretation": (
                CHIMEW_BANK_CHANNEL_ALGORITHM2_INTERPRETATION
            ),
            "numeric_policy": CHIMEW_BANK_CHANNEL_NUMERIC_POLICY,
            "provenance": problem["provenance"],
            "input_sha256": input_sha256,
            "metrics": {
                **problem["metrics"],
                "stage1_cost_rank": stage1_cost,
                "stage2_cost_rank": stage2_cost,
                "certified_matchings": 1 + checked_alternatives,
                "certificate_disagreements": 0,
            },
            "direction_priority": priorities,
        }
        document_view = {**header, "assignments": iter_assignments()}
        if output_path is not None:
            from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage

            return write_chimew_bank_channel_report_storage(
                output_path,
                document_view,
                _derive_physical_costs=True,
                source_bindings=source_bindings,
            )
        materialized_assignments = []
        stage1_physical_cost = 0.0
        stage2_physical_cost = 0.0
        for assignment in iter_assignments():
            materialized_assignments.append(assignment)
            stage1_physical_cost += float(assignment["bank_cost"])
            stage2_physical_cost += float(assignment["channel_cost"])
        header["metrics"]["stage1_physical_site_cost"] = stage1_physical_cost
        header["metrics"]["stage2_physical_site_cost"] = stage2_physical_cost
        return {**header, "assignments": materialized_assignments}
