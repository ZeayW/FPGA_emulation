CREATE TEMP VIEW {{PREFIX}}metadata AS SELECT * FROM {{SPLIT}}.metadata;
CREATE TEMP VIEW {{PREFIX}}parts AS SELECT * FROM {{SPLIT}}.parts;
CREATE TEMP VIEW {{PREFIX}}resources AS SELECT * FROM {{SPLIT}}.resources;
CREATE TEMP VIEW {{PREFIX}}net_segments AS SELECT * FROM {{SPLIT}}.net_segments;
CREATE TEMP VIEW {{PREFIX}}source_instances AS
 SELECT c.seq AS source_instance_seq,c.id AS original_id,c.name,c.flags,c.type,
        c.resources_seq,c.parameters_seq,c.attributes_seq,c.constants_seq,c.extras_seq
 FROM {{SOURCE}}.cell_records c
 WHERE EXISTS(SELECT 1 FROM {{ASSIGNMENT}}.copies cp WHERE cp.source_instance_seq=c.seq);
CREATE TEMP VIEW {{PREFIX}}instance_copies AS
 SELECT cp.part_index,cp.source_instance_seq,
        CASE WHEN cp.is_replica=1 THEN cp.copy_id ELSE NULL END AS copy_id,cp.is_replica
 FROM {{ASSIGNMENT}}.copies cp;
CREATE TEMP VIEW {{PREFIX}}source_nets AS
 SELECT n.seq AS source_net_seq,n.id AS original_net,n.name,n.flags,n.cut_class
 FROM {{SOURCE}}.net_records n
 WHERE EXISTS(SELECT 1 FROM {{SPLIT}}.net_segments s WHERE s.source_net_seq=n.seq);
CREATE TEMP VIEW {{PREFIX}}payloads AS SELECT * FROM {{SOURCE}}.payloads;
CREATE TEMP VIEW {{PREFIX}}pin_names AS
 SELECT * FROM {{SOURCE}}.pin_names
 UNION ALL
 SELECT CAST((SELECT value FROM {{SPLIT}}.metadata WHERE key='shadow_pin_seq') AS INTEGER),'shadow_out'
 WHERE NOT EXISTS(SELECT 1 FROM {{SOURCE}}.pin_names WHERE name='shadow_out');
CREATE TEMP VIEW {{PREFIX}}endpoint_segments AS
 SELECT cp.part_index,e.net_seq AS source_net_seq,e.role,e.ordinal,
        e.instance_seq AS source_instance_seq,NULL AS synthetic_instance_id,
        e.port_seq,e.bit,e.extras_seq,0 AS synthetic
 FROM {{SOURCE}}.endpoint_records e
 JOIN {{ASSIGNMENT}}.copies cp ON cp.source_instance_seq=e.instance_seq
 UNION ALL
 SELECT s.part_index,e.net_seq,e.role,e.ordinal,NULL,NULL,e.port_seq,e.bit,e.extras_seq,0
 FROM {{SPLIT}}.net_segments s
 JOIN {{SOURCE}}.endpoint_records e ON e.net_seq=s.source_net_seq
 WHERE e.instance_seq IS NULL
 UNION ALL
 SELECT s.part_index,s.source_net_seq,0,s.ordinal,NULL,'__emuflow_rx_'||e.entry_id,
        CAST((SELECT value FROM {{SPLIT}}.metadata WHERE key='shadow_pin_seq') AS INTEGER),0,
        CAST((SELECT value FROM {{SPLIT}}.metadata WHERE key='shadow_extras_seq') AS INTEGER),1
 FROM {{SPLIT}}.shadow_drivers s
 JOIN {{SCHEDULE}}.schedule_entries e ON e.seq=s.schedule_entry_seq;
CREATE TEMP VIEW {{PREFIX}}ports AS
 SELECT p.part_index,json_extract(r.record_json,'$.id') AS id,r.record_json
 FROM {{SPLIT}}.port_memberships p
 JOIN {{SOURCE}}.port_records r ON r.seq=p.source_port_seq;
CREATE TEMP VIEW {{PREFIX}}cut_sinks AS SELECT * FROM {{ASSIGNMENT}}.cut_sinks;
CREATE TEMP VIEW {{PREFIX}}exact_cut_sources AS SELECT '' AS net_id,0 AS ordinal,'' AS segment_id WHERE 0;
CREATE TEMP VIEW {{PREFIX}}exact_cut_nodes AS SELECT '' AS net_id WHERE 0;
CREATE TEMP VIEW {{PREFIX}}exact_logic_segments AS SELECT '' AS source_cut_net,0 AS part_index,'' AS segment_id WHERE 0;
