"""Clock-edge windows from engine cycle accounting, independent of path delay.

The engine owns these facts. Uniform clock scaling changes the edge window;
setup, uncertainty and physical clock delays require their own evidence.
This module does not calculate or certify an operating frequency.
"""
from dataclasses import dataclass
import math

from .errors import ValidationError


@dataclass(frozen=True)
class ClockEdgeBudget:
    source_clock: str
    target_clock: str
    source_period_ns: float
    target_period_ns: float
    launch_edge_ns: float
    capture_edge_ns: float
    source_offset_ns: float
    target_cycle_time_ns: float
    multicycle_adjustment_ns: float

    def __post_init__(self):
        for name in ('source_clock','target_clock'):
            value=getattr(self,name)
            if not isinstance(value,str)or not value or value=='NULL':
                raise ValidationError('clock edge budget lacks an explicit '+name)
        for name in self.__dataclass_fields__:
            if name.endswith('_ns'):
                value=getattr(self,name)
                if isinstance(value,bool)or not isinstance(value,(int,float))or not math.isfinite(value):
                    raise ValidationError('invalid clock edge budget '+name)
        if min(self.source_period_ns,self.target_period_ns)<=0 or self.edge_window_ns<=0:
            raise ValidationError('clock edge budget requires positive periods and setup window')

    @property
    def edge_window_ns(self):
        return self.target_cycle_time_ns+self.multicycle_adjustment_ns-self.launch_edge_ns

    @property
    def target_period_coefficient(self):
        return self.edge_window_ns/self.target_period_ns

    def launch_relative_arrival_ns(self,arrival_ns):
        if isinstance(arrival_ns,bool)or not isinstance(arrival_ns,(int,float))or not math.isfinite(arrival_ns):
            raise ValidationError('invalid engine arrival time')
        return arrival_ns-self.source_offset_ns-self.launch_edge_ns

    def scaled_edge_window_ns(self,*,source_period_ns,target_period_ns):
        for value in (source_period_ns,target_period_ns):
            if isinstance(value,bool)or not isinstance(value,(int,float))or not math.isfinite(value)or value<=0:
                raise ValidationError('clock scaling requires finite positive periods')
        source_scale=source_period_ns/self.source_period_ns
        target_scale=target_period_ns/self.target_period_ns
        if not math.isclose(source_scale,target_scale,rel_tol=1e-7,abs_tol=1e-12):
            raise ValidationError('clock edge budget requires uniform source/capture clock scaling')
        return self.edge_window_ns*target_scale


def clock_edge_budget_from_engine_fields(fields):
    """Read the bounded Tcl qualifier's already unit-converted engine fields."""
    for flag in ('path_delay','latch_check'):
        value=fields.get(flag)
        if value not in ('0','1',0,1):raise ValidationError('missing or invalid engine check classification')
        if value in ('1',1):raise ValidationError('absolute path-delay and latch checks require a separate budget model')
    values={}
    try:
        for name in ClockEdgeBudget.__dataclass_fields__:
            value=fields[name]
            if isinstance(value,bool):raise ValueError('Boolean clock fact')
            values[name]=float(value)if name.endswith('_ns')else value
    except (KeyError,TypeError,ValueError)as error:
        raise ValidationError('invalid or incomplete engine clock budget')from error
    return ClockEdgeBudget(**values)
