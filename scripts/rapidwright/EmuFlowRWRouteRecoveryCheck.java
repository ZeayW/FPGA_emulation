/* Standalone policy qualification; no FPGA workload or QoR claim. */
import java.util.concurrent.ForkJoinPool;
import java.util.stream.IntStream;
import java.util.HashMap;
import java.util.Map;

public final class EmuFlowRWRouteRecoveryCheck {
    private static int checks;

    private static void require(boolean condition) {
        checks++;
        if (!condition) throw new AssertionError("policy check " + checks);
    }

    public static void main(String[] args) throws Exception {
        EmuFlowRWRoute.PersistentCongestion<String> policy =
            new EmuFlowRWRoute.PersistentCongestion<>(8);
        for (int iteration = 1; iteration <= 7; iteration++) {
            require(!policy.shouldExpand("a", iteration));
        }
        require(policy.shouldExpand("a", 8));
        require(!policy.shouldExpand("a", 8));
        for (int iteration = 9; iteration <= 15; iteration++) {
            require(!policy.shouldExpand("a", iteration));
        }
        require(policy.shouldExpand("a", 16));
        policy.pruneBefore(17);
        require(policy.size() == 0);
        for (int iteration = 17; iteration <= 23; iteration++) {
            require(!policy.shouldExpand("a", iteration));
        }
        require(policy.shouldExpand("a", 24));
        require(!policy.shouldExpand("b", 24));
        require(!policy.shouldExpand("b", 25));
        policy.pruneBefore(25);
        require(policy.size() == 1);
        for (int iteration = 25; iteration <= 30; iteration++) {
            require(!policy.shouldExpand("b", iteration));
        }
        require(policy.shouldExpand("b", 31));

        EmuFlowRWRoute.PersistentCongestion<String> concurrent =
            new EmuFlowRWRoute.PersistentCongestion<>(8);
        for (int iteration = 1; iteration <= 7; iteration++) {
            require(!concurrent.shouldExpand("one", iteration));
        }
        ForkJoinPool pool = new ForkJoinPool(8);
        try {
            long expansions = pool.submit(() -> IntStream.range(0, 128).parallel()
                .filter(index -> concurrent.shouldExpand("one", 8)).count()).get();
            require(expansions == 1);
        } finally {
            pool.shutdown();
        }
        EmuFlowRWRoute.PersistentCongestion<Integer> many =
            new EmuFlowRWRoute.PersistentCongestion<>(8);
        for (int index = 0; index < 20000; index++) {
            require(!many.shouldExpand(index, 1));
        }
        for (int index = 0; index < 10; index++) require(!many.shouldExpand(index, 2));
        many.pruneBefore(2);
        require(many.size() == 10);
        EmuFlowRWRoute.PersistentCongestion<String> immediate =
            new EmuFlowRWRoute.PersistentCongestion<>(1);
        require(immediate.shouldExpand("x", 1));
        require(!immediate.shouldExpand("x", 1));
        require(immediate.shouldExpand("x", 2));
        EmuFlowRWRoute.PersistentCongestion<String> gaps =
            new EmuFlowRWRoute.PersistentCongestion<>(3);
        require(!gaps.shouldExpand("gap", 1));
        require(!gaps.shouldExpand("gap", 2));
        require(!gaps.shouldExpand("gap", 4));
        require(!gaps.shouldExpand("gap", 5));
        require(gaps.shouldExpand("gap", 6));
        EmuFlowRWRoute.requireNoPendingIndirectConnections(0, 0);
        for (long[] pending : new long[][] {{1, 0}, {0, 1}, {3, 5}}) {
            boolean failed = false;
            try {
                EmuFlowRWRoute.requireNoPendingIndirectConnections(pending[0], pending[1]);
            } catch (IllegalStateException expected) {
                failed = expected.getMessage().contains("before routed timing");
            }
            require(failed);
        }
        Map<String, String> owners = new HashMap<>();
        EmuFlowRWRoute.claimPhysicalPip(owners, "pip-a", "net-a");
        EmuFlowRWRoute.claimPhysicalPip(owners, "pip-a", new String("net-a"));
        require(owners.size() == 1);
        EmuFlowRWRoute.claimPhysicalPip(owners, "pip-b", "net-b");
        require(owners.size() == 2);
        boolean conflict = false;
        try {
            EmuFlowRWRoute.claimPhysicalPip(owners, "pip-a", "net-b");
        } catch (IllegalStateException expected) {
            conflict = expected.getMessage().contains("before routed timing");
        }
        require(conflict);
        require(owners.get("pip-a").equals("net-a"));
        System.out.println("{\"status\":\"pass\",\"checks\":" + checks
            + ",\"scope\":\"policy-only-not-physical-routing\"}");
    }
}
