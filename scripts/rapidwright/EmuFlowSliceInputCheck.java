/* Offline qualification of the pinned device's shared slice input pins.
 * Does not route, alter a DUT, or qualify a complete physical flow.
 */
import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Unisim;
import java.util.List;

public final class EmuFlowSliceInputCheck {
    public static void main(String[] args) {
        if (args.length != 2) throw new IllegalArgumentException("part site");
        String[] muxes = {"F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH",
                          "F8MUX_BOT", "F8MUX_TOP", "F9MUX"};
        for (String bel : muxes) {
            Unisim type = bel.startsWith("F7") ? Unisim.MUXF7
                : bel.startsWith("F8") ? Unisim.MUXF8 : Unisim.MUXF9;
            inspect(args, type, bel, "S");
        }
        for (char letter = 'A'; letter <= 'H'; letter++) {
            inspect(args, Unisim.FDPE, letter + "FF", "D");
            inspect(args, Unisim.FDPE, letter + "FF2", "D");
        }
    }

    private static void inspect(String[] args, Unisim type, String bel, String pin) {
        Design design = new Design("slice_pin_probe", args[0]);
        Cell cell = design.createAndPlaceCell("probe", type, args[1] + "/" + bel);
        List<String> pins = cell.getAllCorrespondingSitePinNames(pin);
        if (pins.isEmpty()) throw new IllegalStateException(bel + "/" + pin);
        System.out.println("SLICE_INPUT\t" + bel + "\t" + pin + "\t" + pins);
    }
}
