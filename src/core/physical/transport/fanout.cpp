#include "physical/transport/fanout.hpp"
#include "common/bounded_json.hpp"
#include "ir/resource_view.hpp"
#include "storage/schema/emuir_compact_schema.hpp"
#include <algorithm>
#include <climits>
#include <iomanip>
#include <optional>
#include <sstream>
namespace emuflow::core {
namespace {
using I = std::int64_t;
using R = JsonRecord;
using O = std::map<std::string, R>;
using A = std::vector<R>;
void Check(bool yes, const std::string &why) {
  if (!yes)
    throw StageError(ErrorCode::kInvalidArtifact, "transport fanout: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "transport fanout cancelled");
}
I Count(SqliteDatabase &db, const std::string &sql) {
  auto q = db.Prepare(sql);
  Check(q.Step(), "missing aggregate");
  return q.TypedInteger(0);
}
void Empty(SqliteDatabase &db, const std::string &sql, const char *why) {
  auto q = db.Prepare(sql);
  Check(!q.Step(), why);
}
I Intern(SqliteDatabase &db, const char *table, const char *field,
         const std::string &value) {
  auto q = db.Prepare(std::string("SELECT seq FROM ") + table + " WHERE " +
                      field + "=?");
  q.BindText(1, value);
  if (q.Step())
    return q.TypedInteger(0);
  auto put = db.Prepare(std::string("INSERT INTO ") + table + "(" + field +
                        ") VALUES (?)");
  put.BindText(1, value);
  put.Execute();
  return Count(db, "SELECT last_insert_rowid()");
}
struct Sink {
  std::optional<I> instance;
  I port, bit, extras;
};
void SinkRow(SqliteStatement &put, I net, I ordinal, const Sink &sink) {
  put.Reset();
  put.BindInteger(1, net);
  put.BindInteger(2, 1);
  put.BindInteger(3, ordinal);
  if (sink.instance)
    put.BindInteger(4, *sink.instance);
  else
    put.BindNull(4);
  put.BindInteger(5, sink.port);
  put.BindInteger(6, sink.bit);
  put.BindInteger(7, sink.extras);
  put.Execute();
}
std::string Stem(I candidate, I level, I group) {
  std::ostringstream s;
  s << "$emuflow_transport_fanout$" << std::setw(6) << std::setfill('0')
    << candidate << "$l" << std::setw(2) << level << "$g" << std::setw(6)
    << group;
  return s.str();
}
} // namespace
std::string BufferTransportFanout(SqliteDatabase &db, I maximum,
                                  const volatile std::sig_atomic_t *flag) {
  Check(maximum >= 2 && maximum <= INT_MAX, "maximum must be an integer >= 2");
  Check(db.InTransaction(), "requires the private writer transaction");
  Check(Count(db, "PRAGMA application_id") == 0x454d5549 &&
            Count(db, "PRAGMA user_version") == 2,
        "requires compact EmuIR v2");
  const std::string native_ddl = std::string(kEmuirCompactDDL) +
                                 "CREATE INDEX endpoint_owner ON "
                                 "endpoint_records(instance_seq,port_seq,bit);";
  RequireSqliteDDL(db, native_ddl.c_str());
  {
    auto q =
        db.Prepare("SELECT value FROM metadata WHERE key='storage_schema'");
    Check(q.Step() && q.TypedText(0) == "emuflow.emuir-sqlite/v2",
          "storage schema disagrees");
  }
  struct Candidate {
    I seq, sinks;
    std::string id;
  };
  std::vector<Candidate> candidates;
  {
    auto q = db.Prepare(
        "SELECT n.seq,n.id,sum(e.role=0),sum(e.role=1) FROM net_records n JOIN "
        "endpoint_records e ON e.net_seq=n.seq WHERE n.cut_class NOT IN "
        "('clock','reset') GROUP BY n.seq,n.id HAVING sum(e.role=0)=1 AND "
        "sum(e.role=1)>? ORDER BY n.seq");
    q.BindInteger(1, maximum);
    while (q.Step()) {
      Stop(flag);
      candidates.push_back(
          {q.TypedInteger(0), q.TypedInteger(3), q.TypedText(1)});
    }
  }
  const auto original_max =
      Count(db, "SELECT coalesce(max(fanout),0) FROM net_records WHERE "
                "cut_class NOT IN ('clock','reset')");
  O policy{{"scope", "synthesized-transport-only"},
           {"topology", "balanced-lut1-tree"},
           {"maximum_fanout", maximum},
           {"excluded_cut_classes", A{"clock", "reset"}},
           {"placement", "before-pack-and-place"},
           {"storage", "compact-emuir-streaming"}};
  I next_cell = Count(db, "SELECT coalesce(max(seq),-1)+1 FROM cell_records"),
    next_net = Count(db, "SELECT coalesce(max(seq),-1)+1 FROM net_records");
  const I initial_cells = next_cell, initial_nets = next_net;
  I resource = 0, parameters = 0, constants = 0, empty = 0, input = 0,
    output = 0;
  if (!candidates.empty()) {
    O resources;
    for (const auto *r : kResourceFields)
      resources.emplace(r, std::string(r) == "lut" ? 1 : 0);
    resource = Intern(db, "payloads", "value", R(resources).EncodeUtf8());
    parameters = Intern(
        db, "payloads", "value",
        R(O{{"LUT", "10"}, {"WIDTH", "00000000000000000000000000000001"}})
            .EncodeUtf8());
    constants = Intern(db, "payloads", "value", "[]");
    empty = Intern(db, "payloads", "value", "{}");
    input = Intern(db, "pin_names", "name", "A");
    output = Intern(db, "pin_names", "name", "Y");
  }
  // Retain the original electrical facts separately from the decisions used to
  // build the buffer tree. The audit below checks the final writer relations.
  db.Execute("CREATE TEMP TABLE fanout_expected(root INTEGER,instance_seq "
             "INTEGER,port_seq INTEGER,bit INTEGER,extras_seq INTEGER);CREATE "
             "TEMP TABLE fanout_created(root INTEGER,net INTEGER PRIMARY "
             "KEY,cell INTEGER UNIQUE,leaf INTEGER)");
  A records;
  I added = 0, largest = 0;
  for (std::size_t candidate_index = 0; candidate_index < candidates.size();
       ++candidate_index) {
    Stop(flag);
    const auto &candidate = candidates[candidate_index];
    std::vector<Sink> downstream;
    {
      auto q = db.Prepare(
          "SELECT e.instance_seq,e.port_seq,e.bit,e.extras_seq FROM "
          "endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq "
          "JOIN pin_names p ON p.seq=e.port_seq WHERE e.net_seq=? AND e.role=1 "
          "ORDER BY coalesce(c.id,''),p.name,e.bit,e.ordinal");
      q.BindInteger(1, candidate.seq);
      while (q.Step()) {
        Stop(flag);
        downstream.push_back(
            {q.IsNull(0) ? std::optional<I>{} : q.TypedInteger(0),
             q.TypedInteger(1), q.TypedInteger(2), q.TypedInteger(3)});
      }
    }
    Check(I(downstream.size()) == candidate.sinks,
          "source endpoint count changed");
    {
      auto put = db.Prepare("INSERT INTO fanout_expected SELECT "
                            "?,instance_seq,port_seq,bit,extras_seq FROM "
                            "endpoint_records WHERE net_seq=? AND role=1");
      put.BindInteger(1, candidate.seq);
      put.BindInteger(2, candidate.seq);
      put.Execute();
    }
    {
      auto erase =
          db.Prepare("DELETE FROM endpoint_records WHERE net_seq=? AND role=1");
      erase.BindInteger(1, candidate.seq);
      erase.Execute();
    }
    I level = 0, buffer_count = 0;
    A widths;
    auto put = db.Prepare("INSERT INTO endpoint_records VALUES(?,?,?,?,?,?,?)");
    while (I(downstream.size()) > maximum) {
      std::vector<Sink> upstream;
      I group = 0;
      for (std::size_t offset = 0; offset < downstream.size();
           offset += std::size_t(maximum), ++group) {
        Stop(flag);
        Check(next_cell < INT64_MAX && next_net < INT64_MAX,
              "sequence overflow");
        const auto stem = Stem(candidate_index, level, group),
                   cell = stem + "$buf", net = stem + "$net";
        {
          auto q = db.Prepare("SELECT 1 FROM cell_records WHERE id=? UNION ALL "
                              "SELECT 1 FROM net_records WHERE id=?");
          q.BindText(1, cell);
          q.BindText(2, net);
          Check(!q.Step(), "buffer identifier collision");
        }
        const I attributes = Intern(
            db, "payloads", "value",
            R(O{{"emuflow_high_fanout_buffer", "transport-balanced-tree-v1"},
                {"emuflow_high_fanout_owner", candidate.id},
                {"emuflow_high_fanout_level", std::to_string(level)}})
                .EncodeUtf8());
        const I extras =
            Intern(db, "payloads", "value",
                   R(O{{"fanout_buffer_owner", candidate.id}}).EncodeUtf8());
        {
          auto q = db.Prepare(
              "INSERT INTO cell_records VALUES(?,?,NULL,1,'$lut',?,?,?,?,?)");
          q.BindInteger(1, next_cell);
          q.BindText(2, cell);
          q.BindInteger(3, resource);
          q.BindInteger(4, parameters);
          q.BindInteger(5, attributes);
          q.BindInteger(6, constants);
          q.BindInteger(7, empty);
          q.Execute();
        }
        const I count = std::min<I>(maximum, downstream.size() - offset);
        {
          auto q = db.Prepare("INSERT INTO net_records "
                              "VALUES(?,?,NULL,1,NULL,0,?,'combinational',?)");
          q.BindInteger(1, next_net);
          q.BindText(2, net);
          q.BindInteger(3, count);
          q.BindInteger(4, extras);
          q.Execute();
        }
        put.Reset();
        put.BindInteger(1, next_net);
        put.BindInteger(2, 0);
        put.BindInteger(3, 0);
        put.BindInteger(4, next_cell);
        put.BindInteger(5, output);
        put.BindInteger(6, 0);
        put.BindInteger(7, empty);
        put.Execute();
        for (I i = 0; i < count; ++i)
          SinkRow(put, next_net, i, downstream[offset + i]);
        {
          auto q = db.Prepare("INSERT INTO fanout_created VALUES(?,?,?,?)");
          q.BindInteger(1, candidate.seq);
          q.BindInteger(2, next_net);
          q.BindInteger(3, next_cell);
          q.BindInteger(4, level == 0);
          q.Execute();
        }
        upstream.push_back({next_cell, input, 0, empty});
        ++next_cell;
        ++next_net;
        ++added;
        ++buffer_count;
      }
      widths.emplace_back(group);
      downstream = std::move(upstream);
      ++level;
    }
    for (std::size_t i = 0; i < downstream.size(); ++i)
      SinkRow(put, candidate.seq, i, downstream[i]);
    {
      auto q = db.Prepare("UPDATE net_records SET fanout=? WHERE seq=?");
      q.BindInteger(1, downstream.size());
      q.BindInteger(2, candidate.seq);
      q.Execute();
    }
    largest = std::max(largest, candidate.sinks);
    records.emplace_back(O{{"net", candidate.id},
                           {"net_index", candidate.seq},
                           {"original_fanout", candidate.sinks},
                           {"root_fanout", downstream.size()},
                           {"levels", level},
                           {"level_buffer_counts", widths},
                           {"buffer_cells", buffer_count}});
  }
  if (!candidates.empty()) {
    // Compare multisets, including repeated top/pin facts and endpoint extras.
    db.Execute("CREATE TEMP VIEW fanout_leaf_facts AS SELECT "
               "c.root,e.instance_seq,e.port_seq,e.bit,e.extras_seq FROM "
               "fanout_created c JOIN endpoint_records e ON e.net_seq=c.net "
               "WHERE c.leaf=1 AND e.role=1");
    Empty(db,
          "SELECT root,instance_seq,port_seq,bit,extras_seq,count(*) FROM "
          "fanout_expected GROUP BY 1,2,3,4,5 EXCEPT SELECT "
          "root,instance_seq,port_seq,bit,extras_seq,count(*) FROM "
          "fanout_leaf_facts GROUP BY 1,2,3,4,5",
          "buffering lost or changed an original sink");
    Empty(db,
          "SELECT root,instance_seq,port_seq,bit,extras_seq,count(*) FROM "
          "fanout_leaf_facts GROUP BY 1,2,3,4,5 EXCEPT SELECT "
          "root,instance_seq,port_seq,bit,extras_seq,count(*) FROM "
          "fanout_expected GROUP BY 1,2,3,4,5",
          "buffering introduced or repeated an original sink");
    Empty(db,
          "SELECT 1 FROM fanout_created c LEFT JOIN endpoint_records e ON "
          "e.instance_seq=c.cell AND e.role=1 GROUP BY c.cell HAVING "
          "count(e.net_seq)!=1 LIMIT 1",
          "buffer input does not have one driver");
    Empty(db,
          "SELECT 1 FROM endpoint_records e LEFT JOIN cell_records c ON "
          "c.seq=e.instance_seq WHERE e.instance_seq IS NOT NULL AND c.seq IS "
          "NULL LIMIT 1",
          "orphan endpoint");
    auto q = db.Prepare(
        "SELECT 1 FROM net_records n JOIN fanout_created c ON c.net=n.seq "
        "WHERE n.fanout>? OR n.fanout!=(SELECT count(*) FROM endpoint_records "
        "e WHERE e.net_seq=n.seq AND e.role=1) LIMIT 1");
    q.BindInteger(1, maximum);
    Check(!q.Step(), "buffer net fanout disagrees");
    const O counts{
        {"cell_records", Count(db, "SELECT count(*) FROM cell_records")},
        {"net_records", Count(db, "SELECT count(*) FROM net_records")},
        {"endpoint_records",
         Count(db, "SELECT count(*) FROM endpoint_records")}};
    auto meta = db.Prepare(
        "UPDATE metadata SET value=? WHERE key='record_counts_json'");
    meta.BindText(1, R(counts).EncodeUtf8());
    meta.Execute();
    Check(Count(db, "SELECT count(*) FROM cell_records") ==
                  initial_cells + added &&
              Count(db, "SELECT count(*) FROM net_records") ==
                  initial_nets + added,
          "transformed cell/net census disagrees");
  }
  const auto maximum_result =
      Count(db, "SELECT coalesce(max(fanout),0) FROM net_records WHERE "
                "cut_class NOT IN ('clock','reset')");
  (void)original_max;
  Stop(flag);
  return R(O{{"schema", "emuflow.transport-fanout-buffering/v1"},
             {"status", "pass"},
             {"policy", policy},
             {"summary", O{{"buffered_nets", candidates.size()},
                           {"buffer_cells", added},
                           {"added_nets", added},
                           {"maximum_original_fanout", largest},
                           {"maximum_result_fanout", maximum_result}}},
             {"nets", records}})
      .EncodeUtf8();
}
} // namespace emuflow::core
