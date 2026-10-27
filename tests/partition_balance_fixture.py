"""Bounded independent resource arithmetic for native partition qualification."""
import math
from emuflow.errors import ValidationError
from emuflow.partition import TRANSPORTED_CUT_CLASSES, summarize_balance_observation
from emuflow.partition_limits import planning_resource_capacities, strict_balance_limits
from emuflow.resources import RESOURCE_FIELDS
from tests.partition_views import ClusterColumns


def active_partition_fpgas(platform,constraints):
    ids=set(constraints.get('active_fpgas',(fpga.id for fpga in platform.fpgas)))
    selected=[fpga for fpga in platform.fpgas if fpga.id in ids]
    if not selected or len(selected)!=len(ids):raise ValidationError('balance active FPGA set is invalid')
    return selected


def transported_cut_classes_for_clusters(clusters):
    raw=clusters.get('policy',{}).get('transported_cut_classes',sorted(TRANSPORTED_CUT_CLASSES))
    if not isinstance(raw,list) or not all(isinstance(x,str) for x in raw) or not TRANSPORTED_CUT_CLASSES<=set(raw)<=TRANSPORTED_CUT_CLASSES|{'combinational'}:
        raise ValidationError('clusters.policy.transported_cut_classes is not a supported policy')
    return set(raw)


def _cluster_assignment_summary(clusters,owners,parts):
    if len(clusters)>65536:raise AssertionError('resource fixture exceeds bound')
    if len(clusters)!=len(owners):raise ValidationError('balance assignment cluster coverage changed')
    if isinstance(clusters,ClusterColumns):
        from tests.native_partition_core_fixture import reduce_native_partition_loads
        return reduce_native_partition_loads(clusters,owners,parts)
    dimensions=('cells',*RESOURCE_FIELDS)
    totals=dict.fromkeys(dimensions,0);loads={p:dict(totals) for p in parts};counts=dict.fromkeys(parts,0)
    for row in clusters:
        part=owners.get(row['id'])
        if part not in loads:raise ValidationError('balance assignment references unknown FPGA or cluster coverage changed')
        counts[part]+=1
        for key in dimensions:
            value=len(row['instances']) if key=='cells' else row['resources'].get(key,0)
            totals[key]+=value;loads[part][key]+=value
    return totals,loads,counts


def _balance_partition_fpgas(platform,active):return active_partition_fpgas(platform,{} if active is None else {'active_fpgas':active})


def _validate_balance_loads(fpgas,totals_all,loads_all,present,tolerance,overrides,requested,reservations):
    dimensions=['cells',*(key for key in RESOURCE_FIELDS if present[key] and all(fpga.effective_capacity.get(key,0)>0 for fpga in fpgas))]
    totals={key:totals_all[key] for key in dimensions};strict=set(dimensions) if requested is None else set(requested)&set(dimensions)
    percents={key:100.0*float((overrides or {}).get(key,tolerance)) for key in dimensions}
    limits=strict_balance_limits(fpgas,totals,tolerance,overrides or {},strict,reservations)
    planning=planning_resource_capacities(fpgas,reservations)
    loads={fpga.id:{key:loads_all[fpga.id][key] for key in dimensions} for fpga in fpgas}
    allowed={fpga.id:{key:limits[fpga.id][key] if totals[key] else 0.0 for key in dimensions} for fpga in fpgas}
    violations=[dict(fpga=fpga.id,dimension=key,load=loads[fpga.id][key],allowed=allowed[fpga.id][key]) for fpga in fpgas for key in dimensions if loads[fpga.id][key]>allowed[fpga.id][key]]
    if violations:raise ValidationError('assignment violates effective multi-resource balance/capacity: '+str(violations[:8]))
    ratios=[loads[p][key]/allowed[p][key] if allowed[p][key] else (math.inf if loads[p][key] else 0.0) for p in loads for key in dimensions]
    capacity=lambda fpga,table:{key:totals[key] if key=='cells' else table[key] for key in dimensions}
    return dict(requested_balance_percent=100.0*tolerance,effective_balance_percent=max(percents.values(),default=0.0),
        requested_balance_percent_by_dimension=percents,effective_balance_percent_by_dimension=dict(percents),balance_auto_relaxed=False,
        balance_dimensions=dimensions,strict_balance_dimensions=sorted(strict),capacity_only_dimensions=sorted(set(dimensions)-strict),
        constraint_mode_by_dimension={key:'balance' if key in strict else 'capacity' for key in dimensions},
        enforced_balance_percent_by_dimension={key:percents[key] for key in dimensions if key in strict},
        capacity_limits={fpga.id:capacity(fpga,planning[fpga.id]) for fpga in fpgas},
        physical_capacity_limits={fpga.id:capacity(fpga,fpga.effective_capacity) for fpga in fpgas},
        resource_reservations_by_fpga={fpga.id:dict(sorted((reservations or {}).get(fpga.id,{}).items())) for fpga in fpgas},
        balance_observation=summarize_balance_observation(loads,strict),balance_loads=loads,balance_allowed_loads=allowed,
        max_balance_limit_ratio=max(ratios,default=0.0),balance_violations=0,strict_balance_violations=0,capacity_violations=0)


def validate_cluster_assignment_balance(platform,clusters,cluster_assignment,requested_tolerance,
        requested_tolerance_by_dimension=None,active_fpgas=None,requested_balance_dimensions=(),resource_reservations_by_fpga=None):
    fpgas=_balance_partition_fpgas(platform,active_fpgas)
    totals,loads,_counts=_cluster_assignment_summary(clusters,cluster_assignment,[fpga.id for fpga in fpgas])
    return _validate_balance_loads(fpgas,totals,loads,{key:bool(totals[key]) for key in RESOURCE_FIELDS},requested_tolerance,
        requested_tolerance_by_dimension,requested_balance_dimensions,resource_reservations_by_fpga)
