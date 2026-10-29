#include "timing/sta_projection.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/json_record.hpp"
#include "storage/schema/sta_path_schema.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <fcntl.h>
#include <fstream>
#include <optional>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using I = std::int64_t;
using D = BoundedJson;
using Raw = std::map<std::string, std::string>;
void Check(bool valid, const std::string &message) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact, "STA projection: " + message);
}
void Stop(const volatile std::sig_atomic_t *cancelled) {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed, "STA projection cancelled");
}
std::string Text(SqliteStatement &row, int column) {
  const auto value = row.TypedText(column);
  Check(!value.empty(), "empty identity");
  (void)JsonQuoteUtf8(value);
  return value;
}
std::string Array(const std::vector<std::string> &tokens) {
  std::string out = "[";
  for (const auto &token : tokens) {
    if (out.size() > 1)
      out += ',';
    out += token;
  }
  return out + "]";
}
struct Workspace {
  fs::path path;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".sta-projection-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create private workspace");
    path = name;
  }
  ~Workspace() {
    std::error_code error;
    fs::remove_all(path, error);
  }
};
struct StagedText {
  fs::path path;
  std::ofstream stream;
  explicit StagedText(const fs::path &target) {
    auto name =
        (target.parent_path() / ("." + target.filename().string() + ".XXXXXX"))
            .string();
    const int fd = mkstemp(name.data());
    Check(fd >= 0, "cannot create publication temporary");
    close(fd);
    path = name;
    stream.open(path, std::ios::binary | std::ios::trunc);
    Check(bool(stream), "cannot open publication temporary");
  }
  ~StagedText() {
    stream.close();
    std::error_code error;
    fs::remove(path, error);
  }
  void Close() {
    stream.flush();
    Check(bool(stream), "output write failed");
    stream.close();
    Check(!stream.fail(), "output close failed");
  }
};
I Scalar(SqliteDatabase &db, const std::string &sql) {
  auto row = db.Prepare(sql);
  Check(row.Step(), "missing SQL census");
  return row.TypedInteger(0);
}
std::string Meta(SqliteDatabase &db, const std::string &owner,
                 const char *key) {
  auto row = db.Prepare("SELECT value FROM " + owner + ".metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), std::string("missing metadata ") + key);
  auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return value;
}
void Attach(SqliteDatabase &db, const ArtifactRef &ref, const char *alias) {
  auto row = db.Prepare(std::string("ATTACH DATABASE ? AS ") + alias);
  row.BindText(1, SqliteImmutableUri(ref.path()));
  row.Execute();
}
// UTF-8 byte order agrees with Unicode scalar order. Tags/terminators preserve
// nested tuple order, including prefix strings and empty tuples. SQLite stores
// these keys as BLOBs, never as escaped JSON text whose punctuation changes
// order.
void StringKey(std::string &out, const std::string &value) {
  for (unsigned char byte : value) {
    out += char(byte);
    if (!byte)
      out += char(255);
  }
  out += char(0);
  out += char(0);
}
std::string TupleKey(const std::vector<std::string> &values) {
  std::string out;
  for (const auto &value : values) {
    out += char(1);
    StringKey(out, value);
  }
  out += char(0);
  return out;
}
struct Cut {
  std::string net, source;
  std::vector<std::string> sinks;
};
struct Norm {
  double positive, negative, period;
  explicit Norm(D &manifest) {
    D values(manifest.Token("normalization"));
    values.Keys({"positive_slack_scale_ns", "negative_slack_scale_ns",
                 "max_clock_period_ns"});
    positive = values.Number("positive_slack_scale_ns");
    negative = values.Number("negative_slack_scale_ns");
    period = values.Number("max_clock_period_ns");
    Check(positive > 0 && negative > 0 && period > 0,
          "normalization is invalid");
  }
  double Slack(double clock, double slack) const {
    const double value = slack >= 0 ? slack * clock / (positive * period)
                                    : slack / (negative * clock);
    Check(std::isfinite(value), "normalized slack is not finite");
    return value;
  }
};
std::string Configuration(const std::vector<ArtifactRef> &inputs) {
  Check(inputs.size() == 5, "input coverage is invalid");
  const char *kinds[] = {"sta-path-selector-v1", "timing-path-store-v1",
                         "assignment-index-v4"};
  for (unsigned i = 0; i < 3; ++i)
    Check(inputs[i].schema() == kinds[i], "input schema disagrees");
  Check(inputs[4].schema() == "sta-projection-request-v1",
        "request schema disagrees");
  Check(inputs[3].schema() == "assignment-selector-v4" ||
            (inputs[3].schema() == "assignment-index-v4" &&
             inputs[3].path() == inputs[2].path()),
        "assignment selector source disagrees");
  CheckedRequestReader reader(inputs[4].path(), 262144);
  Check(reader.Read<std::uint64_t>() == 0x31525053464d5545ULL &&
            reader.Read<std::uint32_t>() == 1,
        "request version disagrees");
  for (unsigned i = 0; i < 4; ++i)
    if (!(ReadFileGeneration(reader) == inputs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,
                       "STA projection source generation changed");
  auto config = reader.ReadString32(false, 131072);
  reader.RequireEnd();
  return config;
}
void ReadPaths(SqliteDatabase &db, D &manifest, const Norm &norm,
               const volatile std::sig_atomic_t *cancelled) {
  db.Execute(R"SQL(
    CREATE TABLE projected(ordinal INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,
      domain TEXT NOT NULL,period REAL NOT NULL,required REAL NOT NULL,
      delay REAL NOT NULL,normalized REAL NOT NULL,signature_key BLOB NOT NULL,
      cuts_key BLOB NOT NULL,transitions_key BLOB NOT NULL,record TEXT NOT NULL);
    CREATE TABLE covered(net TEXT PRIMARY KEY) WITHOUT ROWID;
    CREATE TEMP TABLE cut_refs AS SELECT c.cut_index,c.net_id,p.fpga AS source,
      (SELECT json_group_array(fpga) FROM
        (SELECT p2.fpga FROM assignment.cut_sinks s JOIN assignment.parts p2
          ON p2.part_index=s.sink_part_index WHERE s.net_id=c.net_id ORDER BY p2.fpga)) AS sinks
      FROM assignment.cuts c JOIN assignment.parts p ON p.part_index=c.source_part_index;
    CREATE UNIQUE INDEX cut_refs_net ON cut_refs(net_id);
  )SQL");
  Check(Scalar(db, "SELECT COUNT(*) FROM cut_refs") ==
            Scalar(db, "SELECT COUNT(*) FROM assignment.cuts"),
        "cut source identities are incomplete");
  Check(Scalar(db, "SELECT COUNT(*) FROM cut_refs") > 0,
        "projection requires partition cut nets");
  auto rows = db.Prepare(
      R"SQL(SELECT p.ordinal,p.path_id,p.clock_domain,p.clock_period_ns,p.slack_ns,p.fixed_delay_ns,
      p.start_object,p.end_object,sp.fpga,ep.fpga FROM paths.paths p
      LEFT JOIN assignment.copies sc ON sc.copy_id=p.start_instance AND sc.is_replica=0
      LEFT JOIN assignment.parts sp ON sp.part_index=sc.part_index
      LEFT JOIN assignment.copies ec ON ec.copy_id=p.end_instance AND ec.is_replica=0
      LEFT JOIN assignment.parts ep ON ep.part_index=ec.part_index ORDER BY p.ordinal)SQL");
  auto nets = db.Prepare(
      R"SQL(SELECT pn.path_ordinal,pn.net_ordinal,pn.net_id,c.cut_index,c.source,c.sinks
      FROM paths.path_nets pn LEFT JOIN cut_refs c ON c.net_id=pn.net_id
      ORDER BY pn.path_ordinal,pn.net_ordinal)SQL");
  auto insert =
      db.Prepare("INSERT INTO projected VALUES(?,?,?,?,?,?,?,?,?,?,?)");
  auto cover = db.Prepare("INSERT OR IGNORE INTO covered VALUES(?)");
  bool more = nets.Step();
  I count = 0;
  double positive = -1, negative = 0, maximum_period = 0;
  SqliteTransaction transaction(db);
  while (rows.Step()) {
    Stop(cancelled);
    const auto ordinal = rows.TypedInteger(0);
    Check(ordinal == count, "path ordinal population is invalid");
    ++count;
    const auto id = Text(rows, 1), clock = Text(rows, 2);
    const auto period = rows.TypedReal(3), slack = rows.TypedReal(4),
               delay = rows.TypedReal(5);
    Check(std::isfinite(period) && period > 0 && std::isfinite(slack) &&
              std::isfinite(delay) && delay >= 0,
          "path timing is invalid");
    const double required = delay + slack,
                 normalized = norm.Slack(period, slack);
    Check(std::isfinite(required), "required time is not finite");
    maximum_period = std::max(maximum_period, period);
    if (slack >= 0)
      positive = std::max(positive, slack);
    else
      negative = std::min(negative, slack);
    Check(!more || nets.TypedInteger(0) >= ordinal, "orphan path membership");
    std::set<std::string> unique;
    std::vector<Cut> candidates;
    I member = 0;
    while (more && nets.TypedInteger(0) == ordinal) {
      Check(nets.TypedInteger(1) == member++, "path net ordering is invalid");
      const auto net = Text(nets, 2);
      Check(unique.insert(net).second, "path nets are not unique");
      if (!nets.IsNull(3)) {
        Cut cut{net, Text(nets, 4), {}};
        for (const auto &token : D::Array(nets.TypedText(5)))
          cut.sinks.push_back(DecodeJsonString(token));
        Check(!cut.sinks.empty(), "cut has no remote sinks");
        candidates.push_back(std::move(cut));
      }
      more = nets.Step();
    }
    Check(member > 0, "path has no source nets");
    const bool chain = !rows.IsNull(6) && !rows.IsNull(7) && !rows.IsNull(9);
    std::vector<Cut> cuts;
    std::vector<std::array<std::string, 3>> transitions;
    if (chain) {
      auto target = Text(rows, 9);
      for (auto it = candidates.rbegin(); it != candidates.rend(); ++it) {
        if (it->source == target)
          continue;
        Check(std::find(it->sinks.begin(), it->sinks.end(), target) !=
                  it->sinks.end(),
              "path " + id + " partition " + target +
                  " cannot be reached through cut net " + it->net);
        transitions.push_back({it->net, it->source, target});
        cuts.push_back(*it);
        target = it->source;
      }
      Check(rows.IsNull(8) || Text(rows, 8) == target,
            "path " + id + " launch/capture partition chain is inconsistent");
      std::reverse(cuts.begin(), cuts.end());
      std::reverse(transitions.begin(), transitions.end());
    } else
      cuts = std::move(candidates);
    if (cuts.empty())
      continue;
    std::vector<std::string> cut_tokens, signature_tokens, cut_ids, signatures,
        transition_tokens;
    for (const auto &cut : cuts) {
      std::string signature = cut.source + "->";
      for (std::size_t i = 0; i < cut.sinks.size(); ++i) {
        if (i)
          signature += ',';
        signature += cut.sinks[i];
      }
      cut_ids.push_back(cut.net);
      signatures.push_back(signature);
      cut_tokens.push_back(JsonQuoteUtf8(cut.net));
      signature_tokens.push_back(JsonQuoteUtf8(signature));
      cover.Reset();
      cover.BindText(1, cut.net);
      cover.Execute();
    }
    std::string transition_key;
    for (const auto &transition : transitions) {
      transition_key += char(1);
      transition_key += TupleKey({transition[0], transition[1], transition[2]});
      transition_tokens.push_back(
          D::Encode({{"net", JsonQuoteUtf8(transition[0])},
                     {"from", JsonQuoteUtf8(transition[1])},
                     {"to", JsonQuoteUtf8(transition[2])}}));
    }
    transition_key += char(0);
    Raw record{{"id", JsonQuoteUtf8(id)},
               {"clock_domain", JsonQuoteUtf8(clock)},
               {"clock_period_ns", JsonRecord(period).Encode()},
               {"slack_ns", JsonRecord(slack).Encode()},
               {"fixed_delay_ns", JsonRecord(delay).Encode()},
               {"normalized_slack", JsonRecord(normalized).Encode()},
               {"cut_nets", Array(cut_tokens)},
               {"cut_signature", Array(signature_tokens)}};
    if (chain)
      record["cut_transitions"] = Array(transition_tokens);
    const auto signature_key = TupleKey(signatures),
               cut_key = TupleKey(cut_ids);
    insert.Reset();
    insert.BindInteger(1, ordinal);
    insert.BindText(2, id);
    insert.BindText(3, clock);
    insert.BindReal(4, period);
    insert.BindReal(5, required);
    insert.BindReal(6, delay);
    insert.BindReal(7, normalized);
    insert.BindBlob(8, signature_key.data(), signature_key.size());
    insert.BindBlob(9, cut_key.data(), cut_key.size());
    insert.BindBlob(10, transition_key.data(), transition_key.size());
    insert.BindText(11, D::Encode(record));
    insert.Execute();
  }
  Check(!more, "orphan path net membership");
  Check(count > 0, "path database is empty");
  D payloads(manifest.Token("payloads")), payload(payloads.Token("paths")),
      summary(manifest.Token("summary"));
  Check(count == payload.Integer("records") &&
            count == summary.Integer("paths"),
        "path publication count disagrees");
  Check(std::abs(norm.positive - (positive <= 0 ? 1 : positive)) <= 1e-12 &&
            std::abs(norm.negative - (negative == 0 ? 1 : -negative)) <=
                1e-12 &&
            std::abs(norm.period - maximum_period) <= 1e-12,
        "normalization does not match its paths");
  Check(Scalar(db, "SELECT COUNT(*) FROM projected") > 0,
        "path database has no path crossing this partition");
  transaction.Commit();
  db.Execute(R"SQL(CREATE TABLE representatives AS SELECT ordinal,members FROM
    (SELECT ordinal,ROW_NUMBER() OVER(PARTITION BY domain,period,required,signature_key,cuts_key,transitions_key
      ORDER BY delay DESC,normalized ASC,id DESC) AS rank,
      COUNT(*) OVER(PARTITION BY domain,period,required,signature_key,cuts_key,transitions_key) AS members
     FROM projected) WHERE rank=1;
    CREATE INDEX projected_group ON projected(domain,period,required,signature_key,cuts_key,transitions_key,id);
  )SQL");
  Check(Scalar(db, "SELECT COALESCE(SUM(members),0) FROM representatives") ==
            Scalar(db, "SELECT COUNT(*) FROM projected"),
        "compression member coverage disagrees");
}
void Emit(SqliteDatabase &db, std::ostream &out, D &manifest,
          const std::string &design, const std::string &source_hash,
          const volatile std::sig_atomic_t *cancelled) {
  const auto total = Scalar(db, "SELECT COUNT(*) FROM projected"),
             compressed = Scalar(db, "SELECT COUNT(*) FROM representatives");
  Raw root{
      {"schema", JsonQuote("emuflow.sta-paths/v1")},
      {"design", JsonQuoteUtf8(design)},
      {"source",
       D::Encode({{"provider", JsonQuote("partition-projected-sta-paths-v1")},
                  {"input_sha256", JsonQuote(source_hash)}})},
      {"normalization", manifest.Token("normalization")},
      {"compression",
       D::Encode(
           {{"original_paths", std::to_string(total)},
            {"compressed_paths", std::to_string(compressed)},
            {"lossless_by",
             JsonQuote(
                 "clock-domain+period+required-time+ordered-cut-signature+cut-"
                 "net-sequence+member-sink-transitions/max-fixed-delay")}})},
      {"paths", "[]"}};
  auto rows = db.Prepare("SELECT "
                         "p.record,p.domain,p.period,p.required,p.signature_"
                         "key,p.cuts_key,p.transitions_key,"
                         "r.members FROM representatives r JOIN projected p ON "
                         "p.ordinal=r.ordinal ORDER BY "
                         "p.domain,p.period,p.required,p.signature_key,p.cuts_"
                         "key,p.transitions_key");
  auto members = db.Prepare(
      "SELECT id,delay,normalized FROM projected WHERE domain=? AND period=? "
      "AND required=? AND "
      "signature_key=? AND cuts_key=? AND transitions_key=? ORDER BY id");
  out << '{';
  bool first = true;
  I paths = 0, member_count = 0;
  for (const auto &field : root) {
    if (!first)
      out << ',';
    first = false;
    out << JsonQuoteUtf8(field.first) << ':';
    if (field.first != "paths") {
      out << field.second;
      continue;
    }
    out << '[';
    while (rows.Step()) {
      Stop(cancelled);
      if (paths++)
        out << ',';
      // Data records use the record cursor, not the bounded control parser.
      Raw record;
      VisitJsonObjectTokens(
          rows.TypedText(0), [&](const auto &key, const auto &token) {
            Check(record.size() < 16 && record.emplace(key, token).second,
                  "invalid representative record");
          });
      auto fields = record;
      fields["compressed_path_ids"] = "[]";
      out << '{';
      bool first_field = true;
      for (const auto &item : fields) {
        if (!first_field)
          out << ',';
        first_field = false;
        out << JsonQuoteUtf8(item.first) << ':';
        if (item.first != "compressed_path_ids") {
          out << item.second;
          continue;
        }
        members.Reset();
        members.BindText(1, rows.TypedText(1));
        members.BindReal(2, rows.TypedReal(2));
        members.BindReal(3, rows.TypedReal(3));
        for (int i = 4; i <= 6; ++i) {
          const auto key = rows.Blob(i);
          members.BindBlob(i, key.data(), key.size());
        }
        out << '[';
        I count = 0;
        std::optional<std::tuple<double, double, std::string>> best;
        while (members.Step()) {
          Stop(cancelled);
          if (count++)
            out << ',';
          const auto id = Text(members, 0);
          const auto choice =
              std::make_tuple(members.TypedReal(1), -members.TypedReal(2), id);
          if (!best || choice > *best)
            best = choice;
          out << JsonQuoteUtf8(id);
        }
        out << ']';
        Check(count == rows.TypedInteger(7),
              "compressed member population disagrees");
        // Independent C++ dominance audit of the SQL-selected representative.
        Check(best && std::get<2>(*best) == DecodeJsonString(record.at("id")) &&
                  std::get<0>(*best) ==
                      ParseFiniteDecimal(record.at("fixed_delay_ns"),
                                         "representative delay") &&
                  -std::get<1>(*best) ==
                      ParseFiniteDecimal(record.at("normalized_slack"),
                                         "representative normalized slack"),
              "representative does not dominate its original members");
        member_count += count;
      }
      out << '}';
    }
    out << ']';
  }
  out << "}\n";
  Check(paths == compressed && member_count == total,
        "emitted representative/member census disagrees");
}
} // namespace
StageResult ProjectStaPaths(const std::vector<ArtifactRef> &inputs,
                            const std::string &output,
                            const std::string &summary,
                            const volatile std::sig_atomic_t *cancelled) {
  std::optional<std::string> configuration;
  return RunStage(
      "timing.project-paths",
      [&] {
        configuration = Configuration(inputs);
        return inputs;
      },
      [&](const auto &refs) {
        Stop(cancelled);
        D options(*configuration);
        options.Keys({"verify_payload", "output"});
        const fs::path target = fs::absolute(output),
                       report = fs::absolute(summary);
        Check(!fs::is_symlink(target) && !fs::is_symlink(report),
              "output aliases a link");
        Check(target != report && fs::is_directory(target.parent_path()) &&
                  fs::is_directory(report.parent_path()),
              "invalid output directories");
        for (const auto &ref : refs)
          Check(fs::weakly_canonical(target) != fs::path(ref.path()) &&
                    fs::weakly_canonical(report) != fs::path(ref.path()),
                "output aliases a source");
        D manifest(D::Read(refs[0]));
        Check(manifest.Text("schema") == "emuflow.sta-path-database/v3",
              "only current indexed TimingPathDB is supported");
        D payloads(manifest.Token("payloads")),
            payload(payloads.Token("paths"));
        const fs::path member = payload.Text("path");
        Check(!member.empty() && member == member.filename() && member != "." &&
                  member != ".." &&
                  payload.Text("format") == "sqlite-sta-path/v1" &&
                  fs::canonical(fs::path(refs[0].path()).parent_path() /
                                member) == fs::path(refs[1].path()),
              "path payload owner disagrees");
        if (options.Boolean("verify_payload"))
          Check(payload.Text("sha256") == Sha256File(refs[1], cancelled),
                "path payload seal disagrees");
        SqliteStore source_paths(refs[1]), source_assignment(refs[2]);
        RequireSqliteDDL(source_paths.database(), kStaPathStoreDDL);
        const auto design = DecodeJsonString(
            Meta(source_assignment.database(), "main", "design"));
        Check(design == manifest.Text("design"),
              "path database design does not match assignment");
        if (refs[3].schema() == "assignment-selector-v4") {
          D selected(D::Read(refs[3]));
          Check(selected.Text("schema") ==
                    "emuflow.phase3-assignment-storage/v4",
                "assignment selector version disagrees");
          D document(selected.Token("document")),
              descriptor(selected.Token("cut_nets_index"));
          Check(document.Text("schema") == "emuflow.partition-assignment/v1" &&
                    document.Text("design") == design,
                "assignment selector namespace disagrees");
          const fs::path member = descriptor.Text("artifact");
          Check(!member.empty() && member == member.filename() &&
                    member != "." && member != ".." &&
                    fs::canonical(fs::path(refs[3].path()).parent_path() /
                                  member) == fs::path(refs[2].path()),
                "assignment payload owner disagrees");
          Check(descriptor.Text("sha256") ==
                    DecodeJsonString(Meta(source_assignment.database(), "main",
                                          "cut_records_sha256")),
                "assignment cut seal disagrees");
          Check(descriptor.Integer("records") ==
                    Scalar(source_assignment.database(),
                           "SELECT COUNT(*) FROM cuts"),
                "assignment cut count disagrees");
        }

        Workspace work(target.parent_path());
        SqliteDatabase db((work.path / "projection.sqlite").string(),
                          SqliteDatabase::Mode::kCreateExclusive);
        db.Execute("PRAGMA cache_size=-16384;PRAGMA "
                   "temp.cache_size=-16384;PRAGMA temp_store=FILE");
        Attach(db, refs[1], "paths");
        Attach(db, refs[2], "assignment");
        Norm norm(manifest);
        ReadPaths(db, manifest, norm, cancelled);
        StagedText artifact(target), summary_file(report);
        Emit(db, artifact.stream, manifest, design,
             Sha256File(refs[0], cancelled), cancelled);
        const auto cuts = Scalar(db, "SELECT COUNT(*) FROM cut_refs"),
                   covered = Scalar(db, "SELECT COUNT(*) FROM covered");
        Raw result{
            {"status", JsonQuote("pass")},
            {"design", JsonQuoteUtf8(design)},
            {"database_paths",
             std::to_string(Scalar(db, "SELECT COUNT(*) FROM paths.paths"))},
            {"projected_paths",
             std::to_string(Scalar(db, "SELECT COUNT(*) FROM projected"))},
            {"compressed_paths",
             std::to_string(
                 Scalar(db, "SELECT COUNT(*) FROM representatives"))},
            {"cut_nets", std::to_string(cuts)},
            {"covered_cut_nets", std::to_string(covered)},
            {"uncovered_cut_nets", std::to_string(cuts - covered)},
            {"output", JsonQuoteUtf8(options.Text("output"))}};
        summary_file.stream
            << D::Encode(
                   {{"schema", JsonQuote("emuflow.sta-projection-report/v1")},
                    {"status", JsonQuote("pass")},
                    {"implementation_id",
                     JsonQuote("native-sta-path-projection-v1")},
                    {"summary", D::Encode(result)}})
            << '\n';
        artifact.Close();
        summary_file.Close();
        Stop(cancelled);
        PublishArtifactGroup(
            {{artifact.path, target}, {summary_file.path, report}}, work.path,
            refs, [&] { Stop(cancelled); });
        source_paths.RequireUnchanged();
        source_assignment.RequireUnchanged();
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "system-routing-paths-v1"),
            ArtifactRef::Bind(summary, "sta-projection-summary-v1")};
      });
}
} // namespace emuflow::core
