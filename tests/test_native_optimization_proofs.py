"""Canonical checked proof receipts -> native endpoint-bound dispositions."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.store_publication import bind_store_source
from emuflow.vivado_optimization import (build_optimization_proofs,OptimizationProofView,
                                        checked_native_optimization_context)
from tests.native_build import emuflow_native,native_optimization_proofs_test
from tests.test_vivado_optimization import VivadoOptimizationTests


def checked_context(proof,mapped,lineage,digest,checkpoint=''):
    with OptimizationProofView(proof,expected_fpga='fpga0')as view:
        return checked_native_optimization_context(view,bind_store_source(mapped,'xilinx-mapped-index-v2'),
            bind_store_source(lineage,'vivado-lineage-store-v2'),fpga='fpga0',mapped_sha256=digest,
            synthesized_checkpoint_sha256=checkpoint)


@pytest.fixture
def native_root(tmp_path,monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        native=tmp_path/'native';(native/'bin').mkdir(parents=True)
        (native/'bin/emuflow-native').symlink_to(emuflow_native());monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))


@pytest.fixture
def proof(tmp_path,native_root):
    mapped,lineage,digest,origins=VivadoOptimizationTests().prepare(tmp_path)
    output=tmp_path/'proofs.sqlite';build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,fpga='fpga0')
    config=checked_context(output,mapped,lineage,digest)
    config['queries']=[dict(source=f's{origins[name]}',cells=[name])for name in ['dead','one','mutual_known_a']]
    return config,tmp_path/'context.json',origins


def execute(proof,mode=None):
    config,path,_=proof;path.write_text(json.dumps(config))
    command=[str(native_optimization_proofs_test()),str(path)]
    if mode:command.append(mode)
    return subprocess.run(command,capture_output=True,text=True)


def test_unobservable_and_constant_dispositions_remain_distinct(proof):
    result=execute(proof);assert result.returncode==0,result.stderr
    rows=json.loads(result.stdout)
    assert rows[0][1:]==[0,None,'','dead']
    assert rows[1][1:]==[1,1,'specified','one']
    assert rows[2][1:]==[1,0,'specified','mutual_known_a']


@pytest.mark.parametrize('name',['kept','dynamic','zero','zero_chain','mutual_a','disabled_unknown_init'])
def test_absent_and_conditional_sources_cannot_be_promoted_to_proofs(proof,name):
    proof[0]['queries']=[dict(source=f's{proof[2][name]}',cells=[name])]
    result=execute(proof);assert result.returncode!=0 and not result.stdout


@pytest.mark.parametrize('source',['s00','s-1','s+1','s18446744073709551616','p:0','pzz:0','p666f6f:0','s'])
def test_noncanonical_or_foreign_source_references_fail(proof,source):
    proof[0]['queries'][0]['source']=source
    result=execute(proof);assert result.returncode!=0 and not result.stdout


def test_partition_namespaced_reference_keeps_its_owner(proof):
    proof[0]['queries'][0]['source']='p'+b'fpga0'.hex()+':'+str(proof[2]['dead'])
    result=execute(proof);assert result.returncode==0,result.stderr


@pytest.mark.parametrize('damage',['endpoint','fpga','mapped-seal','proof-generation','mapped-generation','lineage-generation','routed-inventory','retained-source','source-name'])
def test_prepared_generations_and_point_source_binding_are_required(proof,damage):
    config,path,origins=proof
    if damage=='endpoint':config['queries'][0]['cells']=['kept','dynamic']
    elif damage=='fpga':config['fpga']='fpga1'
    elif damage=='mapped-seal':config['mapped_sha256']='0'*64
    elif damage.endswith('-generation'):
        index={'proof-generation':0,'mapped-generation':1,'lineage-generation':2}[damage]
        os.utime(config['inputs'][index]['path'],None)
    else:
        # Explicit offline corruption qualifies native per-query rejection,
        # beyond the normal stale-receipt rejection tested above.
        index=2 if damage=='routed-inventory'else 0
        with closing(sqlite3.connect(config['inputs'][index]['path']))as db, db:
            if damage=='routed-inventory':
                value=json.loads(db.execute("SELECT value FROM metadata WHERE key='stages_json'").fetchone()[0]);value['routed']['inventory_sha256']='0'*64
                db.execute("UPDATE metadata SET value=? WHERE key='stages_json'",(json.dumps(value),))
            elif damage=='retained-source':
                db.execute('INSERT INTO eliminated_sources VALUES(?,?,NULL,NULL)',(origins['kept'],'unobservable-source-closure'))
                db.execute('INSERT INTO source_names VALUES(?,?)',(origins['kept'],'kept'))
                config['queries']=[dict(source=f's{origins["kept"]}',cells=['kept'])]
            else:db.execute('UPDATE source_names SET name=? WHERE origin=?',('kept',origins['dead']));config['queries'][0]['cells']=['kept']
        config['inputs'][index]['generation']=native_file_generation(Path(config['inputs'][index]['path']))
    result=execute(proof);assert result.returncode!=0 and not result.stdout


def test_checked_context_does_not_accept_a_status_document_or_renew_a_changed_owner(proof):
    config,path,_=proof;proof_path,mapped,lineage=[Path(row['path'])for row in config['inputs']]
    with OptimizationProofView(proof_path,expected_fpga='fpga0')as view:
        bindings=[bind_store_source(mapped,'xilinx-mapped-index-v2'),bind_store_source(lineage,'vivado-lineage-store-v2')]
        with pytest.raises(ValidationError,match='checked proof reader'):
            checked_native_optimization_context(dict(view),*bindings,fpga='fpga0',mapped_sha256=config['mapped_sha256'])
        os.utime(proof_path,None)
        with pytest.raises(ValidationError,match='generation changed'):
            checked_native_optimization_context(view,*bindings,fpga='fpga0',mapped_sha256=config['mapped_sha256'])


def test_identity_cannot_be_reused_from_another_owner_or_after_mutation(proof,tmp_path):
    other=tmp_path/'copy.sqlite';shutil.copyfile(proof[0]['inputs'][0]['path'],other);proof[0]['other_proof']=str(other)
    result=execute(proof,'foreign');assert result.returncode!=0 and not result.stdout
    result=execute(proof,'mutate');assert result.returncode!=0 and not result.stdout


@pytest.fixture(params=[None,'pipeline','cycle'])
def vendor(tmp_path,native_root,request):
    import csv
    from emuflow.vivado_constant_frontier import check_constant_sources
    from emuflow.vivado_lineage import import_cell_lineage
    from tests.test_vivado_constant_frontier import conditional_model
    mapped,functions,proof,inventory,observed,digest,origins=conditional_model(tmp_path,state_chain=request.param)
    checked=check_constant_sources(mapped,functions,proof,observed,inventory,mapped_sha256=digest,
        candidate_proofs_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),checkpoint_sha256='a'*64,
        fpga='fpga0',selected_sources=[f's{origins["conditional"]}'])
    routed=tmp_path/'routed.tsv'
    with inventory.open()as source,routed.open('w')as target:
        reader=csv.reader(source,delimiter='\t');writer=csv.writer(target,delimiter='\t',lineterminator='\n');writer.writerow(next(reader))
        for i,row in enumerate(reader):writer.writerow(row[:4]+[f'SLICE_X{i}Y0','AFF',str(i),'0','LEAF'])
    lineage=tmp_path/'routed.sqlite';import_cell_lineage(mapped,functions,{'synthesized':inventory,'routed':routed},lineage,mapped_sha256=digest)
    output=tmp_path/'routed-proof.sqlite';build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,fpga='fpga0',checked_constants=checked,synthesized_checkpoint_sha256='a'*64)
    config=checked_context(output,mapped,lineage,digest,'a'*64)
    config['queries']=[dict(source=f's{origins["conditional"]}',cells=['conditional'])]
    return config,tmp_path/'vendor-context.json',origins


def test_selected_vendor_initialization_keeps_its_checked_source_and_frontier(vendor):
    result=execute(vendor);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)[0][1:]==[1,0,'vendor-confirmed','conditional']


@pytest.mark.parametrize('damage',['checkpoint','document-value','document-seal','frontier','source-stage','legacy-policy','unconfirmed-row'])
def test_vendor_claims_require_the_exact_bounded_confirmation(vendor,damage):
    config,_,origins=vendor
    if damage=='checkpoint':config['synthesized_checkpoint_sha256']='b'*64
    else:
        path=Path(config['inputs'][0]['path'])
        with closing(sqlite3.connect(path))as db, db:
            raw=db.execute('SELECT document FROM vendor_confirmation_document').fetchone()[0];doc=json.loads(raw)
            if damage=='document-value':doc['sources'][0]['constant_value']=1
            elif damage=='frontier':doc['frontiers']=[]
            elif damage=='source-stage':doc['source_inventory_sha256']='0'*64
            elif damage=='legacy-policy':doc['schema']='emuflow.vivado-selected-constant-confirmations/v1'
            elif damage=='unconfirmed-row':db.execute('UPDATE eliminated_sources SET constant_value=1 WHERE origin=?',(origins['conditional'],))
            raw=json.dumps(doc,sort_keys=True,separators=(',',':'));db.execute('UPDATE vendor_confirmation_document SET document=?',(raw,))
            if damage!='document-seal':db.execute("UPDATE metadata SET value=? WHERE key='vendor_confirmation_document_sha256'",(json.dumps(hashlib.sha256(raw.encode()).hexdigest()),))
            else:db.execute("UPDATE metadata SET value=? WHERE key='vendor_confirmation_document_sha256'",(json.dumps('0'*64),))
        config['inputs'][0]['generation']=native_file_generation(path)
    result=execute(vendor);assert result.returncode!=0 and not result.stdout
