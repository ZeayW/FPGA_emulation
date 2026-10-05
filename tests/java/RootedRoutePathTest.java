import java.util.*;

/** Run against the production path extractor; no device database is needed. */
public final class RootedRoutePathTest {
    static void check(boolean value) {
        if (!value) throw new AssertionError();
    }
    static void rejects(Runnable action) {
        try { action.run(); } catch (IllegalStateException expected) { return; }
        throw new AssertionError("Invalid path accepted");
    }
    public static void main(String[] args) {
        for (int seed = 0; seed < 20; seed++) {
            List<String[]> edges = new ArrayList<>(Arrays.asList(
                new String[]{"source", "branch"}, new String[]{"branch", "sink"},
                new String[]{"branch", "other"}, new String[]{"alternate", "altSink"}));
            Collections.shuffle(edges, new Random(seed));
            EmuFlowRWRoute.RoutedPath<String, String> graph =
                new EmuFlowRWRoute.RoutedPath<>(Set.of("source", "alternate"));
            for (String[] edge : edges) {
                // A reverse-encoded physical PIP must have the same effective direction.
                boolean reverse = seed % 2 == 0;
                graph.add(edge[reverse ? 1 : 0], edge[reverse ? 0 : 1],
                    edge[0] + "->" + edge[1], reverse);
            }
            check(graph.nodes("sink").equals(List.of("sink", "branch", "source")));
            check(graph.nodes("altSink").equals(List.of("altSink", "alternate")));
            check(graph.nodes("source").equals(List.of("source")));
            check(graph.edge("sink").equals("branch->sink"));
            // Visiting a sibling or another root must not leak prior trace state.
            check(graph.nodes("other").equals(List.of("other", "branch", "source")));
            check(graph.nodes("sink").equals(List.of("sink", "branch", "source")));
            rejects(() -> graph.nodes("disconnected"));
            rejects(() -> graph.add("other", "sink", "ambiguous", false));
            rejects(() -> graph.add("branch", "sink", "duplicate", false));
            rejects(() -> graph.add(null, "invalid", "null", false));
        }
        EmuFlowRWRoute.RoutedPath<String, String> cyclic =
            new EmuFlowRWRoute.RoutedPath<>(Set.of("root"));
        cyclic.add("a", "b", "a->b", false); cyclic.add("b", "a", "b->a", false);
        rejects(() -> cyclic.nodes("a"));
        EmuFlowRWRoute.RoutedPath<String, String> broken =
            new EmuFlowRWRoute.RoutedPath<>(Set.of("root"));
        broken.add("missing", "sink", "truncated", false);
        rejects(() -> broken.nodes("sink"));
        EmuFlowRWRoute.RoutedPath<String, String> otherNet =
            new EmuFlowRWRoute.RoutedPath<>(Set.of("second-root"));
        otherNet.add("second-root", "sink", "second-edge", false);
        check(otherNet.nodes("sink").equals(List.of("sink", "second-root")));
        rejects(() -> new EmuFlowRWRoute.RoutedPath<String, String>(Set.of()));
        System.out.println("Rooted route path checks passed (20 PIP orders, alternate root, invalid paths)");
    }
}
