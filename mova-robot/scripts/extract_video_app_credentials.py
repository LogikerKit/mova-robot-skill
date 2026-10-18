#!/usr/bin/env python3
"""Extract MOVA's application credentials from a user-supplied APK/XAPK.

No application code is executed. This accepts the verified MOVA initialization
pattern and refuses unknown layouts. App credentials are separate from personal
MOVA login credentials. Values are never printed or placed in this repository.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import zipfile


class UnsupportedAPK(ValueError):
    pass


class Dex:
    def __init__(self, data: bytes):
        self.data = data
        if data[:4] != b"dex\n" or len(data) < 112 or self.u32(40) != 0x12345678:
            raise UnsupportedAPK("Unsupported DEX format")
        self.strings = []
        for i in range(self.u32(56)):
            off = self.u32(self.u32(60) + i * 4)
            _, pos = self.uleb(off)
            end = data.index(b"\0", pos)
            self.strings.append(data[pos:end].decode("utf-8", errors="replace"))
        self.types = [self.strings[self.u32(self.u32(68) + i * 4)] for i in range(self.u32(64))]
        self.methods = []
        for i in range(self.u32(88)):
            cls, proto, name = struct.unpack_from("<HHI", data, self.u32(92) + 8 * i)
            self.methods.append((self.types[cls], self.strings[name], proto))

    def u32(self, pos):
        return struct.unpack_from("<I", self.data, pos)[0]

    def uleb(self, pos):
        value = 0
        for shift in range(0, 35, 7):
            byte = self.data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, pos
        raise UnsupportedAPK("Invalid DEX integer")

    def target_methods(self):
        for i in range(self.u32(96)):
            off = self.u32(100) + i * 32
            if self.types[self.u32(off)] != "Lcom/dreame/movahome/MainApplication;":
                continue
            pos = self.u32(off + 24)
            if not pos:
                continue
            sizes = []
            for _ in range(4):
                val, pos = self.uleb(pos)
                sizes.append(val)
            for _ in range(sizes[0] + sizes[1]):
                _, pos = self.uleb(pos)
                _, pos = self.uleb(pos)
            for size in sizes[2:]:
                index = 0
                for _ in range(size):
                    diff, pos = self.uleb(pos)
                    _, pos = self.uleb(pos)
                    code, pos = self.uleb(pos)
                    index += diff
                    name = self.methods[index][1]
                    if name.startswith("lambda$initAlifySdk$") and code:
                        count = self.u32(code + 12)
                        yield struct.unpack_from(f"<{count}H", self.data, code + 16)

    def constant(self, words, pos):
        op = words[pos] & 255
        reg = words[pos] >> 8
        if op == 0x1A:  # const-string, 21c
            return reg, self.strings[words[pos + 1]], pos + 2
        if op == 0x1B:  # const-string/jumbo, 31c
            return reg, self.strings[words[pos + 1] | words[pos + 2] << 16], pos + 3
        raise UnsupportedAPK("Unexpected application credential initializer")

    def credentials(self):
        found = []
        for words in self.target_methods():
            for pos in range(len(words) - 10):
                if words[pos] & 255 not in (0x1A, 0x1B):
                    continue
                try:
                    _, marker, call = self.constant(words, pos)
                    if marker != "uat" or words[call] & 255 != 0x6E:
                        continue
                    if self.methods[words[call + 1]][:2] != ("Ljava/lang/String;", "contains"):
                        continue
                    # contains("uat"), move-result, if-nez to test branch.
                    branch = call + 4
                    if words[call + 3] & 255 != 0x0A or words[branch] & 255 != 0x39:
                        continue
                    key_reg, key, next_pos = self.constant(words, branch + 2)
                    secret_reg, secret, next_pos = self.constant(words, next_pos)
                    if words[next_pos] & 255 != 0x28:
                        continue
                    delta = words[next_pos] >> 8
                    if delta >= 128:
                        delta -= 256
                    merged = next_pos + delta
                    # Require the discovered registers to feed the SDK initializer.
                    matched = False
                    for k in range(merged, min(merged + 16, len(words) - 2)):
                        if words[k] & 255 != 0x6E:
                            continue
                        method = self.methods[words[k + 1]]
                        if method[:2] != ("Lcom/dreame/sdk/alify/AliFyApplication;", "initAliSdk"):
                            continue
                        count = words[k] >> 12
                        regs = [(words[k + 2] >> (4 * n)) & 15 for n in range(4)]
                        if count == 4 and regs[2:] == [key_reg, secret_reg]:
                            matched = True
                    if matched and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", key) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", secret):
                        found.append({"appKey": key, "appSecret": secret})
                except (IndexError, struct.error, UnsupportedAPK):
                    continue
        return found


def extract(path: Path):
    if not path.is_file() or path.stat().st_size > 768 * 1024**2:
        raise UnsupportedAPK("Expected a regular APK/XAPK no larger than 768 MiB")
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as outer:
        if "AndroidManifest.xml" in outer.namelist():
            apk = raw
        elif "com.dreame.movahome.apk" in outer.namelist():
            if outer.getinfo("com.dreame.movahome.apk").file_size > 512 * 1024**2:
                raise UnsupportedAPK("APK too large")
            apk = outer.read("com.dreame.movahome.apk")
        else:
            raise UnsupportedAPK("Expected the MOVAhome APK or XAPK")
    matches = []
    with zipfile.ZipFile(io.BytesIO(apk)) as archive:
        for entry in archive.infolist():
            if not re.fullmatch(r"classes\d*\.dex", entry.filename):
                continue
            if entry.file_size > 128 * 1024**2:
                raise UnsupportedAPK("DEX too large")
            data = archive.read(entry)
            if b"lambda$initAlifySdk$" in data:
                matches.extend(Dex(data).credentials())
    unique = {json.dumps(item, sort_keys=True) for item in matches}
    if len(unique) != 1:
        raise UnsupportedAPK("No unique verified MOVA application credential initializer found")
    result = json.loads(unique.pop())
    result["sourceApkSha256"] = hashlib.sha256(apk).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--config-dir", type=Path, default=Path(os.environ.get("MOVA_CONFIG_DIR", "~/.config/mova-robot")).expanduser())
    args = parser.parse_args()
    try:
        result = extract(args.apk)
        args.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = args.config_dir / "video-app.json"
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if output.is_symlink() or not stat.S_ISREG(output.stat().st_mode) or output.stat().st_size > 16384 or json.loads(output.read_text()) != result:
                raise UnsupportedAPK("Different application credentials already exist; refusing replacement")
            output.chmod(0o600)
            print(json.dumps({"ok": True, "status": "already_present", "path": str(output)}))
            return
        with os.fdopen(fd, "w") as handle:
            json.dump(result, handle)
        print(json.dumps({"ok": True, "status": "stored", "path": str(output)}))
    except (OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile) as error:
        # No raw APK contents, app keys, or credentials enter error messages.
        print(json.dumps({"ok": False, "errorType": type(error).__name__}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
