// Original compact ownership joins; no host-side circuit reconstruction.
void CheckSource(SqliteDatabase &db) {
  Reject(db,
         "SELECT id FROM source_emuir.net_records WHERE typeof(seq)!='integer' "
         "OR seq<0 OR seq>9223372036854775804 LIMIT 1",
         "net ordinal is invalid");
  Reject(db,
         "SELECT id FROM source_emuir.cell_records WHERE "
         "typeof(seq)!='integer' OR seq<0 LIMIT 1",
         "cell ordinal is invalid");
  Reject(db,
         "SELECT c.id FROM source_emuir.cell_records c LEFT JOIN "
         "source_emuir.payloads k ON k.seq=c.constants_seq LEFT JOIN "
         "source_emuir.payloads p ON p.seq=c.parameters_seq LEFT JOIN "
         "source_emuir.payloads a ON a.seq=c.attributes_seq WHERE k.seq IS "
         "NULL OR (c.parameters_seq IS NOT NULL AND p.seq IS NULL) OR "
         "(c.attributes_seq IS NOT NULL AND a.seq IS NULL) LIMIT 1",
         "has missing payloads");

  Reject(db, R"SQL(SELECT c.id
              FROM source_emuir.cell_records AS c
         LEFT JOIN source_emuir.payloads AS p ON p.seq=c.parameters_seq
         LEFT JOIN source_emuir.payloads AS a ON a.seq=c.attributes_seq
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq
             WHERE (p.value IS NOT NULL AND (
                       json_valid(p.value)=0 OR json_type(p.value)!='object'))
                OR (a.value IS NOT NULL AND (
                       json_valid(a.value)=0 OR json_type(a.value)!='object'))
                OR json_valid(k.value)=0 OR json_type(k.value)!='array'
             LIMIT 1)SQL",
         "has invalid payloads");
  Reject(db, R"SQL(SELECT e.net_seq,e.instance_seq
              FROM source_emuir.endpoint_records AS e
         LEFT JOIN source_emuir.net_records AS n ON n.seq=e.net_seq
         LEFT JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
         LEFT JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
             WHERE n.seq IS NULL
                OR e.role NOT IN (0,1)
                OR e.bit < 0
                OR p.seq IS NULL
                OR (e.instance_seq IS NOT NULL AND c.seq IS NULL)
             LIMIT 1)SQL",
         "endpoint relation is invalid");
  Reject(db, R"SQL(SELECT c.id
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
             WHERE json_type(item.value)!='object'
                OR json_type(item.value,'$.port')!='text'
                OR json_extract(item.value,'$.port')=''
                OR json_type(item.value,'$.bit')!='integer'
                OR json_extract(item.value,'$.bit') < 0
                OR json_type(item.value,'$.value')!='text'
             LIMIT 1)SQL",
         "has invalid constants");
  Reject(db, R"SQL(SELECT c.id,p.name,e.bit
              FROM source_emuir.endpoint_records AS e
              JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
              JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
             WHERE e.instance_seq IS NOT NULL
             GROUP BY e.instance_seq,e.port_seq,e.bit
            HAVING min(e.net_seq) != max(e.net_seq)
             LIMIT 1)SQL",
         "has multiple nets");
  Reject(db, R"SQL(SELECT c.id,json_extract(item.value,'$.port'),
                   json_extract(item.value,'$.bit')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
             GROUP BY c.seq,json_extract(item.value,'$.port'),
                      json_extract(item.value,'$.bit')
            HAVING min(json_extract(item.value,'$.value')) !=
                   max(json_extract(item.value,'$.value'))
             LIMIT 1)SQL",
         "has conflicting constants");
  Reject(db, R"SQL(SELECT c.id,json_extract(item.value,'$.port'),
                   json_extract(item.value,'$.bit')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
              JOIN source_emuir.pin_names AS p
                ON p.name=json_extract(item.value,'$.port')
              JOIN source_emuir.endpoint_records AS e
                ON e.instance_seq=c.seq AND e.port_seq=p.seq
               AND e.bit=json_extract(item.value,'$.bit')
             LIMIT 1)SQL",
         "has conflicting constant and net");
}
void NativeProjection(SqliteDatabase &db) {
  db.Execute(R"SQL(INSERT INTO cells(id,seq,type,record_json)
            SELECT c.id,c.seq,c.type,
                   '{"attributes":' || coalesce(a.value,'{}') ||
                   ',"hide_name":0,"parameters":' || coalesce(p.value,'{}') ||
                   ',"type":' || json_quote(c.type) || '}'
              FROM source_emuir.cell_records AS c
         LEFT JOIN source_emuir.payloads AS p ON p.seq=c.parameters_seq
         LEFT JOIN source_emuir.payloads AS a ON a.seq=c.attributes_seq
             ORDER BY c.seq)SQL");
  db.Execute(R"SQL(WITH pin_values AS (
              SELECT e.instance_seq AS cell_seq,c.id AS cell_id,
                     e.port_seq,p.name AS port,e.bit AS port_bit,
                     CASE WHEN min(e.role) != max(e.role) THEN 2
                          ELSE 1 - min(e.role) END AS bit_direction,
                     min(e.net_seq) + 2 AS net_bit
                FROM source_emuir.endpoint_records AS e
                JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
                JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
               WHERE e.instance_seq IS NOT NULL
               GROUP BY e.instance_seq,e.port_seq,e.bit
            ), pins AS (
              SELECT cell_seq,cell_id,port,port_bit,
                     max(port_bit) OVER (
                       PARTITION BY cell_seq,port_seq
                     ) + 1 AS port_width,
                     CASE
                       WHEN min(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            ) != max(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            )
                         OR max(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            ) = 2
                       THEN 2 ELSE bit_direction END AS direction,
                     net_bit
                FROM pin_values
            )
            INSERT INTO cell_pins(
              cell_seq,cell_id,port,port_bit,port_width,direction,
              value_kind,net_bit,constant)
            SELECT cell_seq,cell_id,port,port_bit,port_width,direction,
                   0,net_bit,NULL
              FROM pins ORDER BY cell_seq,port,port_bit)SQL");
  db.Execute(R"SQL(CREATE TEMP TABLE native_constant_ports(
              cell_seq INTEGER NOT NULL,port TEXT NOT NULL,
              PRIMARY KEY(cell_seq,port)
            ) WITHOUT ROWID;
            INSERT INTO native_constant_ports
            SELECT DISTINCT c.seq,json_extract(item.value,'$.port')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item;)SQL");
  db.Execute(R"SQL(WITH constant_values AS (
              SELECT c.seq AS cell_seq,c.id AS cell_id,
                     json_extract(item.value,'$.port') AS port,
                     json_extract(item.value,'$.bit') AS port_bit,
                     min(json_extract(item.value,'$.value')) AS constant
                FROM source_emuir.cell_records AS c
                JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                     json_each(k.value) AS item
               GROUP BY c.seq,port,port_bit
            ), pins AS (
              SELECT cell_seq,cell_id,port,port_bit,
                     max(port_bit) OVER (
                       PARTITION BY cell_seq,port
                     ) + 1 AS port_width,constant
                FROM constant_values
            )
            INSERT INTO cell_pins(
              cell_seq,cell_id,port,port_bit,port_width,direction,
              value_kind,net_bit,constant)
            SELECT cell_seq,cell_id,port,port_bit,port_width,0,
                   1,NULL,constant
              FROM pins ORDER BY cell_seq,port,port_bit)SQL");
  db.Execute(R"SQL(UPDATE cell_pins AS pin
               SET port_width=(
                     SELECT max(peer.port_bit) + 1 FROM cell_pins AS peer
                      WHERE peer.cell_seq=pin.cell_seq AND peer.port=pin.port
                   ),
                   direction=(
                     SELECT CASE WHEN min(peer.direction) != max(peer.direction)
                                      OR max(peer.direction) = 2
                                 THEN 2 ELSE min(peer.direction) END
                       FROM cell_pins AS peer
                      WHERE peer.cell_seq=pin.cell_seq AND peer.port=pin.port
                   )
             WHERE (pin.cell_seq,pin.port) IN (
                     SELECT cell_seq,port FROM native_constant_ports
                   ))SQL");
  db.Execute("DROP TABLE native_constant_ports");
}
void CheckPins(SqliteDatabase &db) {
  Reject(db, R"SQL(SELECT cell_id,port
              FROM cell_pins
             GROUP BY cell_seq,cell_id,port
            HAVING min(port_bit) != 0 OR count(*) != max(port_bit) + 1
             LIMIT 1)SQL",
         "is not contiguous");
}

// Independently rejoin final physical pins to the original compact ownership.
// This checks source facts, not the insertion decisions or a producer snapshot.
void AuditProjection(SqliteDatabase &db) {
  Reject(
      db,
      R"SQL(SELECT c.id FROM cells c LEFT JOIN source_emuir.cell_records s ON s.seq=c.seq
 LEFT JOIN source_emuir.payloads p ON p.seq=s.parameters_seq LEFT JOIN source_emuir.payloads a ON a.seq=s.attributes_seq
 WHERE s.id IS NULL OR c.id!=s.id OR json_extract(c.record_json,'$.type')!=c.type
 OR json(json_extract(c.record_json,'$.attributes'))!=json(coalesce(a.value,'{}'))
 OR (s.type=c.type AND json(json_extract(c.record_json,'$.parameters'))!=json(coalesce(p.value,'{}')))
 LIMIT 1)SQL",
      "final mapped identity or payload differs from source");
  Reject(
      db,
      R"SQL(SELECT p.name FROM source_emuir.endpoint_records e JOIN source_emuir.pin_names p ON p.seq=e.port_seq
 WHERE e.instance_seq IS NULL AND NOT EXISTS(SELECT 1 FROM source_emuir.port_records t
 WHERE json_extract(t.record_json,'$.id')=p.name AND e.bit<json_extract(t.record_json,'$.width')) LIMIT 1)SQL",
      "top endpoint is outside its declared port");
  db.Execute(R"SQL(
CREATE TEMP VIEW audit_source_nets AS SELECT c.id AS cell_id,
 CASE WHEN c.type IN ('$lut','$_LUT_') AND p.name='A' THEN 'I'||e.bit
      WHEN c.type IN ('$lut','$_LUT_') AND p.name='Y' AND e.bit=0 THEN 'O' ELSE p.name END AS port,
 CASE WHEN c.type IN ('$lut','$_LUT_') AND p.name='A' THEN 0 ELSE e.bit END AS bit,
 e.net_seq+2 AS value,e.role
 FROM source_emuir.endpoint_records e JOIN source_emuir.cell_records c ON c.seq=e.instance_seq JOIN source_emuir.pin_names p ON p.seq=e.port_seq;
CREATE TEMP VIEW audit_source_constants AS SELECT c.id AS cell_id,
 CASE WHEN c.type IN ('$lut','$_LUT_') AND json_extract(k.value,'$.port')='A' THEN 'I'||json_extract(k.value,'$.bit')
      WHEN c.type IN ('$lut','$_LUT_') AND json_extract(k.value,'$.port')='Y' AND json_extract(k.value,'$.bit')=0 THEN 'O'
      ELSE json_extract(k.value,'$.port') END AS port,
 CASE WHEN c.type IN ('$lut','$_LUT_') AND json_extract(k.value,'$.port')='A' THEN 0 ELSE json_extract(k.value,'$.bit') END AS bit,
 json_extract(k.value,'$.value') AS value FROM source_emuir.cell_records c JOIN source_emuir.payloads a ON a.seq=c.constants_seq,json_each(a.value) k
 UNION ALL SELECT id,'CE',0,'1' FROM source_emuir.cell_records WHERE type IN ('$_DFF_P_','$_DFF_N_')
 UNION ALL SELECT id,'R',0,'0' FROM source_emuir.cell_records WHERE type IN ('$_DFF_P_','$_DFF_N_');
)SQL");
  Reject(
      db,
      R"SQL(SELECT s.cell_id FROM audit_source_nets s LEFT JOIN cell_pins p ON p.cell_id=s.cell_id AND p.port=s.port AND p.port_bit=s.bit
 WHERE p.cell_id IS NULL OR p.value_kind!=0 OR p.net_bit!=s.value
 OR (s.role=0 AND p.direction NOT IN (1,2)) OR (s.role=1 AND p.direction NOT IN (0,2)) LIMIT 1)SQL",
      "final net pin differs from source");
  Reject(
      db,
      R"SQL(SELECT s.cell_id FROM audit_source_constants s LEFT JOIN cell_pins p ON p.cell_id=s.cell_id AND p.port=s.port AND p.port_bit=s.bit
 WHERE p.cell_id IS NULL OR p.value_kind!=1 OR p.constant!=s.value OR p.direction NOT IN (0,2) LIMIT 1)SQL",
      "final constant pin differs from source");
  Reject(
      db,
      R"SQL(SELECT 'coverage' WHERE (SELECT count(*) FROM cell_pins)!=(SELECT count(*) FROM (
 SELECT cell_id,port,bit FROM audit_source_nets UNION SELECT cell_id,port,bit FROM audit_source_constants)))SQL",
      "final mapped pin coverage differs from source");
  db.Execute("DROP VIEW audit_source_nets;DROP VIEW audit_source_constants");
}
