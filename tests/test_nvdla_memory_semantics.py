"""Differential simulation against the pinned NVDLA SRAM implementations.

Set NVDLA_RAM_AUDIT_ROOT to a directory containing original synth/, model/
and vlibs/ trees, and IVERILOG/VVP to existing executables if not on PATH.
No reference models are invented here: the original wrappers, macro models,
and library cells are compiled with the design's SYNTHESIS define.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.benchmarks.nvdla_ram_stubs import generate, PHYSICAL_MEMORY_POLICY


AUDIT_ROOT = os.environ.get("NVDLA_RAM_AUDIT_ROOT")
IVERILOG = os.environ.get("IVERILOG") or shutil.which("iverilog")
VVP = os.environ.get("VVP") or shutil.which("vvp")
IVERILOG_LIBDIR = os.environ.get("IVERILOG_LIBDIR")


def _bench(name, kind, depth, width):
    address_width = max(1, (depth - 1).bit_length())
    ports = ["clk", "ra", "re", "wa", "we", "di", "pwrbus_ram_pd"]
    if kind in {"rwsp", "rwsthp"}:
        ports.append("ore")
    if kind == "rwsthp":
        ports.extend(["byp_sel", "dbyp"])
    connections = ", ".join(f".{port}({port})" for port in ports)
    # Non-write-through macros intentionally leave simultaneous same-address
    # access unspecified. Test it only for wrappers with explicit forwarding.
    power_collision = "ra=1; step; step;" if kind in {"rwst", "rwsthp"} else ""
    avoid_collision = "if (re && we && ra == wa) wa = (wa + 1) % DEPTH;" if kind in {"rws", "rwsp"} else ""
    return f'''`timescale 1ns/10ps
module tb;
localparam W={width}, DEPTH={depth};
reg clk=1;
reg [{address_width - 1}:0] ra=0,wa=0;
reg re=0,we=0,ore=0,byp_sel=0;
reg [W-1:0] di=0,dbyp=0;
reg [31:0] pwrbus_ram_pd=0;
wire [W-1:0] reference, candidate;
{name} original ({connections}, .dout(reference));
replacement_{name} replacement ({connections}, .dout(candidate));
integer i,j,k,seed=1024,cycles=0,known=0;
reg checking=0;
task step;
begin
  #2; clk=0; #5; clk=1; #4;
  if(checking) begin
    for(k=0;k<W;k=k+1) begin
      if(reference[k] === 1'b0 || reference[k] === 1'b1) begin
        known=known+1;
        if(candidate[k] !== reference[k]) begin
          $display("MISMATCH {name} cycle=%0d bit=%0d ref=%b got=%b re=%b we=%b ore=%b ra=%d wa=%d power=%h",cycles,k,reference[k],candidate[k],re,we,ore,ra,wa,pwrbus_ram_pd);
          $fatal(1);
        end
      end
    end
  end
  cycles=cycles+1;
  #1;
end
endtask
initial begin
  // Initialize through writes, never by reaching into either implementation.
  for(i=0;i<DEPTH;i=i+1) begin
    we=1; wa=i; di={{W{{1'b0}}}}; step;
  end
  we=0; re=1; ra=0; ore=1; step; step; checking=1;
  // The retained-read-address case distinguishes the 20x289 extra flop bit
  // from a generic read-data register, including under output-enable stalls.
  re=0; ore=0; we=1; wa=0; di={{W{{1'b1}}}}; step;
  we=0; ore=1; step; step;
  for(i=0;i<600;i=i+1) begin
    ra=($random(seed) & 32'h7fffffff)%DEPTH;
    wa=($random(seed) & 32'h7fffffff)%DEPTH;
    re=$random(seed); we=$random(seed); ore=$random(seed);
    byp_sel=$random(seed);
    for(j=0;j<W;j=j+1) begin di[j]=$random(seed); dbyp[j]=$random(seed); end
    {avoid_collision}
    step;
  end
  // Retention and sleep controls remain live functional inputs. Exercise
  // attempted reads/writes while blocked and check retained contents later.
  byp_sel=0; ore=1;
  for(i=0;i<2;i=i+1) begin
    // Establish addr1=0 before power transition; flush the reference macro's
    // half-cycle write phase before asserting power controls.
    pwrbus_ram_pd=0; re=0; we=1; wa=1; di=0; step;
    we=0; re=1; ra=0; step; step;
    re=0; step; step;
    pwrbus_ram_pd=(i==0) ? 32'h100 : 32'h80; step; step;
    re=1; we=1; wa=1; ra=0; di={{W{{1'b1}}}}; step; step;
    // Explicit write-through remains outside the macro power clamp.
    {power_collision}
    we=0; re=0; step; step;
    pwrbus_ram_pd=0; re=1; ra=1; step; step;
  end
  if(known < W*500) $fatal(1, "insufficient known reference output coverage");
  $display("PASS {name} cycles=%0d compared_bits=%0d",cycles,known);
  $finish;
end
endmodule
'''


@unittest.skipUnless(AUDIT_ROOT and IVERILOG and VVP,
                     "requires pinned original NVDLA sources and existing Icarus Verilog")
class NvdlaMemorySemanticsTest(unittest.TestCase):
    def compile_and_simulate(self, root, out, name, models):
        match = re.fullmatch(r"nv_ram_(rws|rwsp|rwst|rwsthp)_(\d+)x(\d+)", name)
        self.assertIsNotNone(match)
        bench = out / (name + "_tb.v")
        bench.write_text(_bench(name, match[1], int(match[2]), int(match[3])))
        executable = out / (name + ".vvp")
        command = [str(IVERILOG), "-g2012", "-DSYNTHESIS", "-s", "tb", "-o", str(executable)]
        if IVERILOG_LIBDIR:
            command.extend(["-B", IVERILOG_LIBDIR])
        for directory in ("synth", "model", "vlibs"):
            command.extend(["-y", str(root / directory), "-I", str(root / directory)])
        command.extend([str(bench), str(models)])
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=120)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        return subprocess.run([str(VVP), str(executable)], capture_output=True, text=True, timeout=120)

    def test_detects_original_extra_bit_data_register_bug(self):
        root = Path(AUDIT_ROOT).resolve()
        with tempfile.TemporaryDirectory(prefix="nvdla-memory-negative-") as temporary:
            out = Path(temporary)
            models = out / "replacement.v"
            generate(root / "synth", models, PHYSICAL_MEMORY_POLICY)
            text = models.read_text()
            needle = "output_data <= {extra_bit[extra_read_address], ram_output};"
            self.assertEqual(text.count(needle), 1)
            text = text.replace(needle, "output_data <= {legacy_extra_data, ram_output};")
            text = text.replace("  reg [4:0] extra_read_address;", """  reg [4:0] extra_read_address;
  reg legacy_extra_data;
  always @(posedge clk) if (re) legacy_extra_data <= extra_bit[ra];""")
            models.write_text(re.sub(r"\bmodule (nv_ram_)", r"module replacement_\1", text))
            result = self.compile_and_simulate(root, out, "nv_ram_rwsp_20x289", models)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bit=288", result.stdout)
            print("EXPECTED_NEGATIVE_CONTROL " + result.stdout.splitlines()[0])

    def test_all_original_wrappers_against_physical_models(self):
        root = Path(AUDIT_ROOT).resolve()
        for directory in ("synth", "model", "vlibs"):
            self.assertTrue((root / directory).is_dir(), directory)
        with tempfile.TemporaryDirectory(prefix="nvdla-memory-semantics-") as temporary:
            out = Path(temporary)
            models = out / "replacement.v"
            count, modeled = generate(root / "synth", models, PHYSICAL_MEMORY_POLICY)
            self.assertEqual(count, 28, "expected complete pinned NVDLA SRAM wrapper inventory")
            self.assertEqual(count, modeled)
            models.write_text(re.sub(r"\bmodule (nv_ram_)", r"module replacement_\1", models.read_text()))
            for original in sorted((root / "synth").glob("nv_ram_*.v")):
                if original.stem.endswith("_logic"):
                    continue
                name = original.stem
                with self.subTest(wrapper=name):
                    result = self.compile_and_simulate(root, out, name, models)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("PASS " + name, result.stdout)
                    print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
