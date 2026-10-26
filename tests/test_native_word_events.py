"""Native complete-fanin event kernel qualification; no production path claim."""
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def kernel(tmp_path_factory):
    output = tmp_path_factory.mktemp("word-kernel")/"qualify-word-events"
    compiler = shutil.which("c++") or shutil.which("clang++")
    assert compiler, "C++17 compiler required"
    subprocess.run([compiler, "-std=c++17", "-O2", "-I", str(ROOT/"src/native"),
                    str(ROOT/"tests/native_word_events_test.cpp"),
                    str(ROOT/"src/native/emuflow_core/word_events.cpp"), "-o", str(output)], check=True)
    return output


def project(kernel, roots, transfers, *, period=5., epoch=0., frame=2048, fail=None, chosen=None, slacks=None):
    text = ["check" if chosen is not None else "project",
            f"{period!r} {epoch!r} {frame} {len(roots)} {len(transfers)}", *map(repr, roots)]
    for phase, ratio, latency, stable, fanins in transfers:
        text.append(f"{phase} {ratio} {latency} {int(stable)} {len(fanins)}")
        text.extend(f"{int(event)} {source} {data!r} {cost!r}" for event, source, data, cost in fanins)
    if chosen is not None:
        text.extend([str(len(chosen)), *map(str, chosen), str(len(slacks)), *map(repr, slacks)])
    result = subprocess.run([str(kernel)], input="\n".join(text)+"\n", text=True, capture_output=True)
    if fail:
        assert result.returncode != 0
        assert fail in result.stderr
        return
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def transfer(fanins, *, phase=0, ratio=8, latency=6, stable=False):
    return phase, ratio, latency, stable, fanins


def exhaustive(roots, transfers, period=5., epoch=0.):
    # Qualification oracle enumerates every earlier occurrence and checks every
    # input separately. It neither computes a max-readiness guard nor uses ceil.
    chosen = [None]*len(transfers)
    while any(value is None for value in chosen):
        changed = False
        for i, (phase, ratio, latency, stable, fanins) in enumerate(transfers):
            if chosen[i] is not None or any(event and chosen[source] is None for event, source, _, _ in fanins):
                continue
            for word in range(1, 2000):
                if word % (ratio//8) != phase:
                    continue
                if all((not event or word > chosen[source]+transfers[source][2]) and
                       epoch+word*period >= (epoch+(chosen[source]+transfers[source][2])*period if event else roots[source])+cost
                       for event, source, _, cost in fanins):
                    chosen[i] = word;changed = True;break
            assert chosen[i] is not None, "oracle fixture exceeds bounded calendar"
        assert changed, "cyclic oracle fixture"
    return chosen


def test_complete_fanin_and_same_edge_relay_are_resolved_before_selected_paths(kernel):
    # The second root is absent from a hypothetical selected path through root0.
    # Its later arrival still governs the shared serializer and both relays.
    roots = [0., 50.]
    transfers = [transfer([(False, 0, 2., 2.), (False, 1, 1., 1.)], ratio=24, phase=1),
                 transfer([(True, 0, 0., 0.)], ratio=16, phase=1),
                 transfer([(True, 0, 4., -2.), (True, 1, 2., 2.)])]
    selected = project(kernel, roots, transfers)
    assert selected == exhaustive(roots, transfers) == [13, 21, 28]
    assert project(kernel, roots, [transfer(transfers[0][4][:1], ratio=24, phase=1)]) == [1]


def test_signed_cost_is_a_check_adjustment_and_does_not_replace_raw_delay(kernel):
    roots = [0.]
    transfers = [transfer([(False, 0, 8.642, 5.413)]),
                 transfer([(True, 0, 20., -2.)])]
    assert project(kernel, roots, transfers) == [2, 9]
    # RX shadow is captured at word8; favorable cost cannot reuse that edge.
    assert exhaustive(roots, transfers) == [2, 9]


@pytest.mark.parametrize("guard,expected", [(5., 1), (math.nextafter(5., -math.inf), 1),
                                           (math.nextafter(5., math.inf), 2)])
def test_exact_word_edges_do_not_use_epsilon_snapping(kernel, guard, expected):
    assert project(kernel, [0.], [transfer([(False, 0, 1., guard)])]) == [expected]


def test_unsorted_dag_and_varied_word_phases_match_exhaustive_oracle(kernel):
    generator = random.Random(20261026)
    roots = [0., 50., 25.]
    for _ in range(20):
        transfers = []
        for i in range(25):
            ratio = generator.choice([8, 16, 24, 40, 56, 224, 256])
            fanins = [(False, generator.randrange(len(roots)), generator.randrange(12)/2,
                       generator.randrange(-4, 24)/2)]
            for parent in generator.sample(range(i), min(i, generator.randrange(4))):
                fanins.append((True, parent, generator.randrange(12)/2, generator.randrange(-4, 24)/2))
            transfers.append(transfer(fanins, ratio=ratio, phase=generator.randrange(ratio//8)))
        permutation = list(range(len(transfers)));generator.shuffle(permutation)
        inverse = {old:new for new,old in enumerate(permutation)}
        shuffled = [(*transfers[old][:4], [(event, inverse[source] if event else source, data, cost)
                                         for event, source, data, cost in transfers[old][4]]) for old in permutation]
        assert project(kernel, roots, shuffled) == exhaustive(roots, shuffled)


@pytest.mark.parametrize("roots,transfers,message", [
    ([0.], [transfer([])], "complete source semantics"),
    ([0.], [transfer([(False, 0, 1., 1.)], stable=True)], "complete source semantics"),
    ([], [transfer([(False, 0, 1., 1.)])], "architectural launch phase"),
    ([math.nan], [transfer([(False, 0, 1., 1.)])], "launch phase"),
    ([0.], [transfer([(False, 0, -1., 1.)])], "fanin cost"),
    ([0.], [transfer([(False, 0, 1., math.inf)])], "fanin cost"),
    ([0.], [transfer([(False, 0, 1e308, -1e308)])], "fanin cost"),
    ([0.], [transfer([(False, 0, 1., 1.)], ratio=9)], "eight-bit word"),
    ([0.], [transfer([(False, 0, 1., 1.)], ratio=8, phase=1)], "eight-bit word"),
    ([0.], [transfer([(True, 0, 1., 1.)])], "self event"),
    ([0.], [transfer([(True, 1, 1., 1.)]), transfer([(True, 0, 1., 1.)])], "contains a cycle"),
    ([1e100], [transfer([(False, 0, 1., 1.)])], "misses frame drain"),
])
def test_invalid_or_incomplete_evidence_fails(kernel, roots, transfers, message):
    project(kernel, roots, transfers, fail=message)


def test_configuration_stable_source_is_explicit_and_first_edge_is_positive(kernel):
    assert project(kernel, [], [transfer([], stable=True, ratio=24, phase=0)]) == [3]


def test_clock_resolution_and_capture_domain_fail_closed(kernel):
    project(kernel, [0.], [transfer([(False, 0, 0., 0.)])], epoch=1e20, fail="loses edge resolution")
    project(kernel, [0.], [transfer([(False, 0, 0., 0.)], latency=2**32-2),
                           transfer([(True, 0, 0., 0.)])], fail="cannot arrive before frame drain")


def test_projection_obeys_rtl_strict_drain_boundary(kernel):
    transfers = [transfer([(False, 0, 1., 50.)])]
    assert project(kernel, [0.], transfers, frame=18) == [10]  # RX16 < frame-1.
    project(kernel, [0.], transfers, frame=17, fail="misses frame drain")
    project(kernel, [0.], transfers, frame=0, fail="requires its frame drain boundary")


def test_changing_dut_period_requires_reprojection_and_is_not_monotone(kernel):
    transfers = [transfer([(False, 0, 1., 1.)], ratio=64, phase=1)]
    at_80 = project(kernel, [80./2], transfers)
    at_99 = project(kernel, [99./2], transfers)
    assert at_80 == [9] and (at_80[0]+6)*5 <= 80.
    assert at_99 == [17] and (at_99[0]+6)*5 > 99.


def test_native_checker_uses_support_slacks_and_rejects_skipped_or_missed_edges(kernel):
    transfers = [transfer([(False, 0, 8.642, 5.413)])]
    assert project(kernel, [0.], transfers, chosen=[2], slacks=[4.587]) is True
    project(kernel, [0.], transfers, chosen=[1], slacks=[-.413], fail="fails an OpenSTA support check")
    project(kernel, [0.], transfers, chosen=[3], slacks=[9.587], fail="earlier word occurrence")
    project(kernel, [0.], transfers, chosen=[2], slacks=[], fail="coverage is incomplete")
    project(kernel, [0.], transfers, chosen=[2], slacks=[4.587, 0.], fail="extra records")
    project(kernel, [0.], transfers, chosen=[2], slacks=[math.nan], fail="fails an OpenSTA support check")
    project(kernel, [0.], transfers, chosen=[], slacks=[], fail="population differs")


def test_native_checker_checks_strict_causality_and_explicit_constants(kernel):
    transfers = [transfer([(False, 0, 0., 0.)]), transfer([(True, 0, 0., 0.)])]
    assert project(kernel, [0.], transfers, chosen=[1, 8], slacks=[5., 5.]) is True
    project(kernel, [0.], transfers, chosen=[1, 7], slacks=[5., 0.], fail="reuses its RX capture edge")
    project(kernel, [], [transfer([], stable=True)], chosen=[2], slacks=[], fail="earlier word occurrence")
    project(kernel, [0.], transfers, chosen=[1, 8], slacks=[5., 5.], frame=15, fail="phase or frame drain")


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENSTA"), reason="explicit qualified OpenSTA required")
def test_real_opensta_checks_all_fanins_and_independently_verifies_first_legal_word(kernel, tmp_path):
    from emuflow.global_sta import EventCheck, run_event_checks
    roots = [0., 50.]
    transfers = [transfer([(False, 0, 2., 1.5), (False, 1, 4., 5.)], ratio=24, phase=1),
                 transfer([(True, 0, 0., 0.)], ratio=16, phase=1),
                 transfer([(True, 0, 3., -1.), (True, 1, 2., 2.)])]
    selected = project(kernel, roots, transfers)
    checks = []
    for i, (_, _, latency, _, fanins) in enumerate(transfers):
        name = f"synthetic-transfer-{i}"
        for j, (event, source, data, cost) in enumerate(fanins):
            launch = (selected[source]+transfers[source][2])*5. if event else roots[source]
            checks.append(EventCheck(name, "tx", str(j), launch, (data,), selected[i]*5.-(cost-data)))
        # Synthetic observations satisfy the generic engine bundle interface;
        # they are not original DUT members or a global coverage certificate.
        for role in ("target", "runtime"):
            checks.append(EventCheck(name, role, "synthetic-observation", selected[i]*5., (latency*5.,), (selected[i]+latency)*5.))
    measurements = run_event_checks(checks, tmp_path, os.environ["EMUFLOW_TEST_OPENSTA"])
    assert all(row["slack_ns"] >= -1e-6 for row in measurements)
    by_check = {(row["path"], row["role"], row["event"]):row for row in measurements}
    support_slacks = [by_check[(f"synthetic-transfer-{i}", "tx", str(j))]["slack_ns"]
                      for i, record in enumerate(transfers) for j in range(len(record[4]))]
    assert project(kernel, roots, transfers, chosen=selected, slacks=support_slacks) is True
    # Delaying the last transfer by a whole table round passes its numerical
    # STA checks, but must fail the independent first-occurrence requirement.
    from dataclasses import replace
    shift = transfers[-1][1]/8*5.
    last_name = f"synthetic-transfer-{len(transfers)-1}"
    late_checks = [replace(row, required_ns=row.required_ns+shift,
                          launch_ns=row.launch_ns+(shift if row.role in ('target','runtime') else 0.))
                   if row.path==last_name else row for row in checks]
    late_measurements = run_event_checks(late_checks, tmp_path/'late', os.environ['EMUFLOW_TEST_OPENSTA'])
    assert all(row['slack_ns'] >= -1e-6 for row in late_measurements)
    late_by_check = {(row['path'],row['role'],row['event']):row for row in late_measurements}
    late_slacks = [late_by_check[(f'synthetic-transfer-{i}','tx',str(j))]['slack_ns']
                   for i,record in enumerate(transfers)for j in range(len(record[4]))]
    late_selected = list(selected);late_selected[-1] += transfers[-1][1]//8
    project(kernel, roots, transfers, chosen=late_selected, slacks=late_slacks, fail='earlier word occurrence')
    for i, (phase, ratio, _, _, fanins) in enumerate(transfers):
        for earlier in range(1, selected[i]):
            if earlier % (ratio//8) != phase:
                continue
            admissible = []
            for j, (event, source, data, cost) in enumerate(fanins):
                observed = by_check[(f"synthetic-transfer-{i}", "tx", str(j))]["arrival_ns"]
                admissible.append((not event or earlier > selected[source]+transfers[source][2]) and
                                  observed+cost-data <= earlier*5.+1e-6)
            assert not all(admissible), "producer skipped an earlier legal occurrence"
