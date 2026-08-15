# pc88_tape_tools
pc88_tape_tools - convert between t88 and cmt, and split/join cmt files

## NEC PC-8001 / PC-8801 Cassette Tape Format Utility (`pc88_tape_tools.py`).

Provides parsing, splitting, joining, and bidirectional conversion between the
multi-file container format (.t88) and raw sequential tape dumps (.cmt).

Supported Protocols & Formats:
- N-BASIC (PC-8001), N80-BASIC (PC-8001mkII), N88-BASIC V1/V2 (PC-8801 series)
- .cmt: Sequential tape stream using exact BIOS / Monitor ROM state machine:
  - 0x24: Monitor Machine Code header + structured 0x3A records (length-jumped),
    terminated strictly by 0-length record.
  - 0xD3: Tokenized BASIC (CSAVE) traversed line-by-line until 0x0000 pointer.
  - 0x9C: ASCII sequential files consumed until 0x1A EOF.
- .t88: Authentic Manuke Station / X88000 24-byte header container format
  with 12-byte DATA sub-headers and carrier lead-in/gap tags.

### Output of `--help`
```
usage: pc88_tape_tools.py [-h] [--test]
                          {t2c,c2t,split-cmt,split-t88,join-cmt,join-t88} ...

NEC PC-8001 / PC-8801 Cassette Tape Format Utility (.t88 / .cmt)

positional arguments:
  {t2c,c2t,split-cmt,split-t88,join-cmt,join-t88}
                        Subcommand to execute
    t2c                 Convert .t88 container to raw .cmt tape dump
    c2t                 Convert raw .cmt tape dump to .t88 container
    split-cmt           Split multi-file .cmt or .t88 into individual .cmt
                        files
    split-t88           Split multi-file .cmt or .t88 into individual .t88
                        files
    join-cmt            Join multiple files into a single .cmt file
    join-t88            Join multiple files into a single .t88 container

options:
  -h, --help            show this help message and exit
  --test                Run internal unit tests and exit
```

```
usage: pc88_tape_tools.py t2c [-h] [-o OUTPUT] input

positional arguments:
  input                Path to input .t88 file

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output .cmt file (optional)
```

```
usage: pc88_tape_tools.py c2t [-h] [-o OUTPUT] [--comment COMMENT] [-b BAUD]
                              input

positional arguments:
  input                Path to input .cmt file

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output .t88 file (optional)
  --comment COMMENT    Optional comment metadata string embedded in T88 file
  -b, --baud BAUD      Baud rate for output T88 file (default: 1200)
```

```
usage: pc88_tape_tools.py split-cmt [-h] [-o OUTPUT_DIR] input

positional arguments:
  input                 Path to input .cmt or .t88 file

options:
  -h, --help            show this help message and exit
  -o, --output-dir OUTPUT_DIR
                        Output directory for split files (optional)
```

```
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
```

```
usage: pc88_tape_tools.py join-cmt [-h] -o OUTPUT inputs [inputs ...]

positional arguments:
  inputs               Input .cmt or .t88 files to concatenate

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  Path to output merged .cmt file
```

```
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

