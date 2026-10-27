"""Small native bridge source facts, using the current typed input writers."""

import json
from pathlib import Path
from types import SimpleNamespace

from emuflow.common.io import file_sha256
from emuflow.physical.xilinx_netlist import open_xilinx_mapped_index
from emuflow.physical.openparf.xilinx_openparf_atomic import (
    open_xilinx_openparf_atomic_placement,
    write_xilinx_openparf_atomic_placement_certificate,
)
from emuflow.physical.openparf.xilinx_openparf_source_storage import (
    write_xilinx_openparf_atomic_source_storage,
)
from tests.test_rwroute_native import write_mapped


def prepare_native_handoff(root, *, split_ff_control_sets=False, include_hard=False):
    from tests.test_xilinx_openparf_bridge import _native_certificate

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    mapped_json, architecture, certificate = _native_certificate(
        root, include_hard=include_hard, split_ff_control_sets=split_ff_control_sets
    )
    mapped = root / "mapped.sqlite"
    write_mapped(mapped, json.loads(mapped_json.read_text()))
    digest = file_sha256(mapped)
    source_path = root / "typed-source.json"
    with open_xilinx_mapped_index(mapped) as index:
        source = write_xilinx_openparf_atomic_source_storage(
            source_path,
            mapped_path=mapped,
            mapped_value=index,
            mapped_sha256=digest,
            top="top",
            macro_clusters=[],
            cascades=[],
            physical_macro_count=0,
        )
        owners = {
            instance: cluster
            for instance, cluster, _ in source.iter_source_assignments()
        }
    with open_xilinx_openparf_atomic_placement(certificate, allow_legacy=False) as view:
        value = dict(view)
        value["clusters"] = list(view.iter_clusters())
    value["source"]["mapped_sha256"] = digest
    value["source"]["packed_sha256"] = file_sha256(source_path)
    for cluster in value["clusters"]:
        for assignment in cluster["assignments"]:
            assignment["source_cluster"] = owners[assignment["instance"]]
    write_xilinx_openparf_atomic_placement_certificate(certificate, value)
    return SimpleNamespace(
        mapped=mapped,
        architecture=architecture,
        certificate=certificate,
        source=source_path,
        owners=owners,
    )


def build_native_handoff(value, output):
    from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract

    return materialize_xilinx_openparf_atomic_contract(
        value.mapped,
        value.architecture,
        value.certificate,
        Path(output),
        source_packed_path=value.source,
    )


def encode_typed_bridge_facts(
    mapped_path, architecture_path, certificate_path, directory
):
    """Translate bounded fixture records to current owners; no placement decisions."""
    from emuflow.common.errors import ValidationError
    from emuflow.physical.openparf.xilinx_openparf_atomic import (
        build_xilinx_openparf_atomic_source,
    )
    from emuflow.physical.xilinx_netlist import is_xilinx_mapped_index

    mapped_path, certificate_path, directory = map(
        Path, (mapped_path, certificate_path, directory)
    )
    directory.mkdir(parents=True, exist_ok=True)
    selector = json.loads(certificate_path.read_text())
    if selector.get("source", {}).get("mapped_sha256") != file_sha256(mapped_path):
        raise ValidationError("fixture certificate source identity seal disagrees")
    if is_xilinx_mapped_index(mapped_path):
        raise ValueError("fixture encoding only accepts explicit bounded mapped JSON")
    mapped_value = json.loads(mapped_path.read_text())
    if sum(len(m["cells"]) for m in mapped_value["modules"].values()) > 4096:
        raise ValueError("mapped fixture population exceeds bound")
    with open_xilinx_openparf_atomic_placement(certificate_path) as view:
        certificate = dict(view)
        certificate["clusters"] = list(view.iter_clusters())
    if sum(len(c["assignments"]) for c in certificate["clusters"]) > 4096:
        raise ValueError("certificate fixture population exceeds bound")
    for module in mapped_value["modules"].values():
        module.setdefault("ports", {})
        module.setdefault("netnames", {})
    index = directory / "mapped.sqlite"
    write_mapped(index, mapped_value)
    source_path = directory / "source.json"
    with open_xilinx_mapped_index(index) as mapped:
        source = build_xilinx_openparf_atomic_source(
            index, source_path, mapped_value=mapped
        )
        owners = {name: owner for name, owner, _ in source.iter_source_assignments()}
    certificate["source"].update(
        mapped_sha256=file_sha256(index), packed_sha256=file_sha256(source_path)
    )
    for cluster in certificate["clusters"]:
        for assignment in cluster["assignments"]:
            # IDs are scoped to the new fixture artifact, not the old JSON.
            assignment["source_cluster"] = owners.get(
                assignment["instance"], assignment["source_cluster"]
            )
    typed_certificate = directory / "certificate.json"
    write_xilinx_openparf_atomic_placement_certificate(typed_certificate, certificate)
    return SimpleNamespace(
        mapped=index,
        architecture=Path(architecture_path),
        certificate=typed_certificate,
        source=source_path,
        owners=owners,
    )


def validate_certificate_facts(
    mapped, certificate, architecture, top, *, native_bram_groups=None
):
    """Exercise the real native producer with explicitly bounded device facts."""
    import copy
    import tempfile
    from collections import Counter
    from emuflow.physical.handoff import materialize_xilinx_openparf_atomic_contract
    from emuflow.physical.openparf.xilinx_openparf_atomic import (
        build_xilinx_openparf_atomic_source,
    )
    from tests.test_xilinx_native_device_constraints import PINNED, _artifact, _reseal

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        value = copy.deepcopy(mapped)
        for module in value["modules"].values():
            module.setdefault("ports", {})
            module.setdefault("netnames", {})
            for cell in module["cells"].values():
                cell.setdefault("parameters", {})
                cell.setdefault("attributes", {})
                cell.setdefault("connections", {})
                cell.setdefault("port_directions", {})
        mapped_path = root / "mapped.sqlite"
        write_mapped(mapped_path, value)
        arch = copy.deepcopy(architecture.value)
        arch["part"] = json.loads(PINNED.read_text())["device"]["part"]
        for site in arch["sites"]:
            site["physical_region"] = {"slr": "SLR0", "clock_region": "X0Y0"}
        architecture_path = root / "architecture.json"
        architecture_path.write_text(json.dumps(arch))
        provider = root / "provider.json"
        provider.write_bytes(PINNED.read_bytes())
        native = _artifact(architecture_path, provider)
        payload = native["payload"]
        payload["capabilities"]["dedicated_adjacency.CARRY_NEXT"] = "core_missing"
        groups = copy.deepcopy(list((native_bram_groups or {}).values()))
        for group in groups:
            for i, role in enumerate(("upper", "lower", "whole")):
                view = group[role]
                view.update(
                    site_index=i,
                    native_bel={
                        "lower": "RAMBFIFO18",
                        "upper": "RAMB18E2_U",
                        "whole": "RAMBFIFO36E2",
                    }[role],
                    native_site_type={
                        "lower": "RAMBFIFO18",
                        "upper": "RAMB181",
                        "whole": "RAMBFIFO36",
                    }[role],
                )
        capacity = Counter(
            (
                s["physical_region"]["slr"],
                s["physical_region"]["clock_region"],
                s["type"],
            )
            for s in arch["sites"]
        )
        payload.update(
            bram_tile_groups=groups,
            dedicated_adjacency=[],
            site_capacity=[
                dict(slr=k[0], clock_region=k[1], site_type=k[2], sites=n)
                for k, n in sorted(capacity.items())
            ],
        )
        payload["summary"].update(
            bram_tile_groups=len(groups),
            capacity_buckets=len(capacity),
            clock_regions=1,
            dedicated_edges=0,
            dedicated_edges_by_kind={
                k: 0 for k in payload["summary"]["dedicated_edges_by_kind"]
            },
            sites=len(arch["sites"]),
            slrs=1,
        )
        _reseal(native)
        native_path = root / "native.json"
        native_path.write_text(json.dumps(native))
        source_path = root / "source.json"
        with open_xilinx_mapped_index(mapped_path) as index:
            source = build_xilinx_openparf_atomic_source(
                mapped_path, source_path, mapped_value=index
            )
            owners = {
                name: owner for name, owner, _ in source.iter_source_assignments()
            }
        cert = copy.deepcopy(certificate)
        cert["part"] = arch["part"]
        cert["source"] = {
            "mapped_sha256": file_sha256(mapped_path),
            "architecture_sha256": file_sha256(architecture_path),
            "packed_sha256": file_sha256(source_path),
            "native_placement_sha256": "0" * 64,
            "name_map_sha256": "0" * 64,
            "native_constraints_sha256": file_sha256(native_path),
            "provider_manifest_sha256": file_sha256(provider),
        }
        for cluster in cert["clusters"]:
            for row in cluster["assignments"]:
                row["source_cluster"] = owners[row["instance"]]
        path = root / "certificate.json"
        write_xilinx_openparf_atomic_placement_certificate(path, cert)
        return materialize_xilinx_openparf_atomic_contract(
            mapped_path,
            architecture_path,
            path,
            root / "out",
            top=top,
            source_packed_path=source_path,
            native_constraints_path=native_path,
            provider_manifest_path=provider,
        )
