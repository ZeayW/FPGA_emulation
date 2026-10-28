"""Single-stage adapter harness; no Python flow execution order is retained."""
from emuflow.flow.native_compile import _request
from emuflow.flow.native_compile_adapter import _initial_context
from emuflow.flow.multi_fpga_flow import _compile_preflight


def preflight(*args, **kwargs):
    from pathlib import Path
    import tempfile
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    with tempfile.TemporaryDirectory(prefix="preflight-contract-") as directory:
        if kwargs.get("architecture_timing_db") is None:
            kwargs["architecture_timing_db"] = write_mock_xilinx_timing_contract(Path(directory) / "timing.json")
        request, _ = _request(args, kwargs)
        context = _initial_context(request)
        _compile_preflight(context)
        return context
