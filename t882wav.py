#!/usr/bin/env python3
"""
PC-8001 / PC-8801 T88 Cassette Image to Audio WAV Streaming Converter
=======================================================================

Overview & Architecture
-----------------------
Real-time streaming audio synthesizer converting NEC PC-8001 / PC-8801 series
standard .t88 tape container images into standard uncompressed RIFF/WAVE audio.

Hardware & Signal Specifications:
- FSK Modulation: Mark = 2400 Hz (Logic 1), Space = 1200 Hz (Logic 0).
- Supported CMT Baud Rates: 1200 baud and 600 baud.
- 600 baud is strictly pulse-doubled (bit-doubled) 1200 baud:
  * 1200 baud: 1 Mark bit = 2 cycles of 2400 Hz; 1 Space bit = 1 cycle of 1200 Hz.
  *  600 baud: 1 Mark bit = 4 cycles of 2400 Hz; 1 Space bit = 2 cycles of 1200 Hz.
- Frame Format: 1 Start bit (0 / Space), 8 Data bits (LSB-first), 2 Stop bits (1 / Mark).
- T88 Clock: 4800 ticks/sec (1 tick = 1/4800 s, exactly one half-cycle of 2400 Hz).
  * 1200 baud: 44 ticks/byte (fmt 0x01CC).
  *  600 baud: 88 ticks/byte (fmt 0x00CC).

Three Waveform Synthesis Modes:
1. 'tape' / 'cassette' (Default):
   Simulates magnetic tape saturation and cassette playback head induction.
   Uses a smooth tanh saturation curve with natural harmonic richness.
   Ideal for playing into real retro computers, cassette tape recorders,
   or software demodulators (e.g. wav2t88 / emulators).
2. 'shaped' / 'pc':
   Simulates the analog output circuitry of the physical PC-8001 / PC-8801 hardware.
   Models the RC low-pass edge smoothing and AC-coupling capacitor droop/sag.
3. 'ideal' / 'square':
   Pure digital square wave directly from the PC 8255 / 8251 CMT OUT port.
   Ideal for chaining into external physical DSP cassette channel modelers
   (such as wav2cas / cassette_modeler.py).

Streaming & Header Updating:
- Streams sample blocks directly to the output without buffering the entire audio in RAM.
- Emits standard WAV header with streaming placeholder sizes (0xFFFFFFFF) initially.
- At completion, automatically updates the header with exact file/sample count if the
  destination is seekable (file), or gracefully preserves the streaming header if
  unseekable (stdout / pipe / FIFO).
"""

import sys
import os
import io
import math
import struct
import argparse
from typing import BinaryIO, Optional, Tuple, List, Generator

# Prevent writing bytecode (.pyc) files into the working directory
sys.dont_write_bytecode = True


# ============================================================================
# T88 Tag Identifiers & Constants
# ============================================================================


class T88Tag:
    END: int = 0x0000  # Terminal block marker
    VERSION: int = 0x0001  # Version info (uint16)
    GAP: int = 0x0100  # Blank / silence interval (start_tick, length_ticks)
    DATA: int = 0x0101  # Serial UART data block (start_tick, length_ticks, dlen, fmt)
    SPACE: int = 0x0102  # 1200 Hz Space tone burst (start_tick, length_ticks)
    MARK: int = 0x0103  # 2400 Hz Mark carrier tone (start_tick, length_ticks)
    COMMENT: int = 0x0010  # UTF-8 / ASCII annotation text


T88_HEADER_MAGICS: Tuple[bytes, ...] = (
    b"PC-8801 Tape Image(T88)\x00",
    b"PC-8001 Tape Image(T88)\x00",
    b"PC-8801 ",
    b"T88-FILE",
    b"PC-8001 ",
)


# ============================================================================
# Streaming T88 Reader
# ============================================================================


class StreamingT88Reader:
    """
    Incremental streaming reader for T88 tape image containers.
    Supports seekable files, unseekable pipes, and stdin streams.
    """

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.header_bytes = b""
        self._parse_header()

    def _read_exact(self, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = self.stream.read(count - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _parse_header(self):
        hdr = self._read_exact(24)
        if len(hdr) < 24:
            raise ValueError(
                "Input stream is too short to be a valid T88 container (<24 bytes)."
            )

        valid = False
        for magic in T88_HEADER_MAGICS:
            if hdr.startswith(magic) or hdr[: len(magic)] == magic:
                valid = True
                break
        if not valid and not (hdr.startswith(b"PC-") or hdr.startswith(b"T88")):
            raise ValueError(
                f"Invalid T88 magic signature: got {hdr!r}. Expected standard 'PC-8801 Tape Image(T88)'."
            )
        self.header_bytes = hdr

    def iter_blocks(self) -> Generator[Tuple[int, int, bytes], None, None]:
        """
        Yields (tag_id, length, payload) for each tagged block in the T88 stream.
        """
        while True:
            tag_hdr = self._read_exact(4)
            if len(tag_hdr) < 4:
                break
            tag_id, length = struct.unpack("<HH", tag_hdr)
            payload = self._read_exact(length) if length > 0 else b""
            if len(payload) < length:
                # Truncated payload at EOF
                break

            yield (tag_id, length, payload)
            if tag_id == T88Tag.END:
                break


# ============================================================================
# Streaming WAV Writer with Graceful Header Finalization
# ============================================================================


class StreamingWavWriter:
    """
    Incremental streaming RIFF/WAVE writer.
    - Writes standard 16-bit PCM WAV chunks to output stream.
    - Initially outputs streaming placeholder 0xFFFFFFFF for chunk sizes.
    - On finalize(), seeks back to write exact chunk sizes if seekable,
      or leaves streaming header intact if output is a pipe/stdout.
    """

    def __init__(
        self,
        out_stream: BinaryIO,
        sample_rate: int = 44100,
        channels: int = 1,
        stereo_mode: str = "dual",
    ):
        self.out = out_stream
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.stereo_mode = stereo_mode.lower()
        self.bits_per_sample = 16
        self.bytes_per_sample = 2
        self.block_align = self.channels * self.bytes_per_sample
        self.byte_rate = self.sample_rate * self.block_align

        self.total_frames_written = 0
        self.total_pcm_bytes_written = 0
        self.header_written = False

        self._write_initial_header()

    def _write_initial_header(self):
        # 12 bytes RIFF header + 24 bytes fmt chunk + 8 bytes data header = 44 bytes
        # Streaming placeholder sizes = 0xFFFFFFFF
        placeholder_size = 0xFFFFFFFF
        hdr = bytearray()
        hdr.extend(b"RIFF")
        hdr.extend(struct.pack("<I", placeholder_size))
        hdr.extend(b"WAVE")
        hdr.extend(b"fmt ")
        hdr.extend(struct.pack("<I", 16))  # Subchunk1Size for PCM
        hdr.extend(
            struct.pack(
                "<HHIIHH",
                1,  # AudioFormat: 1 = PCM
                self.channels,  # NumChannels
                self.sample_rate,  # SampleRate
                self.byte_rate,  # ByteRate
                self.block_align,  # BlockAlign
                self.bits_per_sample,  # BitsPerSample
            )
        )
        hdr.extend(b"data")
        hdr.extend(struct.pack("<I", placeholder_size))

        self.out.write(bytes(hdr))
        self.out.flush()
        self.header_written = True

    def write_pcm_samples(self, mono_floats: List[float]):
        """
        Converts floating-point samples [-1.0, 1.0] to 16-bit PCM and streams out.
        """
        if not mono_floats:
            return

        num_frames = len(mono_floats)
        # Quantize to 16-bit signed integer [-32768, 32767]
        quantized = [
            max(-32768, min(32767, int(round(s * 32767.0)))) for s in mono_floats
        ]

        if self.channels == 1:
            raw_bytes = struct.pack(f"<{num_frames}h", *quantized)
        elif self.channels == 2:
            stereo_pcm = []
            smode = self.stereo_mode
            if smode in ("dual", "both", "mono", "center"):
                for v in quantized:
                    stereo_pcm.extend([v, v])
            elif smode in ("left", "l", "0"):
                for v in quantized:
                    stereo_pcm.extend([v, 0])
            elif smode in ("right", "r", "1"):
                for v in quantized:
                    stereo_pcm.extend([0, v])
            elif smode in ("inv_right", "diff", "invert_r"):
                for v in quantized:
                    stereo_pcm.extend([v, -v])
            else:
                for v in quantized:
                    stereo_pcm.extend([v, v])
            raw_bytes = struct.pack(f"<{num_frames * 2}h", *stereo_pcm)
        else:
            raw_bytes = struct.pack(f"<{num_frames}h", *quantized)

        self.out.write(raw_bytes)
        self.total_frames_written += num_frames
        self.total_pcm_bytes_written += len(raw_bytes)

    def finalize(self) -> bool:
        """
        Attempts to update the WAV header with exact sizes if seekable.
        Returns True if seek was successful, False if stream was unseekable (pipe).
        """
        self.out.flush()
        is_seekable = False
        try:
            if hasattr(self.out, "seekable"):
                is_seekable = self.out.seekable()
            else:
                self.out.seek(0, os.SEEK_CUR)
                is_seekable = True
        except (io.UnsupportedOperation, OSError, AttributeError):
            is_seekable = False

        if not is_seekable:
            return False

        try:
            cur_pos = self.out.tell()
            # Update RIFF size at byte offset 4: (total_pcm_bytes + 36)
            riff_size = self.total_pcm_bytes_written + 36
            # Avoid 32-bit overflow if size exceeds 4 GB
            riff_size_field = min(0xFFFFFFFF, riff_size)
            data_size_field = min(0xFFFFFFFF, self.total_pcm_bytes_written)

            self.out.seek(4, os.SEEK_SET)
            self.out.write(struct.pack("<I", riff_size_field))

            # Update data size at byte offset 40
            self.out.seek(40, os.SEEK_SET)
            self.out.write(struct.pack("<I", data_size_field))

            # Seek back to original end position
            self.out.seek(cur_pos, os.SEEK_SET)
            self.out.flush()
            return True
        except (io.UnsupportedOperation, OSError, AttributeError):
            return False


# ============================================================================
# FSK & Waveform Synthesizer (Tape, Shaped PC, Ideal Square)
# ============================================================================


class T88ToWavSynthesizer:
    """
    Sample-accurate FSK synthesizer converting T88 timing & data tags into audio.
    Maintains exact sub-sample fractional time accumulator to eliminate cumulative drift.
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        mode: str = "tape",
        amplitude: float = 0.8,
        speed_factor: float = 1.0,
        invert_polarity: bool = False,
    ):
        self.sr = float(sample_rate)
        self.dt = 1.0 / self.sr
        self.mode = mode.lower()
        self.amplitude = max(0.01, min(1.0, float(amplitude)))
        self.speed = max(0.5, min(2.0, float(speed_factor)))
        self.invert = bool(invert_polarity)

        self.current_time = 0.0
        self.current_tick = 0

        # Shaped PC Analog Circuit Filter States:
        # 1st-order RC lowpass filter (~6000 Hz) + AC coupling DC blocker (~150 Hz)
        rc_cutoff_hz = 6000.0
        rc_w = 2.0 * math.pi * rc_cutoff_hz
        self.lp_alpha = (rc_w * self.dt) / (1.0 + rc_w * self.dt)
        self.rc_lp = 0.0
        self.dc_x1 = 0.0
        self.dc_y1 = 0.0

    def shape_sample(self, phase: float) -> float:
        """
        Transforms instantaneous phase into an audio sample based on active mode.
        """
        sin_val = math.sin(phase)
        base = 0.0

        if self.mode in ("ideal", "square", "direct"):
            base = 1.0 if sin_val >= 0.0 else -1.0
        elif self.mode in ("tape", "cassette", "readback"):
            # Tape saturation: tanh soft-clipping + 2nd harmonic magnetic asymmetry
            s = sin_val + 0.15 * math.sin(2.0 * phase)
            base = math.tanh(s * 1.8)
        elif self.mode in ("shaped", "pc", "circuit"):
            # PC analog stage: digital square -> RC lowpass edge smoothing -> AC highpass tilt
            raw = 1.0 if sin_val >= 0.0 else -1.0
            self.rc_lp += self.lp_alpha * (raw - self.rc_lp)
            hp_y = self.rc_lp - self.dc_x1 + 0.995 * self.dc_y1
            self.dc_x1 = self.rc_lp
            self.dc_y1 = hp_y
            base = hp_y
        else:
            base = sin_val

        out = base * self.amplitude
        if self.invert:
            out = -out
        return out

    def generate_silence(self, duration_sec: float) -> List[float]:
        if duration_sec <= 0.0:
            return []
        samples = []
        t_end = self.current_time + duration_sec
        while self.current_time < t_end:
            samples.append(0.0)
            self.current_time += self.dt
        return samples

    def generate_tone(self, freq: float, duration_sec: float) -> List[float]:
        if duration_sec <= 0.0:
            return []
        samples = []
        actual_freq = freq * self.speed
        t_end = self.current_time + duration_sec
        t_start = self.current_time
        two_pi_f = 2.0 * math.pi * actual_freq

        while self.current_time < t_end:
            t_rel = self.current_time - t_start
            phase = two_pi_f * t_rel
            samples.append(self.shape_sample(phase))
            self.current_time += self.dt
        return samples

    def generate_uart_data(self, data_bytes: bytes, baud: int) -> List[float]:
        if not data_bytes:
            return []
        samples = []
        actual_baud = baud * self.speed
        bit_dur = 1.0 / actual_baud
        f_mark = 2400.0 * self.speed
        f_space = 1200.0 * self.speed
        two_pi_mark = 2.0 * math.pi * f_mark
        two_pi_space = 2.0 * math.pi * f_space

        for b in data_bytes:
            # 1 Start bit (0 / Space), 8 Data bits (LSB-first), 2 Stop bits (1 / Mark)
            bits = [0] + [(b >> i) & 1 for i in range(8)] + [1, 1]
            for bit in bits:
                two_pi_f = two_pi_mark if bit == 1 else two_pi_space
                t_end = self.current_time + bit_dur
                t_start = self.current_time
                while self.current_time < t_end:
                    t_rel = self.current_time - t_start
                    phase = two_pi_f * t_rel
                    samples.append(self.shape_sample(phase))
                    self.current_time += self.dt
        return samples


# ============================================================================
# Main Processing Engine
# ============================================================================


def log_diag(msg: str):
    sys.stderr.write(f"[t882wav] {msg}\n")
    sys.stderr.flush()


def format_time_ticks(ticks: int) -> str:
    sec = ticks / 4800.0
    m = int(sec // 60)
    s = sec % 60
    return f"{m:02d}:{s:06.3f}"


def convert_t88_to_wav(
    in_stream: BinaryIO,
    out_stream: BinaryIO,
    mode: str = "tape",
    sample_rate: int = 44100,
    channels: int = 1,
    stereo_mode: str = "dual",
    amplitude: float = 0.8,
    speed_factor: float = 1.0,
    invert_polarity: bool = False,
    baud_override: Optional[int] = None,
    chunk_frames: int = 4096,
    quiet: bool = False,
):
    reader = StreamingT88Reader(in_stream)
    writer = StreamingWavWriter(
        out_stream,
        sample_rate=sample_rate,
        channels=channels,
        stereo_mode=stereo_mode,
    )
    synth = T88ToWavSynthesizer(
        sample_rate=sample_rate,
        mode=mode,
        amplitude=amplitude,
        speed_factor=speed_factor,
        invert_polarity=invert_polarity,
    )

    if not quiet:
        chan_desc = (
            "Mono (1-ch)" if channels == 1 else f"Stereo (2-ch, {stereo_mode.upper()})"
        )
        log_diag(
            f"Synthesizing: {sample_rate} Hz, 16-bit, {chan_desc}, Mode: {mode.upper()}, "
            f"Amplitude: {amplitude:.2f}, Speed: {speed_factor:.3f}x"
        )

    current_tick = 0
    total_data_bytes = 0
    data_block_count = 0
    mark_tag_count = 0
    space_tag_count = 0
    gap_tag_count = 0
    last_progress_tick = 0

    # Stream buffer for writing in uniform chunks
    sample_buffer: List[float] = []

    def flush_samples(force: bool = False):
        nonlocal sample_buffer
        while len(sample_buffer) >= chunk_frames or (force and sample_buffer):
            num_to_write = len(sample_buffer) if force else chunk_frames
            chunk = sample_buffer[:num_to_write]
            writer.write_pcm_samples(chunk)
            sample_buffer = sample_buffer[num_to_write:]

    for tag_id, length, payload in reader.iter_blocks():
        if tag_id in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK):
            if len(payload) >= 8:
                st, lt = struct.unpack("<II", payload[:8])
                # Inter-block timeline synchronization
                if st > current_tick:
                    gap_ticks = st - current_tick
                    sample_buffer.extend(synth.generate_silence(gap_ticks / 4800.0))
                    current_tick = st

                dur_sec = lt / 4800.0
                if tag_id == T88Tag.GAP:
                    gap_tag_count += 1
                    sample_buffer.extend(synth.generate_silence(dur_sec))
                elif tag_id == T88Tag.SPACE:
                    space_tag_count += 1
                    sample_buffer.extend(synth.generate_tone(1200.0, dur_sec))
                elif tag_id == T88Tag.MARK:
                    mark_tag_count += 1
                    sample_buffer.extend(synth.generate_tone(2400.0, dur_sec))

                current_tick = st + lt
                flush_samples()

        elif tag_id == T88Tag.DATA:
            if len(payload) >= 12:
                st, lt, dlen, fmt = struct.unpack("<IIHH", payload[:12])
                pdata = payload[12 : 12 + dlen]

                if st > current_tick:
                    gap_ticks = st - current_tick
                    sample_buffer.extend(synth.generate_silence(gap_ticks / 4800.0))
                    current_tick = st

                # Determine effective baud rate: PC-8001/PC-8801 strictly supports 1200 or 600 baud
                if baud_override in (600, 1200):
                    eff_baud = baud_override
                elif fmt == 0x00CC:
                    eff_baud = 600
                elif fmt == 0x01CC:
                    eff_baud = 1200
                elif dlen > 0 and lt > 0:
                    ticks_per_byte = lt / dlen
                    # 88 ticks/byte = 600 baud, 44 ticks/byte = 1200 baud
                    eff_baud = (
                        600
                        if abs(ticks_per_byte - 88) < abs(ticks_per_byte - 44)
                        else 1200
                    )
                else:
                    eff_baud = 1200

                sample_buffer.extend(synth.generate_uart_data(pdata, eff_baud))
                total_data_bytes += len(pdata)
                data_block_count += 1
                current_tick = st + lt

                if not quiet:
                    log_diag(
                        f"Data Block {data_block_count:2d}: {len(pdata):5d} bytes [{eff_baud} baud] "
                        f"at {format_time_ticks(st)}"
                    )

                flush_samples()

        elif tag_id == T88Tag.COMMENT:
            if not quiet and payload:
                comment_str = payload.decode("utf-8", errors="ignore").strip()
                log_diag(f"Comment: {comment_str}")

        # Throttled periodic progress
        if not quiet and (current_tick - last_progress_tick) >= (4800 * 15):
            last_progress_tick = current_tick
            log_diag(
                f"Progress: {format_time_ticks(current_tick)} "
                f"({total_data_bytes} bytes in {data_block_count} blocks synthesized)"
            )

    # Flush any remaining audio samples
    flush_samples(force=True)

    # Finalize WAV header (seek back and update chunk sizes if seekable)
    seek_ok = writer.finalize()
    dur_sec = writer.total_frames_written / sample_rate
    m = int(dur_sec // 60)
    s = dur_sec % 60

    if not quiet:
        hdr_status = (
            "Exact Size Updated (Seekable File)"
            if seek_ok
            else "Streaming Header Preserved (Pipe/Stdout)"
        )
        log_diag(
            f"Finished: {writer.total_frames_written:,} samples ({m:02d}:{s:06.3f} duration), "
            f"{total_data_bytes:,} data bytes across {data_block_count} blocks. [{hdr_status}]"
        )


# ============================================================================
# Tape Inspector (--inspect / --info)
# Outputs report directly to stdout
# ============================================================================


def run_inspector(in_stream: BinaryIO, out_stream=sys.stdout):
    reader = StreamingT88Reader(in_stream)

    print(
        "======================================================================",
        file=out_stream,
    )
    print(
        "               PC-8001 / PC-8801 T88 IMAGE INSPECTOR                  ",
        file=out_stream,
    )
    print(
        "======================================================================",
        file=out_stream,
    )
    print(f"Magic Signature: {reader.header_bytes.rstrip(b'\x00')!r}", file=out_stream)

    total_ticks = 0
    total_data_bytes = 0
    block_index = 0
    carrier_marks = 0
    carrier_spaces = 0
    gaps = 0
    comments = []
    blocks_info = []

    for tag_id, length, payload in reader.iter_blocks():
        block_index += 1
        if tag_id == T88Tag.VERSION:
            ver = struct.unpack("<H", payload[:2])[0] if len(payload) >= 2 else 0
            blocks_info.append(f"  #{block_index:03d} | VERSION | 0x{ver:04X}")
        elif tag_id == T88Tag.COMMENT:
            c_text = payload.decode("utf-8", errors="ignore").strip()
            comments.append(c_text)
            blocks_info.append(f"  #{block_index:03d} | COMMENT | {c_text!r}")
        elif tag_id in (T88Tag.GAP, T88Tag.SPACE, T88Tag.MARK):
            if len(payload) >= 8:
                st, lt = struct.unpack("<II", payload[:8])
                total_ticks = max(total_ticks, st + lt)
                tname = (
                    "GAP"
                    if tag_id == T88Tag.GAP
                    else ("SPACE" if tag_id == T88Tag.SPACE else "MARK")
                )
                if tag_id == T88Tag.MARK:
                    carrier_marks += 1
                elif tag_id == T88Tag.SPACE:
                    carrier_spaces += 1
                elif tag_id == T88Tag.GAP:
                    gaps += 1
                dur_s = lt / 4800.0
                blocks_info.append(
                    f"  #{block_index:03d} | {tname:<7} | tick {st:8d}..{st+lt:<8d} ({lt:6d} ticks, {dur_s:6.3f}s)"
                )
        elif tag_id == T88Tag.DATA:
            if len(payload) >= 12:
                st, lt, dlen, fmt = struct.unpack("<IIHH", payload[:12])
                total_ticks = max(total_ticks, st + lt)
                total_data_bytes += dlen
                eff_baud = 600 if fmt == 0x00CC else 1200
                dur_s = lt / 4800.0
                pdata = payload[12 : 12 + dlen]
                preview = repr(pdata[:16])
                blocks_info.append(
                    f"  #{block_index:03d} | DATA    | tick {st:8d}..{st+lt:<8d} ({lt:6d} ticks, {dur_s:6.3f}s) | "
                    f"dlen={dlen:5d} [{eff_baud} baud] {preview}"
                )
        elif tag_id == T88Tag.END:
            blocks_info.append(f"  #{block_index:03d} | END")

    dur_sec = total_ticks / 4800.0
    m = int(dur_sec // 60)
    s = dur_sec % 60

    print(
        f"Total Tape Duration: {m:02d}:{s:06.3f} ({total_ticks:,} ticks @ 4800 Hz)",
        file=out_stream,
    )
    print(f"Total Data Payload : {total_data_bytes:,} bytes", file=out_stream)
    print(
        f"Carrier Tone Tags  : {carrier_marks} Mark (2400 Hz), {carrier_spaces} Space (1200 Hz), {gaps} Blank Gaps",
        file=out_stream,
    )
    if comments:
        print(f"Embedded Comments  : {'; '.join(comments)}", file=out_stream)

    print("\n--- T88 Tag Sequence Breakdown ---", file=out_stream)
    for line in blocks_info:
        print(line, file=out_stream)
    print(
        "======================================================================",
        file=out_stream,
    )


# ============================================================================
# Built-In Test Suite (--test)
# ============================================================================


def run_test_suite() -> bool:
    sys.stderr.write(
        "======================================================================\n"
    )
    sys.stderr.write(
        "        PC-8001 / PC-8801 T88-to-WAV Synthesizer Test Suite          \n"
    )
    sys.stderr.write(
        "======================================================================\n\n"
    )

    # We test against input_file_0 (wav2t88) and input_file_1 (pc88_tape_tools) if present
    try:
        import input_file_0
        import input_file_1

        has_tools = True
    except ImportError:
        has_tools = False

    all_passed = True

    def report_test(name: str, passed: bool, detail: str = ""):
        nonlocal all_passed
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        sys.stderr.write(f"[{status}] {name}")
        if detail:
            sys.stderr.write(f" -> {detail}")
        sys.stderr.write("\n")

    # 1. Unseekable / Pipe WAV Header Writer Test
    class MockUnseekableStream(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, offset, whence=0):
            raise io.UnsupportedOperation("Unseekable stream")

    unseekable_io = MockUnseekableStream()
    w_unseek = StreamingWavWriter(unseekable_io, sample_rate=44100, channels=1)
    w_unseek.write_pcm_samples([0.5, -0.5, 0.0] * 100)
    finalized_unseek = w_unseek.finalize()
    val_unseek = unseekable_io.getvalue()
    unseek_ok = (
        (not finalized_unseek)
        and len(val_unseek) == (44 + 300 * 2)
        and val_unseek[:4] == b"RIFF"
    )
    report_test("Unseekable Output Stream (Pipe/Stdout) Streaming Header", unseek_ok)

    # 2. Seekable File Header Update Test
    seekable_io = io.BytesIO()
    w_seek = StreamingWavWriter(seekable_io, sample_rate=44100, channels=1)
    w_seek.write_pcm_samples([0.5, -0.5, 0.0] * 100)
    finalized_seek = w_seek.finalize()
    val_seek = seekable_io.getvalue()
    (r_size,) = struct.unpack("<I", val_seek[4:8])
    (d_size,) = struct.unpack("<I", val_seek[40:44])
    seek_ok = finalized_seek and (d_size == 300 * 2) and (r_size == d_size + 36)
    report_test(
        "Seekable Output Stream (File) Exact Header Size Update",
        seek_ok,
        f"riff_size={r_size}, data_size={d_size}",
    )

    # 3. Stereo Channel Routing Modes Test
    stereo_io = io.BytesIO()
    w_stereo = StreamingWavWriter(
        stereo_io, sample_rate=44100, channels=2, stereo_mode="dual"
    )
    w_stereo.write_pcm_samples([1.0, -1.0])
    w_stereo.finalize()
    s_val = stereo_io.getvalue()
    stereo_ok = (len(s_val) == 44 + 4 * 2) and struct.unpack("<HH", s_val[44:48]) == (
        32767,
        32767,
    )
    report_test("Stereo Dual-Mono Channel Routing", stereo_ok)

    # 4. Inspector Output Destination (Must write to stdout)
    dummy_t88 = b"PC-8801 Tape Image(T88)\x00" + struct.pack("<HH", T88Tag.END, 0)
    inspect_stdout_buf = io.StringIO()
    run_inspector(io.BytesIO(dummy_t88), out_stream=inspect_stdout_buf)
    inspect_ok = "T88 IMAGE INSPECTOR" in inspect_stdout_buf.getvalue()
    report_test("Tape Inspector Output to Stdout", inspect_ok)

    # 5. Synthesizer Modes on 1200 and 600 Baud T88 Test Data (Bidirectional Verification)
    if has_tools:
        test_payloads = [
            (
                "1200 Baud BASIC Program",
                b"\xd3" * 10 + b"TESTPRG" + b"\x90 HELLO WORLD\x00\x00\x00",
                1200,
            ),
            (
                "600 Baud MON Machine Language",
                b"\x24" * 10
                + b"BIN001"
                + b"\x3a\x80\x00\x80"
                + b"\x3a\x04\x01\x02\x03\x04\xec"
                + b"\x3a\x00\x00",
                600,
            ),
            (
                "9-Byte Short File Header Block (1200 baud)",
                b"\x00TESTHD \x00\x00",
                1200,
            ),
            ("9-Byte Short File Header Block (600 baud)", b"\x00TEST60 \x00\x00", 600),
            (
                "Binary 0x00 and 0xFF Sequence (1200 baud)",
                b"\x00" * 32 + b"\xff" * 32 + b"\xaa\x55" * 16,
                1200,
            ),
            (
                "Binary 0x00 and 0xFF Sequence (600 baud)",
                b"\x00" * 16 + b"\xff" * 16 + b"\xaa\x55" * 8,
                600,
            ),
        ]

        for desc, payload_data, baud in test_payloads:
            t88_obj = input_file_1.T88File.from_cmt_data(payload_data, baud=baud)
            t88_in_bytes = t88_obj.pack()

            for synth_mode in ("tape", "shaped", "ideal"):
                wav_out = io.BytesIO()
                convert_t88_to_wav(
                    io.BytesIO(t88_in_bytes),
                    wav_out,
                    mode=synth_mode,
                    sample_rate=44100,
                    channels=1,
                    quiet=True,
                )
                wav_bytes = wav_out.getvalue()

                # Demodulate WAV back to T88 with input_file_0
                t88_demod_out = io.BytesIO()
                input_file_0.process_stream(
                    io.BytesIO(wav_bytes), t88_demod_out, quiet=True
                )
                demod_bytes = t88_demod_out.getvalue()

                # Extract payload with input_file_1
                demod_file = input_file_1.T88File.unpack(io.BytesIO(demod_bytes))
                demod_payload = demod_file.extract_cmt_payload()

                matched = demod_payload == payload_data
                report_test(
                    f"Roundtrip: {desc} [{synth_mode.upper()} Mode]",
                    matched,
                    f"Payload: {len(demod_payload)}/{len(payload_data)} bytes",
                )

        # 6. Multi-Block Session with Tight Carrier Gap Test (1200 and 600 baud)
        for b_rate in (1200, 600):
            fmt_code = 0x01CC if b_rate == 1200 else 0x00CC
            tpb = 44 if b_rate == 1200 else 88
            hdr_bytes = b"\x00HDR01  \x00"
            data_body = b"\xd3" * 10 + b"BODY_DATA_PAYLOAD_CHUNK"

            h1 = (
                struct.pack(
                    "<IIHH", 4800, len(hdr_bytes) * tpb, len(hdr_bytes), fmt_code
                )
                + hdr_bytes
            )
            data_start = 4800 + len(hdr_bytes) * tpb + 320
            h2 = (
                struct.pack(
                    "<IIHH", data_start, len(data_body) * tpb, len(data_body), fmt_code
                )
                + data_body
            )

            multi_blocks = [
                input_file_1.T88Block(
                    input_file_1.T88Tag.VERSION, struct.pack("<H", 0x0100)
                ),
                input_file_1.T88Block(
                    input_file_1.T88Tag.MARK, struct.pack("<II", 0, 4800)
                ),
                input_file_1.T88Block(
                    (
                        input_file_1.T88Tag.DATA_1200
                        if b_rate == 1200
                        else input_file_1.T88Tag.DATA_300
                    ),
                    h1,
                ),
                input_file_1.T88Block(
                    input_file_1.T88Tag.MARK,
                    struct.pack("<II", 4800 + len(hdr_bytes) * tpb, 320),
                ),  # ~66ms carrier
                input_file_1.T88Block(
                    (
                        input_file_1.T88Tag.DATA_1200
                        if b_rate == 1200
                        else input_file_1.T88Tag.DATA_300
                    ),
                    h2,
                ),
                input_file_1.T88Block(
                    input_file_1.T88Tag.MARK,
                    struct.pack("<II", data_start + len(data_body) * tpb, 2400),
                ),
                input_file_1.T88Block(input_file_1.T88Tag.END, b""),
            ]
            multi_t88 = input_file_1.T88File(blocks=multi_blocks).pack()

            wav_multi = io.BytesIO()
            convert_t88_to_wav(
                io.BytesIO(multi_t88), wav_multi, mode="tape", quiet=True
            )
            demod_multi = io.BytesIO()
            input_file_0.process_stream(
                io.BytesIO(wav_multi.getvalue()), demod_multi, quiet=True
            )
            demod_multi_file = input_file_1.T88File.unpack(
                io.BytesIO(demod_multi.getvalue())
            )
            multi_matched = (
                demod_multi_file.extract_cmt_payload() == hdr_bytes + data_body
            )
            report_test(
                f"Multi-Block Session with Tight Carrier Gap ({b_rate} baud)",
                multi_matched,
            )

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
        prog="t882wav.py",
        description="Stream PC-8001 / PC-8801 .t88 tape container image to standard WAV audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Waveform Modes:
  tape    : (Default) Simulates magnetic tape saturation & playback head response.
  shaped  : Simulates PC-8001 / PC-8801 analog RC output buffer stage & AC droop.
  ideal   : Pure mathematical square wave from the PC 8255 / 8251 CMT OUT port.

Baud Rates:
  PC-8001 / PC-8801 hardware and .t88 format standardly support 1200 baud and
  600 baud (pulse-doubled 1200 baud).

Examples:
  # Stream file to file in default tape saturation mode:
  t882wav.py game.t88 game.wav

  # Stream from stdin to stdout via pipe:
  cat game.t88 | t882wav.py - - > game.wav

  # Generate shaped PC circuit output at 48 kHz:
  t882wav.py game.t88 game.wav --mode shaped --sample-rate 48000

  # Inspect T88 timing & contents to stdout:
  t882wav.py game.t88 --inspect > report.txt

  # Chain into external physical DSP cassette channel modeler (wav2cas):
  t882wav.py game.t88 - --mode ideal | python3 cassette_modeler.py - game_tape.wav
""",
    )
    parser.add_argument(
        "input", nargs="?", default=None, help="Input .t88 file or '-' for stdin / pipe"
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output .wav file or '-' for stdout / pipe",
    )
    parser.add_argument(
        "--mode",
        "-m",
        "--wave",
        type=str,
        default="tape",
        choices=["tape", "cassette", "shaped", "pc", "ideal", "square"],
        help="Waveform synthesis mode: 'tape' (default), 'shaped' (PC circuit), 'ideal' (square)",
    )
    parser.add_argument(
        "--sample-rate",
        "-r",
        type=int,
        default=44100,
        help="Audio sample rate in Hz (default: 44100)",
    )
    parser.add_argument(
        "--channels",
        "-c",
        type=int,
        default=1,
        choices=[1, 2],
        help="Number of audio channels: 1 (mono, default) or 2 (stereo)",
    )
    parser.add_argument(
        "--stereo-mode",
        type=str,
        default="dual",
        choices=["dual", "left", "right", "diff"],
        help="Stereo channel distribution: 'dual' (default), 'left', 'right', 'diff'",
    )
    parser.add_argument(
        "--amplitude",
        "-a",
        "--volume",
        "-v",
        type=float,
        default=0.80,
        help="Audio peak amplitude in range 0.01..1.0 (default: 0.80, -2 dBFS)",
    )
    parser.add_argument(
        "--baud",
        "-b",
        type=int,
        choices=[600, 1200],
        default=None,
        help="Override baud rate for DATA blocks (600 or 1200 baud)",
    )
    parser.add_argument(
        "--speed",
        "-s",
        type=float,
        default=1.0,
        help="Tape motor speed factor multiplier (default: 1.0)",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="Invert audio polarity (180 degree phase)",
    )
    parser.add_argument(
        "--inspect",
        "--info",
        action="store_true",
        help="Inspect T88 blocks, baud rates, and timing, writing report to stdout.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run comprehensive built-in test suite and verify bidirectional decoding.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress diagnostic progress output to stderr.",
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
            run_inspector(in_stream, out_stream=sys.stdout)
        finally:
            if in_stream is not sys.stdin.buffer:
                in_stream.close()
        sys.exit(0)

    if args.output is None or args.output == "-":
        out_stream = sys.stdout.buffer
    else:
        out_stream = open(args.output, "wb")

    try:
        convert_t88_to_wav(
            in_stream,
            out_stream,
            mode=args.mode,
            sample_rate=args.sample_rate,
            channels=args.channels,
            stereo_mode=args.stereo_mode,
            amplitude=args.amplitude,
            speed_factor=args.speed,
            invert_polarity=args.invert,
            baud_override=args.baud,
            quiet=args.quiet,
        )
    except KeyboardInterrupt:
        log_diag("Conversion stopped by user.")
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
