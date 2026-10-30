"""Native pin owners against a bounded oracle and real fixed P/N inventories."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import test_chimew_phase6 as fixtures
from tests.chimew_pin_reference import build_chimew_phase6_pin_plan as reference
from tests.chimew_fixed_wiring_reference import build_fixed_wiring_chimew_electrical_map
from tests.native_build import emuflow_native
from tests.schedule_store_fixture import write_tdm_schedule_storage
from emuflow.platform.default_platform import research_platform_path
from emuflow.platform.platform import Platform
from emuflow.transport.chimew.chimew_fixed_wiring import FixedWiringChannels
from emuflow.transport.chimew.chimew_bank_channel_storage import BankChannelInputView, write_chimew_bank_channel_storage
from emuflow.transport.pin_artifacts_native import _board_sources, build_pin_artifacts
from emuflow.transport.pin_plan_storage import PinPlanEntrySequence, read_pin_plan
from emuflow.transport.position_hints_storage import PositionHintEntrySequence, read_position_hints
from emuflow.transport.chimew.chimew_binding_storage import ChimewBindingEntrySequence, read_chimew_binding
from emuflow.scheduling.tdm_schedule_storage import TypedScheduleEntrySequence


@pytest.fixture(params=["parallel", "fixed2", "fixed4"])
def pin_case(request):
    fixtures.ChimewPhase6AdapterTest.setUpClass()
    case = fixtures.ChimewPhase6AdapterTest()
    case.setUp()
    if request.param == "parallel":
        return case
    case.platform = Platform.load(research_platform_path(int(request.param[-1])))
    link = case.platform.links[0]
    a, b = link.endpoints
    case.schedule.update(platform=case.platform.name, metrics={"frame_slots":8}, bank_allocations=[
        {"link":other.id,"from":source,"to":sink,"bank_count":count,
         "wires_per_bank":other.wires_per_bank,"lane_offset":offset,
         "lanes":count*other.wires_per_bank}
        for other in case.platform.links
        for source,sink,count,offset in [
            (*other.endpoints, other.bank_count if other.id==link.id else 0, 0),
            (*reversed(other.endpoints),0,other.transport_bits_per_cycle_per_direction)]
    ])
    for i,row in enumerate(case.schedule["entries"]):
        row.update(link=link.id, lane=i, slot=i, tdm_ratio=8,
                   **{"from":a,"to":b,"capacity_key":f"{link.id}:{a}:{b}"})
    document = case.assignment_input
    document["platform"] = case.platform.name
    document["domains"][0].update(fpga_a=a,fpga_b=b)
    wire = FixedWiringChannels(case.platform).channel(link.id,0,a,b)
    document["bank_pairs"][0]["bank_a"]["id"] = wire["bank_a"]
    document["bank_pairs"][0]["bank_b"]["id"] = wire["bank_b"]
    for group in document["groups"]:
        group["direction"] = "a_to_b"
        for member in group["members"]:
            member["fanout"]["x"] = 0.0
            member["fanins"][0]["x"] = 100.0
    case.electrical_map = build_fixed_wiring_chimew_electrical_map(case.platform,document,
        channel_lanes={f"channel{i}":{"link":link.id,"physical_lane":i,"direction":"a_to_b"} for i in range(2)},
        fpga_y_bounds=[{"fpga":fpga,"y_min":0.0,"y_max":100.0} for fpga in (a,b)],boarddb_sha256="d"*64)
    return case


def invocation(case,root,bank_input):
    sources = _board_sources(case.platform,root)
    schedule, electrical = root/"schedule.json",root/"electrical.json"
    write_tdm_schedule_storage(schedule,case.schedule)
    electrical.write_text(json.dumps(case.electrical_map))
    sources.update(schedule={"path":str(schedule),"schema":"physical-input-document-v1"},
                   electrical={"path":str(electrical),"schema":"physical-input-document-v1"},
                   bank_input={"path":str(bank_input.manifest_path),"schema":"chimew-bank-input-selector-v1"})
    request = root/"request.json"
    request.write_text(json.dumps({"schema":"emuflow.chimew-pin-artifacts-input/v1","region_count":31,"sources":sources}))
    return request,sources


def execute(request,output,receipt):
    return subprocess.run([str(emuflow_native()),"--report",str(receipt),"phase6","build-pin-artifacts",str(request),str(output)],capture_output=True,text=True)


def test_native_projection_and_independent_validation_match_reference(pin_case,tmp_path):
    case = pin_case
    with write_chimew_bank_channel_storage(tmp_path/"bank_input.json",case.assignment_input) as bank:
        expected = reference(case.schedule,case.platform,bank,case.electrical_map,
                             bank_channel_report_output_path=tmp_path/"reference_report.json")
        expected["bank_channel_report"].close()
        request,_ = invocation(case,tmp_path,bank)
        output,receipt = tmp_path/"native",tmp_path/"operation.json"
        result = execute(request,output,receipt)
        assert result.returncode==0, receipt.read_text() if receipt.exists() else result.stderr
        for key,read in [("position_hints",read_position_hints),("pin_plan",read_pin_plan),("electrical_binding",read_chimew_binding)]:
            assert read(output/(key+".json"),materialize=True)==expected[key]
        assert json.loads((output/"pin_artifacts.json").read_text())["validation"]==expected["validation"]
        assert len(json.loads(receipt.read_text())["outputs"])==9


def test_host_does_not_iterate_schedule_group_pin_or_binding_records(pin_case,tmp_path):
    case = pin_case
    with write_chimew_bank_channel_storage(tmp_path/"bank_input.json",case.assignment_input) as bank:
        _,sources = invocation(case,tmp_path,bank)
        with patch.object(TypedScheduleEntrySequence,"__iter__",side_effect=AssertionError("Python schedule walk")), \
             patch.object(BankChannelInputView,"iter_normalized_groups_by_id",side_effect=AssertionError("Python group walk")), \
             patch.object(PinPlanEntrySequence,"__iter__",side_effect=AssertionError("Python plan walk")), \
             patch.object(PositionHintEntrySequence,"__iter__",side_effect=AssertionError("Python position walk")), \
             patch.object(ChimewBindingEntrySequence,"__iter__",side_effect=AssertionError("Python binding walk")):
            result = build_pin_artifacts(Path(sources["schedule"]["path"]),case.platform,bank.manifest_path,
                                         Path(sources["electrical"]["path"]),tmp_path/"native")
            result["bank_channel_report"].close()
            assert result["validation"]["status"]=="pass"


def test_receipt_cannot_overwrite_a_source_or_selector_payload(pin_case,tmp_path):
    with write_chimew_bank_channel_storage(tmp_path/"bank_input.json",pin_case.assignment_input) as bank:
        request,sources = invocation(pin_case,tmp_path,bank)
        targets = [request,bank.store_path,*[Path(row["path"]) for row in sources.values()]]
        schedule = Path(sources["schedule"]["path"])
        targets.append(schedule.with_name(json.loads(schedule.read_text())["artifact"]))
        for target in targets:
            before = target.read_bytes()
            result = execute(request,tmp_path/"native",target)
            assert result.returncode != 0
            assert "aliases" in result.stderr
            assert target.read_bytes()==before
            assert not (tmp_path/"native").exists()


@pytest.mark.parametrize("reader,schema", [
    (read_pin_plan,"emuflow.placement-aware-pin-plan/v1"),
    (read_position_hints,"emuflow.signal-position-hints/v1"),
    (read_chimew_binding,"emuflow.chimew-phase6-electrical-binding/v3"),
])
def test_current_readers_reject_retired_inline_documents(tmp_path,reader,schema):
    from emuflow.common.errors import ValidationError
    path = tmp_path/"inline.json"
    path.write_text(json.dumps({"schema":schema,"entries":[]}))
    before = path.read_bytes()
    with pytest.raises(ValidationError):
        reader(path)
    assert path.read_bytes()==before
    assert list(tmp_path.iterdir())==[path]


def test_builder_requires_current_schedule_store_and_preserves_raw_source(pin_case,tmp_path):
    with write_chimew_bank_channel_storage(tmp_path/"bank_input.json",pin_case.assignment_input) as bank:
        request,sources = invocation(pin_case,tmp_path,bank)
        schedule = Path(sources["schedule"]["path"])
        schedule.write_text(json.dumps(pin_case.schedule))
        before = schedule.read_bytes()
        output,receipt = tmp_path/"native",tmp_path/"operation.json"
        result = execute(request,output,receipt)
        assert result.returncode != 0
        assert "selector envelope differs" in receipt.read_text()
        assert schedule.read_bytes()==before and not output.exists()
        assert not list(tmp_path.glob(".chimew-pins-*"))
