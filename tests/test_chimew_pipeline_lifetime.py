"""Native audit failures release their readers before returning a traceback."""
import json
import shutil
from unittest.mock import patch
import pytest
from emuflow.common.errors import ValidationError
from emuflow_tools.transport.chimew.chimew_pipeline import run_chimew_phase6_pipeline, validate_chimew_phase6_pipeline
from tests.chimew_pipeline_fixture import pipeline_inputs


def test_failed_pipeline_validation_closes_owned_report_with_live_traceback(tmp_path):
    paths,sources=pipeline_inputs(tmp_path)
    output=tmp_path/"pipeline"
    report=run_chimew_phase6_pipeline(*(paths[name] for name in ("schedule","platform","crossings","positions","rudy_input","bank_input","electrical")),output,source_paths=sources)
    generation=(output/report["artifacts"]["generation_manifest"]["path"]).parent
    report["metrics"]["groups"]+=1
    (output/"pipeline_report.json").write_text(json.dumps(report))
    with patch("emuflow_tools.transport.chimew.chimew_bank_channel_report_storage.read_chimew_bank_channel_report",side_effect=AssertionError("Python report reader")):
        with pytest.raises(ValidationError,match="summary.*does not agree") as error:
            validate_chimew_phase6_pipeline(output)
        assert error.value.__traceback__ is not None
        # The native process has unwound every SQLite reader. This removal must
        # succeed on NFS while the rejected Python traceback remains alive.
        shutil.rmtree(generation)
        assert not generation.exists()
