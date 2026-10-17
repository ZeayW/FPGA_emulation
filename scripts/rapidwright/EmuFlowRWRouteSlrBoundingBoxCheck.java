/* Actual pinned-device geometry qualification; no full-flow/QoR claim. */
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.Unisim;
import com.xilinx.rapidwright.rwroute.Connection;
import com.xilinx.rapidwright.rwroute.CUFR;
import com.xilinx.rapidwright.rwroute.RWRoute;
import com.xilinx.rapidwright.rwroute.RWRouteConfig;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;

public final class EmuFlowRWRouteSlrBoundingBoxCheck {
    private static int checks;
    private static void require(boolean value) {
        checks++;
        if (!value) throw new AssertionError("SLR check " + checks);
    }

    private static final class Probe extends CUFR {
        Probe(Design d) { super(d, config()); }
        void init() { preprocess(); initialize(); }
        Connection connection() { return indirectConnections.get(0); }
        int length() { return routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES; }
    }

    private static RWRouteConfig config() {
        return new RWRouteConfig(new String[] {"--nonTimingDriven", "--useUTurnNodes"});
    }

    private static Design design(String part, String source, String sink) {
        Design d = new Design("slr_bounds", part);
        d.setDesignOutOfContext(true);
        Net net = d.createNet("data");
        net.connect(d.createAndPlaceCell("source", Unisim.FDRE, source + "/AFF"), "Q");
        net.connect(d.createAndPlaceCell("sink", Unisim.FDRE, sink + "/AFF"), "D");
        return d;
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("expected provider part");
        for (boolean north : new boolean[] {true, false}) {
            String a = north ? "SLICE_X402Y570" : "SLICE_X403Y600";
            String b = north ? "SLICE_X403Y600" : "SLICE_X402Y570";
            Probe p = new Probe(design(args[0], a, b)); p.init();
            Connection c = p.connection(); int length = p.length();
            require(c.isCrossSLR()); require(c.isCrossSLRnorth() == north);
            // Exercise the exact deficient shape seen in real design logs,
            // using actual provider connections and SLL length.
            c.setYMinBB((short) 555); c.setYMaxBB((short) (555 + length + 1));
            short xLow = c.getXMinBB(), xHigh = c.getXMaxBB();
            require(EmuFlowRWRoute.ensureStrictSlrBoundingBox(c, length));
            require(c.getYMaxBB() - c.getYMinBB() == length + 2);
            require(north ? c.getYMinBB() == 555 : c.getYMaxBB() == 555 + length + 1);
            require(c.getXMinBB() == xLow && c.getXMaxBB() == xHigh);
            require(!EmuFlowRWRoute.ensureStrictSlrBoundingBox(c, length));
            c.setYMinBB((short) 500); c.setYMaxBB((short) 700);
            require(!EmuFlowRWRoute.ensureStrictSlrBoundingBox(c, length));
            require(c.getYMinBB() == 500 && c.getYMaxBB() == 700);

            // Verify the actual provider's strict-edge predicate, not just
            // arithmetic duplicated from the adapter.
            int x = c.getSourceRnode().getEndTileXCoordinate();
            int y = c.getSourceRnode().getEndTileYCoordinate();
            c.setXMinBB((short) (x - 1)); c.setXMaxBB((short) (x + 1));
            c.setYMinBB((short) y); c.setYMaxBB((short) (y + length + 1));
            require(!c.getSourceRnode().isInConnectionBoundingBox(c));
            c.setYMinBB((short) (y - length - 1)); c.setYMaxBB((short) y);
            require(!c.getSourceRnode().isInConnectionBoundingBox(c));
            c.setYMinBB((short) (y - 1)); c.setYMaxBB((short) (y + 1));
            require(c.getSourceRnode().isInConnectionBoundingBox(c));

            // The production initializer must apply the same correction,
            // before its main partition tree is built; no routing is run.
            Class<?> type = Class.forName("EmuFlowRWRoute$AdaptiveBoundingBoxCUFR");
            Constructor<?> ctor = type.getDeclaredConstructor(Design.class, RWRouteConfig.class);
            ctor.setAccessible(true);
            Object router = ctor.newInstance(design(args[0], a, b), config());
            for (String method : new String[] {"preprocess", "initialize"}) {
                Method init = RWRoute.class.getDeclaredMethod(method);
                init.setAccessible(true); init.invoke(router);
            }
            Field field = RWRoute.class.getDeclaredField("indirectConnections");
            field.setAccessible(true);
            Connection actual = ((List<Connection>) field.get(router)).get(0);
            require(actual.getYMaxBB() - actual.getYMinBB() >= length + 2);
        }
        Probe local = new Probe(design(args[0], "SLICE_X0Y0", "SLICE_X1Y0"));
        local.init(); Connection c = local.connection();
        short low = c.getYMinBB(), high = c.getYMaxBB();
        require(!c.isCrossSLR());
        require(!EmuFlowRWRoute.ensureStrictSlrBoundingBox(c, local.length()));
        require(c.getYMinBB() == low && c.getYMaxBB() == high);
        System.out.println("{\"status\":\"pass\",\"checks\":" + checks
            + ",\"scope\":\"pinned-device-SLR-bounding-geometry-only\",\"routing_run\":false}");
    }
}
