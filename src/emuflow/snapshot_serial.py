"""POSIX USB-serial transport with select-bounded I/O; no extra dependency."""
import os
import select
import termios


class SnapshotSerialPort:
    def __init__(self, device):
        self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if not os.isatty(self.fd):
                raise ValueError("snapshot host requires a serial TTY")
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[4] = attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception:
            self.close()
            raise

    def read(self, count, timeout):
        if not select.select([self.fd], [], [], timeout)[0]:
            raise TimeoutError("serial receive timeout")
        return os.read(self.fd, count)

    def write(self, data, timeout):
        if not select.select([], [self.fd], [], timeout)[1]:
            raise TimeoutError("serial transmit timeout")
        return os.write(self.fd, data)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
