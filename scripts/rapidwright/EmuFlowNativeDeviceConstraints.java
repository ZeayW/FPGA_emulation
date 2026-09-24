/*
 * Copyright (c) EmuFlow contributors.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Export the small set of Xilinx device facts that a physical placer cannot
 * infer safely from coordinates.  CARRY_NEXT edges are accepted only when
 * both primitive-specific BEL pins reach dedicated SitePins and both
 * SitePins resolve to the same canonical RapidWright Node.  Neither routing
 * intent labels nor coordinate-neighbour heuristics participate in the proof.
 */

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
            "emuflow.xilinx-native-device-constraints/v1";
    private static final String ROUTE_BACKEND =
            "rapidwright-native-device-database-v1";
    private static final String PROOF_METHOD =
            "rapidwright-primitive-bel-sitepin-same-canonical-node-v1";

    private static final class CapacityKey implements Comparable<CapacityKey> {
        final String slr;
        final String clockRegion;
        final String siteType;

        CapacityKey(String slr, String clockRegion, String siteType) {
            this.slr = slr;
            this.clockRegion = clockRegion;
            this.siteType = siteType;
        }

        @Override
        public int compareTo(CapacityKey other) {
            int compare = slr.compareTo(other.slr);
            if (compare != 0) return compare;
            compare = clockRegion.compareTo(other.clockRegion);
            if (compare != 0) return compare;
            return siteType.compareTo(other.siteType);
        }
    }

    private static final class CarryEndpoint {
        final Site site;
        final Node node;
        final String nodeKey;
        final String memberSha256;
        final int memberCount;

        CarryEndpoint(
                Site site,
                Node node,
                String nodeKey,
                String memberSha256,
                int memberCount) {
            this.site = site;
            this.node = node;
            this.nodeKey = nodeKey;
            this.memberSha256 = memberSha256;
            this.memberCount = memberCount;
        }
    }

    private static final class CarryEdge {
        final CarryEndpoint source;
        final CarryEndpoint target;

        CarryEdge(CarryEndpoint source, CarryEndpoint target) {
            this.source = source;
            this.target = target;
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

    private static String quote(String value) {
        return "\"" + escape(value) + "\"";
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte element : value) {
            result.append(String.format("%02x", element & 0xff));
        }
        return result.toString();
    }

    private static String sha256(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return hex(digest.digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static String nodeKey(Node node) {
        require(node != null && !node.isInvalidNode(), "invalid canonical node");
        Tile tile = node.getTile();
        int wireIndex = node.getWireIndex();
        require(tile != null, "canonical node has no root tile");
        require(wireIndex >= 0 && wireIndex < tile.getWireCount(),
                "canonical node root wire is out of range");
        return tile.getName() + "\u0000" + wireIndex + "\u0000"
                + tile.getWireName(wireIndex);
    }

    private static String[] nodeMemberSeal(Node node) {
        Wire[] members = node.getAllWiresInNode();
        require(members != null && members.length > 0,
                "dedicated node has no native wire members");
        List<String> records = new ArrayList<>();
        for (Wire wire : members) {
            require(wire != null && wire.getTile() != null,
                    "dedicated node contains a null wire");
            int wireIndex = wire.getWireIndex();
            Tile tile = wire.getTile();
            require(wireIndex >= 0 && wireIndex < tile.getWireCount(),
                    "dedicated node member is out of range");
            Node resolved = Node.getNode(tile, wireIndex);
            require(resolved != null && nodeKey(resolved).equals(nodeKey(node)),
                    "dedicated node member resolves to a different node");
            records.add(tile.getName() + "\u0000" + wireIndex + "\u0000"
                    + tile.getWireName(wireIndex));
        }
        Collections.sort(records);
        return new String[]{sha256(String.join("\n", records)),
                Integer.toString(records.size())};
    }

    private static BELPin requireCarryPin(
            Site site, String pinName, String sitePinName, boolean output) {
        BELPin pin = site.getBELPin("CARRY8", pinName);
        // Some boundary SLICE sites retain a CARRY8 BEL but omit one of the
        // inter-site endpoint pins entirely.  Absence proves no edge; only an
        // endpoint that exists is allowed to proceed to the strict checks.
        if (pin == null) return null;
        require(output ? pin.isOutput() : pin.isInput(),
                site.getName() + " CARRY8/" + pinName + " direction is invalid");
        String connectedSitePinName = pin.getConnectedSitePinName();
        // A primitive pin with no site-port connection proves no external
        // edge.  It is a legal chain endpoint, not a failed adjacency proof.
        if (connectedSitePinName == null) return null;
        require(sitePinName.equals(connectedSitePinName),
                site.getName() + " CARRY8/" + pinName
                        + " does not reach " + sitePinName);
        SitePin sitePin = pin.getSitePin(site);
        require(sitePin != null && sitePinName.equals(sitePin.getPinName()),
                site.getName() + " CARRY8/" + pinName
                        + " has no native SitePin object");
        BELPin portPin = sitePin.getBELPin();
        require(portPin != null && portPin.isSitePort()
                        && portPin.isDedicatedSitePin(),
                site.getName() + "/" + sitePinName
                        + " is not a dedicated native SitePin");
        require(output ? site.isOutputPin(sitePinName) : site.isInputPin(sitePinName),
                site.getName() + "/" + sitePinName + " direction is invalid");
        return pin;
    }

    private static CarryEndpoint carryEndpoint(
            Site site, String pinName, String sitePinName, boolean output) {
        BELPin pin = requireCarryPin(site, pinName, sitePinName, output);
        if (pin == null) return null;
        Node node = pin.getExternalNode(site);
        if (node == null || node.isInvalidNode()) return null;
        Wire[] members = node.getAllWiresInNode();
        if (members == null || members.length == 0) return null;
        String[] memberSeal = nodeMemberSeal(node);
        return new CarryEndpoint(
                site,
                node,
                nodeKey(node),
                memberSeal[0],
                Integer.parseInt(memberSeal[1]));
    }

    private static TreeMap<CapacityKey, Long> capacity(Device device) {
        TreeMap<CapacityKey, Long> result = new TreeMap<>();
        for (Site site : device.getAllSites()) {
            require(site != null && site.getTile() != null,
                    "device contains a null site or site tile");
            ClockRegion clockRegion = site.getClockRegion();
            SLR slr = site.getTile().getSLR();
            require(clockRegion != null,
                    site.getName() + " has no native clock-region membership");
            require(slr != null,
                    site.getName() + " has no native SLR membership");
            CapacityKey key = new CapacityKey(
                    slr.getName(), clockRegion.getName(),
                    site.getSiteTypeEnum().name());
            result.put(key, result.getOrDefault(key, 0L) + 1L);
        }
        require(!result.isEmpty(), "device contains no capacity buckets");
        return result;
    }

    private static List<CarryEdge> carryEdges(Device device) {
        List<CarryEndpoint> sources = new ArrayList<>();
        Map<String, CarryEndpoint> targetsByNode = new HashMap<>();
        for (Site site : device.getAllSites()) {
            if (site == null || site.getBEL("CARRY8") == null) continue;
            CarryEndpoint source = carryEndpoint(site, "CO7", "COUT", true);
            CarryEndpoint target = carryEndpoint(site, "CIN", "CIN", false);
            if (source != null) sources.add(source);
            if (target != null) {
                require(targetsByNode.put(target.nodeKey, target) == null,
                        "multiple CARRY8/CIN endpoints share a canonical node");
            }
        }
        List<CarryEdge> edges = new ArrayList<>();
        for (CarryEndpoint source : sources) {
            CarryEndpoint target = targetsByNode.get(source.nodeKey);
            if (target == null) continue;
            require(!source.site.getName().equals(target.site.getName()),
                    "CARRY_NEXT self edge is invalid");
            require(source.nodeKey.equals(target.nodeKey),
                    "CARRY_NEXT endpoints do not share one canonical node");
            require(source.memberSha256.equals(target.memberSha256)
                            && source.memberCount == target.memberCount,
                    "CARRY_NEXT endpoints disagree on canonical-node members");
            SLR sourceSlr = source.site.getTile().getSLR();
            SLR targetSlr = target.site.getTile().getSLR();
            require(sourceSlr != null && targetSlr != null
                            && sourceSlr.getName().equals(targetSlr.getName()),
                    "CARRY_NEXT native edge crosses SLRs");
            edges.add(new CarryEdge(source, target));
        }
        edges.sort(Comparator.comparing((CarryEdge edge) -> edge.source.site.getName())
                .thenComparing(edge -> edge.target.site.getName()));
        return edges;
    }

    private static List<List<String>> carryChains(List<CarryEdge> edges) {
        Map<String, String> next = new HashMap<>();
        Set<String> targets = new HashSet<>();
        for (CarryEdge edge : edges) {
            String source = edge.source.site.getName();
            String target = edge.target.site.getName();
            require(next.put(source, target) == null,
                    "CARRY_NEXT source has multiple targets");
            require(targets.add(target), "CARRY_NEXT target has multiple sources");
        }
        List<String> starts = new ArrayList<>();
        for (String source : next.keySet()) {
            if (!targets.contains(source)) starts.add(source);
        }
        Collections.sort(starts);
        List<List<String>> chains = new ArrayList<>();
        Set<String> visitedEdges = new HashSet<>();
        for (String start : starts) {
            List<String> chain = new ArrayList<>();
            Set<String> chainSites = new HashSet<>();
            String site = start;
            chain.add(site);
            chainSites.add(site);
            while (next.containsKey(site)) {
                require(visitedEdges.add(site), "CARRY_NEXT edge visited twice");
                site = next.get(site);
                require(chainSites.add(site), "CARRY_NEXT native graph has a cycle");
                chain.add(site);
            }
            require(chain.size() >= 2, "CARRY_NEXT chain is too short");
            chains.add(chain);
        }
        require(visitedEdges.size() == edges.size(),
                "CARRY_NEXT native graph contains a cycle or orphan edge");
        chains.sort(Comparator.comparing(chain -> String.join("\u0000", chain)));
        return chains;
    }

    private static String proofSha256(List<CarryEdge> edges) {
        List<String> proofs = new ArrayList<>();
        for (CarryEdge edge : edges) {
            proofs.add("CARRY_NEXT\u0000"
                    + edge.source.site.getName() + "\u0000CARRY8\u0000CO7\u0000COUT\u0000"
                    + edge.target.site.getName() + "\u0000CARRY8\u0000CIN\u0000CIN\u0000"
                    + edge.source.nodeKey + "\u0000" + edge.source.memberCount + "\u0000"
                    + edge.source.memberSha256);
        }
        Collections.sort(proofs);
        return sha256(String.join("\n", proofs));
    }

    private static String endpoint(boolean source) {
        return "{"
                + "\"bel\":\"CARRY8\","
                + "\"bel_pin\":" + quote(source ? "CO7" : "CIN") + ","
                + "\"logical_port\":" + quote(source ? "CO" : "CI") + ","
                + "\"selection\":" + (source
                    ? "{\"index\":7,\"kind\":\"bit\"}"
                    : "{\"kind\":\"all\"}") + ","
                + "\"site_pin\":" + quote(source ? "COUT" : "CIN")
                + "}";
    }

    private static String chainsJson(List<List<String>> chains) {
        StringBuilder result = new StringBuilder("[");
        for (int chainIndex = 0; chainIndex < chains.size(); ++chainIndex) {
            if (chainIndex > 0) result.append(',');
            result.append('[');
            List<String> chain = chains.get(chainIndex);
            for (int siteIndex = 0; siteIndex < chain.size(); ++siteIndex) {
                if (siteIndex > 0) result.append(',');
                result.append(quote(chain.get(siteIndex)));
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

    private static String adjacencyJson(
            List<CarryEdge> edges, List<List<String>> chains) {
        if (edges.isEmpty()) return "[]";
        return "[{"
                + "\"chains\":" + chainsJson(chains) + ","
                + "\"edge_count\":" + edges.size() + ","
                + "\"kind\":\"CARRY_NEXT\","
                + "\"native_proof_sha256\":" + quote(proofSha256(edges)) + ","
                + "\"proof_method\":" + quote(PROOF_METHOD) + ","
                + "\"source_endpoint\":" + endpoint(true) + ","
                + "\"target_endpoint\":" + endpoint(false) + "}]";
    }

    /* All object keys are lexicographically ordered for Python's canonical seal. */
    private static String payload(
            String deviceName,
            String fullPart,
            String version,
            String revision,
            String manifestSha256,
            String databaseMd5,
            String architectureSha256,
            TreeMap<CapacityKey, Long> capacity,
            List<CarryEdge> edges,
            List<List<String>> chains) {
        Set<String> slrs = new HashSet<>();
        Set<String> clockRegions = new HashSet<>();
        long sites = 0;
        for (Map.Entry<CapacityKey, Long> entry : capacity.entrySet()) {
            slrs.add(entry.getKey().slr);
            clockRegions.add(entry.getKey().clockRegion);
            sites += entry.getValue();
        }
        return "{"
                + "\"capabilities\":{"
                + "\"clock_region_site_capacity\":\"native_supported\","
                + "\"dedicated_adjacency.BRAM_CASCADE\":\"unverified\","
                + "\"dedicated_adjacency.CARRY_NEXT\":"
                + quote(edges.isEmpty() ? "core_missing" : "native_supported") + ","
                + "\"dedicated_adjacency.DSP_CASCADE\":\"unverified\","
                + "\"dedicated_adjacency.URAM_CASCADE\":\"unverified\","
                + "\"half_column_clock_capacity\":\"unverified\","
                + "\"slr_site_capacity\":\"native_supported\"},"
                + "\"dedicated_adjacency\":" + adjacencyJson(edges, chains) + ","
                + "\"site_capacity\":" + capacityJson(capacity) + ","
                + "\"source\":{"
                + "\"architecture_sha256\":" + quote(architectureSha256) + ","
                + "\"device\":" + quote(deviceName) + ","
                + "\"device_database_md5\":" + quote(databaseMd5) + ","
                + "\"full_part\":" + quote(fullPart) + ","
                + "\"generator\":{"
                + "\"revision\":" + quote(revision) + ","
                + "\"version\":" + quote(version) + "},"
                + "\"provider_manifest_sha256\":" + quote(manifestSha256) + ","
                + "\"route_backend\":" + quote(ROUTE_BACKEND) + "},"
                + "\"summary\":{"
                + "\"capacity_buckets\":" + capacity.size() + ","
                + "\"clock_regions\":" + clockRegions.size() + ","
                + "\"dedicated_edges\":" + edges.size() + ","
                + "\"sites\":" + sites + ","
                + "\"slrs\":" + slrs.size() + "}"
                + "}";
    }

    public static void main(String[] args) throws IOException {
        require(args.length == 8,
                "usage: <device> <full-part> <version> <revision> "
                        + "<manifest-sha256> <database-md5> "
                        + "<architecture-sha256> <output>");
        Device device = Device.getDevice(args[1]);
        require(device != null, "RapidWright could not load " + args[1]);
        require(device.getName().equalsIgnoreCase(args[0]),
                "native device name does not match provider manifest");
        TreeMap<CapacityKey, Long> capacity = capacity(device);
        List<CarryEdge> edges = carryEdges(device);
        List<List<String>> chains = carryChains(edges);
        String payload = payload(
                args[0], args[1], args[2], args[3], args[4], args[5], args[6],
                capacity, edges, chains);
        String document = "{\"payload\":" + payload
                + ",\"payload_sha256\":" + quote(sha256(payload))
                + ",\"schema\":" + quote(SCHEMA) + "}\n";
        Path output = Paths.get(args[7]);
        if (output.getParent() != null) Files.createDirectories(output.getParent());
        Files.write(output, document.getBytes(StandardCharsets.UTF_8));
    }
}
