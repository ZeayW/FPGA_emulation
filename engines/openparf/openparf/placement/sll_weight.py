"""Evaluate the bounded SLL multiplier update with a fixed Adam step."""

import math


def bounded_sll_psi(psi, step_size, delta_sll, m_hat, v_hat, epsilon):
    """Apply the LEAPS fixed-step Adam update inside the native bounds.

    LEAPS Equation (23) uses ``t_psi`` as a fixed step size.  The previous
    implementation instead evaluated ``step_rate ** abs(delta_sll)``.  Since
    ``delta_sll`` is an absolute count, that made the update depend
    exponentially on design size and destabilized large placements.  The EMA
    and Adam moments already carry the magnitude and direction of the SLL
    change, so ``delta_sll`` is validated here but is not applied a second
    time.  Preserve OpenPARF's existing ``[0, 1000)`` acceptance policy.
    """

    psi, step_size, delta_sll, m_hat, v_hat, epsilon = map(
        float, (psi, step_size, delta_sll, m_hat, v_hat, epsilon)
    )
    if (
        not all(math.isfinite(x) for x in (psi, step_size, delta_sll, m_hat, v_hat, epsilon))
        or not 0 <= psi < 1000
        or step_size <= 0 or v_hat < 0 or epsilon <= 0
    ):
        raise FloatingPointError("invalid native SLL multiplier state")
    denominator = math.sqrt(v_hat) + epsilon
    candidate = psi + step_size * m_hat / denominator
    if not math.isfinite(candidate):
        return psi if m_hat > 0 else 0.0
    return max(0.0, candidate) if candidate < 1000 else psi
