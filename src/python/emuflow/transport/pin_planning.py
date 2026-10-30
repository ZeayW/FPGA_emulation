"""Bounded entry points for native Chimew pin-plan validation."""
from emuflow.common.errors import ValidationError
from emuflow.transport.pin_artifacts_native import validate_pin_artifacts

SIGNAL_POSITION_HINTS_SCHEMA = "emuflow.signal-position-hints/v1"
PIN_PLAN_SCHEMA = "emuflow.placement-aware-pin-plan/v1"
CHIMEW_PIN_PLAN_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHYSICAL_POSITION_PROVIDER = "chimew-physical-site-projection-v1"


def validate_pin_plan(schedule, platform, positions, plan):
    paths = [getattr(value, "manifest_path", None) for value in (schedule, positions, plan)]
    if any(path is None for path in paths):
        raise ValidationError("pin-plan validation requires current typed artifact owners")
    return validate_pin_artifacts(paths[0], platform, paths[2], positions_path=paths[1])["pin_validation"]
