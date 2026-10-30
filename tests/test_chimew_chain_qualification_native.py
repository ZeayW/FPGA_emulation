"""Native complete certificates against an independent test-only reference."""
import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.native_build import emuflow_native
from tests import test_chimew_qualification as fixtures
from tests.chimew_chain_reference import build_chimew_phase6_qualification as reference
from tests.chimew_qualification_fixture import typed_pair, chain_paths
from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
from emuflow.common.errors import ValidationError
from emuflow.transport.chimew.chimew_qualification import qualify_chimew_artifact_chain

NAMES = ("schedule", "crossings", "initial_grouping", "positions", "refined_grouping",
         "rudy_input", "rudy_report", "bank_channel_input", "bank_channel_report")


@pytest.fixture
def chain():
    fixtures.ChimewQualificationTest.setUpClass()
    case = fixtures.ChimewQualificationTest()
    case.setUp()
    yield case
    fixtures.ChimewQualificationTest.tearDownClass()


def documents(chain):
    return dict(zip(NAMES, (copy.deepcopy(value) for value in (
        chain.schedule, chain.crossings, chain.initial, chain.positions, chain.refined,
        chain.rudy_input, chain.rudy_report, chain.bank_input, chain.bank_report))))


def invocation(tmp_path, values, left, right, binding):
    paths = {"bank_channel_input": left.manifest_path, "bank_channel_report": right.manifest_path}
    for key, value in values.items():
        if key.startswith("bank_channel_"):
            continue
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[key] = path
    request = {"schema": "emuflow.chimew-qualification-input/v1", "artifacts": {
        key: {"path": str(Path(path).resolve()), "schema": (
            "chimew-bank-input-selector-v1" if key == "bank_channel_input" else
            "chimew-bank-report-selector-v1" if key == "bank_channel_report" else
            "physical-input-document-v1")} for key, path in paths.items()}}
    if binding is not None:
        request["source_binding"] = binding
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request), encoding="utf-8")
    return source, paths


@pytest.mark.parametrize("mutation", [
    "none", "rudy-roundoff", "position-roundoff", "bound", "bound-timing", "null-guard",
    "position", "crossing", "group", "rudy-load", "rudy-provenance", "bank-fanout",
    "bank-members", "source-binding",
])
def test_native_complete_chain_matches_independent_reference(chain, tmp_path, mutation):
    values = documents(chain)
    binding = None
    if mutation == "position":
        values["positions"]["entries"][0]["source_y"] += 1
    elif mutation == "position-roundoff":
        values["positions"]["entries"][0]["source_y"] += 1e-10
    elif mutation == "crossing":
        values["crossings"]["entries"][0]["encoding"] += 1
    elif mutation == "group":
        values["refined_grouping"]["entries"][0]["group"] = 100
    elif mutation == "rudy-load":
        values["rudy_report"]["bins"][0]["load"] += 1
    elif mutation == "rudy-roundoff":
        values["rudy_report"]["metrics"]["peak_utilization"] += 1e-10
    elif mutation == "rudy-provenance":
        values["rudy_report"]["input_sha256"] = "f" * 64
    elif mutation in ("bank-fanout", "bank-members"):
        member = values["bank_channel_input"]["groups"][0]["members"][0]
        if mutation == "bank-fanout":
            member["fanout"]["y"] += 1
        else:
            member["id"] = "unknown"
        # A valid new bank/report pair must still fail the independent join to
        # placement and refined membership, beyond an input-digest mismatch.
        values["bank_channel_report"] = evaluate_chimew_bank_channel_assignment(
            values["bank_channel_input"])
    elif mutation == "null-guard":
        values["crossings"]["timing_guard"] = None
    elif mutation in ("bound", "bound-timing", "source-binding"):
        provenance = values["rudy_input"]["provenance"]
        digests = {key: provenance[key + "_sha256"] for key in ("placement", "netlist", "architecture")}
        digests.update(routing=values["crossings"]["provenance"]["routing_sha256"], package_pins="e" * 64)
        if mutation == "bound-timing":
            digests["timing_paths"] = "1" * 64
        elif mutation == "source-binding":
            digests["routing"] = "f" * 64
        binding = {"scope": "byte-bound-source-artifacts", "digests": digests}
    arguments = [values[key] for key in NAMES]
    valid = mutation in ("none", "rudy-roundoff", "position-roundoff", "bound", "bound-timing", "null-guard")
    if valid:
        expected = reference(*arguments, source_binding=binding)
    else:
        with pytest.raises(ValidationError):
            reference(*arguments, source_binding=binding)
    with typed_pair(values["bank_channel_input"], values["bank_channel_report"]) as (left, right):
        source, _ = invocation(tmp_path, values, left, right, binding)
        out, receipt = tmp_path / "qualification.json", tmp_path / "operation.json"
        previous = b"previous published certificate\n"
        out.write_bytes(previous)
        result = subprocess.run([str(emuflow_native()), "--report", str(receipt), "phase6",
            "qualify-chimew-chain", str(source), str(out)], capture_output=True, text=True)
        if valid:
            assert result.returncode == 0, receipt.read_text() if receipt.exists() else result.stderr
            assert json.loads(out.read_text()) == expected
            assert out.read_text() == json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        else:
            assert result.returncode != 0
            assert out.read_bytes() == previous


def test_qualification_host_never_loads_graph_records_in_python(chain, tmp_path):
    from emuflow.transport.chimew import chimew_bank_channel_storage as inputs
    from emuflow.transport.chimew import chimew_bank_channel_report_storage as reports
    values = documents(chain)
    with chain_paths([values[key] for key in NAMES]) as (paths, output):
        with patch("emuflow.common.io.read_json", side_effect=AssertionError("Python graph read")), \
                patch.object(inputs._RawGroupSequence, "__iter__", side_effect=AssertionError("Python group scan")), \
                patch.object(reports._AssignmentSequence, "__iter__", side_effect=AssertionError("Python assignment scan")):
            result = qualify_chimew_artifact_chain(paths, output)
        assert result["status"] == "pass"


@pytest.mark.parametrize("owner", ["request", "output"])
def test_receipt_aliases_are_rejected_without_overwriting_owners(tmp_path, owner):
    source, out = tmp_path / "request.json", tmp_path / "certificate.json"
    source.write_text("{}")
    out.write_text("original")
    before = source.read_bytes(), out.read_bytes()
    result = subprocess.run([str(emuflow_native()), "--report", str(source if owner == "request" else out),
        "phase6", "qualify-chimew-chain", str(source), str(out)], capture_output=True, text=True)
    assert result.returncode != 0
    assert (source.read_bytes(), out.read_bytes()) == before


@pytest.mark.parametrize("document, field", [("crossings", "encoding"), ("rudy_input", "x")])
def test_duplicate_record_keys_cannot_be_hidden_by_canonicalization(chain, tmp_path, document, field):
    import re
    values = documents(chain)
    with typed_pair(values["bank_channel_input"], values["bank_channel_report"]) as (left, right):
        source, paths = invocation(tmp_path, values, left, right, None)
        original = paths[document].read_text()
        pattern = r'("' + field + r'"\s*:\s*-?[0-9.eE+\-]+)'
        modified, count = re.subn(pattern, r'\1,\1', original, count=1)
        assert count == 1
        paths[document].write_text(modified)
        output = tmp_path / "certificate.json"
        output.write_text("previous certificate")
        result = subprocess.run([str(emuflow_native()), "phase6", "qualify-chimew-chain", str(source), str(output)],
                                capture_output=True, text=True)
        assert result.returncode != 0
        assert "duplicate" in result.stderr
        assert output.read_text() == "previous certificate"
