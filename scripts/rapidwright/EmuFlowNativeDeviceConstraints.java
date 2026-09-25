/* Copyright (c) EmuFlow contributors. SPDX-License-Identifier: Apache-2.0 */

/*
 * Export source-sealed native dedicated-wire adjacency and regional capacity.
 * Every accepted edge is proven by the complete primitive-family SitePin
 * vector resolving to identical canonical RapidWright Nodes at both sites.
 * Coordinates and routing-intent labels are deliberately not used.
 */

import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.BELClass;
import com.xilinx.rapidwright.device.BELPin;
import com.xilinx.rapidwright.device.ClockRegion;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.SLR;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SitePin;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.Wire;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

public final class EmuFlowNativeDeviceConstraints {
    private static final String SCHEMA =
            "emuflow.xilinx-native-device-constraints/v2";
    private static final String ROUTE_BACKEND =
            "rapidwright-native-device-database-v1";
    private static final String PROOF_METHOD =
            "rapidwright-dedicated-sitepin-vector-directed-path-v2";
    private static final String DIRECT_ARC_TYPE = "DIRECTIONAL_NOT_BUFFERED21";

    private static final class CapacityKey implements Comparable<CapacityKey> {
        final String slr;
        final String clockRegion;
        final String siteType;
        CapacityKey(String slr, String clockRegion, String siteType) {
            this.slr = slr;
            this.clockRegion = clockRegion;
            this.siteType = siteType;
        }
        @Override public int compareTo(CapacityKey other) {
            int value = slr.compareTo(other.slr);
            if (value != 0) return value;
            value = clockRegion.compareTo(other.clockRegion);
            return value != 0 ? value : siteType.compareTo(other.siteType);
        }
    }

    private static final class EndpointPair {
        final String source;
        final String target;
        EndpointPair(String source, String target) {
            this.source = source;
            this.target = target;
        }
    }

    private static final class Family {
        final String kind;
        final String contract;
        final String sitePrefix;
        final String tilePrefix;
        final List<EndpointPair> endpoints;
        Family(String kind, String contract, String sitePrefix, String tilePrefix,
               List<EndpointPair> endpoints) {
            this.kind = kind;
            this.contract = contract;
            this.sitePrefix = sitePrefix;
            this.tilePrefix = tilePrefix;
            this.endpoints = endpoints;
        }
    }

    private static final class Endpoint {
        final Node node;
        final String nodeKey;
        final String memberSha256;
        final int memberCount;
        Endpoint(Node node, String nodeKey, String memberSha256, int memberCount) {
            this.node = node;
            this.nodeKey = nodeKey;
            this.memberSha256 = memberSha256;
            this.memberCount = memberCount;
        }
    }

    private static final class EndpointVector {
        final Site site;
        final String contractSiteName;
        final List<Endpoint> endpoints;
        final String key;
        EndpointVector(Site site, String contractSiteName, List<Endpoint> endpoints) {
            this.site = site;
            this.contractSiteName = contractSiteName;
            this.endpoints = endpoints;
            List<String> keys = new ArrayList<>();
            for (Endpoint endpoint : endpoints) keys.add(endpoint.nodeKey);
            this.key = String.join("\u0000", keys);
        }
    }

    private static final class Connection {
        final Endpoint source;
        final Endpoint target;
        final EndpointVector targetVector;
        final String arcSha256;
        final int arcCount;
        Connection(Endpoint source, TargetRef target, String arcSha256, int arcCount) {
            this.source = source;
            this.target = target.endpoint;
            this.targetVector = target.vector;
            this.arcSha256 = arcSha256;
            this.arcCount = arcCount;
        }
    }

    private static final class TargetRef {
        final EndpointVector vector;
        final Endpoint endpoint;
        TargetRef(EndpointVector vector, Endpoint endpoint) {
            this.vector = vector;
            this.endpoint = endpoint;
        }
    }

    private static final class Edge {
        final EndpointVector source;
        final EndpointVector target;
        final List<Connection> connections;
        Edge(EndpointVector source, EndpointVector target,
             List<Connection> connections) {
            this.source = source;
            this.target = target;
            this.connections = connections;
        }
    }

    private static final class FamilyResult {
        final Family family;
        final List<Edge> edges;
        final List<List<String>> chains;
        FamilyResult(Family family, List<Edge> edges, List<List<String>> chains) {
            this.family = family;
            this.edges = edges;
            this.chains = chains;
        }
    }

    private EmuFlowNativeDeviceConstraints() {}

    private static void require(boolean condition, String message) {
        if (!condition) throw new IllegalStateException(message);
    }

    private static String escape(String value) {
        StringBuilder result = new StringBuilder(value.length() + 16);
        for (int index = 0; index < value.length(); ++index) {
            char character = value.charAt(index);
            switch (character) {
                case '\\': result.append("\\\\"); break;
                case '"': result.append("\\\""); break;
                case '\b': result.append("\\b"); break;
                case '\f': result.append("\\f"); break;
                case '\n': result.append("\\n"); break;
                case '\r': result.append("\\r"); break;
                case '\t': result.append("\\t"); break;
                default:
                    if (character < 0x20) {
                        result.append(String.format("\\u%04x", (int) character));
                    } else {
                        result.append(character);
                    }
            }
        }
        return result.toString();
    }

    private static String quote(String value) { return "\"" + escape(value) + "\""; }

    private static String sha256(String value) {
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder(bytes.length * 2);
            for (byte element : bytes) result.append(String.format("%02x", element & 0xff));
            return result.toString();
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static String nodeKey(Node node) {
        require(node != null && !node.isInvalidNode(), "invalid canonical node");
        Tile tile = node.getTile();
        int wire = node.getWireIndex();
        require(tile != null && wire >= 0 && wire < tile.getWireCount(),
                "canonical node root is invalid");
        return tile.getName() + "\u0000" + wire + "\u0000" + tile.getWireName(wire);
    }

    private static String[] nodeMemberSeal(Node node) {
        Wire[] members = node.getAllWiresInNode();
        require(members != null && members.length > 0,
                "dedicated node has no native wire members");
        List<String> records = new ArrayList<>();
        String expected = nodeKey(node);
        for (Wire member : members) {
            require(member != null && member.getTile() != null,
                    "dedicated node has a null member");
            Tile tile = member.getTile();
            int wire = member.getWireIndex();
            require(wire >= 0 && wire < tile.getWireCount(),
                    "dedicated node member is invalid");
            require(expected.equals(nodeKey(Node.getNode(tile, wire))),
                    "dedicated node member resolves elsewhere");
            records.add(tile.getName() + "\u0000" + wire + "\u0000"
                    + tile.getWireName(wire));
        }
        Collections.sort(records);
        return new String[]{sha256(String.join("\n", records)),
                Integer.toString(records.size())};
    }

    private static Endpoint endpoint(Site site, String sitePinName, boolean output) {
        BELPin selected = null;
        for (BEL bel : site.getBELs()) {
            if (bel.getBELClass() == BELClass.PORT) continue;
            for (BELPin pin : bel.getPins()) {
                if ((output ? pin.isOutput() : pin.isInput())
                        && sitePinName.equals(pin.getConnectedSitePinName())) {
                    require(selected == null,
                            site.getName() + "/" + sitePinName
                                    + " has multiple primitive endpoints");
                    selected = pin;
                }
            }
        }
        if (selected == null) return null;
        SitePin sitePin = selected.getSitePin(site);
        require(sitePin != null && sitePinName.equals(sitePin.getPinName()),
                site.getName() + "/" + sitePinName + " has no native SitePin");
        BELPin port = sitePin.getBELPin();
        // RapidWright does not mark every hard-block cascade PORT BEL pin as
        // `isDedicatedSitePin()`, even when the two SitePins resolve to the
        // same PIP-free canonical Node.  The latter identity (checked for the
        // complete family vector below) is the native proof; the optional
        // metadata flag is therefore deliberately not treated as authority.
        require(port != null && port.isSitePort(),
                site.getName() + "/" + sitePinName + " is not a site port");
        require(output ? site.isOutputPin(sitePinName) : site.isInputPin(sitePinName),
                site.getName() + "/" + sitePinName + " direction is invalid");
        Node node = selected.getExternalNode(site);
        if (node == null || node.isInvalidNode()) return null;
        String[] seal = nodeMemberSeal(node);
        return new Endpoint(
                node, nodeKey(node), seal[0], Integer.parseInt(seal[1]));
    }

    private static EndpointVector vector(
            Site site, String contractSiteName, Family family, boolean source) {
        List<Endpoint> result = new ArrayList<>();
        for (EndpointPair pair : family.endpoints) {
            Endpoint endpoint = endpoint(site, source ? pair.source : pair.target, source);
            if (endpoint == null) return null;
            result.add(endpoint);
        }
        return new EndpointVector(site, contractSiteName, result);
    }

    private static void addRange(List<EndpointPair> result, String source,
                                 String target, int width) {
        for (int index = 0; index < width; ++index) {
            result.add(new EndpointPair(source + index, target + index));
        }
    }

    private static List<Family> families() {
        List<Family> result = new ArrayList<>();
        result.add(new Family("CARRY_NEXT", "carry8-co7-ci-all-v1", "SLICE_", "",
                Arrays.asList(new EndpointPair("COUT", "CIN"))));

        List<EndpointPair> dsp = new ArrayList<>();
        addRange(dsp, "ACOUT_B", "ACIN_B", 30);
        addRange(dsp, "BCOUT_B", "BCIN_B", 18);
        dsp.add(new EndpointPair("CARRYCASCOUT", "CARRYCASCIN"));
        dsp.add(new EndpointPair("MULTSIGNOUT", "MULTSIGNIN"));
        addRange(dsp, "PCOUT", "PCIN", 48);
        result.add(new Family("DSP_CASCADE", "dsp48e2-all-cascade-sitepins-v1",
                "DSP48E2_", "DSP_", dsp));

        List<EndpointPair> bram = new ArrayList<>();
        for (String port : Arrays.asList("A", "B")) {
            for (String half : Arrays.asList("L", "U")) {
                addRange(bram, "CASDO" + port + half, "CASDI" + port + half, 16);
                addRange(bram, "CASDOP" + port + half, "CASDIP" + port + half, 2);
            }
        }
        bram.add(new EndpointPair("CASOUTDBITERR", "CASINDBITERR"));
        bram.add(new EndpointPair("CASOUTSBITERR", "CASINSBITERR"));
        result.add(new Family("BRAM_CASCADE",
                "ramb36e2-72-data-parity-2-ecc-cascade-sitepins-v2",
                "RAMB36_", "BRAM_", bram));

        String[] suffixes = {"ADDR_A", "ADDR_B", "BWE_A", "BWE_B",
                "DBITERR_A", "DBITERR_B", "DIN_A", "DIN_B", "DOUT_A", "DOUT_B",
                "EN_A", "EN_B", "RDACCESS_A", "RDACCESS_B", "RDB_WR_A", "RDB_WR_B",
                "SBITERR_A", "SBITERR_B"};
        int[] widths = {23, 23, 9, 9, 1, 1, 72, 72, 72, 72,
                1, 1, 1, 1, 1, 1, 1, 1};
        List<EndpointPair> uram = new ArrayList<>();
        for (int index = 0; index < suffixes.length; ++index) {
            String source = "CAS_OUT_" + suffixes[index];
            String target = "CAS_IN_" + suffixes[index];
            if (widths[index] == 1) uram.add(new EndpointPair(source, target));
            else addRange(uram, source, target, widths[index]);
        }
        result.add(new Family("URAM_CASCADE", "uram288-all-cascade-sitepins-v1",
                "URAM288_", "URAM_", uram));
        result.sort(Comparator.comparing(family -> family.kind));
        return result;
    }

    private static Set<String> eligibleSites(Path path) throws IOException {
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        Set<String> result = new HashSet<>();
        for (String line : lines) {
            require(!line.isEmpty() && result.add(line),
                    "architecture site allowlist is empty or duplicated");
        }
        require(!result.isEmpty(), "architecture site allowlist is empty");
        return result;
    }

    private static TreeMap<CapacityKey, Long> capacity(
            Device device, Set<String> eligibleSites) {
        TreeMap<CapacityKey, Long> result = new TreeMap<>();
        Set<String> seen = new HashSet<>();
        for (Site site : device.getAllSites()) {
            require(site != null && site.getTile() != null,
                    "device contains a null site or tile");
            if (!eligibleSites.contains(site.getName())) continue;
            require(seen.add(site.getName()), "native device contains a duplicate site");
            ClockRegion clockRegion = site.getClockRegion();
            SLR slr = site.getTile().getSLR();
            require(clockRegion != null && slr != null,
                    site.getName() + " has incomplete region membership");
            CapacityKey key = new CapacityKey(slr.getName(), clockRegion.getName(),
                    site.getSiteTypeEnum().name());
            result.put(key, result.getOrDefault(key, 0L) + 1L);
        }
        require(seen.equals(eligibleSites),
                "ArchitectureDB site allowlist does not match the native device");
        require(!result.isEmpty(), "device contains no sites");
        return result;
    }

    private static String pipRecord(PIP pip) {
        require(pip != null && pip.getTile() != null,
                "native direct arc has no tile");
        Node start = pip.getStartNode();
        Node end = pip.getEndNode();
        require(start != null && end != null,
                "native direct arc has incomplete nodes");
        String[] startSeal = nodeMemberSeal(start);
        String[] endSeal = nodeMemberSeal(end);
        return pip.getTile().getName() + "\u0000" + pip.getStartWireIndex()
                + "\u0000" + pip.getEndWireIndex() + "\u0000" + pip.getPIPType()
                + "\u0000" + nodeKey(start) + "\u0000" + startSeal[1]
                + "\u0000" + startSeal[0] + "\u0000" + nodeKey(end)
                + "\u0000" + endSeal[1] + "\u0000" + endSeal[0];
    }

    private static void requireReverseArc(Node start, PIP selected, Family family) {
        String record = pipRecord(selected);
        int matches = 0;
        for (PIP pip : selected.getEndNode().getAllUphillPIPs()) {
            if (!DIRECT_ARC_TYPE.equals(pip.getPIPType().name())) continue;
            Node candidateStart = pip.getStartNode();
            if (candidateStart != null
                    && nodeKey(start).equals(nodeKey(candidateStart))
                    && record.equals(pipRecord(pip))) {
                matches += 1;
            }
        }
        require(matches == 1,
                family.kind + " exact target arc lacks one reverse native proof");
    }

    private static Connection connection(
            Endpoint source, Map<String, TargetRef> targets, Family family) {
        Node node = source.node;
        List<String> records = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (int depth = 0; depth <= 16; ++depth) {
            String key = nodeKey(node);
            require(seen.add(key), family.kind + " direct arc contains a cycle");
            TargetRef target = targets.get(key);
            if (target != null) {
                return new Connection(source, target,
                        sha256(String.join("\n", records)), records.size());
            }
            List<PIP> direct = new ArrayList<>();
            List<PIP> targetArcs = new ArrayList<>();
            for (PIP pip : node.getAllDownhillPIPs()) {
                if (!DIRECT_ARC_TYPE.equals(pip.getPIPType().name())) continue;
                Node start = pip.getStartNode();
                Node end = pip.getEndNode();
                require(start != null && end != null,
                        family.kind + " direct arc has incomplete nodes");
                if (!family.tilePrefix.isEmpty()) {
                    require(start.getTile().getName().startsWith(family.tilePrefix)
                                    && end.getTile().getName().startsWith(family.tilePrefix),
                            family.kind + " direct arc leaves its hard-block tile");
                }
                require(!pip.isBidirectional() && !pip.isRouteThru()
                                && !pip.isGapArc(),
                        family.kind + " cascade uses a general routing arc");
                direct.add(pip);
                if (targets.containsKey(nodeKey(end))) targetArcs.add(pip);
            }
            if (direct.isEmpty()) return null;
            require(targetArcs.size() <= 1,
                    family.kind + " direct cascade reaches multiple target sites");
            if (!targetArcs.isEmpty()) {
                PIP selected = targetArcs.get(0);
                requireReverseArc(node, selected, family);
                records.add(pipRecord(selected));
                TargetRef exactTarget = targets.get(nodeKey(selected.getEndNode()));
                return new Connection(source, exactTarget,
                        sha256(String.join("\n", records)), records.size());
            }
            require(direct.size() == 1,
                    family.kind + " direct cascade branches before its exact target");
            PIP selected = direct.get(0);
            require(key.equals(nodeKey(selected.getStartNode())),
                    family.kind + " direct arc starts on a different node");
            records.add(pipRecord(selected));
            node = selected.getEndNode();
        }
        throw new IllegalStateException(family.kind + " direct arc exceeds 16 hops");
    }

    private static String contractSiteName(
            Site site, Family family, Set<String> eligibleSites) {
        if (eligibleSites.contains(site.getName())) return site.getName();
        if (!"BRAM_CASCADE".equals(family.kind)) return null;
        List<String> anchors = new ArrayList<>();
        for (Site tileSite : site.getTile().getSites()) {
            if (tileSite != null && eligibleSites.contains(tileSite.getName())
                    && tileSite.getName().startsWith("RAMB18_")) {
                anchors.add(tileSite.getName());
            }
        }
        require(anchors.size() == 1,
                site.getName() + " does not have exactly one ArchitectureDB "
                        + "RAMB18/RAMB36 mode anchor");
        return anchors.get(0);
    }

    private static List<Edge> edges(
            Device device, Family family, Set<String> eligibleSites) {
        List<EndpointVector> sources = new ArrayList<>();
        List<EndpointVector> targetVectors = new ArrayList<>();
        for (Site site : device.getAllSites()) {
            if (site == null || !site.getName().startsWith(family.sitePrefix)) continue;
            String contractSiteName = contractSiteName(site, family, eligibleSites);
            if (contractSiteName == null) continue;
            EndpointVector source = vector(site, contractSiteName, family, true);
            EndpointVector target = vector(site, contractSiteName, family, false);
            if (source != null) sources.add(source);
            if (target != null) targetVectors.add(target);
        }
        List<Map<String, TargetRef>> targets = new ArrayList<>();
        for (int index = 0; index < family.endpoints.size(); ++index) {
            Map<String, TargetRef> byNode = new HashMap<>();
            for (EndpointVector vector : targetVectors) {
                Endpoint endpoint = vector.endpoints.get(index);
                require(byNode.put(endpoint.nodeKey,
                                new TargetRef(vector, endpoint)) == null,
                        family.kind + " target endpoint node is not unique");
            }
            targets.add(byNode);
        }
        List<Edge> result = new ArrayList<>();
        for (EndpointVector source : sources) {
            List<Connection> connections = new ArrayList<>();
            EndpointVector target = null;
            boolean complete = true;
            for (int index = 0; index < source.endpoints.size(); ++index) {
                Connection connection = connection(
                        source.endpoints.get(index), targets.get(index), family);
                if (connection == null) {
                    complete = false;
                    break;
                }
                if (target == null) target = connection.targetVector;
                else require(target == connection.targetVector,
                        family.kind + " endpoint vector reaches multiple sites");
                connections.add(connection);
            }
            if (!complete) continue;
            require(target != null
                            && !source.contractSiteName.equals(target.contractSiteName),
                    family.kind + " self edge is invalid");
            SLR sourceSlr = source.site.getTile().getSLR();
            SLR targetSlr = target.site.getTile().getSLR();
            require(sourceSlr != null && targetSlr != null
                            && sourceSlr.getName().equals(targetSlr.getName()),
                    family.kind + " edge crosses SLRs");
            if ("BRAM_CASCADE".equals(family.kind)) {
                ClockRegion sourceClockRegion = source.site.getClockRegion();
                ClockRegion targetClockRegion = target.site.getClockRegion();
                require(sourceClockRegion != null && targetClockRegion != null,
                        family.kind + " edge lacks clock-region membership");
                if (!sourceClockRegion.getName().equals(
                            targetClockRegion.getName())) continue;
            }
            result.add(new Edge(source, target, connections));
        }
        result.sort(Comparator.comparing(
                    (Edge edge) -> edge.source.contractSiteName)
                .thenComparing(edge -> edge.target.contractSiteName));
        return result;
    }

    private static List<List<String>> chains(String kind, List<Edge> edges) {
        Map<String, String> next = new HashMap<>();
        Set<String> targets = new HashSet<>();
        for (Edge edge : edges) {
            String source = edge.source.contractSiteName;
            String target = edge.target.contractSiteName;
            require(next.put(source, target) == null, kind + " branches");
            require(targets.add(target), kind + " merges");
        }
        List<String> starts = new ArrayList<>();
        for (String source : next.keySet()) if (!targets.contains(source)) starts.add(source);
        Collections.sort(starts);
        List<List<String>> result = new ArrayList<>();
        Set<String> visited = new HashSet<>();
        for (String start : starts) {
            List<String> chain = new ArrayList<>();
            Set<String> members = new HashSet<>();
            String site = start;
            chain.add(site);
            members.add(site);
            while (next.containsKey(site)) {
                require(visited.add(site), kind + " edge repeats");
                site = next.get(site);
                require(members.add(site), kind + " contains a cycle");
                chain.add(site);
            }
            result.add(chain);
        }
        require(visited.size() == edges.size(), kind + " has orphan or cyclic edges");
        result.sort(Comparator.comparing(chain -> String.join("\u0000", chain)));
        return result;
    }

    private static String proofSha256(Family family, List<Edge> edges) {
        List<String> records = new ArrayList<>();
        for (Edge edge : edges) {
            for (int index = 0; index < family.endpoints.size(); ++index) {
                EndpointPair pair = family.endpoints.get(index);
                Connection connection = edge.connections.get(index);
                records.add(family.kind + "\u0000" + edge.source.contractSiteName
                        + "\u0000" + pair.source + "\u0000"
                        + edge.target.contractSiteName + "\u0000" + pair.target
                        + "\u0000" + connection.source.nodeKey + "\u0000"
                        + connection.source.memberCount + "\u0000"
                        + connection.source.memberSha256 + "\u0000"
                        + connection.target.nodeKey + "\u0000"
                        + connection.target.memberCount + "\u0000"
                        + connection.target.memberSha256 + "\u0000"
                        + connection.arcCount + "\u0000" + connection.arcSha256);
            }
        }
        Collections.sort(records);
        return sha256(String.join("\n", records));
    }

    private static String chainsJson(List<List<String>> chains) {
        StringBuilder result = new StringBuilder("[");
        for (int i = 0; i < chains.size(); ++i) {
            if (i > 0) result.append(',');
            result.append('[');
            for (int j = 0; j < chains.get(i).size(); ++j) {
                if (j > 0) result.append(',');
                result.append(quote(chains.get(i).get(j)));
            }
            result.append(']');
        }
        return result.append(']').toString();
    }

    private static String capacityJson(TreeMap<CapacityKey, Long> capacity) {
        StringBuilder result = new StringBuilder("[");
        boolean first = true;
        for (Map.Entry<CapacityKey, Long> entry : capacity.entrySet()) {
            if (!first) result.append(',');
            first = false;
            CapacityKey key = entry.getKey();
            result.append("{\"clock_region\":").append(quote(key.clockRegion))
                    .append(",\"site_type\":").append(quote(key.siteType))
                    .append(",\"sites\":").append(entry.getValue())
                    .append(",\"slr\":").append(quote(key.slr)).append('}');
        }
        return result.append(']').toString();
    }

    private static String adjacencyJson(List<FamilyResult> families) {
        StringBuilder result = new StringBuilder("[");
        boolean first = true;
        for (FamilyResult family : families) {
            if (family.edges.isEmpty()) continue;
            if (!first) result.append(',');
            first = false;
            result.append("{\"chains\":").append(chainsJson(family.chains))
                    .append(",\"edge_count\":").append(family.edges.size())
                    .append(",\"endpoint_contract\":").append(quote(family.family.contract))
                    .append(",\"kind\":").append(quote(family.family.kind))
                    .append(",\"native_proof_sha256\":")
                    .append(quote(proofSha256(family.family, family.edges)))
                    .append(",\"proof_method\":").append(quote(PROOF_METHOD)).append('}');
        }
        return result.append(']').toString();
    }

    private static String capabilitiesJson(List<FamilyResult> families) {
        TreeMap<String, String> values = new TreeMap<>();
        for (FamilyResult family : families) {
            values.put("dedicated_adjacency." + family.family.kind,
                    family.edges.isEmpty() ? "core_missing" : "native_supported");
        }
        StringBuilder result = new StringBuilder("{")
                .append("\"clock_region_site_capacity\":\"native_supported\",");
        for (Map.Entry<String, String> entry : values.entrySet()) {
            result.append(quote(entry.getKey())).append(':')
                    .append(quote(entry.getValue())).append(',');
        }
        return result.append("\"half_column_clock_capacity\":\"unverified\",")
                .append("\"slr_site_capacity\":\"native_supported\"}").toString();
    }

    private static String edgeCountsJson(List<FamilyResult> families) {
        StringBuilder result = new StringBuilder("{");
        for (int index = 0; index < families.size(); ++index) {
            if (index > 0) result.append(',');
            FamilyResult family = families.get(index);
            result.append(quote(family.family.kind)).append(':').append(family.edges.size());
        }
        return result.append('}').toString();
    }

    private static String payload(String deviceName, String fullPart, String version,
                                  String revision, String manifestSha256,
                                  String databaseMd5, String architectureSha256,
                                  TreeMap<CapacityKey, Long> capacity,
                                  List<FamilyResult> families) {
        Set<String> slrs = new HashSet<>();
        Set<String> clockRegions = new HashSet<>();
        long sites = 0;
        long edges = 0;
        for (Map.Entry<CapacityKey, Long> entry : capacity.entrySet()) {
            slrs.add(entry.getKey().slr);
            clockRegions.add(entry.getKey().clockRegion);
            sites += entry.getValue();
        }
        for (FamilyResult family : families) edges += family.edges.size();
        return "{\"capabilities\":" + capabilitiesJson(families)
                + ",\"dedicated_adjacency\":" + adjacencyJson(families)
                + ",\"site_capacity\":" + capacityJson(capacity)
                + ",\"source\":{\"architecture_sha256\":" + quote(architectureSha256)
                + ",\"device\":" + quote(deviceName)
                + ",\"device_database_md5\":" + quote(databaseMd5)
                + ",\"full_part\":" + quote(fullPart)
                + ",\"generator\":{\"revision\":" + quote(revision)
                + ",\"version\":" + quote(version) + "}"
                + ",\"provider_manifest_sha256\":" + quote(manifestSha256)
                + ",\"route_backend\":" + quote(ROUTE_BACKEND) + "}"
                + ",\"summary\":{\"capacity_buckets\":" + capacity.size()
                + ",\"clock_regions\":" + clockRegions.size()
                + ",\"dedicated_edges\":" + edges
                + ",\"dedicated_edges_by_kind\":" + edgeCountsJson(families)
                + ",\"sites\":" + sites + ",\"slrs\":" + slrs.size() + "}}";
    }

    public static void main(String[] args) throws IOException {
        require(args.length == 9,
                "usage: <device> <full-part> <version> <revision> "
                        + "<manifest-sha256> <database-md5> <architecture-sha256> "
                        + "<architecture-sites> <output>");
        Device device = Device.getDevice(args[1]);
        require(device != null && device.getName().equals(args[0]),
                "device identity mismatch");
        Set<String> eligibleSites = eligibleSites(Paths.get(args[7]));
        TreeMap<CapacityKey, Long> capacity = capacity(device, eligibleSites);
        List<FamilyResult> results = new ArrayList<>();
        for (Family family : families()) {
            List<Edge> edges = edges(device, family, eligibleSites);
            results.add(new FamilyResult(family, edges, chains(family.kind, edges)));
        }
        String payload = payload(args[0], args[1], args[2], args[3], args[4],
                args[5], args[6], capacity, results);
        String document = "{\"payload\":" + payload + ",\"payload_sha256\":"
                + quote(sha256(payload)) + ",\"schema\":" + quote(SCHEMA) + "}\n";
        Path output = Paths.get(args[8]);
        Path parent = output.toAbsolutePath().getParent();
        if (parent != null) Files.createDirectories(parent);
        Files.write(output, document.getBytes(StandardCharsets.UTF_8));
    }
}
