# pc88_tape_tools
pc88_tape_tools - convert between t88 and cmt, and split/join/analyze cmt files

## NEC PC-8001 / PC-8801 Cassette Tape Format Utility (`pc88_tape_tools.py`).

Provides state-machine parsing, splitting, joining, diagnostic analysis, and bidirectional
conversion between physical container images (.t88) and raw sequential tape dumps (.cmt).

### Format Architecture and Relationshop
```
+-----------------------------------------------------------------------------------+
| .t88 Container (Physical Carrier / Container Layer)                               |
|   [24-Byte Header] -> [Blocks: VERSION, COMMENT, MARK, SPACE, GAP, DATA_1200/300] |
|   * Carrier lead-ins (MARK/GAP/SPACE) define block intervals and tape carrier.    |
|   * 12-byte DATA sub-header embeds start tick, tick length, and baud duration.    |
+-----------------------------------------------------------------------------------+
                                         │
                   extract_cmt_payload() │ from_cmt_data()
                                         ▼
+-----------------------------------------------------------------------------------+
| .cmt Sequential Stream (Logical Demodulated Stream Layer)                         |
|   * Continuous sequential byte stream directly consumed by BIOS/Monitor ROM.      |
|   * File boundaries are defined by Protocol Headers or Address Record Syncs:      |
|     - 0xD3: CSAVE Tokenized BASIC Program (Line-linked table -> 0x0000 pointer)   |
|     - 0x24: MON Machine Language Header + 0x3A records (terminated by :00)        |
|     - 0x9C: ASCII Text / Sequential Data (consumed until 0x1A EOF)                |
|     - 0x3A: Headerless MON O / MON I Stream (: [addr:2] [chk] -> : [len] -> :00)  |
|     - 0xFF: Custom Machine Language Loaders (e.g. NONTAMA: len + load/exec addr)  |
+-----------------------------------------------------------------------------------+
```

### Supported Protocols & State Machines:
- .t88 (Physical Signal / Container Layer):
    An emulation container capturing the physical cassette signal structure.
    Consists of a 24-byte ASCII header followed by tagged timing and data blocks:
  - 0x0103 (MARK): Lead-in carrier tone burst (~2400 Hz high frequency).
  - 0x0102 (SPACE): Space carrier tone (~1200 Hz low frequency).
  - 0x0100 (GAP): Silence / unrecorded tape interval.
  - 0x0101 (DATA): Timing sub-header (12 bytes: start_tick, tick_len, data_len)
                   plus raw demodulated byte payload.
  - 0x0010 (COMMENT): UTF-8/ASCII metadata annotations.
  - 0x0000 (END): Terminal container marker.
- .cmt (Logical Sequential Tape Stream):
    The continuous demodulated byte stream presented to the CPU/BIOS I/O state machine.
    Contains no container framing; boundaries are determined purely by protocol state:
  - 0xD3: CSAVE Tokenized BASIC Program.
          Preamble (3-10x 0xD3) + 6-byte filename + inter-block sync tone +
          linked line table traversed line-by-line until 0x0000 next-pointer.
  - 0x24: MON Machine Language Header (MON W / MON R).
          Preamble (3-10x 0x24) + 6-byte filename + 4-byte Start Address Record
          (: [addr_hi:1] [addr_lo:1] [chk:1]) + length-jumped data records
          (: [len:1] [data:len] [chk:1]) + 0-length terminator (`: \x00` [chk:1]).
  - 0x9C: ASCII Sequential File (SAVE / PRINT#).
          Preamble (3-10x 0x9C) + 6-byte filename + text stream terminated by 0x1A (EOF).
  - 0x3A: Headerless Monitor Machine Language Records (MON O / MON I).
          Direct 4-byte Start Address Record + length-jumped data records,
          terminated strictly by 0-length record (`: \x00`).
  - 0xFF: Custom Machine Language Loaders (e.g. NONTAMA format).
          Header preamble (`\xffNONTAMA`) + 6-byte descriptor (load_addr, len, exec_addr) +
          direct length-jumped payload.
  
### Output of `--help-all`
```
usage: pc88_tape_tools.py [-h] [--test] [--help-all] <command> ...

NEC PC-8001 / PC-8801 Cassette Tape Format Utility (.t88 / .cmt)

options:
  -h, --help   show this help message and exit
  --test       Run internal unit tests and exit
  --help-all   Show full detailed help for all subcommands at once and exit

Available Subcommands:
  <command>
    analyze    Analyze tape image structure, programs, baud rate, and metadata
    t2c        Convert .t88 container to raw .cmt tape dump
    c2t        Convert raw .cmt tape dump to .t88 container
    split-cmt  Split multi-file .cmt or .t88 into individual .cmt files
    split-t88  Split multi-file .cmt or .t88 into individual .t88 files
    join-cmt   Join multiple files into a single .cmt file
    join-t88   Join multiple files into a single .t88 container

Tip: Run 'pc88_tape_tools.py <subcommand> --help' (e.g. 'pc88_tape_tools.py split-t88 --help') or 'pc88_tape_tools.py --help-all' to view detailed options for all subcommands at once.


================================================================================
DETAILED SUBCOMMAND HELP
================================================================================

--- Subcommand: analyze ---
usage: pc88_tape_tools.py analyze [-h] [-v] input

positional arguments:
  input          Path to input .t88 or .cmt file to analyze

options:
  -h, --help     show this help message and exit
  -v, --verbose  Display full T88 block table and detailed diagnostics

--- Subcommand: t2c ---
usage: pc88_tape_tools.py t2c [-h] [-o OUTPUT] input

positional arguments:
  input                Path to input .t88 file

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output .cmt file (optional)

--- Subcommand: c2t ---
usage: pc88_tape_tools.py c2t [-h] [-o OUTPUT] [--comment COMMENT] [-b BAUD]
                              input

positional arguments:
  input                Path to input .cmt file

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output .t88 file (optional)
  --comment COMMENT    Optional comment metadata string embedded in T88 file
  -b, --baud BAUD      Baud rate for output T88 file (default: 1200)

--- Subcommand: split-cmt ---
usage: pc88_tape_tools.py split-cmt [-h] [-o OUTPUT_DIR] input

positional arguments:
  input                 Path to input .cmt or .t88 file

options:
  -h, --help            show this help message and exit
  -o, --output-dir OUTPUT_DIR
                        Output directory for split files (optional)

--- Subcommand: split-t88 ---
usage: pc88_tape_tools.py split-t88 [-h] [-o OUTPUT_DIR] [-b BAUD]
                                    [--cmt-baud CMT_BAUD] [--comment COMMENT]
                                    input

positional arguments:
  input                 Path to input .cmt or .t88 file

options:
  -h, --help            show this help message and exit
  -o, --output-dir OUTPUT_DIR
                        Output directory for split files (optional)
  -b, --baud BAUD       Override baud rate for output .t88 files (preserves
                        original timing by default for .t88)
  --cmt-baud, --default-baud CMT_BAUD
                        Default baud rate when input is a raw .cmt file
                        (default: 1200)
  --comment COMMENT     Optional comment metadata string embedded in T88 files

--- Subcommand: join-cmt ---
usage: pc88_tape_tools.py join-cmt [-h] -o OUTPUT inputs [inputs ...]

positional arguments:
  inputs               Input .cmt or .t88 files to concatenate

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output merged .cmt file

--- Subcommand: join-t88 ---
usage: pc88_tape_tools.py join-t88 [-h] -o OUTPUT [-b BAUD]
                                   [--cmt-baud CMT_BAUD] [--comment COMMENT]
                                   inputs [inputs ...]

positional arguments:
  inputs                Input .cmt or .t88 files to concatenate

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   Path to output merged .t88 file
  -b, --baud BAUD       Override baud rate for ALL output chunks (both .t88
                        and .cmt inputs)
  --cmt-baud, --default-baud CMT_BAUD
                        Default baud rate to use for raw .cmt inputs (default:
                        1200). Does not affect .t88 inputs.
  --comment COMMENT     Optional comment metadata string embedded in T88 file
```

# Note on the code and the tools used to write it

Parts of this code were written with assistance from LLM-integrated coding tools. If you don't like it, feel free to use other software or rewrite parts you dislike. PR's are welcome!

## How did I end up using those? Don't I dislike slop?

Yes I hate it. This project began because I wanted tape image conversion tools where the conversion steps were all clearly documented and readable code, but which also performed well enough in terms of accuracy to actually be the tool I use. I started out writing the tool myself, but my manual attempts hadn't yielded comparable accuracy to existing closed-source tools so I started using the tools to help find the bugs and suggest improvements, and IMO the result is now good enough to actually be useful in some scenarios. In terms of slop, the tool-generated code doesn't closely resemble any existing solutions I have found. Rather it's a fairly passable translation of my requests into Python.

