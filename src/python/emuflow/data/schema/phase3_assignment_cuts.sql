CREATE TABLE cuts(
  cut_index INTEGER PRIMARY KEY,
  net_id TEXT NOT NULL UNIQUE,
  source_part_index INTEGER NOT NULL,
  cut_class TEXT NOT NULL,
  sink_endpoints INTEGER NOT NULL CHECK(sink_endpoints>0),
  transport_round INTEGER CHECK(transport_round IS NULL OR transport_round>=0),
  dependency_level INTEGER CHECK(dependency_level IS NULL OR dependency_level>=0),
  combinational_dependency_depth INTEGER CHECK(
    combinational_dependency_depth IS NULL OR
    combinational_dependency_depth>=0
  )
);
CREATE TABLE cut_predecessors(
  cut_index INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  predecessor_net_id TEXT NOT NULL,
  PRIMARY KEY(cut_index,ordinal),
  UNIQUE(cut_index,predecessor_net_id)
) WITHOUT ROWID;
CREATE INDEX cut_predecessors_net
  ON cut_predecessors(predecessor_net_id,cut_index);
