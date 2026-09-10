import copy
import unittest
from emuflow.ecp5_sdf import read_nextpnr_sdf
from emuflow.errors import ValidationError


SDF = r'''(DELAYFILE (PROGRAM "nextpnr") (DIVIDER /) (TIMESCALE 1ps)
(CELL (CELLTYPE "top") (INSTANCE)
 (DELAY (ABSOLUTE (INTERCONNECT ff\[0\]/Q lut/A (10:20:30) (20:30:40)))))
(CELL (CELLTYPE "TRELLIS_FF") (INSTANCE ff\[0\])
 (DELAY (ABSOLUTE (IOPATH CLK Q (100:110:120) (110:120:130))))
 (TIMINGCHECK (SETUPHOLD (posedge M) (posedge CLK) (10:20:30) (-5:0:5))
              (SETUPHOLD (negedge M) (posedge CLK) (10:20:30) (-5:0:5))))
(CELL (CELLTYPE "TRELLIS_COMB") (INSTANCE lut)
 (DELAY (ABSOLUTE (IOPATH A F (20:30:40) (30:40:50)))))
)'''


class Ecp5SdfTests(unittest.TestCase):
    def model(self):
        return {"modules": {"top": {"cells": {
            "ff[0]": {"type":"TRELLIS_FF", "connections":{"CLK":[1],"M":[2],"Q":[3]}},
            "lut": {"type":"TRELLIS_COMB", "connections":{"A":[3],"F":[4]}}
        }}}}

    def test_preserves_polarities_and_setuphold_units(self):
        result=read_nextpnr_sdf(SDF,self.model())
        self.assertEqual(result['interconnect'][(('ff[0]','Q'),('lut','A'))],((.01,.02,.03),(.02,.03,.04)))
        self.assertEqual(result['cells']['ff[0]']['setuphold'][(('posedge','M'),('posedge','CLK'))],((.01,.02,.03),(-.005,0,.005)))
        self.assertFalse(result['timing_path_coverage_qualified'])
        self.assertFalse(result['global_timing_qualified'])

    def test_malformed_or_unsupported_sdf_fails(self):
        for text in (SDF[:-2],SDF.replace('1ps','1ns'),SDF.replace('ABSOLUTE','INCREMENT'),
                     SDF.replace('10:20:30','10::30'),SDF.replace('10:20:30','30:20:10'),
                     SDF.replace('10:20:30','nan:nan:nan'),SDF.replace('10:20:30','-1:0:1'),
                     SDF.replace('(IOPATH A F','(COND A F')):
            with self.subTest(text=text),self.assertRaises(ValidationError):
                read_nextpnr_sdf(text,self.model())

    def test_connectivity_and_cell_coverage_are_checked(self):
        for mutation in ('wire','missing','extra','type','port'):
            routed=self.model();cells=routed['modules']['top']['cells']
            if mutation=='wire':cells['lut']['connections']['A']=[99]
            if mutation=='missing':del cells['lut']
            if mutation=='extra':cells['extra']=copy.deepcopy(cells['lut'])
            if mutation=='type':cells['lut']['type']='UNKNOWN'
            if mutation=='port':del cells['ff[0]']['connections']['M']
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):
                read_nextpnr_sdf(SDF,routed)

    def test_duplicates_rejected(self):
        arc='(IOPATH A F (20:30:40) (30:40:50))'
        with self.assertRaises(ValidationError):
            read_nextpnr_sdf(SDF.replace(arc,arc+arc),self.model())
