"""Pin-identity and lane-accounting checks; actual IP observations are separate."""
import copy

import pytest

from emuflow.errors import ValidationError
from emuflow.lvds_selectio import selectio_bank_recipe
from emuflow.virtual_board import _inventory


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
