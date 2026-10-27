CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE parts(
  part_index INTEGER PRIMARY KEY,
  fpga TEXT NOT NULL UNIQUE
);
CREATE TABLE copies(
  source_instance_seq INTEGER NOT NULL,
  part_index INTEGER NOT NULL,
  copy_id TEXT NOT NULL,
  is_replica INTEGER NOT NULL CHECK(is_replica IN (0,1)),
  PRIMARY KEY(source_instance_seq, copy_id),
  UNIQUE(copy_id)
) WITHOUT ROWID;
CREATE INDEX copies_part_source
  ON copies(part_index, source_instance_seq);
CREATE TABLE cut_sinks(
  net_id TEXT NOT NULL,
  source_part_index INTEGER NOT NULL,
  sink_part_index INTEGER NOT NULL,
  PRIMARY KEY(net_id, sink_part_index)
) WITHOUT ROWID;
CREATE INDEX cut_sinks_sink
  ON cut_sinks(sink_part_index, net_id);
