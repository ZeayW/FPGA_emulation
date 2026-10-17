/* Explicit failure-boundary qualification, not physical-closure evidence. */
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.Unisim;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;

public final class EmuFlowRWRouteFailureBoundaryCheck {
    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("expected provider part");
        Design design = new Design("failure_boundary", args[0]);
        Net net = design.createNet("data");
        net.connect(design.createAndPlaceCell("source", Unisim.FDRE, "SLICE_X0Y0/AFF"), "Q");
        net.connect(design.createAndPlaceCell("sink", Unisim.FDRE, "SLICE_X1Y0/AFF"), "D");
        Class<?> router = Class.forName("EmuFlowRWRoute$AdaptiveBoundingBoxCUFR");
        Method route = router.getDeclaredMethod(
            "routeDesignWithUserDefinedArguments", Design.class, String[].class
        );
        route.setAccessible(true);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        PrintStream original = System.out;
        boolean rejected = false;
        try (PrintStream capture = new PrintStream(output, true, StandardCharsets.UTF_8)) {
            System.setOut(capture);
            try {
                // Pinned RWRoute loops while iteration < maxIterations.
                // One deliberately exhausts the budget before routing the net.
                route.invoke(null, design, new String[] {
                    "--nonTimingDriven", "--useUTurnNodes", "--maxIterations", "1"
                });
            } catch (InvocationTargetException failure) {
                Throwable cause = failure.getCause();
                if (!(cause instanceof IllegalStateException)
                    || !cause.getMessage().contains("RWRoute incomplete before routed timing")) {
                    throw failure;
                }
                rejected = true;
            }
        } finally {
            System.setOut(original);
        }
        String text = output.toString(StandardCharsets.UTF_8);
        if (!rejected || !text.contains("Routing terminated after 0 iterations.")) {
            throw new AssertionError("fixture did not exercise routing-budget exhaustion");
        }
        if (text.contains("Statistics") || text.contains("direct connections")) {
            throw new AssertionError("invalid route reached upstream postprocessing");
        }
        System.out.println("{\"status\":\"pass\",\"scope\":\"forced-budget-exhaustion-only\","
            + "\"postprocessing_entered\":false,\"whole_flow\":false}");
    }
}
