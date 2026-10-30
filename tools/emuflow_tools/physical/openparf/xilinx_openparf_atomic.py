"""Optional API for native OpenPARF source and input preparation."""
from __future__ import annotations
from pathlib import Path
def build_xilinx_openparf_atomic_source(mapped_path: Path, output_path: Path, *, top=None, mapped_value=None, mapped_sha256=None):
    from emuflow_tools.physical.openparf.input_native import build_atomic_source
    return build_atomic_source(mapped_path, output_path, top=top, mapped_value=mapped_value, mapped_sha256=mapped_sha256)


def export_xilinx_openparf_atomic(mapped_path: Path, packed_path: Path, architecture_path: Path, output_dir: Path, *, top=None, native_constraints_path=None, provider_manifest_path=None, mapped_value=None, packed_value=None, architecture=None, allowed_slrs=None):
    from emuflow_tools.physical.openparf.input_native import prepare_atomic_input
    return prepare_atomic_input(mapped_path, packed_path, architecture_path, output_dir, top=top, native_constraints_path=native_constraints_path, provider_manifest_path=provider_manifest_path, mapped_value=mapped_value, packed_value=packed_value, architecture=architecture, allowed_slrs=allowed_slrs)
