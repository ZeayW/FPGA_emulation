CREATE TABLE net_weights (
    net_id TEXT PRIMARY KEY,
    criticality REAL NOT NULL,
    path_count INTEGER NOT NULL,
    weight REAL NOT NULL
);
