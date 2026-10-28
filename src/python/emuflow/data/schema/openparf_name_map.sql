CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE atoms(
  seq INTEGER PRIMARY KEY,
  openparf TEXT NOT NULL UNIQUE,
  instance TEXT NOT NULL UNIQUE,
  cell_type TEXT NOT NULL,
  source_cluster TEXT NOT NULL,
  resource TEXT NOT NULL
);
CREATE INDEX atoms_instance_order ON atoms(instance);
CREATE TABLE carry_cascades(
  seq INTEGER PRIMARY KEY,
  cascade_id TEXT NOT NULL UNIQUE,
  record_json TEXT NOT NULL CHECK(json_valid(record_json))
);
CREATE TABLE cascade_members(
  cascade_seq INTEGER NOT NULL REFERENCES carry_cascades(seq),
  member_seq INTEGER NOT NULL,
  instance TEXT NOT NULL UNIQUE REFERENCES atoms(instance),
  PRIMARY KEY(cascade_seq,member_seq)
) WITHOUT ROWID;
