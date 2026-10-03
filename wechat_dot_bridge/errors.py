class BridgeError(Exception):
    """Only fixed diagnostic codes may leave private runtime boundaries."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
