import os
import unittest
from unittest.mock import patch
from emuflow.errors import ValidationError
from emuflow.snapshot_host import SnapshotHostClient, encode_record, decode_record


class Stream:
    def __init__(self, payloads):
        self.pending = b"".join(encode_record(p, n) for n, p in enumerate(payloads))
        self.sent = bytearray()
        self.timeouts = []

    def read(self, count, timeout):
        self.timeouts.append(timeout)
        result, self.pending = self.pending[:min(3,count)], self.pending[min(3,count):]
        return result

    def write(self, data, timeout):
        self.timeouts.append(timeout)
        count = min(4, len(data))
        self.sent.extend(data[:count])
        return count


class SnapshotHostTests(unittest.TestCase):
    def client(self, stream, **kw):
        return SnapshotHostClient(stream, session_id=0x789, input_bits=33, output_bits=33, **kw)

    def test_rtl_golden_crc_and_corruption(self):
        golden=bytes.fromhex("a55a0100000123456789abcdefa513")
        self.assertEqual(encode_record(0x0123456789abcdef,0),golden)
        self.assertEqual(decode_record(golden,0),0x0123456789abcdef)
        for index in range(15):
            damaged=bytearray(golden); damaged[index]^=1
            with self.assertRaises(ValidationError): decode_record(bytes(damaged),0)
        with self.assertRaises(ValidationError): decode_record(golden,1)
        with self.assertRaises(ValidationError): decode_record(golden[:-1],0)

    def test_partial_io_two_steps_and_exact_commands(self):
        stream=Stream([0x6101000000210021,
                       0x72000000deadbeef,0x7201000000000001,0x7300000000000000,
                       0x7200000112345678,0x7201000100000000,0x7300000100000000])
        client=self.client(stream); client.connect()
        self.assertEqual(client.step(0x112345678),0x1deadbeef)
        self.assertEqual(client.step(0),0x12345678)
        records=[decode_record(bytes(stream.sent[n*15:(n+1)*15]),n) for n in range(7)]
        self.assertEqual(records,[0x6001000000000789,0x7000000012345678,
            0x7001000000000001,0x7100000000000000,0x7000000100000000,
            0x7001000100000000,0x7100000100000000])
        self.assertEqual(client.epoch,2)

    def test_bad_done_padding_index_fail_without_retry(self):
        for replies in ([0x7200000000000000,0x7201000000000000,0x7300000100000000],
                        [0x7200000000000000,0x7201000000000002],
                        [0x7201000000000000]):
            stream=Stream([0x6101000000210021,*replies]); client=self.client(stream); client.connect()
            with self.assertRaises(ValidationError): client.step(0)
            self.assertTrue(client.failed)
            before=bytes(stream.sent)
            with self.assertRaises(ValidationError): client.step(0)
            with self.assertRaises(ValidationError): client.connect()
            self.assertEqual(bytes(stream.sent),before)

    def test_configuration_and_receive_timeout(self):
        client=self.client(Stream([0x6101000000200021]))
        with self.assertRaises(ValidationError): client.connect()
        self.assertTrue(client.failed)
        client=self.client(Stream([]))
        with self.assertRaises(TimeoutError): client.connect()
        self.assertTrue(client.failed)

    def test_deadline_is_not_renewed_by_partial_read(self):
        stream=Stream([0x6101000000210021]); clock=[0.0]
        old=stream.read
        def slow(count,timeout):
            clock[0]+=0.4
            return old(count,timeout)
        stream.read=slow
        with patch("emuflow.snapshot_host.time.monotonic",side_effect=lambda:clock[0]):
            client=self.client(stream,timeout=1)
            with self.assertRaises(TimeoutError): client.connect()
        self.assertTrue(client.failed)
        self.assertLessEqual(clock[0],1.21)

    def test_bad_arguments_never_touch_stream(self):
        for timeout in (0,-1,True,float("nan"),float("inf")):
            with self.assertRaises(ValidationError): self.client(None,timeout=timeout)
        stream=Stream([0x6101000000210021]); client=self.client(stream); client.connect()
        before=bytes(stream.sent)
        for value in (True,-1,1<<33):
            with self.assertRaises(ValidationError): client.step(value)
        self.assertEqual(bytes(stream.sent),before)
        self.assertFalse(client.failed)

    @unittest.skipUnless(os.name=="posix","POSIX serial transport")
    def test_real_pty_serial_transport(self):
        import pty
        from emuflow.snapshot_serial import SnapshotSerialPort
        master,slave=pty.openpty()
        try:
            with SnapshotSerialPort(os.ttyname(slave)) as port:
                os.write(master,b"\x00\xff\x0a\x13")
                self.assertEqual(port.read(4,1),b"\x00\xff\x0a\x13")
                self.assertEqual(port.write(b"\x11\x0d\x00",1),3)
                import select
                self.assertTrue(select.select([master],[],[],1)[0])
                self.assertEqual(os.read(master,3),b"\x11\x0d\x00")
                with self.assertRaises(TimeoutError): port.read(1,0.01)
            self.assertIsNone(port.fd)
        finally:
            os.close(master); os.close(slave)
