#!/usr/bin/env python3
"""nup_packer_gui.py - Tkinter GUI that builds an Eden .nup from loose exefs/romfs trees."""

import hashlib, os, queue, struct, threading, traceback, uuid
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

NO_DIR = 0xFFFFFFFF
CHUNK = 8 * 1024 * 1024
align16 = lambda n: (n + 15) & ~15


# ----------------------------------------------------------------- tree model
class Dir:
    def __init__(s, name):
        s.name, s.dirs, s.files, s.idx, s.first, s.count = name, [], [], 0, 0, 0


class File:
    def __init__(s, name, path):
        s.name, s.path, s.idx = name, path, 0
        s.size = 0
        s.sha = b""
        s.codec = 0
        s.eoff = 0
        s.esize = 0


def scan(path, name):
    d = Dir(name)
    for e in sorted(path.iterdir(), key=lambda p: p.name):
        if e.is_dir():
            d.dirs.append(scan(e, e.name))
        elif e.is_file():
            d.files.append(File(e.name, e))
    return d


def collect(d, dirs, files):
    d.idx = len(dirs)
    dirs.append(d)
    for s in d.dirs:
        collect(s, dirs, files)
    for f in d.files:
        f.idx = len(files)
        files.append(f)


# ----------------------------------------------------------------- helpers
def resolve_inputs(folder: Path):
    """Accepts a package folder (has exefs/romfs) or a parent holding base/ [+ update/]."""
    if (folder / "exefs").is_dir() or (folder / "romfs").is_dir():
        return folder, None
    base = folder / "base"
    upd = folder / "update"
    if base.is_dir():
        return base, (upd if upd.is_dir() else None)
    return None, None


def tlv(t, payload):
    return struct.pack("<HHI", t, 0, len(payload)) + payload


def loc_block(items):
    out = b""
    for lang, data in items:
        l = lang.lower().encode()
        out += bytes([len(l)]) + l + struct.pack("<I", len(data)) + data
    return out


def parse_ver(s):
    a = [int(x) for x in s.strip().split(".") if x] + [0, 0, 0]
    return ((a[0] & 0xFFFF) << 16) | ((a[1] & 0xFF) << 8) | (a[2] & 0xFF)


def guess_title_id(folder: Path):
    """Best-effort read of the program id from main.npdm (ACI0)."""
    try:
        d = (folder / "exefs" / "main.npdm").read_bytes()
        if d[:4] != b"META":
            return ""
        aci0 = struct.unpack_from("<I", d, 0x70)[0]
        if d[aci0 : aci0 + 4] != b"ACI0":
            return ""
        return f"{struct.unpack_from('<Q', d, aci0 + 0x10)[0]:016X}"
    except Exception:
        return ""


class Pkg:
    def __init__(s, path, kind, title_id, version, flags, meta):
        path = Path(path)
        s.kind, s.title_id, s.version, s.flags = kind, title_id, version, flags
        s.root = Dir(path.name or "pkg")
        s.exefs = s.romfs = None
        if (path / "exefs").is_dir():
            s.exefs = scan(path / "exefs", "exefs")
            s.root.dirs.append(s.exefs)
        if (path / "romfs").is_dir():
            s.romfs = scan(path / "romfs", "romfs")
            s.root.dirs.append(s.romfs)
        m = b""
        npdm = path / "exefs" / "main.npdm"
        if npdm.is_file():
            m += tlv(0x0001, npdm.read_bytes())
        m += meta
        s.meta, s.meta_off = m, 0


# ----------------------------------------------------------------- builder
def build_nup(cfg, log, progress):
    base_dir, auto_upd = resolve_inputs(Path(cfg["folder"]))
    upd_dir = Path(cfg["update"]) if cfg["update"] else auto_upd
    if base_dir is None and not upd_dir:
        raise ValueError("Input folder has no exefs/romfs and no base/ subfolder.")
    tid = int(cfg["title_id"], 16)

    base_meta = b""
    if cfg["icon"]:
        base_meta += tlv(0x0003, Path(cfg["icon"]).read_bytes())
    if cfg["title"]:
        base_meta += tlv(0x0005, cfg["title"].encode())
    if cfg["loc_titles"]:
        base_meta += tlv(
            0x0009, loc_block([(l, t.encode()) for l, t in cfg["loc_titles"]])
        )
    disp = (
        tlv(0x0004, cfg["display_version"].encode()) if cfg["display_version"] else b""
    )

    pkgs = []
    if base_dir:
        pkgs.append(
            Pkg(
                base_dir,
                0,
                tid,
                parse_ver(cfg["version"]),
                0,
                base_meta + (b"" if upd_dir else disp),
            )
        )
    if upd_dir:
        pkgs.append(
            Pkg(
                upd_dir,
                1,
                tid,
                parse_ver(cfg["update_version"]),
                1 if (cfg["standalone"] or not base_dir) else 0,
                disp,
            )
        )
        if not base_dir:  # standalone update: put title/icon there
            pkgs[-1].meta += base_meta
    log(f"Packages: {', '.join(('base' if p.kind == 0 else 'update') for p in pkgs)}")

    dirs, files = [], []
    for p in pkgs:
        collect(p.root, dirs, files)
    D, F, P = len(dirs), len(files), len(pkgs)
    total_bytes = sum(f.path.stat().st_size for f in files)
    log(f"{D} dirs, {F} files, {total_bytes / 1e9:.2f} GB")

    children = []
    for d in dirs:
        d.first = len(children)
        children += [s.idx for s in d.dirs] + [D + f.idx for f in d.files]
        d.count = len(children) - d.first

    pool, pmap = bytearray(), {}

    def intern(s):
        if s not in pmap:
            b = s.encode()
            pmap[s] = (len(pool), len(b))
            pool.extend(b)

    for d in dirs:
        intern(d.name)
    for f in files:
        intern(f.name)

    pool_off = 0x30 + P * 0x40 + D * 0x18 + F * 0x50 + 4 * len(children)
    toc_size = pool_off + len(pool)

    pos = align16(0x80 + toc_size)
    for p in pkgs:
        if p.meta:
            p.meta_off = pos
            pos = align16(pos + len(p.meta))
    blob_off = pos

    level = cfg["zstd"]
    cctx = None
    if level > 0:
        import zstandard

        cctx = zstandard.ZstdCompressor(level=level)

    done = 0
    with open(cfg["output"], "wb") as out:
        out.seek(blob_off)
        for f in files:
            f.size = f.path.stat().st_size
            if f.size == 0:
                f.sha = hashlib.sha256(b"").digest()
                continue
            pad = (-pos) % 16
            out.write(b"\0" * pad)
            pos += pad
            start = pos
            h = hashlib.sha256()
            stored_raw = True
            if cctx:
                with open(f.path, "rb") as fin:  # pass 1: hash
                    while chunk := fin.read(CHUNK):
                        h.update(chunk)
                with open(f.path, "rb") as fin:  # pass 2: compress
                    cctx.copy_stream(fin, out, size=f.size)
                csize = out.tell() - start
                if csize < f.size:
                    stored_raw, f.codec, f.esize = False, 1, csize
                    done += f.size
                else:  # not worth it -> raw
                    out.seek(start)
                    out.truncate()
            if stored_raw:
                f.codec = 0
                with open(f.path, "rb") as fin:
                    while chunk := fin.read(CHUNK):
                        if not cctx:
                            h.update(chunk)
                        out.write(chunk)
                        if not cctx:
                            done += len(chunk)
                            progress(done / total_bytes)
                f.esize = f.size
                if cctx:
                    done += f.size
            f.sha = h.digest()
            f.eoff = start
            pos = start + f.esize
            progress(done / max(total_bytes, 1))
        file_size = pos

        toc = struct.pack("<IIQQQQQ", 0x01434F54, P, D, F, pool_off, len(pool), 0)
        for p in pkgs:
            toc += struct.pack(
                "<QIIIIBBHQII20s",
                p.title_id,
                p.root.idx,
                p.exefs.idx if p.exefs else NO_DIR,
                p.romfs.idx if p.romfs else NO_DIR,
                p.version,
                p.kind,
                p.flags,
                0,
                p.meta_off,
                len(p.meta),
                0,
                b"\0" * 20,
            )
        for d in dirs:
            no, nl = pmap[d.name]
            toc += struct.pack("<IIIIQ", no, nl, d.first, d.count, 0)
        for f in files:
            no, nl = pmap[f.name]
            toc += struct.pack(
                "<IIIQ32sQQBBH8s",
                no,
                nl,
                0,
                f.size,
                f.sha,
                f.eoff,
                f.esize,
                f.codec,
                0,
                0,
                b"\0" * 8,
            )
        toc += struct.pack(f"<{len(children)}I", *children) + bytes(pool)
        assert len(toc) == toc_size

        sup = struct.pack(
            "<4sHHQQ32sQQII16s32s",
            b"NUP\x01",
            0x80,
            1,
            0x80,
            toc_size,
            hashlib.sha256(toc).digest(),
            blob_off,
            file_size,
            P,
            0,
            uuid.uuid4().bytes,
            b"\0" * 32,
        )
        assert len(sup) == 0x80
        out.seek(0)
        out.write(sup)
        out.write(toc)
        for p in pkgs:
            if p.meta:
                out.seek(p.meta_off)
                out.write(p.meta)
        out.truncate(file_size)
    log(f"Done: {cfg['output']} ({file_size / 1e9:.2f} GB)")


# ----------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NUP Packer")
        self.geometry("720x560")
        self.q = queue.Queue()
        self.v = {
            k: tk.StringVar()
            for k in (
                "folder",
                "update",
                "output",
                "title_id",
                "title",
                "version",
                "update_version",
                "display_version",
                "icon",
                "zstd",
            )
        }
        self.v["version"].set("1.0.0")
        self.v["update_version"].set("1.0.1")
        self.v["zstd"].set("0")
        self.standalone = tk.BooleanVar()

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        r = 0

        def row(label, key, browse=None, extra=None):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(f, textvariable=self.v[key]).grid(
                row=r, column=1, sticky="ew", pady=2, padx=4
            )
            if browse:
                ttk.Button(f, text="Browse…", command=browse).grid(row=r, column=2)
            if extra:
                ttk.Button(f, text=extra[0], command=extra[1]).grid(row=r, column=2)
            r += 1

        row("Input folder (base or MKW)", "folder", self.pick_folder)
        row("Update folder (optional)", "update", lambda: self.pick_dir("update"))
        row("Output .nup", "output", self.pick_output)
        row("Title ID (hex)", "title_id", extra=("Auto", self.auto_tid))
        row("Title name", "title")
        row("Base version", "version")
        row("Update version", "update_version")
        row("Display version", "display_version")
        row("Icon (optional)", "icon", self.pick_icon)
        row("zstd level (0 = raw)", "zstd")
        ttk.Checkbutton(
            f,
            text="Standalone update (update has complete tree)",
            variable=self.standalone,
        ).grid(row=r, column=1, sticky="w")
        r += 1

        ttk.Label(f, text="Localized titles (lang=name, one per line)").grid(
            row=r, column=0, columnspan=3, sticky="w"
        )
        r += 1
        self.loc = tk.Text(f, height=3)
        self.loc.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1

        self.btn = ttk.Button(f, text="Build NUP", command=self.start)
        self.btn.grid(row=r, column=0, columnspan=3, pady=6)
        r += 1
        self.bar = ttk.Progressbar(f, maximum=1.0)
        self.bar.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1
        self.logbox = tk.Text(f, height=8, state="disabled")
        self.logbox.grid(row=r, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(r, weight=1)
        self.after(100, self.poll)

    # pickers
    def pick_folder(self):
        d = filedialog.askdirectory(title="Select base or MKW folder")
        if d:
            self.v["folder"].set(d)
            if not self.v["title_id"].get():
                self.auto_tid()
            if not self.v["output"].get():
                self.v["output"].set(
                    str(Path(d).with_suffix(".nup"))
                    if (Path(d) / "exefs").is_dir()
                    else str(Path(d) / f"{Path(d).name}.nup")
                )

    def pick_dir(self, key):
        d = filedialog.askdirectory()
        if d:
            self.v[key].set(d)

    def pick_output(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".nup", filetypes=[("NUP", "*.nup")]
        )
        if p:
            self.v["output"].set(p)

    def pick_icon(self):
        p = filedialog.askopenfilename()
        if p:
            self.v["icon"].set(p)

    def auto_tid(self):
        folder = self.v["folder"].get()
        if not folder:
            return
        base, _ = resolve_inputs(Path(folder))
        tid = guess_title_id(base) if base else ""
        if tid:
            self.v["title_id"].set(tid)
        else:
            self.log("Couldn't read title ID from main.npdm; enter it manually.")

    # logging / progress
    def log(self, msg):
        self.q.put(("log", msg))

    def prog(self, x):
        self.q.put(("prog", x))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.config(state="normal")
                    self.logbox.insert("end", val + "\n")
                    self.logbox.see("end")
                    self.logbox.config(state="disabled")
                elif kind == "prog":
                    self.bar["value"] = val
                elif kind == "end":
                    self.btn.config(state="normal")
                    if val:
                        messagebox.showerror("Error", val)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # run
    def start(self):
        try:
            loc = []
            for line in self.loc.get("1.0", "end").splitlines():
                if "=" in line:
                    l, t = line.split("=", 1)
                    loc.append((l.strip(), t.strip()))
            cfg = {k: v.get().strip() for k, v in self.v.items()}
            cfg["zstd"] = int(cfg["zstd"] or 0)
            cfg["standalone"] = self.standalone.get()
            cfg["loc_titles"] = loc
            if not cfg["folder"] or not cfg["output"] or not cfg["title_id"]:
                raise ValueError("Input folder, output file and title ID are required.")
            int(cfg["title_id"], 16)
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return
        self.btn.config(state="disabled")
        self.bar["value"] = 0
        threading.Thread(target=self.work, args=(cfg,), daemon=True).start()

    def work(self, cfg):
        err = ""
        try:
            build_nup(cfg, self.log, self.prog)
        except ImportError:
            err = "zstd needs the 'zstandard' package:  pip install zstandard"
        except Exception as e:
            err = str(e)
            self.log(traceback.format_exc())
        self.q.put(("end", err))


def main():
    App().mainloop()


if __name__ == "__main__":
    App().mainloop()
