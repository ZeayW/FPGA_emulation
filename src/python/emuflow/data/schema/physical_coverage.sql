CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE endpoint_bindings(
 endpoint TEXT PRIMARY KEY NOT NULL,kind TEXT NOT NULL,schedule_entry TEXT NOT NULL,fpga TEXT NOT NULL,
 measured_min_delay_ns REAL NOT NULL,measured_max_delay_ns REAL NOT NULL,measurement TEXT
) WITHOUT ROWID;
CREATE TABLE logic_segment_bindings(
 segment TEXT PRIMARY KEY NOT NULL,fpga TEXT NOT NULL,measured_min_delay_ns REAL NOT NULL,
 measured_max_delay_ns REAL NOT NULL,measurement TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE board_bindings(
 seq INTEGER PRIMARY KEY NOT NULL,schedule_entry TEXT NOT NULL UNIQUE,link TEXT NOT NULL,
 source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL,delay_bound_ns REAL NOT NULL,measurement TEXT NOT NULL
);
CREATE TABLE missing_endpoints(endpoint TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID;
CREATE TABLE missing_logic(segment TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID;
