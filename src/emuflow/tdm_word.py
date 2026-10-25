"""Declared 8:1 LVDS word events shared by scheduling and timing consumers.

The fixed model arc starts at TX word load and ends at RX word visibility. It
already includes serialization and deserialization; serial bit position does
not create an earlier logic-visible arrival within a complete received word.
This model does not characterize a vendor PHY or establish board signoff.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .errors import ValidationError


WORD_EVENT_POLICY = "periodic-lvds-8bit-word-load-to-rx-visible-v2"


def word_transport_metadata(platform, schedule, *, pin_plan=None, physical_io=False):
    """Admit a declared word schedule to its logical transport boundary."""
    enabled = any(link.word_timing is not None for link in platform.links)
    expected = WORD_EVENT_POLICY if enabled else None
    if schedule.get("word_event_policy") != expected:
        raise ValidationError("transport word event policy differs from BoardDB")
    if enabled:
        if any(link.word_timing is None for link in platform.links):
            raise ValidationError("one transport cannot mix periodic word and one-shot link formats")
        if pin_plan is not None:
            raise ValidationError("periodic word transport requires word-group-aware pin planning")
        if physical_io:
            raise ValidationError("periodic word PHY binding is not yet qualified")
    return {"word_event_policy": expected} if enabled else {}


def word_endpoint_metadata(link, *, slot, arrival_slot, lane, ratio):
    timing = link.word_timing
    if timing is None:
        return {}
    words = timing.word_cycles_for_ratio(ratio)
    _integer(slot, "load index", minimum=1)
    _integer(arrival_slot, "capture index", minimum=1)
    timing.channel_position(lane, link.data_lanes_per_direction)
    if arrival_slot != slot + timing.fixed_latency_word_cycles:
        raise ValidationError("word endpoint arrival differs from its fixed capture latency")
    return {"tdm_ratio":ratio, "word_phase":slot % words}


def _number(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        raise ValidationError(f"TDM word {name} must be finite" + (" and positive" if positive else ""))
    return float(value)


def _integer(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"TDM word {name} is invalid")
    return value


@dataclass(frozen=True)
class WordTransfer:
    word_load_ns: float
    serial_tx_ns: float
    rx_visible_ns: float
    tdm_wait_ns: float
    delay_ns: float


@dataclass(frozen=True)
class LVDSWordTiming:
    word_period_ns: float
    fixed_delay_ns: float
    minimum_ratio: int = 8
    maximum_ratio: int = 256
    ratio_quantum: int = 8
    bits_per_word: int = 8

    def __post_init__(self):
        _number(self.word_period_ns, "period", positive=True)
        _number(self.fixed_delay_ns, "fixed delay", positive=True)
        _integer(self.bits_per_word, "bits per word", minimum=1)
        if self.bits_per_word != 8:
            raise ValidationError("LVDS word timing requires eight serial bits per word")
        for value, name in ((self.minimum_ratio, "minimum ratio"),
                            (self.maximum_ratio, "maximum ratio"),
                            (self.ratio_quantum, "ratio quantum")):
            _integer(value, name, minimum=1)
        if (self.minimum_ratio > self.maximum_ratio
                or self.ratio_quantum % self.bits_per_word
                or self.minimum_ratio % self.ratio_quantum
                or self.maximum_ratio % self.ratio_quantum):
            raise ValidationError("LVDS ratio domain must contain complete eight-bit words")
        if self.fixed_delay_ns < self.word_period_ns:
            raise ValidationError("LVDS fixed word-load-to-RX arc cannot omit whole-word transmission")

    @property
    def serial_ui_ns(self):
        return self.word_period_ns / self.bits_per_word

    @property
    def line_rate_gbps(self):
        return 1.0 / self.serial_ui_ns

    @property
    def fixed_latency_word_cycles(self):
        return math.ceil(self.fixed_delay_ns / self.word_period_ns)

    def word_cycles_for_ratio(self, ratio):
        _integer(ratio, "ratio", minimum=1)
        if not self.minimum_ratio <= ratio <= self.maximum_ratio or ratio % self.ratio_quantum:
            raise ValidationError("LVDS ratio lies outside the declared word-aligned domain")
        return ratio // self.bits_per_word

    def pre_schedule_delay_ns(self, ratio):
        self.word_cycles_for_ratio(ratio)
        return self.fixed_delay_ns + ratio * self.serial_ui_ns

    def channel_position(self, channel, physical_lanes):
        _integer(channel, "parallel channel")
        _integer(physical_lanes, "physical lane count", minimum=1)
        if channel >= physical_lanes * self.bits_per_word:
            raise ValidationError("TDM parallel channel exceeds physical lane word capacity")
        return divmod(channel, self.bits_per_word)

    def transfer(self, *, ready_ns, word_index, bit_in_word, ratio, word_epoch_ns=0.0):
        """Resolve one assigned event; a missed event never wraps to another round."""
        ready = _number(ready_ns, "readiness")
        epoch = _number(word_epoch_ns, "epoch")
        _integer(word_index, "index", minimum=1)
        _integer(bit_in_word, "bit position")
        if bit_in_word >= self.bits_per_word:
            raise ValidationError("TDM serial bit position exceeds its word")
        self.word_cycles_for_ratio(ratio)
        if word_index > (1 << 32) - 1:
            raise ValidationError("TDM word index exceeds the native scheduling domain")
        load = _number(epoch + word_index * self.word_period_ns, "load event")
        if ready > load:
            raise ValidationError("TDM source misses its assigned word-load event")
        wait = load - ready
        if wait > ratio * self.serial_ui_ns:
            raise ValidationError("TDM scheduled wait exceeds the declared ratio upper bound")
        visible = _number(load + self.fixed_delay_ns, "RX visibility event")
        return WordTransfer(load, load + bit_in_word * self.serial_ui_ns,
                            visible, wait, self.fixed_delay_ns + wait)

    def next_transfer(self, *, ready_ns, phase_word, bit_in_word, ratio, word_epoch_ns=0.0):
        """Select the first occurrence in an explicitly periodic lane table.

        This resolves the execution of a cyclic table. It does not reinterpret
        a missed one-shot transfer or change the assigned signal order.
        """
        words = self.word_cycles_for_ratio(ratio)
        _integer(phase_word, "phase word")
        if phase_word >= words:
            raise ValidationError("TDM word phase lies outside its lane's periodic table")
        ready = _number(ready_ns, "readiness")
        epoch = _number(word_epoch_ns, "epoch")
        first = epoch + phase_word * self.word_period_ns
        cycles = max(0, math.ceil((ready - first) / (words * self.word_period_ns)),
                     math.ceil((epoch + self.word_period_ns - first) / (words * self.word_period_ns)))
        return self.transfer(ready_ns=ready, word_index=phase_word + cycles * words,
                             bit_in_word=bit_in_word, ratio=ratio, word_epoch_ns=epoch)

    @classmethod
    def from_link(cls, link):
        if link.mode != "source_synchronous" or link.payload_bits_per_lane_per_cycle != 8:
            raise ValidationError("LVDS word timing requires an explicit source-synchronous 8-bit link")
        model = link.tdm_delay_model
        if model is None or link.max_line_rate_gbps_per_lane is None:
            raise ValidationError("LVDS word link requires an explicit ratio model and lane rate limit")
        result = cls(1000.0 / link.fabric_clock_mhz, model.intercept_ns,
                     model.min_ratio, model.max_ratio, model.ratio_quantum)
        if not math.isclose(model.ratio_coefficient_ns, result.serial_ui_ns, rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError("LVDS ratio coefficient must equal one serial UI, not one word period")
        if result.line_rate_gbps > link.max_line_rate_gbps_per_lane * (1.0 + 1e-12):
            raise ValidationError("LVDS word rate exceeds the declared physical lane limit")
        if not math.isclose(result.fixed_delay_ns, result.fixed_latency_word_cycles * result.word_period_ns, rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError("LVDS word capture requires an integral fixed word-cycle latency")
        if link.latency_cycles != result.fixed_latency_word_cycles:
            raise ValidationError("LVDS cycle latency must describe the fixed arc without a ratio estimate")
        return result
