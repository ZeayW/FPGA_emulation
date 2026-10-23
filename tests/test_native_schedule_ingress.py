"""Native RouteStore ingress parity and fail-closed publication contracts."""
import copy
import json
import os
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.platform import RatioDelayModel
from emuflow.system_route_storage import read_system_routes,write_system_route_storage,_demand_from_route
from emuflow.tdm import build_tdm_schedule, _write_native_schedule_board_request
from tests.native_build import emuflow_native,tdm_exact_scheduler


def _case(tmp_path, affine, repeated):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    case=StaticExactCombinationalCutPartitionTest();case.setUp()
    if affine and affine != "tree":
        link=case.platform.links[0]
        case.platform=replace(case.platform,links=(replace(link,data_lanes_per_direction=2,
            tdm_delay_model=RatioDelayModel("linear-ratio-v1",3.2,0.1,64 if affine=="wide-ratio" else 8,256,8),
            **({"bank_count":2,"wires_per_bank":1} if affine=="banks" else {})),))
    _,assignment=case._exact_artifacts(dependent_return=True)
    routes=case._exact_routes(assignment,frame_slots=256 if repeated else 16)
    routes["semantic_contract"]={**routes["semantic_contract"],"indexed":True}
    if affine == "tree":
        from emuflow.routing import normalize_route_constraints
        link=case.platform.links[0]
        case.platform=replace(case.platform,
            fpgas=(*case.platform.fpgas,replace(case.platform.fpgas[0],id="fpga2"),replace(case.platform.fpgas[0],id="fpga3")),
            links=(link,replace(link,id="link_0_2",endpoints=("fpga0","fpga2")),
                   replace(link,id="link_0_3",endpoints=("fpga0","fpga3")),
                   replace(link,id="link_2_1",endpoints=("fpga2","fpga1"))))
        root=next(route for route in routes["routes"] if not route["predecessor_cut_nets"])
        root["sinks"]=["fpga1","fpga3"]
        root["tree_edges"]=[{"link":"link_2_1","from":"fpga2","to":"fpga1"},
                            {"link":"link_0_3","from":"fpga0","to":"fpga3"},
                            {"link":"link_0_2","from":"fpga0","to":"fpga2"}]
        routes["constraints"]=normalize_route_constraints(None,case.platform,frame_slots=16)
        routes["demands"]=[_demand_from_route(route) for route in routes["routes"]]
    if repeated:
        template=next(route for route in routes["routes"] if not route["predecessor_cut_nets"])
        routes["routes"]=[]
        for i in range(40):
            route=copy.deepcopy(template);route.update(id=f"d{i:03d}",net=f"n{39-i:03d}")
            routes["routes"].append(route)
        routes["demands"]=[_demand_from_route(route) for route in routes["routes"]]
    selector=tmp_path/"routes.json";write_system_route_storage(selector,routes)
    return case,read_system_routes(selector)


@pytest.mark.parametrize("affine,repeated",[(False,False),(True,False),(True,True),("banks",False),("tree",False)])
def test_native_ingress_matches_legacy_numeric_model_and_schedule(tmp_path,monkeypatch,affine,repeated):
    native_binary,legacy_binary=emuflow_native(),tdm_exact_scheduler()
    case,routes=_case(tmp_path,affine,repeated)
    reference=build_tdm_schedule(routes,case.platform)
    captures=[];run=subprocess.run
    def record(command,*args,**kwargs):
        if command[0]==str(legacy_binary) and len(command)==3:
            captures.append(Path(command[1]).read_bytes())
        return run(command,*args,**kwargs)
    with patch("emuflow.tdm.NATIVE_EXACT_SCHEDULE_MIN_ROUTES",0), \
         patch("emuflow.native_tools.resolve_native_stage_command",return_value=[str(legacy_binary)]), \
         patch("emuflow.tdm.subprocess.run",side_effect=record):
        legacy=build_tdm_schedule(routes,case.platform)
    assert len(captures)==1
    assert legacy==reference
    bindir=tmp_path/"native"/"bin";bindir.mkdir(parents=True)
    (bindir/"emuflow-native").symlink_to(native_binary)
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT",str(bindir.parent))
    monkeypatch.setattr("emuflow.tdm.NATIVE_EXACT_SCHEDULE_MIN_ROUTES",0)
    with patch("emuflow.tdm._baseline_ratio_hops",side_effect=AssertionError("Python H-sized wire groups")), \
         patch("emuflow.tdm._route_hops",side_effect=AssertionError("Python tree lowering")), \
         patch.object(type(routes["routes"]),"iter_native_exact_records",side_effect=AssertionError("Python hop input cursor")):
        native=build_tdm_schedule(routes,case.platform)
    assert native==reference
    source=native["entries"].native_store_publication_source
    assert source is not None
    assert Path(source["input"]).read_bytes()==captures[0]


def _request(tmp_path,case,routes):
    from emuflow.routing import build_directed_graph
    from emuflow.tdm import _bank_direction_allocations
    constraints=routes["constraints"]
    _,arcs,capacities=build_directed_graph(case.platform,constraints)
    request=tmp_path/"board.bin"
    _write_native_schedule_board_request(request,native_file_generation(routes.store_path),case.platform,constraints,arcs,
        _bank_direction_allocations(routes,case.platform,constraints),sorted(fpga.id for fpga in case.platform.fpgas),
        sorted(link.id for link in case.platform.links),sorted(capacities))
    return request


@pytest.mark.parametrize("corruption",["trailing","truncated","generation","predecessor","cycle","arc","metadata","config_max","alias","aux_alias","report_alias"])
def test_native_ingress_rejects_invalid_sources_without_replacing_outputs(tmp_path,corruption):
    binary=emuflow_native();case,routes=_case(tmp_path,False,False)
    request=_request(tmp_path,case,routes)
    output=tmp_path/"scheduled.bin";auxiliary=output.with_name(output.name+".input")
    output.write_bytes(b"previous-result");auxiliary.write_bytes(b"previous-model")
    command=[str(binary),"phase5","schedule-from-store",str(routes.store_path),str(request),str(output)]
    if corruption=="trailing":request.write_bytes(request.read_bytes()+b"x")
    elif corruption=="truncated":request.write_bytes(request.read_bytes()[:-1])
    elif corruption=="config_max":
        data=bytearray(request.read_bytes());data[84:88]=struct.pack("<I",32);request.write_bytes(data)
    elif corruption=="generation":
        # Retain the producer snapshot: even a byte-identical replacement fails.
        replacement=tmp_path/"replacement.sqlite";replacement.write_bytes(routes.store_path.read_bytes())
        os.replace(replacement,routes.store_path)
    elif corruption=="alias":command[-1]=str(request).removesuffix(".input")
    elif corruption=="aux_alias":
        alias=tmp_path/"aliased.input";request.rename(alias)
        command[-2]=str(alias);command[-1]=str(tmp_path/"aliased")
        saved_alias=alias.read_bytes()
    elif corruption=="report_alias":
        command=[str(binary),"--report",str(auxiliary),*command[1:]]
    else:
        import sqlite3
        with sqlite3.connect(routes.store_path) as connection:
            if corruption=="predecessor":connection.execute("UPDATE route_predecessors SET predecessor_net='unknown'")
            elif corruption=="cycle":connection.execute("UPDATE route_edges SET sink_fpga=source_fpga WHERE route_seq=0")
            elif corruption=="arc":connection.execute("UPDATE route_edges SET link_id='unknown' WHERE route_seq=0")
            else:connection.execute("UPDATE routes SET dependency_level=NULL,combinational_dependency_depth=NULL WHERE seq=0")
        data=bytearray(request.read_bytes());data[12:68]=struct.pack("<3Q4q",*native_file_generation(routes.store_path));request.write_bytes(data)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0,result.stdout
    if corruption in {"alias","aux_alias","report_alias"}:
        assert result.returncode==2
    if corruption=="aux_alias":
        assert alias.read_bytes()==saved_alias
        assert not (tmp_path/"aliased").exists()
    assert output.read_bytes()==b"previous-result"
    assert auxiliary.read_bytes()==b"previous-model"
    assert not list(tmp_path.glob("*.pending.*"))


def test_raw_model_bounds_fail_before_count_allocation(tmp_path):
    binary=emuflow_native();source=tmp_path/"model.bin";output=tmp_path/"result.bin"
    for capacities,routes in [(2**32-1,0),(0,2**32-1)]:
        source.write_bytes(struct.pack("<Q7I",0x3149534D44544545,1,32,1,1,2,capacities,routes))
        result=subprocess.run([str(binary),"phase5","schedule",str(source),str(output)],capture_output=True,text=True)
        assert result.returncode==1
        assert "truncated" in result.stderr
        assert not output.exists()
