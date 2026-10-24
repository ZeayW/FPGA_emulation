#!/usr/bin/env python3
"""Embed the installed SQL schema into native builds without a second DDL copy."""
import argparse
from pathlib import Path


def generate(output):
    root = Path(__file__).resolve().parents[1]
    text = (root / 'src/native/emuflow_core/assignment_schema.hpp.in').read_text(encoding='utf-8')
    for name in ('core', 'cuts'):
        ddl = (root / ('src/emuflow/data/schema/phase3_assignment_' + name + '.sql')).read_text(encoding='utf-8')
        if ')EMUFLOW_SQL"' in ddl:
            raise ValueError('SQL schema contains the native raw-string delimiter')
        text = text.replace('@EMUFLOW_ASSIGNMENT_' + name + '_DDL@', ddl)
    if '@EMUFLOW_ASSIGNMENT_' in text:
        raise ValueError('native schema template has an unresolved field')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or output.read_text(encoding='utf-8') != text:
        output.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    generate(parser.parse_args().output)
