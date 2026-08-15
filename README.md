# pc88_tape_tools
pc88_tape_tools - convert between t88 and cmt, and split/join cmt files

## Output of `--help`
```
usage: pc88_tape_tool.py [-h] [--test] {t2c,c2t,split-cmt,join-cmt} ...

NEC PC-8001 / PC-8801 Cassette Tape Format Utility (.t88 / .cmt)

positional arguments:
  {t2c,c2t,split-cmt,join-cmt}
                        Subcommand to execute
    t2c                 Convert .t88 container to raw .cmt tape dump
    c2t                 Convert raw .cmt tape dump to .t88 container
    split-cmt           Split multi-file .cmt or .t88 into individual files
    join-cmt            Join multiple .cmt files into a single .cmt file

options:
  -h, --help            show this help message and exit
  --test                Run internal unit tests and exit
```

# Note on the code and the tools used to write it

Parts of this code were written with assistance from LLM-integrated coding tools. If you don't like it, feel free to use other software or rewrite parts you dislike. PR's are welcome!

## How did I end up using those? Don't I dislike slop?

Yes I hate it. This project began because I wanted tape audio conversion tools where the conversion steps were all clearly documented and readable code, but which also performed well enough in terms of accuracy to actually be the tool I use. I started out writing the tool myself, but my manual attempts hadn't yielded comparable accuracy to existing closed-source tools so I started using the tools to help find the bugs and suggest improvements, and IMO the result is now good enough to actually be useful in some scenarios. In terms of slop, the tool-generated code doesn't closely resemble any existing solutions I have found. Rather it's a fairly passable translation of my requests into Python.

