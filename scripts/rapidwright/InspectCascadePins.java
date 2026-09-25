/*
 * Copyright (c) EmuFlow contributors.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Diagnostic helper for auditing dedicated hard-block cascade endpoints in a
 * pinned RapidWright device database.  This does not infer adjacency: it only
 * prints primitive BEL pins, their connected SitePins, and canonical external
 * Nodes so a provider adapter can be implemented and reviewed fail-closed.
 */

import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.BELPin;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.Tile;

public final class InspectCascadePins {
    private InspectCascadePins() {}

    private static boolean cascadeName(String name) {
        String upper = name.toUpperCase();
        return upper.contains("CAS")
                || upper.startsWith("ACIN") || upper.startsWith("ACOUT")
                || upper.startsWith("BCIN") || upper.startsWith("BCOUT")
                || upper.startsWith("PCIN") || upper.startsWith("PCOUT")
                || upper.startsWith("MULTSIGN")
                || upper.startsWith("CARRY");
    }

    private static String nodeKey(Node node) {
        if (node == null || node.isInvalidNode()) return "-";
        Tile tile = node.getTile();
        int wire = node.getWireIndex();
        if (tile == null || wire < 0 || wire >= tile.getWireCount()) return "?";
        return tile.getName() + "/" + wire + "/" + tile.getWireName(wire);
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: <full-part>");
        }
        Device device = Device.getDevice(args[0]);
        String[] prefixes = {"DSP48E2_", "RAMB36_", "URAM288_"};
        for (String prefix : prefixes) {
            Site selected = null;
            for (Site site : device.getAllSites()) {
                if (site != null && site.getName().startsWith(prefix)) {
                    selected = site;
                    break;
                }
            }
            System.out.println("SITE\t" + prefix + "\t"
                    + (selected == null ? "-" : selected.getName()));
            if (selected == null) continue;
            for (BEL bel : selected.getBELs()) {
                for (BELPin pin : bel.getPins()) {
                    if (!cascadeName(pin.getName())) continue;
                    String sitePin = pin.getConnectedSitePinName();
                    Node external = pin.getExternalNode(selected);
                    System.out.println("PIN\t" + selected.getName()
                            + "\t" + bel.getName()
                            + "\t" + pin.getName()
                            + "\t" + (pin.isInput() ? "I" : "O")
                            + "\t" + (sitePin == null ? "-" : sitePin)
                            + "\t" + nodeKey(external));
                }
            }
        }
    }
}
