"""Clock facts owned by the v4 TimingPathDB, one typed row per original path.

The relation shares its atomic generation/seal with paths and path_nets.
Phase2 import validates each engine record once; indexed consumers borrow an
ordered cursor rather than duplicating budgets per net or loading a path map.
"""
import math

from .clock_budget import clock_edge_budget_from_engine_fields
from .errors import ValidationError

SCHEMA='emuflow.sta-path-database/v4'
FORMAT='sqlite-sta-path/v2'
STORE_SCHEMA='timing-path-store-v2'
FIELDS=('source_clock','source_period_ns','launch_edge_ns','capture_edge_ns',
    'source_offset_ns','target_cycle_time_ns','multicycle_adjustment_ns',
    'check_margin_ns','source_latency_ns','target_clock_delay_ns','clock_skew_ns',
    'output_delay','path_delay','latch_check')
TSV_SUFFIX='\t'.join('source_clock_hex'if key=='source_clock'else key for key in FIELDS)
DDL='CREATE TABLE clock_budgets(path_ordinal INTEGER PRIMARY KEY REFERENCES paths(ordinal),'+','.join(
    name+(' TEXT NOT NULL CHECK(length(source_clock)>0)'if name=='source_clock'else
        ' INTEGER NOT NULL CHECK('+name+' IN (0,1))'if name in FIELDS[-3:]else' REAL NOT NULL')for name in FIELDS)+',CHECK(source_period_ns>0 AND target_cycle_time_ns+multicycle_adjustment_ns-launch_edge_ns>0));'


def validate_clock_budget(record):
    facts=record.get('clock_budget')
    if not isinstance(facts,dict)or set(facts)!=set(FIELDS):
        raise ValidationError('STA path clock budget fields are incomplete or invalid')
    for name in FIELDS[1:]:
        value=facts[name]
        if isinstance(value,bool)or not isinstance(value,(int,float))or not math.isfinite(value):
            raise ValidationError('STA path clock budget numeric fact is invalid: '+name)
        if name in FIELDS[-3:]and (not isinstance(value,int)or value not in (0,1)):
            raise ValidationError('STA path clock budget check classification is invalid')
    return clock_edge_budget_from_engine_fields({**facts,
        'target_clock':record['clock_domain'],'target_period_ns':record['clock_period_ns']})


def parse_tsv_budget(fields,record):
    if len(fields)!=len(FIELDS):raise ValidationError('STA path clock budget TSV width differs')
    try:
        facts={name:(bytes.fromhex(value).decode('utf-8')if name=='source_clock'else
            int(value)if name in FIELDS[-3:]else float(value))for name,value in zip(FIELDS,fields)}
    except (ValueError,UnicodeDecodeError)as error:
        raise ValidationError('STA path clock budget TSV fact is invalid')from error
    record['clock_budget']=facts
    validate_clock_budget(record)


class ClockBudgetCursor:
    def __init__(self,connection,path_ids=None):
        if connection.execute('PRAGMA user_version').fetchone()[0]!=2:
            raise ValidationError('STA clock budget storage version differs')
        columns=tuple(row[1]for row in connection.execute('PRAGMA table_info(clock_budgets)'))
        if columns!=('path_ordinal',*FIELDS):raise ValidationError('STA clock budget storage columns differ')
        where='';args=()
        join=''
        if path_ids is not None:
            args=tuple(path_ids)
            if not args:raise ValidationError('empty indexed clock budget selection')
            where=' WHERE p.path_id IN ('+','.join('?'for _ in args)+')'
            join=' JOIN paths p ON p.ordinal=b.path_ordinal'
        self.rows=iter(connection.execute('SELECT b.path_ordinal,'+','.join('b.'+key for key in FIELDS)+
            ' FROM clock_budgets b'+join+where+' ORDER BY b.path_ordinal',args))

    def attach(self,ordinal,record):
        row=next(self.rows,None)
        if row is None or row[0]!=ordinal:raise ValidationError('STA path clock budget coverage/order differs')
        record['clock_budget']=dict(zip(FIELDS,row[1:]))
        validate_clock_budget(record)

    def finish(self):
        if next(self.rows,None)is not None:raise ValidationError('STA path clock budget has extra rows')
