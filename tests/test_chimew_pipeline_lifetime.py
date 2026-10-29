"""Semantic failure releases typed readers before their traceback is discarded."""

from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.transport.chimew import chimew_pipeline
from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage
from emuflow.common.errors import ValidationError
from tests.native_build import emuflow_native


def test_failed_pipeline_validation_closes_owned_report_with_live_traceback(tmp_path):
    from tests.test_chimew_bank_channel import ChimewBankChannelTest

    case = ChimewBankChannelTest()
    case.setUp()
    report = evaluate_chimew_bank_channel_assignment(
        case.document, executable=str(emuflow_native())
    )
    view = write_chimew_bank_channel_report_storage(tmp_path / "report.json", report)
    # Open the lazy lookup connection as well as the generation lease.
    view["assignments"][0]
    paths = {
        name: Path(name)
        for name in (
            "schedule",
            "platform",
            "crossings",
            "positions",
            "initial_grouping",
            "refined_grouping",
            "rudy_input",
            "rudy_report",
            "bank_channel_report",
            "electrical_map",
        )
    }
    with (
        patch.object(chimew_pipeline, "read_tdm_schedule", return_value={}),
        patch.object(chimew_pipeline.Platform, "load", return_value=object()),
        patch.object(chimew_pipeline, "read_json", return_value={}),
        patch.object(
            chimew_pipeline, "read_chimew_bank_channel_report", return_value=view
        ) as reader,
    ):
        with pytest.raises(ValidationError, match="BoardDB hash differs") as error:
            chimew_pipeline._validate_pipeline_artifact_semantics_with_bank_input(
                paths,
                case.document,
                provider=chimew_pipeline.CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
            )
        reader.assert_called_once()
        assert error.value.__traceback__ is not None
        with pytest.raises(ValidationError, match="closed"):
            view["assignments"][0]
    # No gc.collect or traceback clearing: an independent producer can replace
    # this generation while the rejected validation and its views remain alive.
    replacement = write_chimew_bank_channel_report_storage(
        tmp_path / "report.json", report
    )
    replacement.close()
