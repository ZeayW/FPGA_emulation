/* SPDX-License-Identifier: Apache-2.0 */
import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.SitePinInst;
import com.xilinx.rapidwright.design.Unisim;
import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.BELPin;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SitePIP;

/** Offline probe of fabric versus dedicated carry initialization.
 * Only the selected slice is traversed. No route certificate is produced,
 * and no device-wide endpoint ownership map or design checkpoint is written.
 */
public final class InspectCarryInitialization {
    private InspectCarryInitialization() {}

    public static void main(String[] args) {
        if (args.length < 3 || args.length > 4) {
            throw new IllegalArgumentException(
                "usage: <part> <carry-site> <driver-site> [CARRY_TYPE]"
            );
        }
        Design design = new Design("carry_initialization_probe", args[0]);
        design.setDesignOutOfContext(true);
        Site site = design.getDevice().getSite(args[1]);
        if (site == null) throw new IllegalArgumentException("unknown site " + args[1]);
        String carryType = args.length == 4 ? args[3] : "SINGLE_CY8";
        Cell carry = design.createAndPlaceCell(
            "carry", Unisim.CARRY8, args[1] + "/CARRY8", "CARRY_TYPE", carryType
        );
        Cell source = design.createAndPlaceCell(
            "source", Unisim.FDRE, args[2] + "/AFF"
        );
        System.out.println("MAPPING\t" + carry.getPinMappingsL2P());
        for (String pin : new String[]{"CI", "CI_TOP"}) {
            System.out.println("ALTERNATIVES\t" + pin + "\t"
                + carry.getAllCorrespondingSitePinNames(pin));
        }
        Net net = design.createNet("fabric_carry_init");
        net.connect(source, "Q");
        SitePinInst chosen = net.connect(carry, "CI");
        System.out.println("SELECTED\t" + (chosen == null ? "intra-site"
            : chosen.getSiteInstName() + "/" + chosen.getName()));
        for (BEL bel : site.getBELs()) {
            String name = bel.getName();
            if (!(name.contains("CARRY") || name.contains("CYINIT")
                    || name.equals("AX") || name.equals("EX")
                    || name.equals("CIN") || name.equals("COUT"))) continue;
            for (BELPin pin : bel.getPins()) {
                System.out.println("PIN\t" + name + "/" + pin.getName()
                    + "\t" + (pin.isInput() ? "I" : "O")
                    + "\t" + pin.getConnectedSitePinName());
                for (BELPin other : pin.getSiteConns()) {
                    System.out.println("CONNECTION\t" + name + "/" + pin.getName()
                        + "\t" + other.getBELName() + "/" + other.getName());
                }
                for (SitePIP pip : pin.getSitePIPs()) {
                    System.out.println("SITEPIP\t" + name + "/" + pin.getName()
                        + "\t" + pip.getName(site) + "\t" + pip.getInputPinName()
                        + "\t" + pip.getOutputPinName());
                }
            }
        }
        // A second isolated design tests the documented dynamic CYINIT input.
        // Do not mutate the baseline or pretend that a site-pin-only probe is
        // a routed implementation or a timing qualification.
        Design alternate = new Design("fabric_cyinit_probe", args[0]);
        alternate.setDesignOutOfContext(true);
        Cell fabricCarry = alternate.createAndPlaceCell(
            "carry", Unisim.CARRY8, args[1] + "/CARRY8", "CARRY_TYPE", carryType
        );
        Cell fabricSource = alternate.createAndPlaceCell(
            "source", Unisim.FDRE, args[2] + "/AFF"
        );
        fabricCarry.removePinMapping("CIN");
        fabricCarry.removePinMapping("AX");
        fabricCarry.addPinMapping("AX", "CI");
        Net fabricNet = alternate.createNet("fabric_cyinit");
        fabricNet.connect(fabricSource, "Q");
        SitePinInst fabricPin = fabricNet.connect(fabricCarry, "CI");
        System.out.println("FABRIC_MAPPING\t" + fabricCarry.getPinMappingsL2P());
        System.out.println("FABRIC_SELECTED\t" + (fabricPin == null ? "intra-site"
            : fabricPin.getSiteInstName() + "/" + fabricPin.getName()));
        if (fabricPin == null || !fabricPin.getName().equals("AX")) {
            throw new IllegalStateException("dynamic carry initialization did not bind AX");
        }
    }
}
