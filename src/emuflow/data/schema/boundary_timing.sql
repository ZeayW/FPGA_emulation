
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE timings(
  id TEXT PRIMARY KEY NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  schedule_entry TEXT NOT NULL CHECK(length(schedule_entry) > 0),
  delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0),
  start_object TEXT NOT NULL CHECK(length(start_object) > 0),
  end_object TEXT NOT NULL CHECK(length(end_object) > 0),
  measurement TEXT NOT NULL CHECK(length(measurement) > 0)
) WITHOUT ROWID;
CREATE UNIQUE INDEX timings_schedule_kind_unique
  ON timings(schedule_entry,kind);
CREATE INDEX timings_kind_id ON timings(kind,id);
