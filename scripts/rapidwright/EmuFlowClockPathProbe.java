/* Device-backed qualification of the extended clock-path PIP direction. */
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Tile;

public final class EmuFlowClockPathProbe {
    public static void main(String[] args) {
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
