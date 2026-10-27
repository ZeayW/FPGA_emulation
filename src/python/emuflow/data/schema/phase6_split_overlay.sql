CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT NOT NULL UNIQUE);
CREATE TABLE net_segments(
  part_index INTEGER NOT NULL,
  source_net_seq INTEGER NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('original','logic_replica','transport_shadow')),
  PRIMARY KEY(part_index,source_net_seq)
) WITHOUT ROWID;
CREATE INDEX net_segments_source ON net_segments(source_net_seq,part_index);
CREATE TABLE shadow_drivers(
  part_index INTEGER NOT NULL,
  source_net_seq INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  schedule_entry_seq INTEGER NOT NULL,
  PRIMARY KEY(part_index,source_net_seq),
  UNIQUE(schedule_entry_seq)
) WITHOUT ROWID;
CREATE TABLE port_memberships(
  part_index INTEGER NOT NULL,
  source_port_seq INTEGER NOT NULL,
  PRIMARY KEY(part_index,source_port_seq)
) WITHOUT ROWID;
CREATE TABLE resources(
  part_index INTEGER NOT NULL,
  resource TEXT NOT NULL,
  amount INTEGER NOT NULL,
  PRIMARY KEY(part_index,resource)
) WITHOUT ROWID;
