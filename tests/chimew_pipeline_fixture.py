"""Explicit small pipeline input facts; not a production flow or NVDLA proof."""
import copy
import hashlib
from pathlib import Path
from emuflow.common.json_io import write_json
from tests.chimew_grouping_fixture import build_chimew_initial_groups
from tests.chimew_refinement_fixture import refine_chimew_groups
from tests.test_chimew_qualification import ChimewQualificationTest
from tests.schedule_store_fixture import write_tdm_schedule_storage
from emuflow_tools.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage
from emuflow_tools.transport.chimew.chimew_qualification import canonical_sha256


def pipeline_inputs(root, *, byte_bound=True, opaque_prefix=b"fixture source "):
    cls=ChimewQualificationTest
    cls.setUpClass()
    try:
        case=cls();case.setUp()
        board={"schema":"emuflow.boarddb/v1","platform":{"name":case.schedule["platform"],"kind":"hardware","description":"pipeline contract"},
            "fpgas":[{"id":name,"part":"fixture","utilization_limit":1.0,"capacity":{"lut":100}} for name in ("A","B")],
            "links":[{"id":"AB","endpoints":["A","B"],"direction":"full_duplex","mode":"parallel","data_lanes_per_direction":2,"fabric_clock_mhz":100.0,"latency_cycles":1}]}
        sources={};digests={}
        if byte_bound:
            for name in ("routing","placement","netlist","architecture","package_pins"):
                path=root/(name+".source");path.write_bytes(opaque_prefix+name.encode()+b"\n");sources[name]=path;digests[name]=hashlib.sha256(path.read_bytes()).hexdigest()
            case.crossings["provenance"]["routing_sha256"]=digests["routing"]
            case.positions["provenance"]["placement_sha256"]=digests["placement"]
            for name in ("placement","netlist","architecture"):
                case.rudy_input["provenance"][name+"_sha256"]=digests[name]
        refined=refine_chimew_groups(case.schedule,case.crossings,build_chimew_initial_groups(case.schedule,case.crossings),case.positions)
        case.bank_input["provenance"].update(grouping_sha256=canonical_sha256(refined),placement_sha256=case.positions["provenance"]["placement_sha256"],architecture_sha256=case.rudy_input["provenance"]["architecture_sha256"])
        paths={name:root/(name+".json") for name in ("schedule","platform","crossings","positions","rudy_input","bank_input","electrical")}
        write_json(paths["platform"],board);write_tdm_schedule_storage(paths["schedule"],case.schedule)
        write_chimew_bank_channel_storage(paths["bank_input"],case.bank_input).close()
        for name in ("crossings","positions","rudy_input"):
            write_json(paths[name],getattr(case,name))
        electrical={"schema":"emuflow.chimew-electrical-channel-map/v2","provider":"source-qualified-boarddb-electrical-channel-map-v1",
            "design":case.schedule["design"],"platform":case.schedule["platform"],
            "provenance":{"producer":"fixture-electrical","producer_version":"1","boarddb_sha256":hashlib.sha256(paths["platform"].read_bytes()).hexdigest(),"package_pin_inventory_sha256":digests.get("package_pins","e"*64)},
            "fpga_y_bounds":[{"fpga":fpga,"y_min":0.0,"y_max":100.0} for fpga in ("A","B")],
            "channels":[{"chimew_channel":f"channel{i}","link":"AB","physical_lane":i,"direction":"either","bank_a":"A0","bank_b":"B0","package_pin_a":f"A{i}","package_pin_b":f"B{i}","iostandard":"LVCMOS18","supported_iostandards":["LVCMOS18"],"bank_voltage":1.8,"electrical_class":"single_ended_parallel","reserved":False} for i in range(2)],
            "metrics":{"channels":2,"package_pins":4,"concrete_lanes":2}}
        write_json(paths["electrical"],electrical)
        return paths, sources if byte_bound else None
    finally:
        cls.tearDownClass()
