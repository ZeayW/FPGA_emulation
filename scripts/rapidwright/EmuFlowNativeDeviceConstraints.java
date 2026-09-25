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
            "rapidwright-dedicated-sitepin-vector-same-canonical-node-v1";

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
        final List<EndpointPair> endpoints;
        Family(String kind, String contract, String sitePrefix,
               List<EndpointPair> endpoints) {
            this.kind = kind;
            this.contract = contract;
            this.sitePrefix = sitePrefix;
            this.endpoints = endpoints;
        }
    }

    private static final class Endpoint {
        final String nodeKey;
        final String memberSha256;
        final int memberCount;
        Endpoint(String nodeKey, String memberSha256, int memberCount) {
            this.nodeKey = nodeKey;
            this.memberSha256 = memberSha256;
            this.memberCount = memberCount;
        }
    }

    private static final class EndpointVector {
        final Site site;
        final List<Endpoint> endpoints;
        final String key;
        EndpointVector(Site site, List<Endpoint> endpoints) {
            this.site = site;
            this.endpoints = endpoints;
            List<String> keys = new ArrayList<>();
            for (Endpoint endpoint : endpoints) keys.add(endpoint.nodeKey);
            this.key = String.join("\u0000", keys);
        }
    }

    private static final class Edge {
        final EndpointVector source;
        final EndpointVector target;
        Edge(EndpointVector source, EndpointVector target) {
            this.source = source;
            this.target = target;
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
        require(port != null && port.isSitePort() && port.isDedicatedSitePin(),
                site.getName() + "/" + sitePinName + " is not dedicated");
        require(output ? site.isOutputPin(sitePinName) : site.isInputPin(sitePinName),
                site.getName() + "/" + sitePinName + " direction is invalid");
        Node node = selected.getExternalNode(site);
        if (node == null || node.isInvalidNode()) return null;
        String[] seal = nodeMemberSeal(node);
        return new Endpoint(nodeKey(node), seal[0], Integer.parseInt(seal[1]));
    }

    private static EndpointVector vector(Site site, Family family, boolean source) {
        List<Endpoint> result = new ArrayList<>();
        for (EndpointPair pair : family.endpoints) {
            Endpoint endpoint = endpoint(site, source ? pair.source : pair.target, source);
            if (endpoint == null) return null;
            result.add(endpoint);
        }
        return new EndpointVector(site, result);
    }

    private static void addRange(List<EndpointPair> result, String source,
                                 String target, int width) {
        for (int index = 0; index < width; ++index) {
            result.add(new EndpointPair(source + index, target + index));
        }
    }

    private static List<Family> families() {
        List<Family> result = new ArrayList<>();
        result.add(new Family("CARRY_NEXT", "carry8-co7-ci-all-v1", "SLICE_",
                Arrays.asList(new EndpointPair("COUT", "CIN"))));

        List<EndpointPair> dsp = new ArrayList<>();
        addRange(dsp, "ACOUT_B", "ACIN_B", 30);
        addRange(dsp, "BCOUT_B", "BCIN_B", 18);
        dsp.add(new EndpointPair("CARRYCASCOUT", "CARRYCASCIN"));
        dsp.add(new EndpointPair("MULTSIGNOUT", "MULTSIGNIN"));
        addRange(dsp, "PCOUT", "PCIN", 48);
        result.add(new Family("DSP_CASCADE", "dsp48e2-all-cascade-sitepins-v1",
                "DSP48E2_", dsp));

        List<EndpointPair> bram = new ArrayList<>();
        for (String port : Arrays.asList("A", "B")) {
            for (String half : Arrays.asList("L", "U")) {
                addRange(bram, "CASDO" + port + half, "CASDI" + port + half, 16);
                addRange(bram, "CASDOP" + port + half, "CASDIP" + port + half, 2);
            }
        }
        bram.add(new EndpointPair("CASOUTDBITERR", "CASINDBITERR"));
        bram.add(new EndpointPair("CASOUTSBITERR", "CASINSBITERR"));
        result.add(new Family("BRAM_CASCADE", "ramb36e2-all-cascade-sitepins-v1",
                "RAMB36_", bram));

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
                "URAM288_", uram));
        result.sort(Comparator.comparing(family -> family.kind));
        return result;
    }

    private static TreeMap<CapacityKey, Long> capacity(Device device) {
        TreeMap<CapacityKey, Long> result = new TreeMap<>();
        for (Site site : device.getAllSites()) {
            require(site != null && site.getTile() != null,
                    "device contains a null site or tile");
            ClockRegion clockRegion = site.getClockRegion();
            SLR slr = site.getTile().getSLR();
            require(clockRegion != null && slr != null,
                    site.getName() + " has incomplete region membership");
            CapacityKey key = new CapacityKey(slr.getName(), clockRegion.getName(),
                    site.getSiteTypeEnum().name());
            result.put(key, result.getOrDefault(key, 0L) + 1L);
        }
        require(!result.isEmpty(), "device contains no sites");
        return result;
    }

    private static List<Edge> edges(Device device, Family family) {
        List<EndpointVector> sources = new ArrayList<>();
        Map<String, EndpointVector> targets = new HashMap<>();
        for (Site site : device.getAllSites()) {
            if (site == null || !site.getName().startsWith(family.sitePrefix)) continue;
            EndpointVector source = vector(site, family, true);
            EndpointVector target = vector(site, family, false);
            if (source != null) sources.add(source);
            if (target != null) require(targets.put(target.key, target) == null,
                    family.kind + " target vector is not unique");
        }
        List<Edge> result = new ArrayList<>();
        for (EndpointVector source : sources) {
            EndpointVector target = targets.get(source.key);
            if (target == null) continue;
            require(!source.site.getName().equals(target.site.getName()),
                    family.kind + " self edge is invalid");
            require(source.endpoints.size() == target.endpoints.size(),
                    family.kind + " vector size differs");
            for (int index = 0; index < source.endpoints.size(); ++index) {
                Endpoint left = source.endpoints.get(index);
                Endpoint right = target.endpoints.get(index);
                require(left.nodeKey.equals(right.nodeKey)
                                && left.memberSha256.equals(right.memberSha256)
                                && left.memberCount == right.memberCount,
                        family.kind + " canonical node proof differs");
            }
            SLR sourceSlr = source.site.getTile().getSLR();
            SLR targetSlr = target.site.getTile().getSLR();
            require(sourceSlr != null && targetSlr != null
                            && sourceSlr.getName().equals(targetSlr.getName()),
                    family.kind + " edge crosses SLRs");
            result.add(new Edge(source, target));
        }
        result.sort(Comparator.comparing((Edge edge) -> edge.source.site.getName())
                .thenComparing(edge -> edge.target.site.getName()));
        return result;
    }

    private static List<List<String>> chains(String kind, List<Edge> edges) {
        Map<String, String> next = new HashMap<>();
        Set<String> targets = new HashSet<>();
        for (Edge edge : edges) {
            String source = edge.source.site.getName();
            String target = edge.target.site.getName();
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
                Endpoint endpoint = edge.source.endpoints.get(index);
                records.add(family.kind + "\u0000" + edge.source.site.getName()
                        + "\u0000" + pair.source + "\u0000"
                        + edge.target.site.getName() + "\u0000" + pair.target
                        + "\u0000" + endpoint.nodeKey + "\u0000"
                        + endpoint.memberCount + "\u0000" + endpoint.memberSha256);
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
        require(args.length == 8,
                "usage: <device> <full-part> <version> <revision> "
                        + "<manifest-sha256> <database-md5> <architecture-sha256> <output>");
        Device device = Device.getDevice(args[1]);
        require(device != null && device.getName().equals(args[0]),
                "device identity mismatch");
        TreeMap<CapacityKey, Long> capacity = capacity(device);
        List<FamilyResult> results = new ArrayList<>();
        for (Family family : families()) {
            List<Edge> edges = edges(device, family);
            results.add(new FamilyResult(family, edges, chains(family.kind, edges)));
        }
        String payload = payload(args[0], args[1], args[2], args[3], args[4],
                args[5], args[6], capacity, results);
        String document = "{\"payload\":" + payload + ",\"payload_sha256\":"
                + quote(sha256(payload)) + ",\"schema\":" + quote(SCHEMA) + "}\n";
        Path output = Paths.get(args[7]);
        Path parent = output.toAbsolutePath().getParent();
        if (parent != null) Files.createDirectories(parent);
        Files.write(output, document.getBytes(StandardCharsets.UTF_8));
    }
}
