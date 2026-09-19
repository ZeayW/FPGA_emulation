/*
 * Copyright (c) EmuFlow contributors.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Stream a compact integrity certificate from RapidWright's native device
 * database.  This deliberately does not materialize the complete routing
 * graph: XCVU19P is large enough that duplicating all wires and nodes in an
 * FPGA Interchange DeviceResources message exceeds the memory budget of the
 * validation farm.  RWRoute consumes the same native database directly.
 */

import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.Wire;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

public final class EmuFlowRouteResourceCertificate {
    private static final String SCHEMA =
            "emuflow.rapidwright-route-resource-certificate/v1";
    private static final String ROUTE_BACKEND =
            "rapidwright-native-device-database-v1";

    private static final class Counts {
        long allTiles;
        long siteTypes;
        long tileTypes;
        long allSites;
        long wires;
        long wiresWithoutNode;
        long wiresUnaccounted;
        long nodes;
        long nodeWireMemberships;
        long pips;
    }

    private EmuFlowRouteResourceCertificate() {}

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new IllegalStateException(message);
        }
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

    private static boolean sameWire(Node node, Tile tile, int wireIndex) {
        return node.getTile() == tile && node.getWireIndex() == wireIndex;
    }

    private static Counts validateAndCount(Device device) {
        Counts counts = new Counts();
        counts.siteTypes = device.getSiteTypeCount();
        counts.tileTypes = device.getTileTypeCount();
        counts.allSites = device.getAllSites().length;

        for (Tile tile : device.getAllTiles()) {
            require(tile != null, "device contains a null tile");
            ++counts.allTiles;
            int wireCount = tile.getWireCount();
            require(wireCount >= 0, "tile has a negative wire count");
            counts.wires += wireCount;

            for (int wireIndex = 0; wireIndex < wireCount; ++wireIndex) {
                Node node = Node.getNode(tile, wireIndex);
                if (node == null || node.isInvalidNode()) {
                    ++counts.wiresWithoutNode;
                    continue;
                }
                Tile rootTile = node.getTile();
                int rootWireIndex = node.getWireIndex();
                require(rootTile != null, "node has a null root tile");
                require(rootWireIndex >= 0
                                && rootWireIndex < rootTile.getWireCount(),
                        "node root wire index is out of range");
                if (!sameWire(node, tile, wireIndex)) {
                    continue;
                }

                Wire[] members = node.getAllWiresInNode();
                require(members != null, "canonical node has null members");
                // RapidWright represents an unconnected tile wire as a
                // self-rooted Node whose expanded membership is empty.  It is
                // not a routable node and is accounted for with null nodes.
                if (members.length == 0) {
                    ++counts.wiresWithoutNode;
                    continue;
                }
                ++counts.nodes;
                for (Wire member : members) {
                    require(member != null && member.getTile() != null,
                            "node contains a null wire or tile");
                    int memberIndex = member.getWireIndex();
                    require(memberIndex >= 0
                                    && memberIndex < member.getTile().getWireCount(),
                            "node member wire index is out of range");
                    Node resolved = Node.getNode(member.getTile(), memberIndex);
                    require(resolved != null && !resolved.isInvalidNode(),
                            "node member does not resolve to a valid node");
                    require(sameWire(resolved, rootTile, rootWireIndex),
                            "node member resolves to a different canonical node");
                    ++counts.nodeWireMemberships;
                }
            }

            for (PIP pip : tile.getPIPs()) {
                require(pip != null && pip.getTile() == tile,
                        "PIP has a null or mismatched tile");
                int start = pip.getStartWireIndex();
                int end = pip.getEndWireIndex();
                require(start >= 0 && start < wireCount,
                        "PIP start wire index is out of range");
                require(end >= 0 && end < wireCount,
                        "PIP end wire index is out of range");
                ++counts.pips;
            }
        }

        require(counts.allTiles > 0, "device contains no tiles");
        require(counts.wires > 0, "device contains no routing wires");
        require(counts.nodes > 0, "device contains no routing nodes");
        require(counts.nodeWireMemberships > 0,
                "device contains no node-to-wire memberships");
        require(counts.pips > 0, "device contains no programmable interconnects");
        require(counts.nodeWireMemberships + counts.wiresWithoutNode
                        <= counts.wires,
                "route traversal counted more wire memberships than wires");
        counts.wiresUnaccounted = counts.wires
                - counts.nodeWireMemberships
                - counts.wiresWithoutNode;
        return counts;
    }

    /*
     * Keys are emitted in lexicographic order at every level.  Python checks
     * the hash using json.dumps(sort_keys=True, separators=(",", ":")).
     */
    private static String payload(
            String deviceName,
            String fullPart,
            String version,
            String revision,
            String manifestSha256,
            String deviceDatabaseMd5,
            Counts counts) {
        return "{"
                + "\"device\":" + quote(deviceName) + ","
                + "\"device_database_md5\":" + quote(deviceDatabaseMd5) + ","
                + "\"full_part\":" + quote(fullPart) + ","
                + "\"generator\":{"
                + "\"revision\":" + quote(revision) + ","
                + "\"version\":" + quote(version) + "},"
                + "\"provider_manifest_sha256\":" + quote(manifestSha256) + ","
                + "\"reference_integrity\":{"
                + "\"errors\":0,"
                + "\"route_resources_present\":true,"
                + "\"status\":\"pass\"},"
                + "\"resource_counts\":{"
                + "\"all_sites\":" + counts.allSites + ","
                + "\"all_tiles\":" + counts.allTiles + ","
                + "\"node_wire_memberships\":" + counts.nodeWireMemberships + ","
                + "\"nodes\":" + counts.nodes + ","
                + "\"pips\":" + counts.pips + ","
                + "\"site_types\":" + counts.siteTypes + ","
                + "\"tile_types\":" + counts.tileTypes + ","
                + "\"wires\":" + counts.wires + ","
                + "\"wires_unaccounted\":" + counts.wiresUnaccounted + ","
                + "\"wires_without_node\":" + counts.wiresWithoutNode + "},"
                + "\"route_backend\":" + quote(ROUTE_BACKEND) + ","
                + "\"timing_qualification\":"
                + quote("not-encoded-rwroute-native-device-database")
                + "}";
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 7) {
            System.err.println(
                    "usage: EmuFlowRouteResourceCertificate <device> <full-part> "
                    + "<rapidwright-version> <rapidwright-revision> "
                    + "<provider-manifest-sha256> <device-db-md5> <output.json>");
            System.exit(2);
        }
        String deviceName = args[0].toLowerCase();
        String fullPart = args[1].toLowerCase();
        String version = args[2];
        String revision = args[3].toLowerCase();
        String manifestSha256 = args[4].toLowerCase();
        String deviceDatabaseMd5 = args[5].toLowerCase();
        require(revision.matches("[0-9a-f]{40}"), "invalid RapidWright revision");
        require(manifestSha256.matches("[0-9a-f]{64}"),
                "invalid provider manifest SHA-256");
        require(deviceDatabaseMd5.matches("[0-9a-f]{32}"),
                "invalid device database MD5");

        Device device = Device.getDevice(deviceName);
        require(device != null, "RapidWright did not load the requested device");
        require(device.getName().equalsIgnoreCase(deviceName),
                "RapidWright loaded a different device");
        Counts counts = validateAndCount(device);
        String payload = payload(
                deviceName,
                fullPart,
                version,
                revision,
                manifestSha256,
                deviceDatabaseMd5,
                counts);
        String document = "{\"payload\":" + payload
                + ",\"payload_sha256\":\"" + sha256(payload) + "\""
                + ",\"schema\":\"" + SCHEMA + "\"}\n";
        Path output = Path.of(args[6]);
        Path parent = output.toAbsolutePath().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        Files.writeString(output, document, StandardCharsets.UTF_8);
    }
}
