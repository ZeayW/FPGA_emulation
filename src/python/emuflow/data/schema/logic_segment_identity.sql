
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE segments(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  segment_id TEXT NOT NULL CHECK(length(segment_id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('launch','transition','capture')),
  system_path TEXT NOT NULL CHECK(length(system_path) > 0),
  member_path TEXT NOT NULL CHECK(length(member_path) > 0),
  cut_index INTEGER NOT NULL CHECK(cut_index >= 0),
  fpga TEXT NOT NULL CHECK(length(fpga) > 0),
  replace_tx_endpoint TEXT,
  start_pin TEXT NOT NULL CHECK(length(start_pin) > 0),
  end_pin TEXT NOT NULL CHECK(length(end_pin) > 0),
  static_exact_segment_id TEXT,
  start_object_kind TEXT CHECK(start_object_kind IN ('pin','port')),
  end_object_kind TEXT CHECK(end_object_kind IN ('pin','port')),
  cone_anchor_object_kind TEXT CHECK(cone_anchor_object_kind IN ('pin','port')),
  cone_anchor_pin TEXT
);
CREATE UNIQUE INDEX segments_id_unique ON segments(segment_id);
CREATE UNIQUE INDEX segments_sequence_unique ON segments(seq);
CREATE INDEX segments_canonical_cursor
  ON segments(system_path,member_path,cut_index,segment_id);
CREATE TABLE unsupported_members(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  path TEXT NOT NULL CHECK(length(path) > 0),
  reason TEXT NOT NULL CHECK(length(reason) > 0)
);
CREATE UNIQUE INDEX unsupported_members_path_unique
  ON unsupported_members(path);
