"""Build a symlink-farm copy of the pristine sqlglot checkout with patched files."""

from __future__ import annotations

import os
import shutil

from . import diffapply


def read_pristine(base: str, rel: str):
    p = os.path.join(base, rel)
    try:
        with open(p, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None


def patched_files(base: str, patch_text: str):
    """Return {relpath: (old_text, new_text)} (texts may be None) and the FilePatch list."""
    fps = diffapply.parse(patch_text)
    out = {}
    for fp in fps:
        if fp.binary:
            continue
        old_rel = fp.old_path
        rel = fp.path
        old_text = read_pristine(base, old_rel) if old_rel else None
        if rel in out:  # same file patched twice: chain
            old_text = out[rel][1]
        new_text = diffapply.apply(old_text, fp)
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            out[fp.old_path] = (old_text, None)
        out[rel] = (old_text if rel not in out else out[rel][0], new_text)
    return out, fps


def build(base: str, dest: str, files: dict):
    """files: {relpath: new_text_or_None}. Creates dest as an overlay of base."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    # directories that need to be real (contain patched entries)
    real_dirs = {""}
    for rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))

    def fill(rel_dir):
        src_dir = os.path.join(base, rel_dir) if rel_dir else base
        dst_dir = os.path.join(dest, rel_dir) if rel_dir else dest
        os.makedirs(dst_dir, exist_ok=True)
        names = set(os.listdir(src_dir)) if os.path.isdir(src_dir) else set()
        for rel in files:
            d, _, n = rel.rpartition("/")
            if d == rel_dir:
                names.add(n)
        for name in names:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in files:
                text = files[rel]
                if text is not None:
                    with open(os.path.join(dst_dir, name), "w", encoding="utf-8", newline="") as f:
                        f.write(text)
                continue
            if rel in real_dirs:
                fill(rel)
                continue
            os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))

    fill("")
    return dest
