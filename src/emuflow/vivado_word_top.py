"""Bounded physical projection of an independently checked Phase6 word plan.

The wiring owner supplies package pairs; the word plan supplies whole-bank
direction and logical slices. This module adds native instances and explicit
external clock/startup service ports, without copying circuit or endpoint data.
"""
from __future__ import annotations

from pathlib import Path
import re

from .errors import ValidationError
from .indexed_physical_ir import IndexedPhysicalIR
from .io import file_sha256
from .lvds_bank_phy import selectio_bank_to_systemverilog
from .lvds_selectio import selectio_bank_recipe,SELECTIO_IP_DEFINITION,WORD_PHY_RX_ALIGNMENT
from .lvds_word_aligner import word_aligner_to_systemverilog
from .lvds_word_plan import WORD_PHY_PLAN_SCHEMA
from .tdm_word import WORD_EVENT_POLICY
from .verilog import _identifier
from .virtual_board import WORD_MODEL,WORD_WIRING_SCHEMA,_digest

SCHEMA='emuflow.vivado-word-top-preparation/v1'
SERVICE_INPUTS=('__emuflow_riu_clk','__emuflow_all_tx_ready',
    '__emuflow_all_plls_locked','__emuflow_alignment_enable','__emuflow_payload_enable')
SERVICE_OUTPUTS=('__emuflow_local_tx_ready','__emuflow_local_rx_ready',
    '__emuflow_local_plls_locked','__emuflow_local_reset_done',
    '__emuflow_local_rx_word_valid','__emuflow_local_training_error')


def _unique_records(records,key,context):
    result={}
    for item in records:
        identity=key(item)
        if identity in result:raise ValidationError(context+' repeats')
        result[identity]=item
    return result


def _bank_projection(board,plan,fpga):
    if (board.get('schema')!=WORD_WIRING_SCHEMA or board.get('model')!=WORD_MODEL
        or plan.get('schema')!=WORD_PHY_PLAN_SCHEMA or plan.get('status')!='pass'
        or plan.get('word_event_policy')!=WORD_EVENT_POLICY
        or plan.get('platform')!=board.get('platform')
        or plan.get('source',{}).get('board_sha256')!=_digest(board)
        or plan.get('native_phy')!={'ip_definition':SELECTIO_IP_DEFINITION,'rx_alignment':WORD_PHY_RX_ALIGNMENT}):
        raise ValidationError('native word top requires a source-bound supported word PHY plan')
    nodes=_unique_records(board['fpgas'],lambda f:f['id'],'word board FPGA')
    plans=_unique_records(plan['fpgas'],lambda f:f['fpga'],'word plan FPGA')
    if fpga not in nodes or set(nodes)!=set(plans):
        raise ValidationError('native word top FPGA population differs')
    assignments=_unique_records(nodes[fpga]['bank_assignments'],lambda b:(b['link'],b['bank_group']),'word bank assignment')
    wires=_unique_records(board['wires'],lambda w:(w['link'],w['bank_group'],w['wire_in_bank']),'word data wire')
    clocks=_unique_records(board['forwarded_clocks'],lambda c:(c['link'],c['bank_group']),'word forwarded clock')
    banks=[];seen=set();ip_modules=set()
    for record in plans[fpga]['banks']:
        key=(record['link'],record['bank_group']);bank=record['bank'];direction=record['direction']
        if (isinstance(record['bank_group'],bool)or not isinstance(record['bank_group'],int)
            or isinstance(record['logical_port_width'],bool)or not isinstance(record['logical_port_width'],int)
            or not isinstance(record['logical_lsb'],int)
            or direction not in {'tx','rx'} or bank in seen or key not in assignments
            or assignments[key]['bank']!=bank or isinstance(record['logical_lsb'],bool)
            or record['logical_lsb']!=record['bank_group']*160
            or record['logical_port_width']<record['logical_lsb']+160
            or record['logical_port_width']%160 or record['forwarded_clock']!=clocks.get(key,{}).get('id')):
            raise ValidationError('native word bank direction, slice or clock binding differs')
        if record['ip_module']in ip_modules:raise ValidationError('native word IP module repeats')
        if record['ip_module']!=f'emuflow_selectio_bank{bank}_{direction}':
            raise ValidationError('native word IP module differs from its bank/direction identity')
        ip_modules.add(record['ip_module'])
        seen.add(bank)
        recipe=selectio_bank_recipe(bank,direction,**({'rx_alignment':WORD_PHY_RX_ALIGNMENT}if direction=='rx'else{}))
        if nodes[fpga]['part']!=recipe.part:raise ValidationError('native word package part differs')
        bindings=[]
        for lane,pair in enumerate((*recipe.data_pairs,recipe.clock_pair)):
            owner=clocks[key]if lane==20 else wires.get((*key,lane))
            if owner is None:raise ValidationError('native word data-lane population differs')
            endpoints=_unique_records(owner['endpoints'],lambda e:e['fpga'],'word wire endpoint')
            if set(endpoints)!={fpga,record['peer']}:
                raise ValidationError('native word physical peer differs')
            endpoint=endpoints[fpga]
            if endpoint['bank']!=bank or endpoint['pair_id']!=pair['pair_id']:
                raise ValidationError('native word pair order or bank differs')
            for polarity in ('p','n'):
                if endpoint[polarity]['package_pin']!=pair[polarity]['package_pin']:
                    raise ValidationError('native word pair package pin differs')
            stem=f'__emuflow_bank{bank}_{direction}_'+('clock'if lane==20 else f'data{lane}')
            bindings.append({'role':'clock'if lane==20 else'data','pair':pair,
                'p':stem+'_p','n':stem+'_n'})
        banks.append({'record':record,'recipe':recipe,'bindings':bindings,
            'instance':f'native_bank{bank}_{direction}', 'module':f'emuflow_lvds_bank{bank}_{direction}'})
    return nodes[fpga]['part'],banks


def write_vivado_word_top(ir_path:Path,output:Path,*,fpga,board,plan):
    """Emit native-bank sources/XDC and hierarchical word timing boundaries.

    Full Phase6 validation owns schedule/transport validity. This consumer
    independently checks its bounded pin, direction and interface projection.
    Global readiness and pausible DUT clock services remain explicit OOC inputs.
    """
    part,banks=_bank_projection(board,plan,fpga)
    with IndexedPhysicalIR(ir_path)as ir:
        source_top=ir.value['design']['top'];ports={p['id']:p for p in ir.value['ports']}
    top='__emuflow_physical_top'
    reserved={top,'emuflow_lvds_word_align',*(b['module']for b in banks),*(b['record']['ip_module']for b in banks)}
    if source_top in reserved:raise ValidationError('native word top identity collides')
    for name in ('fabric_clk','reset','links_ready'):
        if ports.get(name,{}).get('direction')!='input'or ports[name]['width']!=1:
            raise ValidationError('native word partition lacks a scalar service input: '+name)
    if set(ports)&set((*SERVICE_INPUTS,*SERVICE_OUTPUTS)):
        raise ValidationError('native word startup service port collides')
    buses={};ranges={};physical={}
    for bank in banks:
        record=bank['record'];bus=record['logical_port'];width=record['logical_port_width']
        direction='output'if record['direction']=='tx'else'input'
        if (ports.get(bus,{}).get('direction')!=direction or ports[bus]['width']!=width
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',record['ip_module'])):
            raise ValidationError('native word partition bus or IP identity differs')
        buses[bus]=width
        occupied=ranges.setdefault(bus,set());begin=record['logical_lsb']
        if begin in occupied:raise ValidationError('native word bus bank slice overlaps')
        occupied.add(begin)
        for binding in bank['bindings']:
            for polarity in ('p','n'):
                name=binding[polarity]
                if name in ports or name in physical:raise ValidationError('native word physical port collides')
                physical[name]=direction
    output.mkdir(parents=True,exist_ok=True)
    names=('physical_top.sv','native-word-banks.sv','native-word-ips.tsv',
        'native-word-fixed-io.xdc','fixed-io-query.tsv','native-word-boundaries.tsv','native-word-clocks.tsv')
    if any((output/name).exists()for name in names):raise ValidationError('native word top requires fresh output files')
    declarations=[]
    for name,port in ports.items():
        if name not in buses:
            declarations.append(f"  {port['direction']} wire "+(f"[{port['width']-1}:0] "if port['width']>1 else'')+_identifier(name))
    declarations.extend('  input wire '+name for name in SERVICE_INPUTS)
    declarations.extend('  output wire '+name for name in SERVICE_OUTPUTS)
    declarations.extend(f'  {direction} wire {name}'for name,direction in physical.items())
    lines=['// OOC clock/reset/startup consensus services are external.',f'module {top} (',',\n'.join(declarations),');']
    lines.extend(f'  wire [{width-1}:0] {_identifier(bus)};'for bus,width in buses.items())
    lines += [f'  {_identifier(source_top)} mapped_partition (',
        ',\n'.join(f'    .{_identifier(name)}({_identifier(name)})'for name in ports),'  );']
    for bus in sorted(buses):
        if ports[bus]['direction']=='input':
            for begin in range(0,buses[bus],160):
                if begin not in ranges[bus]:lines.append(f"  assign {_identifier(bus)}[{begin} +: 160] = '0;")
    status={key:[]for key in ('tx_ready','rx_ready','pll_locked','reset_done','rx_word_valid','training_error')}
    wrappers=[];configs=[];io_rows=[];boundary_rows=[];clock_rows=[];xdc=[]
    for bank in banks:
        record=bank['record'];recipe=bank['recipe'];instance=bank['instance'];direction=recipe.direction
        stem=f'bank{recipe.bank}_{direction}';prefix=f'{stem}_';config=stem+'.tsv'
        (output/config).write_text(recipe.configuration_tsv())
        configs.append((record['ip_module'],config))
        wrappers.append(selectio_bank_to_systemverilog(recipe,ip_module=record['ip_module'],module=bank['module'],include_common=False))
        signals=['pll_locked','reset_done',direction+'_ready']+(['rx_word_valid','training_error']if direction=='rx'else[])
        for signal in signals:
            name=prefix+signal;lines.append('  wire '+name+';');status[signal].append(name)
        connections=['.tdm_clk(fabric_clk)','.riu_clk(__emuflow_riu_clk)',
            '.reset('+('reset'if direction=='tx'else'reset || !__emuflow_all_tx_ready')+')',
            '.pll_locked('+prefix+'pll_locked)','.reset_done('+prefix+'reset_done)',
            '.bank_ready('+prefix+direction+'_ready)']
        slice=f"{_identifier(record['logical_port'])}[{record['logical_lsb']} +: 160]"
        if direction=='tx':connections+=['.payload_enable(__emuflow_payload_enable)',f'.tx_words({slice})']
        else:connections+=['.all_plls_locked(__emuflow_all_plls_locked)',
            '.alignment_enable(__emuflow_alignment_enable)',f'.rx_words({slice})',
            '.word_valid('+prefix+'rx_word_valid)','.training_error('+prefix+'training_error)']
        for lane,binding in enumerate(bank['bindings']):
            pair=binding['pair']
            for p in ('p','n'):
                connections.append(f".{pair[p]['port']}({binding[p]})")
                xdc += [f"set_property PACKAGE_PIN {pair[p]['package_pin']} [get_ports {{{binding[p]}}}]",
                        f"set_property IOSTANDARD LVDS [get_ports {{{binding[p]}}}]"]
            io_rows.append((direction,binding['p'],binding['n'],pair['p']['package_pin'],pair['n']['package_pin'],'LVDS'))
            if binding['role']=='clock':
                if direction=='rx':clock_rows.append((f'emuflow_rx_bank{recipe.bank}',binding['p'],'1.25'))
                continue
            for bit in range(8):
                logical=f"{record['logical_port']}[{record['logical_lsb']+8*lane+bit}]"
                # A hierarchical pin is resolved through its actual connected
                # leaf after synthesis; no optimizer name preservation is used.
                native=(f"{instance}/native_phy/{pair['word_port']}[{bit}]"if direction=='tx'
                    else f'{instance}/rx_words[{8*lane+bit}]')
                boundary_rows.append((logical,'word-'+direction,native,binding['p']))
        lines += [f"  {bank['module']} {instance} (",',\n'.join('    '+c for c in connections),'  );']
    if any(bank['record']['direction']=='rx'for bank in banks):wrappers.append(word_aligner_to_systemverilog())
    for signal,values in status.items():
        op=' | 'if signal=='training_error'else' & '
        expression=op.join(values)if values else("1'b0"if signal=='training_error'else"1'b1")
        lines.append(f'  assign __emuflow_local_{signal} = {expression};')
    lines+=['endmodule','']
    for name,port,period in [('emuflow_riu_clk','__emuflow_riu_clk','10'),*clock_rows]:
        xdc.append(f'create_clock -name {name} -period {period} [get_ports {{{port}}}]')
    (output/'physical_top.sv').write_text('\n'.join(lines))
    (output/'native-word-banks.sv').write_text('\n'.join(wrappers))
    (output/'native-word-fixed-io.xdc').write_text('\n'.join(xdc)+'\n')
    def table(name,header,rows,hex_columns=()):
        with(output/name).open('w')as stream:
            stream.write(header+'\n')
            for row in rows:stream.write('\t'.join(str(v).encode().hex()if i in hex_columns else str(v)for i,v in enumerate(row))+'\n')
    table('native-word-ips.tsv','module\tconfiguration',configs)
    table('fixed-io-query.tsv','direction\tp_hex\tn_hex\tp_pin_hex\tn_pin_hex\tiostandard_hex',io_rows,range(1,6))
    table('native-word-boundaries.tsv','logical_hex\tkind\tphysical_hex\tanchor_hex',boundary_rows,(0,2,3))
    table('native-word-clocks.tsv','clock_hex\tport_hex\tperiod_ns',
        [('emuflow_riu_clk','__emuflow_riu_clk','10'),*clock_rows],(0,1))
    artifacts={name:{'path':str(output/name),'sha256':file_sha256(output/name)}for name in names}
    artifacts.update({config:{'path':str(output/config),'sha256':file_sha256(output/config)}for _,config in configs})
    return {'schema':SCHEMA,'status':'pass','qualification':'native-word-physical-top-preparation',
        'fpga':fpga,'part':part,'top':top,'hierarchy_prefix':'mapped_partition',
        'native_phy':plan['native_phy'],'source_board_sha256':plan['source']['board_sha256'],
        'source_word_plan_sha256':_digest(plan),'active_banks':len(banks),
        'data_pairs':20*len(banks),'forwarded_clock_pairs':len(banks),'physical_ports':len(physical),
        'external_services':{'inputs':list(SERVICE_INPUTS),'outputs':list(SERVICE_OUTPUTS),
            'contract':'common synchronous global startup/readiness consensus and pausible DUT clock service; no distributed barrier implementation'},
        'artifacts':artifacts,'physical_implementation_qualified':False}
