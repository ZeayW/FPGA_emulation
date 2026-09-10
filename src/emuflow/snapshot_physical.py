"""One physical qualification consumer for an emitted snapshot board.

The returned compact summary deliberately remains incomplete for whole-design
timing. All large files are read once here and remain ephemeral scratch.
"""
import json
from pathlib import Path
from .ecp5_qualification import qualify_snapshot_clock_coverage
from .ecp5_sdf import read_nextpnr_sdf
from .ecp5_timing_coverage import qualify_ecp5_timing_coverage, qualify_ecp5_comb_arcs
from .snapshot_cdc import qualify_snapshot_uart_cdc
from .snapshot_reset import qualify_snapshot_reset_structure
from .snapshot_physical_binding import bind_snapshot_routed_identities, bind_snapshot_transport_storage
from .errors import ValidationError


def qualify_snapshot_physical(output_dir, interface, physical_report, *, mapped_top, host_uart=False):
    source=interface.get("source_binding")
    if not isinstance(source,dict): raise ValidationError("snapshot physical qualification requires source binding")
    root=Path(output_dir)
    routed=json.loads((root/'routed.json').read_text())
    mapped=json.loads((root/'mapped.json').read_text())
    clocks=qualify_snapshot_clock_coverage(routed,physical_report)
    delays=read_nextpnr_sdf((root/'routed.sdf').read_text(),routed)
    coverage=qualify_ecp5_timing_coverage(routed,delays)
    comb=qualify_ecp5_comb_arcs(routed,delays)
    uart=qualify_snapshot_uart_cdc(routed,mapped,delays,mapped_top=mapped_top,host_uart=host_uart)
    reset=qualify_snapshot_reset_structure(routed)
    # Request original state only: optimized-away internal combinational aliases
    # are not made into a false original-path coverage obligation/certificate.
    state_source=dict(source,nets={})
    states=bind_snapshot_routed_identities(state_source,routed,hierarchy='core.dut',
        mapped=mapped,mapped_top=mapped_top)['registers']
    storage=bind_snapshot_transport_storage(interface,routed,mapped=mapped,mapped_top=mapped_top)
    return {'status':'physical_structure_qualified_global_timing_pending',
        'clock_coverage':clocks,'timing_annotation_coverage':coverage,
        'combinational_arc_coverage':comb,
        'uart_data_cdc':uart,'reset_structure':reset,
        'original_state':{'source_registers':len(states),
            'constant_registers':sum(v['kind']=='constant' for v in states.values()),
            'distinct_physical_ffs':len({v['cell'] for v in states.values() if v['kind']=='ff'})},
        'transport_storage':{role:len(storage[role]) for role in ('tx','rx')},
        'delay_annotation':{'physical_cells':len(delays['cells']),
            'interconnect_arcs':len(delays['interconnect']),
            'primitive_iopaths':sum(len(v['iopaths']) for v in delays['cells'].values())},
        'full_flow_qualified':False,'global_wns_tns':None,
        'pending':['original_path_delay_coverage','reset_recovery_removal','metastability_assumptions',
                   'asynchronous_global_timing']}
