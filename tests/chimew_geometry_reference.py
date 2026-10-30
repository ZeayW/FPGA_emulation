"""Independent fixture oracle for selected physical endpoint coordinates."""
from emuflow_tools.common.streaming_json import StreamingJSONObjectReader
from contextlib import ExitStack
from collections import OrderedDict
from pathlib import Path
import sqlite3
from emuflow_tools.timing.boundary_identity_storage import read_boundary_identity_storage
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.physical.vivado.vivado_lineage import LINEAGE_SCHEMA, PLACEMENT_COORDINATE_SYSTEM
from emuflow_tools.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry

class XilinxChimewGeometry:
    """A leased, indexed query session; memory does not grow with placed cells."""

    def __init__(self, source: Path, platform, report, architecture: Path, *, package_geometry=None):
        from emuflow_tools.transport.chimew.chimew_fixed_wiring import FixedWiringChannels
        self.stack = ExitStack()
        self.databases = {}
        self.generations = {}
        self.sources = []
        self.bounds = {}
        self.location_sql = {}
        self.architecture_generation = native_file_generation(architecture)
        self.fixed = FixedWiringChannels(platform)
        self.pins = {}
        self.forwarded = 0
        self.source_points = OrderedDict()
        self.site_seals = {}
        try:
            parts = {f.part for f in platform.fpgas}
            if len(parts) != 1:
                raise ValidationError("Chimew lookahead requires one shared device geometry")
            catalog = package_geometry or XilinxPackageGeometry.open(architecture,next(iter(parts)))
            if (not isinstance(catalog,XilinxPackageGeometry) or catalog.path != architecture.resolve()
                    or catalog.part != next(iter(parts))):
                raise ValidationError("Chimew package geometry belongs to a different device artifact")
            catalog.require_unchanged()
            pins = catalog.pins
            self.architecture = {"path": str(catalog.path), "sha256": catalog.sha256}
            self.generations[catalog.path] = catalog.generation
            # All eligible conductors must agree with the fixed board owner.
            for wire in self.fixed.board["wires"]:
                for endpoint in wire["endpoints"]:
                    for polarity in ("p", "n"):
                        pin = endpoint[polarity]
                        actual = pins.get(pin["package_pin"])
                        if actual is None or actual[0] != pin["site"]:
                            raise ValidationError("Chimew device geometry differs from fixed P/N wiring")
                        self.pins[endpoint["fpga"], pin["package_pin"]] = actual[1:]
            records = report.get("fpgas")
            if not isinstance(records, list):
                raise ValidationError("Chimew physical prepass lacks FPGA records")
            expected = {f.id for f in platform.fpgas}
            seen_fpgas = set()
            for record in records:
                fpga = record.get("fpga")
                if fpga not in expected or fpga in seen_fpgas or record.get("status") != "pass":
                    raise ValidationError("Chimew physical prepass FPGA coverage is invalid")
                seen_fpgas.add(fpga)
                # Empty partitions cannot own a scheduled endpoint.
                if record.get("active") is False:
                    xy = [point for (owner, _pin), point in self.pins.items() if owner == fpga]
                    self.bounds[fpga] = (min(v[0] for v in xy),max(v[0] for v in xy),
                                         min(v[1] for v in xy),max(v[1] for v in xy))
                    self.sources.append({"fpga": fpga, "active": False})
                    continue
                stages = record["stages"]
                boundary = read_boundary_identity_storage(
                    Path(stages["placement_ir"]["boundary_identity"]["output"]))
                self.stack.callback(boundary.close)
                if boundary["fpga"] != fpga or boundary["platform"] != platform.name:
                    raise ValidationError("Chimew boundary identity belongs to a different FPGA")
                boundary_path = boundary.store_path
                if "vivado_implementation" in stages:
                    producer = stages["vivado_implementation"]
                    mapped = stages["mapped_verilog"]["mapped_index"]
                    physical = {"path": producer["cell_coverage"]["output"],
                                "sha256": producer["cell_coverage"]["sha256"]}
                    db = self._open(Path(physical["path"]))
                    metadata = dict(db.execute("SELECT key,value FROM metadata"))
                    if (db.execute("PRAGMA application_id").fetchone()[0] != 0x454D564C
                            or db.execute("PRAGMA user_version").fetchone()[0] != 2
                            or metadata.get("schema") != LINEAGE_SCHEMA
                            or metadata.get("coordinate_system") != PLACEMENT_COORDINATE_SYSTEM
                            or metadata.get("mapped_index_sha256") != mapped["sha256"]
                            or metadata.get("retained_stage") != "routed"):
                        raise ValidationError("Chimew Vivado placement/source identity disagrees")
                    self._attach(db, Path(mapped["path"]), "mapped")
                    self.location_sql[fpga] = """SELECT avg(s.tile_{axis}) FROM mapped.cells cell
                        JOIN objects o ON o.origin=cell.seq AND o.stage='routed'
                        JOIN sites s ON s.seq=o.site_seq WHERE cell.id={instance}"""
                    bounds = db.execute("SELECT min(tile_column),max(tile_column),min(tile_row),max(tile_row) FROM sites WHERE stage='routed'").fetchone()
                elif "rapidwright_implementation" in stages:
                    producer = stages["rapidwright_implementation"]
                    artifacts = producer["result"]["artifacts"]
                    mapped, physical = artifacts["mapped"], artifacts["physical_handoff"]
                    db = self._open(Path(physical["path"]))
                    if (db.execute("PRAGMA application_id").fetchone()[0] != 0x454D5550
                            or db.execute("PRAGMA user_version").fetchone()[0] != 1):
                        raise ValidationError("Chimew requires the typed Xilinx physical handoff")
                    header = db.execute("SELECT value_json FROM document WHERE name='packed'").fetchone()
                    import json
                    if (header is None
                            or json.loads(header[0]).get("source", {}).get("mapped_json_sha256") != mapped["sha256"]
                            or json.loads(header[0]).get("source", {}).get("architecture_sha256") != self.architecture["sha256"]):
                        raise ValidationError("Chimew OpenPARF placement/source identity disagrees")
                    site_ref = producer["placement"]["site_database"]
                    site_path = Path(site_ref["path"])
                    now = native_file_generation(site_path)
                    expected_generation = site_ref.get("generation")
                    digest = site_ref.get("sha256")
                    if (site_ref.get("schema") != "emuflow.openparf-atomic-site-database/v1"
                            or not isinstance(expected_generation,list) or len(expected_generation)!=7
                            or any(isinstance(v,bool) or not isinstance(v,int) for v in expected_generation)
                            or expected_generation[:5] != list(now[:5])
                            or not isinstance(digest,str) or len(digest)!=64
                            or any(c not in "0123456789abcdef" for c in digest)):
                        raise ValidationError("Chimew OpenPARF site map generation differs from its producer")
                    if expected_generation != list(now):
                        # Managed hard-link publication legitimately changes
                        # ctime. Require the producer's original byte seal,
                        # checking each current inode generation only once.
                        if now not in self.site_seals:
                            actual = file_sha256(site_path)
                            if native_file_generation(site_path) != now:
                                raise ValidationError("Chimew site map changed while checking its content seal")
                            self.site_seals[now] = actual
                        if self.site_seals[now] != digest:
                            raise ValidationError("Chimew OpenPARF site map content seal disagrees")
                    self._attach(db, site_path, "geometry")
                    site_metadata = dict(db.execute("SELECT key,value FROM geometry.metadata"))
                    if (site_metadata.get("coordinate_system") != PLACEMENT_COORDINATE_SYSTEM
                            or site_metadata.get("architecture_sha256") != self.architecture["sha256"]
                            or db.execute("SELECT 1 FROM geometry.sqlite_master WHERE type='index' AND name='physical_sites_name'").fetchone() is None):
                        raise ValidationError("Chimew OpenPARF site map lacks native tile coordinates")
                    self.location_sql[fpga] = """SELECT min(s.physical_{axis}) FROM assignment a
                        JOIN cluster pc ON pc.ordinal=a.cluster_ordinal
                        JOIN geometry.physical_sites p ON p.physical_site=pc.site
                        JOIN geometry.sites s ON s.dense_x=p.dense_x AND s.dense_y=p.dense_y
                        WHERE a.instance={instance}
                        GROUP BY a.instance
                        HAVING min(s.physical_x)=max(s.physical_x) AND min(s.physical_y)=max(s.physical_y)"""
                    bounds = db.execute("SELECT min(physical_x),max(physical_x),min(physical_y),max(physical_y) FROM geometry.sites").fetchone()
                else:
                    raise ValidationError("Chimew lookahead requires an indexed Xilinx physical backend")
                self._attach(db, source, "source")
                if (db.execute("PRAGMA source.application_id").fetchone()[0] != 0x454D5549
                        or db.execute("PRAGMA source.user_version").fetchone()[0] != 2):
                    raise ValidationError("Chimew requires the compact authoritative EmuIR owner")
                self._attach(db, boundary_path, "boundary")
                self.databases[fpga] = db
                if any(not isinstance(v,int) or v < 0 for v in bounds):
                    raise ValidationError("Chimew physical prepass has no native placed source objects")
                xy = [point for (owner, _pin), point in self.pins.items() if owner == fpga]
                self.bounds[fpga] = (min(bounds[0], min(v[0] for v in xy)),
                                     max(bounds[1], max(v[0] for v in xy)),
                                     min(bounds[2], min(v[1] for v in xy)),
                                     max(bounds[3], max(v[1] for v in xy)))
                self.sources.append({"fpga": fpga, "mapped": mapped, "placement": physical,
                                     **({"site_database": site_ref} if "rapidwright_implementation" in stages else {}),
                                     "boundary": {"path": str(boundary_path),
                                                  "sha256": boundary.selector["store_sha256"]}})
            if seen_fpgas != expected:
                raise ValidationError("Chimew requires a placed lookahead for every selected FPGA")
        except BaseException:
            self.close()
            raise

    def _open(self, path):
        db = sqlite3.connect(immutable_sqlite_uri(path), uri=True)
        self.stack.callback(db.close)
        generation = native_file_generation(path)
        if path in self.generations and self.generations[path] != generation:
            raise ValidationError("Chimew input generation changed while binding owners")
        self.generations[path] = generation
        return db

    def _attach(self, db, path, name):
        db.execute(f"ATTACH DATABASE ? AS {name}", (immutable_sqlite_uri(path),))
        generation = native_file_generation(path)
        if path in self.generations and self.generations[path] != generation:
            raise ValidationError("Chimew input generation changed while binding owners")
        self.generations[path] = generation

    def point(self, fpga, net, entry, kind):
        """Average source endpoints; forwarded hops use their actual registers."""
        if kind not in {"tx","rx"}:
            raise ValidationError("Chimew endpoint direction is invalid")
        if fpga not in self.databases:
            raise ValidationError("Chimew active hop belongs to an inactive FPGA")
        db = self.databases[fpga]
        # Vivado expressions use tile_column/tile_row; OpenPARF uses physical_x/y.
        if "tile_" in self.location_sql[fpga]:
            x = self.location_sql[fpga].format(axis="column", instance="c.id")
            y = self.location_sql[fpga].format(axis="row", instance="c.id")
        else:
            x = self.location_sql[fpga].format(axis="x", instance="c.id")
            y = self.location_sql[fpga].format(axis="y", instance="c.id")
        key = fpga,net,kind
        row = self.source_points.pop(key,None)
        if row is None:
            row = db.execute(f"""SELECT avg(({x})),avg(({y})) FROM source.net_records n
                JOIN source.endpoint_records e ON e.net_seq=n.seq AND e.role=?
                JOIN source.cell_records c ON c.seq=e.instance_seq WHERE n.id=?""",
                (0 if kind == "tx" else 1, net)).fetchone()
        if row[0] is not None:
            self.source_points[key] = row
            if len(self.source_points)>4096:
                self.source_points.popitem(last=False)
        if row[0] is None:
            bx, by = x.replace("c.id", "b.instance"), y.replace("c.id", "b.instance")
            row = db.execute(f"""SELECT avg(({bx})),avg(({by})),count(({bx})),count(*)
                FROM boundary.endpoints e JOIN boundary.boundary_registers b ON b.endpoint_id=e.id
                WHERE e.schedule_entry=? AND e.kind=? AND e.logical_net=?""", (entry, kind, net)).fetchone()
            if row[0] is None or row[2] != row[3]:
                raise ValidationError(f"Chimew active {fpga}/{entry}/{kind} lacks placed source or boundary coverage")
            self.forwarded += 1
        x, y = float(row[0]), float(row[1])
        low, high = self.bounds[fpga][2:]
        return x, y, 0.5 if low == high else (y-low)/(high-low)

    def close(self):
        self.stack.close()
        self.source_points.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                for path, generation in self.generations.items():
                    if native_file_generation(path) != generation:
                        raise ValidationError("Chimew physical input generation changed during lookahead")
        finally:
            self.close()



def read_package_geometry_reference(path: Path, part: str) -> dict[str, tuple[str, int, int]]:
    """Read only the part and package catalog, skipping the device site graph."""
    generation = native_file_generation(path)
    pins: dict[str, tuple[str, int, int]] = {}
    seen = set()
    actual_part = None
    schema, transform, model = None, None, None
    with path.open(encoding="utf-8") as stream:
        reader = StreamingJSONObjectReader(stream)
        for key in reader.object_keys():
            if key in seen:
                raise ValidationError("Chimew ArchitectureDB repeats a root field")
            seen.add(key)
            if key == "schema":
                schema = reader.read_value()
            elif key == "coordinate_transform":
                transform = reader.read_value()
            elif key == "physical_region_model":
                model = reader.read_value()
            elif key == "part":
                actual_part = reader.read_value()
            elif key == "physical_regions":
                for region_key in reader.object_keys():
                    if region_key != "io_banks":
                        reader.skip_value()
                        continue
                    banks = reader.read_value()  # Device-sized, never design-sized.
                    if not isinstance(banks, list) or not 0 < len(banks) <= 256:
                        raise ValidationError("Chimew native package bank catalog is invalid")
                    for bank in banks:
                        if not isinstance(bank, dict) or not isinstance(bank.get("pins"), list):
                            raise ValidationError("Chimew native package bank is invalid")
                        for pin in bank["pins"]:
                            if not isinstance(pin,dict):
                                raise ValidationError("Chimew native package pin is invalid")
                            name, site, tile = pin.get("package_pin"), pin.get("site"), pin.get("native_tile")
                            if (not isinstance(name, str) or not name or name in pins
                                    or not isinstance(site, str) or not site or not isinstance(tile, dict)
                                    or tile.get("coordinate_system") != PLACEMENT_COORDINATE_SYSTEM):
                                raise ValidationError("Chimew package pin lacks unique native tile geometry")
                            xy = tile.get("column"), tile.get("row")
                            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in xy):
                                raise ValidationError("Chimew package pin native tile coordinate is invalid")
                            pins[name] = site, xy[0], xy[1]
                            if len(pins) > 16384:
                                raise ValidationError("Chimew package pin catalog exceeds device bounds")
            else:
                reader.skip_value()
        reader.finish()
    if (schema != "emuflow.archdb/v1" or not isinstance(transform, dict)
            or transform.get("name") != "tile-grid-with-site-stride-v1"
            or not isinstance(model, dict) or model.get("io_bank_encoded") is not True
            or not isinstance(model.get("overlay"),dict)
            or model["overlay"].get("schema") != "emuflow.physical-region-sidecar/v1"
            or not isinstance(model["overlay"].get("sha256"),str)
            or len(model["overlay"]["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in model["overlay"]["sha256"])
            or actual_part != part or not pins or native_file_generation(path) != generation):
        raise ValidationError("Chimew ArchitectureDB part, geometry or generation disagrees")
    return pins
