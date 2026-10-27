INSERT INTO parts SELECT part_index,fpga FROM assignment.parts;
INSERT INTO net_segments
SELECT cp.part_index,e.net_seq,
       CASE WHEN cs.net_id IS NOT NULL THEN 'transport_shadow'
            WHEN max(CASE WHEN cp.is_replica=1 AND e.role=0 THEN 1 ELSE 0 END)=1
              THEN 'logic_replica' ELSE 'original' END
FROM src.endpoint_records e
JOIN assignment.copies cp ON cp.source_instance_seq=e.instance_seq
JOIN src.net_records n ON n.seq=e.net_seq
LEFT JOIN assignment.cut_sinks cs ON cs.net_id=n.id AND cs.sink_part_index=cp.part_index
GROUP BY cp.part_index,e.net_seq;
CREATE TEMP TABLE incoming(net_id TEXT NOT NULL,part_index INTEGER NOT NULL,
 entry_seq INTEGER NOT NULL,PRIMARY KEY(net_id,part_index)) WITHOUT ROWID;
INSERT INTO incoming
 SELECT e.net,p.part_index,e.seq FROM schedule.schedule_entries e
 JOIN parts p ON p.fpga=e.sink_fpga;
INSERT INTO shadow_drivers
SELECT n.part_index,n.source_net_seq,
       coalesce((SELECT max(e.ordinal)+1 FROM src.endpoint_records e
                 WHERE e.net_seq=n.source_net_seq AND e.role=0
                   AND (e.instance_seq IS NULL OR EXISTS(
                       SELECT 1 FROM assignment.copies cp
                       WHERE cp.source_instance_seq=e.instance_seq AND cp.part_index=n.part_index))),0),
       i.entry_seq
FROM net_segments n
JOIN src.net_records sn ON sn.seq=n.source_net_seq
JOIN assignment.cut_sinks cs ON cs.net_id=sn.id AND cs.sink_part_index=n.part_index
JOIN incoming i ON i.net_id=sn.id AND i.part_index=n.part_index;
INSERT INTO port_memberships
 SELECT DISTINCT s.part_index,p.seq FROM net_segments s
 JOIN src.endpoint_records e ON e.net_seq=s.source_net_seq AND e.instance_seq IS NULL
 JOIN src.pin_names pn ON pn.seq=e.port_seq
 JOIN src.port_records p ON json_extract(p.record_json,'$.id')=pn.name;
INSERT INTO resources
SELECT c.part_index,j.key,sum(CAST(j.value AS INTEGER))
FROM assignment.copies c JOIN src.cell_records i ON i.seq=c.source_instance_seq
JOIN src.payloads p ON p.seq=i.resources_seq,json_each(p.value) j
GROUP BY c.part_index,j.key HAVING sum(CAST(j.value AS INTEGER))!=0;
DROP TABLE incoming;
