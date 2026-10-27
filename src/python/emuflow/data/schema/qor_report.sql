CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE timing_paths(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  path TEXT NOT NULL CHECK(length(path) > 0),
  typed_presence INTEGER NOT NULL CHECK(typed_presence >= 0),
  representative_path TEXT,
  path_scope TEXT,
  clock_domain TEXT,
  target_period_ns REAL,
  cone_bound INTEGER,
  interconnect_delay_model TEXT,
  system_delay_bound_ns REAL NOT NULL,
  target_required_time_ns REAL NOT NULL,
  target_clock_slack_bound_ns REAL NOT NULL,
  runtime_required_time_ns REAL NOT NULL,
  runtime_clock_slack_bound_ns REAL NOT NULL,
  extra_json TEXT NOT NULL
);
CREATE UNIQUE INDEX timing_paths_identity ON timing_paths(path);
