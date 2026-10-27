from __future__ import annotations

class EmuFlowError(Exception):
    """Base class for actionable user-facing flow errors."""


class ValidationError(EmuFlowError):
    """Raised when a versioned artifact violates its schema invariants."""


class TDMScheduleInfeasibleError(ValidationError):
    """Raised when fixed frame/ratio constraints have no concrete schedule."""


class ImportError(EmuFlowError):
    """Raised when an external tool artifact cannot be imported."""
