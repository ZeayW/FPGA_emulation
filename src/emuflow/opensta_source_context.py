"""Ephemeral indexed source context for canonical launch-clock extraction."""
from dataclasses import dataclass
from pathlib import Path
import shutil

from .errors import ValidationError
from .indexed_timing import IndexedTimingView
from .io import file_sha256
from .native_tools import resolve_native_executable
from .opensta import (DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, _clock_map,
    _prepare_opensta_source_model, _write_opensta_source_files, require_opensta_engine)
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged


@dataclass(frozen=True)
class OpenSTASourceContext:
    command: tuple[str, ...]
    environment: dict[str, str]
    timing_inputs: dict[Path, str]
    source_bindings: tuple[StoreSource, ...]
    reference_clocks_ns: dict[str, float]
    timing_io: dict
    model: dict
    engine: dict

    def require_unchanged(self):
        require_store_sources_unchanged(self.source_bindings)


def emit_opensta_source_context(original_binding, directory, *, clocks, timing_io,
        timing_model_path=DEFAULT_TIMING_MODEL, architecture_timing_db_path=None,
        executable=None):
    """Use Phase 2's emitter without rerunning path extraction or synthesis.

    The caller supplies the original source's existing validated generation,
    original reference clocks and I/O assumptions. Implementation-clock scaling
    belongs to the later native consumer. Model files are new ephemeral bytes;
    their seals are not claims to restore historical Phase 2 model bytes.
    Retain the returned leases through the launch-clock producer invocation.
    """
    if not isinstance(original_binding, StoreSource) or original_binding.schema != 'emuir-compact-v2':
        raise ValidationError('source context requires the original compact generation lease')
    require_store_sources_unchanged((original_binding,))
    model_path = Path(architecture_timing_db_path if architecture_timing_db_path is not None else timing_model_path).resolve()
    model_binding = bind_store_source(model_path, 'sta-source-document-v1')
    tool = Path(resolve_native_executable('sta', executable)).resolve()
    engine_binding = bind_store_source(tool, 'sta-source-document-v1')
    engine = require_opensta_engine(str(tool))
    scripts = [OPENSTA_EXPORT_SCRIPT.with_name(name).resolve() for name in
               ('export_launch_clock_edges.tcl', 'design_context.tcl', 'launch_clock_edges.tcl')]
    script_bindings = tuple(bind_store_source(path, 'sta-source-document-v1') for path in scripts)
    parents = (original_binding, model_binding, engine_binding, *script_bindings)
    require_store_sources_unchanged(parents)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        view = IndexedTimingView(original_binding.path)
        try:
            model, types, clock_logic, _coverage = _prepare_opensta_source_model(
                view, timing_model_path, architecture_timing_db_path, 'engine-edge-v1')
            reference_clocks = _clock_map(view, clocks)
            io_summary = _write_opensta_source_files(view, root, model, types, reference_clocks, timing_io)
            model_identity = dict(name=model['name'], source=model['source'], clock_logic=clock_logic)
        finally:
            view.close()
        produced = tuple(bind_store_source(root / name, 'sta-source-document-v1') for name in
                         ('mapped.v', 'timing.lib', 'clocks.tsv', 'timing-io.tsv'))
        # Each new/model/script file is sealed once while its generation lease
        # is held. The engine probe already owns its executable/launcher seal.
        inputs = {binding.path: file_sha256(binding.path) for binding in (*produced, model_binding, *script_bindings)}
        inputs[tool] = engine.get('launcher_sha256', engine['executable_sha256'])
        bindings = (*parents, *produced)
        require_store_sources_unchanged(bindings)
        result = OpenSTASourceContext(
            (str(tool), '-no_init', '-exit', str(scripts[0])),
            dict(EMUFLOW_STA_LIBERTY=str(root / 'timing.lib'), EMUFLOW_STA_VERILOG=str(root / 'mapped.v'),
                 EMUFLOW_STA_CLOCKS=str(root / 'clocks.tsv'), EMUFLOW_STA_TIMING_IO=str(root / 'timing-io.tsv')),
            inputs, bindings, reference_clocks, io_summary, model_identity, engine)
        result.require_unchanged()
        return result
    except BaseException:
        shutil.rmtree(root)
        raise
