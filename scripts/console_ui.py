"""Small, plain-text formatting helpers for terminals and redirected logs."""
import shutil
import sys
import textwrap


def rule(stream=None):
    print("-" * min(78, max(40, shutil.get_terminal_size((78, 24)).columns - 2)),
          file=stream or sys.stdout, flush=True)


def section(title, stream=None):
    stream = stream or sys.stdout
    print(file=stream)
    rule(stream)
    print(f"  {title}", file=stream, flush=True)
    rule(stream)
    print(file=stream, flush=True)


def note(message, indent=2, stream=None):
    width = min(78, max(40, shutil.get_terminal_size((78, 24)).columns - 2))
    print(textwrap.fill(str(message), width=width, initial_indent=" " * indent,
                        subsequent_indent=" " * indent, break_long_words=False,
                        break_on_hyphens=False), file=stream or sys.stdout, flush=True)


def option(key, label, detail=None):
    note(f"[{key}] {label}")
    if detail:
        note(detail, indent=6)
    print(flush=True)
