"""Evaluate the existing bounded SLL multiplier update without power overflow."""

import math


def bounded_sll_psi(psi, step_rate, delta_sll, m_hat, v_hat, epsilon):
    """Preserve the native [0,1000) acceptance rule, including huge SLL deltas.

    Ordinary updates retain the original arithmetic. If the power itself
    overflows, compare the signed update in log space against the existing
    bounds before evaluating it. Rejecting an oversize positive candidate or
    clamping a negative one to zero does not require representing that power.
    """

    psi, step_rate, delta_sll, m_hat, v_hat, epsilon = map(
        float, (psi, step_rate, delta_sll, m_hat, v_hat, epsilon)
    )
    if (
        not all(math.isfinite(x) for x in (psi, step_rate, delta_sll, m_hat, v_hat, epsilon))
        or not 0 <= psi < 1000
        or step_rate <= 0 or v_hat < 0 or epsilon <= 0
    ):
        raise FloatingPointError("invalid native SLL multiplier state")
    denominator = math.sqrt(v_hat) + epsilon
    try:
        candidate = psi + math.pow(step_rate, abs(delta_sll)) * m_hat / denominator
    except OverflowError:
        if m_hat == 0:
            return psi
        magnitude = abs(delta_sll) * math.log(step_rate) + math.log(abs(m_hat)) - math.log(denominator)
        remaining = 1000 - psi if m_hat > 0 else psi
        if remaining <= 0 or magnitude >= math.log(remaining):
            return psi if m_hat > 0 else 0.0
        candidate = psi + math.copysign(math.exp(magnitude), m_hat)
    return max(0.0, candidate) if candidate < 1000 else psi
