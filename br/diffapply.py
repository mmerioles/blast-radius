"""Minimal unified-diff parser/applier (no `patch`/`git` in the slim image)."""

from __future__ import annotations

import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old_path", "new_path", "hunks", "binary")

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []  # list of (old_start, old_len, new_start, new_len, lines)
        self.binary = False

    @property
    def path(self):
        return self.new_path if self.new_path is not None else self.old_path

    @property
    def is_new(self):
        return self.old_path is None

    @property
    def is_delete(self):
        return self.new_path is None


def _strip(p: str):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[FilePatch] = []
    cur = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            out.append(cur)
            parts = line[len("diff --git "):].split(" b/")
            if len(parts) == 2:
                cur.old_path = _strip(parts[0])
                cur.new_path = _strip("b/" + parts[1])
            i += 1
            continue
        if line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("Binary files") and cur is not None:
            cur.binary = True
        elif line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                out.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            i += 2
            continue
        elif line.startswith("@@") and cur is not None:
            m = HUNK_RE.match(line)
            os_, ol, ns, nl = m.group(1), m.group(2), m.group(3), m.group(4)
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < n and (ro > 0 or rn > 0):
                l = lines[i]
                if l.startswith("\\"):
                    body.append(l)
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag == " ":
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    ro -= 1
                elif tag == "+":
                    rn -= 1
                else:
                    break
                body.append(l if l else " ")
                i += 1
            while i < n and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return out


def apply(old_text: str | None, fp: FilePatch) -> str | None:
    """Apply hunks to old_text. Returns new text (None for deletion)."""
    if fp.is_delete:
        return None
    old = [] if old_text is None else old_text.split("\n")
    # track trailing newline: split gives last '' if text ends with \n
    trailing_nl = old_text is None or old_text.endswith("\n")
    if old and old[-1] == "" and trailing_nl:
        old = old[:-1]
    result = []
    pos = 0  # index into old
    offset_total = 0
    no_nl_new = False
    for os_, ol, ns, nl, body in fp.hunks:
        old_chunk = []
        new_chunk = []
        prev = None
        for l in body:
            if l.startswith("\\"):
                if prev == "+":
                    no_nl_new = True
                elif prev == " ":
                    no_nl_new = True
                continue
            tag, content = l[:1], l[1:]
            if tag in (" ", "-"):
                old_chunk.append(content)
            if tag in (" ", "+"):
                new_chunk.append(content)
            prev = tag
        start = (os_ - 1) if ol > 0 else os_
        start += offset_total
        # verify / search for context
        def matches(s):
            return old[s:s + len(old_chunk)] == old_chunk

        if not matches(start):
            found = None
            for d in range(1, 2000):
                for s in (start - d, start + d):
                    if pos <= s <= len(old) - len(old_chunk) and matches(s):
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError(f"hunk does not apply to {fp.path} at {os_}")
            offset_total += found - start
            start = found
        result.extend(old[pos:start])
        result.extend(new_chunk)
        pos = start + len(old_chunk)
    result.extend(old[pos:])
    text = "\n".join(result)
    if result and not no_nl_new:
        text += "\n"
    return text


def changed_old_lines(fp: FilePatch):
    """Return (modified_old_lines, insertion_points).

    modified_old_lines: 1-based old line numbers removed/replaced.
    insertion_points: old line numbers *after which* pure insertions occur (0 = top).
    """
    mod = set()
    ins = set()
    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol > 0 else os_ + 1
        pending_del = False
        for l in body:
            if l.startswith("\\"):
                continue
            tag = l[:1]
            if tag == " ":
                o += 1
                pending_del = False
            elif tag == "-":
                mod.add(o)
                o += 1
                pending_del = True
            elif tag == "+":
                if not pending_del:
                    ins.add(o - 1)
    return mod, ins
