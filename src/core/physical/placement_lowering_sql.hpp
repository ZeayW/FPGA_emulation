#pragma once
namespace emuflow::core {
inline constexpr const char* kLoweringStageDDL = R"LOWERING_SQL(CREATE TEMP TABLE stage_cells(
              id TEXT PRIMARY KEY,
              name TEXT,
              type TEXT NOT NULL,
              resources_json TEXT NOT NULL,
              parameters_json TEXT,
              attributes_json TEXT,
              constants_json TEXT NOT NULL,
              extras_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TEMP TABLE stage_nets(
              id TEXT PRIMARY KEY,
              name TEXT,
              yosys_bit INTEGER,
              bus_index INTEGER,
              fanout INTEGER,
              cut_class TEXT NOT NULL,
              extras_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TEMP TABLE stage_endpoints(
              net_id TEXT NOT NULL,
              role INTEGER NOT NULL,
              order_group INTEGER NOT NULL,
              order_a INTEGER NOT NULL,
              order_b INTEGER NOT NULL,
              order_c INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );
            CREATE INDEX stage_endpoint_order ON stage_endpoints(
              net_id,role,order_group,order_a,order_b,order_c
            );
            CREATE TEMP TABLE stage_aliases(
              net_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              alias TEXT NOT NULL,
              PRIMARY KEY(net_id,ordinal)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE requested_nets(
              kind INTEGER NOT NULL,
              net_id TEXT NOT NULL,
              PRIMARY KEY(kind,net_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE expected_rx(
              net_id TEXT NOT NULL,
              instance_id TEXT NOT NULL,
              PRIMARY KEY(net_id,instance_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE fanout_recompute(
              net_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TEMP TABLE port_additions(
              driver_port TEXT NOT NULL,
              driver_bit INTEGER NOT NULL,
              overlay_order INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );)LOWERING_SQL";
inline constexpr const char* kLoweringDutCells = R"LOWERING_SQL(INSERT INTO stage_cells
            SELECT COALESCE(c.copy_id,i.original_id),
                   CASE WHEN i.flags & 1 THEN i.original_id ELSE i.name END,
                   i.type,r.value,p.value,a.value,k.value,
                   CASE WHEN c.is_replica=1 THEN
                     json_set(x.value,'$.replica_of',i.original_id,
                              '$.replica_target_fpga',?)
                   ELSE x.value END
            FROM split_instance_copies c
            JOIN split_source_instances i
              ON i.source_instance_seq=c.source_instance_seq
            JOIN split_payloads r ON r.seq=i.resources_seq
            LEFT JOIN split_payloads p ON p.seq=i.parameters_seq
            LEFT JOIN split_payloads a ON a.seq=i.attributes_seq
            JOIN split_payloads k ON k.seq=i.constants_seq
            JOIN split_payloads x ON x.seq=i.extras_seq
            WHERE c.part_index=?)LOWERING_SQL";
inline constexpr const char* kLoweringTransportCells = R"LOWERING_SQL(INSERT INTO stage_cells
            SELECT ? || c.id,? || c.id,c.type,r.value,p.value,a.value,
                   k.value,x.value
            FROM transport_ir.cell_records c
            JOIN transport_ir.payloads r ON r.seq=c.resources_seq
            LEFT JOIN transport_ir.payloads p ON p.seq=c.parameters_seq
            LEFT JOIN transport_ir.payloads a ON a.seq=c.attributes_seq
            JOIN transport_ir.payloads k ON k.seq=c.constants_seq
            JOIN transport_ir.payloads x ON x.seq=c.extras_seq
            ORDER BY c.seq)LOWERING_SQL";
inline constexpr const char* kLoweringBaseSinks = R"LOWERING_SQL(CREATE TEMP TABLE base_sink_counts AS
            SELECT source_net_seq,count(*) AS fanout
            FROM split_endpoint_segments
            WHERE part_index=? AND role=1
            GROUP BY source_net_seq)LOWERING_SQL";
inline constexpr const char* kLoweringBaseSinkIndex = R"LOWERING_SQL(CREATE UNIQUE INDEX base_sink_identity ON base_sink_counts(source_net_seq))LOWERING_SQL";
inline constexpr const char* kLoweringExactTable = R"LOWERING_SQL(CREATE TEMP TABLE exact_info(
              net_id TEXT PRIMARY KEY,
              role TEXT NOT NULL
            ) WITHOUT ROWID)LOWERING_SQL";
inline constexpr const char* kLoweringExactNets = R"LOWERING_SQL(INSERT INTO exact_info(net_id,role)
                SELECT DISTINCT cs.net_id,
                  CASE
                    WHEN s.source_kind='transport_shadow'
                      THEN 'transport_shadow_sink'
                    WHEN cs.source_part_index=? THEN 'cut_source'
                    ELSE 'local_logic'
                  END
                FROM split_cut_sinks cs
                JOIN split_source_nets n ON n.original_net=cs.net_id
                JOIN split_net_segments s
                  ON s.source_net_seq=n.source_net_seq AND s.part_index=?)LOWERING_SQL";
inline constexpr const char* kLoweringDutNets = R"LOWERING_SQL(INSERT INTO stage_nets
            SELECT n.original_net,
                   CASE WHEN n.flags & 1 THEN n.original_net ELSE n.name END,
                   NULL,0,COALESCE(b.fanout,0),n.cut_class,
                   CASE WHEN e.net_id IS NULL THEN
                     json_object('original_net',n.original_net,
                                 'source_kind',s.source_kind)
                   ELSE
                     json_set(
                       json_object('original_net',n.original_net,
                                   'source_kind',s.source_kind),
                       '$.static_exact_boundary',
                       json_object(
                         'semantic_contract_sha256',?,
                         'role',e.role
                       )
                     )
                   END
            FROM split_net_segments s
            JOIN split_source_nets n
              ON n.source_net_seq=s.source_net_seq
            LEFT JOIN base_sink_counts b
              ON b.source_net_seq=s.source_net_seq
            LEFT JOIN exact_info e ON e.net_id=n.original_net
            WHERE s.part_index=?)LOWERING_SQL";
inline constexpr const char* kLoweringDutEndpoints = R"LOWERING_SQL(WITH resolved AS (
              SELECT n.original_net,e.role,e.synthetic,e.ordinal,pn.name AS port,
                     e.bit,x.value AS extras_json,
                     CASE
                       WHEN e.synthetic=1 THEN e.synthetic_instance_id
                       WHEN e.source_instance_seq IS NULL THEN NULL
                       ELSE COALESCE(c.copy_id,i.original_id)
                     END AS instance_id
              FROM split_endpoint_segments e
              JOIN split_source_nets n
                ON n.source_net_seq=e.source_net_seq
              JOIN split_pin_names pn ON pn.seq=e.port_seq
              JOIN split_payloads x ON x.seq=e.extras_seq
              LEFT JOIN split_instance_copies c
                ON c.part_index=e.part_index
               AND c.source_instance_seq=e.source_instance_seq
              LEFT JOIN split_source_instances i
                ON i.source_instance_seq=e.source_instance_seq
              WHERE e.part_index=?
            )
            INSERT INTO stage_endpoints
            SELECT original_net,role,0,synthetic,
                   CASE WHEN instance_id IS NULL THEN 1 ELSE 0 END,
                   ordinal,instance_id,port,bit,extras_json
            FROM resolved
            WHERE role<>0 OR instance_id IS NULL
               OR instance_id NOT LIKE '__emuflow_rx_%')LOWERING_SQL";
inline constexpr const char* kLoweringRequestedRx = R"LOWERING_SQL(INSERT OR IGNORE INTO requested_nets SELECT 1,net_id FROM expected_rx)LOWERING_SQL";
inline constexpr const char* kLoweringMissingRequested = R"LOWERING_SQL(SELECT r.kind,r.net_id FROM requested_nets r
            LEFT JOIN stage_nets n ON n.id=r.net_id
            WHERE n.id IS NULL ORDER BY r.kind,r.net_id LIMIT 1)LOWERING_SQL";
inline constexpr const char* kLoweringRxMismatch = R"LOWERING_SQL(WITH actual AS (
              SELECT n.original_net AS net_id,e.synthetic_instance_id AS instance_id
              FROM split_endpoint_segments e
              JOIN split_source_nets n
                ON n.source_net_seq=e.source_net_seq
              WHERE e.part_index=? AND e.role=0 AND e.synthetic=1
                AND e.synthetic_instance_id LIKE '__emuflow_rx_%'
            ), mismatch AS (
              SELECT 'unexpected' AS kind,a.net_id,a.instance_id FROM actual a
              LEFT JOIN expected_rx x
                ON x.net_id=a.net_id AND x.instance_id=a.instance_id
              WHERE x.net_id IS NULL
              UNION ALL
              SELECT 'missing',x.net_id,x.instance_id FROM expected_rx x
              LEFT JOIN actual a
                ON a.net_id=x.net_id AND a.instance_id=x.instance_id
              WHERE a.net_id IS NULL
            ) SELECT kind,net_id,instance_id FROM mismatch LIMIT 1)LOWERING_SQL";
inline constexpr const char* kLoweringPortEndpoints = R"LOWERING_SQL(INSERT INTO stage_endpoints
            SELECT n.original_net,1,1,1,0,a.overlay_order,
                   a.instance_id,a.port,a.bit,a.extras_json
            FROM port_additions a
            JOIN split_pin_names pn ON pn.name=a.driver_port
            JOIN split_endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            JOIN split_source_nets n
              ON n.source_net_seq=e.source_net_seq)LOWERING_SQL";
inline constexpr const char* kLoweringPortFanout = R"LOWERING_SQL(INSERT OR IGNORE INTO fanout_recompute(net_id)
            SELECT DISTINCT n.original_net
            FROM port_additions a
            JOIN split_pin_names pn ON pn.name=a.driver_port
            JOIN split_endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            JOIN split_source_nets n
              ON n.source_net_seq=e.source_net_seq)LOWERING_SQL";
inline constexpr const char* kLoweringUnmatchedPort = R"LOWERING_SQL(SELECT a.driver_port,a.driver_bit,count(e.source_net_seq)
            FROM port_additions a
            LEFT JOIN split_pin_names pn ON pn.name=a.driver_port
            LEFT JOIN split_endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            GROUP BY a.driver_port,a.driver_bit
            HAVING count(e.source_net_seq)<>1 LIMIT 1)LOWERING_SQL";
inline constexpr const char* kLoweringTransportNets = R"LOWERING_SQL(INSERT INTO stage_nets
            SELECT ? || n.id,
                   ? || CASE WHEN n.flags & 1 THEN n.id ELSE n.name END,
                   n.yosys_bit,n.bus_index,
                   (SELECT count(*) FROM transport_ir.endpoint_records e
                    JOIN transport_ir.pin_names p ON p.seq=e.port_seq
                    WHERE e.net_seq=n.seq AND e.role=1
                      AND NOT (e.instance_seq IS NULL AND
                               p.name IN ('source_values','shadow_values'))),
                   n.cut_class,x.value
            FROM transport_ir.net_records n
            JOIN transport_ir.payloads x ON x.seq=n.extras_seq
            LEFT JOIN consumed_transport_nets c ON c.id=n.id
            WHERE c.id IS NULL ORDER BY n.seq)LOWERING_SQL";
inline constexpr const char* kLoweringTransportAliases = R"LOWERING_SQL(INSERT INTO stage_aliases
            SELECT ? || n.id,a.ordinal,a.alias
            FROM transport_ir.alias_records a
            JOIN transport_ir.net_records n ON n.seq=a.net_seq
            LEFT JOIN consumed_transport_nets c ON c.id=n.id
            WHERE c.id IS NULL ORDER BY n.seq,a.ordinal)LOWERING_SQL";
inline constexpr const char* kLoweringTransportEndpoints = R"LOWERING_SQL(INSERT INTO stage_endpoints
            SELECT ? || n.id,e.role,0,0,0,e.ordinal,
                   CASE WHEN cell.id IS NULL THEN NULL ELSE ? || cell.id END,
                   pin.name,e.bit,x.value
            FROM transport_ir.endpoint_records e
            JOIN transport_ir.net_records n ON n.seq=e.net_seq
            LEFT JOIN transport_ir.cell_records cell ON cell.seq=e.instance_seq
            JOIN transport_ir.pin_names pin ON pin.seq=e.port_seq
            JOIN transport_ir.payloads x ON x.seq=e.extras_seq
            LEFT JOIN consumed_transport_nets c ON c.id=n.id
            WHERE c.id IS NULL
              AND NOT (e.instance_seq IS NULL AND
                       pin.name IN ('source_values','shadow_values'))
            ORDER BY n.seq,e.role,e.ordinal)LOWERING_SQL";
inline constexpr const char* kLoweringFanout = R"LOWERING_SQL(UPDATE stage_nets SET fanout=(
              SELECT count(*) FROM stage_endpoints e
              WHERE e.net_id=stage_nets.id AND e.role=1
            ) WHERE id IN (SELECT net_id FROM fanout_recompute))LOWERING_SQL";
inline constexpr const char* kLoweringMissingCell = R"LOWERING_SQL(SELECT e.net_id,e.instance_id FROM stage_endpoints e
            LEFT JOIN stage_cells c ON c.id=e.instance_id
            WHERE e.instance_id IS NOT NULL AND c.id IS NULL
            LIMIT 1)LOWERING_SQL";
inline constexpr const char* kLoweringMissingNet = R"LOWERING_SQL(SELECT e.net_id FROM stage_endpoints e
            LEFT JOIN stage_nets n ON n.id=e.net_id
            WHERE n.id IS NULL LIMIT 1)LOWERING_SQL";
inline constexpr const char* kLoweringIntern = R"LOWERING_SQL(INSERT OR IGNORE INTO payloads(value)
  SELECT resources_json FROM stage_cells;
INSERT OR IGNORE INTO payloads(value)
  SELECT parameters_json FROM stage_cells
  WHERE parameters_json IS NOT NULL;
INSERT OR IGNORE INTO payloads(value)
  SELECT attributes_json FROM stage_cells
  WHERE attributes_json IS NOT NULL;
INSERT OR IGNORE INTO payloads(value)
  SELECT constants_json FROM stage_cells;
INSERT OR IGNORE INTO payloads(value)
  SELECT extras_json FROM stage_cells;
INSERT OR IGNORE INTO payloads(value)
  SELECT extras_json FROM stage_nets;
INSERT OR IGNORE INTO payloads(value)
  SELECT extras_json FROM stage_endpoints;

INSERT INTO cell_records(
  id,name,flags,type,resources_seq,parameters_seq,attributes_seq,
  constants_seq,extras_seq
)
SELECT c.id,
       CASE WHEN c.name=c.id THEN NULL ELSE c.name END,
       CASE WHEN c.name=c.id THEN 1 ELSE 0 END,
       c.type,r.seq,p.seq,a.seq,k.seq,x.seq
FROM stage_cells c
JOIN payloads r ON r.value=c.resources_json
LEFT JOIN payloads p ON p.value=c.parameters_json
LEFT JOIN payloads a ON a.value=c.attributes_json
JOIN payloads k ON k.value=c.constants_json
JOIN payloads x ON x.value=c.extras_json
ORDER BY c.id;

INSERT INTO net_records(
  id,name,flags,yosys_bit,bus_index,fanout,cut_class,extras_seq
)
SELECT n.id,
       CASE WHEN n.name=n.id THEN NULL ELSE n.name END,
       CASE WHEN n.name=n.id THEN 1 ELSE 0 END,
       n.yosys_bit,n.bus_index,n.fanout,n.cut_class,x.seq
FROM stage_nets n JOIN payloads x ON x.value=n.extras_json
ORDER BY n.id;

INSERT INTO pin_names(name)
  SELECT DISTINCT port FROM stage_endpoints ORDER BY port;

INSERT INTO endpoint_records(
  net_seq,role,ordinal,instance_seq,port_seq,bit,extras_seq
)
SELECT net.seq,e.role,
       row_number() OVER (
         PARTITION BY e.net_id,e.role
         ORDER BY e.order_group,e.order_a,e.order_b,e.order_c
       )-1,
       cell.seq,pin.seq,e.bit,x.seq
FROM stage_endpoints e
JOIN net_records net ON net.id=e.net_id
LEFT JOIN cell_records cell ON cell.id=e.instance_id
JOIN pin_names pin ON pin.name=e.port
JOIN payloads x ON x.value=e.extras_json
ORDER BY net.seq,e.role,e.order_group,e.order_a,e.order_b,e.order_c;

INSERT INTO alias_records(net_seq,ordinal,alias)
SELECT n.seq,a.ordinal,a.alias FROM stage_aliases a
JOIN net_records n ON n.id=a.net_id
ORDER BY n.seq,a.ordinal;)LOWERING_SQL";
inline constexpr const char* kLoweringEndpointIndex = R"LOWERING_SQL(CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit))LOWERING_SQL";
}
