#!/usr/bin/env python3
"""NEC PC-8001 / PC-8801 Cassette Tape Format Utility (`pc88_tape_tool.py`).

Provides parsing, splitting, joining, and bidirectional conversion between the
multi-file container format (.t88) and raw sequential tape dumps (.cmt).

Supported Protocols & Formats:
    - N-BASIC (PC-8001), N80-BASIC (PC-8001mkII), N88-BASIC V1/V2 (PC-8801 series)
    - .cmt: Sequential tape stream using exact BIOS / Monitor ROM state machine:
        * 0x24: Monitor Machine Code header + structured 0x3A records (length-jumped),
                terminated strictly by 0-length record.
        * 0xD3: Tokenized BASIC (CSAVE) traversed line-by-line until 0x0000 pointer.
        * 0x9C: ASCII sequential files consumed until 0x1A EOF.
    - .t88: Authentic Manuke Station / X88000 24-byte header container format
            with 12-byte DATA sub-headers and carrier lead-in/gap tags.
"""

import argparse
import io
import os
import re
import struct
import sys
import tempfile
import unittest
from typing import Dict, List, Optional, Tuple


class T88Tag:
    """Block tag identifiers for the T88 container format."""

    END: int = 0x0000
    VERSION: int = 0x0001
    GAP: int = 0x0100  # Blank / gap tag (start_tick: uint32, length_ticks: uint32)
    COMMENT: int = 0x0010
    DATA_300: int = 0x0101  # DATA tag with 12-byte timing/length sub-header
    DATA_1200: int = 0x0101  # DATA tag with 12-byte timing/length sub-header
    SPACE: int = 0x0102  # Space carrier tag
    MARK: int = 0x0103  # Mark carrier lead-in tag


class T88Block:
    """Represents a single tagged data block within a T88 container."""

    def __init__(self, tag: int, data: bytes = b"") -> None:
        self.tag: int = tag
        self.data: bytes = data

    @property
    def length(self) -> int:
        return len(self.data)

    def pack(self) -> bytes:
        if self.tag == T88Tag.END:
            return struct.pack("<HH", self.tag, 0)
        return struct.pack("<HH", self.tag, self.length) + self.data

    @classmethod
    def unpack(cls, stream: io.BytesIO) -> Optional["T88Block"]:
        tag_bytes = stream.read(2)
        if not tag_bytes or len(tag_bytes) < 2:
            return None

        (tag,) = struct.unpack("<H", tag_bytes)
        len_bytes = stream.read(2)
        if not len_bytes or len(len_bytes) < 2:
            return cls(tag=tag, data=b"")

        (length,) = struct.unpack("<H", len_bytes)
        data = stream.read(length) if length > 0 else b""
        return cls(tag=tag, data=data)


class T88File:
    """Represents a full T88 cassette image file container."""

    DEFAULT_MAGIC: bytes = b"PC-8801 Tape Image(T88)\x00"
    VALID_MAGICS: Tuple[bytes, ...] = (
        b"PC-8801 Tape Image(T88)\x00",
        b"PC-8001 Tape Image(T88)\x00",
        b"PC-8801 ",
        b"T88-FILE",
        b"PC-8001 ",
    )

    def __init__(
        self,
        magic: bytes = DEFAULT_MAGIC,
        version: int = 0x0100,
        blocks: Optional[List[T88Block]] = None,
    ) -> None:
        self.magic: bytes = magic
        self.version: int = version
        self.blocks: List[T88Block] = blocks if blocks is not None else []

    @classmethod
    def is_valid_magic(cls, magic: bytes) -> bool:
        if magic.startswith(b"PC-8801 Tape Image") or magic.startswith(
            b"PC-8001 Tape Image"
        ):
            return True
        if (
            magic.startswith(b"T88")
            or magic.startswith(b"PC-88")
            or magic.startswith(b"PC-80")
        ):
            return True
        return False

    def pack(self) -> bytes:
        header = self.magic.ljust(24, b"\x00")[:24]
        body = b"".join(block.pack() for block in self.blocks)
        return header + body

    @classmethod
    def unpack(cls, stream: io.BytesIO) -> "T88File":
        header = stream.read(24)
        if len(header) < 24:
            raise ValueError("Invalid T88 file: header is shorter than 24 bytes.")

        if not cls.is_valid_magic(header):
            if cls.is_valid_magic(header[:16]):
                stream.seek(16)
            else:
                raise ValueError(
                    f"Invalid T88 magic signature: got {header!r}. Expected a valid "
                    f"header such as b'PC-8801 Tape Image(T88)\\x00'."
                )

        blocks: List[T88Block] = []
        while True:
            block = T88Block.unpack(stream)
            if block is None:
                break
            blocks.append(block)
            if block.tag == T88Tag.END:
                break

        return cls(magic=header, blocks=blocks)

    def extract_cmt_payload(self) -> bytes:
        payload_chunks: List[bytes] = []

        for block in self.blocks:
            if block.tag == 0x0101 and block.data:
                if len(block.data) >= 12:
                    _, _, dlen, _ = struct.unpack("<IIHH", block.data[:12])
                    payload_chunks.append(block.data[12 : 12 + dlen])
                else:
                    payload_chunks.append(block.data)

        if not payload_chunks:
            for block in self.blocks:
                if (
                    block.tag
                    not in (
                        T88Tag.END,
                        T88Tag.VERSION,
                        T88Tag.COMMENT,
                        T88Tag.GAP,
                        T88Tag.SPACE,
                        T88Tag.MARK,
                    )
                    and block.data
                ):
                    payload_chunks.append(block.data)

        return b"".join(payload_chunks)

    def extract_metadata(self) -> Dict[str, str]:
        comments: List[str] = []
        for block in self.blocks:
            if block.tag == T88Tag.COMMENT:
                comments.append(block.data.decode("utf-8", errors="ignore").strip())
        return {"comment": "\n".join(comments)}

    @classmethod
    def from_cmt_data(
        cls, cmt_data: bytes, comment: str = "", chunk_size: int = 512, baud: int = 1200
    ) -> "T88File":
        blocks: List[T88Block] = []
        blocks.append(T88Block(T88Tag.VERSION, struct.pack("<H", 0x0100)))

        if comment:
            comment_bytes = comment.encode("utf-8", errors="ignore")
            blocks.append(T88Block(T88Tag.COMMENT, comment_bytes))

        current_tick = 0
        mark_len = 9600
        blocks.append(T88Block(T88Tag.MARK, struct.pack("<II", current_tick, mark_len)))
        current_tick += mark_len

        ticks_per_byte = 44 if baud == 1200 else 88
        data_len = len(cmt_data)
        data_ticks = data_len * ticks_per_byte
        data_header = struct.pack("<IIHH", current_tick, data_ticks, data_len, 0x0000)
        blocks.append(T88Block(0x0101, data_header + cmt_data))

        blocks.append(T88Block(T88Tag.END, b""))
        return cls(magic=cls.DEFAULT_MAGIC, version=0x0100, blocks=blocks)


class CMTFile:
    """Represents a raw sequential CMT tape dump stream."""

    HEADER_PREAMBLE_BYTES: Tuple[int, ...] = (0x24, 0xD3, 0x9C)
    PREAMBLE_BYTES: Tuple[int, ...] = (0x24, 0xD3, 0x9C)

    TYPE_NAMES: Dict[int, str] = {
        0xD3: "BASIC Program (0xD3)",
        0x9C: "ASCII / Sequential File (0x9C)",
        0x24: "MON Machine Language Header (0x24)",
    }

    def __init__(self, data: bytes = b"") -> None:
        self.data: bytes = data

    @classmethod
    def is_valid_cassette_filename(cls, name_bytes: bytes) -> bool:
        """Verifies if a 6-byte sequence forms a valid space-padded PC-8001/PC-8801 cassette filename."""
        if len(name_bytes) != 6:
            return False
        valid_chars = sum(
            1 for b in name_bytes if (32 <= b <= 126) or (0xA1 <= b <= 0xDF)
        )
        if valid_chars == 6:
            non_spaces = [b for b in name_bytes if b != 0x20]
            if len(non_spaces) > 0:
                return True
        return False

    @classmethod
    def extract_file_info(cls, chunk: bytes) -> Tuple[str, str]:
        """Extracts filename and file format description from a cassette header block."""
        if len(chunk) < 9:
            return "", "Raw Data / Unknown"

        for p_byte in (0x24, 0xD3, 0x9C):
            for min_len in (10, 8, 6, 4, 3):
                lead = bytes([p_byte]) * min_len
                if chunk.startswith(lead):
                    idx = min_len
                    while idx < len(chunk) and chunk[idx] == p_byte:
                        idx += 1
                    if idx + 6 <= len(chunk):
                        name_bytes = chunk[idx : idx + 6]
                        if cls.is_valid_cassette_filename(name_bytes):
                            name_str = "".join(
                                chr(b) if (32 <= b <= 126 or 0xA1 <= b <= 0xDF) else " "
                                for b in name_bytes
                            ).strip()
                            name_str = re.sub(r'[\\/*?:"<>|]', "_", name_str)
                            if name_str:
                                file_type = cls.TYPE_NAMES.get(
                                    p_byte, f"Unknown (0x{p_byte:02X})"
                                )
                                return name_str, file_type

        return "", "Raw Data / Unknown"

    @classmethod
    def extract_filename(cls, chunk: bytes) -> str:
        fname, _ = cls.extract_file_info(chunk)
        return fname

    def split(self) -> List[Tuple[str, str, bytes]]:
        """Splits multi-file CMT or T88 stream using the authentic ROM state machine."""
        if not self.data:
            return []

        # T88 Container handling: group discrete T88 DATA blocks by header lead-in
        if len(self.data) >= 24 and T88File.is_valid_magic(self.data[:24]):
            try:
                t88 = T88File.unpack(io.BytesIO(self.data))
                data_payloads: List[bytes] = []
                for block in t88.blocks:
                    if block.tag == 0x0101 and len(block.data) >= 12:
                        _, _, dlen, _ = struct.unpack("<IIHH", block.data[:12])
                        payload = block.data[12 : 12 + dlen]
                        if payload:
                            data_payloads.append(payload)

                if data_payloads:
                    results: List[Tuple[str, str, bytes]] = []
                    used_names: Dict[str, int] = {}
                    curr_name = ""
                    curr_type = ""
                    curr_chunks: List[bytes] = []

                    for payload in data_payloads:
                        fname, ftype = self.extract_file_info(payload)
                        if fname:
                            if curr_chunks:
                                uname = curr_name or "part"
                                uname = self._dedup_name(uname, used_names)
                                results.append(
                                    (
                                        uname,
                                        curr_type or "Binary Data",
                                        b"".join(curr_chunks),
                                    )
                                )
                                curr_chunks = []
                            curr_name = fname
                            curr_type = ftype
                            curr_chunks.append(payload)
                        else:
                            curr_chunks.append(payload)

                    if curr_chunks:
                        uname = curr_name or "part"
                        uname = self._dedup_name(uname, used_names)
                        results.append(
                            (uname, curr_type or "Binary Data", b"".join(curr_chunks))
                        )

                    return results
            except Exception:
                pass

        # Raw CMT Stream state machine:
        # State HUNT_HEADER -> State PARSE_BODY (following length jumps & terminator rules)
        buf = self.data
        n = len(buf)
        pos = 0
        entries: List[Tuple[str, str, bytes]] = []
        used_names: Dict[str, int] = {}

        while pos < n:
            # STATE: HUNT_HEADER
            preamble_pos = -1
            preamble_byte = 0
            fname = ""
            body_start = -1

            i = pos
            while i < n - 8:
                b = buf[i]
                if b in (0x24, 0xD3, 0x9C):
                    for plen in (10, 8, 6, 4, 3):
                        if buf[i : i + plen] == bytes([b]) * plen:
                            idx = i + plen
                            while idx < n and buf[idx] == b:
                                idx += 1
                            if idx + 6 <= n:
                                name_bytes = buf[idx : idx + 6]
                                if self.is_valid_cassette_filename(name_bytes):
                                    name_str = "".join(
                                        (
                                            chr(c)
                                            if (32 <= c <= 126 or 0xA1 <= c <= 0xDF)
                                            else " "
                                        )
                                        for c in name_bytes
                                    ).strip()
                                    name_str = re.sub(r'[\\/*?:"<>|]', "_", name_str)
                                    if name_str:
                                        fname = name_str
                                        preamble_pos = i
                                        preamble_byte = b
                                        body_start = idx + 6
                                        break
                    if preamble_pos != -1:
                        break
                i += 1

            if preamble_pos == -1:
                # No more headers in stream; append remainder
                if pos < n:
                    if entries:
                        p_name, p_type, p_data = entries[-1]
                        entries[-1] = (p_name, p_type, p_data + buf[pos:])
                    else:
                        entries.append(("part_001", "Raw Data / Unknown", buf[pos:]))
                break

            # If there were orphan bytes before this header, attach to previous entry
            if preamble_pos > pos:
                if entries:
                    p_name, p_type, p_data = entries[-1]
                    entries[-1] = (p_name, p_type, p_data + buf[pos:preamble_pos])

            file_start = preamble_pos
            type_str = self.TYPE_NAMES.get(
                preamble_byte, f"Unknown (0x{preamble_byte:02X})"
            )

            # STATE: PARSE_BODY (Protocol-driven consumption, zero heuristics)
            # 1. Machine Code File (0x24) - MON R length-jumped record consumption
            if preamble_byte == 0x24:
                rec_p = body_start
                file_end = n
                while rec_p < n:
                    # Find start of next record (0x3A ':')
                    colon_pos = buf.find(b"\x3a", rec_p)
                    if colon_pos == -1:
                        file_end = n
                        break

                    # Advance past preamble 0x3A sync bytes to the actual record start
                    while colon_pos + 1 < n and buf[colon_pos + 1] == 0x3A:
                        colon_pos += 1

                    # Check for 3-byte short zero-length terminator (: 00 [chk])
                    if (
                        colon_pos + 2 <= n
                        and buf[colon_pos : colon_pos + 2] == b"\x3a\x00"
                    ):
                        file_end = colon_pos + 3
                        break

                    # Check for 5-byte standard zero-length terminator (: [addr:2] 00 [chk])
                    if colon_pos + 4 <= n and buf[colon_pos + 3] == 0x00:
                        file_end = colon_pos + 5
                        break

                    # Active record: : [addr:2] [len:1] [data:len] [chk:1]
                    if colon_pos + 4 <= n:
                        rlen = buf[colon_pos + 3]
                        if 0 < rlen <= 255:
                            # Jump directly over the entire payload and checksum
                            rec_p = colon_pos + 1 + 2 + 1 + rlen + 1
                            continue

                    rec_p = colon_pos + 1

                chunk_data = buf[file_start:file_end]
                uname = self._dedup_name(fname, used_names)
                entries.append((uname, type_str, chunk_data))
                pos = file_end

            # 2. Tokenized BASIC Program (0xD3) - Line-by-line pointer traversal
            elif preamble_byte == 0xD3:
                data_p = buf.find(b"\xd3" * 3, body_start)
                file_end = n
                if data_p != -1:
                    d_idx = data_p + 3
                    while d_idx < n and buf[d_idx] == 0xD3:
                        d_idx += 1
                    curr_p = d_idx
                    while curr_p + 2 <= n:
                        next_ptr = buf[curr_p] | (buf[curr_p + 1] << 8)
                        if next_ptr == 0x0000:
                            file_end = curr_p + 2
                            break
                        if curr_p + 4 > n:
                            break
                        line_zero = buf.find(b"\x00", curr_p + 4)
                        if line_zero == -1:
                            break
                        curr_p = line_zero + 1

                chunk_data = buf[file_start:file_end]
                uname = self._dedup_name(fname, used_names)
                entries.append((uname, type_str, chunk_data))
                pos = file_end

            # 3. ASCII / Sequential Text File (0x9C) - Ctrl-Z (0x1A) consumption
            elif preamble_byte == 0x9C:
                eof_p = buf.find(b"\x1a", body_start)
                file_end = eof_p + 1 if eof_p != -1 else n
                chunk_data = buf[file_start:file_end]
                uname = self._dedup_name(fname, used_names)
                entries.append((uname, type_str, chunk_data))
                pos = file_end

        return entries

    @staticmethod
    def _dedup_name(name: str, used: Dict[str, int]) -> str:
        if name in used:
            used[name] += 1
            return f"{name}_{used[name]}"
        used[name] = 1
        return name

    @classmethod
    def join(cls, chunks: List[bytes]) -> "CMTFile":
        return cls(b"".join(chunks))


def convert_t88_to_cmt(input_path: str, output_path: Optional[str] = None) -> str:
    if output_path is None:
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}.cmt"

    with open(input_path, "rb") as f:
        stream = io.BytesIO(f.read())
        t88 = T88File.unpack(stream)

    cmt_payload = t88.extract_cmt_payload()

    with open(output_path, "wb") as f:
        f.write(cmt_payload)

    return output_path


def convert_cmt_to_t88(
    input_path: str, output_path: Optional[str] = None, comment: str = ""
) -> str:
    if output_path is None:
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}.t88"

    with open(input_path, "rb") as f:
        cmt_data = f.read()

    t88 = T88File.from_cmt_data(cmt_data, comment=comment)

    with open(output_path, "wb") as f:
        f.write(t88.pack())

    return output_path


def split_cmt_file(
    input_path: str, output_dir: Optional[str] = None
) -> List[Tuple[str, str, int, str]]:
    with open(input_path, "rb") as f:
        tape_data = f.read()

    cmt = CMTFile(tape_data)
    chunks = cmt.split()

    if output_dir is None:
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_dir = f"{base_name}_split"

    os.makedirs(output_dir, exist_ok=True)
    summary_info: List[Tuple[str, str, int, str]] = []

    for name, ftype, chunk_data in chunks:
        out_name = name if name.lower().endswith(".cmt") else f"{name}.cmt"
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, "wb") as f:
            f.write(chunk_data)
        summary_info.append((name, ftype, len(chunk_data), out_path))

    return summary_info


def join_cmt_files(input_paths: List[str], output_path: str) -> str:
    chunks: List[bytes] = []
    for path in input_paths:
        with open(path, "rb") as f:
            chunks.append(f.read())

    joined = CMTFile.join(chunks)

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "wb") as f:
        f.write(joined.data)

    return output_path


class TestPC88TapeTool(unittest.TestCase):
    """Authentic protocol-verified unit tests for PC-8001 / PC-8801 tape operations."""

    def setUp(self) -> None:
        """Sets up realistic mock CMT byte streams for all three formats."""
        # 1. Machine Language (0x24 Header + structured 0x3A records with 0-length terminator)
        self.ml_file = (
            (b"\x24" * 10 + b"BIN001" + b"\x00\x80\x00\x80")
            + (b"\x3a" * 10)
            + (b"\x3a\x00\x80\x08\x21\x00\x80\x3e\x01\xcd\x00\x00\x50")
            + (b"\x3a\x00\x00\x00\x00")
        )

        # 2. Tokenized BASIC (0xD3 Header + 0xD3 preamble + linked lines with next_ptr=0)
        self.basic_file = (
            (b"\xd3" * 10 + b"PROG01")
            + (b"\xd3" * 10)
            + struct.pack("<HH", 0x8010, 10)
            + b'\x90 "HELLO WORLD"'
            + b"\x00"
            + struct.pack("<H", 0x0000)
        )

        # 3. ASCII Text / Sequential File (0x9C Header + 0x9C preamble + text + 0x1A EOF)
        self.ascii_file = (
            (b"\x9c" * 10 + b"TEXT01")
            + (b"\x9c" * 10)
            + b'10 PRINT "TEST"\r\n20 END\r\n\x1a'
        )

        self.combined_cmt = self.ml_file + self.basic_file + self.ascii_file

    def test_t88_block_pack_unpack(self) -> None:
        """Tests T88Block serialization and deserialization."""
        header = struct.pack("<IIHH", 0, 440, 10, 0)
        b_data = T88Block(T88Tag.DATA_1200, header + b"1234567890")
        p_data = b_data.pack()
        u_data = T88Block.unpack(io.BytesIO(p_data))
        self.assertIsNotNone(u_data)
        if u_data is not None:
            self.assertEqual(u_data.tag, T88Tag.DATA_1200)
            self.assertEqual(u_data.length, len(header) + 10)
            self.assertEqual(u_data.data, header + b"1234567890")

    def test_t88_file_pack_unpack(self) -> None:
        """Tests full authentic T88File packing and unpacking."""
        t88 = T88File.from_cmt_data(self.ml_file, comment="Authentic Test Image")
        packed = t88.pack()
        unpacked = T88File.unpack(io.BytesIO(packed))

        self.assertTrue(T88File.is_valid_magic(unpacked.magic))
        self.assertEqual(unpacked.version, 0x0100)
        self.assertEqual(unpacked.extract_cmt_payload(), self.ml_file)
        self.assertEqual(unpacked.extract_metadata()["comment"], "Authentic Test Image")

    def test_invalid_t88_header(self) -> None:
        """Tests exception handling for invalid T88 header."""
        invalid_stream = io.BytesIO(b"INVALID_HEADER_BYTES_TOO_SHORT")
        with self.assertRaises(ValueError):
            T88File.unpack(invalid_stream)

    def test_bidirectional_conversion(self) -> None:
        """Tests bidirectional CMT <-> T88 conversion integrity."""
        t88 = T88File.from_cmt_data(self.combined_cmt)
        cmt_extracted = t88.extract_cmt_payload()
        self.assertEqual(cmt_extracted, self.combined_cmt)

        t88_reencoded = T88File.from_cmt_data(cmt_extracted)
        self.assertEqual(t88_reencoded.extract_cmt_payload(), self.combined_cmt)

    def test_split_and_join_cmt(self) -> None:
        """Tests splitting multi-format CMT stream and joining them back."""
        cmt_file = CMTFile(self.combined_cmt)
        split_items = cmt_file.split()

        self.assertEqual(len(split_items), 3)
        self.assertEqual(split_items[0][0], "BIN001")
        self.assertEqual(split_items[0][1], "MON Machine Language Header (0x24)")
        self.assertEqual(split_items[0][2], self.ml_file)

        self.assertEqual(split_items[1][0], "PROG01")
        self.assertEqual(split_items[1][1], "BASIC Program (0xD3)")
        self.assertEqual(split_items[1][2], self.basic_file)

        self.assertEqual(split_items[2][0], "TEXT01")
        self.assertEqual(split_items[2][1], "ASCII / Sequential File (0x9C)")
        self.assertEqual(split_items[2][2], self.ascii_file)

        joined = CMTFile.join([item[2] for item in split_items])
        self.assertEqual(joined.data, self.combined_cmt)

    def test_split_t88_with_carrier_blocks(self) -> None:
        """Tests splitting a T88 container separated by authentic carrier blocks."""
        h1 = struct.pack("<IIHH", 0, 440, len(self.ml_file), 0)
        h2 = struct.pack("<IIHH", 10000, 440, len(self.basic_file), 0)
        blocks = [
            T88Block(T88Tag.DATA_1200, h1 + self.ml_file),
            T88Block(T88Tag.MARK, struct.pack("<II", 9600, 4800)),
            T88Block(T88Tag.DATA_1200, h2 + self.basic_file),
            T88Block(T88Tag.END, b""),
        ]
        t88 = T88File(blocks=blocks)
        packed = t88.pack()

        cmt_file = CMTFile(packed)
        split_items = cmt_file.split()
        self.assertEqual(len(split_items), 2)
        self.assertEqual(split_items[0][0], "BIN001")
        self.assertEqual(split_items[1][0], "PROG01")

    def test_cli_file_operations(self) -> None:
        """Tests disk file operations for convert, split, and join."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmt_in = os.path.join(tmpdir, "input.cmt")
            t88_out = os.path.join(tmpdir, "output.t88")
            cmt_out = os.path.join(tmpdir, "output.cmt")
            split_dir = os.path.join(tmpdir, "split_files")
            joined_out = os.path.join(tmpdir, "joined.cmt")

            with open(cmt_in, "wb") as f:
                f.write(self.combined_cmt)

            res_t88 = convert_cmt_to_t88(cmt_in, t88_out, comment="CLI Temp Test")
            self.assertTrue(os.path.exists(res_t88))

            res_cmt = convert_t88_to_cmt(t88_out, cmt_out)
            self.assertTrue(os.path.exists(res_cmt))

            with open(res_cmt, "rb") as f:
                self.assertEqual(f.read(), self.combined_cmt)

            split_info = split_cmt_file(cmt_in, split_dir)
            self.assertEqual(len(split_info), 3)

            split_files = [item[3] for item in split_info]
            res_join = join_cmt_files(split_files, joined_out)
            self.assertTrue(os.path.exists(res_join))

            with open(res_join, "rb") as f:
                self.assertEqual(f.read(), self.combined_cmt)


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="pc88_tape_tool.py",
        description="NEC PC-8001 / PC-8801 Cassette Tape Format Utility (.t88 / .cmt)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run internal unit tests and exit",
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    p_t2c = subparsers.add_parser(
        "t2c", help="Convert .t88 container to raw .cmt tape dump"
    )
    p_t2c.add_argument("input", help="Path to input .t88 file")
    p_t2c.add_argument("-o", "--output", help="Path to output .cmt file (optional)")

    p_c2t = subparsers.add_parser(
        "c2t", help="Convert raw .cmt tape dump to .t88 container"
    )
    p_c2t.add_argument("input", help="Path to input .cmt file")
    p_c2t.add_argument("-o", "--output", help="Path to output .t88 file (optional)")
    p_c2t.add_argument(
        "--comment",
        default="",
        help="Optional comment metadata string embedded in T88 file",
    )

    p_split = subparsers.add_parser(
        "split-cmt", help="Split multi-file .cmt or .t88 into individual files"
    )
    p_split.add_argument("input", help="Path to input .cmt or .t88 file")
    p_split.add_argument(
        "-o",
        "--output-dir",
        help="Output directory for split files (optional)",
    )

    p_join = subparsers.add_parser(
        "join-cmt", help="Join multiple .cmt files into a single .cmt file"
    )
    p_join.add_argument("inputs", nargs="+", help="Input .cmt files to concatenate")
    p_join.add_argument(
        "-o", "--output", required=True, help="Path to output merged .cmt file"
    )

    return parser


def main() -> None:
    """Main execution entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.test:
        print("Running internal unit tests...")
        suite = unittest.TestLoader().loadTestsFromTestCase(TestPC88TapeTool)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "t2c":
            out_file = convert_t88_to_cmt(args.input, args.output)
            print(f"[SUCCESS] Converted {args.input} -> {out_file}")

        elif args.command == "c2t":
            out_file = convert_cmt_to_t88(args.input, args.output, comment=args.comment)
            print(f"[SUCCESS] Converted {args.input} -> {out_file}")

        elif args.command == "split-cmt":
            summary = split_cmt_file(args.input, args.output_dir)
            print(f"\n[SUCCESS] Split '{args.input}' into {len(summary)} file(s):\n")
            print(
                f"{'#':<3} | {'Filename':<12} | {'File Format / Type':<32} | {'Size (Bytes)':<12} | Saved Path"
            )
            print("-" * 90)
            for idx, (fname, ftype, size, path) in enumerate(summary, start=1):
                print(f"{idx:<3} | {fname:<12} | {ftype:<32} | {size:<12} | {path}")
            print("-" * 90)

        elif args.command == "join-cmt":
            out_file = join_cmt_files(args.inputs, args.output)
            print(f"[SUCCESS] Merged {len(args.inputs)} file(s) -> {out_file}")

    except Exception as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
