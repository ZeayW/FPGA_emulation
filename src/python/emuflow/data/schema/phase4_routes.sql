
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE demands(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  route_seq INTEGER NOT NULL UNIQUE
);
CREATE TABLE routes(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  net_id TEXT NOT NULL UNIQUE,
  source_fpga TEXT NOT NULL,
  width_bits INTEGER NOT NULL,
  transport_round INTEGER,
  cut_class TEXT,
  dependency_level INTEGER,
  combinational_dependency_depth INTEGER,
  max_latency_cycles INTEGER,
  predicted_max_delay_ns REAL,
  CHECK(length(demand_id)>0),
  CHECK(length(net_id)>0),
  CHECK(length(source_fpga)>0),
  CHECK(typeof(width_bits)='integer' AND width_bits>0),
  CHECK(transport_round IS NULL OR (
    typeof(transport_round)='integer' AND transport_round>=0
  )),
  CHECK(cut_class IS NULL OR cut_class IN (
    'combinational','register_input','register_output'
  )),
  CHECK((dependency_level IS NULL AND combinational_dependency_depth IS NULL)
    OR (typeof(dependency_level)='integer' AND dependency_level>=0
      AND typeof(combinational_dependency_depth)='integer'
      AND combinational_dependency_depth>=0)),
  CHECK(max_latency_cycles IS NULL OR (
    typeof(max_latency_cycles)='integer' AND max_latency_cycles>=0
  )),
  CHECK(predicted_max_delay_ns IS NULL OR (
    typeof(predicted_max_delay_ns) IN ('integer','real')
    AND predicted_max_delay_ns>=0
  ))
);
CREATE TABLE route_sinks(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,sink_fpga)
) WITHOUT ROWID;
CREATE TABLE route_edges(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,link_id,source_fpga,sink_fpga)
) WITHOUT ROWID;
CREATE TABLE route_predecessors(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  predecessor_net TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,predecessor_net)
) WITHOUT ROWID;
CREATE TABLE timing_paths(
  seq INTEGER PRIMARY KEY,
  path_id TEXT NOT NULL UNIQUE,
  record_json TEXT NOT NULL,
  CHECK(json_valid(record_json))
);
CREATE TABLE route_master_selections(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  generator TEXT NOT NULL
);
CREATE TABLE refinement_batch_paths(
  seq INTEGER PRIMARY KEY,
  batch_index INTEGER NOT NULL,
  position INTEGER NOT NULL,
  path_index INTEGER NOT NULL UNIQUE,
  UNIQUE(batch_index,position)
);
CREATE INDEX routes_net_order ON routes(net_id,seq);
CREATE INDEX routes_demand_order ON routes(demand_id,seq);
CREATE INDEX route_edges_arc ON route_edges(link_id,source_fpga,sink_fpga);
CREATE INDEX route_predecessors_net ON route_predecessors(predecessor_net);
