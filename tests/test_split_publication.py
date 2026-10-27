"""Independent split validation precedes source-bound native publication."""
from tests.phase3_store_fixture import write_assignment_index_fixture

from unittest.mock import patch
import os
import shutil

import pytest

from emuflow import split_netlist_storage as split
from emuflow import split_overlay as owner
from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
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
    write_assignment_index_fixture(assignment, case.ir, case.assignment)
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
    validate = owner.bind_split_sources
    publish = owner.publish_sqlite_store
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
            owner, "bind_split_sources", side_effect=change_after_validation
        ),
        patch.object(owner, "publish_sqlite_store", side_effect=native_publish),
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


@pytest.mark.parametrize("touch_metadata", [False, True])
def test_exact_owner_is_borrowed_without_alias_or_copy(tmp_path,touch_metadata):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    from tests.schedule_reference import build_tdm_schedule
    from emuflow.native_tools import native_file_generation
    case=StaticExactCombinationalCutPartitionTest();case.setUp()
    _,value=case._exact_artifacts(dependent_return=True)
    ir=tmp_path/'ir.sqlite';assignment=tmp_path/'assignment.sqlite';selector=tmp_path/'schedule.json'
    write_compact_emuir(ir,case.ir.to_dict());write_assignment_index_fixture(assignment,case.ir,value)
    write_tdm_schedule_storage(selector,build_tdm_schedule(case._exact_routes(value),case.platform))
    schedule=read_tdm_schedule(selector);source=tmp_path/'assignment.exact.bin'
    before=source.read_bytes();generation=native_file_generation(source)
    output=tmp_path/'phase6/split.sqlite';observed=[]
    bind=owner.bind_split_sources;publish=owner.publish_sqlite_store
    def check(*args,**kwargs):
        refs=bind(*args,**kwargs)
        if touch_metadata:source.chmod(source.stat().st_mode ^ 0o100)
        return refs
    def commit(*args,**kwargs):
        observed.extend(kwargs['sources']);return publish(*args,**kwargs)
    with patch.object(owner,'bind_split_sources',side_effect=check), \
         patch.object(owner,'publish_sqlite_store',side_effect=commit), \
         patch.object(os,'link',side_effect=AssertionError('borrowed owner must not be linked')), \
         patch.object(shutil,'copyfile',side_effect=AssertionError('borrowed owner must not be copied')):
        def produce():return split.write_split_netlists_sqlite(output,ir,assignment,schedule,
            expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
        if touch_metadata:
            with pytest.raises(ValidationError,match='generation changed'):produce()
            assert not output.exists() and not observed
        else:
            produce();assert native_file_generation(source)==generation
            assert not output.with_name('assignment.exact.bin').exists()
            exact=[item for item in observed if item.schema=='phase3-exact-data-v2']
            assert len(exact)==1 and exact[0].path==source.resolve()
    assert source.read_bytes()==before
    assert not list(output.parent.glob('.*.tmp'))
