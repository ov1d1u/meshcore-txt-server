"""Version 1 text protocol shared with the Android client."""

from __future__ import annotations

import re
import secrets
import zlib
import base64
import binascii
from dataclasses import dataclass
from typing import Iterator, TextIO

MAX_FRAME_BYTES = 100
MAX_DECOMPRESSED_CHUNK_BYTES = 1024
_ID = r"[0-9A-Fa-f]{4}"
_REQUEST = re.compile(rf"^T1([IP])({_ID})([0-9]{{3}})?$")
_CANCEL = re.compile(rf"^T1C({_ID})$")
_RETRY = re.compile(rf"^T1R({_ID})\.([0-9A-Za-z]+)$")
_DATA = re.compile(rf"^T1([DZ])({_ID})\.([0-9A-Za-z]+)(?:\.([0-9A-Za-z]+))?:", re.DOTALL)
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_COMPRESSED_SIZES = (1024, 768, 512, 384, 256, 192, 128)
_END = re.compile(rf"^T1E({_ID})\.([0-9A-Za-z]+)\.([0-9A-Fa-f]{{8}})$")
_ERROR = re.compile(rf"^T1X({_ID})\.(BAD|NF|BUSY|IO)$")
_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    kind: str
    request_id: str
    page: int | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class Data:
    request_id: str
    sequence: int
    text: str
    count: int | None = None


@dataclass(frozen=True)
class End:
    request_id: str
    count: int
    crc32: int


@dataclass(frozen=True)
class Error:
    request_id: str
    code: str


def base36(value: int) -> str:
    if value < 0:
        raise ValueError("negative sequence")
    if value == 0:
        return "0"
    result = ""
    while value:
        value, digit = divmod(value, 36)
        result = _DIGITS[digit] + result
    return result


def new_request_id() -> str:
    return f"{secrets.randbelow(65536):04X}"


def request_frame(request_id: str, page: int | None = None) -> str:
    _check_id(request_id)
    if page is None:
        return f"T1I{request_id.upper()}"
    if not 100 <= page <= 999:
        raise ValueError("page must be 100..999")
    return f"T1P{request_id.upper()}{page:03d}"


def cancel_frame(request_id: str) -> str:
    _check_id(request_id)
    return f"T1C{request_id.upper()}"


def retry_frame(request_id: str, sequence: int) -> str:
    _check_id(request_id)
    return f"T1R{request_id.upper()}.{base36(sequence)}"


def error_frame(request_id: str, code: str) -> str:
    _check_id(request_id)
    if code not in {"BAD", "NF", "BUSY", "IO"}:
        raise ValueError("unknown error code")
    return f"T1X{request_id.upper()}.{code}"


def end_frame(request_id: str, count: int, crc32: int) -> str:
    _check_id(request_id)
    return f"T1E{request_id.upper()}.{base36(count)}.{crc32 & 0xFFFFFFFF:08X}"


def parse_request(frame: str) -> Request:
    _check_frame(frame)
    match = _CANCEL.fullmatch(frame)
    if match:
        return Request("C", match.group(1).upper())
    match = _RETRY.fullmatch(frame)
    if match:
        return Request("R", match.group(1).upper(), sequence=_parse_base36(match.group(2)))
    match = _REQUEST.fullmatch(frame)
    if not match:
        raise ProtocolError("invalid request")
    kind, request_id, page_text = match.groups()
    if kind == "I" and page_text is not None:
        raise ProtocolError("index request has a page")
    if kind == "P" and (page_text is None or not 100 <= int(page_text) <= 999):
        raise ProtocolError("invalid page")
    return Request(kind, request_id.upper(), int(page_text) if page_text else None)


def parse_response(frame: str) -> Data | End | Error:
    _check_frame(frame)
    match = _DATA.match(frame)
    if match:
        sequence = _parse_base36(match.group(3))
        count = _parse_base36(match.group(4)) if match.group(4) is not None else None
        if (sequence == 0) != (count is not None) or count == 0:
            raise ProtocolError("only the first data chunk carries a positive count")
        payload = frame[match.end():]
        if match.group(1) == "Z":
            payload = _decode_compressed(payload)
        return Data(match.group(2).upper(), sequence, payload, count)
    match = _END.fullmatch(frame)
    if match:
        return End(match.group(1).upper(), _parse_base36(match.group(2)), int(match.group(3), 16))
    match = _ERROR.fullmatch(frame)
    if match:
        return Error(match.group(1).upper(), match.group(2))
    raise ProtocolError("invalid response")


def iter_data_frames(request_id: str, source: TextIO, total_count: int) -> Iterator[tuple[str, bytes]]:
    """Yield frames and raw UTF-8 bytes without holding a page in memory."""
    _check_id(request_id)
    if total_count < 0:
        raise ValueError("negative chunk count")
    sequence = 0
    pending = bytearray()
    overflow = b""
    exhausted = False

    def header_for(number: int, kind: str) -> str:
        total = f".{base36(total_count)}" if number == 0 else ""
        return f"T1{kind}{request_id.upper()}.{base36(number)}{total}:"

    while True:
        if overflow:
            pending.extend(overflow)
            overflow = b""
        while len(pending) < MAX_DECOMPRESSED_CHUNK_BYTES and not exhausted:
            character = source.read(1)
            if not character:
                exhausted = True
                break
            encoded = character.encode("utf-8")
            if len(pending) + len(encoded) > MAX_DECOMPRESSED_CHUNK_BYTES:
                overflow = encoded
                break
            pending.extend(encoded)
        if not pending:
            break

        header = header_for(sequence, "D")
        capacity = MAX_FRAME_BYTES - len(header)
        raw_length = _utf8_prefix_length(pending, capacity)
        if raw_length == 0:
            raise ProtocolError("sequence header exceeds frame budget")

        compressed_length = 0
        compressed_payload = ""
        sizes = set(_COMPRESSED_SIZES)
        sizes.add(len(pending))
        for size in sorted(sizes, reverse=True):
            if size > len(pending):
                continue
            length = _utf8_prefix_length(pending, size)
            if length <= raw_length:
                break
            encoded = base64.urlsafe_b64encode(zlib.compress(pending[:length])).rstrip(b"=")
            if len(encoded) <= capacity:
                compressed_length = length
                compressed_payload = encoded.decode("ascii")
                break

        if compressed_length:
            raw = bytes(pending[:compressed_length])
            frame = header_for(sequence, "Z") + compressed_payload
        else:
            raw = bytes(pending[:raw_length])
            frame = header + raw.decode("utf-8")
        yield frame, raw
        del pending[:len(raw)]
        sequence += 1


def _utf8_prefix_length(data: bytes | bytearray, limit: int) -> int:
    length = min(len(data), limit)
    while length < len(data) and length > 0 and data[length] & 0xC0 == 0x80:
        length -= 1
    return length


def _decode_compressed(payload: str) -> str:
    if not _BASE64URL.fullmatch(payload):
        raise ProtocolError("invalid compressed payload")
    try:
        encoded = payload.encode("ascii")
        compressed = base64.b64decode(encoded + b"=" * (-len(encoded) % 4),
                                      altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(compressed).rstrip(b"=") != encoded:
            raise ProtocolError("noncanonical compressed payload")
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_DECOMPRESSED_CHUNK_BYTES + 1)
        if (len(raw) > MAX_DECOMPRESSED_CHUNK_BYTES or not inflater.eof
                or inflater.unused_data or inflater.unconsumed_tail):
            raise ProtocolError("invalid compressed stream")
        raw += inflater.flush()
        if not raw or len(raw) > MAX_DECOMPRESSED_CHUNK_BYTES:
            raise ProtocolError("invalid decompressed length")
        return raw.decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError, zlib.error) as exc:
        raise ProtocolError("invalid compressed payload") from exc


def _check_id(request_id: str) -> None:
    if not re.fullmatch(_ID, request_id):
        raise ValueError("request ID must be four hex digits")


def _check_frame(frame: str) -> None:
    if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds 100 UTF-8 bytes")


def _parse_base36(value: str) -> int:
    if len(value) > 13:
        raise ProtocolError("sequence too large")
    try:
        parsed = int(value, 36)
        if parsed > 0x7FFFFFFFFFFFFFFF:
            raise ProtocolError("sequence too large")
        return parsed
    except ValueError as exc:
        raise ProtocolError("invalid base36 value") from exc


def checksum(data: bytes, previous: int = 0) -> int:
    return zlib.crc32(data, previous) & 0xFFFFFFFF
