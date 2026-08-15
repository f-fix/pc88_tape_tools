#!/usr/bin/env python3
"""NEC PC-8001 / PC-8801 Cassette Tape Format Utility (`pc88_tape_tools.py`).

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

Developed using LLM coding assistance.
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

        ticks_per_byte = int(round(44 * 1200 / baud)) if baud > 0 else 44
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

    # Canonical CSAVE/monitor sync-tone length.
    CANONICAL_SYNC_LEN: int = 8

    def __init__(self, data: bytes = b"") -> None:
        self.data: bytes = data

    @classmethod
    def is_valid_cassette_filename(
        cls, name_bytes: bytes, allow_null: bool = False
    ) -> bool:
        """Verifies if a 6-byte sequence forms a valid space-padded PC-8001/PC-8801 cassette filename."""
        if len(name_bytes) != 6:
            return False
        ok_byte = (
            lambda b: (32 <= b <= 126)
            or (0xA1 <= b <= 0xDF)
            or (allow_null and b == 0x00)
        )
        valid_chars = sum(1 for b in name_bytes if ok_byte(b))
        if valid_chars == 6:
            non_spaces = [b for b in name_bytes if b not in (0x20, 0x00)]
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
                        allow_null = idx >= cls.CANONICAL_SYNC_LEN
                        if cls.is_valid_cassette_filename(
                            name_bytes, allow_null=allow_null
                        ):
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

                    # If multiple files were identified from T88 blocks, return them directly
                    if len(results) > 1 or (
                        results and not self.extract_file_info(data_payloads[0])[0]
                    ):
                        return results
            except Exception:
                pass

        # Raw CMT Stream state machine:
        buf = self.data
        if len(buf) >= 24 and T88File.is_valid_magic(buf[:24]):
            try:
                t88_obj = T88File.unpack(io.BytesIO(buf))
                buf = t88_obj.extract_cmt_payload()
            except Exception:
                pass

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
                                allow_null = (idx - i) >= self.CANONICAL_SYNC_LEN
                                if self.is_valid_cassette_filename(
                                    name_bytes, allow_null=allow_null
                                ):
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
                # No more headers in stream; append remainder to the last file
                if pos < n:
                    if entries:
                        p_name, p_type, p_data = entries[-1]
                        entries[-1] = (p_name, p_type, p_data + buf[pos:])
                    else:
                        entries.append(("part_001", "Raw Data / Unknown", buf[pos:]))
                break

            # If there were bytes before this header, attach to previous entry without loss
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
                    colon_pos = buf.find(b"\x3a", rec_p)
                    if colon_pos == -1:
                        file_end = n
                        break

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
                            rec_p = colon_pos + 1 + 2 + 1 + rlen + 1
                            continue

                    rec_p = colon_pos + 1

                chunk_data = buf[file_start:file_end]
                uname = self._dedup_name(fname, used_names)
                entries.append((uname, type_str, chunk_data))
                pos = file_end

            # 2. Tokenized BASIC Program (0xD3) - Line-by-line pointer traversal
            elif preamble_byte == 0xD3:
                file_end = n
                curr_p = body_start

                if curr_p + 3 <= n and buf[curr_p : curr_p + 3] == b"\xd3" * 3:
                    while curr_p < n and buf[curr_p] == 0xD3:
                        curr_p += 1

                line_count = 0
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
                    line_count += 1

                if file_end == n and line_count == 0:
                    data_p = buf.find(b"\xd3" * 3, body_start)
                    if data_p != -1:
                        curr_p = data_p + 3
                        while curr_p < n and buf[curr_p] == 0xD3:
                            curr_p += 1
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


def _extract_payload_or_raw(data: bytes) -> bytes:
    """Helper to extract CMT payload if data is T88 format, or return raw data."""
    if len(data) >= 24 and T88File.is_valid_magic(data[:24]):
        try:
            t88 = T88File.unpack(io.BytesIO(data))
            return t88.extract_cmt_payload()
        except Exception:
            return data
    return data


def convert_t88_to_cmt(input_path: str, output_path: Optional[str] = None) -> str:
    """Converts .t88 container to raw .cmt tape dump with 100% byte-exact payload preservation."""
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
    input_path: str,
    output_path: Optional[str] = None,
    comment: str = "",
    baud: int = 1200,
) -> str:
    """Converts raw .cmt tape dump to .t88 container format."""
    if output_path is None:
        base, _ = os.path.splitext(input_path)
        output_path = f"{base}.t88"

    with open(input_path, "rb") as f:
        cmt_data = f.read()

    t88 = T88File.from_cmt_data(cmt_data, comment=comment, baud=baud)

    with open(output_path, "wb") as f:
        f.write(t88.pack())

    return output_path


def split_cmt_file(
    input_path: str, output_dir: Optional[str] = None
) -> List[Tuple[str, str, int, str]]:
    """Splits multi-file .cmt or .t88 into individual numbered .cmt files."""
    with open(input_path, "rb") as f:
        tape_data = f.read()

    cmt = CMTFile(tape_data)
    chunks = cmt.split()

    if output_dir is None:
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_dir = f"{base_name}_split"

    os.makedirs(output_dir, exist_ok=True)
    summary_info: List[Tuple[str, str, int, str]] = []

    for idx, (name, ftype, chunk_data) in enumerate(chunks, start=1):
        clean_name = name[:-4] if name.lower().endswith(".cmt") else name
        out_name = f"{idx:02d}_{clean_name}.cmt"
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, "wb") as f:
            f.write(chunk_data)
        summary_info.append((name, ftype, len(chunk_data), out_path))

    return summary_info


def split_t88_file(
    input_path: str,
    output_dir: Optional[str] = None,
    comment: str = "",
    baud: Optional[int] = None,
    default_baud: int = 1200,
    cmt_baud: Optional[int] = None,
) -> List[Tuple[str, str, int, str]]:
    """Splits multi-file .cmt or .t88 into individual numbered .t88 files.

    When input is a .t88 container, preserves original carrier tags (MARK/GAP/SPACE)
    and per-block baud/timing parameters unless overridden by the `baud` argument.
    """
    with open(input_path, "rb") as f:
        raw_data = f.read()

    if output_dir is None:
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_dir = f"{base_name}_split"

    os.makedirs(output_dir, exist_ok=True)
    summary_info: List[Tuple[str, str, int, str]] = []

    if cmt_baud is not None:
        default_baud = cmt_baud

    if len(raw_data) >= 24 and T88File.is_valid_magic(raw_data[:24]):
        try:
            t88 = T88File.unpack(io.BytesIO(raw_data))
            file_sections: List[Tuple[str, str, List[T88Block]]] = []
            pending_carriers: List[T88Block] = []
            curr_blocks: List[T88Block] = []
            curr_name = ""
            curr_type = ""
            used_names: Dict[str, int] = {}

            for block in t88.blocks:
                if block.tag in (T88Tag.VERSION, T88Tag.END):
                    continue
                elif block.tag == T88Tag.COMMENT:
                    if not comment:
                        curr_blocks.append(block)
                elif block.tag in (T88Tag.MARK, T88Tag.GAP, T88Tag.SPACE):
                    pending_carriers.append(block)
                elif block.tag == 0x0101:  # DATA block
                    payload = b""
                    if len(block.data) >= 12:
                        _, _, dlen, _ = struct.unpack("<IIHH", block.data[:12])
                        payload = block.data[12 : 12 + dlen]
                    else:
                        payload = block.data

                    fname, ftype = CMTFile.extract_file_info(payload)
                    if fname:
                        if curr_blocks:
                            uname = CMTFile._dedup_name(curr_name or "part", used_names)
                            file_sections.append(
                                (uname, curr_type or "Binary Data", curr_blocks)
                            )
                            curr_blocks = []
                        curr_name = fname
                        curr_type = ftype
                        curr_blocks.extend(pending_carriers)
                        pending_carriers = []
                        curr_blocks.append(block)
                    else:
                        curr_blocks.extend(pending_carriers)
                        pending_carriers = []
                        curr_blocks.append(block)

            if curr_blocks:
                curr_blocks.extend(pending_carriers)
                pending_carriers = []
                uname = CMTFile._dedup_name(curr_name or "part", used_names)
                file_sections.append((uname, curr_type or "Binary Data", curr_blocks))

            # If discrete T88 block sequences exist for each file, output them directly
            if len(file_sections) > 1 or (
                file_sections and len(CMTFile(t88.extract_cmt_payload()).split()) <= 1
            ):
                for idx, (fname, ftype, blocks) in enumerate(file_sections, start=1):
                    new_blocks: List[T88Block] = []
                    new_blocks.append(
                        T88Block(T88Tag.VERSION, struct.pack("<H", 0x0100))
                    )
                    if comment:
                        new_blocks.append(
                            T88Block(
                                T88Tag.COMMENT,
                                comment.encode("utf-8", errors="ignore"),
                            )
                        )

                    timing_blocks = [
                        b
                        for b in blocks
                        if b.tag in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK, 0x0101)
                    ]
                    min_tick = 0
                    for tb in timing_blocks:
                        if len(tb.data) >= 8:
                            st, _ = struct.unpack("<II", tb.data[:8])
                            min_tick = st
                            break

                    curr_tick = 0
                    for b in blocks:
                        if b.tag in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK):
                            if len(b.data) >= 8:
                                st, lt = struct.unpack("<II", b.data[:8])
                                if baud is None:
                                    new_st = max(0, st - min_tick)
                                else:
                                    new_st = curr_tick
                                    curr_tick += lt
                                new_b_data = struct.pack("<II", new_st, lt) + b.data[8:]
                                new_blocks.append(T88Block(b.tag, new_b_data))
                            else:
                                new_blocks.append(T88Block(b.tag, b.data))
                        elif b.tag == 0x0101:  # DATA block
                            if len(b.data) >= 12:
                                st, lt, dlen, res = struct.unpack("<IIHH", b.data[:12])
                                payload = b.data[12 : 12 + dlen]
                                if baud is None:
                                    new_st = max(0, st - min_tick)
                                    new_lt = lt
                                else:
                                    ticks_per_byte = (
                                        int(round(44 * 1200 / baud)) if baud > 0 else 44
                                    )
                                    new_lt = dlen * ticks_per_byte
                                    new_st = curr_tick
                                    curr_tick += new_lt
                                new_b_data = (
                                    struct.pack("<IIHH", new_st, new_lt, dlen, res)
                                    + payload
                                )
                                new_blocks.append(T88Block(b.tag, new_b_data))
                            else:
                                new_blocks.append(T88Block(b.tag, b.data))
                        else:
                            new_blocks.append(T88Block(b.tag, b.data))

                    new_blocks.append(T88Block(T88Tag.END, b""))
                    split_t88 = T88File(
                        magic=t88.magic, version=t88.version, blocks=new_blocks
                    )
                    t88_bytes = split_t88.pack()

                    clean_name = fname[:-4] if fname.lower().endswith(".t88") else fname
                    out_name = f"{idx:02d}_{clean_name}.t88"
                    out_path = os.path.join(output_dir, out_name)
                    with open(out_path, "wb") as out_f:
                        out_f.write(t88_bytes)
                    summary_info.append((fname, ftype, len(t88_bytes), out_path))

                return summary_info
        except Exception:
            pass

    # Stream-level split for raw CMT or multi-file single-DATA-block containers
    raw_cmt = _extract_payload_or_raw(raw_data)
    cmt = CMTFile(raw_cmt)
    chunks = cmt.split()
    effective_baud = baud if baud is not None else default_baud

    for idx, (name, ftype, chunk_data) in enumerate(chunks, start=1):
        t88_obj = T88File.from_cmt_data(
            chunk_data, comment=comment, baud=effective_baud
        )
        t88_bytes = t88_obj.pack()
        clean_name = name[:-4] if name.lower().endswith(".t88") else name
        out_name = f"{idx:02d}_{clean_name}.t88"
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, "wb") as out_f:
            out_f.write(t88_bytes)
        summary_info.append((name, ftype, len(t88_bytes), out_path))

    return summary_info


def join_cmt_files(input_paths: List[str], output_path: str) -> str:
    """Merges multiple files into a single concatenated .cmt file."""
    chunks: List[bytes] = []
    for path in input_paths:
        with open(path, "rb") as f:
            data = f.read()
        chunks.append(_extract_payload_or_raw(data))

    joined = CMTFile.join(chunks)

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "wb") as f:
        f.write(joined.data)

    return output_path


def join_t88_files(
    input_paths: List[str],
    output_path: str,
    comment: str = "",
    baud: Optional[int] = None,
    default_baud: int = 1200,
    cmt_baud: Optional[int] = None,
) -> str:
    """Merges multiple files (.t88 or .cmt) into a consolidated .t88 container."""
    combined_blocks: List[T88Block] = []
    combined_blocks.append(T88Block(T88Tag.VERSION, struct.pack("<H", 0x0100)))

    if comment:
        combined_blocks.append(
            T88Block(T88Tag.COMMENT, comment.encode("utf-8", errors="ignore"))
        )

    if cmt_baud is not None:
        default_baud = cmt_baud

    current_tick = 0

    for path in input_paths:
        with open(path, "rb") as f:
            data = f.read()

        is_t88 = len(data) >= 24 and T88File.is_valid_magic(data[:24])

        if is_t88:
            try:
                t88 = T88File.unpack(io.BytesIO(data))
                file_blocks = [
                    b for b in t88.blocks if b.tag not in (T88Tag.VERSION, T88Tag.END)
                ]

                min_tick = 0
                for b in file_blocks:
                    if b.tag in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK, 0x0101):
                        if len(b.data) >= 8:
                            st, _ = struct.unpack("<II", b.data[:8])
                            min_tick = st
                            break

                file_start_tick = current_tick
                file_max_end = current_tick

                for b in file_blocks:
                    if b.tag in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK):
                        if len(b.data) >= 8:
                            st, lt = struct.unpack("<II", b.data[:8])
                            if baud is None:
                                new_st = file_start_tick + max(0, st - min_tick)
                                file_max_end = max(file_max_end, new_st + lt)
                            else:
                                new_st = current_tick
                                current_tick += lt
                                file_max_end = current_tick
                            new_b_data = struct.pack("<II", new_st, lt) + b.data[8:]
                            combined_blocks.append(T88Block(b.tag, new_b_data))
                        else:
                            combined_blocks.append(T88Block(b.tag, b.data))

                    elif b.tag == 0x0101:  # DATA block
                        if len(b.data) >= 12:
                            st, lt, dlen, res = struct.unpack("<IIHH", b.data[:12])
                            payload = b.data[12 : 12 + dlen]
                            if baud is None:
                                new_st = file_start_tick + max(0, st - min_tick)
                                new_lt = lt
                                file_max_end = max(file_max_end, new_st + new_lt)
                            else:
                                ticks_per_byte = (
                                    int(round(44 * 1200 / baud)) if baud > 0 else 44
                                )
                                new_lt = dlen * ticks_per_byte
                                new_st = current_tick
                                current_tick += new_lt
                                file_max_end = current_tick
                            new_b_data = (
                                struct.pack("<IIHH", new_st, new_lt, dlen, res)
                                + payload
                            )
                            combined_blocks.append(T88Block(b.tag, new_b_data))
                        else:
                            combined_blocks.append(T88Block(b.tag, b.data))

                    elif b.tag == T88Tag.COMMENT:
                        if not comment:
                            combined_blocks.append(T88Block(b.tag, b.data))
                    else:
                        combined_blocks.append(T88Block(b.tag, b.data))

                current_tick = file_max_end
                continue
            except Exception:
                pass

        # Input is raw CMT data
        effective_cmt_baud = baud if baud is not None else default_baud
        ticks_per_byte = (
            int(round(44 * 1200 / effective_cmt_baud)) if effective_cmt_baud > 0 else 44
        )
        mark_len = 9600
        combined_blocks.append(
            T88Block(T88Tag.MARK, struct.pack("<II", current_tick, mark_len))
        )
        current_tick += mark_len

        data_len = len(data)
        data_ticks = data_len * ticks_per_byte
        data_header = struct.pack("<IIHH", current_tick, data_ticks, data_len, 0x0000)
        combined_blocks.append(T88Block(0x0101, data_header + data))
        current_tick += data_ticks

    combined_blocks.append(T88Block(T88Tag.END, b""))

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    joined_t88 = T88File(blocks=combined_blocks)
    with open(output_path, "wb") as f:
        f.write(joined_t88.pack())

    return output_path


class TestPC88TapeTool(unittest.TestCase):
    """Authentic protocol-verified unit tests for PC-8001 / PC-8801 tape operations."""

    def setUp(self) -> None:
        """Sets up realistic mock CMT byte streams for all formats."""
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

        # 4. Multi-ML tape: Three consecutive MON R files
        self.ml_file_2 = (
            (b"\x24" * 10 + b"BIN002" + b"\x00\x90\x00\x90")
            + (b"\x3a" * 10)
            + (b"\x3a\x00\x90\x04\x3e\x01\xcd\x00\x50")
            + (b"\x3a\x00\x00\x00\x00")
        )
        self.ml_file_3 = (
            (b"\x24" * 10 + b"BIN003" + b"\x00\xa0\x00\xa0")
            + (b"\x3a" * 10)
            + (b"\x3a\x00\xa0\x04\x3e\x02\xcd\x00\x50")
            + (b"\x3a\x00\x00\x00\x00")
        )
        self.three_ml_cmt = self.ml_file + self.ml_file_2 + self.ml_file_3

        # 5. Null-padded CSAVE Program (short name, zero-padded instead of space-padded)
        self.null_padded_basic_file = (
            (b"\xd3" * 10 + b"DOOR\x00\x00")
            + struct.pack("<HH", 0x8010, 10)
            + b'\x90 "DOOR"'
            + b"\x00"
            + struct.pack("<H", 0x0000)
        )

        # 6. Short (non-canonical) sync run immediately followed by bytes that coincidentally resemble a null-padded name
        self.coincidental_short_run = (
            b"\x00" * 4 + b"\x24\x24\x24" + b"DTTT\x00\x00" + b"\x00" * 4
        )

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

    def test_baud_rate_override(self) -> None:
        """Tests overriding baud rate when creating T88 containers."""
        t88_1200 = T88File.from_cmt_data(self.ml_file, baud=1200)
        t88_300 = T88File.from_cmt_data(self.ml_file, baud=300)

        data_block_1200 = [b for b in t88_1200.blocks if b.tag == 0x0101][0]
        data_block_300 = [b for b in t88_300.blocks if b.tag == 0x0101][0]

        _, ticks_1200, dlen_1200, _ = struct.unpack("<IIHH", data_block_1200.data[:12])
        _, ticks_300, dlen_300, _ = struct.unpack("<IIHH", data_block_300.data[:12])

        self.assertEqual(dlen_1200, len(self.ml_file))
        self.assertEqual(dlen_300, len(self.ml_file))
        self.assertEqual(ticks_1200, len(self.ml_file) * 44)
        self.assertEqual(ticks_300, len(self.ml_file) * 176)

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

    def test_three_consecutive_mon_r_files(self) -> None:
        """Tests that a tape with three consecutive MON R files splits completely without overshooting."""
        cmt_file = CMTFile(self.three_ml_cmt)
        split_items = cmt_file.split()

        self.assertEqual(len(split_items), 3)
        self.assertEqual(split_items[0][0], "BIN001")
        self.assertEqual(split_items[0][2], self.ml_file)

        self.assertEqual(split_items[1][0], "BIN002")
        self.assertEqual(split_items[1][2], self.ml_file_2)

        self.assertEqual(split_items[2][0], "BIN003")
        self.assertEqual(split_items[2][2], self.ml_file_3)

    def test_null_padded_filename_after_canonical_sync(self) -> None:
        """Null-padded name IS recognized when preceded by a full sync run."""
        cmt_file = CMTFile(self.null_padded_basic_file)
        split_items = cmt_file.split()
        self.assertEqual(len(split_items), 1)
        self.assertEqual(split_items[0][0], "DOOR")
        self.assertEqual(split_items[0][1], "BASIC Program (0xD3)")
        self.assertEqual(split_items[0][2], self.null_padded_basic_file)

    def test_no_false_positive_on_short_run_with_nulls(self) -> None:
        """Null-padded-looking bytes after a short/non-canonical sync run are not treated as header."""
        cmt_file = CMTFile(self.coincidental_short_run)
        split_items = cmt_file.split()
        self.assertEqual(len(split_items), 1)
        self.assertNotEqual(split_items[0][0], "DTTT")

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

    def test_t88_to_t88_split_and_join_preserves_timing_and_carrier_blocks(
        self,
    ) -> None:
        """Tests that T88 -> T88 split and join preserves original timing and carrier blocks without CMT loss."""
        h1 = struct.pack("<IIHH", 9600, len(self.ml_file) * 44, len(self.ml_file), 0)
        h2 = struct.pack(
            "<IIHH", 25000, len(self.basic_file) * 176, len(self.basic_file), 0
        )  # 300 baud
        blocks = [
            T88Block(T88Tag.VERSION, struct.pack("<H", 0x0100)),
            T88Block(T88Tag.MARK, struct.pack("<II", 0, 9600)),
            T88Block(T88Tag.DATA_1200, h1 + self.ml_file),
            T88Block(T88Tag.MARK, struct.pack("<II", 20000, 5000)),
            T88Block(T88Tag.DATA_300, h2 + self.basic_file),
            T88Block(T88Tag.END, b""),
        ]
        t88_orig = T88File(blocks=blocks)

        with tempfile.TemporaryDirectory() as tmpdir:
            t88_in = os.path.join(tmpdir, "input.t88")
            split_dir = os.path.join(tmpdir, "split_t88")
            rejoined_out = os.path.join(tmpdir, "rejoined.t88")

            with open(t88_in, "wb") as f:
                f.write(t88_orig.pack())

            split_info = split_t88_file(t88_in, split_dir, baud=None)
            self.assertEqual(len(split_info), 2)
            self.assertEqual(os.path.basename(split_info[0][3]), "01_BIN001.t88")
            self.assertEqual(os.path.basename(split_info[1][3]), "02_PROG01.t88")

            with open(split_info[0][3], "rb") as f:
                t88_part1 = T88File.unpack(io.BytesIO(f.read()))
            with open(split_info[1][3], "rb") as f:
                t88_part2 = T88File.unpack(io.BytesIO(f.read()))

            self.assertTrue(any(b.tag == T88Tag.MARK for b in t88_part1.blocks))
            self.assertTrue(any(b.tag == T88Tag.MARK for b in t88_part2.blocks))

            dblock1 = [b for b in t88_part1.blocks if b.tag == 0x0101][0]
            dblock2 = [b for b in t88_part2.blocks if b.tag == 0x0101][0]
            _, ticks1, dlen1, _ = struct.unpack("<IIHH", dblock1.data[:12])
            _, ticks2, dlen2, _ = struct.unpack("<IIHH", dblock2.data[:12])
            self.assertEqual(ticks1, dlen1 * 44)
            self.assertEqual(ticks2, dlen2 * 176)

            res_join = join_t88_files(
                [split_info[0][3], split_info[1][3]], rejoined_out, baud=None
            )
            with open(res_join, "rb") as f:
                t88_rejoined = T88File.unpack(io.BytesIO(f.read()))

            self.assertEqual(
                t88_rejoined.extract_cmt_payload(), t88_orig.extract_cmt_payload()
            )

    def test_join_t88_mixed_inputs_with_cmt_baud(self) -> None:
        """Tests that join-t88 preserves T88 timings while applying cmt_baud specifically to CMT inputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            t88_300 = T88File.from_cmt_data(self.basic_file, baud=300)
            p_t88 = os.path.join(tmpdir, "part1.t88")
            with open(p_t88, "wb") as f:
                f.write(t88_300.pack())

            p_cmt = os.path.join(tmpdir, "part2.cmt")
            with open(p_cmt, "wb") as f:
                f.write(self.ml_file)

            p_joined = os.path.join(tmpdir, "joined_mixed.t88")
            join_t88_files([p_t88, p_cmt], p_joined, baud=None, cmt_baud=600)

            with open(p_joined, "rb") as f:
                joined_t88 = T88File.unpack(io.BytesIO(f.read()))

            data_blocks = [b for b in joined_t88.blocks if b.tag == 0x0101]
            self.assertEqual(len(data_blocks), 2)

            _, ticks1, dlen1, _ = struct.unpack("<IIHH", data_blocks[0].data[:12])
            self.assertEqual(ticks1, dlen1 * 176)

            _, ticks2, dlen2, _ = struct.unpack("<IIHH", data_blocks[1].data[:12])
            self.assertEqual(ticks2, dlen2 * 88)

    def test_t88_to_t88_split_and_join_with_baud_override(self) -> None:
        """Tests that T88 -> T88 split and join recalculates timing when baud is overridden."""
        with tempfile.TemporaryDirectory() as tmpdir:
            t88_in = os.path.join(tmpdir, "input.t88")
            split_dir = os.path.join(tmpdir, "split_t88")
            rejoined_out = os.path.join(tmpdir, "rejoined.t88")

            t88_orig = T88File.from_cmt_data(self.combined_cmt, baud=1200)
            with open(t88_in, "wb") as f:
                f.write(t88_orig.pack())

            split_info = split_t88_file(t88_in, split_dir, baud=300)
            for _, _, _, out_path in split_info:
                with open(out_path, "rb") as f:
                    part = T88File.unpack(io.BytesIO(f.read()))
                dblock = [b for b in part.blocks if b.tag == 0x0101][0]
                _, ticks, dlen, _ = struct.unpack("<IIHH", dblock.data[:12])
                self.assertEqual(ticks, dlen * 176)

            split_files = [item[3] for item in split_info]
            res_join = join_t88_files(split_files, rejoined_out, baud=1200)
            with open(res_join, "rb") as f:
                rejoined = T88File.unpack(io.BytesIO(f.read()))

            for b in [b for b in rejoined.blocks if b.tag == 0x0101]:
                _, ticks, dlen, _ = struct.unpack("<IIHH", b.data[:12])
                self.assertEqual(ticks, dlen * 44)

    def test_help_all_formatting(self) -> None:
        """Tests that format_all_help produces valid combined help for all subcommands."""
        parser = build_arg_parser()
        all_help = format_all_help(parser)
        self.assertIn("DETAILED SUBCOMMAND HELP", all_help)
        self.assertIn("Subcommand: t2c", all_help)
        self.assertIn("Subcommand: c2t", all_help)
        self.assertIn("Subcommand: split-cmt", all_help)
        self.assertIn("Subcommand: split-t88", all_help)
        self.assertIn("Subcommand: join-cmt", all_help)
        self.assertIn("Subcommand: join-t88", all_help)

    def test_cli_file_operations(self) -> None:
        """Tests disk file operations for convert, split, and join (both CMT and T88)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmt_in = os.path.join(tmpdir, "input.cmt")
            t88_out = os.path.join(tmpdir, "output.t88")
            cmt_out = os.path.join(tmpdir, "output.cmt")
            split_dir = os.path.join(tmpdir, "split_files")
            split_t88_dir = os.path.join(tmpdir, "split_t88_files")
            joined_out = os.path.join(tmpdir, "joined.cmt")
            joined_t88_out = os.path.join(tmpdir, "joined.t88")

            with open(cmt_in, "wb") as f:
                f.write(self.combined_cmt)

            res_t88 = convert_cmt_to_t88(
                cmt_in, t88_out, comment="CLI Temp Test", baud=1200
            )
            self.assertTrue(os.path.exists(res_t88))

            res_cmt = convert_t88_to_cmt(t88_out, cmt_out)
            self.assertTrue(os.path.exists(res_cmt))

            with open(res_cmt, "rb") as f:
                self.assertEqual(f.read(), self.combined_cmt)

            split_info = split_cmt_file(cmt_in, split_dir)
            self.assertEqual(len(split_info), 3)
            self.assertEqual(os.path.basename(split_info[0][3]), "01_BIN001.cmt")
            self.assertEqual(os.path.basename(split_info[1][3]), "02_PROG01.cmt")
            self.assertEqual(os.path.basename(split_info[2][3]), "03_TEXT01.cmt")

            split_files = [item[3] for item in split_info]
            res_join = join_cmt_files(split_files, joined_out)
            self.assertTrue(os.path.exists(res_join))

            with open(res_join, "rb") as f:
                self.assertEqual(f.read(), self.combined_cmt)

            split_t88_info = split_t88_file(cmt_in, split_t88_dir, baud=1200)
            self.assertEqual(len(split_t88_info), 3)
            self.assertEqual(os.path.basename(split_t88_info[0][3]), "01_BIN001.t88")
            self.assertEqual(os.path.basename(split_t88_info[1][3]), "02_PROG01.t88")
            self.assertEqual(os.path.basename(split_t88_info[2][3]), "03_TEXT01.t88")

            split_t88_files = [item[3] for item in split_t88_info]
            res_t88_join = join_t88_files(split_t88_files, joined_t88_out, baud=1200)
            self.assertTrue(os.path.exists(res_t88_join))

            with open(res_t88_join, "rb") as f:
                unpacked_join = T88File.unpack(io.BytesIO(f.read()))
                self.assertEqual(unpacked_join.extract_cmt_payload(), self.combined_cmt)


def format_all_help(parser: argparse.ArgumentParser) -> str:
    """Formats full help output for the top-level parser and all subparsers."""
    out = io.StringIO()
    parser.print_help(out)
    out.write("\n\n" + "=" * 80 + "\n")
    out.write("DETAILED SUBCOMMAND HELP\n")
    out.write("=" * 80 + "\n")

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice, subparser in action.choices.items():
                out.write(f"\n--- Subcommand: {choice} ---\n")
                sub_out = io.StringIO()
                subparser.print_help(sub_out)
                out.write(sub_out.getvalue().strip() + "\n")
    return out.getvalue()


def build_arg_parser() -> argparse.ArgumentParser:
    """Builds the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="pc88_tape_tools.py",
        description="NEC PC-8001 / PC-8801 Cassette Tape Format Utility (.t88 / .cmt)",
        epilog="Tip: Run '%(prog)s <subcommand> --help' (e.g. 'pc88_tape_tools.py split-t88 --help') "
        "or '%(prog)s --help-all' to view detailed options for all subcommands at once.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run internal unit tests and exit",
    )
    parser.add_argument(
        "--help-all",
        action="store_true",
        help="Show full detailed help for all subcommands at once and exit",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="Available Subcommands",
        metavar="<command>",
    )

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
    p_c2t.add_argument(
        "-b",
        "--baud",
        type=int,
        default=1200,
        help="Baud rate for output T88 file (default: 1200)",
    )

    p_split_cmt = subparsers.add_parser(
        "split-cmt", help="Split multi-file .cmt or .t88 into individual .cmt files"
    )
    p_split_cmt.add_argument("input", help="Path to input .cmt or .t88 file")
    p_split_cmt.add_argument(
        "-o",
        "--output-dir",
        help="Output directory for split files (optional)",
    )

    p_split_t88 = subparsers.add_parser(
        "split-t88", help="Split multi-file .cmt or .t88 into individual .t88 files"
    )
    p_split_t88.add_argument("input", help="Path to input .cmt or .t88 file")
    p_split_t88.add_argument(
        "-o",
        "--output-dir",
        help="Output directory for split files (optional)",
    )
    p_split_t88.add_argument(
        "-b",
        "--baud",
        type=int,
        default=None,
        help="Override baud rate for output .t88 files (preserves original timing by default for .t88)",
    )
    p_split_t88.add_argument(
        "--cmt-baud",
        "--default-baud",
        dest="cmt_baud",
        type=int,
        default=1200,
        help="Default baud rate when input is a raw .cmt file (default: 1200)",
    )
    p_split_t88.add_argument(
        "--comment",
        default="",
        help="Optional comment metadata string embedded in T88 files",
    )

    p_join_cmt = subparsers.add_parser(
        "join-cmt", help="Join multiple files into a single .cmt file"
    )
    p_join_cmt.add_argument(
        "inputs", nargs="+", help="Input .cmt or .t88 files to concatenate"
    )
    p_join_cmt.add_argument(
        "-o", "--output", required=True, help="Path to output merged .cmt file"
    )

    p_join_t88 = subparsers.add_parser(
        "join-t88", help="Join multiple files into a single .t88 container"
    )
    p_join_t88.add_argument(
        "inputs", nargs="+", help="Input .cmt or .t88 files to concatenate"
    )
    p_join_t88.add_argument(
        "-o", "--output", required=True, help="Path to output merged .t88 file"
    )
    p_join_t88.add_argument(
        "-b",
        "--baud",
        type=int,
        default=None,
        help="Override baud rate for ALL output chunks (both .t88 and .cmt inputs)",
    )
    p_join_t88.add_argument(
        "--cmt-baud",
        "--default-baud",
        dest="cmt_baud",
        type=int,
        default=1200,
        help="Default baud rate to use for raw .cmt inputs (default: 1200). Does not affect .t88 inputs.",
    )
    p_join_t88.add_argument(
        "--comment",
        default="",
        help="Optional comment metadata string embedded in T88 file",
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

    if args.help_all:
        print(format_all_help(parser))
        sys.exit(0)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "t2c":
            out_file = convert_t88_to_cmt(args.input, args.output)
            print(f"[SUCCESS] Converted {args.input} -> {out_file}")

        elif args.command == "c2t":
            out_file = convert_cmt_to_t88(
                args.input,
                args.output,
                comment=args.comment,
                baud=args.baud,
            )
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

        elif args.command == "split-t88":
            summary = split_t88_file(
                args.input,
                args.output_dir,
                comment=args.comment,
                baud=args.baud,
                cmt_baud=args.cmt_baud,
            )
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

        elif args.command == "join-t88":
            out_file = join_t88_files(
                args.inputs,
                args.output,
                comment=args.comment,
                baud=args.baud,
                cmt_baud=args.cmt_baud,
            )
            print(f"[SUCCESS] Merged {len(args.inputs)} file(s) -> {out_file}")

    except Exception as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
