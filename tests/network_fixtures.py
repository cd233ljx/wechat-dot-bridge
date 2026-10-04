"""Synthetic socket fixtures for real HTTPCore framing and cancellation tests."""

class FakeTimer:
    instances = []

    def __init__(self, interval, callback):
        self.interval, self.callback = interval, callback
        self.daemon = False
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class FakeSocket:
    def __init__(self, response=b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.response = response
        self.connected = []
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.shutdowns = []
        self.on_handshake = None

    def connect(self, address):
        self.connected.append(address)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, value):
        if self.closed:
            raise OSError("fixture closed socket")
        self.sent.append(bytes(value))

    def recv(self, count):
        if self.closed:
            raise OSError("fixture closed socket")
        part, self.response = self.response[:count], self.response[count:]
        return part

    def do_handshake(self):
        if self.on_handshake:
            self.on_handshake()
        if self.closed:
            raise OSError("fixture expired handshake")

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed = True


