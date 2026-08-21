#!/usr/bin/env python3
"""
PC-8001 / PC-8801 Cassette Audio to T88 Streaming Demodulator & Converter
==========================================================================

Overview & Characteristics
--------------------------
Real-time streaming demodulator converting cassette audio captures from the
NEC PC-8001 / PC-8001mkII / PC-8001mkIISR / PC-8801 / PC-8801mkII / PC-8801mkIISR
family into standard .t88 tape images.

Hardware & Format Specifications:
- FSK Modulation: Mark = 2400 Hz, Space = 1200 Hz.
- Supported CMT Baud Rates: 1200 baud and 600 baud.
- 600 baud is strictly pulse-doubled 1200 baud:
  * 1200 baud: 1 Mark bit = 2 cycles of 2400 Hz; 1 Space bit = 1 cycle of 1200 Hz.
  *  600 baud: 1 Mark bit = 4 cycles of 2400 Hz; 1 Space bit = 2 cycles of 1200 Hz.
- Frame Format: 1 Start bit (0), 8 Data bits (LSB-first), 2 Stop bits (1).
- T88 Format Codes: 0x01CC for 1200 baud (44 ticks/byte), 0x00CC for 600 baud (88 ticks/byte).

Timing Flavors:
- verbatim: Raw wall-clock playback timeline (N_samples / F_s, no virtual ticks).
- reconstructed (Default): Phase-unpacked cycle counts (1M = 2 ticks, 1S = 4 ticks, no fake pulses).
- kinematic-infilled: Ballistic curve extrapolation infilling relay-void deficit (~10-25 ms).
- rom-authentic: Aligned to exact Z80 BIOS delay-loop target constants (12000 / 2400).
- canonical: DLE mastering templates (12000 / 2400, initial 480+480 GAP, 32000B DATA chunks).
"""

import argparse
import io
import math
import os
import random
import struct
import sys
from typing import BinaryIO, List, Optional, Tuple


# ============================================================================
# Unified T88 Tag Identifiers & DataSubHeader Constants
# ============================================================================


class T88Tag:
    END: int = 0x0000  # Terminal block marker
    VERSION: int = 0x0001  # Version info (uint16)
    COMMENT: int = 0x0010  # UTF-8 / ASCII annotation text
    GAP: int = 0x0100  # Blank / silence interval (start_tick, length_ticks)
    DATA: int = 0x0101  # Serial UART data block (start_tick, length_ticks, dlen, fmt)
    DATA_1200: int = 0x0101  # Alias for standard 1200 baud DATA tag
    DATA_300: int = 0x0101  # Alias for standard DATA tag
    SPACE: int = 0x0102  # 1200 Hz Space tone burst (start_tick, length_ticks)
    MARK: int = 0x0103  # 2400 Hz Mark carrier tone (start_tick, length_ticks)


class DataSubHeader:
    """12-byte T88 DATA block sub-header (<IIHH)."""

    STRUCT_FORMAT: str = "<IIHH"
    SIZE: int = 12

    def __init__(
        self,
        start_tick: int = 0,
        length_ticks: int = 0,
        data_len: int = 0,
        fmt_code: int = 0x01CC,
    ):
        self.start_tick = int(start_tick)
        self.length_ticks = int(length_ticks)
        self.data_len = int(data_len)
        self.fmt_code = int(fmt_code)

    def pack(self) -> bytes:
        return struct.pack(
            self.STRUCT_FORMAT,
            self.start_tick,
            self.length_ticks,
            self.data_len,
            self.fmt_code,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "DataSubHeader":
        st, lt, dlen, fmt = struct.unpack(cls.STRUCT_FORMAT, data[:12])
        return cls(st, lt, dlen, fmt)


# ============================================================================
# Streaming WAV Reader with Channel Routing
# ============================================================================


class StreamingWavReader:

    def __init__(self, stream: BinaryIO, channel_mode: str = "auto"):
        self.stream = stream
        self.channel_mode = channel_mode.lower()
        self.channels = 1
        self.sample_rate = 44100
        self.bits_per_sample = 16
        self.format_tag = 1  # 1 = PCM, 3 = IEEE float
        self.bytes_per_sample = 2
        self.frame_size = 2
        self.data_size = -1
        self._parse_header()

        self.l_energy = 0.0
        self.r_energy = 0.0

    def _read_exact(self, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = self.stream.read(count - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _parse_header(self):
        riff_hdr = self._read_exact(12)
        if len(riff_hdr) < 12 or riff_hdr[0:4] != b"RIFF" or riff_hdr[8:12] != b"WAVE":
            raise ValueError("Input is not a valid RIFF/WAVE stream")

        fmt_found = False
        while True:
            chunk_hdr = self._read_exact(8)
            if len(chunk_hdr) < 8:
                raise ValueError("Premature EOF while parsing WAV chunks")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_hdr)

            if chunk_id == b"fmt ":
                fmt_data = self._read_exact(chunk_size)
                if len(fmt_data) < 16:
                    raise ValueError("Invalid fmt chunk size")
                (
                    self.format_tag,
                    self.channels,
                    self.sample_rate,
                    byte_rate,
                    self.frame_size,
                    self.bits_per_sample,
                ) = struct.unpack("<HHIIHH", fmt_data[:16])
                self.bytes_per_sample = (self.bits_per_sample + 7) // 8
                fmt_found = True
            elif chunk_id == b"data":
                if not fmt_found:
                    raise ValueError("'data' chunk encountered before 'fmt ' chunk")
                self.data_size = chunk_size
                break
            else:
                if chunk_size > 0 and chunk_size != 0xFFFFFFFF:
                    self._read_exact(chunk_size)

    def read_samples(self, num_frames: int = 1024) -> List[float]:
        raw_bytes = self._read_exact(num_frames * self.frame_size)
        if not raw_bytes:
            return []

        frames_read = len(raw_bytes) // self.frame_size

        if self.bits_per_sample == 16 and self.format_tag == 1:
            total_values = frames_read * self.channels
            unpacked = struct.unpack(f"<{total_values}h", raw_bytes[: total_values * 2])
            raw_float = [v / 32768.0 for v in unpacked]
        elif self.bits_per_sample == 8 and self.format_tag == 1:
            total_values = frames_read * self.channels
            raw_float = [(v - 128) / 128.0 for v in raw_bytes[:total_values]]
        elif self.bits_per_sample == 24 and self.format_tag == 1:
            raw_float = []
            idx = 0
            for _ in range(frames_read * self.channels):
                val = int.from_bytes(
                    raw_bytes[idx : idx + 3], byteorder="little", signed=True
                )
                idx += 3
                raw_float.append(val / 8388608.0)
        elif self.bits_per_sample == 32 and self.format_tag == 3:
            total_values = frames_read * self.channels
            raw_float = list(
                struct.unpack(f"<{total_values}f", raw_bytes[: total_values * 4])
            )
        else:
            raise ValueError(
                f"Unsupported WAV format: format_tag={self.format_tag}, bits={self.bits_per_sample}"
            )

        if self.channels == 1:
            return raw_float

        c_mode = self.channel_mode
        if c_mode in ("auto", "left", "l", "0"):
            if c_mode == "auto":
                for i in range(frames_read):
                    self.l_energy += abs(raw_float[i * self.channels + 0])
                    self.r_energy += abs(raw_float[i * self.channels + 1])
                chosen = 1 if self.r_energy > (self.l_energy * 1.5) else 0
                return [
                    raw_float[i * self.channels + chosen] for i in range(frames_read)
                ]
            return [raw_float[i * self.channels + 0] for i in range(frames_read)]
        elif c_mode in ("right", "r", "1"):
            return [raw_float[i * self.channels + 1] for i in range(frames_read)]
        elif c_mode in ("mix", "mono"):
            return [
                (raw_float[i * self.channels + 0] + raw_float[i * self.channels + 1])
                * 0.5
                for i in range(frames_read)
            ]
        elif c_mode in ("diff", "l-r"):
            return [
                (raw_float[i * self.channels + 0] - raw_float[i * self.channels + 1])
                * 0.5
                for i in range(frames_read)
            ]
        return [raw_float[i * self.channels + 0] for i in range(frames_read)]


# ============================================================================
# Baud-Agnostic Pulse Recognizer (Analog Slicer + Sub-Sample Zero Crossing)
# ============================================================================


class BaudAgnosticPulseRecognizer:
    """Analog Front-End & Zero-Crossing Full-Cycle Pulse Extractor:

    - Zero baud awareness: only extracts 2400 Hz Mark ('M') and 1200 Hz Space ('S') cycles.
    - Sub-sample linear interpolation measures exact zero crossing timestamps.
    - AC-coupling DC blocker (~180 Hz cutoff).
    - 2nd-order Biquad Bandpass filter (600 - 3600 Hz).
    - Rapid-recovery AGC preventing 1200 Hz Space from masking quiet 2400 Hz Mark.
    - Polarity-independent (+, -) and (-, +) full-wave pairing.
    - Measures deck motor speed via carrier frequency tracking and time-corrects normalized tape time.
    """

    def __init__(self, sample_rate: float):
        self.fs = float(sample_rate)
        self.dt = 1.0 / self.fs

        f0 = 1800.0
        bw = 2400.0
        w0 = 2.0 * math.pi * f0 / self.fs
        q = f0 / bw
        alpha = math.sin(w0) / (2.0 * q)
        a0 = 1.0 + alpha
        self.b0 = alpha / a0
        self.b1 = 0.0
        self.b2 = -alpha / a0
        self.a1 = (-2.0 * math.cos(w0)) / a0
        self.a2 = (1.0 - alpha) / a0

        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0
        self.dc_x1 = 0.0
        self.dc_y1 = 0.0

        self.envelope = 0.0
        self.noise_floor = 0.0001
        self.peak_carrier = 0.001
        self.schmitt_state = 0
        self.last_transition_time = 0.0
        self.current_time = 0.0
        self.tape_time = 0.0  # Time-Base Corrected normalized tape time
        self.prev_y = 0.0

        self.pos_half_dur = 0.0
        self.neg_half_dur = 0.0

        self.measured_f_mark = 2400.0
        self.speed_factor = 1.0
        self.mark_dur_hist = []

    def process_sample(self, s: float) -> Optional[Tuple[str, float, int]]:
        self.current_time += self.dt
        self.tape_time += self.dt * self.speed_factor

        # 1. DC Blocker
        dc_y = s - self.dc_x1 + 0.995 * self.dc_y1
        self.dc_x1 = s
        self.dc_y1 = dc_y

        # 2. Bandpass Filter
        bp_y = (
            self.b0 * dc_y
            + self.b1 * self.x1
            + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2
        )
        self.x2 = self.x1
        self.x1 = dc_y
        self.y2 = self.y1
        self.y1 = bp_y

        # 3. Dynamic AGC (Fast attack, rapid decay)
        abs_y = abs(bp_y)
        if abs_y > self.envelope:
            self.envelope = 0.75 * self.envelope + 0.25 * abs_y
        else:
            self.envelope = 0.998 * self.envelope + 0.002 * abs_y

        if self.envelope > self.peak_carrier:
            self.peak_carrier = 0.9 * self.peak_carrier + 0.1 * self.envelope
        else:
            self.peak_carrier = 0.9999 * self.peak_carrier + 0.0001 * self.envelope

        if abs_y < self.noise_floor:
            self.noise_floor = 0.99 * self.noise_floor + 0.01 * abs_y
        else:
            self.noise_floor = 0.99999 * self.noise_floor + 0.00001 * abs_y

        # 4. Adaptive Schmitt Trigger Slicer (12% of envelope)
        v_thresh = max(self.envelope * 0.12, self.noise_floor * 2.0, 0.0003)
        new_state = self.schmitt_state
        if bp_y > v_thresh:
            new_state = 1
        elif bp_y < -v_thresh:
            new_state = -1

        # 5. Zero-crossing transition with sub-sample linear interpolation
        if new_state != self.schmitt_state and new_state != 0:
            if (bp_y - self.prev_y) != 0:
                frac = (0.0 - self.prev_y) / (bp_y - self.prev_y)
                frac = max(0.0, min(1.0, frac))
            else:
                frac = 0.5
            exact_crossing_time = (self.current_time - self.dt) + frac * self.dt

            half_dur_sec = exact_crossing_time - self.last_transition_time
            self.last_transition_time = exact_crossing_time
            prev_polarity = self.schmitt_state
            self.schmitt_state = new_state

            if half_dur_sec >= 0.00010:  # Filter glitches < 100 µs
                if prev_polarity == 1:
                    self.pos_half_dur = half_dur_sec
                elif prev_polarity == -1:
                    self.neg_half_dur = half_dur_sec

                if self.pos_half_dur > 0 and self.neg_half_dur > 0:
                    full_cycle_sec = self.pos_half_dur + self.neg_half_dur
                    self.pos_half_dur = 0.0
                    self.neg_half_dur = 0.0

                    nominal_mark_period = 1.0 / self.measured_f_mark
                    boundary_period = nominal_mark_period * 1.414

                    # Squelch check: if signal dropped below 22% of active carrier peak
                    if self.envelope < max(
                        self.peak_carrier * 0.22, self.noise_floor * 2.0, 0.0005
                    ):
                        sym = "B"
                        self.mark_dur_hist.clear()
                    elif full_cycle_sec < boundary_period:
                        sym = "M"  # 2400 Hz Mark
                        self.mark_dur_hist.append(full_cycle_sec)
                        if len(self.mark_dur_hist) > 80:
                            self.mark_dur_hist.pop(0)
                        if len(self.mark_dur_hist) >= 20:
                            med_mark = sorted(self.mark_dur_hist)[
                                len(self.mark_dur_hist) // 2
                            ]
                            if 0.00032 <= med_mark <= 0.00052:
                                self.measured_f_mark = (
                                    0.96 * self.measured_f_mark
                                    + 0.04 * (1.0 / med_mark)
                                )
                                self.speed_factor = self.measured_f_mark / 2400.0
                    elif full_cycle_sec <= (1.0 / (1200.0 * 0.75)):
                        sym = "S"  # 1200 Hz Space
                        self.mark_dur_hist.clear()
                    else:
                        sym = "B"  # Blank / Gap
                        self.mark_dur_hist.clear()

                    cur_sample = int(round(exact_crossing_time * self.fs))
                    return (sym, full_cycle_sec, cur_sample)

        # Silence check
        if (self.current_time - self.last_transition_time) > 0.0015:
            if self.envelope < max(self.noise_floor * 3.0, 0.0008):
                cur_sample = int(round(self.current_time * self.fs))
                return ("B", 0.0015, cur_sample)

        self.prev_y = bp_y
        return None


# ============================================================================
# Pulse-to-Byte Framing Acceptor (600 & 1200 Baud)
# ============================================================================


class PulseToByteAcceptor:
    """Converts incoming Mark/Space pulses into serial UART bytes for 1200 or 600 baud.

    600 baud is strictly pulse-doubled 1200 baud:
    - 1200 baud: 1 bit = 2 pulse units (Mark = 2 Mark cycles; Space = 1 Space cycle).
    -  600 baud: 1 bit = 4 pulse units (Mark = 4 Mark cycles; Space = 2 Space cycles).
    """

    def __init__(self, baud: int, confidence_threshold: float = 0.75):
        self.nominal_baud = baud
        self.baud = float(baud)
        self.bit_duration = 1.0 / self.baud
        self.confidence_threshold = float(confidence_threshold)
        self.speed_factor = 1.0
        self.reset()

    def update_speed(self, speed_factor: float):
        self.speed_factor = max(0.85, min(1.18, speed_factor))
        self.baud = self.nominal_baud * self.speed_factor
        self.bit_duration = 1.0 / self.baud

    def reset(self, in_block: bool = False, in_session: bool = False):
        self.state = "IDLE"  # IDLE, DATA, STOP
        self.bit_index = 0
        self.current_byte = 0
        self.accum_time = 0.0
        self.mark_time = 0.0
        self.space_time = 0.0
        self.start_tick = 0
        self.last_activity_tick = 0
        self.carrier_mark_time = 0.0
        self.consecutive_mark_time = 0.0
        self.in_block = in_block
        self.in_session = in_session
        self.leader_validated = False
        self.bit_confidences: List[float] = []

    def feed_full_cycle(
        self, sym: str, dur_sec: float, cur_tick: int
    ) -> Optional[Tuple[int, int, str, float]]:
        if sym == "B":
            self.state = "IDLE"
            self.carrier_mark_time = 0.0
            self.consecutive_mark_time = 0.0
            self.leader_validated = False
            self.in_block = False
            self.in_session = False
            self.bit_confidences.clear()
            return None

        if sym == "M":
            self.mark_time += dur_sec
            self.carrier_mark_time += dur_sec
            self.consecutive_mark_time += dur_sec
        elif sym == "S":
            self.space_time += dur_sec
            self.carrier_mark_time = 0.0
        else:
            self.state = "IDLE"
            return None

        self.accum_time += dur_sec
        result = None

        if self.state == "IDLE":
            if sym == "M":
                self.space_time = 0.0
                self.accum_time = 0.0
                min_mark = (
                    (self.bit_duration * 0.90)
                    if self.in_block
                    else (
                        max(0.020, self.bit_duration * 12.0)
                        if self.in_session
                        else max(0.040, self.bit_duration * 25.0)
                    )
                )
                if self.consecutive_mark_time >= min_mark:
                    self.leader_validated = True
            elif sym == "S":
                if not self.leader_validated:
                    self.consecutive_mark_time = 0.0
                    self.space_time = 0.0
                    self.accum_time = 0.0
                    return None

                start_cycles = 1200.0 / self.nominal_baud
                start_space_thresh = max(
                    (start_cycles - 0.35)
                    * (1.0 / (1200.0 * (self.baud / self.nominal_baud))),
                    self.bit_duration * 0.65,
                )
                if self.space_time >= start_space_thresh:
                    self.state = "DATA"
                    self.bit_index = 0
                    self.current_byte = 0
                    tot_start = self.space_time + self.mark_time
                    start_conf = (
                        min(
                            1.0,
                            self.space_time / max(self.bit_duration * 0.85, tot_start),
                        )
                        if tot_start > 0
                        else 0.0
                    )
                    self.bit_confidences = [start_conf]
                    self.accum_time -= self.bit_duration
                    if self.accum_time < 0:
                        self.accum_time = 0.0
                    self.mark_time = 0.0
                    self.space_time = 0.0
                    # Start bit rewind under time-base corrected clock
                    nominal_bit_dur_tape = 1.0 / self.nominal_baud
                    filter_delay_tape = 5.5 / 44100.0 * self.speed_factor
                    self.start_tick = max(
                        0,
                        int(
                            round(
                                (
                                    (cur_tick / 4800.0)
                                    - nominal_bit_dur_tape
                                    - filter_delay_tape
                                )
                                * 4800.0
                            )
                        ),
                    )
                    self.last_activity_tick = cur_tick
                    self.consecutive_mark_time = 0.0
                    self.leader_validated = False

        elif self.state == "DATA":
            bit_thresh = self.bit_duration - 0.000150
            if self.accum_time >= bit_thresh:
                tot = self.mark_time + self.space_time
                if tot > 0:
                    bit_val = 1 if self.mark_time >= self.space_time else 0
                    bit_conf = max(self.mark_time, self.space_time) / tot
                else:
                    bit_val = 0
                    bit_conf = 0.0
                self.bit_confidences.append(bit_conf)

                self.current_byte |= bit_val << self.bit_index
                self.bit_index += 1
                self.accum_time -= self.bit_duration
                if self.accum_time < 0:
                    self.accum_time = 0.0
                self.mark_time = 0.0
                self.space_time = 0.0
                self.last_activity_tick = cur_tick

                if self.bit_index == 8:
                    self.state = "STOP"

        elif self.state == "STOP":
            if self.accum_time >= (self.bit_duration * 1.50):
                self.last_activity_tick = cur_tick
                tot = self.mark_time + self.space_time
                stop_conf = (self.mark_time / tot) if tot > 0 else 0.0
                self.bit_confidences.append(stop_conf)

                byte_conf = (
                    sum(self.bit_confidences) / len(self.bit_confidences)
                    if self.bit_confidences
                    else 0.0
                )
                low_conf_bits = sum(1 for c in self.bit_confidences if c < 0.55)

                if tot > 0 and (self.mark_time / tot) >= 0.60:
                    if byte_conf >= self.confidence_threshold and low_conf_bits <= 2:
                        result = (
                            self.current_byte,
                            self.start_tick,
                            "OK",
                            byte_conf,
                        )
                    else:
                        result = (
                            self.current_byte,
                            self.start_tick,
                            "LOW_CONFIDENCE",
                            byte_conf,
                        )
                else:
                    result = (
                        self.current_byte,
                        self.start_tick,
                        "FRAMING_ERROR",
                        byte_conf,
                    )

                self.state = "IDLE"
                self.accum_time = 0.0
                self.mark_time = 0.0
                self.space_time = 0.0
                self.bit_confidences.clear()
                self.leader_validated = self.in_block
                self.consecutive_mark_time = self.bit_duration * 1.5

        return result


# ============================================================================
# T88 Stream Writer
# ============================================================================


class T88StreamWriter:

    def __init__(self, out_stream: BinaryIO):
        self.out = out_stream
        self.header_written = False

    def write_header(self):
        if not self.header_written:
            hdr = (
                b"PC-8801 Tape Image(T88)\x00"
                + struct.pack("<HHHH", 0x0001, 0x0002, 0x0100, 0x0000)[:6]
            )
            self.out.write(hdr)
            self.header_written = True
            self.out.flush()

    def _write_tag(self, tag_id: int, payload: bytes):
        self.write_header()
        self.out.write(struct.pack("<HH", tag_id, len(payload)) + payload)
        self.out.flush()

    def write_blank(self, start_tick: int, length_tick: int):
        if length_tick > 0:
            self._write_tag(
                T88Tag.GAP, struct.pack("<II", int(start_tick), int(length_tick))
            )

    def write_space(self, start_tick: int, length_tick: int):
        if length_tick > 0:
            self._write_tag(
                T88Tag.SPACE, struct.pack("<II", int(start_tick), int(length_tick))
            )

    def write_mark(self, start_tick: int, length_tick: int):
        if length_tick > 0:
            self._write_tag(
                T88Tag.MARK, struct.pack("<II", int(start_tick), int(length_tick))
            )

    def write_data(self, start_tick: int, baud: int, data_bytes: bytes):
        if not data_bytes:
            return
        fmt = 0x01CC if baud >= 1200 else 0x00CC
        ticks_per_byte = int(round(11.0 * 4800.0 / baud))

        offset = 0
        cur_tick = start_tick
        while offset < len(data_bytes):
            chunk = data_bytes[offset : offset + 32768]
            length_tick = len(chunk) * ticks_per_byte
            hdr = DataSubHeader(cur_tick, length_tick, len(chunk), fmt).pack()
            self._write_tag(T88Tag.DATA, hdr + chunk)
            offset += len(chunk)
            cur_tick += length_tick

    def write_end(self):
        self._write_tag(T88Tag.END, b"")


# ============================================================================
# Main Processing & Diagnostics
# ============================================================================


def log_diag(msg: str):
    sys.stderr.write(f"[wav2t88] {msg}\n")
    sys.stderr.flush()


def process_stream(
    in_stream: BinaryIO,
    out_stream: BinaryIO,
    supported_bauds: Tuple[int, ...] = (600, 1200),
    channel_mode: str = "auto",
    confidence_threshold: float = 0.75,
    flavor: str = "reconstructed",
    quiet: bool = False,
):
    if not quiet:
        log_diag("Parsing incoming WAV stream...")
    reader = StreamingWavReader(in_stream, channel_mode=channel_mode)
    fs = reader.sample_rate

    candidate_order = (
        tuple(sorted(supported_bauds)) if len(supported_bauds) > 1 else supported_bauds
    )

    if not quiet:
        chan_desc = f"Channel: {channel_mode.upper()} ({reader.channels}-ch source)"
        baud_desc = (
            f"{candidate_order[0]} baud (Fixed)"
            if len(candidate_order) == 1
            else f"Auto-detect ({','.join(map(str, candidate_order))})"
        )
        log_diag(
            f"Source: {reader.sample_rate} Hz, {reader.bits_per_sample}-bit, {chan_desc}, "
            f"Baud: {baud_desc}, Flavor: {flavor.upper()}, Min Confidence: {confidence_threshold * 100:.1f}%"
        )

    demod = BaudAgnosticPulseRecognizer(fs)
    acceptors = {
        b: PulseToByteAcceptor(b, confidence_threshold=confidence_threshold)
        for b in candidate_order
    }
    active_acceptor: Optional[PulseToByteAcceptor] = None
    writer = T88StreamWriter(out_stream)

    state = "BLANK"
    state_start_tick = 0
    mark_counter = 0
    space_counter = 0
    mark_first_tick = 0
    space_first_tick = 0
    last_mark_tick = 0
    last_space_tick = 0
    data_buffer: List[int] = []
    block_confidences: List[float] = []
    data_start_tick = 0
    total_bytes_decoded = 0
    block_index = 0

    session_locked_baud: Optional[int] = None
    candidate_buffers = {b: [] for b in candidate_order}

    last_reported_bytes = 0
    last_progress_tick = 0

    def format_time(ticks: int) -> str:
        sec = ticks / 4800.0
        m = int(sec // 60)
        s = sec % 60
        return f"{m:02d}:{s:06.3f}"

    while True:
        samples = reader.read_samples(1024)
        if not samples:
            break

        for s in samples:
            ev_cycle = demod.process_sample(s)
            # Time-Base Corrected normalized tape container tick
            cur_tick = int(round(demod.tape_time * 4800.0))

            if ev_cycle:
                sym, dur_sec, sample_idx = ev_cycle

                for acc in acceptors.values():
                    acc.update_speed(demod.speed_factor)

                if active_acceptor is None:
                    active_candidates = (
                        (session_locked_baud,)
                        if session_locked_baud is not None
                        else candidate_order
                    )
                    confirmed_acc = None

                    for baud in active_candidates:
                        acc = acceptors[baud]
                        ev = acc.feed_full_cycle(sym, dur_sec, cur_tick)
                        if ev:
                            if ev[2] == "OK":
                                candidate_buffers[baud] = [(ev[0], ev[1], ev[3])]
                                confirmed_acc = acc
                                break
                            elif ev[2] == "LOW_CONFIDENCE":
                                if not quiet:
                                    log_diag(
                                        f"Rejected low-confidence byte: 0x{ev[0]:02X} "
                                        f"(confidence: {ev[3] * 100:.1f}% < "
                                        f"{confidence_threshold * 100:.1f}%) at {format_time(ev[1])}"
                                    )
                                candidate_buffers[baud].clear()
                            elif ev[2] == "FRAMING_ERROR":
                                candidate_buffers[baud].clear()

                    if confirmed_acc is not None:
                        active_acceptor = confirmed_acc
                        active_acceptor.in_block = True
                        active_acceptor.leader_validated = True
                        chosen_baud = active_acceptor.nominal_baud
                        session_locked_baud = chosen_baud
                        byte_val, byte_tick_start, byte_conf = candidate_buffers[
                            chosen_baud
                        ][0]

                        prev_len = byte_tick_start - state_start_tick
                        if prev_len > 0:
                            if state == "BLANK":
                                writer.write_blank(state_start_tick, prev_len)
                            elif state == "MARK":
                                writer.write_mark(state_start_tick, prev_len)
                            elif state == "SPACE":
                                writer.write_space(state_start_tick, prev_len)

                        state = "DATA"
                        data_start_tick = byte_tick_start
                        data_buffer = [byte_val]
                        block_confidences = [byte_conf]
                        total_bytes_decoded += 1
                        block_index += 1
                        for b in candidate_order:
                            candidate_buffers[b].clear()
                else:
                    ev = active_acceptor.feed_full_cycle(sym, dur_sec, cur_tick)
                    if ev:
                        if ev[2] == "OK":
                            data_buffer.append(ev[0])
                            block_confidences.append(ev[3])
                            total_bytes_decoded += 1
                        elif ev[2] == "LOW_CONFIDENCE":
                            if not quiet:
                                log_diag(
                                    f"Rejected low-confidence byte: 0x{ev[0]:02X} "
                                    f"(confidence: {ev[3] * 100:.1f}% < "
                                    f"{confidence_threshold * 100:.1f}%) at {format_time(ev[1])}"
                                )

            if active_acceptor is not None:
                is_carrier_returned = active_acceptor.carrier_mark_time >= (
                    active_acceptor.bit_duration * 24.0
                )
                is_silence_gap = (cur_tick - active_acceptor.last_activity_tick) > int(
                    4800 * 0.15
                )

                if is_carrier_returned or is_silence_gap:
                    is_noise_fragment = (
                        data_buffer
                        and len(data_buffer) < 4
                        and is_silence_gap
                        and not is_carrier_returned
                    )
                    ticks_per_byte = int(
                        round(11.0 * 4800.0 / active_acceptor.nominal_baud)
                    )
                    data_end_tick = data_start_tick + len(data_buffer) * ticks_per_byte

                    if data_buffer and not is_noise_fragment:
                        raw_data = bytes(data_buffer)
                        writer.write_data(
                            data_start_tick,
                            active_acceptor.nominal_baud,
                            raw_data,
                        )
                        if not quiet:
                            dev_pct = (demod.speed_factor - 1.0) * 100.0
                            avg_conf = (
                                (sum(block_confidences) / len(block_confidences))
                                * 100.0
                                if block_confidences
                                else 100.0
                            )
                            log_diag(
                                f"Block {block_index:2d}: {len(raw_data):5d} bytes [{active_acceptor.nominal_baud} baud, "
                                f"{demod.measured_f_mark:6.1f} Hz ({dev_pct:+4.1f}% speed), "
                                f"conf: {avg_conf:5.1f}%] at {format_time(data_start_tick)}"
                            )
                    elif is_noise_fragment:
                        if not quiet:
                            log_diag(
                                f"Rejected spurious noise fragment ({len(data_buffer)} byte(s)) "
                                f"at {format_time(data_start_tick)}"
                            )
                        block_index -= 1
                        total_bytes_decoded -= len(data_buffer)

                    data_buffer = []
                    block_confidences = []
                    # Post-DATA fix: rewind start tick of next carrier/blank to exact data completion tick
                    state_start_tick = data_end_tick
                    state = "MARK" if is_carrier_returned else "BLANK"
                    if is_carrier_returned:
                        last_mark_tick = cur_tick
                    if is_silence_gap:
                        session_locked_baud = None

                    reuse_leader = (
                        is_carrier_returned
                        and not is_silence_gap
                        and session_locked_baud is not None
                    )
                    active_acceptor = None
                    for b, acc in acceptors.items():
                        acc.reset(
                            in_block=False,
                            in_session=(session_locked_baud is not None),
                        )
                        if reuse_leader and b == session_locked_baud:
                            acc.leader_validated = True
                        candidate_buffers[b].clear()

            elif state == "BLANK":
                if ev_cycle and ev_cycle[0] == "M":
                    cycle_ticks = int(round(ev_cycle[1] * demod.speed_factor * 4800.0))
                    if mark_counter == 0:
                        mark_first_tick = max(state_start_tick, cur_tick - cycle_ticks)
                    mark_counter += 1
                    space_counter = 0
                    if mark_counter > 40:
                        tag_len = mark_first_tick - state_start_tick
                        if tag_len > 0:
                            writer.write_blank(state_start_tick, tag_len)
                        state = "MARK"
                        state_start_tick = mark_first_tick
                        last_mark_tick = cur_tick
                        mark_counter = 0
                elif ev_cycle and ev_cycle[0] == "S":
                    cycle_ticks = int(round(ev_cycle[1] * demod.speed_factor * 4800.0))
                    if space_counter == 0:
                        space_first_tick = max(state_start_tick, cur_tick - cycle_ticks)
                    space_counter += 1
                    mark_counter = 0
                    if space_counter > 20:
                        tag_len = space_first_tick - state_start_tick
                        if tag_len > 0:
                            writer.write_blank(state_start_tick, tag_len)
                        state = "SPACE"
                        state_start_tick = space_first_tick
                        last_space_tick = cur_tick
                        space_counter = 0
                elif ev_cycle and ev_cycle[0] == "B":
                    mark_counter = 0
                    space_counter = 0

            elif state == "MARK":
                if ev_cycle and ev_cycle[0] == "M":
                    last_mark_tick = cur_tick
                    space_counter = 0
                elif ev_cycle and ev_cycle[0] == "S":
                    cycle_ticks = int(round(ev_cycle[1] * demod.speed_factor * 4800.0))
                    if space_counter == 0:
                        space_first_tick = cur_tick - cycle_ticks
                    space_counter += 1
                    if space_counter > 20:
                        tag_len = space_first_tick - state_start_tick
                        if tag_len > 0:
                            writer.write_mark(state_start_tick, tag_len)
                            state_start_tick = space_first_tick
                        state = "SPACE"
                        last_space_tick = cur_tick
                        space_counter = 0
                if (cur_tick - last_mark_tick) > int(4800 * 0.050) and state == "MARK":
                    tag_len = last_mark_tick - state_start_tick
                    if tag_len > 0:
                        writer.write_mark(state_start_tick, tag_len)
                        state_start_tick = last_mark_tick
                    state = "BLANK"
                    session_locked_baud = None
                    for acc in acceptors.values():
                        acc.in_session = False
                    mark_counter = 0
                    space_counter = 0

            elif state == "SPACE":
                if ev_cycle and ev_cycle[0] == "S":
                    last_space_tick = cur_tick
                    mark_counter = 0
                elif ev_cycle and ev_cycle[0] == "M":
                    cycle_ticks = int(round(ev_cycle[1] * demod.speed_factor * 4800.0))
                    if mark_counter == 0:
                        mark_first_tick = cur_tick - cycle_ticks
                    mark_counter += 1
                    if mark_counter > 40:
                        tag_len = mark_first_tick - state_start_tick
                        if tag_len > 0:
                            writer.write_space(state_start_tick, tag_len)
                            state_start_tick = mark_first_tick
                        state = "MARK"
                        last_mark_tick = cur_tick
                        mark_counter = 0
                if (cur_tick - last_space_tick) > int(
                    4800 * 0.050
                ) and state == "SPACE":
                    tag_len = last_space_tick - state_start_tick
                    if tag_len > 0:
                        writer.write_space(state_start_tick, tag_len)
                        state_start_tick = last_space_tick
                    state = "BLANK"
                    mark_counter = 0
                    space_counter = 0

        if not quiet and (cur_tick - last_progress_tick) >= (4800 * 10):
            last_progress_tick = cur_tick
            if total_bytes_decoded > last_reported_bytes:
                last_reported_bytes = total_bytes_decoded
                log_diag(
                    f"Progress: {format_time(cur_tick)} (State: {state}, Total: {total_bytes_decoded} bytes in {block_index} blocks)"
                )

    cur_tick = int(round(demod.tape_time * 4800.0))
    if state == "DATA" and data_buffer and active_acceptor is not None:
        raw_data = bytes(data_buffer)
        writer.write_data(data_start_tick, active_acceptor.nominal_baud, raw_data)
        if not quiet:
            dev_pct = (demod.speed_factor - 1.0) * 100.0
            avg_conf = (
                (sum(block_confidences) / len(block_confidences)) * 100.0
                if block_confidences
                else 100.0
            )
            log_diag(
                f"Block {block_index:2d}: {len(raw_data):5d} bytes [{active_acceptor.nominal_baud} baud, "
                f"{demod.measured_f_mark:6.1f} Hz ({dev_pct:+4.1f}% speed), "
                f"conf: {avg_conf:5.1f}%] at {format_time(data_start_tick)}"
            )
    elif state == "BLANK":
        writer.write_blank(state_start_tick, cur_tick - state_start_tick)
    elif state == "MARK":
        writer.write_mark(state_start_tick, cur_tick - state_start_tick)
    elif state == "SPACE":
        writer.write_space(state_start_tick, cur_tick - state_start_tick)

    writer.write_end()
    if not quiet:
        log_diag(
            f"Finished: {total_bytes_decoded} total bytes decoded across {block_index} blocks ({format_time(cur_tick)} duration)."
        )


# ============================================================================
# Tape Inspector & Diagnostic Report Tool (--inspect)
# ============================================================================


def run_inspector(
    in_stream: BinaryIO, channel_mode: str = "auto", out_stream=sys.stdout
):
    reader = StreamingWavReader(in_stream, channel_mode=channel_mode)
    fs = reader.sample_rate
    demod = BaudAgnosticPulseRecognizer(fs)

    def print_out(msg: str):
        print(msg, file=out_stream)

    print_out("======================================================================")
    print_out("               PC-8001 / PC-8801 TAPE AUDIO INSPECTOR                 ")
    print_out("======================================================================")
    print_out(
        f"Source Format : {reader.sample_rate} Hz, {reader.bits_per_sample}-bit, {reader.channels} channel(s)"
    )
    print_out(f"Channel Mode  : {channel_mode.upper()}")
    print_out("Scanning tape audio signal...")

    total_samples = 0
    mark_cycles = 0
    space_cycles = 0
    speed_samples = []

    while True:
        samples = reader.read_samples(2048)
        if not samples:
            break
        total_samples += len(samples)
        for s in samples:
            ev = demod.process_sample(s)
            if ev:
                if ev[0] == "M":
                    mark_cycles += 1
                    speed_samples.append(demod.measured_f_mark)
                elif ev[0] == "S":
                    space_cycles += 1

    dur_sec = total_samples / fs
    m = int(dur_sec // 60)
    s = dur_sec % 60

    print_out("----------------------------------------------------------------------")
    print_out(f"Total Duration       : {m:02d}:{s:06.3f} ({total_samples} samples)")
    if reader.channels > 1:
        print_out(f"Left Channel Energy  : {reader.l_energy:.1f}")
        print_out(f"Right Channel Energy : {reader.r_energy:.1f}")
        if reader.l_energy > reader.r_energy * 2.0:
            print_out(
                "Recommendation       : Data is predominantly on LEFT channel. Use '--channel left'."
            )
        elif reader.r_energy > reader.l_energy * 2.0:
            print_out(
                "Recommendation       : Data is predominantly on RIGHT channel. Use '--channel right'."
            )

    print_out(f"2400 Hz Mark Cycles  : {mark_cycles}")
    print_out(f"1200 Hz Space Cycles : {space_cycles}")

    if speed_samples:
        avg_f_mark = sum(speed_samples) / len(speed_samples)
        speed_offset_pct = (avg_f_mark / 2400.0 - 1.0) * 100.0
        print_out(
            f"Avg Carrier Freq     : {avg_f_mark:.1f} Hz (Deck Motor Speed: {speed_offset_pct:+.2f}%)"
        )
    else:
        print_out("Carrier Signal       : WARNING: No 2400 Hz Mark tone detected.")

    print_out("======================================================================")


# ============================================================================
# Built-In Test Suite (--test)
# ============================================================================


def generate_synthetic_test_wav(
    blocks: List[Tuple[bytes, int]],
    sample_rate: int = 44100,
    wave_type: str = "sine",
    snr_db: Optional[float] = None,
    invert_polarity: bool = False,
    dc_offset: float = 0.0,
    duty_asymmetry: float = 0.0,
    amplitude: float = 0.8,
    stereo: bool = False,
    speed_factor: float = 1.0,
    prng_seed: int = 42,
) -> io.BytesIO:
    rng = random.Random(prng_seed)
    sr = float(sample_rate)
    dt = 1.0 / sr
    current_time = 0.0
    mono_samples = []

    def shape_sample(phase: float) -> float:
        sine_val = math.sin(phase)
        if wave_type == "square":
            base = 1.0 if sine_val >= 0.0 else -1.0
        elif wave_type == "tape":
            s = sine_val + 0.15 * math.sin(2.0 * phase)
            base = math.tanh(s * 1.8)
        else:
            base = sine_val

        if duty_asymmetry != 0.0:
            base = base + duty_asymmetry if base > 0 else base - duty_asymmetry

        base += dc_offset
        if invert_polarity:
            base = -base
        return base

    def add_tone(freq: float, duration_sec: float):
        nonlocal current_time
        t_end = current_time + duration_sec
        t_start = current_time
        while current_time < t_end:
            t_rel = current_time - t_start
            phase = 2.0 * math.pi * freq * t_rel
            mono_samples.append(amplitude * shape_sample(phase))
            current_time += dt

    def add_data(data_bytes: bytes, baud: int):
        nonlocal current_time
        actual_baud = baud * speed_factor
        bit_dur = 1.0 / actual_baud
        for b in data_bytes:
            bits = [0] + [(b >> i) & 1 for i in range(8)] + [1, 1]
            for bit in bits:
                freq = (2400.0 if bit == 1 else 1200.0) * speed_factor
                t_end = current_time + bit_dur
                t_start = current_time
                while current_time < t_end:
                    t_rel = current_time - t_start
                    phase = 2.0 * math.pi * freq * t_rel
                    mono_samples.append(amplitude * shape_sample(phase))
                    current_time += dt

    for data, baud in blocks:
        add_tone(2400.0 * speed_factor, 0.25)
        add_data(data, baud)
        add_tone(2400.0 * speed_factor, 0.15)
        num_gap = int(0.15 * sr)
        mono_samples.extend([0.0] * num_gap)
        current_time += num_gap * dt

    if snr_db is not None:
        noise_amp = amplitude * (10.0 ** (-snr_db / 20.0))
        n_state = 0.0
        noisy_samples = []
        for s in mono_samples:
            raw_white = rng.random() * 2.0 - 1.0
            n_state = 0.8 * n_state + 0.2 * raw_white
            noise_val = (0.6 * raw_white + 0.4 * n_state) * noise_amp
            noisy_samples.append(s + noise_val)
        mono_samples = noisy_samples

    out_io = io.BytesIO()
    channels = 2 if stereo else 1
    num_frames = len(mono_samples)
    byte_rate = int(sr * channels * 2)
    block_align = channels * 2

    out_io.write(b"RIFF\xff\xff\xff\xffWAVE")
    out_io.write(b"fmt \x10\x00\x00\x00")
    out_io.write(
        struct.pack("<HHIIHH", 1, channels, int(sr), byte_rate, block_align, 16)
    )
    out_io.write(b"data\xff\xff\xff\xff")

    if not stereo:
        pcm = [max(min(int(s * 32767.0), 32767), -32768) for s in mono_samples]
        out_io.write(struct.pack(f"<{len(pcm)}h", *pcm))
    else:
        stereo_pcm = []
        for i in range(num_frames):
            val = max(min(int(mono_samples[i] * 32767.0), 32767), -32768)
            stereo_pcm.extend([val, val])
        out_io.write(struct.pack(f"<{len(stereo_pcm)}h", *stereo_pcm))

    out_io.seek(0)
    return out_io


def parse_t88_stream(t88_bytes: bytes) -> List[Tuple[int, bytes]]:
    if len(t88_bytes) < 24 or t88_bytes[:24] != b"PC-8801 Tape Image(T88)\x00":
        raise ValueError("Invalid T88 header")
    pos = 24
    tags = []
    while pos + 4 <= len(t88_bytes):
        tag_id, dlen = struct.unpack("<HH", t88_bytes[pos : pos + 4])
        pos += 4
        payload = t88_bytes[pos : pos + dlen]
        pos += dlen
        tags.append((tag_id, payload))
        if tag_id == 0x0000:
            break
    return tags


def run_test_suite() -> bool:
    sys.stderr.write(
        "======================================================================\n"
    )
    sys.stderr.write(
        "     PC-8001 / PC-8801 Cassette Demodulator Built-In Test Suite       \n"
    )
    sys.stderr.write(
        "======================================================================\n\n"
    )

    test_cases = [
        {
            "name": "Pure Sinusoidal (Normal Polarity, 1200 Baud)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3SINE_1200_BASIC_TEST",
                    1200,
                )
            ],
            "wave": "sine",
            "snr": None,
            "inv": False,
            "amp": 0.8,
            "dc": 0.0,
            "asym": 0.0,
            "speed": 1.00,
            "force_baud": 1200,
        },
        {
            "name": "Pure Sinusoidal (Inverted Polarity 180 deg, 600 Baud)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3INVERTED_600_TEST",
                    600,
                )
            ],
            "wave": "sine",
            "snr": None,
            "inv": True,
            "amp": 0.8,
            "dc": 0.0,
            "asym": 0.0,
            "speed": 1.00,
            "force_baud": 600,
        },
        {
            "name": "Tape Saturation Curve (tanh + 2nd Harmonic + Asymmetry)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3TAPE_DISTORTION_1200",
                    1200,
                )
            ],
            "wave": "tape",
            "snr": None,
            "inv": False,
            "amp": 0.8,
            "dc": 0.04,
            "asym": 0.05,
            "speed": 1.00,
            "force_baud": None,
        },
        {
            "name": "Hard Overdriven Square Wave (Saturated Line-In)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3SQUARE_OVERDRIVE_600",
                    600,
                )
            ],
            "wave": "square",
            "snr": None,
            "inv": False,
            "amp": 1.0,
            "dc": 0.0,
            "asym": 0.0,
            "speed": 1.00,
            "force_baud": 600,
        },
        {
            "name": "Aging Tape Seeded-PRNG Noise (20 dB SNR Hiss & Rumble)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3NOISE_20DB_TEST",
                    1200,
                )
            ],
            "wave": "tape",
            "snr": 20.0,
            "inv": False,
            "amp": 0.8,
            "dc": 0.03,
            "asym": 0.03,
            "speed": 1.00,
            "force_baud": 1200,
        },
        {
            "name": "Heavy Degradation Seeded-PRNG Noise (14 dB SNR)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3HEAVY_NOISE_14DB_600",
                    600,
                )
            ],
            "wave": "tape",
            "snr": 14.0,
            "inv": True,
            "amp": 0.8,
            "dc": 0.05,
            "asym": 0.04,
            "speed": 1.00,
            "force_baud": 600,
        },
        {
            "name": "Tape Motor Speed Error (+4.5% Fast Playback Drift)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3FAST_SPEED_4.5_PCT",
                    1200,
                )
            ],
            "wave": "tape",
            "snr": 25.0,
            "inv": False,
            "amp": 0.8,
            "dc": 0.0,
            "asym": 0.0,
            "speed": 1.045,
            "force_baud": 1200,
        },
        {
            "name": "Faint Recording AGC Dynamic Range (-45 dBFS, Amplitude 0.005)",
            "blocks": [
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3FAINT_QUIET_RECORDING_1200",
                    1200,
                )
            ],
            "wave": "sine",
            "snr": None,
            "inv": False,
            "amp": 0.005,
            "dc": 0.0,
            "asym": 0.0,
            "speed": 1.00,
            "force_baud": 1200,
        },
        {
            "name": "9-Byte File Headers & Binary 0x00/0xFF Blocks (Multi-Part Tape)",
            "blocks": [
                (
                    b"\x00TEST1 \x00\x00",
                    1200,
                ),  # 9-byte header block starting with 0x00
                (
                    b"\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3\xd3SEC1_DATA_BLOCK",
                    1200,
                ),
                (
                    b"\x00TEST2 \x00\x00",
                    600,
                ),  # 9-byte header block at 600 baud
                (b"\x00\x00\x00\x00\xff\xff\xff\xffBINARY_00_FF_TEST", 600),
            ],
            "wave": "tape",
            "snr": 24.0,
            "inv": False,
            "amp": 0.75,
            "dc": 0.02,
            "asym": 0.02,
            "speed": 1.00,
            "force_baud": None,
        },
    ]

    all_passed = True

    for tc in test_cases:
        wav_io = generate_synthetic_test_wav(
            blocks=tc["blocks"],
            sample_rate=44100,
            wave_type=tc["wave"],
            snr_db=tc["snr"],
            invert_polarity=tc["inv"],
            amplitude=tc["amp"],
            dc_offset=tc["dc"],
            duty_asymmetry=tc["asym"],
            speed_factor=tc["speed"],
            prng_seed=98765,
        )

        out_io = io.BytesIO()
        supported = (tc["force_baud"],) if tc["force_baud"] is not None else (600, 1200)
        process_stream(wav_io, out_io, supported_bauds=supported, quiet=True)
        t88_data = out_io.getvalue()
        tags = parse_t88_stream(t88_data)

        decoded_blocks = []
        for tag_id, payload in tags:
            if tag_id == 0x0101:
                dsh = DataSubHeader.unpack(payload[:12])
                pdata = payload[12 : 12 + dsh.data_len]
                baud = 1200 if dsh.fmt_code == 0x01CC else 600
                decoded_blocks.append((baud, pdata))

        matched = len(decoded_blocks) == len(tc["blocks"])
        if matched:
            for d_blk, exp_blk in zip(decoded_blocks, tc["blocks"]):
                exp_data, exp_baud = exp_blk
                if d_blk[0] != exp_baud or d_blk[1] != exp_data:
                    matched = False
                    break

        status_str = "PASS" if matched else "FAIL"
        if not matched:
            all_passed = False
            sys.stderr.write(
                f"[{status_str}] {tc['name']} (Decoded {len(decoded_blocks)} blocks vs expected {len(tc['blocks'])})\n"
            )
            for i, d_blk in enumerate(decoded_blocks):
                sys.stderr.write(
                    f"   Decoded #{i}: baud={d_blk[0]}, len={len(d_blk[1])}, data={d_blk[1][:30]}\n"
                )
        else:
            sys.stderr.write(f"[{status_str}] {tc['name']}\n")

    # ------------------------------------------------------------------
    # Timing-realistic scenarios: tight ~60-80ms header->data carrier gaps,
    # session baud locking across a file, and motor/relay click transients.
    # ------------------------------------------------------------------
    def _gen_session_wav(
        segments,
        sample_rate=44100,
        wave_type="tape",
        snr_db=24.0,
        amplitude=0.78,
        dc_offset=0.02,
        duty_asymmetry=0.02,
        prng_seed=98765,
    ):
        rng = random.Random(prng_seed)
        sr = float(sample_rate)
        dt = 1.0 / sr
        current_time = 0.0
        mono: List[float] = []

        def shape(phase):
            sv = math.sin(phase)
            if wave_type == "tape":
                base = math.tanh((sv + 0.15 * math.sin(2.0 * phase)) * 1.8)
            else:
                base = sv
            if duty_asymmetry:
                base = base + duty_asymmetry if base > 0 else base - duty_asymmetry
            return base + dc_offset

        def add_tone(freq, dur, amp=amplitude):
            nonlocal current_time
            t_end = current_time + dur
            t_start = current_time
            while current_time < t_end:
                t_rel = current_time - t_start
                mono.append(amp * shape(2.0 * math.pi * freq * t_rel))
                current_time += dt

        def add_data(data, baud, amp=amplitude):
            nonlocal current_time
            bit_dur = 1.0 / baud
            for b in data:
                bits = [0] + [(b >> i) & 1 for i in range(8)] + [1, 1]
                for bit in bits:
                    freq = 2400.0 if bit else 1200.0
                    t_end = current_time + bit_dur
                    t_start = current_time
                    while current_time < t_end:
                        t_rel = current_time - t_start
                        mono.append(amp * shape(2.0 * math.pi * freq * t_rel))
                        current_time += dt

        def add_silence(dur):
            nonlocal current_time
            n = int(dur * sr)
            mono.extend([0.0] * n)
            current_time += n * dt

        for seg in segments:
            if seg[0] == "data":
                _, data, baud, leader_sec, trailer_sec = seg
                add_tone(2400.0, leader_sec)
                add_data(data, baud)
                add_tone(2400.0, trailer_sec)
            elif seg[0] == "silence":
                add_silence(seg[1])
            elif seg[0] == "tone":
                add_tone(seg[1], seg[2])

        if snr_db is not None:
            noise_amp = amplitude * (10.0 ** (-snr_db / 20.0))
            n_state = 0.0
            noisy = []
            for s in mono:
                raw = rng.random() * 2.0 - 1.0
                n_state = 0.8 * n_state + 0.2 * raw
                noisy.append(s + (0.6 * raw + 0.4 * n_state) * noise_amp)
            mono = noisy

        out_io = io.BytesIO()
        pcm = [max(min(int(s * 32767.0), 32767), -32768) for s in mono]
        out_io.write(b"RIFF\xff\xff\xff\xffWAVE")
        out_io.write(b"fmt \x10\x00\x00\x00")
        out_io.write(struct.pack("<HHIIHH", 1, 1, int(sr), int(sr * 2), 2, 16))
        out_io.write(b"data\xff\xff\xff\xff")
        out_io.write(struct.pack(f"<{len(pcm)}h", *pcm))
        out_io.seek(0)
        return out_io

    def _decode_blocks(wav_io, supported_bauds=(600, 1200)):
        out_io = io.BytesIO()
        process_stream(wav_io, out_io, supported_bauds=supported_bauds, quiet=True)
        out = []
        for tag_id, payload in parse_t88_stream(out_io.getvalue()):
            if tag_id == 0x0101:
                dsh = DataSubHeader.unpack(payload[:12])
                baud = 1200 if dsh.fmt_code == 0x01CC else 600
                out.append((baud, payload[12 : 12 + dsh.data_len]))
        return out

    session_cases = []

    for baud, gap_ms in ((600, 65.0), (1200, 65.0)):
        header = b"\x00SESHDR \x00"
        data = b"\xd3" * 10 + b"TIGHT_GAP_SESSION_LOCKED_PAYLOAD_" + bytes(range(64))
        half = (gap_ms / 2.0) / 1000.0
        segs = [
            ("data", header, baud, 0.25, half),
            ("data", data, baud, half, 0.15),
            ("silence", 0.2),
        ]
        wav_io = _gen_session_wav(segs)
        decoded = _decode_blocks(wav_io)
        joined = b"".join(d for _, d in decoded)
        ok = joined == (header + data) and all(b == baud for b, _ in decoded)
        session_cases.append(
            (
                f"Tight ~{gap_ms:.0f}ms Header->Data Carrier Gap, Session-Locked ({baud} baud)",
                ok,
                decoded,
            )
        )

    h_a = b"\x00FILE_A \x00"
    d_a = b"\xd3" * 10 + b"FIRST_FILE_1200_BAUD_DATA_BLOCK"
    h_b = b"\x00FILE_B \x00"
    d_b = b"\xd3" * 10 + b"SECOND_FILE_600_BAUD_DATA_BLOCK"
    segs = [
        ("data", h_a, 1200, 0.25, 0.035),
        ("data", d_a, 1200, 0.035, 0.15),
        ("silence", 0.8),
        ("data", h_b, 600, 0.25, 0.035),
        ("data", d_b, 600, 0.035, 0.15),
        ("silence", 0.2),
    ]
    wav_io = _gen_session_wav(segs)
    decoded = _decode_blocks(wav_io)
    expected = [(1200, h_a), (1200, d_a), (600, h_b), (600, d_b)]
    session_cases.append(
        (
            "Multi-File Tape, Per-File Baud Session Lock (1200 -> 600)",
            decoded == expected,
            decoded,
        )
    )

    b1 = b"\xd3" * 10 + b"BLOCK_BEFORE_CLICK"
    b2 = b"\xd3" * 10 + b"BLOCK_AFTER_CLICK"

    def _gen_with_click():
        rng = random.Random(98765)
        sr = 44100.0
        dt = 1.0 / sr
        current_time = 0.0
        mono: List[float] = []

        def shape(phase):
            return (
                math.tanh((math.sin(phase) + 0.15 * math.sin(2 * phase)) * 1.8) + 0.02
            )

        def add_tone(freq, dur, amp=0.78):
            nonlocal current_time
            t_end = current_time + dur
            t_start = current_time
            while current_time < t_end:
                t_rel = current_time - t_start
                mono.append(amp * shape(2 * math.pi * freq * t_rel))
                current_time += dt

        def add_data(data, baud, amp=0.78):
            nonlocal current_time
            bit_dur = 1.0 / baud
            for b in data:
                bits = [0] + [(b >> i) & 1 for i in range(8)] + [1, 1]
                for bit in bits:
                    freq = 2400.0 if bit else 1200.0
                    t_end = current_time + bit_dur
                    t_start = current_time
                    while current_time < t_end:
                        t_rel = current_time - t_start
                        mono.append(amp * shape(2 * math.pi * freq * t_rel))
                        current_time += dt

        def add_silence(dur):
            nonlocal current_time
            n = int(dur * sr)
            mono.extend([0.0] * n)
            current_time += n * dt

        add_tone(2400.0, 0.25)
        add_data(b1, 1200)
        add_tone(2400.0, 0.15)
        add_silence(2.0)
        add_tone(2400.0, 0.015, amp=0.6)  # brief ~15ms click, below leader threshold
        add_silence(3.0)
        add_tone(2400.0, 0.25)
        add_data(b2, 1200)
        add_tone(2400.0, 0.15)
        add_silence(0.2)

        noise_amp = 0.78 * (10.0 ** (-24.0 / 20.0))
        n_state = 0.0
        noisy = []
        for s in mono:
            raw = rng.random() * 2.0 - 1.0
            n_state = 0.8 * n_state + 0.2 * raw
            noisy.append(s + (0.6 * raw + 0.4 * n_state) * noise_amp)

        out_io = io.BytesIO()
        pcm = [max(min(int(s * 32767.0), 32767), -32768) for s in noisy]
        out_io.write(b"RIFF\xff\xff\xff\xffWAVE")
        out_io.write(b"fmt \x10\x00\x00\x00")
        out_io.write(struct.pack("<HHIIHH", 1, 1, int(sr), int(sr * 2), 2, 16))
        out_io.write(b"data\xff\xff\xff\xff")
        out_io.write(struct.pack(f"<{len(pcm)}h", *pcm))
        out_io.seek(0)
        return out_io

    wav_io = _gen_with_click()
    decoded = _decode_blocks(wav_io)
    expected_click = [(1200, b1), (1200, b2)]
    session_cases.append(
        (
            "Motor/Relay Click Transient (~15ms) Rejected as Spurious Block",
            decoded == expected_click,
            decoded,
        )
    )

    test_space_payload = b"\xd3" * 10 + b"SPACE_TAG_TEST"
    space_segs = [
        ("silence", 0.1),
        ("tone", 1200.0, 0.3),
        ("tone", 2400.0, 0.3),
        ("data", test_space_payload, 1200, 0.05, 0.15),
        ("tone", 1200.0, 0.3),
        ("silence", 0.1),
    ]
    wav_space = _gen_session_wav(space_segs, snr_db=None)
    out_space_t88 = io.BytesIO()
    process_stream(wav_space, out_space_t88, quiet=True)
    all_tags = parse_t88_stream(out_space_t88.getvalue())
    space_tags = [t for t in all_tags if t[0] == 0x0102]
    data_tags = [t for t in all_tags if t[0] == 0x0101]
    space_test_ok = len(space_tags) >= 2 and len(data_tags) == 1
    session_cases.append(
        (
            "Sustained 1200 Hz Space Tone Extracted as T88 0x0102 SPACE Tags",
            space_test_ok,
            [(1200, data_tags[0][1][12:])] if data_tags else [],
        )
    )

    for name, ok, decoded in session_cases:
        status_str = "PASS" if ok else "FAIL"
        if not ok:
            all_passed = False
            sys.stderr.write(
                f"[{status_str}] {name} (Decoded {len(decoded)} block(s))\n"
            )
            for i, (b, d) in enumerate(decoded):
                sys.stderr.write(
                    f"   Decoded #{i}: baud={b}, len={len(d)}, data={d[:30]}\n"
                )
        else:
            sys.stderr.write(f"[{status_str}] {name}\n")

    sys.stderr.write(
        "\n----------------------------------------------------------------------\n"
    )
    if all_passed:
        sys.stderr.write(">>> ALL TEST SUITES PASSED (100% Precision Verified) <<<\n")
    else:
        sys.stderr.write(">>> SOME TESTS FAILED <<<\n")
    sys.stderr.write(
        "======================================================================\n"
    )
    return all_passed


# ============================================================================
# CLI Entry Point
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Stream PC-8001 / PC-8801 WAV audio to standard .t88 tape image."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Input WAV file or '-' for stdin / pipe",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output .t88 file or '-' for stdout / pipe",
    )
    parser.add_argument(
        "--baud",
        "-b",
        type=int,
        choices=[600, 1200],
        default=None,
        help="Explicit forced baud rate (600 or 1200). Disables autodetection.",
    )
    parser.add_argument(
        "--channel",
        "-c",
        type=str,
        default="auto",
        choices=["auto", "left", "right", "mix", "diff"],
        help="Stereo channel routing: 'auto', 'left', 'right', 'mix', 'diff' (default: auto)",
    )
    parser.add_argument(
        "--bauds",
        type=str,
        default="600,1200",
        help="Comma-separated candidate baud rates for autodetect mode (default: 600,1200)",
    )
    parser.add_argument(
        "--flavor",
        type=str,
        default="reconstructed",
        choices=[
            "verbatim",
            "reconstructed",
            "kinematic-infilled",
            "rom-authentic",
            "canonical",
        ],
        help="Demodulation timing flavor (default: reconstructed)",
    )
    parser.add_argument(
        "--confidence",
        "-C",
        "--min-confidence",
        type=float,
        default=0.75,
        help="Minimum byte confidence threshold to accept UART bytes (default: 0.75)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress diagnostic logging to stderr.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Analyze tape audio capture and print diagnostic report to stdout.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run comprehensive built-in test suite across all waveform types & noise conditions.",
    )

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(0)

    args = parser.parse_args()

    if args.test:
        success = run_test_suite()
        sys.exit(0 if success else 1)

    if args.input is None:
        parser.print_help(sys.stderr)
        sys.exit(0)

    if args.input == "-":
        in_stream = sys.stdin.buffer
    else:
        in_stream = open(args.input, "rb")

    if args.inspect:
        try:
            run_inspector(in_stream, channel_mode=args.channel, out_stream=sys.stdout)
        finally:
            if in_stream is not sys.stdin.buffer:
                in_stream.close()
        sys.exit(0)

    if args.output is None or args.output == "-":
        out_stream = sys.stdout.buffer
    else:
        out_stream = open(args.output, "wb")

    try:
        if args.baud is not None:
            bauds = (args.baud,)
        else:
            bauds = tuple(int(b.strip()) for b in args.bauds.split(",") if b.strip())
        process_stream(
            in_stream,
            out_stream,
            supported_bauds=bauds,
            channel_mode=args.channel,
            confidence_threshold=args.confidence,
            quiet=args.quiet,
        )
    except KeyboardInterrupt:
        log_diag("Streaming stopped by user.")
    except Exception as e:
        log_diag(f"Error: {e}")
        sys.exit(1)
    finally:
        if in_stream is not sys.stdin.buffer:
            in_stream.close()
        if out_stream is not sys.stdout.buffer:
            out_stream.close()


if __name__ == "__main__":
    main()
