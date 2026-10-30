"""Native input preparation, source admission and tool protocol equivalence."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest

from emuflow.common.native_tools import native_file_generation
from emuflow.frontend.frontend_native import prepare_frontend
from emuflow.frontend.inputs import prepare_inputs
from tests.synthesis_script_reference import build_yosys_script
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from emuflow.platform.platform import Platform
from tests.native_build import emuflow_native
from tests.test_prepared_inputs import boundaries

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / 'platforms/virtual/xcvu3p_2fpga_p2p.json'
PROFILE = 'xilinx-ultrascaleplus-open-v1'


def configuration(tmp_path, *, mode='rtl'):
    rtl = tmp_path/'source.v'
    rtl.write_text('module top; endmodule\n')
    return {
        'schema': 'emuflow.frontend-configuration/v1', 'mode': mode,
        'output_dir': str(tmp_path/'out'), 'working_directory': str(tmp_path),
        'top': 'top', 'clocks': ['clk', 'clk'], 'clock_periods': {'clk': 10.0},
        'mapping_profile': PROFILE, 'mapping_strategy': None,
        'platform': Platform.load(BOARD).to_dict(), 'board': str(BOARD),
        'board_generation': list(native_file_generation(BOARD)),
        'library': str(DEFAULT_XILINX_PRIMITIVE_LIBRARY.resolve()),
        'engine': sys.executable, 'rtl_sources': [str(rtl)],
        'include_dirs': [], 'defines': [], 'source_checksums': None,
        'prepared_frontend': None, 'prepared_timing': None, 'yosys_json': None,
    }


def invoke(value, root, *, duplicate=False):
    source = root/'configuration.json'
    text = json.dumps(value, ensure_ascii=False)
    source.write_text(text[:-1]+',"top":"another"}' if duplicate else text)
    target = root/'request'
    result = subprocess.run([str(emuflow_native()), '--report', str(root/'operation.json'),
        'phase1', 'prepare-configuration', str(source), str(target)],
        capture_output=True, text=True, timeout=15)
    return result, target


def read_rtl_request(path):
    raw = path.read_bytes()
    assert zlib.crc32(raw[:-4]) == struct.unpack('<I', raw[-4:])[0]
    offset = 0
    def number(layout):
        nonlocal offset
        size = struct.calcsize('<'+layout)
        value = struct.unpack_from('<'+layout, raw, offset)
        offset += size
        return value[0] if len(value) == 1 else value
    def text():
        nonlocal offset
        size = number('I')
        value = raw[offset:offset+size].decode()
        offset += size
        return value
    def catalog():
        return [text() for _ in range(number('I'))]
    assert number('QI') == (0x31544E52464D5545, 1)
    output, top, clocks = text(), text(), catalog()
    platform, synthesis, strict = json.loads(text()), json.loads(text()), number('I')
    command, cwd, timeout = catalog(), text(), number('Q')
    library = (text(), number('3Q4q'))
    sources = [(text(), number('3Q4q')) for _ in range(number('I'))]
    assert offset == len(raw)-4
    return SimpleNamespace(output=output, top=top, clocks=clocks, platform=platform,
        synthesis=synthesis, strict=strict, command=command, cwd=cwd, timeout=timeout,
        library=library, sources=sources)


@pytest.mark.parametrize('strategy', [None, 'hierarchical-abc9-v1',
    'hierarchical-classic-v1', 'flatten-classic-v1', 'flatten-abc9-v1'])
def test_rtl_script_matches_maintained_mapping_contract_and_source_receipts(tmp_path, strategy):
    value = configuration(tmp_path)
    value.update(mapping_strategy=strategy, defines=['MODEL=1', 'ENABLE'], include_dirs=[str(tmp_path)])
    # Paths are data in the Yosys script, including quotes and non-ASCII names.
    source = tmp_path/'source "é.v'
    source.write_text('module top; endmodule')
    value['rtl_sources'] = [str(source)]
    header = tmp_path/'config.vh'
    header.write_text('`define WIDTH 4')
    value['source_checksums'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, header)}
    result, target = invoke(value, tmp_path)
    assert result.returncode == 0, result.stderr
    actual = read_rtl_request(target/'native-frontend-request.bin')
    expected = build_yosys_script([source], 'top', Path('<stdout>'), 'xcup', 'native',
        include_dirs=[tmp_path], defines=['MODEL=1', 'ENABLE'], mapping_profile=PROFILE,
        mapping_strategy=strategy or 'hierarchical-abc9-v1', stream_json=True, hierarchical_interchange=True)
    # Compare decoded script tokens: native JSON quoting may retain UTF-8.
    assert actual.command[3].replace('é', r'\u00e9') == expected
    assert actual.clocks == ['clk'] and actual.strict == 0 and actual.timeout == 0
    assert actual.output == str(tmp_path/'out/frontend/phase1')
    assert actual.synthesis['tool_report']['mapping_strategy'] == (strategy or 'hierarchical-abc9-v1')
    assert set(actual.synthesis['sources']) == {str(source)}
    assert {path for path, _ in actual.sources} == {str(source), str(header), str(BOARD.resolve()), str(Path(sys.executable).resolve())}
    for path, generation in [actual.library, *actual.sources]:
        assert native_file_generation(path) == generation
    assert not (tmp_path/'out').exists()  # Preparation never runs an engine.


@pytest.mark.parametrize('damage', ['mode', 'schema', 'duplicate', 'unknown', 'top', 'strategy',
    'source', 'include', 'define', 'checksum', 'checksum-coverage', 'checksum-empty',
    'mixed-json', 'relative', 'clock', 'profile', 'board-generation', 'host-capacity', 'host-topology', 'existing'])
def test_invalid_configuration_cannot_publish_a_request(tmp_path, damage):
    value = configuration(tmp_path)
    if damage == 'mode': value['mode'] = 'legacy'
    if damage == 'schema': value['schema'] = 'unsupported'
    if damage == 'unknown': value['compatibility'] = True
    if damage == 'top': value['top'] = 'top; help'
    if damage == 'strategy': value['mapping_strategy'] = 'unsupported'
    if damage == 'source': value['rtl_sources'] = [str(tmp_path/'missing.v')]
    if damage == 'include': value['include_dirs'] = [str(tmp_path/'space dir')]; (tmp_path/'space dir').mkdir()
    if damage == 'define': value['defines'] = ['MODEL=1; help']
    if damage == 'checksum': value['source_checksums'] = {value['rtl_sources'][0]: '0'*64}
    if damage == 'checksum-coverage': value['source_checksums'] = {str(BOARD): hashlib.sha256(BOARD.read_bytes()).hexdigest()}
    if damage == 'checksum-empty': value['source_checksums'] = {}
    if damage == 'mixed-json': value['yosys_json'] = str(ROOT/'examples/yosys/counter.json')
    if damage == 'relative': value['output_dir'] = 'relative'
    if damage == 'clock': value['clocks'] = [True]
    if damage == 'profile': value['mapping_profile'] = 'legacy'
    if damage == 'board-generation': value['board_generation'][2] += 1
    if damage == 'host-capacity': value['platform']['fpgas'][0]['effective_capacity']['lut'] += 1
    if damage == 'host-topology': value['platform']['links'][0]['data_lanes_per_direction'] += 1
    if damage == 'existing':
        (tmp_path/'request').mkdir(); (tmp_path/'request/native-frontend-request.bin').write_bytes(b'keep')
    result, target = invoke(value, tmp_path, duplicate=damage=='duplicate')
    assert result.returncode != 0
    if damage == 'existing': assert (target/'native-frontend-request.bin').read_bytes() == b'keep'
    else: assert not target.exists()
    assert not (tmp_path/'out').exists()


def test_engine_invocation_path_is_preserved_through_a_symlink(tmp_path):
    value = configuration(tmp_path)
    invocation = tmp_path/'tool-alias'
    invocation.symlink_to(sys.executable)
    value['engine'] = str(invocation)
    result, target = invoke(value, tmp_path)
    assert result.returncode == 0, result.stderr
    actual = read_rtl_request(target/'native-frontend-request.bin')
    assert actual.command[0] == str(invocation)
    assert str(Path(sys.executable).resolve()) in {p for p, _ in actual.sources}


def test_prepared_configuration_binds_real_selector_payloads_without_python_selection(tmp_path, boundaries):
    front, timing = boundaries
    ctx = SimpleNamespace(prepared_frontend=front, prepared_timing=timing,
        sources=(), yosys_json=None, platform_path=BOARD, output_dir=tmp_path/'out',
        top=None, clocks=('clk',), clock_periods={'clk': 10.0}, mapping_profile=PROFILE)
    request = prepare_inputs(ctx)
    value = json.loads(request.read_text())
    assert value['mode'] == 'prepared'
    records = {row['role']: row for row in value['sources']}
    assert set(records) == {'emuir','phase1-report','phase1-board','path-selector','path-store',
        'weight-selector','weight-store','timing-report','board'}
    for row in records.values(): assert row['generation'] == list(native_file_generation(row['path']))
    before = {role: row['generation'] for role, row in records.items()}
    from emuflow.frontend import inputs
    assert not hasattr(inputs, '_prepared_sources')
    assert before == {role:list(native_file_generation(row['path'])) for role,row in records.items()}


def test_replaced_python_frontend_producers_are_absent():
    import inspect
    from emuflow.frontend import frontend_native, inputs
    text = inspect.getsource(frontend_native)
    assert 'CheckedRequestWriter' not in text and 'build_yosys_script' not in text
    assert '_prepared_sources' not in inspect.getsource(inputs)


def test_optional_rtl_host_rechecks_only_original_inputs_after_request_scratch_closes(tmp_path, monkeypatch):
    from emuflow.frontend import frontend_native
    from tests.test_native_frontend import source
    engine = tmp_path/'yosys'
    engine.write_text('#!'+sys.executable+'\nimport sys\nsys.stdout.write('+repr(json.dumps(source()))+')\n')
    engine.chmod(0o700)
    rtl = tmp_path/'rtl.v'; rtl.write_text('module top; endmodule')
    monkeypatch.setattr(frontend_native, 'resolve_native_executable',
        lambda name, *args: str(engine) if name=='yosys' else str(emuflow_native()))
    result = frontend_native.run_rtl_frontend([rtl], 'top', BOARD, tmp_path/'out', clocks=('clk',))
    assert result['status'] == 'pass'
    assert (tmp_path/'out/frontend/phase1/design.emuir.sqlite').is_file()
