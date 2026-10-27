
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE endpoints(
  id TEXT PRIMARY KEY NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  schedule_entry TEXT NOT NULL CHECK(length(schedule_entry) > 0),
  demand TEXT NOT NULL CHECK(length(demand) > 0),
  logical_net TEXT NOT NULL CHECK(length(logical_net) > 0),
  link TEXT NOT NULL CHECK(length(link) > 0),
  peer TEXT NOT NULL CHECK(length(peer) > 0),
  lane INTEGER NOT NULL CHECK(lane >= 0),
  logical_lane INTEGER NOT NULL CHECK(logical_lane >= 0),
  internal_signal TEXT NOT NULL CHECK(length(internal_signal) > 0),
  source_class TEXT NOT NULL CHECK(
    source_class IN ('dut-net','forwarded-shadow','captured-shadow')
  ),
  external_port TEXT NOT NULL CHECK(length(external_port) > 0),
  external_port_bit INTEGER NOT NULL CHECK(external_port_bit >= 0),
  external_net TEXT NOT NULL CHECK(length(external_net) > 0),
  merged_logical_net TEXT
) WITHOUT ROWID;
CREATE UNIQUE INDEX endpoints_schedule_kind_unique
  ON endpoints(schedule_entry,kind);
CREATE INDEX endpoints_kind_id ON endpoints(kind,id);
CREATE TABLE boundary_registers(
  endpoint_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  instance TEXT NOT NULL CHECK(length(instance) > 0),
  PRIMARY KEY(endpoint_id,ordinal),
  UNIQUE(endpoint_id,instance),
  FOREIGN KEY(endpoint_id) REFERENCES endpoints(id)
) WITHOUT ROWID;
