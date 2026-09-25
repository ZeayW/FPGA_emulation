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
import com.xilinx.rapidwright.device.BELClass;
import com.xilinx.rapidwright.device.BELPin;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SitePin;
import com.xilinx.rapidwright.device.Tile;

import java.util.HashSet;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.LinkedHashSet;

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

    private static void auditSite(Site selected, Map<String, List<String>> nodeOwners) {
        String prefix = selected.getName().replaceFirst("_X.*$", "_");
        System.out.println("SITE\t" + prefix + "\t" + selected.getName());
        for (Site tileSite : selected.getTile().getSites()) {
            System.out.println("TILESITE\t" + selected.getTile().getName()
                    + "\t" + tileSite.getSiteIndexInTile()
                    + "\t" + tileSite.getName()
                    + "\t" + tileSite.getSiteTypeEnum());
        }
        Set<String> auditedDownhill = new HashSet<>();
        Set<String> auditedUphill = new HashSet<>();
        for (BEL bel : selected.getBELs()) {
            for (BELPin pin : bel.getPins()) {
                if (!cascadeName(pin.getName())) continue;
                String sitePin = pin.getConnectedSitePinName();
                Node external = pin.getExternalNode(selected);
                SitePin nativeSitePin = pin.getSitePin(selected);
                System.out.println("PIN\t" + selected.getName()
                        + "\t" + bel.getName()
                        + "\t" + pin.getName()
                        + "\t" + (pin.isInput() ? "I" : "O")
                        + "\t" + (sitePin == null ? "-" : sitePin)
                        + "\t" + nodeKey(external)
                        + "\t" + (nativeSitePin == null ? "-"
                            : Boolean.toString(
                                nativeSitePin.getBELPin().isDedicatedSitePin())));
                for (com.xilinx.rapidwright.device.SitePIP sitePip : pin.getSitePIPs()) {
                    System.out.println("SITEPIP\t" + selected.getName()
                            + "\t" + bel.getName() + "/" + pin.getName()
                            + "\t" + sitePip.getName(selected)
                            + "\t" + sitePip.getInputPinName()
                            + "\t" + sitePip.getOutputPinName());
                }
                for (BELPin connection : pin.getSiteConns()) {
                    System.out.println("SITECONN\t" + selected.getName()
                            + "\t" + bel.getName() + "/" + pin.getName()
                            + "\t" + connection.getBELName() + "/"
                            + connection.getName());
                }
                if (pin.isOutput() && external != null
                        && auditedDownhill.add(nodeKey(external))) {
                    for (PIP pip : external.getAllDownhillPIPs()) {
                        Node end = pip.getEndNode();
                        SitePin sink = end == null ? null : end.getSitePin();
                        System.out.println("DOWNPIP\t" + selected.getName()
                                + "\t" + nodeKey(external)
                                + "\t" + pip.getPIPType()
                                + "\t" + pip.isPIPFixed()
                                + "\t" + nodeKey(end)
                                + "\t" + (sink == null ? "-"
                                    : sink.getSite().getName() + "/"
                                        + sink.getPinName())
                                + "\t" + String.join(",",
                                    nodeOwners.getOrDefault(
                                        nodeKey(end), new ArrayList<>())));
                    }
                }
                if (pin.isInput() && external != null
                        && auditedUphill.add(nodeKey(external))) {
                    for (PIP pip : external.getAllUphillPIPs()) {
                        Node start = pip.getStartNode();
                        SitePin source = start == null ? null : start.getSitePin();
                        System.out.println("UPPIP\t" + selected.getName()
                                + "\t" + nodeKey(external)
                                + "\t" + pip.getPIPType()
                                + "\t" + pip.isPIPFixed()
                                + "\t" + nodeKey(start)
                                + "\t" + (source == null ? "-"
                                    : source.getSite().getName() + "/"
                                        + source.getPinName())
                                + "\t" + String.join(",",
                                    nodeOwners.getOrDefault(
                                        nodeKey(start), new ArrayList<>())));
                    }
                }
            }
        }
    }

    public static void main(String[] args) {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: <full-part> [site ...]");
        }
        Device device = Device.getDevice(args[0]);
        Map<String, List<String>> nodeOwners = new HashMap<>();
        for (Site site : device.getAllSites()) {
            if (site == null) continue;
            for (BEL bel : site.getBELs()) {
                if (bel.getBELClass() == BELClass.PORT) continue;
                for (BELPin pin : bel.getPins()) {
                    if (!cascadeName(pin.getName())) continue;
                    Node node = pin.getExternalNode(site);
                    if (node == null || node.isInvalidNode()) continue;
                    nodeOwners.computeIfAbsent(nodeKey(node), ignored -> new ArrayList<>())
                            .add(site.getName() + "/" + bel.getName() + "/"
                                    + pin.getName());
                }
            }
        }
        LinkedHashSet<Site> selected = new LinkedHashSet<>();
        if (args.length > 1) {
            for (int index = 1; index < args.length; ++index) {
                Site site = device.getSite(args[index]);
                if (site == null) {
                    throw new IllegalArgumentException("unknown site " + args[index]);
                }
                selected.add(site);
            }
        } else {
            String[] prefixes = {"DSP48E2_", "RAMB36_", "URAM288_"};
            for (String prefix : prefixes) {
                for (Site site : device.getAllSites()) {
                    if (site != null && site.getName().startsWith(prefix)) {
                        selected.add(site);
                        break;
                    }
                }
            }
        }
        for (Site site : selected) auditSite(site, nodeOwners);
    }
}
