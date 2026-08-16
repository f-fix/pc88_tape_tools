# pc88_tape_tools, t882wav, wav2t88

Convert between t88, cmt, and wav, and split/join/analyze/synthesize/demodulate PC-8001 and PC-8801 tape files

Comprehensive cassette tape audio and container toolkit for the **NEC PC-8001 / PC-8801** family of computers (PC-8001, PC-8001mkII, PC-8001mkIISR, PC-8801, PC-8801mkII, PC-8801mkIISR).

Provides bidirectional conversion, splitting, merging, analysis, streaming audio synthesis, and streaming demodulation across all three layers of the vintage cassette storage stack:
1. **Physical Signal Layer (`.wav`)**: Analog audio recordings and FSK carrier audio.
2. **Container / Carrier Layer (`.t88`)**: Emulation container capturing signal timing, carrier tones, and raw byte blocks.
3. **Logical Tape Stream Layer (`.cmt`)**: Continuous sequential byte streams consumed directly by ROM BIOS / Monitor state machines.

---

## Architecture & Data Flow

```
+-----------------------------------------------------------------------------------+
| Physical Audio Signal (.wav)                                                      |
|   * Standard RIFF/WAVE PCM audio (44.1 kHz / 48 kHz, Mono / Stereo).              |
|   * FSK Modulation: Mark = 2400 Hz (Bit 1), Space = 1200 Hz (Bit 0).              |
|   * Baud Rates: 1200 baud (44 ticks/byte), 600 baud (88 ticks/byte).              |
+-----------------------------------------------------------------------------------+
             │                                              ▲
   wav2t88   │ Real-time Demodulation                       │ Real-time FSK Synthesis
             ▼                                              │ t882wav
+-----------------------------------------------------------------------------------+
| .t88 Container (Physical Carrier / Container Layer)                               |
|   [24-Byte Header] -> [Blocks: VERSION, COMMENT, MARK, SPACE, GAP, DATA]          |
|   * Carrier lead-ins (MARK/GAP/SPACE) define block intervals and carrier tones.   |
|   * 12-byte DATA sub-header embeds start tick, tick length, and baud format code. |
+-----------------------------------------------------------------------------------+
             │                                              ▲
             │ extract_cmt_payload()                        │ from_cmt_data()
  pc88_tape_tools t2c / split-cmt              pc88_tape_tools c2t / join-t88
             ▼                                              │
+-----------------------------------------------------------------------------------+
| .cmt Sequential Stream (Logical Demodulated Stream Layer)                         |
|   * Continuous byte stream presented to the CPU/BIOS I/O state machine.           |
|   * Boundaries defined by protocol sync headers and record structures:            |
|     - 0xD3: CSAVE Tokenized BASIC Program (Line-linked table -> 0x0000 pointer)   |
|     - 0x24: MON Machine Language Header + 0x3A records (terminated by :00)        |
|     - 0x9C: ASCII Text / Sequential Data (consumed until 0x1A EOF)                |
|     - 0x3A: Headerless MON O / MON I Stream (: [addr:2] [chk] -> : [len] -> :00)  |
|     - 0xFF: Custom Machine Language Loaders (e.g. NONTAMA: len + load/exec addr)  |
+-----------------------------------------------------------------------------------+
```

---

## Tool Overview

The suite consists of three modular, zero-external-dependency Python utilities:

| Tool | Primary Function | Key Features |
| :--- | :--- | :--- |
| **`pc88_tape_tools.py`** | `.t88` $\leftrightarrow$ `.cmt` Conversion & Tape Management | Format conversion (`t2c`, `c2t`), file extraction/splitting (`split-cmt`, `split-t88`), tape concatenation (`join-cmt`, `join-t88`), and deep ROM-level structural tape inspection (`analyze`). |
| **`t882wav.py`** | Streaming `.t88` $\rightarrow$ `.wav` FSK Synthesizer | Real-time streaming conversion with bounded memory; 3 waveform synthesis modes (`tape`, `shaped`, `ideal`); dynamic WAV header rewriting for seekable files and pipes; inspection mode (`--inspect`). |
| **`wav2t88.py`** | Streaming `.wav` $\rightarrow$ `.t88` FSK Demodulator | Analog slicer, DC blocker, biquad bandpass filter, rapid-recovery dynamic AGC, sub-sample zero-crossing linear interpolation, automatic 600/1200 baud detection, speed drift tracking, and noise click rejection. |

---

## 1. `pc88_tape_tools.py`

A format utility and state-machine parser for `.t88` container images and raw `.cmt` dumps.

### Subcommands & Usage

```bash
# Analyze a tape image (detects filenames, file types, load addresses, line counts, and baud)
python3 pc88_tape_tools.py analyze game.t88 -v

# Convert .t88 container to raw .cmt dump
python3 pc88_tape_tools.py t2c input.t88 -o output.cmt

# Convert raw .cmt dump to .t88 container (specifying baud rate and optional comment)
python3 pc88_tape_tools.py c2t input.cmt -o output.t88 --baud 1200 --comment "My Tape"

# Split a multi-file .t88 or .cmt into individual program .cmt files
python3 pc88_tape_tools.py split-cmt multi_game.t88 -o ./extracted_cmt/

# Split a multi-file tape into individual standalone .t88 files (preserving carrier lead-in timing)
python3 pc88_tape_tools.py split-t88 multi_game.t88 -o ./extracted_t88/

# Concatenate multiple .cmt / .t88 files into a single unified .cmt stream
python3 pc88_tape_tools.py join-cmt part1.cmt part2.cmt -o merged.cmt

# Merge multiple files into a single master .t88 container
python3 pc88_tape_tools.py join-t88 part1.t88 part2.cmt -o master.t88 --baud 1200
```

---

## 2. `t882wav.py`

A streaming FSK audio synthesizer that converts `.t88` tape container images into standard 16-bit PCM `.wav` audio. Operates in constant $O(1)$ memory without buffering large audio files in RAM.

### Waveform Synthesis Modes (`--mode`)

- **`tape` (Default)**: Simulates tape magnetic saturation using a smooth tanh curve ($v(t) = A \cdot \tanh(1.8(\sin\theta + 0.15\sin 2\theta))$) and playback head response. Generates natural harmonic content that provides maximum decoding reliability when played into real retro hardware or demodulators.
- **`shaped`**: Simulates the physical PC-8001 / PC-8801 hardware line-out analog circuit (RC low-pass edge smoothing at $f_c \approx 6\text{ kHz}$ and AC-coupling capacitor sag at $f_c \approx 150\text{ Hz}$).
- **`ideal`**: Emits pure digital bipolar square waves ($\pm A$) directly as generated by the 8255 PPI / 8251 USART CMT output port.

### Usage Examples

```bash
# Convert .t88 to .wav using default tape saturation mode (44.1 kHz, 16-bit mono)
python3 t882wav.py game.t88 game.wav

# Convert using shaped PC hardware circuit simulation at 48 kHz
python3 t882wav.py game.t88 game.wav --mode shaped --sample-rate 48000

# Stream through UNIX pipes (stdin -> stdout)
cat game.t88 | python3 t882wav.py - - > game.wav

# Generate stereo audio with inverted right channel (differential output for noise cancellation)
python3 t882wav.py game.t88 game_diff.wav --channels 2 --stereo-mode diff

# Inspect T88 block timing and contents (writes report directly to stdout)
python3 t882wav.py game.t88 --inspect > report.txt

# Run internal self-tests
python3 t882wav.py --test
```

---

## 3. `wav2t88.py`

A real-time streaming demodulator that converts cassette audio captures (`.wav`) into standard `.t88` images.

### Signal Processing Architecture

- **AC-Coupling DC Blocker**: Eliminates soundcard DC offset bias.
- **Biquad Bandpass Filter**: 2nd-order bandpass filter centered on the FSK carrier (600–3600 Hz).
- **Fast-Attack Dynamic AGC**: Rapid-recovery envelope follower preventing high-amplitude 1200 Hz Space tones from masking quiet 2400 Hz Mark tones.
- **Adaptive Schmitt Trigger Slicer**: Hysteresis tracking dynamic noise floor.
- **Sub-Sample Linear Interpolation**: Calculates exact fractional zero-crossing timestamps for microsecond-accurate pulse period measurement.
- **Carrier Drift Tracking**: Measures tape motor speed deviations and dynamically tracks carrier frequency.
- **Transient Noise Filtering**: Rejects short motor/relay switch clicks and hum while preserving tight in-session carrier gaps (~60–80 ms).

### Usage Examples

```bash
# Convert WAV audio to .t88 container (automatic 600/1200 baud detection)
python3 wav2t88.py tape_recording.wav output.t88

# Force 1200 baud decoding and specify right audio channel from a stereo capture
python3 wav2t88.py stereo_capture.wav output.t88 --baud 1200 --channel right

# Analyze tape audio capture characteristics (frequency, speed offset, channel energy)
python3 wav2t88.py tape_recording.wav --inspect

# Run comprehensive demodulator test suite across all waveform types and SNR levels
python3 wav2t88.py --test
```

---

## Pipeline Workflows & Advanced Physical DSP Simulation

All tools in the suite support standard UNIX streaming pipes via `-` (stdin/stdout).

### 1. End-to-End Tape Verification Loop (`.t88` $\rightarrow$ `.wav` $\rightarrow$ `.t88`)
Synthesize audio from a `.t88` file and pipe it directly through the demodulator to verify 100% byte-for-byte fidelity:
```bash
python3 t882wav.py original.t88 - --quiet | python3 wav2t88.py - restored.t88
python3 pc88_tape_tools.py analyze restored.t88
```

### 2. Audio Capture to Demodulated `.cmt` Program Extraction
Demodulate a raw WAV recording and immediately unpack all contained programs:
```bash
python3 wav2t88.py tape_in.wav - | python3 pc88_tape_tools.py split-cmt - -o ./extracted_programs/
```

### 3. Physical DSP Cassette Channel Modeling with [`wav2cas`](https://github.com/f-fix/wav2cas)
For advanced simulations modeling full IEC 60094 Type I record pre-emphasis EQ, tape self-demagnetization write loss, Wallace head gap loss, Faraday induction derivative slope ($d\Phi/dt$), and tape hiss, pipe `t882wav.py` in `ideal` mode into `cassette_modeler.py` from the [`wav2cas`](https://github.com/f-fix/wav2cas) project:

```bash
# Synthesize ideal digital square wave -> apply physical cassette DSP simulation -> output WAV
python3 t882wav.py game.t88 - --mode ideal --quiet | \
  python3 /path/to/wav2cas/cassette_modeler.py - -m "record+playback" --drive 1.2 --hiss 0.002 > simulated_tape.wav

# Verify that wav2t88 can demodulate the physically modeled audio:
python3 wav2t88.py simulated_tape.wav recovered.t88
```

---

## Technical Specifications

### Hardware FSK & Serial UART Specifications
- **Mark Frequency**: 2400 Hz (Logic 1)
- **Space Frequency**: 1200 Hz (Logic 0)
- **CMT Baud Rates**:
  - **1200 baud**: 1 Mark bit = 2 cycles of 2400 Hz; 1 Space bit = 1 cycle of 1200 Hz (44 ticks/byte @ 4800 Hz).
  - **600 baud**: Pulse-doubled 1200 baud: 1 Mark bit = 4 cycles of 2400 Hz; 1 Space bit = 2 cycles of 1200 Hz (88 ticks/byte @ 4800 Hz).
- **Serial Framing**: 1 Start bit (`0`), 8 Data bits (LSB-first), 2 Stop bits (`1`). Total 11 bits/byte.
- **T88 Base Tick Rate**: 4800 Hz (1 tick = $1/4800\text{ s}$, exactly one half-cycle of 2400 Hz).

### T88 Tag Specifications
- `0x0000` (**END**): Terminal image marker.
- `0x0001` (**VERSION**): Format revision (e.g. `0x0100`).
- `0x0010` (**COMMENT**): Embedded metadata / annotation text.
- `0x0100` (**GAP**): Blank silence / unrecorded tape interval (`start_tick: uint32`, `length_ticks: uint32`).
- `0x0101` (**DATA**): Serial data payload with timing sub-header (`start_tick: uint32`, `length_ticks: uint32`, `data_len: uint16`, `format: uint16`).
- `0x0102` (**SPACE**): 1200 Hz tone burst (`start_tick: uint32`, `length_ticks: uint32`).
- `0x0103` (**MARK**): 2400 Hz carrier tone burst (`start_tick: uint32`, `length_ticks: uint32`).

---

## Running Built-in Test Suites

Each utility includes a self-test suite:

```bash
# Run format utility unit tests
python3 pc88_tape_tools.py --test

# Run T88-to-WAV synthesis verification tests
python3 t882wav.py --test

# Run WAV-to-T88 demodulator acoustic test suite
python3 wav2t88.py --test
```

---

# Note on the code and the tools used to write it

Parts of this code were written with assistance from LLM-integrated coding tools. If you don't like it, feel free to use other software or rewrite parts you dislike. PRs are welcome!

## How did I end up using those? Don't I dislike slop?

Yes, I hate it. This project began because I wanted tape image conversion tools where the conversion steps were all clearly documented and readable code, but which also performed well enough in terms of accuracy to actually be the tool I use. I started out writing the tool myself, but my manual attempts hadn't yielded comparable accuracy to existing closed-source tools, so I started using the tools to help find the bugs and suggest improvements, and IMO the result is now good enough to actually be useful in some scenarios. In terms of slop, the tool-generated code doesn't closely resemble any existing solutions I have found. Rather it's a fairly passable translation of my requests into Python.
