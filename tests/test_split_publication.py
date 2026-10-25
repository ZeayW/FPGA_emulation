"""Independent split validation precedes source-bound native publication."""

from unittest.mock import patch
import os

import pytest

from emuflow import split_netlist_storage as split
from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.phase3_assignment_index import write_phase3_assignment_index
from emuflow.tdm_schedule_storage import read_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage


@pytest.mark.parametrize("changed", ["none", "ir", "assignment", "schedule"])
def test_split_publication_keeps_prior_generation_when_an_input_changes(
    tmp_path, changed
):
    from tests.test_phase6 import Phase6Test

    case = Phase6Test()
    case.setUp()
    ir, assignment, selector = (
        tmp_path / name for name in ("ir.sqlite", "assignment.sqlite", "schedule.json")
    )
    write_compact_emuir(ir, case.ir.to_dict())
    write_phase3_assignment_index(assignment, case.ir, case.assignment)
    write_tdm_schedule_storage(selector, case.schedule)
    schedule = read_tdm_schedule(selector)
    output = tmp_path / "split.sqlite"

    def produce():
        return split.write_split_netlists_sqlite(
            output,
            ir,
            assignment,
            schedule,
            expected_design=case.ir.value["design"]["name"],
            expected_platform=case.platform.name,
        )

    produce()
    previous = output.read_bytes()
    validate = split.validate_split_netlists_sqlite
    publish = split.publish_sqlite_store
    observed = []

    def change_after_validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        if changed != "none":
            path = {
                "ir": ir,
                "assignment": assignment,
                "schedule": schedule.store_path,
            }[changed]
            path.write_bytes(path.read_bytes() + b"\n")
        return result

    def native_publish(temporary, destination, schema, *, sources):
        observed.extend(source.schema for source in sources)
        return publish(temporary, destination, schema, sources=sources)

    with (
        patch.object(
            split, "validate_split_netlists_sqlite", side_effect=change_after_validation
        ),
        patch.object(split, "publish_sqlite_store", side_effect=native_publish),
    ):
        if changed == "none":
            produce()
        else:
            with pytest.raises(ValidationError, match="generation changed"):
                produce()
    if changed == "none":
        assert set(observed) == {
            "emuir-compact-v2",
            "assignment-index-v4",
            "tdm-schedule-store-v4",
            "tdm-schedule-selector-v4",
        }
    else:
        assert not observed
    assert output.read_bytes() == previous
    assert not list(tmp_path.glob(".split.sqlite.*.tmp"))


@pytest.mark.parametrize("copy_only", [False, True])
@pytest.mark.parametrize("touch_metadata", [False, True])
def test_exact_binary_alias_publication_preserves_content_and_binds_its_final_generation(
    tmp_path, copy_only, touch_metadata
):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    from tests.schedule_reference import build_tdm_schedule

    case = StaticExactCombinationalCutPartitionTest()
    case.setUp()
    _, value = case._exact_artifacts(dependent_return=True)
    schedule_value = build_tdm_schedule(case._exact_routes(value), case.platform)
    ir, assignment, selector = (
        tmp_path / name for name in ("ir.sqlite", "assignment.sqlite", "schedule.json")
    )
    write_compact_emuir(ir, case.ir.to_dict())
    write_phase3_assignment_index(assignment, case.ir, value)
    write_tdm_schedule_storage(selector, schedule_value)
    schedule = read_tdm_schedule(selector)
    source = tmp_path / "assignment.exact.bin"
    before = source.read_bytes()
    output = tmp_path / "phase6" / "split.sqlite"
    publish = split.publish_sqlite_store
    observed = []

    def record_sources(*args, **kwargs):
        observed.extend(kwargs["sources"])
        return publish(*args, **kwargs)

    def link(source, target):
        if copy_only:
            raise OSError("fixture forces the declared cross-filesystem copy path")
        return original_link(source, target)

    original_link = os.link
    validate = split.validate_split_netlists_sqlite

    def validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        if touch_metadata:
            source.chmod(source.stat().st_mode ^ 0o100)
        return result

    with (
        patch.object(split, "publish_sqlite_store", side_effect=record_sources),
        patch.object(split.os, "link", side_effect=link),
        patch.object(split, "validate_split_netlists_sqlite", side_effect=validation),
    ):

        def produce():
            return split.write_split_netlists_sqlite(
                output,
                ir,
                assignment,
                schedule,
                expected_design=case.ir.value["design"]["name"],
                expected_platform=case.platform.name,
            )

        if touch_metadata:
            with pytest.raises(ValidationError, match="generation changed"):
                produce()
            assert source.read_bytes() == before and not output.exists()
            assert not observed and not list(output.parent.glob(".*.tmp"))
            return
        produce()
    target = output.with_name("assignment.exact.bin")
    assert target.read_bytes() == before == source.read_bytes()
    assert os.path.samefile(source, target) == (not copy_only)
    exact = [item for item in observed if item.schema == "phase3-exact-data-v2"]
    assert len(exact) == 1 and exact[0].path == source.resolve()
    assert exact[0].generation == split.native_file_generation(source)
    assert not list(output.parent.glob(".*.tmp"))
