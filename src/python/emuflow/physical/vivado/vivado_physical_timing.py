"""Select the shared native local-path query owner for Vivado."""
from pathlib import Path
from typing import Any

def write_vivado_local_query(merged_ir: Path, fpga: str, query_path: Path,
        identity_path: Path, prepared: Any) -> dict[str, Any]:
    from emuflow.timing.local_path_query_native import write_native_local_path_query
    return write_native_local_path_query(merged_ir, fpga, query_path, identity_path, prepared, backend='vivado')
