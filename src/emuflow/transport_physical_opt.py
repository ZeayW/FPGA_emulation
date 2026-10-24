"""Placement-guided rewiring of existing equivalent transport LUT1 branches.

This explicit qualification transform publishes a new transport EmuIR. The
caller must remap, place, and validate it through the normal Phase 7 pipeline.
It never edits a placed/routed certificate or silently changes router input.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .emuir_storage import open_emuir_sqlite_read_only
from .errors import ValidationError
from .io import file_sha256
from .xilinx_physical_handoff import validate_xilinx_physical_handoff


def _assign(costs, original):
    """Fixed-capacity regret-first assignment; retain the original if worse."""
    capacity = Counter(original)
    preferences = [sorted(range(len(row)), key=lambda j: (row[j], j)) for row in costs]
    order = sorted(range(len(costs)), key=lambda i: (
        -(costs[i][preferences[i][1]] - costs[i][preferences[i][0]]), i
    ))
    chosen = [None] * len(costs)
    for i in order:
        branch = next(j for j in preferences[i] if capacity[j])
        chosen[i] = branch
        capacity[branch] -= 1
    before = sum(row[j] for row, j in zip(costs, original))
    after = sum(row[j] for row, j in zip(costs, chosen))
    return (chosen, before, after) if after < before else (original, before, before)


def _cluster(points, original, anchors):
    """Alternate fixed-capacity assignment and Manhattan-optimal medians.

    Compare against optimally recentered original groups, so merely moving
    a buffer cannot masquerade as an improvement from rewiring its sinks.
    Returned centers are guidance for a fresh placement, never legal sites.
    """
    def medians(labels):
        centers = []
        for j in range(len(anchors)):
            group = [p for p, b in zip(points, labels) if b == j]
            centers.append(tuple(sorted(p[d] for p in group)[len(group)//2]
                                 for d in range(2)) if group else anchors[j])
        return centers

    def cost(labels, centers):
        return sum(abs(p[0]-centers[j][0]) + abs(p[1]-centers[j][1])
                   for p, j in zip(points, labels))

    best, centers = original, medians(original)
    before = after = cost(original, centers)
    # Farthest-point seeding breaks the near-identical central anchors that
    # prevented pure rewiring around already placed buffers from spreading.
    seeds = [min(points)]
    while len(seeds) < len(anchors):
        seeds.append(max(points, key=lambda p: (
            min(abs(p[0]-c[0])+abs(p[1]-c[1]) for c in seeds), p)))
    for initial in (centers, seeds):
        labels, trial = original, initial
        for _ in range(12):
            costs = [[abs(p[0]-c[0])+abs(p[1]-c[1]) for c in trial] for p in points]
            updated, _, _ = _assign(costs, labels)
            trial = medians(updated)
            value = cost(updated, trial)
            if value < after:
                best, centers, after = list(updated), trial, value
            if updated == labels:
                break
            labels = updated
    return best, centers, before, after


def rewire_transport_buffers(source: Path, handoff: Path, output: Path):
    """Reassign sinks only between identity LUT1s sharing exactly one input.

    Existing per-branch fanout is preserved exactly. A group is skipped unless
    every sink and driver has a physical coordinate. Each published change
    strictly reduces within-group Manhattan distance to optimally recentered
    buffers. The returned target locations require fresh placement. This
    predicts a benefit; only a subsequent route can prove it.
    """
    source, handoff, output = map(Path, (source, handoff, output))
    if output.resolve() in {source.resolve(), handoff.resolve()}:
        raise ValidationError("physical optimization requires a separate output")
    source_sha = file_sha256(source)
    handoff_sha = validate_xilinx_physical_handoff(handoff)['physical_handoff_sha256']
    checked = open_emuir_sqlite_read_only(source)
    try:
        if checked.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValidationError("physical transport rewiring requires compact EmuIR v2")
    finally:
        checked.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    db = None
    try:
        shutil.copyfile(source, temporary)
        db = sqlite3.connect(temporary, uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA temp_store=FILE")
        db.execute("ATTACH DATABASE ? AS physical", (
            handoff.resolve().as_uri() + "?mode=ro&immutable=1",
        ))
        positions = {int(row[0]): (int(row[1]), int(row[2])) for row in db.execute(
            "SELECT t.seq,c.x,c.y FROM cell_records t "
            "JOIN physical.assignment a ON a.instance='__emuflow_transport__/'||t.id "
            "JOIN physical.cluster c ON c.ordinal=a.cluster_ordinal"
        )}
        groups = defaultdict(list)
        names = {}
        for row in db.execute(
            "SELECT c.seq,c.id,c.type,p.value AS parameters,a.value AS attributes "
            "FROM cell_records c JOIN payloads a ON a.seq=c.attributes_seq "
            "JOIN payloads p ON p.seq=c.parameters_seq "
            "WHERE instr(c.id,'$emuflow_transport_fanout$')=1 ORDER BY c.id"
        ):
            attrs = json.loads(row['attributes'])
            if attrs.get('emuflow_high_fanout_level') != '0':
                continue
            params = json.loads(row['parameters'])
            if (row['type'] != '$lut' or int(params['WIDTH'], 2) != 1
                    or params['LUT'] != '10'
                    or attrs.get('emuflow_high_fanout_buffer') != 'transport-balanced-tree-v1'):
                raise ValidationError("transport branch is not a proven identity LUT1")
            seq = int(row['seq'])
            pins = list(db.execute(
                "SELECT e.net_seq,e.role,p.name,e.bit FROM endpoint_records e "
                "JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=?",
                (seq,),
            ))
            inputs = [p for p in pins if p['role'] == 1 and p['name'] == 'A' and p['bit'] == 0]
            outputs = [p for p in pins if p['role'] == 0 and p['name'] == 'Y' and p['bit'] == 0]
            if len(pins) != 2 or len(inputs) != 1 or len(outputs) != 1:
                raise ValidationError("transport branch has unexpected electrical connectivity")
            owner = attrs.get('emuflow_high_fanout_owner')
            if not isinstance(owner, str):
                raise ValidationError("transport branch lacks an original net owner")
            groups[(owner, int(inputs[0]['net_seq']))].append((seq, int(outputs[0]['net_seq'])))
            names[seq] = row['id']
        records = []
        db.execute("BEGIN IMMEDIATE")
        for (owner, _), branches in sorted(groups.items()):
            if len(branches) < 2:
                continue
            sinks, original = [], []
            for j, (_, net) in enumerate(branches):
                drivers = list(db.execute(
                    "SELECT instance_seq FROM endpoint_records WHERE net_seq=? AND role=0", (net,)
                ))
                if len(drivers) != 1 or drivers[0][0] != branches[j][0]:
                    raise ValidationError("transport branch output has conflicting drivers")
                rows = list(db.execute(
                    "SELECT instance_seq,port_seq,bit,extras_seq FROM endpoint_records "
                    "WHERE net_seq=? AND role=1 ORDER BY ordinal", (net,)
                ))
                sinks.extend(tuple(row) for row in rows)
                original.extend([j] * len(rows))
            if any(seq not in positions for seq, _ in branches) or any(s[0] not in positions for s in sinks):
                records.append({'owner': owner, 'status': 'skipped-missing-coordinate'})
                continue
            if not sinks:
                continue
            chosen, centers, before, after = _cluster(
                [positions[s[0]] for s in sinks], original,
                [positions[b] for b, _ in branches],
            )
            if Counter(chosen) != Counter(original):
                raise ValidationError("physical rewiring changed branch capacity")
            moves = [{'instance_seq': s[0], 'port_seq': s[1], 'bit': s[2],
                      'from_net_seq': branches[a][1], 'to_net_seq': branches[b][1]}
                     for s, a, b in zip(sinks, original, chosen) if a != b]
            if moves:
                for _, net in branches:
                    db.execute("DELETE FROM endpoint_records WHERE net_seq=? AND role=1", (net,))
                for j, (_, net) in enumerate(branches):
                    assigned = [s for s, b in zip(sinks, chosen) if b == j]
                    db.executemany("INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                                   ((net, 1, ordinal, *s) for ordinal, s in enumerate(assigned)))
                actual = []
                for _, net in branches:
                    actual.extend(tuple(row) for row in db.execute(
                        "SELECT instance_seq,port_seq,bit,extras_seq FROM endpoint_records "
                        "WHERE net_seq=? AND role=1", (net,)))
                if Counter(actual) != Counter(sinks):
                    raise ValidationError("physical rewiring lost or duplicated endpoints")
            records.append({'owner': owner, 'status': 'improved' if moves else 'unchanged',
                            'branches': [names[b] for b, _ in branches],
                            'target_centers': centers,
                            'before_manhattan': before, 'after_manhattan': after, 'moves': moves})
        db.commit()
        db.close()
        db = None
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        return {'schema': 'emuflow.transport-physical-rewire/v1', 'status': 'candidate',
                'source_sha256': source_sha, 'placement_handoff_sha256': handoff_sha,
                'objective': 'capacity-preserving-sink-cluster-median-distance',
                'output_sha256': file_sha256(output), 'groups': records,
                'summary': {'improved_groups': sum(r['status'] == 'improved' for r in records),
                            'moved_sinks': sum(len(r.get('moves', [])) for r in records),
                            'before_manhattan': sum(r.get('before_manhattan', 0) for r in records),
                            'after_manhattan': sum(r.get('after_manhattan', 0) for r in records)}}
    except BaseException:
        if db is not None:
            db.close()
        temporary.unlink(missing_ok=True)
        raise
