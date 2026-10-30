import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = Path("/home/nitin/story_generator/filmora/backend/qa.py")


def ffmpeg_commands(text):
    """Every ffmpeg argument list in the source, from its '[' to the matching ']'."""
    for match in re.finditer(r"""\[(["'])ffmpeg\1\s*,""", text):
        depth, end = 0, match.start()
        for end in range(match.start(), len(text)):
            depth += {"[": 1, "]": -1}.get(text[end], 0)
            if depth == 0:
                break
        yield text[match.start():end + 1]


def test_no_ffmpeg_call_reads_the_callers_stdin():
    # A doctoring loop fed a list of films on stdin lost most of it: the delivery check's ffmpeg read the list as
    # commands, then failed its own full decode (2026-10-30). Every ffmpeg call gets -nostdin, except one whose input
    # is stdin itself (-i -).
    sources = sorted((ROOT / "infofilm").glob("*.py")) + ([QA] if QA.exists() else [])
    bad = []
    for path in sources:
        for command in ffmpeg_commands(path.read_text()):
            if not re.search(r"""["']-""", command):
                continue                                  # a list of program names, not a command
            if "-nostdin" not in command and not re.search(r"""(["'])-i\1\s*,\s*(["'])-\2""", command):
                bad.append(f"{path.name}: {' '.join(command.split())[:90]}")
    assert not bad, "ffmpeg calls without -nostdin:\n" + "\n".join(bad)
