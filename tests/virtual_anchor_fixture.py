"""Small board-anchor facts and expected XDC comments, never a flow producer."""
from collections import defaultdict


def anchor_facts(fpga, platform, endpoints):
    if len(endpoints) > 4096:
        raise ValueError('anchor fixture exceeds endpoint bound')
    links = {link.id: link for link in platform.links}
    groups = defaultdict(list)
    for row in endpoints:
        groups[row['link'], row['peer'], row['kind'], row['lane']].append(row)
    records = []
    for (link, peer, kind, lane), rows in sorted(groups.items()):
        serial = links[link].mode == 'serial'
        width = links[link].payload_bits_per_lane_per_cycle if serial else 1
        fields = (['package_pin_p', 'package_pin_n', 'connector', 'transceiver_site']
                  if serial else ['package_pin', 'bank', 'iostandard'])
        records.append({
            'id': f'{link}:{fpga}:{kind}:{lane}', 'link': link, 'peer': peer,
            'direction': kind, 'logical_lane': lane,
            'physical_lane': lane//width, 'bit_within_physical_lane': lane%width,
            'endpoint_ids': sorted(row['id'] for row in rows),
            'slots': sorted(row['slot'] for row in rows),
            'placement_class': 'virtual_link_io_region', 'binding_status': 'unbound',
            'required_hardware_binding_fields': fields,
        })
    return {'schema': 'emuflow.virtual-io-anchors/v1', 'platform': platform.name,
            'fpga': fpga, 'part': next(row.part for row in platform.fpgas if row.id == fpga),
            'anchors': records, 'required_hardware_binding_fields': sorted({
                field for row in records for field in row['required_hardware_binding_fields']})}


def expected_anchor_xdc(anchors):
    from emuflow_tools.transport.netlist import _sv_name
    records = anchors['anchors']
    if len(records) > 4096:
        raise ValueError('anchor fixture exceeds endpoint bound')
    text = ('# EmuFlow virtual IO anchors.\n'
            '# This file is intentionally non-binding until a hardware BSP is selected.\n'
            '# Replace placeholders through the hardware pin planner; do not source this\n'
            '# template directly in Vivado.\n\n')
    return text+'\n'.join(
        f"# anchor={row['id']} peer={row['peer']} lane={row['logical_lane']} direction={row['direction']}\n"
        f"# set_property PACKAGE_PIN <PACKAGE_PIN> [get_ports {{{_sv_name(row['id'])}}}]\n"
        f"# set_property IOSTANDARD <IOSTANDARD> [get_ports {{{_sv_name(row['id'])}}}]\n"
        for row in records)
