"""Error type with a stable machine-readable code (used for CLI JSON and exit codes)."""

EXIT_CODES = {
    'INVALID_ARG': 2, 'NOT_FOUND': 3, 'BUSY': 4, 'TIMEOUT_NO_TRIGGER': 5, 'TIMEOUT_WATCHDOG': 6,
    'USB_ERROR': 6, 'USB_NO_MEM': 6, 'DECODE_ERROR': 6, 'FPGA_CONFIG_FAILED': 6, 'SECURITY_FAILED': 6,
    'PROTOCOL_ERROR': 6,
    'FW_TOO_OLD': 6, 'SECURITY_KEY_BLANK': 6, 'UNSAFE_OPERATION': 7, 'FIRMWARE_SOURCE': 6,
    'DEVICE_ERROR': 6, 'TIMEOUT_DATA': 6, 'UNSUPPORTED': 6, 'STOPPED': 0, 'HOST_TOO_SLOW': 6, 'WRITE_FAILED': 6,
    'STREAM_BETTER': 2, 'NEEDS_FORCE': 2, 'TRIGGER_OUT_OF_RANGE': 2, 'HOOK_ERROR': 2,
}


class DscopeError(Exception):
    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    @property
    def exit_code(self):
        return EXIT_CODES.get(self.code, 1)

    def to_dict(self):
        d = {'code': self.code, 'message': self.message}
        if self.hint:
            d['hint'] = self.hint
        if getattr(self, 'plan', None):        # STREAM_BETTER / NEEDS_FORCE: the numbers behind the decision
            d['plan'] = self.plan
        return d
