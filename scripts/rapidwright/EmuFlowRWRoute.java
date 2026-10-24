/*
 * Build and route an explicitly placed UltraScale+ design with RWRoute.
 *
 * The input is a compact, tab-separated EmuFlow contract.  This adapter emits
 * a compact JSON route certificate; the Python checker independently rebuilds
 * every routed source-to-sink path and detects resource conflicts.
 */
import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.DesignTools;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.PinType;
import com.xilinx.rapidwright.design.SiteInst;
import com.xilinx.rapidwright.design.SitePinInst;
import com.xilinx.rapidwright.design.Unisim;
import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.BELPin;
import com.xilinx.rapidwright.device.ClockRegion;
import com.xilinx.rapidwright.device.IntentCode;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SiteTypeEnum;
import com.xilinx.rapidwright.edif.EDIFCell;
import com.xilinx.rapidwright.edif.EDIFNet;
import com.xilinx.rapidwright.edif.EDIFPort;
import com.xilinx.rapidwright.rwroute.CUFR;
import com.xilinx.rapidwright.rwroute.Connection;
import com.xilinx.rapidwright.rwroute.NetWrapper;
import com.xilinx.rapidwright.rwroute.NodeStatus;
import com.xilinx.rapidwright.rwroute.RouterHelper;
import com.xilinx.rapidwright.rwroute.RWRouteConfig;
import com.xilinx.rapidwright.timing.TimingModel;
import com.xilinx.rapidwright.util.ParallelismTools;
import java.io.BufferedInputStream;
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.DigestOutputStream;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;
import java.util.function.Predicate;
import org.json.JSONArray;
import org.json.JSONObject;

public final class EmuFlowRWRoute {
    private static final String SCHEMA = "emuflow.xilinx-route-db/v2";
    private static final String ROUTER_STRATEGY =
        "CUFR-HUS-non-timing-driven-uturn-enabled-parallel-unroutable-"
        + "recovery-available-clock-tree-v1";
    private static final double MAX_RECOVERY_ITERATION_SECONDS = 600.0;
    private static final Set<String> EXTENDED_LCB_CLOCK_NETS =
        ConcurrentHashMap.newKeySet();

    /** Count occupied hardware resources, not transformed Java cell objects. */
    private static JSONObject physicalResourceUsage(Design design) {
        int luts = 0, ffs = 0, carries = 0, dsps = 0, brams = 0, urams = 0;
        for (SiteInst siteInst : design.getSiteInsts()) {
            Collection<Cell> siteCells = siteInst.getCells();
            if (siteCells.isEmpty()) continue;
            String kind = siteInst.getSiteTypeEnum().name();
            if (kind.startsWith("SLICE")) {
                int lutMask = 0;
                boolean carry = false;
                for (Cell cell : siteCells) {
                    String bel = cell.getBEL().getName();
                    // 5LUT/6LUT halves share one CLB_LUT capacity unit.
                    if (bel.matches("[A-H][56]LUT")) lutMask |= 1 << (bel.charAt(0) - 'A');
                    else if (bel.matches("[A-H]FF2?")) ffs++;
                    else if (bel.equals("CARRY8")) carry = true;
                }
                luts += Integer.bitCount(lutMask);
                if (carry) carries++;
            } else if (kind.startsWith("DSP")) dsps++;
            else if (kind.startsWith("RAMB")) brams += kind.contains("36") ? 2 : 1;
            else if (kind.startsWith("URAM")) urams++;
        }
        return new JSONObject().put("lut", luts).put("ff", ffs)
            .put("carry8", carries).put("dsp48", dsps)
            .put("bram18k", brams).put("uram288", urams);
    }

    private static void checkResourceBudget(JSONObject usage, Map<String, String> metadata) {
        for (String field : usage.keySet()) {
            String limit = metadata.get("resource_limit_" + field);
            if (limit != null && usage.getInt(field) > Integer.parseInt(limit)) {
                throw new IllegalStateException("strict physical resource budget exceeded: "
                    + field + " used=" + usage.getInt(field) + " allowed=" + limit
                    + "; automatic relaxation is forbidden");
            }
        }
    }

    private static void reportHeap(String stage) {
        Runtime runtime = Runtime.getRuntime();
        long used = runtime.totalMemory() - runtime.freeMemory();
        System.out.printf(
            "INFO: EmuFlow heap stage=%s used_mib=%d committed_mib=%d max_mib=%d%n",
            stage,
            used / (1024L * 1024L),
            runtime.totalMemory() / (1024L * 1024L),
            runtime.maxMemory() / (1024L * 1024L)
        );
    }

    static List<PIP> extendedClockPathPIPs(List<Node> sourceToSinkPath) {
        if (sourceToSinkPath.size() < 2) {
            throw new IllegalArgumentException("extended clock path needs two nodes");
        }
        // The one-argument helper defaults to sink-to-source ordering.
        List<PIP> pips = RouterHelper.getPIPsFromNodes(sourceToSinkPath, true);
        if (pips.size() != sourceToSinkPath.size() - 1) {
            throw new IllegalStateException("extended clock path has missing PIPs");
        }
        return pips;
    }

    static boolean isDedicatedClockNode(Node node) {
        String intent = node.getIntentCode().name();
        // Device data also represents clock mux/buffer connector wires with
        // INTENT_DEFAULT. They are real clock graph edges, not fabric detours.
        return intent.startsWith("NODE_GLOBAL")
            || (intent.equals("INTENT_DEFAULT") && node.getWireName().startsWith("CLK_"));
    }

    /** Dedicated-clock reachability, checking every node before inserting an edge. */
    static <T> Map<T, T> availableClockTree(
        T source, Function<T, ? extends Iterable<T>> downhill,
        Predicate<T> dedicated, Predicate<T> available
    ) {
        if (source == null || !available.test(source)) {
            throw new IllegalStateException("global-clock source unavailable: " + source);
        }
        Map<T, T> parents = new HashMap<>();
        ArrayDeque<T> queue = new ArrayDeque<>();
        parents.put(source, null); queue.add(source);
        while (!queue.isEmpty()) {
            T node = queue.removeFirst();
            for (T next : downhill.apply(node)) {
                if (parents.containsKey(next) || !dedicated.test(next)
                    || !available.test(next)) continue;
                parents.put(next, node); queue.addLast(next);
            }
        }
        return parents;
    }

    /** Find a bounded, available sink tail that joins the reachable clock tree. */
    static <T> List<T> availableClockSinkPath(
        T sink, Map<T, T> parents, Function<T, ? extends Iterable<T>> uphill,
        Predicate<T> join, Predicate<T> available, int maximumDepth
    ) {
        if (sink == null || !available.test(sink)) {
            throw new IllegalStateException("global-clock sink unavailable: " + sink);
        }
        Map<T, T> towardSink = new HashMap<>();
        ArrayDeque<T> queue = new ArrayDeque<>();
        towardSink.put(sink, null); queue.add(sink);
        T selected = null;
        for (int depth = 0; depth <= maximumDepth && !queue.isEmpty(); depth++) {
            int count = queue.size();
            while (count-- > 0) {
                T node = queue.removeFirst();
                if (parents.containsKey(node) && join.test(node)) {
                    selected = node; break;
                }
                if (depth == maximumDepth) continue;
                for (T next : uphill.apply(node)) {
                    if (towardSink.containsKey(next) || !available.test(next)) continue;
                    towardSink.put(next, node); queue.addLast(next);
                }
            }
            if (selected != null) break;
        }
        if (selected == null) {
            throw new IllegalStateException("no reachable available clock leaf within "
                + maximumDepth + " uphill hops of " + sink);
        }
        List<T> path = new ArrayList<>();
        for (T cursor = selected; cursor != null; cursor = parents.get(cursor)) path.add(cursor);
        Collections.reverse(path);
        Set<T> pathNodes = new HashSet<>(path);
        for (T cursor = towardSink.get(selected); cursor != null; cursor = towardSink.get(cursor)) {
            if (!pathNodes.add(cursor)) {
                throw new IllegalStateException("clock tail creates a cycle: " + cursor);
            }
            path.add(cursor);
        }
        return path;
    }

    static final class DistinctClockNodeUsers<T> {
        final Map<T, String> firstOwner = new HashMap<>();
        final Map<T, Set<String>> sharedOwners = new HashMap<>();
        int maximumUsers = 0;

        void add(T node, String clock) {
            String first = firstOwner.putIfAbsent(node, clock);
            if (first == null) { maximumUsers = Math.max(maximumUsers, 1); return; }
            if (first.equals(clock)) return;
            Set<String> owners = sharedOwners.computeIfAbsent(node, ignored -> {
                Set<String> values = new HashSet<>(); values.add(first); return values;
            });
            owners.add(clock);
            maximumUsers = Math.max(maximumUsers, owners.size());
        }
    }
    private static final String[] DSP48E2_COMPONENTS = new String[] {
        "DSP_PREADD_DATA", "DSP_A_B_DATA", "DSP_C_DATA", "DSP_MULTIPLIER",
        "DSP_ALU", "DSP_M_DATA", "DSP_OUTPUT", "DSP_PREADD"
    };

    /**
     * CUFR's upstream --enlargeBoundingBox policy enlarges both unroutable and
     * merely congested connections. CUFR consequently rebuilds its complete
     * partition tree every iteration while that option is enabled. A fixed
     * bounding box is not complete either: upstream RWRoute abandons a truly
     * unroutable connection when enlargement is disabled.
     *
     * Route A therefore widens only connections for which the current search
     * found no route. Those exceptional connections are routed through their
     * own recursive partitioning ternary tree before the unchanged main tree
     * while they remain active and are skipped by the main tree itself. Stable
     * connections are pruned before the next tree is built. This preserves
     * CUFR's original parallel decomposition without serializing a large
     * exceptional set: rebuilding the complete tree around even a few
     * enlarged connections can move ordinary reroutes towards its sequential
     * middle branches.
     * Congested-but-routed connections remain under negotiated congestion and
     * do not trigger bounding-box growth.
     */
    private static final class UnroutableOnlyBoundingBoxCUFR extends CUFR {
        /**
         * Compact Route-A-owned equivalent of CUFR's RPTT, scoped only to
         * connections whose original bounding boxes were genuinely
         * unroutable. The structure follows the balance-driven cutline and
         * middle-before-left/right ordering of RapidWright's Apache-licensed
         * CUFRpartitionTree. Keeping it local is necessary because the
         * upstream node payload is package-private and CUFR's main tree is
         * private.
         */
        private static final class RecoveryPartitionTree {
            private static final class Bounds {
                final int xMin;
                final int xMax;
                final int yMin;
                final int yMax;

                Bounds(int xMin, int xMax, int yMin, int yMax) {
                    this.xMin = xMin;
                    this.xMax = xMax;
                    this.yMin = yMin;
                    this.yMax = yMax;
                }
            }

            private static final class Node {
                final Bounds bounds;
                final List<Connection> connections;
                Node middle;
                Node left;
                Node right;

                Node(Bounds bounds, List<Connection> connections) {
                    this.bounds = bounds;
                    this.connections = connections;
                }

                boolean isLeaf() {
                    return left == null;
                }
            }

            final Node root;
            private final Bounds deviceBounds;

            RecoveryPartitionTree(List<Connection> connections, int xMax, int yMax) {
                deviceBounds = new Bounds(0, xMax, 0, yMax);
                root = new Node(deviceBounds, connections);
                build(root);
            }

            private int clampX(int value) {
                return Math.min(Math.max(value, deviceBounds.xMin), deviceBounds.xMax);
            }

            private int clampY(int value) {
                return Math.min(Math.max(value, deviceBounds.yMin), deviceBounds.yMax);
            }

            private void build(Node node) {
                Collections.sort(node.connections);
                int width = node.bounds.xMax - node.bounds.xMin + 1;
                int height = node.bounds.yMax - node.bounds.yMin + 1;
                double bestScore = Double.POSITIVE_INFINITY;
                double bestPosition = Double.NaN;
                boolean splitX = true;

                if (width > 1) {
                    int[] before = new int[width - 1];
                    int[] after = new int[width - 1];
                    for (Connection connection : node.connections) {
                        int start = Math.max(
                            node.bounds.xMin, clampX(connection.getXMinBB())
                        ) - node.bounds.xMin;
                        int end = Math.min(
                            node.bounds.xMax, clampX(connection.getXMaxBB())
                        ) - node.bounds.xMin;
                        for (int x = start; x < width - 1; x++) before[x]++;
                        for (int x = 0; x < end; x++) after[x]++;
                    }
                    int maximumBefore = before[width - 2];
                    int maximumAfter = after[0];
                    for (int x = 0; x < width - 1; x++) {
                        if (before[x] == maximumBefore || after[x] == maximumAfter) continue;
                        int denominator = Math.max(before[x], after[x]);
                        if (denominator == 0) continue;
                        double score = (double) Math.abs(before[x] - after[x]) / denominator;
                        if (score < bestScore) {
                            bestScore = score;
                            bestPosition = node.bounds.xMin + x + 0.5;
                            splitX = true;
                        }
                    }
                }

                if (height > 1) {
                    int[] before = new int[height - 1];
                    int[] after = new int[height - 1];
                    for (Connection connection : node.connections) {
                        int start = Math.max(
                            node.bounds.yMin, clampY(connection.getYMinBB())
                        ) - node.bounds.yMin;
                        int end = Math.min(
                            node.bounds.yMax, clampY(connection.getYMaxBB())
                        ) - node.bounds.yMin;
                        for (int y = start; y < height - 1; y++) before[y]++;
                        for (int y = 0; y < end; y++) after[y]++;
                    }
                    int maximumBefore = before[height - 2];
                    int maximumAfter = after[0];
                    for (int y = 0; y < height - 1; y++) {
                        if (before[y] == maximumBefore || after[y] == maximumAfter) continue;
                        int denominator = Math.max(before[y], after[y]);
                        if (denominator == 0) continue;
                        double score = (double) Math.abs(before[y] - after[y]) / denominator;
                        if (score < bestScore) {
                            bestScore = score;
                            bestPosition = node.bounds.yMin + y + 0.5;
                            splitX = false;
                        }
                    }
                }

                if (Double.isNaN(bestPosition)) return;
                List<Connection> left = new ArrayList<>();
                List<Connection> middle = new ArrayList<>();
                List<Connection> right = new ArrayList<>();
                if (splitX) {
                    for (Connection connection : node.connections) {
                        if (clampX(connection.getXMaxBB()) < bestPosition) {
                            left.add(connection);
                        } else if (clampX(connection.getXMinBB()) > bestPosition) {
                            right.add(connection);
                        } else {
                            middle.add(connection);
                        }
                    }
                    int cut = (int) Math.floor(bestPosition);
                    node.left = new Node(
                        new Bounds(node.bounds.xMin, cut, node.bounds.yMin, node.bounds.yMax),
                        left
                    );
                    node.right = new Node(
                        new Bounds(cut + 1, node.bounds.xMax, node.bounds.yMin, node.bounds.yMax),
                        right
                    );
                } else {
                    for (Connection connection : node.connections) {
                        if (clampY(connection.getYMaxBB()) < bestPosition) {
                            left.add(connection);
                        } else if (clampY(connection.getYMinBB()) > bestPosition) {
                            right.add(connection);
                        } else {
                            middle.add(connection);
                        }
                    }
                    int cut = (int) Math.floor(bestPosition);
                    node.left = new Node(
                        new Bounds(node.bounds.xMin, node.bounds.xMax, node.bounds.yMin, cut),
                        left
                    );
                    node.right = new Node(
                        new Bounds(node.bounds.xMin, node.bounds.xMax, cut + 1, node.bounds.yMax),
                        right
                    );
                }
                if (left.isEmpty() || right.isEmpty()) {
                    node.left = null;
                    node.right = null;
                    return;
                }
                build(node.left);
                build(node.right);
                if (!middle.isEmpty()) {
                    node.middle = new Node(node.bounds, middle);
                    build(node.middle);
                }
            }
        }

        private final Set<Connection> recoveryConnections =
            ConcurrentHashMap.newKeySet();

        UnroutableOnlyBoundingBoxCUFR(Design design, RWRouteConfig config) {
            super(design, config);
            if (config.isEnlargeBoundingBox()) {
                throw new IllegalArgumentException(
                    "unroutable-only CUFR requires the upstream blanket "
                    + "bounding-box expansion policy to remain disabled"
                );
            }
        }

        private static final int MAX_LCB_SEARCH_DEPTH = 16;

        @Override
        protected void routeGlobalClkNets() {
            super.routeGlobalClkNets();
            DistinctClockNodeUsers<Node> users = new DistinctClockNodeUsers<>();
            long unresolved = 0;
            for (Net clock : clkNets) {
                for (PIP pip : clock.getPIPs()) {
                    Node start = pip.getStartNode(), end = pip.getEndNode();
                    if (start == null) unresolved++; else users.add(start, clock.getName());
                    if (end == null) unresolved++; else users.add(end, clock.getName());
                }
            }
            Map<String, Long> intents = new java.util.TreeMap<>();
            PriorityQueue<String> examples = new PriorityQueue<>(9, Collections.reverseOrder());
            for (Map.Entry<Node, Set<String>> entry : users.sharedOwners.entrySet()) {
                intents.merge(entry.getKey().getIntentCode().name(), 1L, Long::sum);
                String example = entry.getKey()+" clocks="+new java.util.TreeSet<>(entry.getValue());
                if (examples.size() < 8) examples.add(example);
                else if (example.compareTo(examples.peek()) < 0) { examples.poll(); examples.add(example); }
            }
            JSONObject summary = new JSONObject().put("clock_nets", clkNets.size())
                .put("route_nodes", users.firstOwner.size()).put("shared_nodes", users.sharedOwners.size())
                .put("maximum_clock_users", users.maximumUsers).put("unresolved_pip_endpoints", unresolved)
                .put("shared_node_intents", new JSONObject(intents));
            System.out.println("INFO: EmuFlow clock route ownership: "+summary);
            List<String> ordered = new ArrayList<>(examples); Collections.sort(ordered);
            for (String example : ordered) System.out.println("INFO: EmuFlow shared clock node: "+example);
            if (!users.sharedOwners.isEmpty() || unresolved != 0) {
                throw new IllegalStateException("global-clock route ownership is invalid: " + summary);
            }
        }

        // Keep ownership independent of routingGraph.unpreserve(pin): freeing a
        // current endpoint must never hide another clock's physical reservation.
        private final Map<Node, String> clockRouteOwners = new HashMap<>();

        @Override
        protected void routeGlobalClkNet(
            Net clk, Map<Integer, Set<ClockRegion>> usedRoutingTracks
        ) {
            for (SitePinInst pin : clk.getPins()) {
                Node node = pin.getConnectedNode();
                if (node != null && !clockRouteOwners.containsKey(node)) routingGraph.unpreserve(node);
            }
            Predicate<Node> available = node -> {
                String owner = clockRouteOwners.get(node);
                return (owner == null || owner.equals(clk.getName()))
                    && getGlobalRoutingNodeStatus(clk, node) != NodeStatus.UNAVAILABLE;
            };
            long start = System.nanoTime();
            SitePinInst source = clk.getSource();
            if (source == null) throw new IllegalStateException("clock has no source: " + clk.getName());
            Map<Node, Node> tree = availableClockTree(
                source.getConnectedNode(), Node::getAllDownhillNodes,
                EmuFlowRWRoute::isDedicatedClockNode, available
            );
            System.out.println("INFO: EmuFlow available clock tree: net=" + clk.getName()
                + " reachable_nodes=" + tree.size() + " sinks=" + (clk.getPins().size() - 1)
                + " seconds=" + ((System.nanoTime()-start)/1e9));
            Set<PIP> pips = new HashSet<>();
            for (SitePinInst pin : clk.getPins()) {
                if (pin.isOutPin()) continue;
                boolean cascade = isBUFGCEClockInputSink(pin);
                List<Node> path;
                try {
                    path = availableClockSinkPath(
                        pin.getConnectedNode(), tree, Node::getAllUphillNodes,
                        node -> cascade || node.getIntentCode() == IntentCode.NODE_GLOBAL_LEAF,
                        available, MAX_LCB_SEARCH_DEPTH
                    );
                } catch (IllegalStateException error) {
                    Map<String, Long> frontier = new java.util.TreeMap<>();
                    Map<String, String> examples = new java.util.TreeMap<>();
                    for (Node node : tree.keySet()) {
                        for (Node next : node.getAllDownhillNodes()) {
                            if (tree.containsKey(next)) continue;
                            String key = next.getIntentCode().name() + ":" + available.test(next);
                            frontier.merge(key, 1L, Long::sum);
                            examples.merge(key, next.toString(), (left, right) ->
                                left.compareTo(right) <= 0 ? left : right);
                        }
                    }
                    System.out.println("INFO: EmuFlow clock reachability failure: " + new JSONObject()
                        .put("clock", clk.getName()).put("source", source.toString())
                        .put("source_node", source.getConnectedNode().toString())
                        .put("sink", pin.toString()).put("frontier", new JSONObject(frontier))
                        .put("frontier_examples", new JSONObject(examples)));
                    throw error;
                }
                pips.addAll(extendedClockPathPIPs(path));
                // All non-cascade last miles are explicitly reconstructed and
                // independently checked, rather than delegated to an opaque helper.
                if (!cascade) EXTENDED_LCB_CLOCK_NETS.add(clk.getName());
            }
            // Commit only after all sinks are reachable. Reserve used paths,
            // not the whole explored dedicated graph, for subsequent clocks.
            for (PIP pip : pips) {
                for (Node node : new Node[] {pip.getStartNode(), pip.getEndNode()}) {
                    if (node == null || !available.test(node)) {
                        throw new IllegalStateException("clock path occupies unavailable node: "
                            + clk.getName() + " " + node);
                    }
                    clockRouteOwners.put(node, clk.getName());
                }
            }
            clk.setPIPs(pips);
            preserveNet(clk, false);
            for (SitePinInst pin : clk.getPins()) pin.setRouted(true);
        }

        @Override
        protected void routeIndirectConnections(Collection<Connection> connections) {
            long iterationStartNanos = System.nanoTime();
            int recoveryAtStart = 0;
            long unroutedAtStart = 0;
            // A connection only needs the exceptional full-device recovery
            // tree while it remains unrouted, congested, or selected for a
            // timing-driven reroute. Keeping every connection that was ever
            // unroutable makes this tree grow monotonically and repeatedly
            // partitions already-stable routes in later iterations.
            recoveryConnections.removeIf(
                connection -> !super.shouldRoute(connection)
            );
            if (!recoveryConnections.isEmpty()) {
                List<Connection> snapshot = new ArrayList<>(recoveryConnections);
                recoveryAtStart = snapshot.size();
                unroutedAtStart = snapshot.stream()
                    .filter(connection -> !connection.isRouted())
                    .count();
                System.out.println(
                    "INFO: EmuFlow active recovery connections: " + snapshot.size()
                );
                RecoveryPartitionTree tree = new RecoveryPartitionTree(
                    snapshot, design.getDevice().getColumns(), design.getDevice().getRows()
                );
                routeRecoveryTree(tree.root);
                // Recovery can legitimately take longer than the guard on a
                // full device. Drop connections that became stable before
                // judging progress; otherwise the set still contains the
                // successful work until the next iteration and the timeout
                // rejects a completed recovery tail.
                recoveryConnections.removeIf(
                    connection -> !super.shouldRoute(connection)
                );
            }
            super.routeIndirectConnections(connections);
            printCongestionSnapshot(connections);
            recoveryConnections.removeIf(
                connection -> !super.shouldRoute(connection)
            );
            double elapsedSeconds = (
                System.nanoTime() - iterationStartNanos
            ) / 1_000_000_000.0;
            int recoveryRemaining = recoveryConnections.size();
            long unroutedRemaining = recoveryConnections.stream()
                .filter(connection -> !connection.isRouted())
                .count();
            if (recoveryAtStart > 0
                && recoveryRemaining >= recoveryAtStart
                && unroutedRemaining >= unroutedAtStart
                && elapsedSeconds > MAX_RECOVERY_ITERATION_SECONDS) {
                throw new IllegalStateException(
                    "RWRoute recovery iteration " + routeIteration + " took "
                    + elapsedSeconds + " seconds without reducing its active "
                    + "or unrouted connection set (active " + recoveryAtStart
                    + " -> " + recoveryRemaining + ", unrouted "
                    + unroutedAtStart + " -> " + unroutedRemaining
                    + "); refusing an unbounded recovery tail. "
                    + "Re-run placement with high-fanout routability weighting."
                );
            }
        }

        private void printCongestionSnapshot(Collection<Connection> connections) {
            long congestedConnections = connections.stream()
                .filter(Connection::isCongested).count();
            long unroutedConnections = connections.stream()
                .filter(connection -> !connection.isRouted()).count();
            long overusedNodes = 0;
            long excessUsers = 0;
            int maximumOccupancy = 0;
            Map<String, Long> byIntent = new HashMap<>();
            Map<Integer, Long> bySLR = new HashMap<>();
            long[] byMaximumUserFanout = new long[5];
            // [overused nodes, routed connection uses, shared excess exposure,
            // local, short, pin access, SLR0, SLR1, SLR2, SLR3, physical fanout]
            Map<NetWrapper, long[]> byNet = new HashMap<>();
            for (com.xilinx.rapidwright.rwroute.RouteNode node
                : routingGraph.getRnodes()) {
                if (!node.isOverUsed()) continue;
                overusedNodes++;
                int occupancy = node.getOccupancy();
                excessUsers += Math.max(0, occupancy - 1);
                maximumOccupancy = Math.max(maximumOccupancy, occupancy);
                String intent = node.getIntentCode().name();
                int slr = node.getSLRIndex(routingGraph);
                byIntent.merge(intent, 1L, Long::sum);
                bySLR.merge(slr, 1L, Long::sum);
                int maximumUserFanout = 0;
                for (Map.Entry<NetWrapper, Integer> user
                    : node.getUsersConnectionCounts().entrySet()) {
                    long[] contribution = byNet.computeIfAbsent(
                        user.getKey(), wrapper -> {
                            long[] values = new long[11];
                            values[10] = wrapper.getConnections().size();
                            return values;
                        }
                    );
                    maximumUserFanout = Math.max(
                        maximumUserFanout, (int)contribution[10]
                    );
                    contribution[0]++;
                    contribution[1] += user.getValue();
                    contribution[2] += Math.max(0, occupancy - 1);
                    if (intent.equals("NODE_LOCAL")) contribution[3]++;
                    if (intent.equals("NODE_SINGLE")
                        || intent.equals("NODE_DOUBLE")) contribution[4]++;
                    if (intent.equals("NODE_PINBOUNCE")
                        || intent.equals("NODE_PINFEED")) contribution[5]++;
                    if (slr >= 0 && slr < 4) contribution[6 + slr]++;
                }
                // Count each node exactly once. The largest user's fanout
                // distinguishes nodes involving a high-fanout net from nodes
                // contested entirely by small nets. Top-net rankings alone
                // cannot establish which population dominates congestion.
                int bucket = maximumUserFanout <= 4 ? 0
                    : maximumUserFanout <= 16 ? 1
                    : maximumUserFanout <= 64 ? 2
                    : maximumUserFanout <= 256 ? 3 : 4;
                byMaximumUserFanout[bucket]++;
            }
            long bucketTotal = 0;
            for (long count : byMaximumUserFanout) bucketTotal += count;
            if (bucketTotal != overusedNodes) {
                throw new IllegalStateException("congestion fanout buckets lost node coverage");
            }
            List<Map.Entry<String, Long>> intents = new ArrayList<>(
                byIntent.entrySet()
            );
            intents.sort(
                Map.Entry.<String, Long>comparingByValue().reversed()
                    .thenComparing(Map.Entry.comparingByKey())
            );
            StringBuilder topIntents = new StringBuilder();
            for (int index = 0; index < Math.min(8, intents.size()); index++) {
                if (index > 0) topIntents.append(',');
                topIntents.append(intents.get(index).getKey()).append(':')
                    .append(intents.get(index).getValue());
            }
            List<Integer> slrs = new ArrayList<>(bySLR.keySet());
            Collections.sort(slrs);
            StringBuilder slrCounts = new StringBuilder();
            for (int index = 0; index < slrs.size(); index++) {
                if (index > 0) slrCounts.append(',');
                int slr = slrs.get(index);
                slrCounts.append(slr).append(':').append(bySLR.get(slr));
            }
            System.out.println(
                "INFO: EmuFlow congestion snapshot: iteration=" + routeIteration
                + " connections=" + connections.size()
                + " congested_connections=" + congestedConnections
                + " unrouted_connections=" + unroutedConnections
                + " overused_nodes=" + overusedNodes
                + " excess_users=" + excessUsers
                + " maximum_occupancy=" + maximumOccupancy
                + " slr_overused_nodes=" + slrCounts
                + " top_intents=" + topIntents
                + " overused_nodes_by_max_user_fanout=le4:" + byMaximumUserFanout[0]
                + ",5to16:" + byMaximumUserFanout[1]
                + ",17to64:" + byMaximumUserFanout[2]
                + ",65to256:" + byMaximumUserFanout[3]
                + ",gt256:" + byMaximumUserFanout[4]
            );
            PriorityQueue<Map.Entry<NetWrapper, long[]>> topNets =
                new PriorityQueue<>(17, (left, right) -> {
                    int order = Long.compare(
                        left.getValue()[0], right.getValue()[0]
                    );
                    if (order != 0) return order;
                    order = Long.compare(
                        left.getValue()[1], right.getValue()[1]
                    );
                    if (order != 0) return order;
                    return right.getKey().getNet().getName().compareTo(
                        left.getKey().getNet().getName()
                    );
                });
            for (Map.Entry<NetWrapper, long[]> entry : byNet.entrySet()) {
                topNets.add(entry);
                if (topNets.size() > 16) topNets.remove();
            }
            List<Map.Entry<NetWrapper, long[]>> rankedNets =
                new ArrayList<>(topNets);
            rankedNets.sort((left, right) -> {
                int order = Long.compare(
                    right.getValue()[0], left.getValue()[0]
                );
                if (order != 0) return order;
                order = Long.compare(
                    right.getValue()[1], left.getValue()[1]
                );
                if (order != 0) return order;
                return left.getKey().getNet().getName().compareTo(
                    right.getKey().getNet().getName()
                );
            });
            for (int rank = 0; rank < rankedNets.size(); rank++) {
                Map.Entry<NetWrapper, long[]> entry = rankedNets.get(rank);
                NetWrapper wrapper = entry.getKey();
                Net net = wrapper.getNet();
                long[] contribution = entry.getValue();
                SitePinInst source = net.getSource();
                System.out.println(
                    "INFO: EmuFlow congested net: iteration=" + routeIteration
                    + " rank=" + (rank + 1)
                    + " net=" + net.getName()
                    + " overused_nodes=" + contribution[0]
                    + " connection_uses=" + contribution[1]
                    + " shared_excess_exposure=" + contribution[2]
                    + " local_nodes=" + contribution[3]
                    + " short_nodes=" + contribution[4]
                    + " pin_access_nodes=" + contribution[5]
                    + " slr_nodes=0:" + contribution[6]
                    + ",1:" + contribution[7]
                    + ",2:" + contribution[8]
                    + ",3:" + contribution[9]
                    + " fanout=" + wrapper.getConnections().size()
                    + " double_hpwl=" + wrapper.getDoubleHpwl()
                    + " source=" + (source == null ? "none" : source.toString())
                );
            }
        }

        private void routeRecoveryTree(RecoveryPartitionTree.Node node) {
            if (node.isLeaf()) {
                for (Connection connection : node.connections) {
                    if (super.shouldRoute(connection)) routeIndirectConnection(connection);
                }
                return;
            }
            if (node.middle != null) routeRecoveryTree(node.middle);
            ParallelismTools.invokeAll(
                () -> routeRecoveryTree(node.left),
                () -> routeRecoveryTree(node.right)
            );
        }

        @Override
        protected boolean shouldRoute(Connection connection) {
            if (recoveryConnections.contains(connection)) return false;
            return super.shouldRoute(connection);
        }

        @Override
        protected boolean handleUnroutableConnection(Connection connection) {
            connection.enlargeBoundingBox(
                config.getExtensionXIncrement(), config.getExtensionYIncrement()
            );
            recoveryConnections.add(connection);
            if (routeIteration == 1 && swapOutputPin(connection)) return true;
            return false;
        }

        @Override
        protected boolean handleCongestedConnection(Connection connection) {
            return false;
        }

        public static Design routeDesignWithUserDefinedArguments(Design design, String[] args) {
            RWRouteConfig config = new RWRouteConfig(args);
            return routeDesign(new UnroutableOnlyBoundingBoxCUFR(design, config));
        }
    }

    private static final class MaterializedCell {
        final String logicalName;
        final String logicalType;
        final Cell regularCell;
        final SiteInst siteInst;
        final int physicalCells;
        final Map<String, String> parameters;
        final Map<String, String> pinRemap;
        final boolean dedicatedClockGate;
        final Cell clockPrefilterCell;
        final String clockPrefilterNet;

        MaterializedCell(
            String logicalName, String logicalType, Cell regularCell,
            SiteInst siteInst, int physicalCells, Map<String, String> parameters,
            Map<String, String> pinRemap, boolean dedicatedClockGate,
            Cell clockPrefilterCell, String clockPrefilterNet
        ) {
            this.logicalName = logicalName;
            this.logicalType = logicalType;
            this.regularCell = regularCell;
            this.siteInst = siteInst;
            this.physicalCells = physicalCells;
            this.parameters = parameters;
            this.pinRemap = pinRemap;
            this.dedicatedClockGate = dedicatedClockGate;
            this.clockPrefilterCell = clockPrefilterCell;
            this.clockPrefilterNet = clockPrefilterNet;
        }

        boolean isTransformedDSP48E2() {
            return logicalType.equals("DSP48E2");
        }

        boolean isBlockRam() {
            return logicalType.equals("RAMB18E2") || logicalType.equals("RAMB36E2");
        }

        String routedLogicalPin(String logicalPin) {
            return pinRemap.getOrDefault(logicalPin, logicalPin);
        }

        Cell routedCell(String logicalPin) {
            if (clockPrefilterCell != null && !logicalPin.equals("O")) {
                return clockPrefilterCell;
            }
            return regularCell;
        }

        boolean hasClockPrefilter() {
            return clockPrefilterCell != null;
        }
    }

    private static String sha256(Path path) throws Exception {
        MessageDigest hasher = MessageDigest.getInstance("SHA-256");
        try (InputStream stream = new BufferedInputStream(Files.newInputStream(path))) {
            byte[] buffer = new byte[1024 * 1024];
            for (int count = stream.read(buffer); count >= 0; count = stream.read(buffer)) {
                if (count > 0) hasher.update(buffer, 0, count);
            }
        }
        byte[] digest = hasher.digest();
        return hexDigest(digest);
    }

    private static String hexDigest(byte[] digest) {
        StringBuilder value = new StringBuilder();
        for (byte item : digest) value.append(String.format("%02x", item));
        return value.toString();
    }

    private static String nodeName(Node node) {
        return node == null ? null : node.toString();
    }

    private static boolean isBUFGCEClockInputSink(SitePinInst pin) {
        String siteName = pin.getSiteInstName();
        String physicalPinName = pin.getName();
        String sitePinName = pin.getSitePinName();
        return !pin.isOutPin()
            && siteName != null
            && siteName.startsWith("BUFGCE_")
            && (
                "CLK_IN".equals(physicalPinName)
                || "CLK_IN".equals(sitePinName)
            );
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

    private static JSONArray pipPayloadRecord(String netName, PIP pip) {
        JSONObject value = pipRecord(pip);
        return new JSONArray()
            .put(netName)
            .put(value.getString("tile"))
            .put(value.getString("start_wire"))
            .put(value.getString("end_wire"))
            .put(value.getString("start_node"))
            .put(value.getString("end_node"))
            .put(value.getBoolean("bidirectional"))
            .put(value.getBoolean("reversed"));
    }

    private static JSONArray sortedStaticRoots(Net net, boolean vcc) {
        Set<String> roots = new LinkedHashSet<>();
        for (PIP pip : net.getPIPs()) {
            Node start = pip.isReversed() ? pip.getEndNode() : pip.getStartNode();
            Node end = pip.isReversed() ? pip.getStartNode() : pip.getEndNode();
            for (Node node : new Node[] {start, end}) {
                if (node != null && (vcc ? node.isTiedToVcc() : node.isTiedToGnd())) {
                    roots.add(node.toString());
                }
            }
        }
        for (SitePinInst pin : net.getPins()) {
            Node node = pin.getConnectedNode();
            if (node != null && (vcc ? node.isTiedToVcc() : node.isTiedToGnd())) {
                roots.add(node.toString());
            }
        }
        List<String> sorted = new ArrayList<>(roots);
        sorted.sort(String::compareTo);
        return new JSONArray(sorted);
    }

    private static BELPin timingBELPin(SitePinInst sitePin) {
        List<BELPin> connected = new ArrayList<>();
        List<BELPin> mapped = new ArrayList<>();
        for (BELPin belPin : DesignTools.getConnectedBELPins(sitePin)) {
            Cell cell = sitePin.getSiteInst().getCell(belPin.getBELName());
            if (cell == null || belPin.isOutput() != sitePin.isOutPin()) continue;
            connected.add(belPin);
            if (cell.usesPhysicalPin(belPin.getName())) mapped.add(belPin);
        }
        List<BELPin> candidates = mapped.isEmpty() ? connected : mapped;
        candidates.sort(
            Comparator.comparing(BELPin::getBELName).thenComparing(BELPin::getName)
        );
        if (candidates.isEmpty()) {
            throw new IllegalStateException(
                "physical endpoint has no connected cell BEL pin: "
                + sitePin.getSiteInstName() + "/" + sitePin.getName()
            );
        }
        if (sitePin.isOutPin() && candidates.size() != 1) {
            throw new IllegalStateException(
                "physical source endpoint has ambiguous cell BEL pins: "
                + sitePin.getSiteInstName() + "/" + sitePin.getName()
                + " -> " + candidates
            );
        }
        // A single input site pin may legitimately fan out to multiple cells
        // inside its site.  The pinned RapidWright timing model only uses the
        // sink BEL pin to select its EDIF-free physical-endpoint path; route
        // delay remains defined by this SitePinInst and its routed PIP tree.
        return candidates.get(0);
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
            int open = logicalPin.indexOf('[');
            int close = logicalPin.indexOf(']');
            String port = open < 0 ? logicalPin : logicalPin.substring(0, open);
            Integer index = open < 0 || close < open
                ? null : Integer.valueOf(logicalPin.substring(open + 1, close));
            if (index != null && port.equals("WEA")) {
                if (index < 0 || index > 1) {
                    throw new IllegalStateException("RAMB18E2 WEA index is out of range");
                }
                // The device BEL exposes four write-enable pins although the
                // RAMB18E2 Unisim interface has WEA[1:0].  Yosys emits the
                // physical-width quartet in duplicate pairs.  Drive both BEL
                // pins represented by each logical write-enable bit.
                result.add("WEA" + index);
                result.add("WEA" + (index + 2));
                return result;
            }
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

    private static String blockRamLogicalPin(
        MaterializedCell materialized, String logicalPin
    ) {
        if (!materialized.isBlockRam()) return logicalPin;
        int open = logicalPin.indexOf('[');
        String port = open < 0 ? logicalPin : logicalPin.substring(0, open);
        String suffix = open < 0 ? "" : logicalPin.substring(open);
        // Yosys follows the Xilinx primitive declaration names, whereas the
        // RapidWright 2026.1 Unisim library exposes the corresponding native
        // RAMB18E2/RAMB36E2 port names. Translate only at this provider
        // boundary; source-netlist and timing identities stay unchanged.
        switch (port) {
            case "DOADO": port = "DOUTADOUT"; break;
            case "DOBDO": port = "DOUTBDOUT"; break;
            case "DOPADOP": port = "DOUTPADOUTP"; break;
            case "DOPBDOP": port = "DOUTPBDOUTP"; break;
            case "DIADI": port = "DINADIN"; break;
            case "DIBDI": port = "DINBDIN"; break;
            case "DIPADIP": port = "DINPADINP"; break;
            case "DIPBDIP": port = "DINPBDINP"; break;
            default: break;
        }
        if (materialized.logicalType.equals("RAMB18E2") && port.equals("WEA")) {
            int close = logicalPin.indexOf(']');
            if (open < 0 || close != logicalPin.length() - 1) {
                throw new IllegalStateException("RAMB18E2 WEA must be indexed");
            }
            int index = Integer.parseInt(logicalPin.substring(open + 1, close));
            if (index < 0 || index > 3) {
                throw new IllegalStateException("RAMB18E2 physical WEA index is out of range");
            }
            // The mapped physical contract has WEA[3:0], with bits 2/3
            // duplicating bits 0/1.  RapidWright's logical primitive exposes
            // only WEA[1:0].  A later ownership check fails closed if a mapped
            // design ever assigns an alias pair to different nets.
            suffix = "[" + (index % 2) + "]";
        }
        return port + suffix;
    }

    private static void connectBlockRamLogicalPin(
        Net net, MaterializedCell materialized, String logicalPin,
        Map<String, EDIFNet> logicalPinOwners
    ) {
        int open = logicalPin.indexOf('[');
        int close = logicalPin.indexOf(']');
        if ((open < 0) != (close < 0) || (open >= 0 && close != logicalPin.length() - 1)) {
            throw new IllegalStateException("malformed logical pin " + logicalPin);
        }
        String portName = open < 0 ? logicalPin : logicalPin.substring(0, open);
        Integer index = open < 0
            ? null : Integer.valueOf(logicalPin.substring(open + 1, close));
        EDIFPort port = materialized.regularCell.getEDIFCellInst().getPort(portName);
        if (port == null) {
            throw new IllegalStateException(
                materialized.logicalType + " has no logical port " + portName
            );
        }
        if (index == null && port.isBus()) {
            throw new IllegalStateException("bus logical pin must be indexed: " + logicalPin);
        }
        if (index != null) {
            if (!port.isBus()) {
                throw new IllegalStateException("scalar logical pin is indexed: " + logicalPin);
            }
            int low = Math.min(port.getLeft(), port.getRight());
            int high = Math.max(port.getLeft(), port.getRight());
            if (index < low || index > high) {
                throw new IllegalStateException(
                    "logical pin index is outside " + port.getName() + ": " + logicalPin
                );
            }
        }
        EDIFNet logicalNet = net.getLogicalNet();
        if (logicalNet == null) {
            throw new IllegalStateException("physical net has no logical EDIF net");
        }
        String ownerKey = materialized.regularCell.getName() + "\0" + logicalPin;
        EDIFNet previous = logicalPinOwners.putIfAbsent(ownerKey, logicalNet);
        if (previous != null) {
            if (previous != logicalNet) {
                throw new IllegalStateException(
                    "aliased logical pin belongs to different nets: "
                    + materialized.regularCell.getName() + "/" + logicalPin
                );
            }
            return;
        }
        if (index == null) {
            logicalNet.createPortInst(
                port, materialized.regularCell.getEDIFCellInst()
            );
        } else {
            logicalNet.createPortInst(
                port, index, materialized.regularCell.getEDIFCellInst()
            );
        }
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

    private static void ensureSimpleLogicalPinMapping(Cell cell, String logicalPin) {
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
                "physical pin mapping conflict on " + cell.getName() + "/"
                + physicalPin + ": " + existing + " vs " + logicalPin
            );
        }
        if (existing != null) cell.removePinMapping(physicalPin);
        cell.addPinMapping(physicalPin, logicalPin);
    }

    static Net resolveRoutedNet(
        Design design, String logicalName, Map<String, SitePinInst> ioAnchors
    ) {
        // RWRoute normalizes physical net names to their parent EDIF names.
        // An I/O net may therefore be merged into the net created for its
        // top-level port. SitePinInst ownership follows that merge; the input
        // logical identity remains the certificate key.
        SitePinInst anchor = ioAnchors.get(logicalName);
        Net net = anchor == null ? design.getNet(logicalName) : anchor.getNet();
        if (net == null || design.getNet(net.getName()) != net) {
            throw new IllegalStateException(
                "routed logical net has no live physical net: " + logicalName
            );
        }
        return net;
    }

    private static String dsp48e2SitePin(String logicalPin) {
        String physicalPin = logicalPin.replace("[", "").replace("]", "");
        if (physicalPin.startsWith("D") && logicalPin.startsWith("D[")) {
            physicalPin = "DIN" + physicalPin.substring(1);
        } else if (physicalPin.startsWith("ACOUT")) {
            physicalPin = physicalPin.replace("ACOUT", "ACOUT_B");
        } else if (physicalPin.startsWith("ACIN")) {
            physicalPin = physicalPin.replace("ACIN", "ACIN_B");
        } else if (physicalPin.startsWith("BCOUT")) {
            physicalPin = physicalPin.replace("BCOUT", "BCOUT_B");
        } else if (physicalPin.startsWith("BCIN")) {
            physicalPin = physicalPin.replace("BCIN", "BCIN_B");
        } else if (physicalPin.startsWith("PATTERNBDETECT")) {
            physicalPin = physicalPin.replace("PATTERNBDETECT", "PATTERN_B_DETECT");
        } else if (physicalPin.startsWith("PATTERNDETECT")) {
            physicalPin = physicalPin.replace("PATTERNDETECT", "PATTERN_DETECT");
        }
        return physicalPin;
    }

    private static Site allocateClockBufferSite(
        Design design, String originalSiteName, List<Site> available
    ) {
        Site original = design.getDevice().getSite(originalSiteName);
        if (original == null || available.isEmpty()) {
            throw new IllegalStateException("no BUFGCE site is available for " + originalSiteName);
        }
        Site selected = Collections.min(
            available,
            Comparator
                .comparingInt((Site site) ->
                    Math.abs(site.getTile().getColumn() - original.getTile().getColumn())
                    + Math.abs(site.getTile().getRow() - original.getTile().getRow()))
                .thenComparing(Site::getName)
        );
        available.remove(selected);
        return selected;
    }

    private static MaterializedCell materializeCell(
        Design design, String safeName, String[] row, Map<String, String> parameters,
        String[] clockGateRow, boolean clockGateNeedsPrefilter,
        List<Site> availableClockBufferSites
    ) {
        String logicalType = row[3];
        if (clockGateRow != null) {
            if (!logicalType.equals("LUT2")
                || clockGateRow.length != 6
                || !clockGateRow[2].equals("I0")
                || !clockGateRow[3].equals("I1")
                || !clockGateRow[4].equals("O")
                || !clockGateRow[5].equals("lut2-init-1000-fdre-latched-enable-v1")) {
                throw new IllegalStateException("invalid qualified clock-gate contract");
            }
            Site site = allocateClockBufferSite(
                design, row[4], availableClockBufferSites
            );
            Cell prefilter = null;
            Cell cell;
            Map<String, String> pinRemap = new HashMap<>();
            String prefilterNet = null;
            if (clockGateNeedsPrefilter) {
                prefilter = design.createAndPlaceCell(
                    safeName + "$clock_prefilter", Unisim.LUT2,
                    row[4] + "/" + row[5], "INIT", "4'h8"
                );
                cell = design.createAndPlaceCell(
                    safeName, Unisim.BUFGCE, site.getName() + "/BUFCE",
                    "CE_TYPE", "ASYNC"
                );
                prefilterNet = "__emuflow_clock_prefilter_" + safeName;
            } else {
                cell = design.createAndPlaceCell(
                    safeName, Unisim.BUFGCE, site.getName() + "/BUFCE",
                    "CE_TYPE", "ASYNC"
                );
                pinRemap.put("I0", "I");
                pinRemap.put("I1", "CE");
                pinRemap.put("O", "O");
            }
            if (cell == null) {
                throw new IllegalStateException("failed to place clock buffer " + safeName);
            }
            if (clockGateNeedsPrefilter && prefilter == null) {
                throw new IllegalStateException("failed to place clock prefilter " + safeName);
            }
            return new MaterializedCell(
                row[2], logicalType, cell, cell.getSiteInst(),
                clockGateNeedsPrefilter ? 2 : 1, parameters,
                pinRemap, true, prefilter, prefilterNet
            );
        }
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
                row[2], logicalType, cell, cell.getSiteInst(), 1, parameters,
                Collections.emptyMap(), false, null, null
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
            // The physical transform deliberately has no EDIF parent, but
            // RWRoute's preprocessing still classifies every Cell by type
            // before routing.  createAndPlaceCell(EDIFCell=null, ...) leaves
            // that lightweight classification unset, so preserve the exact
            // transformed primitive identity explicitly.
            child.setType(component);
            if (siteInst == null) siteInst = child.getSiteInst();
            if (child.getSiteInst() != siteInst) {
                throw new IllegalStateException("DSP48E2 components do not share one SiteInst");
            }
        }
        if (siteInst == null) throw new IllegalStateException("empty DSP48E2 transform");
        return new MaterializedCell(
            row[2], logicalType, null, siteInst, DSP48E2_COMPONENTS.length,
            parameters, Collections.emptyMap(), false, null, null
        );
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 7) {
            throw new IllegalArgumentException(
                "usage: EmuFlowRWRoute <input.tsv> <output.json> "
                + "<timing-revision> <intersite-sha256> <intrasite-sha256> "
                + "<parts-db-md5> <device-db-md5>"
            );
        }
        Path inputPath = Path.of(args[0]);
        String part = null;
        Map<String, String> metadata = new HashMap<>();
        Map<String, String[]> cellRows = new HashMap<>();
        Map<String, String[]> iobRows = new HashMap<>();
        Map<String, String[]> clockGateRows = new HashMap<>();
        Map<String, String> clockGateInputNets = new HashMap<>();
        Map<String, String> clockGateDriversByNet = new HashMap<>();
        Map<String, Map<String, String>> cellParameters = new HashMap<>();
        Map<String, String> netKinds = new HashMap<>();
        JSONArray excluded = new JSONArray();
        try (BufferedReader reader = Files.newBufferedReader(inputPath, StandardCharsets.UTF_8)) {
        for (String line = reader.readLine(); line != null; line = reader.readLine()) {
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
                case "PARAM":
                    if (fields.length != 4 || !cellRows.containsKey(fields[1])
                        || cellParameters.computeIfAbsent(
                            fields[1], ignored -> new HashMap<>()
                        ).put(fields[2], fields[3]) != null)
                        throw new IllegalArgumentException("invalid PARAM record");
                    break;
                case "CLOCK_GATE":
                    if (fields.length != 6
                        || clockGateRows.put(fields[1], fields) != null)
                        throw new IllegalArgumentException("invalid CLOCK_GATE record");
                    break;
                case "IOB":
                    if (fields.length != 13 || iobRows.put(fields[1], fields) != null)
                        throw new IllegalArgumentException("invalid IOB record");
                    break;
                case "NET":
                    if (fields.length != 3 || netKinds.put(fields[1], fields[2]) != null)
                        throw new IllegalArgumentException("invalid NET record");
                    break;
                case "PIN":
                    if (fields.length != 5)
                        throw new IllegalArgumentException("invalid PIN record");
                    if (clockGateRows.containsKey(fields[2])) {
                        if (fields[3].equals("I0") && fields[4].equals("sink")) {
                            if (clockGateInputNets.put(fields[2], fields[1]) != null)
                                throw new IllegalArgumentException(
                                    "clock gate has multiple I0 route records"
                                );
                        } else if (fields[3].equals("O")
                            && fields[4].equals("driver")) {
                            if (clockGateDriversByNet.put(fields[1], fields[2]) != null)
                                throw new IllegalArgumentException(
                                    "clock net has multiple qualified gate drivers"
                                );
                        }
                    }
                    break;
                case "EXCLUDED":
                    if (fields.length != 3 && fields.length != 4)
                        throw new IllegalArgumentException("invalid EXCLUDED record");
                    JSONObject excludedRecord = new JSONObject()
                        .put("net", fields[1]).put("reason", fields[2]);
                    if (fields.length == 4) {
                        excludedRecord.put("qualification", fields[3]);
                    }
                    excluded.put(excludedRecord);
                    break;
                default:
                    throw new IllegalArgumentException("unknown route-input record " + fields[0]);
            }
        }
        }
        if (part == null) throw new IllegalArgumentException("missing part identity");
        System.out.printf(
            "INFO: EmuFlow input cells=%d nets=%d excluded=%d iobs=%d%n",
            cellRows.size(), netKinds.size(), excluded.length(), iobRows.size()
        );
        reportHeap("input-indexed");
        if (!cellRows.keySet().containsAll(clockGateRows.keySet())) {
            throw new IllegalArgumentException("CLOCK_GATE references an unknown cell");
        }
        Set<String> clockGatesNeedingPrefilter = new LinkedHashSet<>();
        Set<String> internalizedClockEdges = new LinkedHashSet<>();
        Map<String, String> clockGateParents = new HashMap<>();
        for (Map.Entry<String, String> entry : clockGateInputNets.entrySet()) {
            String parent = clockGateDriversByNet.get(entry.getValue());
            if (parent != null) {
                if (parent.equals(entry.getKey())) {
                    throw new IllegalArgumentException("clock gate feeds its own I0 input");
                }
                clockGatesNeedingPrefilter.add(entry.getKey());
                clockGatesNeedingPrefilter.add(parent);
                clockGateParents.put(entry.getKey(), parent);
                internalizedClockEdges.add(entry.getValue());
            }
        }

        Design design = new Design("emuflow_rwroute", part);
        design.setDesignOutOfContext(true);
        Map<String, MaterializedCell> cells = new HashMap<>();
        List<String> cellNames = new ArrayList<>(cellRows.keySet());
        cellNames.sort(String::compareTo);
        int physicalCells = 0;
        int transformedDsp48e2Cells = 0;
        int dedicatedClockGateCells = 0;
        int fabricPrefilterClockGateCells = 0;
        List<Site> availableClockBufferSites = new ArrayList<>(List.of(
            design.getDevice().getAllSitesOfType(SiteTypeEnum.BUFGCE)
        ));
        for (String safeName : cellNames) {
            String[] row = cellRows.get(safeName);
            MaterializedCell cell;
            try {
                cell = materializeCell(
                    design, safeName, row,
                    cellParameters.getOrDefault(safeName, Collections.emptyMap()),
                    clockGateRows.get(safeName),
                    clockGatesNeedingPrefilter.contains(safeName),
                    availableClockBufferSites
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
            if (cell.dedicatedClockGate) dedicatedClockGateCells++;
            if (cell.hasClockPrefilter()) fabricPrefilterClockGateCells++;
        }
        cellRows.clear();
        cellParameters.clear();
        reportHeap("cells-materialized");
        List<String> iobNames = new ArrayList<>(iobRows.keySet());
        iobNames.sort(String::compareTo);
        for (String safeName : iobNames) {
            String[] row = iobRows.get(safeName);
            PinType pinType;
            String logicalType;
            if (row[3].equals("tx")) {
                pinType = PinType.OUT;
                logicalType = "OBUF";
            } else if (row[3].equals("rx")) {
                pinType = PinType.IN;
                logicalType = "IBUF";
            } else {
                throw new IllegalArgumentException("invalid IOB direction " + row[3]);
            }
            if (design.getDevice().getSite(row[8]) == null
                || design.getDevice().getSite(row[9]) == null) {
                throw new IllegalArgumentException(
                    "virtual I/O references an absent P/N site: " + row[8] + "/" + row[9]
                );
            }
            Cell cell = design.createAndPlaceIOB(row[4], pinType, row[6], row[12]);
            if (!cell.getSiteName().equals(row[8])) {
                throw new IllegalStateException(
                    "package pin " + row[6] + " resolved to " + cell.getSiteName()
                    + " instead of " + row[8]
                );
            }
            cells.put(
                safeName,
                new MaterializedCell(
                    row[2], logicalType, cell, cell.getSiteInst(), 1,
                    Collections.emptyMap(), Collections.emptyMap(), false,
                    null, null
                )
            );
            physicalCells++;
        }

        checkResourceBudget(physicalResourceUsage(design), metadata);

        Map<String, String> iobNetNames = new HashMap<>();
        Map<String, SitePinInst> iobNetAnchors = new HashMap<>();
        Map<String, EDIFNet> blockRamLogicalPinOwners = new HashMap<>();
        for (MaterializedCell cell : cells.values()) {
            if (cell.hasClockPrefilter()
                && netKinds.put(cell.clockPrefilterNet, "clock_internal") != null) {
                throw new IllegalStateException(
                    "clock prefilter net identity collides: " + cell.clockPrefilterNet
                );
            }
        }
        List<String> netNames = new ArrayList<>(netKinds.keySet());
        netNames.sort(String::compareTo);
        for (String netName : netNames) design.createNet(netName);
        for (MaterializedCell cell : cells.values()) {
            if (!cell.hasClockPrefilter()) continue;
            Net internal = design.getNet(cell.clockPrefilterNet);
            ensureSimpleLogicalPinMapping(cell.clockPrefilterCell, "O");
            ensureSimpleLogicalPinMapping(cell.regularCell, "I");
            ensureSimpleLogicalPinMapping(cell.regularCell, "CE");
            internal.connect(cell.clockPrefilterCell, "O");
            internal.connect(cell.regularCell, "I");
            design.getVccNet().connect(cell.regularCell, "CE");
        }
        try (BufferedReader reader = Files.newBufferedReader(inputPath, StandardCharsets.UTF_8)) {
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (!line.startsWith("PIN\t")) continue;
                String[] row = line.split("\\t", -1);
                if (row.length != 5 || !netKinds.containsKey(row[1])) {
                    throw new IllegalArgumentException("invalid PIN record");
                }
                String netName = row[1];
                Net net = design.getNet(netName);
                MaterializedCell cell = cells.get(row[2]);
                if (cell == null) throw new IllegalArgumentException("unknown cell " + row[2]);
                String parentClockGate = (
                    row[3].equals("I0") ? clockGateParents.get(row[2]) : null
                );
                if (parentClockGate != null) {
                    MaterializedCell parent = cells.get(parentClockGate);
                    if (parent == null || !parent.hasClockPrefilter()) {
                        throw new IllegalStateException(
                            "cascaded clock parent lacks a physical prefilter"
                        );
                    }
                    net = design.getNet(parent.clockPrefilterNet);
                }
                if (iobRows.containsKey(row[2])) iobNetNames.put(row[2], netName);
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
                        String routedLogicalPin = blockRamLogicalPin(cell, row[3]);
                        ensureLogicalPinMapping(cell, routedLogicalPin);
                        connectBlockRamLogicalPin(
                            net, cell, routedLogicalPin, blockRamLogicalPinOwners
                        );
                        Set<String> sitePins = new LinkedHashSet<>(
                            cell.regularCell.getAllCorrespondingSitePinNames(
                                routedLogicalPin
                            )
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
                        String routedLogicalPin = cell.routedLogicalPin(row[3]);
                        Cell routedCell = cell.routedCell(row[3]);
                        if (routedCell == cell.regularCell) {
                            ensureLogicalPinMapping(cell, routedLogicalPin);
                        } else {
                            ensureSimpleLogicalPinMapping(routedCell, routedLogicalPin);
                        }
                        // General logic can expose alternative dedicated and
                        // fabric exits (for example CARRY8 CO[7] via COUT or
                        // HMUX).  RapidWright selects the legal physical exit;
                        // only BRAM's true lower/upper sink expansion is
                        // explicitly materialized above.
                        connected.add(net.connect(routedCell, routedLogicalPin));
                    }
                    for (SitePinInst pin : connected) {
                        if (pin == null) continue;
                        if (iobRows.containsKey(row[2])) {
                            SitePinInst previous = iobNetAnchors.putIfAbsent(netName, pin);
                            if (previous != null && previous != pin) {
                                throw new IllegalStateException(
                                    "multiple I/O anchors for logical net " + netName
                                );
                            }
                        }
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
        }
        reportHeap("pins-connected");

        List<String> sourceOnlyInternalizedClockNets = new ArrayList<>();
        for (String netName : internalizedClockEdges) {
            Net net = design.getNet(netName);
            if (net != null && net.getSource() != null && net.getSinkPins().isEmpty()) {
                sourceOnlyInternalizedClockNets.add(netName);
                excluded.put(new JSONObject()
                    .put("net", netName)
                    .put("reason", "clock_gate_cascade_internalized")
                    .put("qualification", "lut2-prefilter-fabric-edge-v1"));
            }
        }
        netNames.removeAll(sourceOnlyInternalizedClockNets);

        // This certificate begins and ends at physical site pins; purely
        // intra-site nets remain outside the inter-site route certificate.
        // CUFR is RapidWright's parallel full-design specialization of
        // RWRoute.  HUS is its upstream strategy for large, difficult
        // negotiated-congestion problems. Only genuinely unroutable
        // connections expand their bounding box; ordinary congested routes do
        // not trigger blanket expansion or an unconditional tree rebuild.
        // U-turn resources remain required for legal placements at the
        // complete XCVU19P device boundary.
        reportHeap("before-rwroute");
        UnroutableOnlyBoundingBoxCUFR.routeDesignWithUserDefinedArguments(
            design,
            new String[] {
                "--hus", "--nonTimingDriven", "--useUTurnNodes"
            }
        );
        reportHeap("after-rwroute");

        // RapidWright's lightweight timing model evaluates the concrete
        // routed PIP tree in picoseconds.  Keep this deliberately separate
        // from global setup analysis: EmuFlow binds these per-FPGA route
        // segments to transport/TDM events and delegates global setup WNS/TNS
        // to OpenSTA.  RapidWright's model is not a hold/sign-off engine.
        TimingModel timingModel = new TimingModel(design.getDevice());
        timingModel.build();

        Path outputPath = Path.of(args[1]);
        Path netsPath = outputPath.resolveSibling(
            outputPath.getFileName().toString() + ".nets.jsonl"
        );
        Path pipsPath = outputPath.resolveSibling(
            outputPath.getFileName().toString() + ".pips.jsonl"
        );
        MessageDigest netsDigest = MessageDigest.getInstance("SHA-256");
        MessageDigest pipsDigest = MessageDigest.getInstance("SHA-256");
        int routed = 0;
        int pips = 0;
        int certificateNets = 0;
        int timedEndpoints = 0;
        float maximumRouteDelayPs = 0.0f;
        int staticNets = 0;
        int staticSinks = 0;
        int internalClockNets = 0;
        int extendedLCBClockNets = 0;
        int boundaryClockNets = 0;
        Map<String, Float> iobRouteDelays = new HashMap<>();
        for (Object item : excluded) {
            JSONObject excludedRecord = (JSONObject)item;
            if (excludedRecord.getString("reason").equals("boundary_clock")) {
                boundaryClockNets++;
            }
        }
        try (
            BufferedWriter netsWriter = new BufferedWriter(new OutputStreamWriter(
                new DigestOutputStream(Files.newOutputStream(netsPath), netsDigest),
                StandardCharsets.UTF_8
            ));
            BufferedWriter pipsWriter = new BufferedWriter(new OutputStreamWriter(
                new DigestOutputStream(Files.newOutputStream(pipsPath), pipsDigest),
                StandardCharsets.UTF_8
            ))
        ) {
            for (String netName : netNames) {
                Net net = resolveRoutedNet(design, netName, iobNetAnchors);
                JSONObject record = new JSONObject();
                record.put("net", netName);
                if (!netName.equals(net.getName())) {
                    record.put("physical_net", net.getName());
                }
                String kind = netKinds.get(netName);
                if (!kind.equals("signal") && !kind.equals("clock")
                    && !kind.equals("clock_internal")) {
                    throw new IllegalStateException("unsupported routed net kind: " + kind);
                }
                record.put("kind", kind);
                SitePinInst routedSource = net.getSource();
                if (kind.equals("clock") && (
                    routedSource == null
                    || routedSource.getSiteInst().getSiteTypeEnum() != SiteTypeEnum.BUFGCE
                )) {
                    throw new IllegalStateException(
                        "clock net is not sourced by a dedicated BUFGCE: " + netName
                    );
                }
                String qualification = "ordinary-fabric-signal";
                if (kind.equals("clock")) {
                    if (EXTENDED_LCB_CLOCK_NETS.contains(netName)) {
                        qualification =
                            "dedicated-bufgce-clock-extended-lcb-last-mile";
                        extendedLCBClockNets++;
                    } else qualification = (
                            net.getPins().stream().anyMatch(
                                EmuFlowRWRoute::isBUFGCEClockInputSink
                            )
                                ? "dedicated-bufgce-clock-with-fabric-cascade-spur"
                                : "dedicated-bufgce-clock"
                    );
                } else if (kind.equals("clock_internal")) {
                    qualification = "lut2-prefilter-to-bufgce-v1";
                    internalClockNets++;
                }
                record.put("qualification", qualification);
                JSONArray pins = new JSONArray();
                JSONArray alternateSources = new JSONArray();
                SitePinInst source = net.getSource();
                BELPin sourceBELPin = source == null ? null : timingBELPin(source);
                for (SitePinInst pin : net.getPins()) {
                    JSONObject pinValue = pinRecord(pin);
                    if (pin.isOutPin() && pin != source) {
                        // RapidWright may route one logical driver through more
                        // than one equivalent physical site exit (for example a
                        // LUT O pin and its HMUX exit).  Preserve those roots for
                        // connectivity checking without misreporting multiple
                        // logical drivers.
                        alternateSources.put(pinValue);
                        continue;
                    }
                    if (!pin.isOutPin()) {
                        if (source == null) {
                            throw new IllegalStateException(
                                "routed net has no timing source: " + netName
                            );
                        }
                        BELPin sinkBELPin = timingBELPin(pin);
                        float delayPs = timingModel.calcDelay(
                            source, pin, sourceBELPin, sinkBELPin, net
                        );
                        if (!Float.isFinite(delayPs) || delayPs < 0.0f) {
                            throw new IllegalStateException(
                                "invalid RapidWright route delay for " + netName
                                + "/" + pin.getSiteInstName() + "/" + pin.getName()
                                + ": " + delayPs
                            );
                        }
                        pinValue.put("route_delay_ps", delayPs);
                        for (String safeName : iobNames) {
                            if (!netName.equals(iobNetNames.get(safeName))) continue;
                            String[] row = iobRows.get(safeName);
                            if (row[3].equals("rx") || pin.getSiteInstName().equals(row[8])) {
                                iobRouteDelays.merge(safeName, delayPs, Math::max);
                            }
                        }
                        timedEndpoints++;
                        maximumRouteDelayPs = Math.max(maximumRouteDelayPs, delayPs);
                    }
                    pins.put(pinValue);
                }
                record.put("pins", pins);
                if (alternateSources.length() > 0) {
                    record.put("alternate_sources", alternateSources);
                }
                List<PIP> sortedPips = new ArrayList<>(net.getPIPs());
                sortedPips.sort(Comparator.comparing(PIP::toString));
                record.put("pip_count", sortedPips.size());
                for (PIP pip : sortedPips) {
                    pipsWriter.write(pipPayloadRecord(netName, pip).toString());
                    pipsWriter.newLine();
                }
                record.put("has_gap", net.hasGapRouting());
                record.put("source_present", net.getSource() != null);
                record.put("sink_count", net.getSinkPins().size());
                netsWriter.write(record.toString());
                netsWriter.newLine();
                if (net.hasPIPs()) routed++;
                pips += sortedPips.size();
                certificateNets++;
            }
            for (Net net : new Net[] {design.getGndNet(), design.getVccNet()}) {
                if (net == null || net.getSinkPins().isEmpty()) continue;
                boolean vcc = net.isVCCNet();
                if (!vcc && !net.isGNDNet()) {
                    throw new IllegalStateException("unexpected non-static global net " + net.getName());
                }
                String netName = net.getName();
                JSONObject record = new JSONObject();
                record.put("net", netName);
                record.put("kind", vcc ? "static_vcc" : "static_gnd");
                record.put("qualification", "device-tied-static");
                JSONArray pins = new JSONArray();
                for (SitePinInst pin : net.getPins()) pins.put(pinRecord(pin));
                record.put("pins", pins);
                List<PIP> sortedPips = new ArrayList<>(net.getPIPs());
                sortedPips.sort(Comparator.comparing(PIP::toString));
                record.put("pip_count", sortedPips.size());
                for (PIP pip : sortedPips) {
                    pipsWriter.write(pipPayloadRecord(netName, pip).toString());
                    pipsWriter.newLine();
                }
                JSONArray roots = sortedStaticRoots(net, vcc);
                if (roots.length() == 0) {
                    throw new IllegalStateException(
                        "static net has sinks but no device-tied route root: " + netName
                    );
                }
                record.put("roots", roots);
                record.put("has_gap", net.hasGapRouting());
                record.put("source_present", net.getSource() != null);
                record.put("sink_count", net.getSinkPins().size());
                netsWriter.write(record.toString());
                netsWriter.newLine();
                if (net.hasPIPs()) routed++;
                pips += sortedPips.size();
                staticNets++;
                staticSinks += net.getSinkPins().size();
                certificateNets++;
            }
        }
        String netsSha256 = hexDigest(netsDigest.digest());
        String pipsSha256 = hexDigest(pipsDigest.digest());
        JSONObject output = new JSONObject();
        output.put("schema", SCHEMA);
        output.put("status", "candidate");
        output.put("provider", "rapidwright-rwroute-2026.1.0");
        output.put("part", part);
        output.put("source", new JSONObject()
            .put("mapped_sha256", metadata.get("mapped_sha256"))
            .put("packed_sha256", metadata.get("packed_sha256"))
            .put("placement_sha256", metadata.get("placement_sha256"))
            .putOpt(
                "virtual_io_contract_sha256",
                metadata.get("virtual_io_contract_sha256")
            )
            .put("rwroute_input_sha256", sha256(Path.of(args[0]))));
        JSONObject physicalResources = physicalResourceUsage(design);
        checkResourceBudget(physicalResources, metadata);
        output.put("physical_resources", physicalResources);
        output.put("cells", cells.size());
        JSONArray virtualIO = new JSONArray();
        for (String safeName : iobNames) {
            String[] row = iobRows.get(safeName);
            MaterializedCell cell = cells.get(safeName);
            Float delay = iobRouteDelays.get(safeName);
            if (delay == null) {
                throw new IllegalStateException("virtual I/O has no routed delay: " + row[2]);
            }
            virtualIO.put(new JSONObject()
                .put("id", row[2])
                .put("direction", row[3])
                .put("p_port", row[4])
                .put("n_port", row[5])
                .put("p_package_pin", row[6])
                .put("n_package_pin", row[7])
                .put("p_site", row[8])
                .put("n_site", row[9])
                .put("logical_port", row[10])
                .put("logical_bit", Integer.parseInt(row[11]))
                .put("iostandard", row[12])
                .put("mapped_net", iobNetNames.get(safeName))
                .put("physical_cell", cell.regularCell.getName())
                .put("physical_site", cell.regularCell.getSiteName())
                .put("physical_bel", cell.regularCell.getBELName())
                .put("route_delay_ps", delay)
                .put(
                    "differential_lowering",
                    "rapidwright-lvds-p-iob-with-verified-dedicated-n-mate-v1"
                ));
        }
        output.put("virtual_io", virtualIO);
        JSONArray clockGates = new JSONArray();
        List<String> clockGateNames = new ArrayList<>(clockGateRows.keySet());
        clockGateNames.sort(String::compareTo);
        for (String safeName : clockGateNames) {
            MaterializedCell cell = cells.get(safeName);
            if (cell == null || !cell.dedicatedClockGate) {
                throw new IllegalStateException(
                    "qualified clock gate was not materialized: " + safeName
                );
            }
            clockGates.put(new JSONObject()
                .put("safe_cell", safeName)
                .put("logical_cell", cell.logicalName)
                .put("logical_type", cell.logicalType)
                .put("physical_type", "BUFGCE")
                .put("physical_site", cell.regularCell.getSiteName())
                .put("physical_bel", cell.regularCell.getBELName())
                .put(
                    "input_site",
                    cell.hasClockPrefilter()
                        ? cell.clockPrefilterCell.getSiteName()
                        : cell.regularCell.getSiteName()
                )
                .put(
                    "ce_type",
                    cell.hasClockPrefilter() ? "ASYNC_TIED_HIGH" : "ASYNC"
                )
                .put(
                    "implementation",
                    cell.hasClockPrefilter()
                        ? "lut2-prefilter-bufgce-tied-high"
                        : "direct-bufgce"
                )
                .put("cascaded_input", clockGateParents.containsKey(safeName))
                .put("qualification", "lut2-init-1000-fdre-latched-enable-v1"));
        }
        output.put("clock_gates", clockGates);
        output.put("materialization", new JSONObject()
            .put("route_cells", cells.size())
            .put("physical_cells", physicalCells)
            .put("transformed_dsp48e2_cells", transformedDsp48e2Cells)
            .put("dedicated_clock_gate_cells", dedicatedClockGateCells)
            .put("fabric_prefilter_clock_gate_cells", fabricPrefilterClockGateCells)
            .put(
                "router",
                ROUTER_STRATEGY
            ));
        output.put("payloads", new JSONObject()
            .put("nets", new JSONObject()
                .put("format", "jsonl-object/v1")
                .put("path", netsPath.getFileName().toString())
                .put("sha256", netsSha256)
                .put("records", certificateNets))
            .put("pips", new JSONObject()
                .put("format", "jsonl-array/v1")
                .put("path", pipsPath.getFileName().toString())
                .put("sha256", pipsSha256)
                .put("records", pips)));
        output.put("excluded_nets", excluded);
        output.put("timing", new JSONObject()
            .put("provider", "rapidwright-lightweight")
            .put("family", "UltraScalePlus")
            .put("units", "ps")
            .put("setup_route_delays", "available")
            .put("hold_analysis", "unavailable")
            .put("hard_block_clock_timing", "unqualified")
            .put("source_revision", args[2])
            .put("source_data_sha256", new JSONObject()
                .put("intersite_delay_terms.txt", args[3])
                .put("intrasite_delay_terms.txt", args[4]))
            .put("device_data_md5", new JSONObject()
                .put("data/parts.db", args[5])
                .put(
                    "data/devices/virtexuplus/xcvu19p_db.dat",
                    args[6]
                ))
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
            .put("certificate_nets", certificateNets)
            .put("static_nets", staticNets)
            .put("static_sinks", staticSinks)
            .put("internal_clock_nets", internalClockNets)
            .put("extended_lcb_clock_nets", extendedLCBClockNets)
            .put("nets_with_pips", routed)
            .put("pips", pips)
            .put("excluded_nets", excluded.length())
            .put("boundary_clock_nets", boundaryClockNets));
        Files.writeString(outputPath, output.toString(), StandardCharsets.UTF_8);
    }
}
