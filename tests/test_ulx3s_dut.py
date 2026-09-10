import unittest
from emuflow.ulx3s_dut import build_ulx3s_snapshot_top
from emuflow.errors import ValidationError


class ULX3SDUTTests(unittest.TestCase):
    def build(self, **kw):
        args=dict(top="board_top",dut_module="partition",board="board0",
                  exported_bits=33,imported_bits=17,words=2,session_id=0x1234)
        args.update(kw)
        return build_ulx3s_snapshot_top(**args)

    def test_padding_and_role(self):
        result=self.build()
        self.assertIn("{31'b0, exported_values}",result)
        self.assertIn("remote_values[16:0]",result)
        self.assertIn(".LEADER(1)",result)
        self.assertIn(".LEADER(0)",self.build(board="board1"))
        self.assertIn(".step(commit && !fault && !link_fault && !reset)",result)

    def test_no_zero_width_concat(self):
        self.assertNotIn("0'b0",self.build(exported_bits=64))

    def test_invalid_contract(self):
        for kw in (dict(top="bad;name"),dict(dut_module="board_top"),
                   dict(board="board2"),dict(words=True),dict(words=257),
                   dict(exported_bits=65),dict(imported_bits=0),dict(session_id=-1)):
            with self.subTest(kw=kw), self.assertRaises(ValidationError): self.build(**kw)
