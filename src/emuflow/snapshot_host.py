"""Bounded, fail-stop software client for the ULX3S snapshot host protocol."""
import binascii
import math
import time
from .errors import ValidationError


def encode_record(payload: int, sequence: int) -> bytes:
    if type(payload) is not int or not 0 <= payload < 2**64:
        raise ValidationError("record payload must be uint64")
    if type(sequence) is not int or not 0 <= sequence < 2**16:
        raise ValidationError("record sequence must be uint16")
    body = b"\x01" + sequence.to_bytes(2, "big") + payload.to_bytes(8, "big")
    return b"\xa5\x5a" + body + binascii.crc_hqx(body, 0xffff).to_bytes(2, "big")


def decode_record(frame: bytes, sequence: int) -> int:
    if len(frame) != 15 or frame[:3] != b"\xa5\x5a\x01":
        raise ValidationError("bad host record framing/version")
    if int.from_bytes(frame[3:5], "big") != sequence:
        raise ValidationError("host record sequence mismatch")
    if binascii.crc_hqx(frame[2:13], 0xffff) != int.from_bytes(frame[13:], "big"):
        raise ValidationError("host record CRC mismatch")
    return int.from_bytes(frame[5:13], "big")


class SnapshotHostClient:
    """One synchronous caller, no retries or resynchronization after failure.

    stream.read(count, timeout) and stream.write(data, timeout) must return
    within the supplied seconds. Short reads/writes are permitted; zero progress
    fails. timeout bounds the entire connect or step, not each individual byte.
    The caller must coordinate hardware reset before opening a new session.
    """
    def __init__(self, stream, *, session_id, input_bits, output_bits, timeout=30.0):
        if type(session_id) is not int or not 0 <= session_id < 2**32:
            raise ValidationError("host session must be uint32")
        for bits in (input_bits, output_bits):
            if type(bits) is not int or not 1 <= bits <= 8192:
                raise ValidationError("host width must be 1..8192 bits including padding")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValidationError("host timeout must be finite and positive")
        self.stream, self.session_id = stream, session_id
        self.input_bits, self.output_bits, self.timeout = input_bits, output_bits, timeout
        self.tx_sequence = self.rx_sequence = self.epoch = 0
        self.connected = self.failed = False

    def _remaining(self, deadline):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError("snapshot host transaction deadline expired")
        return remaining

    def _send(self, payload, deadline):
        data = encode_record(payload, self.tx_sequence)
        while data:
            count = self.stream.write(data, self._remaining(deadline))
            if type(count) is not int or not 0 < count <= len(data):
                raise TimeoutError("snapshot host write made no valid progress")
            data = data[count:]
        self.tx_sequence = (self.tx_sequence+1) & 0xffff

    def _receive(self, deadline):
        frame = bytearray()
        while len(frame) < 15:
            part = self.stream.read(15-len(frame), self._remaining(deadline))
            if not isinstance(part, bytes) or not 0 < len(part) <= 15-len(frame):
                raise TimeoutError("snapshot host read incomplete")
            frame.extend(part)
        self._remaining(deadline)
        result = decode_record(bytes(frame), self.rx_sequence)
        self.rx_sequence = (self.rx_sequence+1) & 0xffff
        return result

    def connect(self):
        if self.connected or self.failed:
            raise ValidationError("client cannot reconnect or reuse a failed session")
        deadline = time.monotonic()+self.timeout
        try:
            self._send(0x6001000000000000 | self.session_id, deadline)
            expected = 0x6101000000000000 | self.input_bits << 16 | self.output_bits
            if self._receive(deadline) != expected:
                raise ValidationError("host configuration/width mismatch")
            self.connected = True
        except Exception:
            self.failed = True
            raise

    def step(self, inputs: int) -> int:
        """Return pre-active-edge DUT outputs, only after a matching DONE."""
        if not self.connected or self.failed or self.epoch > 0xffff:
            raise ValidationError("host session unavailable or exhausted")
        if type(inputs) is not int or not 0 <= inputs < 1 << self.input_bits:
            raise ValidationError("host input does not fit the declared vector")
        deadline = time.monotonic()+self.timeout
        try:
            for index in range((self.input_bits+31)//32):
                self._send(0x7000000000000000 | index << 48 | self.epoch << 32 |
                           ((inputs >> (index*32)) & 0xffffffff), deadline)
            self._send(0x7100000000000000 | self.epoch << 32, deadline)
            value = 0
            for index in range((self.output_bits+31)//32):
                record = self._receive(deadline)
                if record >> 32 != (0x72000000 | index << 16 | self.epoch):
                    raise ValidationError("host output index/epoch mismatch")
                value |= (record & 0xffffffff) << (index*32)
            if value >= 1 << self.output_bits:
                raise ValidationError("host output contains nonzero padding")
            if self._receive(deadline) != (0x7300000000000000 | self.epoch << 32):
                raise ValidationError("host completion epoch mismatch")
            self.epoch += 1
            return value
        except Exception:
            self.failed = True
            raise
