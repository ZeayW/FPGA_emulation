CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE clusters(
  seq INTEGER PRIMARY KEY,
  cluster_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  site_templates_json TEXT NOT NULL CHECK(json_valid(site_templates_json)),
  control_set_json TEXT NOT NULL CHECK(json_valid(control_set_json)),
  site_mode TEXT
);
CREATE TABLE assignments(
  cluster_seq INTEGER NOT NULL REFERENCES clusters(seq),
  seq INTEGER NOT NULL,
  instance TEXT NOT NULL UNIQUE,
  cell_type TEXT NOT NULL,
  bel TEXT,
  bel_candidates_json TEXT NOT NULL CHECK(json_valid(bel_candidates_json)),
  PRIMARY KEY(cluster_seq,seq)
) WITHOUT ROWID;
CREATE INDEX assignments_instance_order ON assignments(instance);
CREATE TABLE cascades(
  seq INTEGER PRIMARY KEY,
  cascade_id TEXT NOT NULL UNIQUE,
  record_json TEXT NOT NULL CHECK(json_valid(record_json))
);
CREATE TABLE constants(
  seq INTEGER PRIMARY KEY,
  instance TEXT NOT NULL UNIQUE
);
