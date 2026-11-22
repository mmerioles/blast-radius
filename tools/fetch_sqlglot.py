"""Download sqlglot at the pinned commit from GitHub and unpack it to a directory."""

import io
import os
import shutil
import sys
import tarfile
import time
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"


def main(dest):
    data = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(URL, timeout=120) as r:
                data = r.read()
            break
        except Exception as e:  # noqa
            print(f"fetch failed ({e}), retrying", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    if data is None:
        sys.exit("could not download sqlglot")
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(tmp, filter="data")
    (top,) = os.listdir(tmp)
    shutil.move(os.path.join(tmp, top), dest)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"sqlglot {COMMIT[:10]} -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
