"""Cross-stage regression checks for the declared cable/HP-bank model."""
import copy
import pytest

from emuflow.board_link_timing import (
    build_board_link_timing_model,
    directed_route_link_delays,
    validate_board_link_timing,
)
from emuflow.errors import ValidationError
from tests.global_sta_fixture import bind_physical_checks
from emuflow.platform import Platform
from tests.system_timing_oracle import _fixed_slot_transport_path_delay
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
    assert database["links"][0]["reference_tdm_ratio"] == 8
    assert database["links"][0]["delay_bound_ns"] == 34.0
    assert database["links"][0]["tdm_delay_model"]["ratio_coefficient_ns"] == .5
    directed, _ = directed_route_link_delays(database, platform)
    constraints = normalize_route_constraints(
        {
            "schema": "emuflow.system-route-constraints/v1",
            "frame_slots": 1024,
            "directed_link_delay_ns": directed,
        },
        platform,
    )
    assert constraints["tdm_max_ratio"] == 256
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
                "slot": s, "ready_slot": 0, "tdm_ratio": r, "lane": 0, "hop": i,
                "arrival_slot": s + 8, "capacity_key": edge["link"]+":"+edge["from"]+"->"+edge["to"]}
               for i,(edge,s,r) in enumerate(zip(edges,[3,15],[8,16]))]
    constraints = normalize_route_constraints(None, platform)
    routes = {"constraints": constraints, "routes":[{"id":"d","net":"n",
              "source":"fpga0","sinks":["fpga1"],"tree_edges":edges}]}
    return platform, routes, {"entries": entries}


@pytest.mark.parametrize("missing_tx_time", [False, True])
def test_physical_path_keeps_local_delays_and_separate_readiness_gate(monkeypatch, missing_tx_time):
    platform, routes, schedule = two_hop_case()
    entries = schedule["entries"]
    hops = [{**{k:e[k] for k in ("demand","link","from","to")},
             "schedule_entry":e["id"],"tdm_slot_ns":4.0,"base_link_delay_ns":30.5,
             "fixed_slot_tx_time_ns":e["slot"]*4.0,"tdm_ratio":e["tdm_ratio"],
             "tx_endpoint":f"__emuflow_tx_{e['id']}","rx_endpoint":f"__emuflow_rx_{e['id']}"}
            for i,e in enumerate(entries)]
    record = {"path":"p","cut_nets":["n"],"scheduled_hops":hops,
              "clock_period_ns":100.0,"required_time_ns":100.0}
    segments = [{"cut_index":0,"kind":"launch","delay_ns":2.0,"replace_tx_endpoint":"__emuflow_tx_e0"},
                {"cut_index":1,"kind":"capture","delay_ns":3.0,"replace_tx_endpoint":None}]
    endpoints = {hop[key]:.5 for hop in hops for key in ("tx_endpoint", "rx_endpoint")}
    def evaluate():
        return _fixed_slot_transport_path_delay(record,"p",segments,endpoints,None,
            {e["id"]:e for e in entries},{"n":routes["routes"][0]},
            commit_slot=40,uncertainty_ns=0,platform=platform)
    result = evaluate()
    # 34+38 board; 2+3 local; two RX and one relay TX, .5 each.
    assert result["board_delay_bound_ns"] == 72
    assert result["system_delay_bound_ns"] == 78.5
    assert result["fixed_slot_event_timing_status"] == "pass"
    # Publish factual records into current stores; the production binder must
    # reconstruct its own hop events without the former injected Python stream.
    constraints = normalize_route_constraints({"schema":"emuflow.system-route-constraints/v1","tdm_max_ratio":40}, platform, frame_slots=41)
    route = {**routes['routes'][0], 'width_bits':1, 'cut_class':'register_output',
             'dependency_level':0, 'combinational_dependency_depth':0, 'predecessor_cut_nets':[]}
    routes.update(schema='emuflow.system-routes/v1', design='interconnect', platform=platform.name,
        provider='timing-aware-global-candidate-v1', constraints=constraints,
        demands=[{k:v for k,v in route.items() if k!='tree_edges'}], routes=[route],
        timing={'schema':'emuflow.sta-paths/v1',
                'normalization':{'positive_slack_scale_ns':100.0,'negative_slack_scale_ns':100.0,'max_clock_period_ns':100.0},
                'paths':[{'path':'p','clock_domain':'clk','clock_period_ns':100.0,
                          'required_time_ns':100.0,'fixed_delay_ns':0.0,'cut_nets':['n'],
                          'cut_transitions':[{'net':'n','from':'fpga0','to':'fpga1'}]}]})
    schedule.update(schema='emuflow.tdm-schedule/v1', design='interconnect', platform=platform.name,
        provider='deterministic-dependency-earliest-slot-v1', transport_semantics='registered-boundary',
        route_constraints=constraints, routes=[{k:route[k] for k in ('id','net','source','sinks','tree_edges')}],
        demand_completions=[{'demand':'d','net':'n','source_ready_slot':0,'completion_slot':23,
                            'sink_arrival_slots':{'fpga1':23}}], domain_schedules=[], metrics={})
    physical={'boundary_identities':{}, 'boundary_timing':{}, 'logic_segment_timing':{},
              'board_link_timing':build_board_link_timing_model(platform)}
    for fpga in platform.fpgas:
        common={'status':'pass','design':'interconnect','platform':platform.name,'fpga':fpga.id,'provider':'fixture'}
        identities=[];timing=[];logic=[]
        for entry,hop in zip(entries,hops):
            for kind,owner in (('tx',entry['from']),('rx',entry['to'])):
                if owner!=fpga.id:continue
                identity=hop[kind+'_endpoint']
                identities.append({'id':identity,'kind':kind,'schedule_entry':entry['id']})
                timing.append({'id':identity,'kind':kind,'schedule_entry':entry['id'],'delay_ns':.5,
                    'start_object':'start','end_object':'end',
                    'measurement':'logical-source-to-tx-port' if kind=='tx' else 'rx-port-to-shadow-capture'})
        for index, (owner,segment) in enumerate(zip(('fpga0','fpga1'),segments)):
            if owner==fpga.id:
                logic.append({**segment,'id':f'logic-{index}','system_path':'p','member_path':'p','fpga':owner,
                              'start_pin':'start','end_pin':'end','measurement':'endpoint-exact'})
        counts={'endpoints':len(identities),'tx':sum(r['kind']=='tx' for r in identities),'rx':sum(r['kind']=='rx' for r in identities)}
        physical['boundary_identities'][fpga.id]={**common,'schema':'emuflow.boundary-identity/v1','coverage':{**counts,'external_port_nets':len(identities)},'endpoints':identities}
        physical['boundary_timing'][fpga.id]={**common,'schema':'emuflow.boundary-timing/v1','qualification':'fixture','measurement_scope':'partition-boundary','coverage':counts,'endpoints':timing}
        physical['logic_segment_timing'][fpga.id]={**common,'schema':'emuflow.logic-segment-timing/v1','qualification':'fixture',
            'coverage':{'segments':len(logic),'system_paths':int(bool(logic)),'member_paths':int(bool(logic)),'unsupported_member_paths':0},
            'unsupported_member_paths':[],'segments':logic}
    import emuflow.tdm as tdm
    assert not hasattr(tdm, 'open_tdm_schedule_timing_path_stream')
    def bind():
        return bind_physical_checks(
            {'virtual_dut_clocks':{'clk':{'nominal_period_ns':164,'target_period_ns':100}}},
            routes,schedule,physical,platform)
    if missing_tx_time:
        entries[0]['slot']=None
        with pytest.raises(ValidationError):
            bind()
        return
    checks = bind()
    assert next(c for c in checks if c.role=="target").arcs_ns == (2,34,.5,.5,38,.5,3,0)
    entries[1]["slot"] = 10 # relay TX at40ns precedes arrival46.5 + relay .5
    late = evaluate()
    assert late["system_delay_bound_ns"] == 78.5
    assert late["fixed_slot_event_timing_status"] == "fail"
    late_checks = bind()
    target = next(c for c in late_checks if c.role=='target')
    assert target.arcs_ns == (2,34,.5,.5,38,.5,3,0)
    readiness = next(c for c in late_checks if c.role=='tx')
    assert readiness.required_ns == 40
    assert readiness.launch_ns + sum(readiness.arcs_ns) > readiness.required_ns
    checks.close(); late_checks.close()
