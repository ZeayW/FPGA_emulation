import copy
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")


def validator(name):
    return jsonschema.Draft202012Validator(json.loads((Path(__file__).resolve().parents[1]/"schemas"/name).read_text()))


def test_boundary_schema_accepts_dispositions_and_rejects_fake_scalars():
    from tests.test_boundary_timing_storage import _header, _record
    eliminated = {**_record("rx","rx",0),"measurement":"optimized-away","delay_ns":None,
                  "start_object":None,"end_object":None,"optimization_source":"s7"}
    document = {**_header(),"coverage":{"endpoints":1,"rx":1,"tx":0},"endpoints":[eliminated]}
    v = validator("boundary-timing-v1.schema.json");v.validate(document)
    for key,value in [("delay_ns",0),("start_object","fake"),("optimization_source","s00")]:
        bad = copy.deepcopy(document);bad["endpoints"][0][key]=value
        with pytest.raises(jsonschema.ValidationError):v.validate(bad)
    del document["endpoints"][0]["optimization_source"]
    with pytest.raises(jsonschema.ValidationError):v.validate(document)


def test_cross_layer_schema_disallows_numerical_elimination_and_null_measurement():
    v = validator("cross-layer-physical-binding-v1.schema.json")
    schema = v.schema
    for kind in ("measuredEndpoint","measuredSegment"):
        item_schema = {"$schema":schema["$schema"],**schema["$defs"][kind]}
        item = {"fpga":"fpga0","measurement":"optimized-away","measured_min_delay_ns":None,"measured_max_delay_ns":None}
        if kind=="measuredEndpoint": item.update(endpoint="rx",kind="rx",schedule_entry="s0",optimization_source="s7")
        else: item.update(segment="x",optimized_away_records=1)
        checker=jsonschema.Draft202012Validator(item_schema);checker.validate(item)
        item["measured_max_delay_ns"]=0
        with pytest.raises(jsonschema.ValidationError):checker.validate(item)
        item["measured_max_delay_ns"]=None;item["measurement"]="routed-endpoint-exact"
        with pytest.raises(jsonschema.ValidationError):checker.validate(item)
