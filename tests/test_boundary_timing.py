"""Independent boundary contracts over explicit logical and measured facts."""
from copy import deepcopy

import pytest

from emuflow.timing.boundary_timing import (
    boundary_timing_header,
    validate_boundary_identity_database,
    validate_boundary_timing_database,
)
from emuflow.common.errors import ValidationError
from tests.test_boundary_identity_storage import _header, _record


def facts():
    records = [_record("tx0", "tx", 0), _record("rx0", "rx", 1)]
    identity = {**_header(), "coverage": {
        "endpoints": 2, "tx": 1, "rx": 1, "external_port_nets": 2,
    }, "endpoints": records}
    transport = {"schema": "emuflow.transport-endpoints/v1", "design": "dut",
                 "platform": "board", "fpga": "fpga0", "endpoints": [
        {**{key: row[key] for key in (
            "id", "kind", "schedule_entry", "demand", "link", "peer", "lane", "logical_lane")},
         "net": row["logical_net"], "signal": row["internal_signal"]} for row in records]}
    return transport, identity


@pytest.mark.parametrize("scope,measurements", [
    ("partition-boundary", ("logical-source-to-tx-port", "rx-port-to-shadow-capture")),
    ("board-integrated-interface", (
        "routed-launch-through-tx-boundary-to-interface-endpoint",
        "interface-startpoint-through-rx-boundary-to-shadow-capture")),
])
def test_current_identity_and_physical_timing_cover_exact_endpoints(scope, measurements):
    transport, identity = facts()
    assert validate_boundary_identity_database(identity, transport)["endpoints"] == 2
    timing = {**boundary_timing_header(identity, provider="independent-facts",
        qualification="endpoint-exact", measurement_scope=scope),
        "coverage": {"endpoints": 2, "tx": 1, "rx": 1}, "endpoints": [
            {"id": "tx0", "kind": "tx", "schedule_entry": "schedule-0", "delay_ns": 1.25,
             "start_object": "launch/C", "end_object": "tx-interface/D", "measurement": measurements[0]},
            {"id": "rx0", "kind": "rx", "schedule_entry": "schedule-1", "delay_ns": .75,
             "start_object": "rx-interface/C", "end_object": "shadow/D", "measurement": measurements[1]},
        ]}
    assert validate_boundary_timing_database(timing, identity)["maximum_delay_ns"] == 1.25
    timing["endpoints"].pop()
    with pytest.raises(ValidationError, match="coverage"):
        validate_boundary_timing_database(timing, identity)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "lane", "signal", "register", "coverage"])
def test_boundary_identity_contract_rejects_incomplete_or_mixed_facts(damage):
    transport, identity = facts()
    if damage == "missing":
        identity["endpoints"].pop()
    elif damage == "duplicate":
        identity["endpoints"].append(deepcopy(identity["endpoints"][0]))
    elif damage == "lane":
        identity["endpoints"][0]["lane"] += 1
    elif damage == "signal":
        identity["endpoints"][0]["internal_signal"] = "net:other-run"
    elif damage == "register":
        identity["endpoints"][1]["merged_ir"]["boundary_register_instances"] = []
    else:
        identity["coverage"]["endpoints"] = 999
    with pytest.raises(ValidationError):
        validate_boundary_identity_database(identity, transport)


def test_forwarded_shadow_requires_exact_register_and_source_class():
    transport, identity = facts()
    source, row = transport["endpoints"][0], identity["endpoints"][0]
    source["signal"] = row["internal_signal"] = "shadow:relay:fpga0"
    row["source_class"] = "forwarded-shadow"
    row["merged_ir"].update(logical_net=None,
                           boundary_register_instances=["__emuflow_transport__/relay_ff"])
    assert validate_boundary_identity_database(identity, transport)["tx"] == 1
    row["merged_ir"]["boundary_register_instances"].append("__emuflow_transport__/other_ff")
    with pytest.raises(ValidationError, match="shadow register"):
        validate_boundary_identity_database(identity, transport)
