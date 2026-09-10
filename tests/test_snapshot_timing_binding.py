import unittest
from emuflow.snapshot_timing_binding import bind_snapshot_timing_boundaries, qualify_snapshot_boundary_connections
from emuflow.errors import ValidationError


class TimingBindingTests(unittest.TestCase):
    def model(self):
        population={'board':'board0','launches':{'s':{'kind':'state','identity':'ff','bit':0},
            'rx':{'kind':'cut','identity':'in','bit':0},'input':{'kind':'host','identity':'din','bit':0}},
            'captures':{'s':{'kind':'state','identity':'ff','bit':0},'tx':{'kind':'cut','identity':'out','bit':0},
            'output':{'kind':'host','identity':'dout','bit':0}},'launch_labels':('s','rx','input'),
            'capture_masks':{'s':1,'tx':2,'output':4}}
        interface={'board':'board0','source_binding':{'registers':{'ff':'state0'}},
            'imported_nets':['in'],'exported_nets':['out'],
            'host_inputs':[{'port':'din','bit':0,'index':0}],
            'host_outputs':[{'port':'dout','bit':0,'index':0}]}
        nets={};cells={};roots={};captures={}
        names=['dut.state0','exchange.remote_snapshot[0]','held_inputs[0]','exchange.snapshot[0]','host_outputs[0]']
        for i,name in enumerate(names):
            nets['core.'+name]={'bits':[2*i]}
            cells[str(i)]={'type':'TRELLIS_FF','parameters':{'SD':'1'},'connections':{'Q':[2*i],'DI':[2*i+1]}}
            roots[str(i),'Q']={};captures[str(i),'DI']={}
        routed={'modules':{'top':{'netnames':nets,'cells':cells}}}
        mapped={'modules':{'device':{'netnames':{}}}}
        graph={'roots':roots,'captures':captures,'edges':[],'order':list(roots)+list(captures)}
        return population,interface,routed,graph,mapped

    def test_all_storage_roles_resolve_to_declared_physical_pins(self):
        p,i,r,g,m=self.model()
        b=bind_snapshot_timing_boundaries(p,i,r,g,mapped=m,mapped_top='device')
        self.assertEqual(b['launches'],{'s':('0','Q'),'rx':('1','Q'),'input':('2','Q')})
        self.assertEqual(b['captures'],{'s':('0','DI'),'tx':('3','DI'),'output':('4','DI')})
        g['edges']=[(b['launches'][a],b['captures'][z],None) for a,z in [('s','s'),('rx','tx'),('input','output')]]
        self.assertEqual(qualify_snapshot_boundary_connections(p,b,g)['required_pairs'],3)
        g['edges'].pop()
        with self.assertRaisesRegex(ValidationError,'missing required'):
            qualify_snapshot_boundary_connections(p,b,g)

    def test_wrong_board_or_host_index_is_not_guessed(self):
        for mutation in ('board','host','endpoint'):
            p,i,r,g,m=self.model()
            if mutation=='board':i['board']='board1'
            elif mutation=='host':i['host_inputs']=[]
            else:g['captures'].clear()
            with self.assertRaises(ValidationError):bind_snapshot_timing_boundaries(p,i,r,g,mapped=m,mapped_top='device')

    def test_constant_folded_required_dependency_cannot_silently_pass(self):
        p,i,r,g,m=self.model();r['modules']['top']['netnames']['core.held_inputs[0]']['bits']=['0']
        b=bind_snapshot_timing_boundaries(p,i,r,g,mapped=m,mapped_top='device')
        self.assertEqual(b['constant_launches'],{'input':0})
        with self.assertRaisesRegex(ValidationError,'semantic qualification'):
            qualify_snapshot_boundary_connections(p,b,g)

    def test_extracted_enable_and_hold_are_not_fake_data_arcs(self):
        p,i,r,g,m=self.model()
        g['captures']['0','CE']={}
        g['order'].append(('0','CE'))
        b=bind_snapshot_timing_boundaries(p,i,r,g,mapped=m,mapped_top='device')
        p['capture_masks']={'s':3,'tx':0,'output':0}
        g['edges']=[(b['launches']['rx'],('0','CE'),None)]
        result=qualify_snapshot_boundary_connections(p,b,g)
        self.assertEqual(result['classification'],{'data':0,'synchronous_control':1,'state_hold':1,'unexplained':0})
        self.assertFalse(result['global_timing_qualified'])
        self.assertEqual(len(g['edges']),1)
        g['edges']=[]
        with self.assertRaisesRegex(ValidationError,'missing required'):
            qualify_snapshot_boundary_connections(p,b,g)
