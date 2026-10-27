CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT NOT NULL UNIQUE);
CREATE TABLE hops(
 entry_id TEXT PRIMARY KEY,demand TEXT NOT NULL,net TEXT NOT NULL,link_id TEXT NOT NULL,
 source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL,slot INTEGER NOT NULL,
 arrival_slot INTEGER NOT NULL,lane INTEGER NOT NULL,logical_lane INTEGER NOT NULL,
 route_source TEXT NOT NULL,tdm_ratio INTEGER,
 CHECK(source_fpga!=sink_fpga),CHECK(slot>=0 AND arrival_slot>=slot),CHECK(lane>=0 AND logical_lane>=0)
) WITHOUT ROWID;
CREATE INDEX hops_lane ON hops(link_id,slot,lane);
CREATE INDEX hops_source ON hops(source_fpga,entry_id);
CREATE INDEX hops_sink ON hops(sink_fpga,entry_id);
CREATE INDEX hops_demand ON hops(demand,link_id,source_fpga,sink_fpga);
CREATE TABLE signals(fpga TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('source','shadow')),
 seq INTEGER NOT NULL,signal TEXT NOT NULL,PRIMARY KEY(fpga,kind,seq),UNIQUE(fpga,kind,signal)) WITHOUT ROWID;
CREATE TABLE anchors(id TEXT PRIMARY KEY,fpga TEXT NOT NULL,link TEXT NOT NULL,peer TEXT NOT NULL,
 kind TEXT NOT NULL,lane INTEGER NOT NULL,record_json TEXT NOT NULL,
 UNIQUE(fpga,link,peer,kind,lane)) WITHOUT ROWID;
CREATE INDEX anchors_fpga_identity ON anchors(fpga,id);
CREATE TABLE wire_bindings(link TEXT NOT NULL,wire INTEGER NOT NULL,wire_id TEXT NOT NULL UNIQUE,
 bank_group TEXT NOT NULL,cable TEXT NOT NULL,PRIMARY KEY(link,wire)) WITHOUT ROWID;
CREATE TABLE binding_physical_ports(fpga TEXT NOT NULL,p_port TEXT NOT NULL,record_json TEXT NOT NULL,
 schedule_entry_count INTEGER NOT NULL,schedule_entries_sha256 TEXT NOT NULL,
 link TEXT NOT NULL,wire INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
 PRIMARY KEY(fpga,p_port),UNIQUE(fpga,link,wire,kind)) WITHOUT ROWID;
CREATE VIEW endpoint_values AS
 SELECT '__emuflow_tx_'||entry_id AS id,source_fpga AS fpga,'tx' AS kind,sink_fpga AS peer,
 CASE WHEN source_fpga=route_source THEN 'net:'||net ELSE 'shadow:'||demand||':'||source_fpga END AS signal,
 entry_id AS schedule_entry,demand,net,link_id AS link,slot,arrival_slot,lane,logical_lane
 FROM hops
 UNION ALL
 SELECT '__emuflow_rx_'||entry_id,sink_fpga,'rx',source_fpga,'shadow:'||demand||':'||sink_fpga,
 entry_id,demand,net,link_id,slot,arrival_slot,lane,logical_lane FROM hops;
CREATE VIEW endpoints AS
 SELECT e.*,CASE WHEN (SELECT value FROM metadata WHERE key='semantic_contract_sha256')='null'
 THEN json_object('id',id,'fpga',fpga,'kind',kind,'peer',peer,'signal',signal,'schedule_entry',schedule_entry,
 'demand',demand,'net',net,'link',link,'slot',slot,'arrival_slot',arrival_slot,'lane',lane,'logical_lane',logical_lane)
 ELSE json_patch(json_object('id',id,'fpga',fpga,'kind',kind,'peer',peer,'signal',signal,'schedule_entry',schedule_entry,
 'demand',demand,'net',net,'link',link,'slot',slot,'arrival_slot',arrival_slot,'lane',lane,'logical_lane',logical_lane,
 'boundary_identity','static_exact:'||kind||':'||schedule_entry,
 'semantic_contract_sha256',json((SELECT value FROM metadata WHERE key='semantic_contract_sha256')),'preserve',json('true')),
 CASE WHEN kind='tx' THEN json_object('source_role',CASE WHEN substr(signal,1,4)='net:' THEN 'cut_source' ELSE 'route_tree_relay' END) ELSE '{}' END)
 END AS record_json FROM endpoint_values e;
CREATE VIEW lanes AS
 SELECT 'lane_'||entry_id AS id,entry_id AS schedule_entry,demand,net,link_id AS link,
 source_fpga,sink_fpga,slot,lane,logical_lane,'__emuflow_tx_'||entry_id AS tx_endpoint,
 '__emuflow_rx_'||entry_id AS rx_endpoint,
 json_object('id','lane_'||entry_id,'schedule_entry',entry_id,'demand',demand,'net',net,'link',link_id,
 'from',source_fpga,'to',sink_fpga,'slot',slot,'lane',lane,'logical_lane',logical_lane,
 'tx_endpoint','__emuflow_tx_'||entry_id,'rx_endpoint','__emuflow_rx_'||entry_id) AS record_json FROM hops;
CREATE VIEW binding_adapter_ports AS
 SELECT fpga,p_port,json_extract(record_json,'$.n.port') AS n_port,
 CASE WHEN kind='tx' THEN 'source' ELSE 'sink' END AS role,
 json_extract(record_json,'$.logical_port')||'['||wire||']' AS logical_port,
 json_extract(record_json,'$.logical_port_width') AS logical_port_width,
 json_extract(record_json,'$.p.package_pin') AS package_pin_p,
 json_extract(record_json,'$.n.package_pin') AS package_pin_n,
 json_extract(record_json,'$.iostandard') AS iostandard FROM binding_physical_ports;
CREATE VIEW binding_ends AS
 SELECT fpga,link,wire,kind,json_object(
 'fpga',fpga,'bank',json_extract(record_json,'$.bank'),'pair_id',json_extract(record_json,'$.pair_id'),
 'logical_port',json_extract(record_json,'$.logical_port')||'['||wire||']',
 'logical_port_width',json_extract(record_json,'$.logical_port_width'),
 'p',json_extract(record_json,'$.p'),'n',json_extract(record_json,'$.n')) AS record_json
 FROM binding_physical_ports;
CREATE VIEW binding_entries AS
 SELECT h.entry_id AS schedule_entry,h.link_id AS link,h.slot,h.lane AS wire,h.source_fpga,h.sink_fpga,
 json_object('schedule_entry',h.entry_id,'net',h.net,'slot',h.slot,'arrival_slot',h.arrival_slot,'tdm_ratio',h.tdm_ratio,
 'wire_id',w.wire_id,'wire',h.lane,'link',h.link_id,'bank_group',json(w.bank_group),'cable',json(w.cable),
 'iostandard',json((SELECT value FROM metadata WHERE key='iostandard')),
 'source',json(s.record_json),'sink',json(r.record_json)) AS record_json
 FROM hops h JOIN wire_bindings w ON w.link=h.link_id AND w.wire=h.lane
 JOIN binding_ends s ON s.fpga=h.source_fpga AND s.link=h.link_id AND s.wire=h.lane AND s.kind='tx'
 JOIN binding_ends r ON r.fpga=h.sink_fpga AND r.link=h.link_id AND r.wire=h.lane AND r.kind='rx';
