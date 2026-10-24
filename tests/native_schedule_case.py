"""Small current-contract schedule fixtures; real-design validation uses NVDLA."""
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
from tests.native_build import emuflow_native


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
