"""Bounded bank recipes for native SelectIO integration experiments.

The package inventory owns pin identity. This adapter only derives a wizard
configuration and expected port binding; generation/synthesis observations must
still check that configuration. It does not characterize a PHY or a board.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .errors import ValidationError
from .virtual_board import _inventory, _bank_pairs

RX_ALIGNMENT_POLICIES = ('native-bit-slip-v1', 'word-two-stage-v1')

@dataclass(frozen=True)
class SelectIOBankRecipe:
    part: str
    bank: str
    direction: str
    settings: tuple[tuple[str, str], ...]
    data_pairs: tuple[dict, ...]
    clock_pair: dict
    rx_alignment: str = 'native-bit-slip-v1'

    def configuration_tsv(self):
        return "property\tvalue\n" + "".join(f"CONFIG.{key}\t{value}\n" for key,value in self.settings)


def selectio_bank_recipe(bank: str, direction: str, *, inventory=None,
                        rx_alignment: str = 'native-bit-slip-v1'):
    if direction not in {"tx", "rx"}:
        raise ValidationError("SelectIO bank direction must be tx or rx")
    if rx_alignment not in RX_ALIGNMENT_POLICIES or (direction == 'tx' and rx_alignment != RX_ALIGNMENT_POLICIES[0]):
        raise ValidationError('unsupported SelectIO receive alignment policy')
    if not isinstance(bank,str) or not re.fullmatch(r"[0-9]+",bank) or bank == "65":
        raise ValidationError("SelectIO recipe requires a non-configuration HP bank")
    inventory = _inventory() if inventory is None else inventory
    if inventory.get("part") != "xcvu19p-fsva3824-2-e":
        raise ValidationError("SelectIO recipe requires its exact package inventory")
    matches = [item for item in inventory["banks"] if str(item["id"]) == bank]
    if len(matches) != 1 or matches[0]["type"] != "HP":
        raise ValidationError("SelectIO bank is absent or ambiguous in package inventory")
    pairs = _bank_pairs(matches[0])
    data = [pair for pair in pairs if "_GC_" not in pair["p"]["pin_function"]]
    clocks = [pair for pair in pairs if "_GC_QBC_" in pair["p"]["pin_function"]]
    if len(data) != 20 or len(clocks) != 1:
        raise ValidationError("SelectIO bank requires twenty data pairs and one GC/QBC clock pair")

    def position(pair):
        positions=[]
        for polarity in ("p","n"):
            match=re.fullmatch(r"IO_L[0-9]+[PN]_T([0-3])[LU]_N([0-9]+)_[A-Z0-9_]+",pair[polarity]["pin_function"])
            if match is None:
                raise ValidationError("SelectIO pin lacks a native byte/bit identity")
            positions.append(tuple(map(int,match.groups())))
        byte,pin=positions[0]
        if pin not in {0,2,4,6,8,10} or positions[1] != (byte,pin+1):
            raise ValidationError("SelectIO differential pair has inconsistent native byte/bit identities")
        return byte,pin

    selected = {position(pair):pair for pair in (*data,*clocks)}
    if len(selected) != 21:
        raise ValidationError("SelectIO native pair positions overlap")
    settings=[("BANK",bank+"_(HP)"),("BUS_DIR","0"if direction=="tx"else"1"),
        ("SERIALIZATION_FACTOR","8"),("PLL0_DATA_SPEED","1600.00"),
        # These wizard controls are enumerations, despite looking numeric.
        ("PLL0_INPUT_CLK_FREQ","200.000"if direction=="tx"else"800.000"),
        ("PLL0_CLK_SOURCE","BUFG_TO_PLL"if direction=="tx"else"IBUF_TO_PLL"),
        ("DIFFERENTIAL_IO_STD","LVDS"),("APPEND_PIN_NO","1")]
    for byte in range(4):
        # Native wizard defaults include otherwise unwanted single-ended pins.
        settings.append((f"ENABLE_BYTE{byte}_PIN12","false"))
        for pin in (0,2,4,6,8,10):
            settings.append((f"ENABLE_BYTE{byte}_PIN{pin}","true"if(byte,pin)in selected else"false"))
    bindings=[]
    for pair in data:
        byte,pin=position(pair);key=f"BYTE{byte}_PIN{pin}"
        name="data_"+pair["pair_id"].lower()
        settings.extend([(key+"_SIG_TYPE","DIFF"),(key+"_BUS_DIR",direction.upper()),
            (key+"_SIGNAL_NAME",name+"_p"),(f"BYTE{byte}_PIN{pin+1}_SIGNAL_NAME",name+"_n")])
        native=byte*13+pin
        bindings.append({"pair_id":pair["pair_id"],"native_pin":native,
            "p":{"port":f"{name}_p_{native}","package_pin":pair["p"]["package_pin"]},
            "n":{"port":f"{name}_n_{native+1}","package_pin":pair["n"]["package_pin"]},
            "word_port":f"data_{'from'if direction=='tx'else'to'}_fabric_{name}_p_{native}"})
    clock=clocks[0];byte,pin=position(clock);key=f"BYTE{byte}_PIN{pin}"
    settings.extend([(key+"_SIG_TYPE","DIFF"),(key+"_BUS_DIR",direction.upper()),
                     (key+"_DATA_STROBE","Clk Fwd"if direction=="tx"else"Input Clock")])
    clock_binding={"pair_id":clock["pair_id"],"native_pin":byte*13+pin}
    if direction=="tx":
        settings.extend([(key+"_SIGNAL_NAME","forward_p"),(f"BYTE{byte}_PIN{pin+1}_SIGNAL_NAME","forward_n")])
        clock_binding.update(word_port=f"data_from_fabric_forward_p_{byte*13+pin}",word_value="8'h55")
    else:
        settings.extend([("ENABLE_BITSLIP","1"if rx_alignment=='native-bit-slip-v1'else"0"),("EN_MULTI_INTF_PORTS","1")])
    for polarity in ("p","n"):
        port=f"forward_{polarity}_{byte*13+pin+(polarity=='n')}"if direction=="tx"else"clk_"+polarity
        clock_binding[polarity]={"port":port,"package_pin":clock[polarity]["package_pin"]}
    return SelectIOBankRecipe(inventory["part"],bank,direction,tuple(settings),tuple(bindings),clock_binding,rx_alignment)


def validate_selectio_observations(recipe: SelectIOBankRecipe, directory: Path):
    """Check one bounded native-IP census, without claiming routed PHY timing."""
    identity_path=directory/'tool-identity.tsv'
    if not identity_path.is_file()or identity_path.stat().st_size>16384:
        raise ValidationError('SelectIO tool/device identity is missing or oversized')
    identity_lines=identity_path.read_text().splitlines()
    if not identity_lines or identity_lines[0]!='field\tvalue':
        raise ValidationError('SelectIO tool identity header is invalid')
    identity={}
    for row in identity_lines[1:]:
        fields=row.split('\t')
        if len(fields)!=2 or fields[0]in identity:raise ValidationError('SelectIO tool identity is invalid')
        identity[fields[0]]=fields[1]
    if (identity.get('part')!=recipe.part or identity.get('ip_definition')!='xilinx.com:ip:high_speed_selectio_wiz:3.6'
        or not identity.get('vivado_version')):
        raise ValidationError('SelectIO tool/device identity differs from the supported native recipe')
    def rows(name,header,limit):
        path=directory/name
        if not path.is_file() or path.stat().st_size > 2*1024*1024:
            raise ValidationError("SelectIO observation is absent or exceeds one-bank bound")
        with path.open(encoding="utf-8")as stream:
            if next(stream,"").rstrip("\r\n")!=header:
                raise ValidationError("SelectIO observation header is invalid")
            for ordinal,line in enumerate(stream):
                fields=line.rstrip("\r\n").split("\t")
                if ordinal>=limit or len(fields)!=2:
                    raise ValidationError("SelectIO observation row is invalid")
                try:key=bytes.fromhex(fields[0]).decode("utf-8")
                except (ValueError,UnicodeDecodeError)as error:
                    raise ValidationError("SelectIO observation identity encoding is invalid")from error
                yield key,fields[1]
    properties={}
    for key,encoded in rows('ip-properties.tsv','property_hex\tvalue_hex',16384):
        try:value=bytes.fromhex(encoded).decode('utf-8')
        except (ValueError,UnicodeDecodeError)as error:
            raise ValidationError("SelectIO property encoding is invalid")from error
        if key in properties:raise ValidationError("duplicate SelectIO property")
        properties[key]=value
    for key,expected in recipe.settings:
        actual=properties.get('CONFIG.'+key)
        if actual==expected:continue
        try:equal=float(actual)==float(expected)
        except (TypeError,ValueError):equal=False
        if not equal:raise ValidationError("SelectIO observed configuration differs from recipe: "+key)
    ports={}
    for key,direction in rows('synthesis-ports.tsv','name_hex\tdirection',4096):
        if key in ports or direction not in {'IN','OUT','INOUT'}:
            raise ValidationError("SelectIO synthesis census has duplicate or invalid ports")
        ports[key]=direction
    from .lvds_bank_phy import selectio_core_ports
    expected_ports={f'{name}[{bit}]'if width>1 else name:direction
        for name,(direction,width)in selectio_core_ports(recipe).items()for bit in range(width)}
    if ports!=expected_ports:
        raise ValidationError('SelectIO native interface differs from the supported full-bank port contract')
    physical_direction,word_direction=('OUT','IN')if recipe.direction=='tx'else('IN','OUT')
    expected_data=set()
    expected_words=set()
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        for polarity in ('p','n'):
            physical=pair[polarity]
            name=physical['port']
            if ports.get(name)!=physical_direction:
                raise ValidationError("SelectIO physical pair port is absent or has wrong direction: "+name)
            if pair is not recipe.clock_pair:expected_data.add(name)
        byte,pin=divmod(pair['native_pin'],13)
        for delta,polarity in ((0,'p'),(1,'n')):
            if properties.get(f'CONFIG.BYTE{byte}_PIN{pin+delta}_LOC')!=pair[polarity]['package_pin']:
                raise ValidationError("SelectIO package location differs from declared P/N pair")
        if 'word_port'in pair:
            for bit in range(8):
                name=f"{pair['word_port']}[{bit}]"
                if ports.get(name)!=word_direction:
                    raise ValidationError("SelectIO parallel word bit is absent or has wrong direction: "+name)
                expected_words.add(name)
    actual_data={name for name in ports if re.fullmatch(r'data_l[0-9]+_[pn]_[0-9]+',name)}
    if actual_data!=expected_data:
        raise ValidationError("SelectIO synthesis has an unexpected data pair population")
    complements=set()
    if recipe.direction=='rx':
        complements={f"data_to_fabric_{pair['n']['port']}[{bit}]"for pair in recipe.data_pairs for bit in range(8)}
        if any(ports[name]!='OUT'for name in complements&ports.keys()):
            raise ValidationError("SelectIO complement word must be an unused output")
    actual_words={name for name in ports if name.startswith('data_from_fabric_')or name.startswith('data_to_fabric_')}
    if actual_words-expected_words-complements:
        raise ValidationError("SelectIO synthesis has unexpected parallel data words")
    return {"status":"pass","qualification":"native-IP-configuration-and-port-census",
        "part":recipe.part,"bank":recipe.bank,"direction":recipe.direction,
        "vivado_version":identity['vivado_version'],
        "data_lanes":len(recipe.data_pairs),"payload_word_bits":8*len(recipe.data_pairs),
        "forwarded_clock_pairs":1,"unused_complement_word_bits":len(actual_words&complements),
        "physical_implementation_qualified":False}


def validate_selectio_xci_ports(recipe: SelectIOBankRecipe, path: Path):
    """Audit the complete generated-IP boundary, including unused outputs."""
    from .lvds_bank_phy import selectio_core_ports
    import hashlib
    if not path.is_file()or path.stat().st_size>4*1024*1024:
        raise ValidationError('native SelectIO XCI is missing or exceeds its one-bank bound')
    payload=path.read_bytes()
    try:
        ports=json.loads(payload)['ip_inst']['boundary']['ports']
        if not isinstance(ports,dict):raise ValueError('port object')
        actual={}
        for name,entries in ports.items():
            if not isinstance(entries,list)or len(entries)!=1:raise ValueError('port entry')
            item=entries[0];direction={'in':'IN','out':'OUT'}[item['direction']]
            limits=[item.get('size_left','0'),item.get('size_right','0')]
            if any(not isinstance(n,str)or re.fullmatch(r'[0-9]+',n)is None for n in limits):
                raise ValueError('vector limit')
            width=abs(int(limits[0])-int(limits[1]))+1
            if not 1<=width<=4096 or limits!=[str(width-1),'0']:raise ValueError('vector width/order')
            actual[name]=(direction,width)
    except (KeyError,TypeError,ValueError)as error:
        raise ValidationError('native SelectIO XCI port boundary is invalid')from error
    if actual!=selectio_core_ports(recipe):
        raise ValidationError('native SelectIO XCI ports differ from the complete wrapper interface')
    return {'status':'pass','rx_alignment':recipe.rx_alignment,
        'scalar_port_bits':sum(width for direction,width in actual.values()),
        'xci_sha256':hashlib.sha256(payload).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank",required=True)
    parser.add_argument("--direction",choices=("tx","rx"),required=True)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--check-observations",type=Path)
    args=parser.parse_args()
    if (args.output is None)==(args.check_observations is None):
        parser.error('choose exactly one of --output or --check-observations')
    recipe=selectio_bank_recipe(args.bank,args.direction)
    if args.check_observations is not None:
        print(json.dumps(validate_selectio_observations(recipe,args.check_observations),sort_keys=True))
        return
    args.output.write_text(recipe.configuration_tsv())
    print(json.dumps({"part":recipe.part,"bank":recipe.bank,"direction":recipe.direction,
        "data_pairs":len(recipe.data_pairs),"word_bits_per_cycle":8*len(recipe.data_pairs),
        "clock_pair":recipe.clock_pair,"scope":"native-IP-configuration-only"},sort_keys=True))


if __name__=="__main__":main()
