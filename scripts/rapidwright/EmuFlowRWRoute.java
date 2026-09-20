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
import com.xilinx.rapidwright.design.SitePinInst;
import com.xilinx.rapidwright.design.Unisim;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.rwroute.RWRoute;
import com.xilinx.rapidwright.timing.TimingModel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

public final class EmuFlowRWRoute {
    private static final String SCHEMA = "emuflow.xilinx-route-db/v1";

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

    private static void ensureLogicalPinMapping(Cell cell, String logicalPin) {
        if (cell.getPinMappingsL2P().containsKey(logicalPin)) return;
        String physicalPin = logicalPin.replace("[", "").replace("]", "");
        if (cell.getBEL().getPin(physicalPin) == null) {
            throw new IllegalStateException(
                "logical pin has no BEL pin: " + cell.getName() + "/" + logicalPin
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
        if (!cell.getPinMappingsL2P().containsKey(logicalPin)) {
            throw new IllegalStateException(
                "failed to materialize pin mapping " + cell.getName() + "/" + logicalPin
            );
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: EmuFlowRWRoute <input.tsv> <output.json>");
        }
        List<String> lines = Files.readAllLines(Path.of(args[0]));
        String part = null;
        Map<String, String> metadata = new HashMap<>();
        Map<String, String[]> cellRows = new HashMap<>();
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
        Map<String, Cell> cells = new HashMap<>();
        List<String> cellNames = new ArrayList<>(cellRows.keySet());
        cellNames.sort(String::compareTo);
        for (String safeName : cellNames) {
            String[] row = cellRows.get(safeName);
            Cell cell;
            try {
                Unisim unisim = Unisim.valueOf(row[3]);
                cell = design.createAndPlaceCell(
                    safeName, unisim, row[4] + "/" + row[5]
                );
            } catch (RuntimeException error) {
                throw new IllegalStateException(
                    "failed to materialize " + safeName + " (" + row[2] + ") type="
                    + row[3] + " at " + row[4] + "/" + row[5], error
                );
            }
            if (cell == null) throw new IllegalStateException("failed to place " + safeName);
            cells.put(safeName, cell);
        }

        Map<String, Net> nets = new HashMap<>();
        List<String> netNames = new ArrayList<>(netKinds.keySet());
        netNames.sort(String::compareTo);
        for (String netName : netNames) {
            Net net = design.createNet(netName);
            List<String[]> pins = pinRows.get(netName);
            pins.sort(Comparator.comparing(row -> row[4].equals("driver") ? "0" : "1"));
            for (String[] row : pins) {
                Cell cell = cells.get(row[2]);
                if (cell == null) throw new IllegalArgumentException("unknown cell " + row[2]);
                ensureLogicalPinMapping(cell, row[3]);
                try {
                    // A legal intra-site connection intentionally returns no
                    // SitePinInst; logical/physical pin mappings, not the
                    // nullable return value, establish pin validity.
                    net.connect(cell, row[3]);
                } catch (RuntimeException error) {
                    throw new IllegalStateException(
                        "failed to connect " + row[2] + "/" + row[3], error
                    );
                }
            }
            nets.put(netName, net);
        }

        design.routeSites();
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
        output.put("nets", routeNets);
        output.put("excluded_nets", excluded);
        output.put("timing", new JSONObject()
            .put("provider", "rapidwright-lightweight")
            .put("family", "UltraScalePlus")
            .put("units", "ps")
            .put("setup_route_delays", "available")
            .put("hold_analysis", "unavailable")
            .put("hard_block_clock_timing", "unqualified")
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
