"""Write SHA256SUMS for a release: the checksum list the host's updater checks the installer against.

Run from the repo root after compiling the installer, then upload both files to the
GitHub release:

    uv run python packaging/write_checksums.py packaging/installer/output/YT-MP3-Downloader-Setup.exe

The file is written next to the first input, in the sha256sum format that
ytmp3_host.updater.parse_checksums reads ("<hex>  <file name>").
"""

import hashlib
import sys
from pathlib import Path

SUMS_NAME = "SHA256SUMS"  # = ytmp3_host.updater.HOST_SUMS


def main(paths: list[Path]) -> None:
    lines = []
    for path in paths:
        with path.open("rb") as f:
            lines.append(f"{hashlib.file_digest(f, 'sha256').hexdigest()}  {path.name}\n")
    out = paths[0].with_name(SUMS_NAME)
    out.write_text("".join(lines), encoding="utf-8", newline="\n")
    sys.stdout.write(f"Wrote {out}:\n{''.join(lines)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main([Path(arg) for arg in sys.argv[1:]])
