"""Read a bounded private deployment file without following links."""

import os
import stat
from pathlib import Path


def read_private_file(value, maximum):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Private file requires an absolute deployment path")
    path = Path(value)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Private file must be regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid not in {0, os.geteuid()}
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
            or not 1 <= info.st_size <= maximum
        ):
            raise ValueError("Unsafe private file")
        body = bytearray()
        while len(body) <= maximum:
            chunk = os.read(descriptor, min(8192, maximum + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(body) != info.st_size
            or len(body) > maximum
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        ):
            raise ValueError("Private file changed during read")
        return bytes(body), (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)
