/*
 * Build and route an explicitly placed UltraScale+ design with RWRoute.
 *
 * The input is a compact, tab-separated EmuFlow contract.  This adapter emits
 * a compact JSON route certificate; the Python checker independently rebuilds
 * every routed source-to-sink path and detects resource conflicts.
 */
import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.SiteInst;
import com.xilinx.rapidwright.design.SitePinInst;
import com.xilinx.rapidwright.design.Unisim;
import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.edif.EDIFCell;
import com.xilinx.rapidwright.rwroute.RWRoute;
import com.xilinx.rapidwright.timing.TimingModel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONObject;

public final class EmuFlowRWRoute {
    private static final String SCHEMA = "emuflow.xilinx-route-db/v1";
    private static final String[] DSP48E2_COMPONENTS = new String[] {
        "DSP_PREADD_DATA", "DSP_A_B_DATA", "DSP_C_DATA", "DSP_MULTIPLIER",
        "DSP_ALU", "DSP_M_DATA", "DSP_OUTPUT", "DSP_PREADD"
    };

    private static final class MaterializedCell {
        final String logicalType;
        final Cell regularCell;
        final SiteInst siteInst;
        final int physicalCells;
        final Map<String, String> parameters;

        MaterializedCell(
            String logicalType, Cell regularCell, SiteInst siteInst, int physicalCells,
            Map<String, String> parameters
        ) {
            this.logicalType = logicalType;
            this.regularCell = regularCell;
            this.siteInst = siteInst;
            this.physicalCells = physicalCells;
            this.parameters = parameters;
        }

        boolean isTransformedDSP48E2() {
            return logicalType.equals("DSP48E2");
        }

        boolean isBlockRam() {
            return logicalType.equals("RAMB18E2") || logicalType.equals("RAMB36E2");
        }
    }

    private static String sha256(Path path) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(path));
        StringBuilder value = new StringBuilder();
        for (byte item : digest) value.append(String.format("%02x", item));
        return value.toString();
    }

    private static String nodeName(Node node) {
        return node == null ? null : node.toString();
    }

    private static JSONObject pinRecord(SitePinInst pin) {
        JSONObject value = new JSONObject();
        value.put("site", pin.getSiteInstName());
        value.put("pin", pin.getName());
        value.put("is_output", pin.isOutPin());
        value.put("node", JSONObject.NULL);
        String node = nodeName(pin.getConnectedNode());
        if (node != null) value.put("node", node);
        return value;
    }

    private static JSONObject pipRecord(PIP pip) {
        JSONObject value = new JSONObject();
        value.put("tile", pip.getTile().getName());
        value.put("start_wire", pip.getStartWireName());
        value.put("end_wire", pip.getEndWireName());
        value.put("start_node", JSONObject.NULL);
        value.put("end_node", JSONObject.NULL);
        Node physicalStart = pip.isReversed() ? pip.getEndNode() : pip.getStartNode();
        Node physicalEnd = pip.isReversed() ? pip.getStartNode() : pip.getEndNode();
        String start = nodeName(physicalStart);
        String end = nodeName(physicalEnd);
        if (start != null) value.put("start_node", start);
        if (end != null) value.put("end_node", end);
        value.put("bidirectional", pip.isBidirectional());
        value.put("reversed", pip.isReversed());
        return value;
    }

    private static int integerParameter(MaterializedCell cell, String name) {
        String value = cell.parameters.get(name);
        if (value == null) return 0;
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException error) {
            throw new IllegalStateException(
                cell.logicalType + " parameter " + name + " is not an integer: " + value,
                error
            );
        }
    }

    private static List<String> hardBlockPhysicalPins(
        MaterializedCell materialized, String logicalPin
    ) {
        Cell cell = materialized.regularCell;
        String type = materialized.logicalType;
        String physicalPin = logicalPin.replace("[", "").replace("]", "");
        List<String> result = new ArrayList<>();
        if (type.equals("RAMB18E2")) {
            if (cell.getBEL().getPin(physicalPin) != null) result.add(physicalPin);
            return result;
        }
        if (!type.equals("RAMB36E2")) return result;

        int open = logicalPin.indexOf('[');
        int close = logicalPin.indexOf(']');
        String port = open < 0 ? logicalPin : logicalPin.substring(0, open);
        Integer index = open < 0 || close < open
            ? null : Integer.valueOf(logicalPin.substring(open + 1, close));
        if (index != null && (port.equals("ADDRARDADDR") || port.equals("ADDRBWRADDR"))) {
            String prefix = port.equals("ADDRARDADDR") ? "ADDRARDADDR" : "ADDRBWRADDR";
            result.add(prefix + "L" + index);
            result.add(prefix + "U" + index);
        } else if (index != null && port.equals("WEA")) {
            if (index < 0 || index > 3) {
                throw new IllegalStateException("RAMB36E2 WEA index is out of range");
            }
            result.add("WEAL" + index);
            result.add("WEAU" + index);
        } else if (index != null && port.equals("WEBWE")) {
            if (index < 0 || index > 7) {
                throw new IllegalStateException("RAMB36E2 WEBWE index is out of range");
            }
            boolean simpleDualPort = integerParameter(materialized, "WRITE_WIDTH_B") > 36;
            if (simpleDualPort) {
                result.add((index < 4 ? "WEBWEL" : "WEBWEU") + (index % 4));
            } else {
                if (index > 3) {
                    throw new IllegalStateException(
                        "RAMB36E2 TDP mode cannot use WEBWE[" + index + "]"
                    );
                }
                result.add("WEBWEL" + index);
                result.add("WEBWEU" + index);
            }
        } else if (port.equals("CLKARDCLK") || port.equals("CLKBWRCLK")) {
            result.add(port + "L");
            result.add(port + "U");
        } else if (port.equals("RSTREGB")) {
            result.add("RSTREGBL");
            result.add("RSTREGBU");
        } else if (cell.getBEL().getPin(physicalPin) != null) {
            result.add(physicalPin);
        }
        return result;
    }

    private static void ensureLogicalPinMapping(
        MaterializedCell materialized, String logicalPin
    ) {
        Cell cell = materialized.regularCell;
        if (cell.getPinMappingsL2P().containsKey(logicalPin)) return;
        List<String> physicalPins = hardBlockPhysicalPins(materialized, logicalPin);
        if (physicalPins.isEmpty()) {
            String physicalPin = logicalPin.replace("[", "").replace("]", "");
            if (cell.getBEL().getPin(physicalPin) != null) physicalPins.add(physicalPin);
        }
        if (physicalPins.isEmpty()) {
            throw new IllegalStateException(
                "logical pin has no BEL pin: " + cell.getName() + "/" + logicalPin
            );
        }
        for (String physicalPin : physicalPins) {
            if (cell.getBEL().getPin(physicalPin) == null) {
                throw new IllegalStateException(
                    "logical pin maps to absent BEL pin: " + cell.getName() + "/"
                    + logicalPin + " -> " + physicalPin
                );
            }
            String existing = cell.getLogicalPinMapping(physicalPin);
            if (existing != null && !existing.equals("GND") && !existing.equals("VCC")) {
                throw new IllegalStateException(
                    "physical pin mapping conflict on " + cell.getName() + "/" + physicalPin
                    + ": " + existing + " vs " + logicalPin
                );
            }
            if (existing != null) cell.removePinMapping(physicalPin);
            cell.addPinMapping(physicalPin, logicalPin);
        }
        if (!cell.getPinMappingsL2P().containsKey(logicalPin)) {
            throw new IllegalStateException(
                "failed to materialize pin mapping " + cell.getName() + "/" + logicalPin
            );
        }
    }

    private static String dsp48e2SitePin(String logicalPin) {
        String physicalPin = logicalPin.replace("[", "").replace("]", "");
        if (physicalPin.startsWith("D") && logicalPin.startsWith("D[")) {
            physicalPin = "DIN" + physicalPin.substring(1);
        } else if (physicalPin.startsWith("ACOUT")) {
            physicalPin = physicalPin.replace("ACOUT", "ACOUT_B");
        } else if (physicalPin.startsWith("BCOUT")) {
            physicalPin = physicalPin.replace("BCOUT", "BCOUT_B");
        } else if (physicalPin.startsWith("PATTERNBDETECT")) {
            physicalPin = physicalPin.replace("PATTERNBDETECT", "PATTERN_B_DETECT");
        } else if (physicalPin.startsWith("PATTERNDETECT")) {
            physicalPin = physicalPin.replace("PATTERNDETECT", "PATTERN_DETECT");
        }
        return physicalPin;
    }

    private static MaterializedCell materializeCell(
        Design design, String safeName, String[] row, Map<String, String> parameters
    ) {
        String logicalType = row[3];
        if (!logicalType.equals("DSP48E2")) {
            List<String> propertyPairs = new ArrayList<>();
            List<String> propertyNames = new ArrayList<>(parameters.keySet());
            propertyNames.sort(String::compareTo);
            for (String name : propertyNames) {
                propertyPairs.add(name);
                propertyPairs.add(parameters.get(name));
            }
            Cell cell = design.createAndPlaceCell(
                safeName, Unisim.valueOf(logicalType), row[4] + "/" + row[5],
                propertyPairs.toArray(new String[0])
            );
            if (cell == null) throw new IllegalStateException("failed to place " + safeName);
            return new MaterializedCell(
                logicalType, cell, cell.getSiteInst(), 1, parameters
            );
        }

        Site site = design.getDevice().getSite(row[4]);
        if (site == null || !site.getSiteTypeEnum().name().equals("DSP48E2")) {
            throw new IllegalStateException("DSP48E2 has invalid site " + row[4]);
        }
        if (!row[5].equals("DSP_ALU")) {
            throw new IllegalStateException(
                "DSP48E2 representative BEL must be DSP_ALU, not " + row[5]
            );
        }
        SiteInst siteInst = null;
        for (String component : DSP48E2_COMPONENTS) {
            BEL bel = site.getBEL(component);
            if (bel == null) {
                throw new IllegalStateException(
                    "DSP48E2 site " + row[4] + " lacks component BEL " + component
                );
            }
            Cell child = design.createAndPlaceCell(
                (EDIFCell) null,
                safeName + "$" + component,
                Unisim.valueOf(component),
                site,
                bel
            );
            if (child == null) {
                throw new IllegalStateException(
                    "failed to materialize DSP48E2 component " + component
                );
            }
            if (siteInst == null) siteInst = child.getSiteInst();
            if (child.getSiteInst() != siteInst) {
                throw new IllegalStateException("DSP48E2 components do not share one SiteInst");
            }
        }
        if (siteInst == null) throw new IllegalStateException("empty DSP48E2 transform");
        return new MaterializedCell(
            logicalType, null, siteInst, DSP48E2_COMPONENTS.length, parameters
        );
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: EmuFlowRWRoute <input.tsv> <output.json>");
        }
        List<String> lines = Files.readAllLines(Path.of(args[0]));
        String part = null;
        Map<String, String> metadata = new HashMap<>();
        Map<String, String[]> cellRows = new HashMap<>();
        Map<String, Map<String, String>> cellParameters = new HashMap<>();
        Map<String, String> netKinds = new HashMap<>();
        Map<String, List<String[]>> pinRows = new HashMap<>();
        JSONArray excluded = new JSONArray();
        for (String line : lines) {
            if (line.isEmpty() || line.startsWith("#")) continue;
            String[] fields = line.split("\\t", -1);
            switch (fields[0]) {
                case "META":
                    if (fields.length != 3 || metadata.put(fields[1], fields[2]) != null)
                        throw new IllegalArgumentException("invalid META record");
                    if (fields[1].equals("part")) part = fields[2];
                    break;
                case "CELL":
                    if (fields.length != 6 || cellRows.put(fields[1], fields) != null)
                        throw new IllegalArgumentException("invalid CELL record");
                    cellParameters.put(fields[1], new HashMap<>());
                    break;
                case "PARAM":
                    if (fields.length != 4 || !cellParameters.containsKey(fields[1])
                        || cellParameters.get(fields[1]).put(fields[2], fields[3]) != null)
                        throw new IllegalArgumentException("invalid PARAM record");
                    break;
                case "NET":
                    if (fields.length != 3 || netKinds.put(fields[1], fields[2]) != null)
                        throw new IllegalArgumentException("invalid NET record");
                    pinRows.put(fields[1], new ArrayList<>());
                    break;
                case "PIN":
                    if (fields.length != 5 || !pinRows.containsKey(fields[1]))
                        throw new IllegalArgumentException("invalid PIN record");
                    pinRows.get(fields[1]).add(fields);
                    break;
                case "EXCLUDED":
                    if (fields.length != 3) throw new IllegalArgumentException("invalid EXCLUDED record");
                    excluded.put(new JSONObject().put("net", fields[1]).put("reason", fields[2]));
                    break;
                default:
                    throw new IllegalArgumentException("unknown route-input record " + fields[0]);
            }
        }
        if (part == null) throw new IllegalArgumentException("missing part identity");

        Design design = new Design("emuflow_rwroute", part);
        design.setDesignOutOfContext(true);
        Map<String, MaterializedCell> cells = new HashMap<>();
        List<String> cellNames = new ArrayList<>(cellRows.keySet());
        cellNames.sort(String::compareTo);
        int physicalCells = 0;
        int transformedDsp48e2Cells = 0;
        for (String safeName : cellNames) {
            String[] row = cellRows.get(safeName);
            MaterializedCell cell;
            try {
                cell = materializeCell(
                    design, safeName, row, cellParameters.get(safeName)
                );
            } catch (RuntimeException error) {
                throw new IllegalStateException(
                    "failed to materialize " + safeName + " (" + row[2] + ") type="
                    + row[3] + " at " + row[4] + "/" + row[5], error
                );
            }
            cells.put(safeName, cell);
            physicalCells += cell.physicalCells;
            if (cell.isTransformedDSP48E2()) {
                transformedDsp48e2Cells++;
            }
        }

        Map<String, Net> nets = new HashMap<>();
        List<String> netNames = new ArrayList<>(netKinds.keySet());
        netNames.sort(String::compareTo);
        for (String netName : netNames) {
            Net net = design.createNet(netName);
            List<String[]> pins = pinRows.get(netName);
            pins.sort(Comparator.comparing(row -> row[4].equals("driver") ? "0" : "1"));
            for (String[] row : pins) {
                MaterializedCell cell = cells.get(row[2]);
                if (cell == null) throw new IllegalArgumentException("unknown cell " + row[2]);
                try {
                    List<SitePinInst> connected = new ArrayList<>();
                    if (cell.isTransformedDSP48E2()) {
                        String physicalPin = dsp48e2SitePin(row[3]);
                        if (!cell.siteInst.getSite().hasPin(physicalPin)) {
                            throw new IllegalStateException(
                                "DSP48E2 logical pin " + row[3]
                                + " has no physical site pin " + physicalPin
                            );
                        }
                        connected.add(net.createPin(physicalPin, cell.siteInst));
                    } else if (cell.isBlockRam()) {
                        ensureLogicalPinMapping(cell, row[3]);
                        SitePinInst primary = net.connect(cell.regularCell, row[3]);
                        if (primary != null) connected.add(primary);
                        Set<String> sitePins = new LinkedHashSet<>(
                            cell.regularCell.getAllCorrespondingSitePinNames(row[3])
                        );
                        if (sitePins.isEmpty()) {
                            throw new IllegalStateException(
                                "logical pin has no physical site pin: "
                                + row[2] + "/" + row[3]
                            );
                        }
                        if (row[4].equals("driver") && sitePins.size() != 1) {
                            throw new IllegalStateException(
                                "logical driver expands to multiple physical sources: "
                                + row[2] + "/" + row[3] + " -> " + sitePins
                            );
                        }
                        for (String sitePin : sitePins) {
                            SitePinInst existing = cell.siteInst.getSitePinInst(sitePin);
                            if (existing == null) {
                                connected.add(net.createPin(sitePin, cell.siteInst));
                            } else if (existing.getNet() != net) {
                                throw new IllegalStateException(
                                    "physical site pin already belongs to another net: "
                                    + cell.siteInst.getName() + "/" + sitePin
                                );
                            }
                        }
                    } else {
                        ensureLogicalPinMapping(cell, row[3]);
                        // General logic can expose alternative dedicated and
                        // fabric exits (for example CARRY8 CO[7] via COUT or
                        // HMUX).  RapidWright selects the legal physical exit;
                        // only BRAM's true lower/upper sink expansion is
                        // explicitly materialized above.
                        connected.add(net.connect(cell.regularCell, row[3]));
                    }
                    for (SitePinInst pin : connected) {
                        if (pin == null) continue;
                        boolean expectedOutput = row[4].equals("driver");
                        if (pin.isOutPin() != expectedOutput) {
                            throw new IllegalStateException(
                                "pin direction disagrees with route role: "
                                + row[2] + "/" + row[3]
                            );
                        }
                    }
                } catch (RuntimeException error) {
                    throw new IllegalStateException(
                        "failed to connect " + row[2] + "/" + row[3], error
                    );
                }
            }
            nets.put(netName, net);
        }

        // This certificate begins and ends at physical site pins; purely
        // intra-site nets were excluded by the sealed exporter.  Do not call
        // Design.routeSites(): transformed DSP component cells intentionally
        // have no logical EDIF parent, and asking EDIF to reconstruct one
        // would conflate the inter-site route certificate with a vendor
        // bitstream-complete site implementation.  Primitive/internal timing
        // is modeled separately by EmuFlow's sealed primitive timing stage.
        RWRoute.routeDesignFullNonTimingDriven(design);

        // RapidWright's lightweight timing model evaluates the concrete
        // routed PIP tree in picoseconds.  Keep this deliberately separate
        // from global setup analysis: EmuFlow binds these per-FPGA route
        // segments to transport/TDM events and delegates global setup WNS/TNS
        // to OpenSTA.  RapidWright's model is not a hold/sign-off engine.
        TimingModel timingModel = new TimingModel(design.getDevice());
        timingModel.build();

        JSONArray routeNets = new JSONArray();
        int routed = 0;
        int pips = 0;
        int timedEndpoints = 0;
        float maximumRouteDelayPs = 0.0f;
        for (String netName : netNames) {
            Net net = nets.get(netName);
            JSONObject record = new JSONObject();
            record.put("net", netName);
            record.put("kind", netKinds.get(netName));
            JSONArray pins = new JSONArray();
            SitePinInst source = net.getSource();
            for (SitePinInst pin : net.getPins()) {
                JSONObject pinValue = pinRecord(pin);
                if (!pin.isOutPin()) {
                    if (source == null) {
                        throw new IllegalStateException(
                            "routed net has no timing source: " + netName
                        );
                    }
                    float delayPs = timingModel.calcDelay(source, pin, net);
                    if (!Float.isFinite(delayPs) || delayPs < 0.0f) {
                        throw new IllegalStateException(
                            "invalid RapidWright route delay for " + netName
                            + "/" + pin.getSiteInstName() + "/" + pin.getName()
                            + ": " + delayPs
                        );
                    }
                    pinValue.put("route_delay_ps", delayPs);
                    timedEndpoints++;
                    maximumRouteDelayPs = Math.max(maximumRouteDelayPs, delayPs);
                }
                pins.put(pinValue);
            }
            record.put("pins", pins);
            JSONArray netPips = new JSONArray();
            List<PIP> sortedPips = new ArrayList<>(net.getPIPs());
            sortedPips.sort(Comparator.comparing(PIP::toString));
            for (PIP pip : sortedPips) netPips.put(pipRecord(pip));
            record.put("pips", netPips);
            record.put("has_gap", net.hasGapRouting());
            record.put("source_present", net.getSource() != null);
            record.put("sink_count", net.getSinkPins().size());
            if (net.hasPIPs()) routed++;
            pips += sortedPips.size();
            routeNets.put(record);
        }
        JSONObject output = new JSONObject();
        output.put("schema", SCHEMA);
        output.put("status", "candidate");
        output.put("provider", "rapidwright-rwroute-2026.1.0");
        output.put("part", part);
        output.put("source", new JSONObject()
            .put("mapped_sha256", metadata.get("mapped_sha256"))
            .put("packed_sha256", metadata.get("packed_sha256"))
            .put("placement_sha256", metadata.get("placement_sha256"))
            .put("rwroute_input_sha256", sha256(Path.of(args[0]))));
        output.put("cells", cells.size());
        output.put("materialization", new JSONObject()
            .put("route_cells", cells.size())
            .put("physical_cells", physicalCells)
            .put("transformed_dsp48e2_cells", transformedDsp48e2Cells));
        output.put("nets", routeNets);
        output.put("excluded_nets", excluded);
        output.put("timing", new JSONObject()
            .put("provider", "rapidwright-lightweight")
            .put("family", "UltraScalePlus")
            .put("units", "ps")
            .put("setup_route_delays", "available")
            .put("hold_analysis", "unavailable")
            .put("hard_block_clock_timing", "unqualified")
            .put("logic_coefficients_ps", new JSONObject()
                .put("ff_clock_to_q", timingModel.LOGIC_FF_DELAY)
                .put("carry_co", timingModel.CARRY_CO_DELAY)
                .put("lut_a1", timingModel.LOGIC_LUT_A1_DELAY)
                .put("lut_a2", timingModel.LOGIC_LUT_A2_DELAY)
                .put("lut_a3", timingModel.LOGIC_LUT_A3_DELAY)
                .put("lut_a4", timingModel.LOGIC_LUT_A4_DELAY)
                .put("lut_a5", timingModel.LOGIC_LUT_A5_DELAY)
                .put("lut_a6", timingModel.LOGIC_LUT_A6_DELAY))
            .put("routed_endpoints", timedEndpoints)
            .put("maximum_route_delay_ps", maximumRouteDelayPs));
        output.put("summary", new JSONObject()
            .put("candidate_nets", netNames.size())
            .put("nets_with_pips", routed)
            .put("pips", pips)
            .put("excluded_nets", excluded.length()));
        Files.writeString(Path.of(args[1]), output.toString());
    }
}
