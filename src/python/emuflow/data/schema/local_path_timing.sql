
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE timings(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  id TEXT NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind = 'local'),
  fpga TEXT NOT NULL CHECK(length(fpga) > 0),
  clock_domain TEXT NOT NULL CHECK(length(clock_domain) > 0),
  clock_period_ns REAL NOT NULL CHECK(clock_period_ns > 0.0),
  required_time_ns REAL,
  start_pin TEXT NOT NULL CHECK(length(start_pin) > 0),
  end_pin TEXT NOT NULL CHECK(length(end_pin) > 0),
  measurement TEXT,
  delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0)
);
CREATE UNIQUE INDEX timings_id_unique ON timings(id);
CREATE TABLE path_pins(
  path_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  pin TEXT NOT NULL CHECK(length(pin) > 0),
  PRIMARY KEY(path_id,ordinal),
  FOREIGN KEY(path_id) REFERENCES timings(id)
) WITHOUT ROWID;
