PRAGMA application_id = 1162696016;
PRAGMA user_version = 1;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = MEMORY;
PRAGMA synchronous = FULL;
CREATE TABLE manifest (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema TEXT NOT NULL,
    packed_clusters INTEGER NOT NULL,
    assignments INTEGER NOT NULL,
    cascade_chains INTEGER NOT NULL,
    constants INTEGER NOT NULL
);
CREATE TABLE document (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE cluster (
    ordinal INTEGER PRIMARY KEY,
    cluster_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    site_templates_json TEXT NOT NULL,
    control_set_json TEXT,
    control_sets_json TEXT,
    site_mode TEXT,
    site TEXT NOT NULL UNIQUE,
    site_type TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    fixed INTEGER NOT NULL CHECK(fixed IN (0, 1)),
    physical_region_json TEXT
);
CREATE TABLE assignment (
    cluster_ordinal INTEGER NOT NULL REFERENCES cluster(ordinal),
    instance TEXT NOT NULL UNIQUE,
    cell_type TEXT NOT NULL,
    bel TEXT NOT NULL,
    placement_mode TEXT NOT NULL,
    physical_site TEXT NOT NULL,
    packed_ordinal INTEGER NOT NULL,
    placement_ordinal INTEGER NOT NULL,
    PRIMARY KEY(cluster_ordinal, packed_ordinal)
) WITHOUT ROWID;
CREATE INDEX assignment_placement_order
    ON assignment(cluster_ordinal, placement_ordinal);
CREATE TABLE site_pin_repair (
    seq INTEGER PRIMARY KEY,
    cluster_id TEXT NOT NULL REFERENCES cluster(cluster_id),
    instance TEXT NOT NULL,
    from_bel TEXT NOT NULL,
    to_bel TEXT NOT NULL
);
CREATE TABLE cascade (
    ordinal INTEGER PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE constant (
    ordinal INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
