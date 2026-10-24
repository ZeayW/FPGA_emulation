"""Complete native Phase 5 producer: no Python route/result reconstruction."""
import json
import math
import os
from dataclasses import replace
import random
import struct
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.tdm import build_native_exact_schedule_request, build_tdm_schedule
from emuflow.tdm_schedule_storage import read_tdm_schedule, write_tdm_schedule_storage
from emuflow.system_route_storage import read_system_routes, write_system_route_storage
from tests.native_build import emuflow_native, native_json_record
from tests.native_schedule_case import _case


def test_native_json_numbers_match_python_canonical_encoding():
    generator = random.Random(5401)
    values = [0.0, -0.0, 1.0, 1e-4, 1e-5, 1e15, 1e16, 1e23]
    for exponent in range(-1074, 1024):
        value = math.ldexp(1.0, exponent)
        values.extend([value, math.nextafter(value, 0.0)])
        next_value = math.nextafter(value, math.inf)
        if math.isfinite(next_value):
            values.append(next_value)
    values.extend(n / d for d in range(1, 100) for n in (0, 1, d - 1, d))
    while len(values) < 20000:
        value = struct.unpack("<d", struct.pack("<Q", generator.getrandbits(64)))[0]
        if math.isfinite(value):
            values.append(value)
    encoded = "\n".join(f"{struct.unpack('<Q', struct.pack('<d', value))[0]:x}" for value in values)
    result = subprocess.run([str(native_json_record()), "emit"], input=encoded, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(values)
    for value, native in zip(values, lines):
        assert native == json.dumps(value, allow_nan=False), repr(value)


def install_runtime(tmp_path, monkeypatch):
    bindir = tmp_path / "install/bin"
    bindir.mkdir(parents=True)
    (bindir / "emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(bindir.parent))


@pytest.mark.parametrize("affine,repeated", [(False, False), (True, False), (True, True), ("banks", False), ("tree", False)])
def test_complete_native_producer_matches_reference_records_certificates_and_hashes(tmp_path, monkeypatch, affine, repeated):
    case, routes = _case(tmp_path, affine, repeated)
    reference = build_tdm_schedule(routes, case.platform)
    expected_manifest = write_tdm_schedule_storage(tmp_path / "reference/schedule.json", reference)
    install_runtime(tmp_path, monkeypatch)
    calls = []
    run = subprocess.run

    def record(command, **kwargs):
        calls.append(command)
        assert Path(command[-3]).stat().st_size < 20000
        assert Path(command[-2]).stat().st_size < 20000
        return run(command, **kwargs)

    with patch.object(type(routes["routes"]), "iter_native_exact_metadata", side_effect=AssertionError("Python R metadata")), \
         patch.object(type(routes["routes"]), "iter_native_exact_records", side_effect=AssertionError("Python H metadata")), \
         patch("emuflow.tdm._indexed_certificate", side_effect=AssertionError("Python readiness certificate")), \
         patch("emuflow.tdm._baseline_ratio_hops", side_effect=AssertionError("Python wire map")), \
         patch("emuflow.tdm_schedule_storage._publish_logical_schedule", side_effect=AssertionError("Python record publisher")), \
         patch("emuflow.tdm.subprocess.run", side_effect=record):
        request = build_native_exact_schedule_request(routes, case.platform)
        assert request is not None
        native_manifest = write_tdm_schedule_storage(tmp_path / "native/schedule.json", request)
    assert [command[1:3] for command in calls] == [["phase5", "build-schedule"]]
    actual = read_tdm_schedule(tmp_path / "native/schedule.json", materialize=True)
    assert actual == reference
    for key in ("logical_sha256", "logical_pretty_sha256", "sections", "route_relation", "completion_relation", "entry_relation"):
        assert native_manifest[key] == expected_manifest[key], key
    assert not list((tmp_path / "native").glob("*.input"))
    assert not list((tmp_path / "native").glob("*request*"))


@pytest.mark.parametrize("corruption", ["generation", "header", "domain", "board", "protocol"])
def test_native_generation_rejects_corruption_without_replacing_published_schedule(tmp_path, monkeypatch, corruption):
    case, routes = _case(tmp_path, False, False)
    reference = build_tdm_schedule(routes, case.platform)
    manifest = tmp_path / "schedule.json"
    write_tdm_schedule_storage(manifest, reference)
    previous = manifest.read_bytes()
    install_runtime(tmp_path, monkeypatch)
    request = build_native_exact_schedule_request(routes, case.platform)
    assert request is not None
    if corruption == "generation":
        status = request.route_store.stat()
        os.utime(request.route_store, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000))
    elif corruption == "header":
        request = replace(request, header={**request.header, "metrics": {}})
    elif corruption == "domain":
        request = replace(request, domain_templates=({**request.domain_templates[0], "key": "wrong"}, *request.domain_templates[1:]))
    else:
        original = type(request).write_board_request

        def broken(self, path):
            original(self, path)
            payload = path.read_bytes()
            path.write_bytes(payload[:-1] if corruption == "board" else b"unknown!" + payload[8:])

        monkeypatch.setattr(type(request), "write_board_request", broken)
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule", side_effect=AssertionError("silent reference retry")):
        with pytest.raises(ValidationError):
            write_tdm_schedule_storage(manifest, request)
    assert manifest.read_bytes() == previous
    assert read_tdm_schedule(manifest, materialize=True) == reference
    assert not list(tmp_path.glob("*.publishing.*"))
    assert not list(tmp_path.glob(".*.tmp*"))


def test_indexed_production_rejects_standalone_without_reference_fallback(tmp_path):
    case, routes = _case(tmp_path, False, False)
    with patch("emuflow.native_tools.resolve_native_stage_command", return_value=["standalone"]):
        with pytest.raises(ValidationError, match="requires the unified emuflow-native"):
            build_native_exact_schedule_request(routes, case.platform)


def test_empty_indexed_production_uses_native_and_matches_reference(tmp_path, monkeypatch):
    case, _routes = _case(tmp_path, False, False)
    empty = read_json(tmp_path / "routes.json")
    empty["routes"] = []
    empty["demands"] = []
    path = tmp_path / "empty.json"
    write_system_route_storage(path, empty)
    routes = read_system_routes(path)
    reference = build_tdm_schedule(routes, case.platform)
    install_runtime(tmp_path, monkeypatch)
    request = build_native_exact_schedule_request(routes, case.platform)
    assert request is not None
    output = tmp_path / "schedule.json"
    with patch("emuflow.tdm_schedule_storage._publish_logical_schedule",
               side_effect=AssertionError("empty production retried Python")):
        write_tdm_schedule_storage(output, request)
    assert read_tdm_schedule(output, materialize=True) == reference
