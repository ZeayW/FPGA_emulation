"""Bind native timing sources to their declared input or transport roles.

Source membership, numerical timing and physical acceptance are separate.
An unknown source remains visible; it never becomes a constant or a zero arc.
Only interface/control records are retained here, not the DUT timing graph.
"""
from collections import Counter
from contextlib import closing
import csv
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import sqlite3

from .errors import ValidationError
from .io import file_sha256,write_json
from .store_publication import StoreSource,bind_store_source,require_store_sources_unchanged
from .word_transport_rtl import PHYSICAL_WORD_CALENDAR_POLICY


_INPUT = re.compile(r'^set_input_delay -max ([^ ]+) -clock \[get_clocks \{([^}]+)\}\] \[get_ports \{([^}]+)\}\]$')
_STARTUP = ('common synchronous global startup/readiness consensus and pausible DUT clock service; '
            'no distributed barrier implementation')


def _require(ok,message):
    if not ok:raise ValidationError(message)


def _rows(path):
    with path.open(newline='')as stream:yield from csv.DictReader(stream,delimiter='\t')


def _name(value):
    try:
        data=bytes.fromhex(value);name=data.decode()
        if not name or data.hex()!=value or '\0'in name:raise ValueError()
        return name
    except(ValueError,UnicodeDecodeError):raise ValidationError('native context identity is invalid')from None


def _input_environment(path,clocks):
    """Read only the canonical producer's literal XDC; never execute Tcl."""
    inputs={}
    for line in path.read_text().splitlines():
        if not line.startswith('set_input_delay '):continue
        match=_INPUT.fullmatch(line)
        _require(match is not None,'native source context requires an explicit canonical input environment')
        delay,clock,port=match.groups()
        try:delay=float(delay)
        except ValueError:raise ValidationError('native source input delay is invalid')from None
        _require(math.isfinite(delay)and delay>=0 and clock in clocks,'native source input clock or arrival is invalid')
        _require(port not in inputs,'native source input environment repeats a port')
        inputs[port]={'clock':clock,'launch_edge':'rise','max_arrival_ns':delay}
    return inputs


def qualify_native_word_source_contexts(record,*,runtime_constraints,word_preparation,
                                       calendar_policy,output,capture_census=None,source_properties=None):
    """Observe TX and complete capture-family source membership on one DCP.

    The optional capture census is an immutable producer receipt with its
    checkpoint/input seals and closed all_fanin exports. Without it, capture
    coverage remains incomplete. Unknown membership does not reject P&R or
    discard measured timing. This report does not certify startup hardware,
    external minimum delays, original-path dispositions, or timing closure.
    """
    parents=[];artifacts={}
    def artifact(reference,schema='physical-input-document-v1'):
        path=Path(reference['path']).resolve()
        if path in artifacts:
            _require(artifacts[path]==reference['sha256'],'native source context has conflicting artifact identities')
            return path
        parents.append(bind_store_source(path,schema))
        _require(file_sha256(path)==reference['sha256'],'native source context artifact changed: '+path.name)
        artifacts[path]=reference['sha256']
        return path
    observation=record['observation'];fpga=observation['fpga']
    _require(observation['status']=='complete','native source context requires closed observations')
    roles=record['sources']
    _require(roles.get('status')=='complete'and roles.get('fpga')==fpga and
             roles.get('directory')==observation['source_directory'],
             'native source context catalog belongs to another observation')
    parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))for p in observation['source_generations'])
    xdc=artifact(runtime_constraints);preparation_path=artifact(word_preparation)
    preparation=json.loads(preparation_path.read_text())
    _require(preparation.get('schema')=='emuflow.vivado-word-top-preparation/v1'and
             preparation.get('status')=='pass'and preparation.get('fpga')==fpga,
             'native source context has a different physical top owner')
    contract=artifact(record['clock_contract'])
    clocks={_name(row['clock_hex'])for row in _rows(contract)
            if re.fullmatch(r'emuflow_dut_clk_[0-9]+',_name(row['clock_hex']))}
    _require(len(clocks)==observation['counts']['dut_clocks'],'native source context clock population differs')
    environment=_input_environment(xdc,clocks)
    mapped=artifact(record['input_identity']['mapped_index'],'xilinx-mapped-index-v2')
    lineage=artifact(record['lineage'],'vivado-lineage-store-v2')
    tx=artifact(observation['files']['native-costs.tsv'])
    contexts=Counter()
    for row in _rows(tx):
        if row['source_kind']not in('dut-clock','rx'):
            contexts['tx',row['source_kind'],_name(row['source_hex'])]+=1
    capture_status='pending-capture-source-census'
    if capture_census is not None:
        census_path=artifact(capture_census);census=json.loads(census_path.read_text())
        _require(census.get('status')=='complete-unconditional-native-capture-source-census'and
                 census.get('fpga')==fpga and census.get('original_checkpoint_unchanged')is True,
                 'native capture source census owner or completion differs')
        sealed=census['input_sha256']
        # Retained implementations may relocate identical input files. Bind
        # the census's original owners, then compare their content identities.
        for suffix,digest in [('.dcp',observation['checkpoint']['sha256']),
                              ('runtime.xdc',runtime_constraints['sha256'])]:
            owners=[(path,value)for path,value in sealed.items()if
                    (Path(path).suffix==suffix if suffix=='.dcp'else Path(path).name==suffix)]
            _require(len(owners)==1and owners[0][1]==digest,
                     'native capture source census uses a different checkpoint or input environment')
            artifact(dict(path=owners[0][0],sha256=digest))
        for path,digest in sealed.items():artifact(dict(path=path,sha256=digest))
        files={name:artifact(ref)for name,ref in census['files'].items()}
        family_rows=list(_rows(files['source-census.tsv']))
        families={_name(row['capture_clock_hex']):row for row in family_rows}
        _require(len(families)==len(family_rows)and set(families)==clocks,
                 'native capture source census omits or repeats a DUT family')
        counts=Counter();seen=set()
        for row in _rows(files['external-sources.tsv']):
            clock=_name(row['capture_clock_hex']);kind=row['source_kind'];name=_name(row['source_hex'])
            _require(clock in clocks and kind in('infrastructure_cells','ports','unclassified'),
                     'native capture census source scope is invalid')
            key=(clock,kind,name);_require(key not in seen,'native capture census repeats a source');seen.add(key)
            counts[clock,kind]+=1
            contexts['capture:'+clock,'infrastructure'if kind=='infrastructure_cells'else kind,name]+=1
        for clock,row in families.items():
            _require(all(int(row[k])>=0 for k in('native_startpoints','dut_cells','rx_sources','infrastructure_cells','ports','unclassified'))and
                     int(row['target_pins'])>=0 and
                     (int(row['target_pins'])>0 or int(row['native_startpoints'])==0),
                     'native capture census has invalid population counts')
            for kind in('infrastructure_cells','ports','unclassified'):
                _require(counts[clock,kind]==int(row[kind]),'native capture census source population differs')
        missing=artifact(observation['files']['missing-rx-roles.tsv'])
        missing_rx={row['source_origin']for row in _rows(missing)}
        role_directory=Path(observation['source_directory'])
        role_path=role_directory/'rx-roles.tsv'
        artifact(dict(path=str(role_path),sha256=roles['files']['rx-roles.tsv']['sha256']))
        requested_rx={row['source_origin']for row in _rows(role_path)}
        received=Counter();rx_seen=set()
        for row in _rows(files['rx-sources.tsv']):
            clock=_name(row['capture_clock_hex']);origin=row['source_origin'];key=clock,origin
            _require(clock in clocks and origin in requested_rx and origin not in missing_rx and key not in rx_seen,
                     'native capture census RX role differs from its observed source request')
            rx_seen.add(key);received[clock]+=1
        _require(all(received[clock]==int(row['rx_sources'])for clock,row in families.items()),
                 'native capture census RX source population differs')
        capture_status='complete'
    constants={}
    if source_properties is not None:
        from .native_word_source_constants import read_native_source_constants
        properties_path=artifact(source_properties);properties=json.loads(properties_path.read_text())
        _require(capture_census is not None and properties.get('status')=='complete-native-source-property-observations'and
                 properties.get('fpga')==fpga,'native source properties belong to another capture census')
        sealed=properties['input_sha256']
        checkpoints=[digest for name,digest in sealed.items()if Path(name).suffix=='.dcp']
        _require(checkpoints==[observation['checkpoint']['sha256']]and
                 sealed.get(str(Path(capture_census['path'])))==capture_census['sha256'],
                 'native source properties use a different checkpoint or capture census')
        for path,digest in sealed.items():artifact(dict(path=path,sha256=digest))
        constants,requested=read_native_source_constants(artifact(properties['file']))
        _require(len(requested)==properties['requests'],
                 'native source property population differs')
    supported=(calendar_policy==PHYSICAL_WORD_CALENDAR_POLICY and
               preparation.get('external_services',{}).get('contract')==_STARTUP and
               '__emuflow_payload_enable'in preparation.get('external_services',{}).get('inputs',[]))
    with closing(sqlite3.connect(mapped.as_uri()+'?mode=ro&immutable=1',uri=True))as source,closing(
            sqlite3.connect(lineage.as_uri()+'?mode=ro&immutable=1',uri=True))as native:
        owner=dict(native.execute('SELECT * FROM metadata'))
        _require(owner.get('mapped_index_sha256')==record['input_identity']['mapped_index']['sha256']and
                 owner.get('retained_stage')=='routed','native source lineage belongs to another mapped snapshot')
        controls={}
        if supported:
            metadata=dict(source.execute('SELECT * FROM metadata'))
            ports=json.loads(metadata.get('ports_json','{}'))
            fabric=ports.get('fabric_clk',{}).get('bits',[])
            for alias,role in [('payload_active','transport-payload-active-state'),
                               ('payload_warmup','transport-startup-warmup-state')]:
                row=source.execute('SELECT record_json FROM netnames WHERE id=?',('__emuflow_transport__/'+alias,)).fetchone()
                if row is None:continue
                for bit in json.loads(row[0])['bits']:
                    if type(bit)is not int:continue
                    for(origin,)in source.execute("SELECT cell_seq FROM cell_pins WHERE port='Q'AND direction=1 AND value_kind=0 AND net_bit=?",(bit,)):
                        state=source.execute("SELECT c.type,c.record_json,p.net_bit FROM cells c LEFT JOIN cell_pins p "
                            "ON p.cell_seq=c.seq AND p.port='C'AND p.value_kind=0 WHERE c.seq=?",(origin,)).fetchone()
                        if (len(fabric)==1and state is not None and state[0]=='FDRE'and state[2]==fabric[0]and
                            str(json.loads(state[1]).get('parameters',{}).get('IS_C_INVERTED','0')).strip('0')==''):
                            controls[origin]=role
        @lru_cache(maxsize=None)
        def classify(kind,name):
            if kind=='unclassified'and name in constants:
                return dict(role='static-native-startpoint',coverage='native-constant-net-or-primitive-function',
                            constant=constants[name])
            if kind=='ports':
                matches=[value for port,value in environment.items()if name==port or
                         (port.endswith('[*]')and re.fullmatch(re.escape(port[:-3])+r'\[[0-9]+\]',name))]
                _require(len(matches)<=1,'native source input environment is ambiguous')
                if matches:return dict(role='original-clocked-input',coverage='native-launch-clock-families',**matches[0])
                if supported and name in('reset','links_ready','__emuflow_payload_enable'):
                    return dict(role='runtime-startup-service',coverage='declared-common-startup-and-pausible-clock-service')
            if supported and kind=='infrastructure':
                row=native.execute("SELECT origin FROM objects WHERE stage='routed'AND name=?",(name,)).fetchone()
                if row is not None and row[0]in controls:
                    return dict(role=controls[row[0]],source_origin='s'+str(row[0]),coverage='fixed-word-clock-control-checks')
            return dict(role='unresolved-source-context',coverage='pending')
        rows=[dict(scope=scope,source_kind=kind,source=name,rows=count,**classify(kind,name))
              for(scope,kind,name),count in sorted(contexts.items())]
    pending=[r for r in rows if r['role']=='unresolved-source-context']
    result=dict(schema='emuflow.native-word-source-contexts/v1',fpga=fpga,
        status='complete'if capture_status=='complete'and not pending else'incomplete',
        capture_source_coverage=capture_status,source_contexts=rows,unresolved_contexts=len(pending),
        operating_scope='continuous payload with the declared common startup/readiness and pausible DUT clock service',
        original_input_minimum_delays='not-qualified',timing_acceptance='separate',
        sources=[dict(path=str(p.path),schema=p.schema,generation=list(p.generation))for p in parents])
    require_store_sources_unchanged(parents);write_json(Path(output),result)
    return result
