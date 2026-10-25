"""Pin-identity and lane-accounting checks; actual IP observations are separate."""
import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.lvds_selectio import selectio_bank_recipe, validate_selectio_observations
from emuflow.virtual_board import _inventory


@pytest.mark.parametrize('policy',('native-bit-slip-v1','word-two-stage-v1'))
@pytest.mark.parametrize('damage',(None,'missing','unknown','reverse','width','direction'))
def test_complete_generated_xci_boundary_acceptance(tmp_path,policy,damage):
    import json
    from emuflow.lvds_selectio import validate_selectio_xci_ports
    from emuflow.lvds_bank_phy import selectio_core_ports
    recipe=selectio_bank_recipe('19','rx',rx_alignment=policy)
    ports={name:[{'direction':direction.lower(),**({'size_left':str(width-1),'size_right':'0'}if width>1 else{})}]
        for name,(direction,width)in selectio_core_ports(recipe).items()}
    word=recipe.data_pairs[0]['word_port']
    if damage=='missing':ports.pop('riu_clk')
    if damage=='unknown':ports['unbound_control']=[{'direction':'in'}]
    if damage=='reverse':ports[word][0].update(size_left='0',size_right='7')
    if damage=='width':ports[word][0]['size_left']='6'
    if damage=='direction':ports[word][0]['direction']='in'
    path=tmp_path/'native.xci';path.write_text(json.dumps({'ip_inst':{'boundary':{'ports':ports}}}))
    if damage is None:
        assert validate_selectio_xci_ports(recipe,path)['status']=='pass'
    else:
        with pytest.raises(ValidationError):validate_selectio_xci_ports(recipe,path)


@pytest.mark.parametrize('direction',('tx','rx'))
def test_all_supported_bank_recipes_keep_twenty_data_pairs_and_reserve_the_clock(direction):
    inventory=_inventory()
    for bank in inventory['banks']:
        if bank['id']=='65':continue
        recipe=selectio_bank_recipe(bank['id'],direction,inventory=inventory)
        pins={p['package_pin']:p for p in bank['pins']}
        settings=dict(recipe.settings)
        assert len(settings)==len(recipe.settings)
        assert len(recipe.data_pairs)==20
        assert len({p['native_pin']for p in recipe.data_pairs})==20
        used=set()
        for pair in recipe.data_pairs:
            assert '_GC_'not in pins[pair['p']['package_pin']]['pin_function']
            assert pins[pair['p']['package_pin']]['differential_pair_id']==pair['pair_id']
            assert pins[pair['n']['package_pin']]['differential_pair_id']==pair['pair_id']
            used.update(pair[polarity]['package_pin']for polarity in ('p','n'))
        assert len(used)==40
        clock=recipe.clock_pair
        assert '_GC_QBC_'in pins[clock['p']['package_pin']]['pin_function']
        assert all(clock[polarity]['package_pin']not in used for polarity in ('p','n'))
        assert settings['PLL0_INPUT_CLK_FREQ']==('200.000'if direction=='tx'else'800.000')
        assert settings['PLL0_DATA_SPEED']=='1600.00'
        assert settings['SERIALIZATION_FACTOR']=='8'
        if direction=='tx':assert clock['word_value']=="8'h55"
        else:assert clock['p']['port']=='clk_p'and clock['n']['port']=='clk_n'


def test_known_package_pair_maps_to_its_native_bitslice_positions():
    recipe=selectio_bank_recipe('19','tx')
    first=recipe.data_pairs[0]
    assert first=={'pair_id':'L1','native_pin':0,
        'p':{'port':'data_l1_p_0','package_pin':'BR49'},
        'n':{'port':'data_l1_n_1','package_pin':'BT49'},
        'word_port':'data_from_fabric_data_l1_p_0'}
    assert recipe.clock_pair['p']=={'port':'forward_p_26','package_pin':'BY44'}
    assert recipe.clock_pair['n']=={'port':'forward_n_27','package_pin':'CA44'}
    assert recipe.clock_pair['word_port']=='data_from_fabric_forward_p_26'


@pytest.mark.parametrize('bank,direction',[('65','tx'),('999','rx'),('19','both'),(19,'tx')])
def test_configuration_bank_unknown_bank_and_invalid_direction_are_rejected(bank,direction):
    with pytest.raises(ValidationError):selectio_bank_recipe(bank,direction)


def test_inconsistent_vendor_byte_identity_is_rejected():
    inventory=copy.deepcopy(_inventory())
    bank=next(b for b in inventory['banks']if b['id']=='19')
    pin=next(p for p in bank['pins']if p['package_pin']=='BT49')
    pin['pin_function']=pin['pin_function'].replace('T0L_N1','T1L_N1')
    with pytest.raises(ValidationError,match='inconsistent native byte'):
        selectio_bank_recipe('19','rx',inventory=inventory)


def observations(tmp_path,direction):
    from emuflow.lvds_bank_phy import selectio_core_ports
    recipe=selectio_bank_recipe('19',direction)
    properties={'CONFIG.'+k:v for k,v in recipe.settings}
    ports={f'{name}[{bit}]'if width>1 else name:d
        for name,(d,width)in selectio_core_ports(recipe).items()for bit in range(width)}
    physical,word=('OUT','IN')if direction=='tx'else('IN','OUT')
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        byte,pin=divmod(pair['native_pin'],13)
        for delta,polarity in ((0,'p'),(1,'n')):
            ports[pair[polarity]['port']]=physical
            properties[f'CONFIG.BYTE{byte}_PIN{pin+delta}_LOC']=pair[polarity]['package_pin']
        if 'word_port'in pair:
            ports.update((f"{pair['word_port']}[{bit}]",word)for bit in range(8))
        if direction=='rx'and pair is not recipe.clock_pair:
            ports.update((f"data_to_fabric_{pair['n']['port']}[{bit}]",'OUT')for bit in range(8))
    def write():
        (tmp_path/'tool-identity.tsv').write_text('field\tvalue\npart\t'+recipe.part+
            '\nip_definition\txilinx.com:ip:high_speed_selectio_wiz:3.6\nvivado_version\t2024.2\n')
        (tmp_path/'ip-properties.tsv').write_text('property_hex\tvalue_hex\n'+''.join(
            k.encode().hex()+'\t'+v.encode().hex()+'\n'for k,v in properties.items()))
        (tmp_path/'synthesis-ports.tsv').write_text('name_hex\tdirection\n'+''.join(
            k.encode().hex()+'\t'+v+'\n'for k,v in ports.items()))
    return recipe,properties,ports,write


@pytest.mark.parametrize('direction',('tx','rx'))
def test_census_validates_whole_words_without_counting_complements_or_clock_as_payload(tmp_path,direction):
    recipe,_properties,_ports,write=observations(tmp_path,direction);write()
    result=validate_selectio_observations(recipe,tmp_path)
    assert result['data_lanes']==20 and result['payload_word_bits']==160
    assert result['forwarded_clock_pairs']==1
    assert result['unused_complement_word_bits']==(160 if direction=='rx'else 0)
    assert result['physical_implementation_qualified']is False


@pytest.mark.parametrize('damage',('missing-bit','wrong-direction','extra-word','wrong-package','duplicate-port'))
def test_native_port_or_package_mismatch_cannot_pass_the_recipe_audit(tmp_path,damage):
    recipe,properties,ports,write=observations(tmp_path,'rx')
    word=recipe.data_pairs[0]['word_port']+'[7]'
    if damage=='missing-bit':ports.pop(word)
    elif damage=='wrong-direction':ports[word]='IN'
    elif damage=='extra-word':ports['data_to_fabric_unknown[0]']='OUT'
    elif damage=='wrong-package':properties['CONFIG.BYTE0_PIN1_LOC']='BR49'
    write()
    if damage=='duplicate-port':
        with(tmp_path/'synthesis-ports.tsv').open('a')as stream:stream.write(word.encode().hex()+'\tOUT\n')
    with pytest.raises(ValidationError):validate_selectio_observations(recipe,tmp_path)


def test_census_cannot_be_rebound_to_another_device(tmp_path):
    recipe,_properties,_ports,write=observations(tmp_path,'tx');write()
    path=tmp_path/'tool-identity.tsv'
    path.write_text(path.read_text().replace(recipe.part,'xcvu9p-flga2104-2-e'))
    with pytest.raises(ValidationError,match='tool/device identity differs'):
        validate_selectio_observations(recipe,tmp_path)
