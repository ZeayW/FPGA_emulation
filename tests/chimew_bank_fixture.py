from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage
"""Small raw fixtures enter production only through the current typed owners."""
from contextlib import ExitStack
from pathlib import Path
import tempfile

from emuflow.transport.chimew.chimew_bank_channel import (
    evaluate_chimew_bank_channel_assignment as assign_typed,
)
from emuflow.transport.chimew.chimew_bank_channel_storage import (
    BankChannelInputView, write_chimew_bank_channel_storage,
)


def evaluate_chimew_bank_channel_assignment(document, *, executable=None, output_path=None):
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        source = document if isinstance(document, BankChannelInputView) else stack.enter_context(
            write_chimew_bank_channel_storage(root / "bank-input.json", document))
        view = assign_typed(source, executable=executable,
                            output_path=output_path or root / "bank-report.json")
        if output_path is not None:
            return view
        with view:
            return view.materialize()


def build_chimew_phase6_pin_plan(schedule, platform, bank_input, electrical, **options):
    from emuflow.transport.chimew.chimew_phase6 import build_chimew_phase6_pin_plan as build
    from emuflow.transport.chimew.chimew_bank_channel_report_storage import (
        BankChannelReportView,
    )
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        source = bank_input if isinstance(bank_input, BankChannelInputView) else stack.enter_context(
            write_chimew_bank_channel_storage(root / "bank-input.json", bank_input))
        supplied = options.get("bank_channel_report_document")
        owned_report = supplied is not None and not isinstance(supplied, BankChannelReportView)
        if owned_report:
            options["bank_channel_report_document"] = stack.enter_context(
                write_chimew_bank_channel_report_storage(root / "supplied-report.json", supplied))
        temporary_report = (options.get("bank_channel_report_document") is None
                            and options.get("bank_channel_report_output_path") is None)
        if temporary_report:
            options["bank_channel_report_output_path"] = root / "bank-report.json"
        result = build(schedule, platform, source, electrical, **options)
        if temporary_report or owned_report:
            report = result["bank_channel_report"]
            with report:
                result["bank_channel_report"] = report.materialize()
        return result
