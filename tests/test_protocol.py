import io
import base64
import random
import unittest
import zlib
from pathlib import Path

from teletext.protocol import (
    ProtocolError,
    checksum,
    end_frame,
    iter_data_frames,
    parse_request,
    parse_response,
    request_frame,
    retry_frame,
)


class ProtocolTests(unittest.TestCase):
    def frames_for(self, body):
        count = 0
        while True:
            frames = list(iter_data_frames("BEEF", io.StringIO(body), count))
            if len(frames) == count:
                return frames
            count = len(frames)

    def test_requests(self):
        self.assertEqual(parse_request(request_frame("aB09")).kind, "I")
        self.assertEqual(parse_request(request_frame("AB09", 123)).page, 123)
        self.assertEqual(parse_request(retry_frame("ab09", 35)).sequence, 35)
        for frame in ("T1PAB09999x", "T1PAB09099", "T1IAB09123", "T1PAB09"):
            with self.assertRaises(ProtocolError):
                parse_request(frame)

    def test_unicode_chunks_and_checksum(self):
        body = "# Héllo 🌍\n" * 1000
        frames = self.frames_for(body)
        self.assertLess(len(frames), 100)
        self.assertTrue(all(frame.startswith("T1Z") for frame, _ in frames))
        self.assertTrue(all(len(frame.encode("utf-8")) <= 100 for frame, _ in frames))
        self.assertEqual("".join(parse_response(frame).text for frame, _ in frames), body)
        self.assertEqual(parse_response(frames[0][0]).count, len(frames))
        self.assertTrue(all(parse_response(frame).count is None for frame, _ in frames[1:]))
        crc = 0
        for _, raw in frames:
            crc = checksum(raw, crc)
        end = parse_response(end_frame("BEEF", len(frames), crc))
        self.assertEqual(end.count, len(frames))
        self.assertEqual(end.crc32, checksum(body.encode()))

    def test_mixed_frames_keep_plain_text_when_compression_does_not_help(self):
        random_text = "".join(random.Random(17).choices(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=1000))
        body = "repeated line\n" * 100 + random_text
        frames = self.frames_for(body)
        self.assertTrue(any(frame.startswith("T1Z") for frame, _ in frames))
        self.assertTrue(any(frame.startswith("T1D") for frame, _ in frames))
        self.assertEqual("".join(parse_response(frame).text for frame, _ in frames), body)
        self.assertTrue(all(len(frame.encode("utf-8")) <= 100 for frame, _ in frames))

    def test_incompressible_text_stays_plain(self):
        body = "".join(random.Random(23).choices(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=1000))
        frames = self.frames_for(body)
        self.assertTrue(all(frame.startswith("T1D") for frame, _ in frames))

    def test_plain_unicode_chunks_end_on_character_boundaries(self):
        body = "".join(chr(0x4E00 + index) for index in range(400))
        frames = self.frames_for(body)
        self.assertTrue(all(frame.startswith("T1D") for frame, _ in frames))
        self.assertEqual("".join(parse_response(frame).text for frame, _ in frames), body)
        self.assertTrue(all(len(frame.encode("utf-8")) <= 100 for frame, _ in frames))

    def test_rejects_invalid_compressed_payloads(self):
        def encoded(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        valid = zlib.compress(b"valid")
        bad_payloads = [
            "",
            "!",
            "A",
            encoded(valid[:-2]),
            encoded(valid + b"trailing"),
            encoded(zlib.compress(b"x" * 1025)),
            encoded(zlib.compress(b"\xff")),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_response(f"T1Z1234.0.1:{payload}")
        self.assertEqual(parse_response(
            f"T1Z1234.0.1:{encoded(zlib.compress(b'x' * 1024))}").text,
            "x" * 1024)

    def test_empty_page(self):
        self.assertEqual(list(iter_data_frames("0001", io.StringIO(""), 0)), [])
        self.assertEqual(end_frame("0001", 0, 0), "T1E0001.0.00000000")

    def test_payload_can_include_delimiters(self):
        response = parse_response("T1D1234.0.1:a:b.c")
        self.assertEqual(response.text, "a:b.c")
        for frame in ("T1D1234.0:old", "T1D1234.1.2:wrong", "T1D1234.0.0:empty"):
            with self.assertRaises(ProtocolError):
                parse_response(frame)

    def test_shared_fixtures(self):
        fixture = Path(__file__).parents[1] / "fixtures" / "protocol.tsv"
        for line in fixture.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                continue
            frame, kind, request_id, value = line.split("\t")
            response = parse_response(frame)
            self.assertEqual(response.request_id, request_id)
            if kind == "D":
                count = f"/{response.count}" if response.count is not None else ""
                self.assertEqual(f"{response.sequence}{count}:{response.text}", value)
            elif kind == "E":
                self.assertEqual(f"{response.count}:{response.crc32:08X}", value)
            else:
                self.assertEqual(response.code, value)


if __name__ == "__main__":
    unittest.main()
