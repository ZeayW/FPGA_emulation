"""Cross-stage regression checks for the declared cable/HP-bank model."""
import copy
import pytest

from emuflow.board_link_timing import build_board_link_timing_model, validate_board_link_timing
from emuflow.cross_stage import _scheduled_transport_delay_by_net
from emuflow.errors import ValidationError
from emuflow.global_sta import bind_physical_checks
from emuflow.platform import Platform
from emuflow.system_timing import _fixed_slot_transport_path_delay
from emuflow.routing import normalize_route_constraints
from emuflow.default_platform import research_platform_path


def test_bank_endpoint_inventory_and_dimensions_are_enforced():
    value = Platform.load(research_platform_path()).to_dict()
    for mutate, message in [
        (lambda x: x["links"][0].update(cable_count=5), "bank_count must equal"),
        (lambda x: x["fpgas"][0].update(hp_io_bank_count=35), "exceeding inventory"),
        (lambda x: x["fpgas"][0].pop("hp_io_bank_count"), "requires hp_io_bank_count"),
        (lambda x: x["links"][0].update(data_lanes_per_direction=720), "bank capacity requires"),
        (lambda x: x["links"][0].update(wires_per_bank=True), "positive integer"),
    ]:
        bad = copy.deepcopy(value)
        mutate(bad)
        with pytest.raises(ValidationError, match=message):
            Platform.from_dict(bad)


def test_ratio_timing_database_keeps_model_and_rejects_stale_constants():
    platform = Platform.load(research_platform_path())
    database = build_board_link_timing_model(platform)
    assert validate_board_link_timing(database, platform)["status"] == "pass"
    assert database["links"][0]["delay_bound_ns"] == 30.5
    assert database["links"][0]["tdm_delay_model"]["ratio_coefficient_ns"] == .5
    database["links"][0]["delay_bound_ns"] = 50
    with pytest.raises(ValidationError, match="disagrees with BoardDB"):
        validate_board_link_timing(database, platform)


def two_hop_case():
    value = Platform.load(research_platform_path()).to_dict()
    # F0 -> F2 -> F1, two adjacent links on the K2,2 graph.
    platform = Platform.from_dict(value)
    by_pair = {frozenset(l.endpoints): l.id for l in platform.links}
    edges = [{"link": by_pair[frozenset((a,b))], "from": a, "to": b}
             for a,b in [("fpga0","fpga2"),("fpga2","fpga1")]]
    entries = [{"id": f"e{i}", "demand":"d", "net":"n", **edge,
                "slot": s, "ready_slot": 0, "tdm_ratio": r}
               for i,(edge,s,r) in enumerate(zip(edges,[3,15],[8,16]))]
    constraints = normalize_route_constraints(None, platform)
    routes = {"constraints": constraints, "routes":[{"id":"d","net":"n",
              "source":"fpga0","sinks":["fpga1"],"tree_edges":edges}]}
    return platform, routes, {"entries": entries}


def test_cross_stage_uses_each_hops_ratio_without_another_slot_wait():
    platform, routes, schedule = two_hop_case()
    assert _scheduled_transport_delay_by_net(routes,schedule,platform) == {"n":72.0}
    schedule["entries"][1]["slot"] += 9
    assert _scheduled_transport_delay_by_net(routes,schedule,platform) == {"n":72.0}
    schedule["entries"][1]["tdm_ratio"] = 24
    assert _scheduled_transport_delay_by_net(routes,schedule,platform) == {"n":76.0}
    del schedule["entries"][1]["tdm_ratio"]
    with pytest.raises(ValidationError, match="TDM ratio"):
        _scheduled_transport_delay_by_net(routes,schedule,platform)


def test_physical_path_keeps_local_delays_and_separate_readiness_gate(monkeypatch):
    platform, routes, schedule = two_hop_case()
    entries = schedule["entries"]
    hops = [{**{k:e[k] for k in ("demand","link","from","to")},
             "schedule_entry":e["id"],"tdm_slot_ns":4.0,"base_link_delay_ns":30.5,
             "tx_endpoint":f"tx{i}","rx_endpoint":f"rx{i}"}
            for i,e in enumerate(entries)]
    record = {"path":"p","cut_nets":["n"],"scheduled_hops":hops,
              "clock_period_ns":100.0,"required_time_ns":100.0}
    segments = [{"cut_index":0,"kind":"launch","delay_ns":2.0,"replace_tx_endpoint":"tx0"},
                {"cut_index":1,"kind":"capture","delay_ns":3.0,"replace_tx_endpoint":None}]
    endpoints = {"tx0":.5,"rx0":.5,"tx1":.5,"rx1":.5}
    def evaluate():
        return _fixed_slot_transport_path_delay(record,"p",segments,endpoints,None,
            {e["id"]:e for e in entries},{"n":routes["routes"][0]},
            commit_slot=40,uncertainty_ns=0,platform=platform)
    result = evaluate()
    # 34+38 board; 2+3 local; two RX and one relay TX, .5 each.
    assert result["board_delay_bound_ns"] == 72
    assert result["system_delay_bound_ns"] == 78.5
    assert result["fixed_slot_event_timing_status"] == "pass"
    # Check independently generated OpenSTA raw arc chains match physical sum.
    import emuflow.system_timing as st
    import emuflow.tdm as tdm
    monkeypatch.setattr(st,"_endpoint_delay_database",lambda _: endpoints)
    monkeypatch.setattr(st,"_logic_segment_database",lambda _: {"p":{"p":segments}})
    monkeypatch.setattr(st,"_board_link_delay_database",lambda *_: {
        (h["link"],h["from"],h["to"]):30.5 for h in hops})
    monkeypatch.setattr(tdm,"reconstruct_tdm_schedule_timing_paths",lambda *_: [record])
    schedule.update(transport_semantics="registered-boundary",route_constraints={"frame_slots":41})
    checks = bind_physical_checks({"virtual_dut_clocks":{"clk":{"nominal_period_ns":164,"target_period_ns":10}}},
                                  routes,schedule,{},platform)
    assert next(c for c in checks if c.role=="target").arcs_ns == (2,34,.5,.5,38,.5,3,0)
    entries[1]["slot"] = 10 # relay TX at40ns precedes arrival46.5 + relay .5
    late = evaluate()
    assert late["system_delay_bound_ns"] == 78.5
    assert late["fixed_slot_event_timing_status"] == "fail"
