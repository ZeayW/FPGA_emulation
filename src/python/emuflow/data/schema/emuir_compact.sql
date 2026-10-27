
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE payloads(seq INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
CREATE TABLE cell_records(
 seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, name TEXT, flags INTEGER NOT NULL,
 type TEXT NOT NULL, resources_seq INTEGER NOT NULL, parameters_seq INTEGER,
 attributes_seq INTEGER, constants_seq INTEGER NOT NULL, extras_seq INTEGER NOT NULL);
CREATE TABLE net_records(
 seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, name TEXT, flags INTEGER NOT NULL,
 yosys_bit INTEGER, bus_index INTEGER, fanout INTEGER, cut_class TEXT NOT NULL,
 extras_seq INTEGER NOT NULL);
CREATE TABLE pin_names(seq INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE endpoint_records(
 net_seq INTEGER NOT NULL, role INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 instance_seq INTEGER, port_seq INTEGER NOT NULL, bit INTEGER NOT NULL,
 extras_seq INTEGER NOT NULL, PRIMARY KEY(net_seq,role,ordinal)) WITHOUT ROWID;
CREATE TABLE alias_records(net_seq INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 alias TEXT NOT NULL, PRIMARY KEY(net_seq,ordinal)) WITHOUT ROWID;
CREATE TABLE port_records(seq INTEGER PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE clocks(seq INTEGER PRIMARY KEY, record_json TEXT NOT NULL);
CREATE VIEW instances AS
 SELECT c.seq,c.id,CASE WHEN flags & 1 THEN c.id ELSE c.name END AS name,c.type,
 p.value AS parameters_json,a.value AS attributes_json,x.value AS extras_json
 FROM cell_records c LEFT JOIN payloads p ON p.seq=c.parameters_seq
 LEFT JOIN payloads a ON a.seq=c.attributes_seq JOIN payloads x ON x.seq=c.extras_seq;
CREATE VIEW instance_resources AS
 SELECT c.seq AS instance_seq,r.key AS resource,r.value AS amount
 FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq,
 json_each(p.value) r;
CREATE VIEW nets AS SELECT n.seq,n.id,CASE WHEN n.flags & 1 THEN n.id ELSE n.name END AS name,
 n.yosys_bit,n.bus_index,n.fanout,n.cut_class,p.value AS extras_json
 FROM net_records n LEFT JOIN payloads p ON p.seq=n.extras_seq;
CREATE VIEW endpoints AS
 SELECT e.net_seq,e.role,e.ordinal,c.id AS instance_id,n.name AS port,e.bit,e.instance_seq,
 p.value AS extras_json FROM endpoint_records e
 LEFT JOIN cell_records c ON c.seq=e.instance_seq
 LEFT JOIN pin_names n ON n.seq=e.port_seq LEFT JOIN payloads p ON p.seq=e.extras_seq;
CREATE VIEW net_aliases AS SELECT * FROM alias_records;