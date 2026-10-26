import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.sta import (STA_PATH_DATABASE_CLOCK_TSV_HEADER,import_sta_path_database_tsv_streaming,
    iter_sta_path_database_paths,iter_selected_sta_path_database_paths,validate_sta_path_database,
    sta_path_database_payload_path,_sta_path_record_row)
from emuflow.sta_clock_budgets import FIELDS,SCHEMA,FORMAT
from emuflow.yosys import import_yosys_json

ROOT=Path(__file__).resolve().parents[1]


def prepare(root,*,count=3,bad_source_period=False):
    root.mkdir(exist_ok=True)
    ir=import_yosys_json(ROOT/'examples/yosys/counter.json',top='counter',clocks=['clk'])
    source=root/'design.emuir.sqlite';ir.save(source)
    net=ir.value['nets'][0]['id'];raw=root/'paths.tsv';rows=[STA_PATH_DATABASE_CLOCK_TSV_HEADER]
    for i in range(count):
        # Include a half-cycle capture, a half-cycle launch and a full-cycle path.
        launch,capture,target=(0,5,5)if i==0 else(5,0,10)if i==1 else(0,0,10)
        facts=('clk',0 if bad_source_period else 10,launch,capture,0,target,0,.08,0,0,0,0,0,0)
        cells=[f'p{i}'.encode().hex(),'clk'.encode().hex(),'10',str(target-launch-.08-.12),'.12',net.encode().hex()]
        cells += [str(v).encode().hex()if name=='source_clock'else str(v)for name,v in zip(FIELDS,facts)]
        rows.append('\t'.join(cells))
    raw.write_text('\n'.join(rows)+'\n');out=root/'paths.json'
    import_sta_path_database_tsv_streaming(raw,source,out,provider='opensta-fpga-path-database-v1')
    return source,out,json.loads(out.read_text())


def test_clock_budgets_share_the_path_generation_and_survive_selection_and_relocation(tmp_path):
    source,out,value=prepare(tmp_path/'original')
    assert value['schema']==SCHEMA and value['payloads']['paths']['format']==FORMAT
    assert value['summary']['clock_budget_paths']==3
    assert validate_sta_path_database(out,source)['paths']==3
    rows=list(iter_sta_path_database_paths(out,value))
    chosen=list(iter_selected_sta_path_database_paths(out,value,['p0','p2']))
    assert chosen==[rows[0],rows[2]]
    assert rows[0]['clock_budget']['capture_edge_ns']==5
    assert rows[1]['clock_budget']['launch_edge_ns']==5
    assert all(row['fixed_delay_ns']==.12 for row in rows)
    db=sqlite3.connect(sta_path_database_payload_path(out,value))
    try:
        assert db.execute('PRAGMA user_version').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM clock_budgets').fetchone()[0]==3
    finally:db.close()
    copied=tmp_path/'relocated';shutil.copytree(out.parent,copied)
    assert list(iter_sta_path_database_paths(copied/out.name,value))==rows
    with pytest.raises(ValidationError,match='indexed v4'):_sta_path_record_row(rows[0])


@pytest.mark.parametrize('damage',['missing','extra','schema-format-mix'])
def test_incomplete_and_mixed_budget_generations_are_rejected(tmp_path,damage):
    _source,out,value=prepare(tmp_path)
    if damage=='schema-format-mix':value['payloads']['paths']['format']='sqlite-sta-path/v1'
    else:
        with sqlite3.connect(sta_path_database_payload_path(out,value))as db:
            if damage=='missing':db.execute('DELETE FROM clock_budgets WHERE path_ordinal=1')
            else:db.execute('INSERT INTO clock_budgets SELECT 99,'+','.join(FIELDS)+' FROM clock_budgets WHERE path_ordinal=0')
    with pytest.raises(ValidationError):list(iter_sta_path_database_paths(out,value,verify_payload=False))


def test_invalid_engine_budget_does_not_replace_an_existing_selector(tmp_path):
    _source,out,value=prepare(tmp_path)
    before=out.read_bytes();payload=sta_path_database_payload_path(out,value);payload_bytes=payload.read_bytes()
    with pytest.raises(ValidationError):prepare(tmp_path,bad_source_period=True)
    assert out.read_bytes()==before and payload.read_bytes()==payload_bytes


def test_clock_policy_and_tsv_format_cannot_be_mixed(tmp_path):
    source,out,_value=prepare(tmp_path)
    before=out.read_bytes()
    with pytest.raises(ValidationError,match='policy/header mismatch'):
        import_sta_path_database_tsv_streaming(tmp_path/'paths.tsv',source,out,
            provider='opensta-fpga-path-database-v1',source={'clock_budget_policy':'legacy-absolute-arrival-v1'})
    assert out.read_bytes()==before
