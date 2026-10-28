"""Test-only request builder for the native solver operation."""
from pathlib import Path
from emuflow.physical.openparf.openparf import _runtime_location
from emuflow.physical.openparf import openparf_native_driver
from emuflow.physical.openparf.runtime_native import invoke_runtime

def run_openparf_result(config_path, *, log_path=None, install_root=None, python_executable=None,
                       allow_detailed_legalization_handoff=False, checkpoint=False):
    installation, python, environment = _runtime_location(install_root, python_executable)
    config_path = Path(config_path).resolve()
    report = Path(log_path).absolute().with_suffix(".runtime.json") if log_path is not None else config_path.with_suffix(".runtime.json")
    sources = [("config", config_path), ("driver", Path(openparf_native_driver.__file__))]
    if checkpoint:
        sources.append(("manifest", config_path.with_name("manifest.json")))
    return invoke_runtime("run", report, sources=sources, python=python, installation=installation,
                          environment=environment, detailed=allow_detailed_legalization_handoff, checkpoint=checkpoint)




def validate_openparf_native_metrics(path, *, allow_detailed_legalization_handoff=False):
    path=Path(path).resolve()
    import tempfile
    from emuflow.common.errors import ValidationError
    with tempfile.TemporaryDirectory(prefix='openparf-metrics-') as temporary:
        try:
            result=invoke_runtime('metrics',Path(temporary)/'report.json',sources=[('metrics',path)],
                                  detailed=allow_detailed_legalization_handoff)
        except ValidationError as error:
            raise RuntimeError(str(error)) from error
        return result['metrics']
