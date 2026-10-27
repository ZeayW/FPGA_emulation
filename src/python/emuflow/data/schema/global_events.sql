CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE paths(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq>=0),
  path TEXT NOT NULL UNIQUE CHECK(length(path)>0),
  representative_path TEXT,
  path_scope INTEGER CHECK(path_scope IN (0,1)),
  clock_domain TEXT,
  target_period_ns REAL,
  cone_bound INTEGER CHECK(cone_bound IN (0,1)),
  interconnect_delay_model TEXT
);
CREATE TABLE events(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq>=0),
  event TEXT NOT NULL UNIQUE CHECK(length(event)>0)
);
CREATE TABLE checks(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq>=0),
  path_seq INTEGER NOT NULL CHECK(path_seq>=0),
  role INTEGER NOT NULL CHECK(role BETWEEN 0 AND 3),
  event_seq INTEGER NOT NULL CHECK(event_seq>=0),
  numbers BLOB NOT NULL CHECK(length(numbers)>=16 AND length(numbers)%8=0),
  UNIQUE(path_seq,role,event_seq)
);
