#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# p2flash_gui - small Tk front end for p2flash
#
# Copyright (C) 2026 Birger Kuepper, DG6EU

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import p2flash as core

OK_COLOR = "#1a7f37"
BAD_COLOR = "#c62828"


class App:
    def __init__(self, root, target=None, path=None):
        self.root = root
        self.events = queue.Queue()
        self.image = None
        self.radio = None
        self.busy = False
        self.radios = {}

        root.title("p2flash %s - openHPSDR Protocol 2 firmware update" % core.__version__)
        root.minsize(860, 760)
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # ---- radio
        radio_box = ttk.LabelFrame(frm, text="Radio", padding=8)
        radio_box.grid(row=0, column=0, columnspan=3, sticky="ew", **pad)
        radio_box.columnconfigure(1, weight=1)
        ttk.Label(radio_box, text="IP address:").grid(row=0, column=0, sticky="w")
        self.target = tk.StringVar(value=target or "")
        self.target_box = ttk.Combobox(radio_box, textvariable=self.target)
        self.target_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.target_box.bind("<<ComboboxSelected>>", lambda e: self.query())
        self.target_box.bind("<Return>", lambda e: self.query())
        self.btn_search = ttk.Button(radio_box, text="Search LAN", command=self.search)
        self.btn_search.grid(row=0, column=2, padx=2)
        self.btn_query = ttk.Button(radio_box, text="Query", command=self.query)
        self.btn_query.grid(row=0, column=3, padx=2)
        self.radio_info = ttk.Label(radio_box, text="no radio selected", foreground="gray",
                                    wraplength=780)
        self.radio_info.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- firmware file
        file_box = ttk.LabelFrame(frm, text="Firmware file", padding=8)
        file_box.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
        file_box.columnconfigure(1, weight=1)
        ttk.Label(file_box, text="File:").grid(row=0, column=0, sticky="w")
        self.path = tk.StringVar(value=path or "")
        ttk.Entry(file_box, textvariable=self.path).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(file_box, text="Browse ...", command=self.browse).grid(row=0, column=2)
        self.file_info = ttk.Label(file_box, text="", foreground="gray", wraplength=780)
        self.file_info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(file_box, text="SHA-256:").grid(row=2, column=0, sticky="w")
        self.sha_actual = ttk.Label(file_box, text="-", font="TkFixedFont")
        self.sha_actual.grid(row=2, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Label(file_box, text="Expected:").grid(row=3, column=0, sticky="w")
        self.sha_expected = tk.StringVar()
        self.sha_expected.trace_add("write", lambda *a: self.compare_sha())
        ttk.Entry(file_box, textvariable=self.sha_expected, font="TkFixedFont").grid(
            row=3, column=1, sticky="ew", padx=6)
        self.sha_result = ttk.Label(file_box, text="", width=12)
        self.sha_result.grid(row=3, column=2, sticky="w")
        ttk.Label(file_box, text="optional - or a <file>.sha256 next to the firmware",
                  foreground="gray").grid(row=4, column=1, columnspan=2, sticky="w", padx=6)
        self.path.trace_add("write", lambda *a: self.load_file())

        # ---- options
        opt = ttk.Frame(frm)
        opt.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        self.ignore_name = tk.BooleanVar()
        self.force = tk.BooleanVar()
        ttk.Checkbutton(opt, text="skip file name check", variable=self.ignore_name).pack(side="left")
        ttk.Checkbutton(opt, text="flash even if the radio is in use",
                        variable=self.force).pack(side="left", padx=12)

        # ---- progress
        prog_box = ttk.LabelFrame(frm, text="Progress", padding=8)
        prog_box.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        prog_box.columnconfigure(0, weight=1)
        self.phase = ttk.Label(prog_box, text="ready")
        self.phase.grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(prog_box, mode="determinate", maximum=100)
        self.bar.grid(row=1, column=0, sticky="ew", pady=4)
        self.csum_result = ttk.Label(prog_box, text="", font="TkFixedFont")
        self.csum_result.grid(row=2, column=0, sticky="w")

        # ---- buttons
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", **pad)
        self.btn_flash = ttk.Button(btns, text="Flash firmware", command=self.flash)
        self.btn_flash.pack(side="right")

        # ---- log
        frm.rowconfigure(5, weight=1)
        self.log_text = tk.Text(frm, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=5, column=0, columnspan=3, sticky="nsew", **pad)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if path:
            self.load_file()
        if target:
            self.query()

    # ------------------------------------------------------------ helpers

    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run_bg(self, fn, *args):
        """Run fn in a worker thread; its return value arrives as ('done', fn, value)."""
        def worker():
            try:
                self.events.put(("done", fn.__name__, fn(*args)))
            except Exception as e:     # reported in the GUI
                self.events.put(("error", fn.__name__, e))
        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_search, self.btn_query, self.btn_flash):
            b.configure(state=state)

    def poll(self):
        try:
            while True:
                self.handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    # ------------------------------------------------------------ radio

    def search(self):
        self.log("searching the LAN (broadcast) ...")
        self.set_busy(True)
        self.run_bg(lambda: core.discover("255.255.255.255", want_all=True))

    def query(self):
        host = self.target.get().strip().split()[0] if self.target.get().strip() else ""
        if not host:
            return
        self.log("querying %s ..." % host)
        self.set_busy(True)

        def do_query():
            ip = core.resolve(host)
            return ip, core.discover(ip)
        do_query.__name__ = "query"
        self.run_bg(do_query)

    def show_radio(self, radio):
        self.radio = radio
        if radio is None:
            self.radio_info.configure(text="no reply - wrong address, radio off, or firewall "
                                           "(see log)", foreground=BAD_COLOR)
            return
        state = "IN USE by another program" if radio.running else "idle"
        color = BAD_COLOR if radio.running or radio.board not in core.BOARDS else OK_COLOR
        self.radio_info.configure(
            text="%s   MAC %s   firmware %s   %s" % (radio.board_name, radio.mac_str,
                                                   radio.version, state), foreground=color)

    # ------------------------------------------------------------ file

    def browse(self):
        start = os.path.dirname(self.path.get()) or os.path.expanduser("~")
        p = filedialog.askopenfilename(initialdir=start, title="Firmware image",
                                       filetypes=[("FPGA image", "*.rbf"), ("all files", "*")])
        if p:
            self.path.set(p)

    def load_file(self):
        p = self.path.get().strip()
        self.image = None
        self.sha_actual.configure(text="-")
        if not p or not os.path.isfile(p):
            self.file_info.configure(text="" if not p else "file not found", foreground=BAD_COLOR)
            self.compare_sha()
            return
        try:
            self.image = core.Image(p)
        except (core.FlashError, OSError) as e:
            self.file_info.configure(text=str(e), foreground=BAD_COLOR)
            self.compare_sha()
            return
        img = self.image
        self.file_info.configure(
            text="%s bytes, %d blocks, radio checksum (sum16) 0x%04x"
                 % (core.fmt_bytes(img.size), img.blocks, img.checksum), foreground="")
        self.sha_actual.configure(text=img.sha256)
        expected = core.read_sha256_file(p)
        if expected:
            self.sha_expected.set(expected)
            self.log("expected SHA-256 read from %s.sha256" % os.path.basename(p))
        self.compare_sha()

    def compare_sha(self):
        exp = self.sha_expected.get().strip().lower()
        if not exp or not self.image:
            self.sha_result.configure(text="")
        elif exp == self.image.sha256:
            self.sha_result.configure(text="✔ match", foreground=OK_COLOR)
        else:
            self.sha_result.configure(text="✘ MISMATCH", foreground=BAD_COLOR)

    # ------------------------------------------------------------ flashing

    def flash(self):
        if not self.image:
            messagebox.showerror("p2flash", "Select a valid firmware file first.")
            return
        if not self.radio:
            messagebox.showerror("p2flash", "Query the radio first.")
            return
        exp = self.sha_expected.get().strip().lower()
        if exp and exp != self.image.sha256:
            messagebox.showerror("p2flash", "The SHA-256 of the file does not match the "
                                            "expected value. Not flashing.")
            return
        try:
            core.validate(self.radio, self.image, self.force.get(), self.ignore_name.get())
        except core.FlashError as e:
            messagebox.showerror("p2flash", str(e))
            return
        if not messagebox.askyesno(
                "Flash firmware",
                "Erase and reprogram this radio?\n\n"
                "Radio:  %s  (%s)\nNow:    firmware %s\nFile:   %s\n\n"
                "Do not switch the radio off and do not close this window until the "
                "update has finished." % (self.radio.ip, self.radio.board_name,
                                           self.radio.version, os.path.basename(self.image.path)),
                icon="warning"):
            return
        self.set_busy(True)
        self.csum_result.configure(text="")
        self.phase.configure(text="starting ...", foreground="")
        self.bar.configure(mode="determinate", value=0)
        self.log("flashing %s -> %s" % (os.path.basename(self.image.path), self.radio.ip))
        radio, image = self.radio, self.image

        def post_log(msg):
            self.events.put(("log", None, msg))

        def post_progress(phase, done, total):
            self.events.put(("progress", phase, (done, total)))

        def do_flash():
            f = core.Flasher(radio.ip, log=post_log, progress=post_progress)
            f.flash(image)
            post_log("done - the radio now reloads the new firmware")
            self.events.put(("checksum", None, (f.checksum_reported, f.radio_checksum)))
            self.events.put(("progress", "reboot", (0, 0)))
            return core.wait_for_radio(radio.ip, core.P2_PORT, 60)
        do_flash.__name__ = "flash"
        self.run_bg(do_flash)

    # ------------------------------------------------------------ events

    def handle(self, kind, what, value):
        if kind == "log":
            self.log(value)
        elif kind == "progress":
            done, total = value
            if what == "erase":
                self.phase.configure(text="erasing flash ... %.0f s" % done)
                if str(self.bar.cget("mode")) != "indeterminate":
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(15)
            elif what == "program":
                if str(self.bar.cget("mode")) != "determinate":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                self.bar.configure(value=100.0 * done / total)
                self.phase.configure(text="programming ... block %d of %d (%d %%)"
                                          % (done, total, done * 100 // total))
            elif what == "reboot":
                self.phase.configure(text="waiting for the radio to restart ...")
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
        elif kind == "checksum":
            reported, rsum = value
            if not reported:
                self.csum_result.configure(text="checksum: not reported by this firmware",
                                           foreground="gray")
            elif rsum == self.image.checksum:
                self.csum_result.configure(
                    text="✔ checksum: radio 0x%04x = file 0x%04x" % (rsum, self.image.checksum),
                    foreground=OK_COLOR)
            else:
                self.csum_result.configure(
                    text="✘ checksum: radio 0x%04x != file 0x%04x" % (rsum, self.image.checksum),
                    foreground=BAD_COLOR)
        elif kind == "done":
            self.set_busy(False)
            if what == "<lambda>":             # search
                self.radios = {"%s  (%s, %s)" % (r.ip, r.board_name, r.version): r for r in value}
                self.target_box.configure(values=list(self.radios))
                self.log("found %d radio(s)" % len(value))
                for r in value:
                    self.log("  " + r.describe())
                if len(value) == 1:
                    self.target.set(list(self.radios)[0])
                    self.show_radio(value[0])
                elif not value:
                    self.log("no radio found - enter the IP address and press Query "
                             "(broadcast does not cross routers or VPNs)")
            elif what == "query":
                ip, radio = value
                self.show_radio(radio)
                if radio:
                    self.log("  " + radio.describe())
                else:
                    self.log("no reply from %s. %s" % (ip, core.PORT0_HINT))
            elif what == "flash":
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self.bar["value"] = 100
                if value is None:
                    self.phase.configure(text="flashed, but the radio did not come back - "
                                              "power cycle it", foreground=BAD_COLOR)
                    self.log("the radio did not answer within 60 s after the update")
                else:
                    self.phase.configure(text="finished - radio runs firmware %s" % value.version,
                                         foreground=OK_COLOR)
                    self.log("radio is back: " + value.describe())
                    self.show_radio(value)
        elif kind == "error":
            self.set_busy(False)
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.phase.configure(text="ERROR", foreground=BAD_COLOR)
            self.log("ERROR: %s" % value)
            if what == "flash":
                messagebox.showerror("p2flash", str(value))

    def close(self):
        if self.busy and not messagebox.askyesno(
                "p2flash", "An operation is still running. Closing now can leave the radio "
                           "with an incomplete firmware. Close anyway?", icon="warning"):
            return
        self.root.destroy()


def main(target=None, path=None):
    root = tk.Tk()
    if sys.platform.startswith("linux"):
        style = ttk.Style()
        style.theme_use("clam")           # the default Tk theme looks dated on Linux
        # clam draws the bar almost in the trough colour, so a full bar looks empty
        style.configure("Horizontal.TProgressbar", background="#3b7dd8")
    App(root, target, path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
