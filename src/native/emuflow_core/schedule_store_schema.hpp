#pragma once
namespace emuflow::core {
inline constexpr const char* kScheduleStoreDDL = R"SQL(
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE records(
  section TEXT NOT NULL,
  seq INTEGER NOT NULL,
  identity TEXT NOT NULL,
  record_json TEXT NOT NULL,
  CHECK(json_valid(record_json)),
  CHECK(
    identity IS CASE section
      WHEN 'domain_schedules' THEN json_extract(record_json,'$.key')
      WHEN 'demand_completions' THEN json_extract(record_json,'$.demand')
      ELSE json_extract(record_json,'$.id')
    END
  ),
  PRIMARY KEY(section,seq),
  UNIQUE(section,identity)
) WITHOUT ROWID;

CREATE TABLE schedule_entries(
  seq INTEGER NOT NULL UNIQUE,
  entry_id TEXT NOT NULL UNIQUE,
  demand TEXT NOT NULL,
  net TEXT NOT NULL,
  hop INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  capacity_key TEXT NOT NULL,
  slot INTEGER NOT NULL,
  lane INTEGER NOT NULL,
  ready_slot INTEGER NOT NULL,
  arrival_slot INTEGER NOT NULL,
  tdm_ratio INTEGER,
  PRIMARY KEY(demand,link_id,source_fpga,sink_fpga)
) WITHOUT ROWID;

CREATE TABLE schedule_routes(
  seq INTEGER NOT NULL UNIQUE,
  demand TEXT NOT NULL PRIMARY KEY,
  net TEXT NOT NULL,
  source_fpga TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE schedule_route_sinks(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq)
) WITHOUT ROWID;
CREATE TABLE schedule_route_edges(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq)
) WITHOUT ROWID;
CREATE TABLE schedule_completions(
  seq INTEGER NOT NULL UNIQUE,
  demand TEXT NOT NULL PRIMARY KEY,
  net TEXT NOT NULL,
  source_ready_slot INTEGER NOT NULL,
  completion_slot INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE schedule_completion_arrivals(
  completion_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  arrival_slot INTEGER NOT NULL,
  PRIMARY KEY(completion_seq,seq),
  UNIQUE(completion_seq,sink_fpga)
) WITHOUT ROWID;
CREATE INDEX schedule_routes_net ON schedule_routes(net);
)SQL";
inline constexpr const char* kScheduleStoreIndexDDL = R"SQL(
CREATE INDEX schedule_entries_seq ON schedule_entries(seq);
CREATE INDEX schedule_entries_capacity_order
  ON schedule_entries(capacity_key,demand,link_id,source_fpga,sink_fpga);
CREATE INDEX schedule_entries_collision
  ON schedule_entries(capacity_key,slot,lane);
)SQL";
}
