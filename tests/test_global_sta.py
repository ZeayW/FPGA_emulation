from tests.schedule_store_fixture import write_tdm_schedule_storage
import os
import subprocess
from array import array
from collections.abc import Sequence
from unittest import mock

import pytest
from tests.system_timing_oracle import compare_system_timing

from emuflow.errors import ValidationError
from emuflow.global_sta import (
    EventCheck, _EventCheckBuilder, _IndexedEventChecks,
    _IndexedMeasurements, _IndexedPathMetadata, _IndexedSystemTimingPaths,
    _PathMetadataBuilder,
    _compress_path_equivalent_checks, _write_expanded_measurements,
    bind_physical_checks, build_opensta_timing,
    export_event_checks, read_engine_identity,
    read_measurements, run_event_checks, validate_checks,
)


def _reference_physical_checks(runtime, routes, schedule, physical, platform, *, metadata):
    # These two storage-oracle cases intentionally combine inline route/endpoint
    # facts with indexed segments. Production requires complete current owners.
    from emuflow.global_sta import _bind_physical_checks
    builder = _EventCheckBuilder(bound=True)
    try:
        return _bind_physical_checks(runtime, routes, schedule, physical, platform,
                                     metadata=metadata, result=builder,
                                     path_metadata=_PathMetadataBuilder(builder))
    except BaseException:
        builder._owner.close()
        raise


def example():
    # Two consecutive transported cuts: a missed first TX must not be hidden
    # by a legal final commit. Delays are separate physical arcs, not a total.
    return [EventCheck("p", "tx", "first", 0, (3,), 4),
            EventCheck("p", "tx", "second", 4, (8, .5, 2.5), 16),
            EventCheck("p", "target", "capture", 16, (8, .5, 7), 18),
            EventCheck("p", "runtime", "capture", 16, (8, .5, 7), 100),
            EventCheck("p", "commit", "capture", 16, (8, .5, 7), 96)]


def test_exports_raw_arc_chain_and_absolute_events(tmp_path):
    export_event_checks(example(), tmp_path)
    sdc = (tmp_path / "global_timing.sdc").read_text()
    assert "set_input_delay -clock epoch -max 0" in sdc
    assert "set_output_delay -clock epoch -max -1 [get_ports o2]" in sdc
    assert "find_port" in (tmp_path / "analyze.tcl").read_text()
    assert "gets $constraints line" in (tmp_path / "analyze.tcl").read_text()
    assert "31.5" not in (tmp_path / "global_timing.lib").read_text()
    script = (tmp_path / "analyze.tcl").read_text()
    assert "find_timing" in script
    assert "$::emuflow_top find_pin o$i" in script
    assert "sta::vertex_worst_slack_path $vertex max" in script
    assert "find_timing_paths" not in script
    assert "[$p arrival]" in script
    assert 'sta::endpoint_slack $pin "epoch" max' in script
    assert "[$p required]" not in script
    assert "get_property $p points" not in (tmp_path / "analyze.tcl").read_text()


def test_reject_incomplete_duplicate_and_nan():
    rows = example()
    for bad in (rows[:-2]+rows[-1:], rows+rows[:1],
                rows+[EventCheck("p", "tx", "bad", 0, (float("nan"),), 4)]):
        with pytest.raises(ValidationError):
            validate_checks(bad)

    with pytest.raises(ValidationError, match="observe each original path once"):
        validate_checks(example() + [
            EventCheck("p", "target", "second-capture", 16, (8, .5, 7), 18)
        ])


def test_measurement_requires_exact_endpoint_coverage(tmp_path):
    path = tmp_path / "out"
    path.write_text("endpoint\tarrival_ns\trequired_ns\tslack_ns\no0\t3\t4\t1\n")
    with pytest.raises(ValidationError, match="endpoints"):
        read_measurements(path, example())


def test_measurement_requires_canonical_endpoint_order(tmp_path):
    rows = example()[:2]
    path = tmp_path / "out"
    path.write_text(
        "endpoint\tarrival_ns\trequired_ns\tslack_ns\n"
        "o1\t11\t12\t1\n"
        "o0\t3\t4\t1\n"
    )
    with pytest.raises(ValidationError, match="endpoint"):
        read_measurements(path, rows, verify_arcs=False)


def test_measurements_use_fixed_width_scalars(tmp_path):
    rows = [
        EventCheck(f"path-{index}", "target", "capture", 1.0, (2.0,), 4.0)
        for index in range(20000)
    ]
    path = tmp_path / "measurements.tsv"
    with path.open("w") as stream:
        stream.write("endpoint\tarrival_ns\trequired_ns\tslack_ns\n")
        for index in range(len(rows)):
            stream.write(f"o{index}\t2\t3\t1\n")

    measurements = read_measurements(path, rows)

    assert isinstance(measurements, _IndexedMeasurements)
    assert len(measurements) == len(rows)
    assert measurements.typed_payload_bytes == len(rows) * 3 * 8
    assert measurements[0] == {
        "path": "path-0",
        "role": "target",
        "event": "capture",
        "arrival_ns": 3.0,
        "required_ns": 4.0,
        "slack_ns": 1.0,
    }
    assert measurements[-1]["path"] == "path-19999"


def test_engine_identity_from_existing_banner(tmp_path):
    log = tmp_path / "opensta.log"
    log.write_text("startup warning\nOpenSTA 2.6.0 0c6421e022 Copyright (c) 2024\n")
    assert read_engine_identity(log) == {
        "name": "OpenSTA", "version": "2.6.0", "revision": "0c6421e022"}
    log.write_text("unidentified engine\n")
    with pytest.raises(ValidationError, match="engine version"):
        read_engine_identity(log)


def test_engine_identity_uses_executable_seal_for_archive_build(tmp_path):
    log = tmp_path / "opensta.log"
    digest = "a" * 64
    log.write_text(
        f"EmuFlow OpenSTA executable SHA256 {digest}\n"
        "OpenSTA 2.6.0 GITDIR-NOT Copyright (c) 2024\n"
    )
    assert read_engine_identity(log) == {
        "name": "OpenSTA",
        "version": "2.6.0",
        "revision": f"binary-sha256:{digest}",
        "reported_revision": "GITDIR-NOT",
        "executable_sha256": digest,
    }


def test_engine_identity_retains_distinct_container_launcher_seal(tmp_path):
    log = tmp_path/"opensta.log"
    for revision in ("abcdef123456", "GITDIR-NOT"):
        log.write_text(f"EmuFlow OpenSTA executable SHA256 {'a'*64}\n"
                       f"EmuFlow OpenSTA launcher SHA256 {'b'*64}\n"
                       f"OpenSTA 3.1.0 {revision} Copyright\n")
        identity = read_engine_identity(log)
        assert identity["executable_sha256"] == "a"*64
        assert identity["launcher_sha256"] == "b"*64


@pytest.mark.parametrize("field", [0, 1, 2])
def test_measurement_rejects_corrupt_event_scalars(tmp_path, field):
    rows = example()
    values = [[sum(r.arcs_ns), r.required_ns-r.launch_ns,
               r.required_ns-r.launch_ns-sum(r.arcs_ns)] for r in rows]
    path = tmp_path / "out"
    def write():
        path.write_text("endpoint\tarrival_ns\trequired_ns\tslack_ns\n" +
                        "".join(f"o{i}\t"+"\t".join(map(str, v))+"\n"
                                for i, v in enumerate(values)))
    write()
    assert len(read_measurements(path, rows)) == len(rows)
    values[0][field] += 2
    write()
    with pytest.raises(ValidationError, match="event binding"):
        read_measurements(path, rows)


def test_reject_orphan_events_and_divergent_observation_chains():
    with pytest.raises(ValidationError, match="no original-path"):
        validate_checks(example()+[EventCheck("orphan", "tx", "tx", 0, (1,), 2)])
    rows = example()
    rows[3] = EventCheck("p", "runtime", "capture", 16, (1,), 100)
    with pytest.raises(ValidationError, match="physical chains"):
        validate_checks(rows)


def test_check_population_validation_is_linear():
    # A quadratic original-member scan previously made global timing unusable.
    rows = [EventCheck(str(i), role, "end", 0, (1,), 2)
            for i in range(20000) for role in ("target", "runtime")]
    assert len(validate_checks(rows)) == 40000


def test_public_check_validation_preserves_list_snapshot_contract(tmp_path):
    class MutableChecks(Sequence):
        def __init__(self, rows):
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, index):
            return self.rows[index]

    expected = example()
    for source in (expected, tuple(expected), MutableChecks(expected)):
        validated = validate_checks(source)
        assert isinstance(validated, list)
        assert validated == expected
        assert validated is not source

    mutable = MutableChecks(list(expected))
    snapshot = validate_checks(mutable)
    mutable.rows.clear()
    assert snapshot == expected

    exported = export_event_checks(tuple(expected), tmp_path)
    assert isinstance(exported, list)
    assert exported == expected


def test_indexed_check_builder_is_exact_compact_and_sealed(tmp_path):
    rows = [
        EventCheck(
            f"p{path}", role, event, 0.0, (1.0, 2.0, 3.0), deadline
        )
        for path in range(20000)
        for role, event, deadline in (
            ("tx", "shared-tx", 4.0),
            ("target", "capture", 20.0),
            ("runtime", "capture", 100.0),
            ("commit", "capture", 96.0),
        )
    ]
    builder = _EventCheckBuilder()
    for row in rows:
        builder.append(row)
    compact = builder.finish()

    assert isinstance(compact, _IndexedEventChecks)
    assert compact == rows
    assert len(compact._paths) == 20000
    assert compact._events == ["shared-tx", "capture"]
    assert not isinstance(compact._paths, list)
    assert not isinstance(compact._events, list)
    assert compact._owner.header["numeric_bytes"] == len(rows) * 40
    assert not compact._owner.pending
    assert not compact._owner.event_cache
    assert not hasattr(compact, "_arcs")
    assert not hasattr(compact, "_path_indices")
    assert compact._owner.primary.is_file()
    assert compact._owner.index.is_file()
    assert compact._owner.header["representative_checks"] == 4
    assert compact[-1] == rows[-1]
    assert compact[3:7] == rows[3:7]
    representatives, mapping = _compress_path_equivalent_checks(compact)
    assert len(representatives) == 4
    assert list(mapping[:8]) == list(range(4)) * 2
    assert validate_checks(compact) is compact
    assert validate_checks(representatives) is representatives
    with pytest.raises(ValidationError, match="sealed"):
        builder.append(rows[0])
    with pytest.raises(ValidationError, match="sealed"):
        builder.finish()

    list_dir = tmp_path / "list"
    compact_dir = tmp_path / "compact"
    small_builder = _EventCheckBuilder()
    for row in rows[:8]:
        small_builder.append(row)
    small_compact = small_builder.finish()
    export_event_checks(rows[:8], list_dir)
    assert export_event_checks(small_compact, compact_dir) is small_compact
    from emuflow.global_event_store import event_model_directory
    for name in ("global_timing.lib", "global_timing.v", "global_timing.sdc", "analyze.tcl"):
        assert (event_model_directory(compact_dir) / name).read_bytes() == (list_dir / name).read_bytes()
    root = compact._owner.root
    compact.close()
    assert not root.exists()
    with pytest.raises(ValidationError, match="closed"):
        representatives[0]


@pytest.mark.parametrize("file", ["primary", "index"])
def test_indexed_check_view_rejects_changed_owner(file):
    import os
    builder = _EventCheckBuilder()
    for row in example():
        builder.append(row)
    compact = builder.finish()
    path = getattr(compact._owner, file)
    status = path.stat()
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000))
    with pytest.raises(ValidationError, match="generation changed"):
        compact[0]
    with pytest.raises(ValidationError, match="generation changed"):
        _compress_path_equivalent_checks(compact)
    compact.close()


def test_indexed_path_metadata_is_sorted_compact_mapping():
    from emuflow.local_path_timing import path_id_set_sha256

    builder = _PathMetadataBuilder()
    builder.append(
        "path-z",
        representative_path="representative",
        path_scope="cross-fpga",
        clock_domain="clk",
        target_period_ns=10.0,
        physical_logic_segments_cone_bound=True,
        interconnect_delay_model="linear-ratio-v1",
    )
    builder.append(
        "path-a",
        representative_path="path-a",
        path_scope="same-fpga-local",
        clock_domain=None,
        target_period_ns=20.0,
        physical_logic_segments_cone_bound=False,
    )
    metadata = builder.finish()

    assert isinstance(metadata, _IndexedPathMetadata)
    assert list(metadata) == ["path-a", "path-z"]
    assert metadata["path-a"] == {
        "representative_path": "path-a",
        "path_scope": "same-fpga-local",
        "clock_domain": None,
        "target_period_ns": 20.0,
        "physical_logic_segments_cone_bound": False,
    }
    assert metadata["path-z"]["interconnect_delay_model"] == "linear-ratio-v1"
    assert not isinstance(metadata._paths, list)
    assert not hasattr(metadata, "_strings")
    assert metadata._owner.primary.is_file()
    assert metadata._owner.header["mode"] == 2
    assert metadata.path_id_set_sha256() == path_id_set_sha256(
        ["path-z", "path-a"]
    )
    with pytest.raises(KeyError):
        metadata["missing"]
    with pytest.raises(ValidationError, match="sealed"):
        builder.finish()

    invalid = _PathMetadataBuilder()
    with pytest.raises(ValidationError, match="representative"):
        invalid.append(
            "path",
            representative_path=None,
            path_scope="cross-fpga",
            clock_domain="clk",
            target_period_ns=10.0,
            physical_logic_segments_cone_bound=False,
        )

    duplicate = _PathMetadataBuilder()
    with pytest.raises(ValidationError, match="overlap"):
        for _ in range(2):
            duplicate.append(
                "same", representative_path="same", path_scope="cross-fpga",
                clock_domain="clk", target_period_ns=10.0,
                physical_logic_segments_cone_bound=False,
            )
    duplicate._owner.close()


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("incomplete", [False, True])
def test_check_validation_rejects_interleaved_bundles(indexed, incomplete):
    canonical = [
        EventCheck(path, role, "capture", 0, (1,), deadline)
        for path in ("p", "q")
        for role, deadline in (("target", 2), ("runtime", 20))
    ]
    rows = ([canonical[index] for index in (0, 2, 1, 3)]
            if incomplete else canonical + canonical[:2])
    with pytest.raises(ValidationError, match="population|contiguous"):
        if indexed:
            builder = _EventCheckBuilder()
            try:
                for row in rows:
                    builder.append(row)
                builder.finish()
            finally:
                builder._owner.close()
        else:
            validate_checks(rows)


def test_exact_compression_uses_complete_path_bundles(tmp_path):
    first = example()
    second = [EventCheck("q", row.role, f"q-{row.event}", row.launch_ns,
                         row.arcs_ns, row.required_ns) for row in first]
    changed = [EventCheck("r", row.role, f"r-{row.event}", row.launch_ns,
                          row.arcs_ns, row.required_ns) for row in first]
    changed[0] = EventCheck("r", "tx", "r-first", 0, (3.25,), 4)
    rows = validate_checks(first + second + changed)
    representatives, mapping = _compress_path_equivalent_checks(rows)
    assert representatives == first + changed
    assert isinstance(mapping, array)
    assert list(mapping) == list(range(5)) + list(range(5)) + list(range(5, 10))

    values = [{"path": row.path, "role": row.role, "event": row.event,
               "arrival_ns": row.launch_ns + sum(row.arcs_ns),
               "required_ns": row.required_ns,
               "slack_ns": row.required_ns-row.launch_ns-sum(row.arcs_ns)}
              for row in representatives]
    output = tmp_path / "measurements.tsv"
    emitted = _write_expanded_measurements(output, rows, values, mapping)
    expanded = read_measurements(output, rows)
    assert not isinstance(emitted, list)
    assert emitted == expanded
    assert len(expanded) == len(rows)
    assert expanded[5]["path"] == "q"
    assert expanded[5]["event"] == "q-first"
    assert expanded[5]["slack_ns"] == expanded[0]["slack_ns"]

    damaged = array("I", mapping)
    damaged[10] = 0
    with pytest.raises(ValidationError, match="mapping disagrees"):
        _write_expanded_measurements(
            tmp_path / "damaged-mapping.tsv",
            rows,
            values,
            damaged,
            representatives,
        )


def test_exact_compression_does_not_trust_bundle_hash(monkeypatch):
    import emuflow.global_sta as sta

    rows = [
        EventCheck(path, role, f"{path}-{role}", 0, (delay,), deadline)
        for path, delay in (("p", 1.0), ("q", 2.0), ("r", 1.0))
        for role, deadline in (("target", 4.0), ("runtime", 40.0))
    ]
    monkeypatch.setattr(sta, "_event_check_values_hash", lambda *args: 0)
    representatives, mapping = _compress_path_equivalent_checks(validate_checks(rows))
    assert representatives == rows[:4]
    assert list(mapping) == [0, 1, 2, 3, 0, 1]


def test_native_check_index_compares_repeated_bundles_exactly():
    builder = _EventCheckBuilder()
    rows = []
    for path, delay in (("p", 1.0), ("q", 2.0), ("r", 1.0)):
        for role, deadline in (("target", 4.0), ("runtime", 40.0)):
            row = EventCheck(path, role, f"{path}-{role}", 0, (delay,), deadline)
            rows.append(row)
            builder.append(row)
    compact = builder.finish()
    representatives, mapping = _compress_path_equivalent_checks(compact)
    assert representatives == rows[:4]
    assert list(mapping) == [0, 1, 2, 3, 0, 1]


def test_indexed_check_builder_bounds_live_identity_caches():
    builder = _EventCheckBuilder()
    builder._owner.EVENT_CACHE_SIZE = 2
    builder._owner.CHECK_BATCH_SIZE = 3
    rows = []
    for index in range(20):
        for role, deadline in (("target", 40.0), ("runtime", 400.0)):
            row = EventCheck(
                f"path-{index}",
                role,
                f"event-{index}-{role}",
                0.0,
                (float(index + 1),),
                deadline,
            )
            rows.append(row)
            builder.append(row)
        assert len(builder._owner.event_cache) <= 2
        assert len(builder._owner.pending) < 3
    compact = builder.finish()
    assert compact == rows
    assert not builder._owner.event_cache
    assert not builder._owner.pending
    assert not hasattr(builder, "_path_ids")
    assert not hasattr(builder, "_event_ids")


def test_native_check_index_distinguishes_bundle_counts():
    builder = _EventCheckBuilder()
    rows = [
        EventCheck("p", "target", "p-target", 0, (1,), 4),
        EventCheck("p", "runtime", "p-runtime", 0, (1,), 40),
        EventCheck("q", "tx", "q-tx", 0, (2,), 3),
        EventCheck("q", "target", "q-target", 0, (2,), 4),
        EventCheck("q", "runtime", "q-runtime", 0, (2,), 40),
    ]
    for row in rows:
        builder.append(row)
    compact = builder.finish()
    representatives, mapping = _compress_path_equivalent_checks(compact)
    assert representatives == rows
    assert list(mapping) == list(range(len(rows)))


def test_exact_compression_scales_for_repeated_path_classes():
    rows = [EventCheck(f"p{i}", role, f"{role}-{i}", 0, (1, 2), deadline)
            for i in range(20000)
            for role, deadline in (("target", 4), ("runtime", 40))]
    rows = validate_checks(rows)
    representatives, mapping = _compress_path_equivalent_checks(rows)
    assert len(representatives) == 2
    assert len(mapping) == len(rows)
    assert set(mapping) == {0, 1}


def test_exact_compression_rejects_interleaved_bundles():
    rows = [
        EventCheck("p", "target", "p-target", 0, (1,), 4),
        EventCheck("q", "target", "q-target", 0, (1,), 4),
        EventCheck("p", "runtime", "p-runtime", 0, (1,), 40),
        EventCheck("q", "runtime", "q-runtime", 0, (1,), 40),
    ]
    with pytest.raises(ValidationError, match="contiguous"):
        _compress_path_equivalent_checks(rows)


def test_lazy_expansion_is_reiterable_sliceable_and_fail_closed(tmp_path):
    rows = validate_checks(example())
    representatives, mapping = _compress_path_equivalent_checks(rows)
    values = [
        {
            "path": row.path,
            "role": row.role,
            "event": row.event,
            "arrival_ns": row.launch_ns + sum(row.arcs_ns),
            "required_ns": row.required_ns,
            "slack_ns": row.required_ns - row.launch_ns - sum(row.arcs_ns),
        }
        for row in representatives
    ]
    expanded = _write_expanded_measurements(
        tmp_path / "measurements.tsv", rows, values, mapping
    )
    assert list(expanded) == list(expanded)
    assert expanded[-1] == list(expanded)[-1]
    assert expanded[1:3] == list(expanded)[1:3]
    timing = build_opensta_timing(
        {
            "virtual_dut_clocks": {"clk": {"nominal_period_ns": 100}},
            "virtual_dut_clock": {"nominal_period_ns": 100},
        },
        {
            "paths": {
                "p": {
                    "path_scope": "cross-fpga",
                    "physical_logic_segments_cone_bound": False,
                }
            },
            "source_binding": None,
            "compressed_representative_paths": 1,
        },
        expanded,
    )
    assert timing["summary"]["timing_paths"] == 1
    assert timing["summary"]["maximum_system_delay_bound_ns"] == 31.5

    damaged = array("q", mapping)
    damaged[-1] = len(values)
    with pytest.raises(ValidationError, match="missing representative"):
        _write_expanded_measurements(
            tmp_path / "damaged.tsv", rows, values, damaged
        )


def test_run_event_checks_expands_engine_representatives(tmp_path, monkeypatch):
    import emuflow.global_sta as sta

    first = example()[:4]
    # Keep target/runtime plus both TX legality checks as one complete bundle.
    second = [EventCheck("q", row.role, f"q-{row.event}", row.launch_ns,
                         row.arcs_ns, row.required_ns) for row in first]
    monkeypatch.setattr(sta, "resolve_native_executable",
                        lambda name, executable: "/test/sta")
    monkeypatch.setattr(sta, "require_opensta_engine",
                        lambda tool: {"executable_sha256": "a" * 64})

    def engine(command, *, cwd, stdout, stderr, check):
        assert command == ["/test/sta", "-exit", "analyze.tcl"]
        assert (cwd / "global_timing.v").read_text().count("input i") == 4
        representatives = first
        (cwd / "measurements.tsv").write_text(
            "endpoint\tarrival_ns\trequired_ns\tslack_ns\n" +
            "".join(
                f"o{i}\t{sum(row.arcs_ns)}\t"
                f"{row.required_ns-row.launch_ns}\t"
                f"{row.required_ns-row.launch_ns-sum(row.arcs_ns)}\n"
                for i, row in enumerate(representatives)
            )
        )
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(sta.subprocess, "run", engine)
    values = run_event_checks(first + second, tmp_path)
    assert len(values) == 8
    assert values[4]["path"] == "q"
    assert values[4]["event"] == "q-first"
    assert values[4]["slack_ns"] == values[0]["slack_ns"]
    assert len((tmp_path / "measurements.tsv").read_text().splitlines()) == 9
    assert (tmp_path / "opensta.log").read_text().splitlines()[-1] == (
        "EmuFlow exact path-bundle compression "
        "original_checks=8 representative_checks=4"
    )


def test_comparison_checks_each_path_and_event():
    reference = {"timing_scope": "cross-fpga-subset", "paths": [{
        "path": "p", "system_delay_bound_ns": 31.5,
        "target_required_time_ns": 18, "runtime_required_time_ns": 100,
        "target_clock_slack_bound_ns": -13.5, "runtime_clock_slack_bound_ns": 68.5}]}
    values = [{"path": r.path, "role": r.role, "event": r.event,
               "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
               "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in example()]
    assert compare_system_timing(values, reference)["status"] == "pass"
    values[0]["slack_ns"] = -.5
    assert compare_system_timing(values, reference)["status"] == "fail"
    values[2]["arrival_ns"] += 1
    with pytest.raises(ValidationError, match="disagrees"):
        compare_system_timing(values, reference)


@pytest.mark.parametrize("damage", ["nan", "duplicate", "orphan", "bad-reference"])
def test_comparison_rejects_corrupt_evidence(damage):
    rows = example()
    values = [{"path": r.path, "role": r.role, "event": r.event,
               "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
               "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in rows]
    reference = {"timing_scope": "cross-fpga-subset", "paths": [{
        "path": "p", "system_delay_bound_ns": 31.5,
        "target_required_time_ns": 18, "runtime_required_time_ns": 100,
        "target_clock_slack_bound_ns": -13.5, "runtime_clock_slack_bound_ns": 68.5}]}
    if damage == "nan":
        values[0]["slack_ns"] = float("nan")
    elif damage == "duplicate":
        values.append(values[0])
    elif damage == "orphan":
        values[0]["path"] = "unknown"
    else:
        reference["paths"].append(reference["paths"][0])
    with pytest.raises(ValidationError):
        compare_system_timing(values, reference)




@pytest.mark.parametrize("saved", [False, True])
def test_standalone_phase7c_never_calls_python_composer(tmp_path, monkeypatch, saved):
    from tests.test_phase7c import Phase7CTest
    from emuflow.cross_layer_timing import build_cross_layer_timing_contract
    from emuflow.io import write_json, read_json
    from emuflow.phase7c import run_phase7c
    import emuflow.global_sta as sta
    f = Phase7CTest(); f.setUp()
    for name, value in {"schedule": f.schedule, "platform": f.platform.to_dict(),
                        "physical": f._physical_summary(), "routes": f.routes, **f.reports}.items():
        write_json(tmp_path / f"{name}.json", value)
    write_json(tmp_path / "manifest.json", {"clock_periods": {"clk": 20.0}})
    write_json(tmp_path / "cross_layer_timing.json",
               build_cross_layer_timing_contract(f.routes, f.schedule))
    def forbidden(*args, **kwargs):
        raise AssertionError("original Python timing calculator was called")
    monkeypatch.setattr("tests.system_timing_oracle.build_system_timing", forbidden)
    monkeypatch.setattr("tests.system_timing_oracle.compare_system_timing", forbidden)
    assert not hasattr(sta, "adopt_opensta_results")
    def bind(*args, metadata, **kwargs):
        metadata.update(paths={"p": {"path_scope": "cross-fpga",
            "physical_logic_segments_cone_bound": False}}, source_binding=None,
            compressed_representative_paths=1)
        return example()
    monkeypatch.setattr(sta, "bind_physical_checks", bind)
    def engine(checks, directory, executable, *, verify_arcs):
        assert not verify_arcs
        return [{"path": r.path, "role": r.role, "event": r.event,
                 "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
                 "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in checks]
    if saved:
        def read_saved(path, checks):
            assert path == tmp_path / "saved/measurements.json"
            return engine(checks, None, None, verify_arcs=False)
        monkeypatch.setattr(sta, "run_event_checks", forbidden)
        monkeypatch.setattr("emuflow.global_measurements.read_native_measurements", read_saved)
    else:
        monkeypatch.setattr(sta, "run_event_checks", engine)
    monkeypatch.setattr(sta, "read_engine_identity", lambda p: {"name": "test-sta"})
    run_phase7c(*(tmp_path / f"{n}.json" for n in
                 ("schedule", "platform", "phase3", "phase4", "phase5", "phase6")),
                tmp_path / "out", physical_summary_path=tmp_path / "physical.json",
                routes_path=tmp_path / "routes.json",
                global_sta_results_dir=tmp_path / "saved" if saved else None,
                allow_legacy_phase5_generation=True)
    report = read_json(tmp_path / "out/phase7c_report.json")
    assert "system_timing" not in report and "system_timing_summary" in report
    assert not (tmp_path / "out/system_timing.json").exists()
    assert not (tmp_path / "out/physical_summary.json").exists()
    import json
    storage = json.loads((tmp_path / "out/qor_report.json").read_text())
    assert storage["schema"] == "emuflow.qor-report-storage/v1"
    assert (tmp_path / "out" / storage["artifact"]).is_file()
    assert (tmp_path / "out/qor_report.json").stat().st_size < (tmp_path / "out/phase7c_report.json").stat().st_size
    timing = read_json(tmp_path / "out/qor_report.json")["timing"]
    assert timing["global_opensta"]["execution"] == "standalone"
    assert "cross_checker" not in timing["global_opensta"]
    assert timing["target_clock"]["worst_slack_bound_ns"] == -13.5
    assert timing["runtime_clock"]["tns_bound_ns"] == 0


@pytest.mark.parametrize("late", [False, True])
def test_standalone_report_preserves_event_gate_without_comparison(late, monkeypatch):
    import emuflow.global_sta as sta
    def forbidden(*args, **kwargs):
        raise AssertionError("comparison must not run")
    monkeypatch.setattr("tests.system_timing_oracle.compare_system_timing", forbidden)
    rows = [{"path": r.path, "role": r.role, "event": r.event,
             "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
             "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in example()]
    if late:
        rows[0]["slack_ns"] = -1.0
    metadata = {"paths": {"p": {"path_scope": "cross-fpga", "physical_logic_segments_cone_bound": True}},
                "source_binding": None, "compressed_representative_paths": 1}
    report = sta.build_opensta_timing(
        {
            "virtual_dut_clocks": {"clk": {"nominal_period_ns": 100}},
            "virtual_dut_clock": {"nominal_period_ns": 100},
        },
        metadata,
        rows,
    )
    assert report["status"] == ("fail" if late else "pass")
    assert report["global_opensta"]["event_failures"] == int(late)
    assert report["path_exactness"]["cone_bound_logic_paths"] == 1
    assert report["target_clock"]["tns_bound_ns"] == -13.5
    with pytest.raises(ValidationError):
        sta.build_opensta_timing(
            {
                "virtual_dut_clocks": {"clk": {"nominal_period_ns": 100}},
                "virtual_dut_clock": {"nominal_period_ns": 100},
            },
            metadata,
            [r for r in rows if r["role"] != "runtime"],
        )


@pytest.mark.parametrize("indexed_metadata", [False, True])
@pytest.mark.parametrize("repeated_complete_bundle", [False, True])
def test_standalone_report_rejects_interleaved_measurements(
        indexed_metadata, repeated_complete_bundle):
    import emuflow.global_sta as sta

    rows = [
        {"path": "p", "role": "target", "event": "capture",
         "arrival_ns": 3.0, "required_ns": 10.0, "slack_ns": 7.0},
        {"path": "q", "role": "target", "event": "capture",
         "arrival_ns": 4.0, "required_ns": 10.0, "slack_ns": 6.0},
        {"path": "p", "role": "runtime", "event": "capture",
         "arrival_ns": 3.0, "required_ns": 100.0, "slack_ns": 97.0},
        {"path": "q", "role": "runtime", "event": "capture",
         "arrival_ns": 4.0, "required_ns": 100.0, "slack_ns": 96.0},
    ]
    if repeated_complete_bundle:
        rows = [rows[index] for index in (0, 2, 1, 3, 0, 2)]
    metadata = {
        "paths": {
            path: {
                "path_scope": "cross-fpga",
                "physical_logic_segments_cone_bound": False,
            }
            for path in ("p", "q")
        },
        "source_binding": None,
        "compressed_representative_paths": 2,
    }
    if indexed_metadata:
        builder = _PathMetadataBuilder()
        for path, info in metadata["paths"].items():
            builder.append(path, representative_path=path,
                           clock_domain=None, target_period_ns=10.0, **info)
        metadata["paths"] = builder.finish()
    with pytest.raises(ValidationError, match="contiguous"):
        sta.build_opensta_timing(
            {"virtual_dut_clocks": {"clk": {"nominal_period_ns": 100}},
             "virtual_dut_clock": {"nominal_period_ns": 100}},
            metadata,
            rows,
        )


def test_only_opensta_engine_is_exposed():
    import inspect
    from emuflow.cli import _build_parser
    from emuflow.phase7c import run_phase7c
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    for fn in (run_phase7c, run_multi_fpga_flow):
        assert inspect.signature(fn).parameters["global_timing_engine"].default == "opensta"
    parser = _build_parser()
    commands = [["multi-fpga", "compile", "--out", "out", "--platform", "p"],
                ["phase7c", "--out", "out", "--schedule", "s", "--platform", "p",
                 "--phase3-report", "a", "--phase4-report", "b", "--phase5-report", "c", "--phase6-report", "d"]]
    for cmd in commands:
        assert parser.parse_args(cmd).global_timing_engine == "opensta"
        with pytest.raises(SystemExit):
            parser.parse_args(cmd + ["--global-timing-engine", "python"])


def test_physical_binding_uses_raw_measurements():
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.runtime import build_virtual_runtime
    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(fixture.platform)
    metadata = {}
    checks = bind_physical_checks(build_virtual_runtime(fixture.schedule, fixture.platform),
                                  routes, fixture.schedule, physical, fixture.platform,
                                  metadata=metadata)
    assert isinstance(checks, _IndexedEventChecks)
    assert isinstance(metadata["paths"], _IndexedPathMetadata)
    assert list(metadata["paths"]) == sorted(metadata["paths"])
    assert metadata["paths"][next(iter(metadata["paths"]))]["path_scope"] == "cross-fpga"
    assert sum(c.role == "target" for c in checks) == 1
    assert sum(c.role == "tx" for c in checks) == len(fixture.schedule["entries"])
    assert next(c for c in checks if c.role == "target").arcs_ns[-2:] == (7.0, 0.0)
    del physical["board_link_timing"]
    assert bind_physical_checks(build_virtual_runtime(fixture.schedule, fixture.platform),
                                routes, fixture.schedule, physical, fixture.platform) == checks


def test_indexed_metadata_produces_fixed_width_final_timing_paths():
    builder = _PathMetadataBuilder()
    for path in ("path-a", "path-b"):
        builder.append(
            path,
            representative_path=path,
            path_scope="cross-fpga",
            clock_domain="clk",
            target_period_ns=10.0,
            physical_logic_segments_cone_bound=False,
        )
    metadata = {
        "paths": builder.finish(),
        "source_binding": None,
        "compressed_representative_paths": 2,
    }
    measurements = [
        {
            "path": path,
            "role": role,
            "event": "capture",
            "arrival_ns": 3.0,
            "required_ns": required,
            "slack_ns": required - 3.0,
        }
        for path in ("path-b", "path-a")
        for role, required in (("target", 10.0), ("runtime", 100.0))
    ]

    timing = build_opensta_timing(
        {
            "virtual_dut_clocks": {"clk": {"nominal_period_ns": 100.0}},
            "virtual_dut_clock": {"nominal_period_ns": 100.0},
        },
        metadata,
        measurements,
    )

    assert isinstance(timing["paths"], _IndexedSystemTimingPaths)
    assert timing["paths"].typed_payload_bytes == 2 * 5 * 8
    assert [row["path"] for row in timing["paths"]] == ["path-a", "path-b"]
    assert timing["target_clock"]["worst_slack_bound_ns"] == 7.0
    assert timing["runtime_clock"]["tns_bound_ns"] == 0.0


def test_indexed_logic_segments_are_exact_for_checks_and_system_timing(tmp_path):
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.io import write_json
    from emuflow.logic_segment_timing_storage import (
        logic_segment_timing_reference,
        write_logic_segment_timing_storage,
    )
    from emuflow.physical_summary_storage import (
        publish_logic_segment_timing_store,
        read_physical_summary,
    )
    from emuflow.runtime import build_virtual_runtime
    from tests.system_timing_oracle import build_system_timing

    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    runtime, routes, phase5 = fixture._system_inputs()
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(
        fixture.platform
    )
    inline_metadata = {}
    inline_checks = bind_physical_checks(
        build_virtual_runtime(fixture.schedule, fixture.platform),
        routes,
        fixture.schedule,
        physical,
        fixture.platform,
        metadata=inline_metadata,
    )
    inline_timing = build_system_timing(
        runtime,
        routes,
        fixture.schedule,
        phase5,
        physical,
        fixture.platform,
        semantic_contract=fixture.assignment["semantic_contract"],
    )

    references = {}
    for fpga, database in physical["logic_segment_timing"].items():
        database["system_routes_sha256"] = "a" * 64
        database["tdm_schedule_sha256"] = "b" * 64
        source = tmp_path / fpga / "logic-segment-timing.json"
        header = {
            key: value for key, value in database.items()
            if key not in {"segments", "unsupported_member_paths", "unmeasured_segments"}
        }
        view = write_logic_segment_timing_storage(
            source,
            header=header,
            segments=database["segments"],
            unsupported_member_paths=database.get("unsupported_member_paths", ()),
            unmeasured_segments=database.get("unmeasured_segments", ()),
        )
        view.close()
        references[fpga] = logic_segment_timing_reference(
            source, relative_to=tmp_path
        )
    stored_physical = dict(physical)
    stored_physical["logic_segment_timing"] = references
    first_database = next(iter(physical["logic_segment_timing"].values()))
    stored_physical.setdefault("design", first_database["design"])
    stored_physical.setdefault("platform", first_database["platform"])
    stored_physical["logic_segment_system_routes_sha256"] = "a" * 64
    stored_physical["logic_segment_tdm_schedule_sha256"] = "b" * 64
    semantic = next(iter(physical["logic_segment_timing"].values())).get(
        "semantic_contract_sha256"
    )
    if semantic is not None:
        stored_physical["logic_segment_semantic_contract_sha256"] = semantic
    write_json(tmp_path / "physical-summary.json", stored_physical)
    indexed_physical = read_physical_summary(tmp_path / "physical-summary.json")
    indexed_metadata = {}
    indexed_checks = _reference_physical_checks(
        build_virtual_runtime(fixture.schedule, fixture.platform),
        routes,
        fixture.schedule,
        indexed_physical,
        fixture.platform,
        metadata=indexed_metadata,
    )
    indexed_timing = build_system_timing(
        runtime,
        routes,
        fixture.schedule,
        phase5,
        indexed_physical,
        fixture.platform,
        semantic_contract=fixture.assignment["semantic_contract"],
    )

    assert indexed_checks == inline_checks
    assert dict(indexed_metadata["paths"]) == dict(inline_metadata["paths"])
    assert indexed_timing == inline_timing
    roles = {row.role for row in indexed_checks}
    assert {"tx", "target", "runtime", "commit"} <= roles
    indexed_slacks = [
        row["runtime_clock_slack_bound_ns"] for row in indexed_timing["paths"]
    ]
    inline_slacks = [
        row["runtime_clock_slack_bound_ns"] for row in inline_timing["paths"]
    ]
    assert min(indexed_slacks) == min(inline_slacks)
    assert sum(min(0.0, value) for value in indexed_slacks) == sum(
        min(0.0, value) for value in inline_slacks
    )


def test_indexed_logic_segments_expand_compressed_members_exactly(tmp_path):
    import copy
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.io import write_json
    from emuflow.logic_segment_timing_storage import (
        logic_segment_timing_reference,
        write_logic_segment_timing_storage,
    )
    from emuflow.physical_summary_storage import read_physical_summary
    from emuflow.runtime import build_virtual_runtime

    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    routes = copy.deepcopy(routes)
    routes["timing"]["paths"][0]["compressed_path_ids"] = [
        "path0", "path0-member",
    ]
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(
        fixture.platform
    )
    for database in physical["logic_segment_timing"].values():
        additions = []
        for segment in database["segments"]:
            additions.append({
                **segment,
                "id": f"{segment['id']}-member",
                "member_path": "path0-member",
                "static_exact_segment_id": (
                    f"{segment['static_exact_segment_id']}-member"
                ),
            })
        database["segments"].extend(additions)
        database["coverage"].update({
            "segments": len(database["segments"]), "member_paths": 2,
        })
    runtime = build_virtual_runtime(fixture.schedule, fixture.platform)
    inline_metadata = {}
    inline_checks = bind_physical_checks(
        runtime, routes, fixture.schedule, physical, fixture.platform,
        metadata=inline_metadata,
    )

    references = {}
    for fpga, database in physical["logic_segment_timing"].items():
        database["system_routes_sha256"] = "a" * 64
        database["tdm_schedule_sha256"] = "b" * 64
        selector = tmp_path / fpga / "logic-segment-timing.json"
        header = {
            key: value for key, value in database.items()
            if key not in {"segments", "unsupported_member_paths"}
        }
        view = write_logic_segment_timing_storage(
            selector,
            header=header,
            segments=database["segments"],
            unsupported_member_paths=database["unsupported_member_paths"],
        )
        view.close()
        references[fpga] = logic_segment_timing_reference(
            selector, relative_to=tmp_path
        )
    stored = dict(physical)
    stored["logic_segment_timing"] = references
    first_database = next(iter(physical["logic_segment_timing"].values()))
    stored.setdefault("design", first_database["design"])
    stored.setdefault("platform", first_database["platform"])
    stored["logic_segment_system_routes_sha256"] = "a" * 64
    stored["logic_segment_tdm_schedule_sha256"] = "b" * 64
    stored["logic_segment_semantic_contract_sha256"] = next(
        iter(physical["logic_segment_timing"].values())
    )["semantic_contract_sha256"]
    write_json(tmp_path / "physical-summary.json", stored)
    with read_physical_summary(tmp_path / "physical-summary.json") as indexed:
        indexed_metadata = {}
        indexed_checks = _reference_physical_checks(
            runtime, routes, fixture.schedule, indexed, fixture.platform,
            metadata=indexed_metadata,
        )
    assert indexed_checks == inline_checks
    assert set(indexed_metadata["paths"]) == {"path0", "path0-member"}
    assert {row.role for row in indexed_checks} >= {
        "tx", "target", "runtime", "commit",
    }


def test_physical_binding_does_not_reindex_schedule_entries():
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.runtime import build_virtual_runtime

    class SinglePassEntries(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("global STA rebuilt the schedule entry map")
            return super().__iter__()

    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    runtime = build_virtual_runtime(fixture.schedule, fixture.platform)
    schedule = dict(fixture.schedule)
    schedule["entries"] = SinglePassEntries(fixture.schedule["entries"])
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(
        fixture.platform
    )

    checks = bind_physical_checks(
        runtime,
        routes,
        schedule,
        physical,
        fixture.platform,
    )

    assert checks
    assert schedule["entries"].iterations == 1


def test_indexed_physical_binding_streams_routes_paths_and_schedule(tmp_path):
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.runtime import build_virtual_runtime
    from emuflow.system_route_storage import (
        SystemRouteSequence,
        read_system_routes,
        write_system_route_storage,
    )
    from emuflow.tdm_schedule_storage import (read_tdm_schedule)

    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    runtime = build_virtual_runtime(fixture.schedule, fixture.platform)
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(
        fixture.platform
    )
    expected = bind_physical_checks(
        runtime, routes, fixture.schedule, physical, fixture.platform
    )

    route_path = tmp_path / "routes.json"
    schedule_path = tmp_path / "schedule.json"
    write_system_route_storage(route_path, routes)
    write_tdm_schedule_storage(schedule_path, fixture.schedule)
    indexed_routes = read_system_routes(route_path)
    indexed_schedule = read_tdm_schedule(schedule_path)
    route_records = indexed_routes["routes"]
    timing_records = indexed_routes["timing"]["paths"]

    with mock.patch.object(
        SystemRouteSequence,
        "__iter__",
        side_effect=AssertionError("decoded the complete route relation"),
    ):
        actual = bind_physical_checks(
            runtime,
            indexed_routes,
            indexed_schedule,
            physical,
            fixture.platform,
        )

    assert actual == expected
    assert list.__len__(route_records) == 0
    assert list.__len__(timing_records) == 0


def test_indexed_timing_stream_delegates_path_order_to_route_store(tmp_path):
    import copy
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.system_route_storage import (
        SystemRouteSequence,
        read_system_routes,
        write_system_route_storage,
    )
    from emuflow.tdm import open_tdm_schedule_timing_path_stream
    from emuflow.tdm_schedule_storage import (read_tdm_schedule)

    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    routes = copy.deepcopy(routes)
    base = routes["timing"]["paths"][0]
    routes["timing"]["paths"] = [
        {**base, "path": "path-z"},
        {**base, "path": "path-a"},
    ]
    route_path = tmp_path / "routes.json"
    schedule_path = tmp_path / "schedule.json"
    write_system_route_storage(route_path, routes)
    write_tdm_schedule_storage(schedule_path, fixture.schedule)
    indexed_routes = read_system_routes(route_path)
    indexed_schedule = read_tdm_schedule(schedule_path)
    timing_records = indexed_routes["timing"]["paths"]

    assert timing_records.ordered("path")[0]["path"] == "path-a"
    with mock.patch.object(
        SystemRouteSequence,
        "__iter__",
        side_effect=AssertionError("decoded the complete route relation"),
    ):
        with open_tdm_schedule_timing_path_stream(
            indexed_routes,
            fixture.platform,
            indexed_schedule,
            path_order="path",
        ) as records:
            observed = [
                (record["path"], record["_cut_demands"])
                for record in records
            ]

    assert observed == [
        ("path-a", ["d000001", "d000000"]),
        ("path-z", ["d000001", "d000000"]),
    ]
    assert list.__len__(timing_records) == 0


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENSTA"), reason="real OpenSTA not configured")
def test_real_opensta_fixed_events_and_late_tx(tmp_path):
    rows = example()
    measurements = run_event_checks(rows, tmp_path, os.environ["EMUFLOW_TEST_OPENSTA"])
    for row, check in zip(measurements, rows):
        arrival = check.launch_ns + sum(check.arcs_ns)
        assert row["arrival_ns"] == pytest.approx(arrival, abs=1e-3)
        assert row["required_ns"] == pytest.approx(check.required_ns, abs=1e-3)
        assert row["slack_ns"] == pytest.approx(check.required_ns-arrival, abs=1e-3)
    late = rows.copy()
    late[0] = EventCheck("p", "tx", "first", 0, (4.5,), 4)
    result = run_event_checks(late, tmp_path / "late", os.environ["EMUFLOW_TEST_OPENSTA"])
    assert result[0]["slack_ns"] == pytest.approx(-.5, abs=1e-3)
    assert result[3]["slack_ns"] > 0


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENTIMER"), reason="OpenTimer driver not configured")
@pytest.mark.parametrize("extra_paths", [0, 256])
def test_real_opentimer_fixed_events(tmp_path, extra_paths):
    rows = example()
    rows[0] = EventCheck("p", "tx", "first", 0, (4.5,), 4)
    for i in range(extra_paths):
        # Mixed local/event observations, arc-chain lengths and target periods.
        arcs = tuple((i+j+1)/37 for j in range(1+i % 5))
        for role, deadline in (("target", 10+i % 7), ("runtime", 1000)):
            rows.append(EventCheck(f"p{i}", role, "capture", i/8, arcs, deadline))
    export_event_checks(rows, tmp_path)
    subprocess.run([os.environ["EMUFLOW_TEST_OPENTIMER"], str(len(rows))],
                   cwd=tmp_path, check=True)
    result = read_measurements(tmp_path / "opentimer-measurements.tsv", rows)
    for measured, row in zip(result, rows):
        arrival = row.launch_ns+sum(row.arcs_ns)
        assert measured["arrival_ns"] == pytest.approx(arrival, abs=1e-3)
        assert measured["required_ns"] == pytest.approx(row.required_ns, abs=1e-3)
        assert measured["slack_ns"] == pytest.approx(row.required_ns-arrival, abs=1e-3)
    assert result[0]["slack_ns"] < 0 < result[3]["slack_ns"]


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENTIMER"), reason="OpenTimer driver not configured")
def test_real_opentimer_from_physical_binding(tmp_path):
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from tests.system_timing_oracle import build_system_timing
    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    runtime, routes, phase5 = fixture._system_inputs()
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(fixture.platform)
    checks = bind_physical_checks(runtime, routes, fixture.schedule, physical, fixture.platform)
    export_event_checks(checks, tmp_path)
    from emuflow.global_event_store import event_model_directory
    model = event_model_directory(tmp_path)
    subprocess.run([os.environ["EMUFLOW_TEST_OPENTIMER"], str(len(checks))],
                   cwd=model, check=True)
    measured = read_measurements(model / "opentimer-measurements.tsv", checks)
    reference = build_system_timing(runtime, routes, fixture.schedule, phase5, physical,
                                    fixture.platform,
                                    semantic_contract=fixture.assignment["semantic_contract"])
    assert compare_system_timing(measured, reference)["status"] == "pass"
