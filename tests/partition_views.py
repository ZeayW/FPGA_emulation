"""Small read-only projections of current native partition fixture columns.

No clustering, partition search, semantic analysis or artifact publication is
implemented here. Raw inputs are bounded before the native fixture readers run.
"""
from array import array
from collections.abc import Mapping, Sequence, Set
import copy
import hashlib
import json
from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow.ir.resources import RESOURCE_FIELDS


LIMIT = 65536


class RecordView(list):
    def __iter__(self):return (self._record(i) for i in range(len(self)))
    def __getitem__(self,index):
        if isinstance(index,slice):return [self._record(i) for i in range(*index.indices(len(self)))]
        if type(index) is not int:raise TypeError('fixture record index must be an integer')
        index=index if index>=0 else len(self)+index
        if not 0<=index<len(self):raise IndexError(index)
        return self._record(index)
    def __eq__(self,other):return isinstance(other,Sequence) and list(self)==list(other)
    def __ne__(self,other):return not self==other
    def __deepcopy__(self,memo):return copy.deepcopy(list(self),memo)
    def __copy__(self):return list(self)
    def copy(self):return list(self)
    @staticmethod
    def _immutable(*_args):raise TypeError('fixture record columns are immutable')
    append=clear=extend=insert=pop=remove=reverse=sort=__setitem__=__delitem__=__iadd__=__imul__=_immutable


class ClusterColumns(RecordView):
    def __init__(self,*,name_bytes,name_offsets,member_offsets,member_ordinals,resource_columns,
                 fixed_by_cluster,groups_by_cluster,source_cluster_indices):
        count=len(member_offsets)-1
        if (not 0<=count<=LIMIT or len(source_cluster_indices)>LIMIT or len(name_offsets)!=len(source_cluster_indices)+1
                or len(member_ordinals)!=len(source_cluster_indices) or len(resource_columns)!=len(RESOURCE_FIELDS)
                or any(len(c)!=count for c in resource_columns) or member_offsets[0]!=0 or member_offsets[-1]!=len(member_ordinals)):
            raise ValidationError('fixture cluster column dimensions disagree')
        self._name_bytes,self._name_offsets=name_bytes,name_offsets
        self._member_offsets,self._member_ordinals=member_offsets,member_ordinals
        self._resource_columns=resource_columns;self.source_cluster_indices=source_cluster_indices
        self._fixed_by_cluster=dict(fixed_by_cluster);self._groups_by_cluster={i:tuple(v) for i,v in groups_by_cluster.items()}
    def __len__(self):return len(self._member_offsets)-1
    def _record(self,i):
        members=self._member_ordinals[self._member_offsets[i]:self._member_offsets[i+1]]
        return dict(id=self.cluster_id_at(i),instances=[bytes(self._name_bytes[self._name_offsets[s]:self._name_offsets[s+1]]).decode() for s in members],
            resources={key:column[i] for key,column in zip(RESOURCE_FIELDS,self._resource_columns) if column[i]},
            fixed_fpga=self._fixed_by_cluster.get(i),groups=list(self._groups_by_cluster.get(i,())))
    def cluster_id_at(self,i):
        if not 0<=i<len(self):raise IndexError(i)
        return f'c{i:06d}'
    def cluster_member_count(self,i):
        self.cluster_id_at(i);return self._member_offsets[i+1]-self._member_offsets[i]
    @property
    def packed_payload_bytes(self):return len(self._name_bytes)+sum(len(c)*c.itemsize for c in (self._name_offsets,self._member_offsets,self._member_ordinals,self.source_cluster_indices,*self._resource_columns))


class SelectedCuts(RecordView):
    def __init__(self,cuts,nodes):
        if len(cuts)!=len(nodes) or any(cuts.net_at(i)!=nodes[i]['net'] for i in range(len(cuts))):
            raise ValidationError('fixture exact cut coverage/order disagrees')
        self._cuts,self._nodes=cuts,nodes
    def __len__(self):return len(self._cuts)
    def _record(self,i):
        result=dict(self._cuts[i]);result.pop('transport_round',None)
        result.update({key:self._nodes[i][key] for key in ('dependency_level','combinational_dependency_depth','predecessor_cut_nets')})
        return result
    @property
    def packed_payload_bytes(self):return self._cuts.packed_payload_bytes


class PartTranslation(Sequence):
    itemsize=4
    readonly=True
    def __init__(self,source,translation):self.source,self.translation=source,translation
    def __len__(self):return len(self.source)
    def __getitem__(self,i):
        if isinstance(i,slice):return array('I',(self[j] for j in range(*i.indices(len(self)))))
        return self.translation[self.source[i]]


class ClusterOwners(Mapping):
    def __init__(self,part_ids,part_indices):
        self.part_ids=tuple(part_ids);self.part_indices=array('I',part_indices)
        if (not self.part_ids or len(set(self.part_ids))!=len(self.part_ids) or len(self.part_indices)>LIMIT
                or any(not isinstance(p,str) or not p for p in self.part_ids)
                or any(i>=len(self.part_ids) for i in self.part_indices)):raise ValidationError('fixture cluster ownership is invalid')
    @staticmethod
    def identity(i):return f'c{i:06d}'
    def __len__(self):return len(self.part_indices)
    def __iter__(self):return (self.identity(i) for i in range(len(self)))
    def __getitem__(self,key):
        try:i=int(key[1:])
        except (ValueError,TypeError,IndexError):raise KeyError(key)
        if key!=self.identity(i) or not 0<=i<len(self):raise KeyError(key)
        return self.part_ids[self.part_indices[i]]
    def part_for_index(self,i):
        if type(i) is not int or not 0<=i<len(self):raise IndexError(i)
        return self.part_ids[self.part_indices[i]]
    def __eq__(self,other):
        if isinstance(other,ClusterOwners) and self.part_ids==other.part_ids and self.part_indices is other.part_indices:return True
        return isinstance(other,Mapping) and len(self)==len(other) and all(other.get(key)==self[key] for key in self)
    def __ne__(self,other):return not self==other
    @property
    def packed_payload_bytes(self):return 4*len(self)


class InstanceOwners(Mapping):
    def __init__(self,path,clusters,assignment,*,_known_used_parts=None):
        require_coverage(clusters,assignment)
        self.path=Path(path);self._native_publication=True
        self._native_primary_artifact=clusters.native_artifact
        self._native_primary_generation,self._native_source_generation=clusters.native_generation,clusters.native_source_generation
        if self._native_primary_artifact is None:raise ValidationError('native assignment primary ownership is missing')
        self._cluster_member_offsets=clusters._member_offsets;self._source_clusters=clusters.source_cluster_indices
        used=set(assignment.values()) if _known_used_parts is None else set(_known_used_parts)
        self.part_ids=tuple(sorted(used))
        if isinstance(assignment,ClusterOwners) and self.part_ids==assignment.part_ids:self._cluster_parts=assignment.part_indices
        elif isinstance(assignment,ClusterOwners):
            self._cluster_parts=PartTranslation(assignment.part_indices,{i:self.part_ids.index(p) for i,p in enumerate(assignment.part_ids) if p in used})
        else:self._cluster_parts=array('I',(self.part_ids.index(assignment[f'c{i:06d}']) for i in range(len(clusters))))
        self._original_cluster_assignment=assignment;self._original_cluster_parts=self._cluster_parts;self._original_part_ids=self.part_ids
    def __len__(self):return len(self._source_clusters)
    def iter_source_assignments(self,*,order_by_id=False):
        from emuflow.ir.emuir_storage import open_emuir_sqlite_read_only
        db=open_emuir_sqlite_read_only(self.path)
        try:
            for seq,name in db.execute('SELECT seq,id FROM cell_records ORDER BY '+('id' if order_by_id else 'seq')):yield seq,name,self.part_for_source_seq(seq)
        finally:db.close()
    def __iter__(self):return (name for _,name,_ in self.iter_source_assignments(order_by_id=True))
    def __getitem__(self,key):
        from emuflow.ir.emuir_storage import open_emuir_sqlite_read_only
        db=open_emuir_sqlite_read_only(self.path)
        try:row=db.execute('SELECT seq FROM cell_records WHERE id=?',(key,)).fetchone()
        finally:db.close()
        if row is None:raise KeyError(key)
        return self.part_for_source_seq(row[0])
    def part_for_source_seq(self,seq):
        if type(seq) is not int or not 0<=seq<len(self):raise ValidationError('fixture source sequence is invalid')
        return self.cluster_part(self._source_clusters[seq])
    def cluster_part(self,i):return self.part_ids[self._cluster_parts[i]]
    def cluster_member_count(self,i):return self._cluster_member_offsets[i+1]-self._cluster_member_offsets[i]
    def canonical_sha256(self):
        payload={'instance_assignment':dict(self)}
        return hashlib.sha256((json.dumps(payload,indent=2,sort_keys=True)+'\n').encode()).hexdigest()


class CandidateColumns(Set):
    def __len__(self):return len(self._name_offsets)-1
    def name_at(self,i):
        if i<0:i+=len(self)
        if not 0<=i<len(self):raise IndexError(i)
        return bytes(self._name_bytes[self._name_offsets[i]:self._name_offsets[i+1]]).decode()
    def __iter__(self):return (self.name_at(i) for i in range(len(self)))
    def __contains__(self,key):
        if not isinstance(key,str):return False
        lo,hi=0,len(self)
        while lo<hi:
            mid=(lo+hi)//2
            if self.name_at(mid)<key:lo=mid+1
            else:hi=mid
        return lo<len(self) and self.name_at(lo)==key
    def contains_ordinal(self,i):return type(i) is int and 0<=i<self._net_count and bool(self._ordinal_bits[i>>3]&(1<<(i&7)))
    @property
    def packed_bytes(self):return len(self._ordinal_bits)+len(self._name_bytes)+8*len(self._name_offsets)
    def ordered_names(self):return list(self)
    def streaming_ordered_names(self):return CandidateNames(self)


class CandidateNames(RecordView):
    def __init__(self,source):self._source=source;self._materialized=False
    def __len__(self):return list.__len__(self) if self._materialized else len(self._source)
    def _record(self,i):return list.__getitem__(self,i) if self._materialized else self._source.name_at(i)
    def append(self,value):
        if not self._materialized:list.extend(self,self._source);self._materialized=True
        list.append(self,value)


def require_coverage(clusters,owners):
    if len(owners)!=len(clusters):raise ValidationError('cluster assignment exact coverage failed')
    if isinstance(clusters,ClusterColumns) and isinstance(owners,ClusterOwners):return
    keys=(f'c{i:06d}' for i in range(len(clusters))) if isinstance(clusters,ClusterColumns) else (row['id'] for row in clusters)
    if any(key not in owners for key in keys):raise ValidationError('cluster assignment exact coverage failed')
