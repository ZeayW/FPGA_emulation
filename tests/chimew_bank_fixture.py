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
    import json
    from emuflow.transport.pin_artifacts_native import build_pin_artifacts, copy_pin_artifact
    from emuflow.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView, copy_chimew_bank_channel_report_storage, read_chimew_bank_channel_report
    from emuflow.transport.pin_plan_storage import read_pin_plan
    from emuflow.transport.position_hints_storage import read_position_hints
    from emuflow.transport.chimew.chimew_binding_storage import read_chimew_binding
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary).resolve()
        source = bank_input if isinstance(bank_input, BankChannelInputView) else stack.enter_context(
            write_chimew_bank_channel_storage(root / "input.json", bank_input))
        schedule_path = getattr(schedule, "manifest_path", None)
        if schedule_path is None:
            schedule_path = root / "schedule.json"
            from tests.schedule_store_fixture import write_tdm_schedule_storage
            write_tdm_schedule_storage(schedule_path, schedule)
        electrical_path = root / "electrical.json"
        electrical_path.write_text(json.dumps(electrical))
        qualification = options.get("qualification_document")
        qualification_path = None
        if qualification is not None:
            qualification_path = root / "qualification.json"
            qualification_path.write_text(json.dumps(qualification))
        supplied = options.get("bank_channel_report_document")
        supplied_path = None
        if supplied is not None:
            report = supplied if isinstance(supplied, BankChannelReportView) else stack.enter_context(
                write_chimew_bank_channel_report_storage(root / "supplied.json", supplied))
            supplied_path = report.manifest_path
        result = build_pin_artifacts(schedule_path, platform, source.manifest_path, electrical_path, root / "artifacts",
            qualification_path=qualification_path, bank_report_path=supplied_path,
            region_count=options.get("region_count", 31), executable=options.get("executable"))
        for role, kind, read, option in (
            ("position_hints", "position-hints-store-v1", read_position_hints, "position_hints_output_path"),
            ("pin_plan", "pin-plan-store-v1", read_pin_plan, "pin_plan_output_path"),
            ("electrical_binding", "chimew-binding-store-v1", read_chimew_binding, "electrical_binding_output_path")):
            view = result[role]
            output = options.get(option)
            if output is None:
                result[role] = view.materialize()
            else:
                copy_pin_artifact(view.manifest_path, kind, output)
                result[role] = read(output)
        report = result["bank_channel_report"]
        output = options.get("bank_channel_report_output_path")
        if output is None:
            with report:
                result["bank_channel_report"] = report.materialize()
        else:
            copy_chimew_bank_channel_report_storage(report.manifest_path, output)
            report.close()
            result["bank_channel_report"] = read_chimew_bank_channel_report(output)
        return result
