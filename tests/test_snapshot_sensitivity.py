import unittest,subprocess,tempfile,shutil
from pathlib import Path
from test_snapshot_netlist import fixture
from emuflow.snapshot_sensitivity import export_snapshot_sensitivity_miter
from emuflow.snapshot_timing_population import boundary_id
from emuflow.errors import ValidationError


class SensitivityTests(unittest.TestCase):
    def emit(self,ir=None,launch=None):
        return export_snapshot_sensitivity_miter(ir or fixture(),{'a':'board0','b':'board1','inv':'board1'},
            board='board1',port_owners={'result':'board0'},
            launch=launch or boundary_id('state','b'),capture=boundary_id('cut','comb'))

    def test_independent_and_sensitive_cofactors(self):
        compiler=shutil.which('iverilog');runner=shutil.which('vvp')
        if not compiler or not runner:self.skipTest('Icarus required')
        for truth,expected in [('01',1),('00',0),('11',0),('10',1)]:
            ir=fixture();ir.value['instances'][-1]['parameters']['INIT']=truth
            rtl,info=self.emit(ir)
            self.assertEqual(info['cone_nets'],2)
            with tempfile.TemporaryDirectory() as d:
                p=Path(d);(p/'test.sv').write_text(rtl+f'''module tb;
wire different;sensitivity dut(1'b0,different);
initial begin #1; if(different !== 1'b{expected}) $fatal; $finish; end
endmodule
''')
                subprocess.run([compiler,'-g2012','-s','tb','-o',str(p/'sim'),str(p/'test.sv')],check=True,capture_output=True)
                subprocess.run([runner,str(p/'sim')],check=True,capture_output=True)

    def test_unknown_boundary_fails(self):
        with self.assertRaisesRegex(ValidationError,'unknown'):self.emit(launch='invented')
