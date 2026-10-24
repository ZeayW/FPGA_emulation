/* Device-backed qualification of the extended clock-path PIP direction. */
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.HashSet;
import java.util.Set;
import java.util.Map;
import java.util.TreeMap;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.io.BufferedReader;
import java.io.InputStream;
import java.security.MessageDigest;
import org.json.JSONArray;
import org.json.JSONObject;
import com.xilinx.rapidwright.device.ClockRegion;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SiteTypeEnum;

public final class EmuFlowClockPathProbe {
    private static String digest(Path path) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream stream = Files.newInputStream(path)) {
            byte[] buffer = new byte[1 << 20];
            for (int count; (count = stream.read(buffer)) >= 0;) digest.update(buffer, 0, count);
        }
        StringBuilder result = new StringBuilder();
        for (byte value : digest.digest()) result.append(String.format("%02x", value & 255));
        return result.toString();
    }

    private static JSONArray regionCounts(Map<String, Set<String>> clocks) {
        JSONArray rows = new JSONArray();
        for (Map.Entry<String, Set<String>> entry : clocks.entrySet()) {
            String[] key = entry.getKey().split("\\t", -1);
            rows.put(new JSONObject().put("case", key[0]).put("clock_region", key[1])
                .put("clock_nets", entry.getValue().size()));
        }
        return rows;
    }

    private static void auditClockBufferAllocation(Path input,Path output) throws Exception {
        Set<String> gates=new HashSet<>(),clockNets=new HashSet<>(),noncascadeNets=new HashSet<>();
        Map<String,String[]> cells=new TreeMap<>();Map<String,String> outputs=new TreeMap<>();
        String part=null;
        try (BufferedReader reader=Files.newBufferedReader(input,StandardCharsets.UTF_8)) {
            for (String line;(line=reader.readLine())!=null;) {
                String[] row=line.split("\\t",-1);
                if (row[0].equals("META") && row.length==3 && row[1].equals("part")) part=row[2];
                if (row[0].equals("CLOCK_GATE")) {
                    if (row.length!=6 || !gates.add(row[1])) throw new IllegalArgumentException("invalid clock gate");
                }
            }
        }
        if (part==null || gates.isEmpty()) throw new IllegalArgumentException("no part or clock gates");
        Map<String,String> driverByNet=new TreeMap<>();
        try (BufferedReader reader=Files.newBufferedReader(input,StandardCharsets.UTF_8)) {
            for (String line;(line=reader.readLine())!=null;) {
                String[] row=line.split("\\t",-1);
                if (row[0].equals("CELL")) {
                    if (row.length!=6) throw new IllegalArgumentException("invalid cell");
                    if (gates.contains(row[1])) cells.put(row[1],row);
                    else if (row[3].equals("BUFGCE")) throw new IllegalArgumentException("unmodeled explicit BUFGCE");
                } else if (row[0].equals("NET") && row.length==3 && row[2].equals("clock")) clockNets.add(row[1]);
                else if (row[0].equals("PIN") && row.length==5) {
                    if (row[4].equals("driver") && gates.contains(row[2]) && row[3].equals("O")) {
                        if (driverByNet.put(row[1],row[2])!=null || outputs.put(row[2],row[1])!=null)
                            throw new IllegalArgumentException("duplicate clock gate output");
                    } else if (row[4].equals("sink") && driverByNet.containsKey(row[1]) && !gates.contains(row[2]))
                        noncascadeNets.add(row[1]);
                }
            }
        }
        if (!cells.keySet().equals(gates) || !outputs.keySet().equals(gates))
            throw new IllegalArgumentException("incomplete gate cell/output coverage");
        Device device=Device.getDevice(part);
        List<Site> available=new ArrayList<>(List.of(device.getAllSitesOfType(SiteTypeEnum.BUFGCE)));
        Map<String,Long> regions=new TreeMap<>();Map<Node,Set<String>> mandatoryUsers=new TreeMap<>(java.util.Comparator.comparing(Node::toString));
        JSONArray bindings=new JSONArray();
        // Filtering the adapter's lexically ordered cell list preserves exactly
        // its BUFGCE allocation order; other supported cells consume no BUFGCE.
        for (Map.Entry<String,String[]> entry : cells.entrySet()) {
            String gate=entry.getKey();String[] row=entry.getValue();String net=outputs.get(gate);
            if (!row[3].equals("LUT2") || !clockNets.contains(net)) throw new IllegalArgumentException("unqualified clock root");
            Site site=EmuFlowRWRoute.allocateClockBufferSite(device,row[4],available);
            ClockRegion cr=site.getTile().getClockRegion();String region=cr==null?"@unassigned":cr.getName();
            regions.merge(region,1L,Long::sum);
            Node node=site.getConnectedNode("CLK_OUT");Set<Node> visited=new HashSet<>();JSONArray prefix=new JSONArray();
            for (int depth=0;depth<16 && visited.add(node);depth++) {
                prefix.put(node.toString());
                if (noncascadeNets.contains(net)) mandatoryUsers.computeIfAbsent(node,ignored -> new HashSet<>()).add(net);
                Set<Node> children=new HashSet<>();
                for (Node child : node.getAllDownhillNodes()) if (EmuFlowRWRoute.isDedicatedClockNode(child)) children.add(child);
                if (children.size()!=1 || node.getIntentCode().name().equals("NODE_GLOBAL_LEAF")) break;
                node=children.iterator().next();
            }
            bindings.put(new JSONObject().put("safe_cell",gate).put("logical_cell",row[2]).put("clock_net",net)
                .put("original_site",row[4]).put("bufgce_site",site.getName()).put("tile_clock_region",region)
                .put("has_noncascade_consumer",noncascadeNets.contains(net)).put("unique_source_prefix",prefix));
        }
        JSONObject conflicts=EmuFlowRWRoute.clockConflictSummary(mandatoryUsers,Node::toString,
            node -> node.getIntentCode().name(),node -> {
                ClockRegion cr=node.getTile().getClockRegion();return cr==null?null:cr.getName();
            });
        JSONObject report=new JSONObject().put("schema","emuflow.clock-buffer-allocation-audit/v1")
            .put("qualification","deterministic-allocation-and-unique-dedicated-source-prefix-only")
            .put("input_sha256",digest(input)).put("part",part).put("clock_gates",gates.size())
            .put("source_tile_region_counts",new JSONObject(regions)).put("mandatory_prefix_conflicts",conflicts)
            .put("bindings",bindings);
        Files.writeString(output,report.toString()+"\n",StandardCharsets.UTF_8);
        System.out.println("clock buffer allocation: gates="+gates.size()+" source_regions="+regions
            +" mandatory_prefix_conflicts="+conflicts);
    }

    private static void auditSiteClocks(String deviceName, Path input, Path output) throws Exception {
        Device device = Device.getDevice(deviceName);
        if (device == null) throw new IllegalArgumentException("unknown device " + deviceName);
        Map<String, Set<String>> declared = new TreeMap<>();
        Map<String, Set<String>> actual = new TreeMap<>();
        Map<String, Set<String>> tileActual = new TreeMap<>();
        JSONArray examples = new JSONArray();
        long records = 0, mismatches = 0, siteTileDifferences = 0;
        try (BufferedReader reader = Files.newBufferedReader(input, StandardCharsets.UTF_8)) {
            for (String line; (line = reader.readLine()) != null;) {
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] row = line.split("\\t", -1);
                if (row.length != 5) throw new IllegalArgumentException("expected case, region, SLR, site, clock");
                for (String value : row) if (value.isEmpty()) throw new IllegalArgumentException("empty audit field");
                com.xilinx.rapidwright.device.Site site = device.getSite(row[3]);
                if (site == null || site.getClockRegion() == null || site.getTile().getClockRegion() == null
                    || site.getTile().getSLR() == null) throw new IllegalArgumentException("unknown site region " + row[3]);
                String region = site.getClockRegion().getName();
                String tileRegion = site.getTile().getClockRegion().getName();
                String slr = site.getTile().getSLR().getName();
                declared.computeIfAbsent(row[0]+"\t"+row[1], ignored -> new HashSet<>()).add(row[4]);
                actual.computeIfAbsent(row[0]+"\t"+region, ignored -> new HashSet<>()).add(row[4]);
                tileActual.computeIfAbsent(row[0]+"\t"+tileRegion, ignored -> new HashSet<>()).add(row[4]);
                records++;
                if (!region.equals(tileRegion)) siteTileDifferences++;
                if (!region.equals(row[1]) || !slr.equals(row[2])) {
                    mismatches++;
                    if (examples.length() < 16) examples.put(new JSONObject().put("case", row[0])
                        .put("site", row[3]).put("declared_region", row[1]).put("actual_region", region)
                        .put("tile_region", tileRegion).put("declared_slr", row[2]).put("actual_slr", slr));
                }
            }
        }
        if (records == 0) throw new IllegalArgumentException("empty clock-site audit");
        JSONObject report = new JSONObject().put("schema", "emuflow.clock-site-membership-audit/v1")
            .put("status", mismatches == 0 ? "pass" : "fail")
            .put("qualification", "site-membership-and-clock-counts-only")
            .put("device", device.getName()).put("input_sha256", digest(input)).put("records", records)
            .put("mismatched_site_clock_relations", mismatches).put("site_tile_region_differences", siteTileDifferences)
            .put("examples", examples).put("declared_regions", regionCounts(declared))
            .put("device_site_regions", regionCounts(actual)).put("device_tile_regions", regionCounts(tileActual));
        Files.writeString(output, report.toString()+"\n", StandardCharsets.UTF_8);
        System.out.println("clock-site audit: records="+records+" mismatches="+mismatches
            +" site_tile_region_differences="+siteTileDifferences);
        if (mismatches != 0) throw new IllegalStateException("declared region membership differs from the device");
    }

    public static void main(String[] args) throws Exception {
        if (args.length==3 && args[0].equals("--audit-clock-buffer-allocation")) {
            auditClockBufferAllocation(Path.of(args[1]),Path.of(args[2]));return;
        }
        if (args.length >= 3 && args[0].equals("--clock-source-prefix")) {
            Device device=Device.getDevice(args[1]);
            for (int i=2;i<args.length;i++) {
                Node node=device.getSite(args[i]).getConnectedNode("CLK_OUT");
                Set<Node> visited=new HashSet<>();
                System.out.println("clock-source-site " + args[i]);
                for (int depth=0;depth<16 && visited.add(node);depth++) {
                    List<Node> children=new ArrayList<>();
                    for (Node child : node.getAllDownhillNodes())
                        if (EmuFlowRWRoute.isDedicatedClockNode(child) && !children.contains(child)) children.add(child);
                    System.out.println(node + " intent=" + node.getIntentCode() + " clock_children=" + children.size());
                    if (children.size()!=1) {
                        for (Node child : children) System.out.println("  child " + child + " " + child.getIntentCode());
                        break;
                    }
                    node=children.get(0);
                }
            }
            return;
        }
        if (args.length == 4 && args[0].equals("--clock-graph")) {
            Device device = Device.getDevice(args[1]);
            String[] a = args[2].split("/", 2), b = args[3].split("/", 2);
            Node source = new Node(device.getTile(a[0]), device.getTile(a[0]).getWireIndex(a[1]));
            Node sink = new Node(device.getTile(b[0]), device.getTile(b[0]).getWireIndex(b[1]));
            Map<Node, Node> tree = EmuFlowRWRoute.availableClockTree(source,
                Node::getAllDownhillNodes, EmuFlowRWRoute::isDedicatedClockNode, n -> true);
            Map<String, Integer> intents = new TreeMap<>();
            for (Node node : tree.keySet()) intents.merge(node.getIntentCode().name(), 1, Integer::sum);
            System.out.println("clock-graph nodes=" + tree.size() + " intents=" + intents);
            List<Node> path = EmuFlowRWRoute.availableClockSinkPath(sink, tree,
                Node::getAllUphillNodes, n -> n.getIntentCode().name().equals("NODE_GLOBAL_LEAF"), n -> true, n -> !EmuFlowRWRoute.isDedicatedClockNode(n), 16);
            List<PIP> pips = EmuFlowRWRoute.extendedClockPathPIPs(path);
            for (Node node : path) System.out.println(node.getIntentCode() + " " + node);
            System.out.println("PASS: device clock path has " + pips.size() + " directed PIPs");
            return;
        }
        if (args.length == 1 && args[0].equals("--clock-tree-self-test")) {
            Map<String, List<String>> down = Map.of(
                "source", List.of("blocked", "spine"),
                "blocked", List.of("unreachable_leaf"),
                "spine", List.of("source", "leaf"),
                "leaf", List.of("fabric")
            );
            Map<String, String> tree = EmuFlowRWRoute.availableClockTree(
                "source", n -> down.getOrDefault(n, List.of()),
                n -> !n.equals("fabric"), n -> !n.equals("blocked")
            );
            if (!tree.keySet().equals(Set.of("source", "spine", "leaf"))) {
                throw new IllegalStateException("clock search crossed occupied/non-dedicated nodes: " + tree);
            }
            Map<String, List<String>> up = Map.of(
                "sink", List.of("unreachable_leaf", "tail"),
                "tail", List.of("leaf")
            );
            List<String> path = EmuFlowRWRoute.availableClockSinkPath(
                "sink", tree, n -> up.getOrDefault(n, List.of()),
                n -> n.endsWith("leaf"), n -> true, n -> !n.endsWith("leaf"), 2
            );
            if (!path.equals(List.of("source", "spine", "leaf", "tail", "sink"))) {
                throw new IllegalStateException("clock selected an unreachable nearer leaf: " + path);
            }
            for (int depth : new int[] {0, 1}) {
                try {
                    EmuFlowRWRoute.availableClockSinkPath("sink", tree,
                        n -> up.getOrDefault(n, List.of()), n -> n.endsWith("leaf"), n -> true, n -> !n.endsWith("leaf"), depth);
                } catch (IllegalStateException expected) { continue; }
                throw new IllegalStateException("clock tail exceeded depth bound " + depth);
            }
            Map<String,List<String>> weightedDown=Map.of(
                "source",List.of("busy","free"),"busy",List.of("leaf"),
                "free",List.of("leaf","source"));
            EmuFlowRWRoute.ClockTree<String> weighted=EmuFlowRWRoute.weightedClockTree(
                "source",n -> weightedDown.getOrDefault(n,List.of()),n -> true,n -> true,
                n -> n.equals("busy")?20.0:1.0);
            if (!weighted.parents.get("leaf").equals("free") || weighted.distance.get("leaf")!=2.0)
                throw new IllegalStateException("clock tree ignored congestion penalty");
            Map<String,String> alternatives=new java.util.HashMap<>();
            alternatives.put("source",null);alternatives.put("leaf0","source");alternatives.put("leaf1","source");
            List<String> chosen=EmuFlowRWRoute.availableClockSinkPath("sink",alternatives,
                n -> n.equals("sink")?List.of("leaf0","leaf1"):List.of(),
                n -> n.startsWith("leaf"),n -> true,n -> true,2,
                n -> n.equals("leaf0")?20.0:1.0,n -> 1.0);
            if (!chosen.equals(List.of("source","leaf1","sink")))
                throw new IllegalStateException("sink ignored alternate leaf congestion cost");
            try {
                EmuFlowRWRoute.availableClockTree("source", n -> List.of(), n -> true, n -> false);
            } catch (IllegalStateException expected) {
                System.out.println("PASS: clock search excludes occupied nodes, fabric detours, cycles and unreachable leaves; tail bounds and occupied sources enforced");
                return;
            }
            throw new IllegalStateException("occupied clock source was accepted");
        }
        if (args.length == 1 && args[0].equals("--clock-owner-self-test")) {
            EmuFlowRWRoute.DistinctClockNodeUsers<String> users = new EmuFlowRWRoute.DistinctClockNodeUsers<>();
            users.add("n0", "a"); users.add("n0", "a"); users.add("n1", "a");
            users.add("n1", "b"); users.add("n1", "b"); users.add("n1", "a");
            users.add("n1", "c"); users.add("n2", "b");
            if (users.firstOwner.size()!=3 || users.sharedOwners.size()!=1 || users.maximumUsers!=3
                || users.sharedOwners.get("n1").size()!=3) throw new IllegalStateException("clock node ownership lost distinct-user coverage");
            Map<String,Set<String>> occupancy=new TreeMap<>();
            for (int i=0;i<12;i++) occupancy.put(String.format("n%02d",i),Set.of("a","b"));
            occupancy.put("n99",Set.of("c","b","a"));occupancy.put("single",Set.of("a"));
            JSONObject conflicts=EmuFlowRWRoute.clockConflictSummary(occupancy,n -> n,
                n -> n.equals("n99")?"spine":"leaf",n -> n.equals("n99")?null:"X0Y0");
            JSONArray examples=conflicts.getJSONArray("examples");
            if (conflicts.getLong("shared_nodes")!=13 || conflicts.getLong("excess_users")!=14
                || conflicts.getInt("maximum_users")!=3 || examples.length()!=8
                || conflicts.getJSONObject("shared_node_intents").getInt("leaf")!=12
                || conflicts.getJSONObject("shared_node_tile_regions").getInt("@unassigned")!=1
                || !examples.getJSONObject(0).getString("node").equals("n99")
                || !examples.getJSONObject(7).getString("node").equals("n06")
                || !examples.getJSONObject(0).getJSONArray("clock_nets").toString().equals("[\"a\",\"b\",\"c\"]"))
                throw new IllegalStateException("clock conflict attribution/count/order/bound is invalid: "+conflicts);
            System.out.println("PASS: repeated PIP endpoints do not duplicate clock users; shared-node ownership is exact");
            System.out.println("PASS: clock conflict summaries retain exact totals and eight deterministic owner examples");
            return;
        }
        if (args.length == 4 && args[0].equals("--audit-site-clocks")) {
            auditSiteClocks(args[1], Path.of(args[2]), Path.of(args[3]));
            return;
        }
        if (args.length >= 4 && args[0].equals("--regions")) {
            Device device = Device.getDevice(args[1]);
            String[] leafName = args[2].split("/", 2);
            ClockRegion leafRegion = device.getTile(leafName[0]).getClockRegion();
            Set<ClockRegion> sinks = new HashSet<>();
            for (int i = 3; i < args.length; i++) {
                sinks.add(device.getSite(args[i]).getTile().getClockRegion());
            }
            System.out.println("leaf region=" + leafRegion + "; sink regions=" + sinks
                + "; missing leaf distribution=" + !sinks.contains(leafRegion));
            return;
        }
        if (args.length < 3) {
            throw new IllegalArgumentException("usage: <device> <source node> ... <sink node>");
        }
        Device device = Device.getDevice(args[0]);
        List<Node> nodes = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            String[] parts = args[i].split("/", 2);
            Tile tile = device.getTile(parts[0]);
            nodes.add(new Node(tile, tile.getWireIndex(parts[1])));
        }
        List<PIP> pips = EmuFlowRWRoute.extendedClockPathPIPs(nodes);
        System.out.println("leaf region=" + nodes.get(0).getTile().getClockRegion()
            + "; sink region=" + nodes.get(nodes.size() - 1).getTile().getClockRegion());
        for (int i = 0; i < pips.size(); i++) {
            PIP pip = pips.get(i);
            Node start = pip.isReversed() ? pip.getEndNode() : pip.getStartNode();
            Node end = pip.isReversed() ? pip.getStartNode() : pip.getEndNode();
            if (!start.equals(nodes.get(i)) || !end.equals(nodes.get(i + 1))) {
                throw new IllegalStateException("PIP direction differs at hop " + i);
            }
        }
        Collections.reverse(nodes);
        try {
            EmuFlowRWRoute.extendedClockPathPIPs(nodes);
        } catch (IllegalStateException expected) {
            System.out.println("PASS: forward path " + pips.size()
                + " PIPs; reversed path rejected");
            return;
        }
        throw new IllegalStateException("reversed clock path was accepted");
    }
}
