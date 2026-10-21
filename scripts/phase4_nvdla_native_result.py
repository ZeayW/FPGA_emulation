#!/usr/bin/env python3
"""Seal successful native output and recover its original generalized index map."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from emuflow.io import read_json, write_json
from emuflow.partition_ir import load_partition_ir
from emuflow.partition import build_clusters, load_partition_constraints
from emuflow.phase3_storage import compress_phase3_clusters
from emuflow.platform import Platform
from emuflow.patron_result import (file_record, seal_completed_result, validate_completed_result,
                                   validate_mapping_producer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ir','platform','timing','constraints','native-input','native-output',
                 'binary','native-source','receipt','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--producer-commit',required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must be new')
    validate_mapping_producer(args.producer_commit)
    platform = Platform.load(args.platform)
    ir = load_partition_ir(args.ir)
    constraints = load_partition_constraints(args.constraints,ir,platform,min_used_fpgas=4)
    clusters = build_clusters(ir,constraints,cut_mode='static-exact-combinational')
    context = {'ir':args.ir,'platform':args.platform,'timing':args.timing}
    timing = read_json(args.timing)
    for key,record in timing.get('payloads',{}).items():
        context['timing_payload_'+key] = args.timing.parent/record['path']
    value = seal_completed_result(native_input=args.native_input,native_output=args.native_output,
        binary=args.binary,native_source=args.native_source,receipt=args.receipt,
        producer_commit=args.producer_commit,clusters=(c['id'] for c in clusters['clusters']),
        parts=(p.id for p in platform.fpgas),context=context)
    args.output.mkdir(parents=True)
    write_json(args.output/'clusters.json',compress_phase3_clusters(clusters),compact=True)
    value['context']['clusters'] = file_record(args.output/'clusters.json')
    write_json(args.output/'completed-native.json',value)
    validate_completed_result(args.output/'completed-native.json')


if __name__=='__main__':
    main()
