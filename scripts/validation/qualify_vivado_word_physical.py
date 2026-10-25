#!/usr/bin/env python3
"""Route a synthetic two-bank native-word fixture with the indexed provider.

This checks physical-top, pin, clock and timing-query integration. Its small
manual interface fixture is not Phase1--7 NVDLA or full-design global evidence.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from emuflow.default_platform import materialize_research_platform
from emuflow.io import file_sha256,write_json
from emuflow.local_path_timing import path_id_set_sha256
from emuflow.lvds_word_plan import build_word_phy_plan
from emuflow.platform import Platform
from emuflow.tdm_word import WORD_EVENT_POLICY
from emuflow.virtual_board import build_virtual_board
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_physical import run_indexed_vivado_partition
from emuflow.vivado_physical_timing import write_vivado_local_query
from emuflow.yosys import import_yosys_json


def prepare(root):
    platform_path=root/'boarddb.json'
    materialize_research_platform(platform_path,fpga_count=2,transport_format='lvds-word-v2')
    platform=Platform.load(platform_path);board=build_virtual_board(platform)
    link=platform.links[0];a,b=sorted(link.endpoints)
    schedule={'platform':platform.name,'design':'native-word-interface-fixture','word_event_policy':WORD_EVENT_POLICY,
        'bank_allocations':[{'link':link.id,'from':src,'to':dst,'bank_count':9,'lane_offset':offset,
                            'lanes':1440,'wires_per_bank':20}for src,dst,offset in ((a,b,0),(b,a,1440))]}
    transports={a:{'fpga':a,'word_event_policy':WORD_EVENT_POLICY,'endpoints':[
        {'link':link.id,'peer':b,'kind':'tx','lane':0},{'link':link.id,'peer':b,'kind':'rx','lane':1440}]},
        b:{'fpga':b,'word_event_policy':WORD_EVENT_POLICY,'endpoints':[
        {'link':link.id,'peer':a,'kind':'rx','lane':0},{'link':link.id,'peer':a,'kind':'tx','lane':1440}]}}
    plan=build_word_phy_plan(platform,board,schedule,transports)
    board_path=root/'wiring.json';plan_path=root/'word-plan.json'
    write_json(board_path,board);write_json(plan_path,plan)
    node=next(f for f in plan['fpgas']if f['fpga']==a)
    tx=next(record for record in node['banks']if record['direction']=='tx')
    rx=next(record for record in node['banks']if record['direction']=='rx')
    width=link.transport_bits_per_cycle_per_direction
    rx_bits=list(range(100,100+width))
    ports={name:{'direction':direction,'bits':bits}for name,direction,bits in (
        ('clk','input',[2]),('fabric_clk','input',[3]),('reset','input',[4]),
        ('links_ready','input',[5]),('d','input',[6]),('q','output',[9]),
        (tx['logical_port'],'output',[7]*width),(rx['logical_port'],'input',rx_bits))}
    cells={}
    for name,clock,data,output in (('launch',2,6,7),('shadow',3,rx_bits[rx['logical_lsb']],8),('capture',2,8,9)):
        cells[name]={'type':'FDRE','parameters':{'INIT':'0'},'attributes':{},
            'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'},
            'connections':{'C':[clock],'D':[data],'Q':[output],'CE':['1'],'R':['0']}}
    source=root/'source.json'
    source.write_text(json.dumps({'modules':{'dut':{'attributes':{'top':'1'},'ports':ports,'cells':cells,'netnames':{}}}}))
    ir=root/'source.emuir.sqlite';import_yosys_json(source).save(ir)
    mapped=root/'dut.v';mapped_report=emit_vivado_mapped_verilog(ir,mapped,root/'mapped-report.json')
    runtime={'fabric_clock':{'period_ns':5.0},'virtual_dut_clock':{'nominal_period_ns':320.0},
        'virtual_dut_clocks':{'clk':{'target_period_ns':10.0,'nominal_period_ns':320.0}},
        'timing_model':{'dut_clock_ports':['clk'],'fabric_clock_port':'fabric_clk','fabric_to_dut_max_delay_ns':5.0}}
    paths=[]
    for index,(start,end,clock,period)in enumerate((
        (('launch','Q',0),(None,tx['logical_port'],0),'clk',10.0),
        (('launch','Q',0),(None,tx['logical_port'],8),'clk',10.0),
        ((None,rx['logical_port'],rx['logical_lsb']),('shadow','D',0),'fabric_clk',5.0),
        (('shadow','Q',0),('capture','D',0),'clk',10.0))):
        endpoint=lambda value:dict(zip(('instance','port','bit'),value))
        paths.append({'id':f'word-path{index}','clock_domain':clock,'clock_period_ns':period,
            'fixed_delay_ns':2.0,'slack_ns':period-2.0,'startpoint':endpoint(start),'endpoint':endpoint(end)})
    seals={name:file_sha256(source)for name in ('path_database_sha256','original_ir_sha256','assignment_sha256','routes_sha256')}
    seals.update(original_paths=len(paths),original_path_ids_sha256=path_id_set_sha256([p['id']for p in paths]))
    identity,query=root/'local-identity.sqlite',root/'local-query.tsv'
    write_vivado_local_query(ir,a,query,identity,SimpleNamespace(assignment={'design':'dut'},source_manifest=seals,local_paths_by_fpga={a:paths}))
    return dict(fpga=a,part=platform.fpgas[0].part,ir_path=ir,mapped_verilog_path=mapped,
        mapped_report=mapped_report,runtime=runtime,original_cells=3,transport_cells=0,
        local_identity_path=identity,local_query_path=query,word_phy_plan_path=plan_path,word_wiring_path=board_path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--vivado',required=True)
    parser.add_argument('--place-directive',default='Quick',help='qualification fixture only; production remains Default')
    args=parser.parse_args();root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    inputs=prepare(root)
    report=run_indexed_vivado_partition(**inputs,output_dir=root/'physical',executable=args.vivado,
        place_directive=args.place_directive,dut_frequency_mhz=10.0)
    write_json(root/'qualification.json',{'status':'pass','scope':'synthetic-native-word-physical-and-query-integration-only',
        'tool':report['tool'],'result':report['result'],'word_phy':report['word_phy_implementation'],
        'source_cell_coverage':report['cell_coverage'],'local_timing':report['local_path_timing']['import']})


if __name__=='__main__':main()
