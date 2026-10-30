"""Bounded test inputs and views around the current complete native split stage."""
from tests.phase3_store_fixture import write_current_assignment_fixture
import copy
import json
from pathlib import Path
import subprocess
import tempfile

from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow_tools.stage_clients.transport import run_phase6, validate_phase6
from emuflow.transport.split_netlist_storage import SplitNetlistView, validate_split_netlists_sqlite
from emuflow_tools.transport.transport_storage import read_lane_map, read_transport
from tests.native_build import emuflow_native
from tests.schedule_store_fixture import write_tdm_schedule_storage


class NativeSplitFixture:
    def __init__(self, ir, assignment, schedule, platform):
        if len(ir.value['instances']) > 4096 or len(schedule['entries']) > 16384:
            raise ValidationError('native split fixture exceeds bounded population')
        self.temporary = tempfile.TemporaryDirectory(prefix='native-split-fixture-')
        self.root = Path(self.temporary.name)
        self.source = self.root/'source.emuir.sqlite'
        self.assignment = self.root/'assignment.sqlite'
        self.schedule = self.root/'schedule.json'
        self.board = self.root/'board.json'
        self.output = self.root/'phase6'
        self.platform = platform
        write_compact_emuir(self.source, ir.value)
        value = copy.deepcopy(assignment)
        declared = {row['fpga'] for row in value['partitions']}
        value['partitions'].extend({'fpga': fpga.id, 'resources': {}} for fpga in platform.fpgas if fpga.id not in declared)
        write_current_assignment_fixture(self.root/'assignment.json', ir, value)
        write_tdm_schedule_storage(self.schedule, schedule)
        self.board.write_text(json.dumps(platform.to_dict()))
        self.report = run_phase6(self.source, self.assignment, self.schedule,
            self.board, self.output, clock_periods={clock['id']: 10.0 for clock in ir.value['clocks']})
        self.manifest_path = self.output/'manifest.json'
        self.manifest = read_json(self.manifest_path)
        self.split = self.output/'split-netlists.sqlite'
        self.transport = self.output/'transport.sqlite'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.temporary.cleanup()

    def validate(self):
        return validate_phase6(self.source, self.assignment, self.schedule,
                               self.board, self.manifest_path)

    def validate_split(self):
        return validate_split_netlists_sqlite(self.split, self.source, self.assignment,
            expected_design=self.manifest['design'], expected_platform=self.platform.name)

    def audit_transport(self):
        with tempfile.TemporaryDirectory(prefix='audit-', dir=self.root) as temporary:
            output = Path(temporary)/'transport-audit.json'
            result = subprocess.run([str(emuflow_native()), 'phase6', 'audit-transport',
                                     str(self.transport), str(output)], capture_output=True, text=True)
            if result.returncode:
                assert not output.exists(), 'failed transport audit published success'
                raise ValidationError(result.stdout + result.stderr)
            return read_json(output)

    def artifacts(self):
        netlists = {}
        for fpga in self.platform.fpgas:
            with SplitNetlistView(self.split, fpga.id) as view:
                netlists[fpga.id] = {'instances': list(view.instances()), 'nets': list(view.nets())}
        return {'manifest': self.manifest, 'netlists': netlists,
                'lane_map': read_lane_map(self.output/'lane_map.json', materialize=True),
                'transports': {row['fpga']: read_transport(self.output/row['transport'], materialize=True)
                               for row in self.manifest['fpgas']}}


def materialize_partition_fixture(path, fpga):
    """Decode a small current reader for lowering expectations, never a flow API."""
    with SplitNetlistView(path, fpga) as view:
        if any(count > 4096 for count in view.counts().values()):
            raise ValidationError('partition fixture exceeds population bound')
        return {'schema': 'emuflow.fpga-netlist/v1', 'design': view.metadata['design'],
                'platform': view.metadata['platform'], 'fpga': fpga,
                'ports': view.ports, 'instances': [
                    {key: value for key, value in row.items()
                     if key != 'name' or value is not None}
                    for row in view.instances()],
                'nets': list(view.nets()), 'resources': view.resources,
                **({'semantic_contract_sha256': view.metadata['semantic_contract_sha256']}
                   if view.metadata.get('semantic_contract_sha256') is not None else {})}
