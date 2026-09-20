"""CTS600 / Prot485 protocol layer.

Implements the framing, CRC, and payload structures described in
Lodam's "517200JE Nilan CTS 600 Betjeningsprotokol" (referred to below
as the Lodam protocol document), and confirmed against a live bus
capture.

This module has no I/O in it — it only turns bytes into structured
Python objects and back. See transport.py for the serial layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


# ---------------------------------------------------------------------------
# Function codes (_FC_xxx in the doc)
# ---------------------------------------------------------------------------

class FunctionCode(IntEnum):
    READ_OUTPUT_BITS = 1
    READ_INPUT_BITS = 2
    READ_OUTPUT_REGS = 3
    READ_INPUT_REGS = 4
    WRITE_OUTPUT_BIT = 5
    WRITE_OUTPUT_REG = 6
    WRITE_OUTPUT_BITS = 15
    WRITE_OUTPUT_REGS = 16
    REPORT_SLAVE_ID = 17
    # Lodam-specific (65-72 reserved for Lodam per the doc)
    WI_RO_BITS = 65
    WI_RO_REGS = 66

    EXCEPTION_FLAG = 0x80  # OR'ed onto the function code in an error response


class ErrorCode(IntEnum):
    ILLEGAL_FUNCTION = 1
    ILLEGAL_ADDRESS = 2
    ILLEGAL_VALUE = 3
    DEVICE_FAIL = 4
    SLAVE_BUSY = 6


# Node addresses, per the doc's convention.
NODE_BETJENING = 1   # control panel (bus master role, normally)
NODE_PC = 2
NODE_STYREENHED_1 = 3
NODE_STYREENHED_2 = 4


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def modbus_crc16(data: bytes) -> int:
    """Standard Modbus CRC-16 (poly 0xA001, init 0xFFFF), as an int."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def cts600_crc_bytes(data: bytes) -> bytes:
    """CRC as the two bytes actually placed on the wire.

    The Lodam variant computes the ordinary Modbus CRC and then swaps
    the hi/lo byte order before transmission (see the C reference in
    the Lodam protocol document, and cts600_sniffer.py's cts600_crc()).
    """
    crc = modbus_crc16(data)
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def verify_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    payload, received = frame[:-2], frame[-2:]
    return cts600_crc_bytes(payload) == received


# ---------------------------------------------------------------------------
# Frame encode/decode
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """A single typePACKET: address + function + data + CRC."""

    address: int
    function: int
    data: bytes
    raw: bytes = field(default=b"", repr=False)
    crc_ok: bool = True

    @property
    def is_exception(self) -> bool:
        return bool(self.function & FunctionCode.EXCEPTION_FLAG)

    @property
    def base_function(self) -> int:
        return self.function & ~FunctionCode.EXCEPTION_FLAG


def encode_frame(address: int, function: int, data: bytes = b"") -> bytes:
    """Build a complete on-the-wire frame, CRC included."""
    body = bytes([address, function]) + data
    return body + cts600_crc_bytes(body)


def decode_frame(raw: bytes) -> Frame | None:
    """Parse raw bytes captured off the bus into a Frame.

    Returns None if the frame is too short to be meaningful
    (address + function + CRC is the minimum, i.e. 4 bytes).
    """
    if len(raw) < 4:
        return None
    address, function = raw[0], raw[1]
    data = raw[2:-2]
    crc_ok = verify_crc(raw)
    return Frame(address=address, function=function, data=data, raw=raw, crc_ok=crc_ok)


# ---------------------------------------------------------------------------
# typeWI_RO_REGS / typeWI_RO_BITS payload structures (FC 65 / 66)
#
# Both directions (query and response) use the same layout in this
# protocol: a start address, a count, a byte count, and the data itself.
# What changes is *who* is asserting which half of the exchange on a
# given wire turn -- but structurally query and response share the
# shape (confirmed by the worked example in the Lodam document's
# section 4 and
# by the capture, where every FC41/FC42 frame -- master query and
# slave response alike -- follows this shape).
# ---------------------------------------------------------------------------

@dataclass
class RegBlock:
    start_reg: int
    count: int
    byte_count: int
    data: bytes  # length == byte_count (word data, hi byte first)

    def words(self) -> list[int]:
        n = min(self.count, len(self.data) // 2)
        return [
            (self.data[2 * i] << 8) | self.data[2 * i + 1]
            for i in range(n)
        ]

    def as_text(self) -> str:
        """Render the word data as text (used for display regs).

        Duplicated (not imported) from display_decoder._EXTENDED_CHARSET
        on purpose -- this module stays dependency-free so master.py can
        reuse it without pulling in display concerns. Keep both in sync.
        """
        chars = []
        for b in self.data[: self.byte_count]:
            if 32 <= b <= 126:
                chars.append(chr(b))
            elif b == 0x0B:
                chars.append("Ä")
            elif b == 0x0C:
                chars.append("Ö")
            elif b == 0xDF:
                chars.append("°")
            else:
                chars.append(".")
        return "".join(chars)


@dataclass
class BitBlock:
    start_bit: int
    count: int
    byte_count: int
    data: bytes

    def bits(self) -> list[int]:
        out = []
        for i in range(self.count):
            byte_i, bit_i = divmod(i, 8)
            if byte_i >= len(self.data):
                break
            out.append((self.data[byte_i] >> bit_i) & 1)
        return out


def parse_reg_block(payload: bytes) -> RegBlock | None:
    """Parse the datadel of an FC66 (WI_RO_REGS) frame."""
    if len(payload) < 6:
        return None
    start_reg = (payload[0] << 8) | payload[1]
    count = (payload[2] << 8) | payload[3]
    byte_count = (payload[4] << 8) | payload[5]
    data = payload[6:6 + byte_count]
    return RegBlock(start_reg, count, byte_count, data)


def parse_bit_block(payload: bytes) -> BitBlock | None:
    """Parse the datadel of an FC65/FC41 (WI_RO_BITS) frame.

    NOTE: the Lodam document's own worked example (section 4) shows
    this struct as addr(2) + count(2) + byteCount(1) + data(byteCount),
    with no gap. Bytes actually observed on this bus don't match that
    -- the bus wins here, and this parser follows the bus:
    e.g. raw payload `01 00 00 02 00 01 00` decodes (per ground-truth
    labels already present in cts600_capture.txt) as REG=0x0100,
    COUNT=2, BYTES=1, BITDATA=0x00 -- which only lines up if there is
    one extra (so far always-zero) byte between the count and the
    byte-count fields. We follow the empirically observed layout
    here rather than the doc's minimal example, since it's the one
    that actually matches captured traffic. Worth re-checking if a
    future capture shows a non-zero value in that reserved-looking
    byte.
    """
    if len(payload) < 6:
        return None
    start_bit = (payload[0] << 8) | payload[1]
    count = (payload[2] << 8) | payload[3]
    # payload[4] is an unexplained byte, zero in every frame seen so far.
    byte_count = payload[5]
    data = payload[6:6 + byte_count]
    return BitBlock(start_bit, count, byte_count, data)


def encode_reg_block(start_reg: int, words: list[int]) -> bytes:
    """Build the datadel for an FC66 request/response from a list of words."""
    data = bytearray()
    for w in words:
        data.append((w >> 8) & 0xFF)
        data.append(w & 0xFF)
    header = bytes([
        (start_reg >> 8) & 0xFF, start_reg & 0xFF,
        (len(words) >> 8) & 0xFF, len(words) & 0xFF,
        (len(data) >> 8) & 0xFF, len(data) & 0xFF,
    ])
    return header + bytes(data)


# ---------------------------------------------------------------------------
# typeSLAVEID payload (FC 17 / REPORT_SLAVE_ID response)
# ---------------------------------------------------------------------------

@dataclass
class SlaveId:
    slave_id: int
    run_status: int
    error_status: int
    reset_status: int
    prot_ver: int
    sw_ver_raw: int
    sw_date_raw: int
    sw_time_raw: int
    product: str
    nb_outputs: int
    nb_leds: int
    nb_inputs: int
    nb_keys: int
    nb_output_regs: int
    nb_input_regs: int
    nb_actions: int
    nb_display_cols: int
    nb_display_rows: int
    display_type: int
    display_data_type: int

    @property
    def sw_version(self) -> str:
        # doc: "Software version (x.xx)" -- stored as a 16-bit value.
        return f"{self.sw_ver_raw / 100:.2f}"


def parse_slave_id(payload: bytes) -> SlaveId | None:
    """Parse the datadel of an FC17 (_FC_REPORT_SLAVE_ID) response.

    Layout per the Lodam document's typeSLAVEID (byte_count byte is payload[0]
    and is not otherwise needed here since we already know len(payload)).
    """
    if len(payload) < 1:
        return None
    p = payload[1:]  # skip ucByteCount
    if len(p) < 32:
        return None

    def u16(off):
        return (p[off] << 8) | p[off + 1]

    slave_id = p[0]
    run_status = p[1]
    error_status = p[2]
    reset_status = p[3]
    prot_ver = p[4]
    sw_ver = u16(5)
    sw_date = u16(7)
    sw_time = u16(9)
    product = bytes(p[11:21]).decode("ascii", "replace").strip("\x00")
    off = 21
    nb_outputs = u16(off)
    nb_leds = u16(off + 2)
    nb_inputs = u16(off + 4)
    nb_keys = u16(off + 6)
    nb_output_regs = u16(off + 8)
    nb_input_regs = u16(off + 10)
    # ucReserved[2] at off+12
    nb_actions = u16(off + 14)
    nb_display_cols = u16(off + 16)
    nb_display_rows = u16(off + 18)
    display_type = p[off + 20] if len(p) > off + 20 else 0
    display_data_type = p[off + 21] if len(p) > off + 21 else 0

    return SlaveId(
        slave_id, run_status, error_status, reset_status, prot_ver,
        sw_ver, sw_date, sw_time, product,
        nb_outputs, nb_leds, nb_inputs, nb_keys,
        nb_output_regs, nb_input_regs, nb_actions,
        nb_display_cols, nb_display_rows, display_type, display_data_type,
    )


# ---------------------------------------------------------------------------
# Standard Modbus read requests (FC 1-4 -- typeREQUEST in the doc): read N
# registers/bits starting at an address. Used for actively probing
# addresses the panel doesn't cyclically report (see the Lodam
# document's "Lodam Modbus adresser" table and its "_FC_READ_OUTPUT_REGS" row --
# "ved initialisering / efter behov", i.e. queried at startup or
# on-demand only, unlike the constantly-repeated FC65/66 traffic).
# ---------------------------------------------------------------------------

def encode_read_query(address: int, function: int, start: int, count: int) -> bytes:
    """Build a standard Modbus read request (typeREQUEST): address
    high/low byte, then count high/low byte (per the doc's
    typeREQUEST layout, which this reuses for any of FC1-4)."""
    data = bytes([
        (start >> 8) & 0xFF, start & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF,
    ])
    return encode_frame(address, function, data)


def parse_read_regs_response(payload: bytes) -> list[int] | None:
    """Parse a typeRESPONSE datadel (byte count + data) from an FC3/4
    reply into a list of register words. None if payload is too short
    for the byte count it claims."""
    if len(payload) < 1:
        return None
    byte_count = payload[0]
    data = payload[1:1 + byte_count]
    if len(data) < byte_count:
        return None
    return [
        (data[2 * i] << 8) | data[2 * i + 1]
        for i in range(byte_count // 2)
    ]


def parse_read_bits_response(payload: bytes) -> list[int] | None:
    """Parse a typeRESPONSE datadel from an FC1/2 reply into a list of
    individual bit values (bit 0 first, per the doc)."""
    if len(payload) < 1:
        return None
    byte_count = payload[0]
    data = payload[1:1 + byte_count]
    if len(data) < byte_count:
        return None
    bits = []
    for byte in data:
        for i in range(8):
            bits.append((byte >> i) & 1)
    return bits


def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)
