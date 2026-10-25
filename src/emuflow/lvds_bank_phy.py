"""Native 20-lane SelectIO word interfaces and explicit training controls.

The native IP owns reset/calibration and FIFO read coordination. Bit alignment
uses either the native default or an explicitly selected registered candidate.
The caller supplies global readiness consensus and synchronous application
controls. This wrapper is not itself a distributed barrier or a latency proof.
"""
from __future__ import annotations

import re

from .errors import ValidationError
from .lvds_selectio import SelectIOBankRecipe,RX_ALIGNMENT_OUTPUT_STAGES


def selectio_core_ports(recipe: SelectIOBankRecipe):
    """Declare the qualified v3.6 full-bank customization's bounded interface."""
    ports={name:('IN',1)for name in ('riu_clk','rst')}
    ports.update({name:('OUT',1)for name in ('pll0_locked','pll0_clkout0','shared_pll0_clkoutphy_out','rst_seq_done')})
    for bsc in range(8):
        ports[f'en_vtc_bsc{bsc}']=('IN',1)
        for name in ('vtc_rdy','dly_rdy'):ports[f'{name}_bsc{bsc}']=('OUT',1)
    physical,word=('OUT','IN')if recipe.direction=='tx'else('IN','OUT')
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        for polarity in ('p','n'):ports[pair[polarity]['port']]=(physical,1)
        if 'word_port'in pair:ports[pair['word_port']]=(word,8)
    if recipe.direction=='tx':ports['clk']=('IN',1)
    else:
        for name in ('app_clk','multi_intf_lock_in'):ports[name]=('IN',1)
        for name in ('intf_rdy','fifo_rd_data_valid'):ports[name]=('OUT',1)
        if recipe.rx_alignment=='native-bit-slip-v1':
            ports['start_bitslip']=('IN',1)
            ports['rx_bitslip_sync_done']=('OUT',1)
        for pair in recipe.data_pairs:
            ports['data_to_fabric_'+pair['n']['port']]=('OUT',8)
            for delta in (0,1):
                pin=pair['native_pin']+delta
                ports[f'fifo_rd_clk_{pin}']=('IN',1)
                ports[f'fifo_empty_{pin}']=('OUT',1)
                if recipe.rx_alignment=='native-bit-slip-v1':ports[f'bitslip_error_{pin}']=('OUT',1)
    return ports


def selectio_bank_to_systemverilog(recipe: SelectIOBankRecipe, *, ip_module: str,
                                   module: str | None = None, include_common: bool = True):
    """Wire every native input, keeping training words separate from payload."""
    module=module or f'emuflow_lvds_bank{recipe.bank}_{recipe.direction}'
    if any(not isinstance(name,str)or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name)
           for name in (ip_module,module)):
        raise ValidationError('SelectIO wrapper module identity is invalid')
    ports=selectio_core_ports(recipe)
    declarations=['input wire tdm_clk','input wire riu_clk','input wire reset',
                  'output wire pll_locked','output wire reset_done','output wire bank_ready']
    if recipe.direction=='tx':
        declarations+=['input wire payload_enable','input wire [159:0] tx_words']
    else:
        declarations+=['input wire all_plls_locked','input wire alignment_enable',
            'output wire [159:0] rx_words','output wire word_valid','output wire training_error']
    physical='output'if recipe.direction=='tx'else'input'
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        declarations.extend(f"{physical} wire {pair[p]['port']}"for p in ('p','n'))
    lines=['// Generated native SelectIO bank interface; global consensus is external.',
        '// alignment_enable remains asserted after alignment; it is not a pulse.',
        f'module {module} (',',\n'.join('  '+d for d in declarations),');',
        '  wire [7:0] vtc_ready, delay_ready;',
        '  wire native_reset_done, native_pll_locked;',
        '  assign pll_locked = native_pll_locked;',
        '  assign reset_done = native_reset_done;']
    connections={'riu_clk':'riu_clk','rst':'reset','pll0_locked':'native_pll_locked',
        'rst_seq_done':'native_reset_done','pll0_clkout0':'','shared_pll0_clkoutphy_out':''}
    for bsc in range(8):
        connections.update({f'en_vtc_bsc{bsc}':"1'b1",f'vtc_rdy_bsc{bsc}':f'vtc_ready[{bsc}]',
                            f'dly_rdy_bsc{bsc}':f'delay_ready[{bsc}]'})
    for pair in (*recipe.data_pairs,recipe.clock_pair):
        for polarity in ('p','n'):connections[pair[polarity]['port']]=pair[polarity]['port']
    if recipe.direction=='tx':
        connections['clk']='tdm_clk'
        for lane,pair in enumerate(recipe.data_pairs):
            connections[pair['word_port']]=f"(payload_enable ? tx_words[{8*lane} +: 8] : 8'h2c)"
        connections[recipe.clock_pair['word_port']]=recipe.clock_pair['word_value']
        lines.append('  assign bank_ready = !reset && native_pll_locked && native_reset_done && (&vtc_ready) && (&delay_ready);')
    else:
        native_alignment=recipe.rx_alignment=='native-bit-slip-v1'
        connections.update(app_clk='tdm_clk',multi_intf_lock_in='all_plls_locked',
            intf_rdy='native_interface_ready',fifo_rd_data_valid='native_word_valid')
        lines+=['  wire native_interface_ready, native_word_valid, native_bitslip_done;',
                '  assign bank_ready = !reset && native_pll_locked && native_reset_done && (&vtc_ready) && (&delay_ready)',
                '                   && native_interface_ready && native_bitslip_done && !training_error;']
        if native_alignment:
            connections.update(start_bitslip='alignment_enable',rx_bitslip_sync_done='native_bitslip_done')
            lines+=['  wire [39:0] native_bitslip_errors;',
                    '  assign training_error = |native_bitslip_errors;']
        else:
            lines+=['  wire [159:0] raw_words;',
                    '  wire [19:0] aligned_lanes, aligned_valid, alignment_errors;',
                    '  assign native_bitslip_done = &aligned_lanes;',
                    '  assign training_error = |alignment_errors;']
        lines.append('  assign word_valid = bank_ready && '+('native_word_valid;'if native_alignment else'(&aligned_valid);'))
        for lane,pair in enumerate(recipe.data_pairs):
            connections[pair['word_port']]=f"{'rx_words'if native_alignment else'raw_words'}[{8*lane} +: 8]"
            connections['data_to_fabric_'+pair['n']['port']]=''
            for delta in (0,1):
                pin=pair['native_pin']+delta
                connections[f'fifo_rd_clk_{pin}']='tdm_clk'
                connections[f'fifo_empty_{pin}']=''
                if native_alignment:connections[f'bitslip_error_{pin}']=f'native_bitslip_errors[{2*lane+delta}]'
            if not native_alignment:
                lines += [f'  emuflow_lvds_word_align #(.OUTPUT_STAGES({RX_ALIGNMENT_OUTPUT_STAGES[recipe.rx_alignment]})) lane_{lane} (',
                    '    .clk(tdm_clk), .reset(reset), .enable(alignment_enable && native_interface_ready),',
                    f'    .raw_valid(native_word_valid), .raw_word(raw_words[{8*lane} +: 8]),',
                    f'    .aligned_word(rx_words[{8*lane} +: 8]), .aligned(aligned_lanes[{lane}]),',
                    f'    .word_valid(aligned_valid[{lane}]), .training_error(alignment_errors[{lane}]));']
    if connections.keys()!=ports.keys() or any(not connections[name]for name,(d,_)in ports.items()if d=='IN'):
        raise ValidationError('native SelectIO input/port coverage is incomplete')
    lines += [f'  {ip_module} native_phy (',
        ',\n'.join(f'    .{name}({connections[name]})'for name in sorted(ports)),'  );','endmodule','']
    if recipe.direction=='rx'and recipe.rx_alignment in RX_ALIGNMENT_OUTPUT_STAGES and include_common:
        from .lvds_word_aligner import word_aligner_to_systemverilog
        lines.append(word_aligner_to_systemverilog())
    return '\n'.join(lines)
