#!/usr/bin/env python3
"""nup_packer_gui.py - PySide6 GUI that builds an Eden .nup from loose exefs/romfs trees."""

import hashlib, struct, sys, traceback, uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

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
    base, upd = folder / "base", folder / "update"
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
    """Best-effort read of the program id from main.npdm (ACI0). Switch 1 layout assumed."""
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
        s.meta, s.meta_off = m + meta, 0


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
                disp + (b"" if base_dir else base_meta),
            )
        )
    log("Packages: " + ", ".join("base" if p.kind == 0 else "update" for p in pkgs))

    dirs, files = [], []
    for p in pkgs:
        collect(p.root, dirs, files)
    D, F, P = len(dirs), len(files), len(pkgs)
    total_bytes = max(sum(f.path.stat().st_size for f in files), 1)
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

    cctx = None
    if cfg["zstd"] > 0:
        import zstandard

        cctx = zstandard.ZstdCompressor(level=cfg["zstd"])

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
                else:  # not smaller -> store raw
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
            progress(done / total_bytes)
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


# ----------------------------------------------------------------- worker thread
class BuildThread(QThread):
    log = Signal(str)
    progress = Signal(int)  # 0..1000
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):
        try:
            build_nup(
                self.cfg, self.log.emit, lambda x: self.progress.emit(int(x * 1000))
            )
            self.finished_ok.emit()
        except ImportError:
            self.failed.emit(
                "zstd needs the 'zstandard' package:  pip install zstandard"
            )
        except Exception as e:
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(e))


# ----------------------------------------------------------------- GUI
class PathEdit(QWidget):
    """Line edit + Browse button; accepts drag-and-drop of a path."""

    def __init__(self, mode="dir", save_filter="", parent=None):
        super().__init__(parent)
        self.mode, self.save_filter = mode, save_filter
        self.edit = QLineEdit()
        btn = QPushButton("Browse…")
        btn.clicked.connect(self.browse)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)
        self.setAcceptDrops(True)
        self.edit.setAcceptDrops(False)

    def text(self):
        return self.edit.text().strip()

    def setText(self, t):
        self.edit.setText(t)

    def browse(self):
        if self.mode == "dir":
            p = QFileDialog.getExistingDirectory(self, "Select folder")
        elif self.mode == "open":
            p, _ = QFileDialog.getOpenFileName(self, "Select file")
        else:
            p, _ = QFileDialog.getSaveFileName(
                self, "Output file", "", self.save_filter
            )
        if p:
            self.setText(p)
            self.changed()

    def changed(self):
        pass

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.setText(urls[0].toLocalFile())
            self.changed()


class FolderEdit(PathEdit):
    def __init__(self, on_change, **kw):
        super().__init__(**kw)
        self._cb = on_change

    def changed(self):
        self._cb()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NUP Packer")
        self.resize(720, 640)
        self._thread = None

        self.folder = FolderEdit(self.on_folder, mode="dir")
        self.update_folder = PathEdit("dir")
        self.output = PathEdit("save", "NUP (*.nup)")
        self.title_id = QLineEdit()
        self.title_id.setPlaceholderText("0100ABCD00000000")
        auto = QPushButton("Auto")
        auto.clicked.connect(self.auto_tid)
        tid_row = QHBoxLayout()
        tid_row.addWidget(self.title_id, 1)
        tid_row.addWidget(auto)
        self.title = QLineEdit()
        self.version = QLineEdit("1.0.0")
        self.update_version = QLineEdit("1.0.1")
        self.display_version = QLineEdit()
        self.icon = PathEdit("open")
        self.zstd = QSpinBox()
        self.zstd.setRange(0, 22)
        self.zstd.setSpecialValueText("0 (raw)")
        self.standalone = QCheckBox(
            "Standalone update (update contains the complete tree)"
        )
        self.loc = QPlainTextEdit()
        self.loc.setFixedHeight(60)
        self.loc.setPlaceholderText("fr=Mario Kart World\nja=マリオカート ワールド")
        self.btn = QPushButton("Build NUP")
        self.btn.clicked.connect(self.start)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)

        form = QFormLayout()
        form.addRow("Input folder (base or MKW)", self.folder)
        form.addRow("Update folder (optional)", self.update_folder)
        form.addRow("Output .nup", self.output)
        form.addRow("Title ID (hex)", tid_row)
        form.addRow("Title name", self.title)
        form.addRow("Base version", self.version)
        form.addRow("Update version", self.update_version)
        form.addRow("Display version", self.display_version)
        form.addRow("Icon (optional)", self.icon)
        form.addRow("zstd level", self.zstd)
        form.addRow("", self.standalone)
        form.addRow(QLabel("Localized titles (lang=name, one per line)"))
        form.addRow(self.loc)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.btn)
        lay.addWidget(self.bar)
        lay.addWidget(self.logbox, 1)

    def log(self, msg):
        self.logbox.appendPlainText(msg)

    def on_folder(self):
        d = self.folder.text()
        if not d:
            return
        if not self.title_id.text():
            self.auto_tid()
        if not self.output.text():
            p = Path(d)
            self.output.setText(
                str(p.with_suffix(".nup"))
                if (p / "exefs").is_dir()
                else str(p / f"{p.name}.nup")
            )

    def auto_tid(self):
        folder = self.folder.text()
        if not folder:
            return
        base, _ = resolve_inputs(Path(folder))
        tid = guess_title_id(base) if base else ""
        if tid:
            self.title_id.setText(tid)
        else:
            self.log("Couldn't read title ID from main.npdm; enter it manually.")

    def start(self):
        try:
            loc = []
            for line in self.loc.toPlainText().splitlines():
                if "=" in line:
                    l, t = line.split("=", 1)
                    loc.append((l.strip(), t.strip()))
            cfg = {
                "folder": self.folder.text(),
                "update": self.update_folder.text(),
                "output": self.output.text(),
                "title_id": self.title_id.text().strip(),
                "title": self.title.text().strip(),
                "version": self.version.text().strip(),
                "update_version": self.update_version.text().strip(),
                "display_version": self.display_version.text().strip(),
                "icon": self.icon.text(),
                "zstd": self.zstd.value(),
                "standalone": self.standalone.isChecked(),
                "loc_titles": loc,
            }
            if not cfg["folder"] or not cfg["output"] or not cfg["title_id"]:
                raise ValueError("Input folder, output file and title ID are required.")
            int(cfg["title_id"], 16)
        except Exception as e:
            QMessageBox.critical(self, "Invalid input", str(e))
            return
        self.btn.setEnabled(False)
        self.bar.setValue(0)
        self._thread = BuildThread(cfg, self)
        self._thread.log.connect(self.log)
        self._thread.progress.connect(self.bar.setValue)
        self._thread.finished_ok.connect(lambda: self.btn.setEnabled(True))
        self._thread.failed.connect(self.on_failed)
        self._thread.start()

    def on_failed(self, msg):
        self.btn.setEnabled(True)
        QMessageBox.critical(self, "Error", msg)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
