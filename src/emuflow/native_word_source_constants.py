"""Recognize static native startpoints from routed primitive properties.

These facts describe the implemented pin, not an original-path elimination.
They supply no numerical delay. Unsupported or dynamic sources stay unknown.
"""
import csv
from functools import lru_cache
import re

from .errors import ValidationError


def read_native_source_constants(path):
    properties = {}
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if reader.fieldnames != ['kind', 'object_hex', 'property', 'value_hex']:
            raise ValidationError('native source property header differs')
        for row in reader:
            try:
                name = bytes.fromhex(row['object_hex']).decode()
                value = bytes.fromhex(row['value_hex']).decode()
                if not name or '\0' in name or '\0' in value:
                    raise ValueError()
            except (ValueError, UnicodeDecodeError, TypeError):
                raise ValidationError('native source property identity is invalid') from None
            if row['kind'] not in ('cell', 'pin', 'net', 'connection', 'query'):
                raise ValidationError('native source property object kind differs')
            values = properties.setdefault((row['kind'], name), {})
            key = row['property']
            if key in values and values[key] != value:
                raise ValidationError('native source properties contradict each other')
            values[key] = value

    def fields(kind, name):
        return properties.get((kind, name), {})

    @lru_cache(maxsize=None)
    def pin_constant(name):
        names = fields('connection', name).get('net_names', '').splitlines()
        if not names:
            return None
        types = {fields('net', net).get('TYPE') for net in names}
        if types == {'GROUND'}:
            return 0
        if types == {'POWER'}:
            return 1
        return None

    def possibilities(owner, pin):
        value = pin_constant(owner + '/' + pin)
        return {0, 1} if value is None else {value}

    def constant(name):
        query = fields('query', name)
        if query.get('pin_matches') != '1':
            return None
        value = pin_constant(name)
        if value is not None:
            return {'value': value, 'method': 'native-constant-net'}
        owner, _, pin = name.rpartition('/')
        cell = fields('cell', owner)
        if cell.get('matches') != '1' or fields('pin', name).get('REF_PIN_NAME') != pin:
            return None
        ref = cell.get('REF_NAME')
        if ref == 'DSP_A_B_DATA':
            match = re.fullmatch(r'([AB])2_DATA\[([0-9]+)\]', pin)
            if match is None:
                return None
            bus, bit = match[1], int(match[2])
            if bit >= (27 if bus == 'A' else 18) or cell.get(bus + 'REG') != '0':
                return None
            mode = cell.get(bus + '_INPUT')
            if mode not in ('DIRECT', 'CASCADE'):
                return None
            selected = owner + '/' + (bus if mode == 'DIRECT' else bus + 'CIN') + f'[{bit}]'
            if fields('pin', selected).get('DIRECTION') != 'IN':
                return None
            value = pin_constant(selected)
            if value is not None:
                return {'value': value, 'method': 'native-DSP-unregistered-constant-input',
                        'primitive': ref, 'input_pin': selected,
                        'register_parameter': bus + 'REG', 'register_value': 0,
                        'input_selection': mode}
        if ref == 'CARRY8':
            match = re.fullmatch(r'(O|CO)\[([0-9]+)\]', pin)
            kind = cell.get('CARRY_TYPE')
            if match is None or int(match[2]) >= 8 or kind not in ('SINGLE_CY8', 'DUAL_CY4'):
                return None
            bit = int(match[2])
            begin = 4 if kind == 'DUAL_CY4' and bit >= 4 else 0
            carry = possibilities(owner, 'CI_TOP' if begin else 'CI')
            # Treat all unknown inputs independently. This overapproximates
            # correlations, so a singleton result proves a constant safely.
            for index in range(begin, bit + int(match[1] == 'CO')):
                select = possibilities(owner, f'S[{index}]')
                data = possibilities(owner, f'DI[{index}]')
                carry = (carry if 1 in select else set()) | (data if 0 in select else set())
            if match[1] == 'O':
                carry = {s ^ c for s in possibilities(owner, f'S[{bit}]') for c in carry}
            if len(carry) == 1:
                return {'value': next(iter(carry)), 'method': 'native-CARRY8-constant-function',
                        'primitive': ref, 'carry_type': kind}
        return None

    requested = {name for kind, name in properties if kind == 'query'}
    return {name: proof for name in sorted(requested) if (proof := constant(name)) is not None}, requested
