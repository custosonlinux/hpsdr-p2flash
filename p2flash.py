#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# p2flash - network firmware update for openHPSDR Protocol 2 radios
#
# Copyright (C) 2026 Birger Kuepper, DG6EU
#
# Implements the Erase/Program commands of the openHPSDR Ethernet Protocol
# (Protocol 2, spec v4.4: "Erase Packet", "Program Packet", "Command Reply
# Packet"). Plain UDP to port 1024: no root, no pcap, works through routers
# and VPNs. Flash layout limits and state machine behaviour were taken from
# the FPGA sources (Ethernet/sdr_receive.v, ASMI_interface.v) in
# https://github.com/TAPR/OpenHPSDR-Firmware.

import argparse
import hashlib
import ipaddress
import math
import os
import socket
import struct
import sys
import time

__version__ = "0.1.0"

P2_PORT = 1024
BLOCK = 256

CMD_DISCOVERY = 0x02
CMD_ERASE = 0x04
CMD_PROGRAM = 0x05

REPLY_IDLE = 0x02       # discovery reply, radio not running
REPLY_RUNNING = 0x03    # discovery reply, radio in use by a host
REPLY_ERASE = 0x03      # erase acknowledge / erase done
REPLY_PROGRAM = 0x04    # program acknowledge, "send next block"

# board type (discovery byte 11) -> (name, size of the application flash area)
# The FPGA erases this area before programming (ASMI_interface.v):
#   Hermes / HermesC10: 0x100000..0x1FFFFF, Angelia: 0x100000..0x33FFFF,
#   Orion / Orion MkII: 0x200000..0x60FFFF (rounded down to 4 MiB here).
BOARDS = {
    1: ("Hermes (ANAN-10/100) / HermesC10", 0x100000),
    2: ("Hermes (ANAN-10E/100B)", 0x100000),
    3: ("Angelia (ANAN-100D, Brick3)", 0x240000),
    4: ("Orion (ANAN-200D)", 0x400000),
    5: ("Orion MkII (ANAN-7000DLE/8000DLE)", 0x400000),
    20: ("HermesC10 Mk2PA (ANAN-G2E)", 0x100000),
}
# known board types that must NOT be flashed with this tool
UNSUPPORTED = {
    0: "Atlas/Metis (Protocol 1 only, use the bootloader)",
    6: "Hermes-Lite (uses its own update procedure)",
    10: "Saturn / ANAN-G2 (updated from its Raspberry Pi, not over UDP)",
}
# words expected in the firmware file name, used for a plausibility check
NAME_HINTS = {
    1: ("hermes",),
    2: ("hermes",),
    3: ("angelia",),
    4: ("orion",),
    5: ("orion",),
    20: ("c10", "mk2pa"),
}

PORT0_HINT = (
    "Hint: older firmware (e.g. HermesC10 v11.0.5) answers from UDP source port 0. "
    "Routers/firewalls often drop that (OPNsense: Firewall > Settings > Advanced > "
    "'Disable blocking of port 0'). On the local LAN this does not matter.")

WARN_ERASED = (
    "The application area of the flash may already be erased. The radio keeps "
    "running the old firmware from RAM - do NOT switch it off. Close all SDR "
    "programs and run p2flash again.")

WARN_PARTIAL = (
    "Programming stopped half way. The radio keeps running the old firmware from "
    "RAM, but its flash now holds an incomplete image, and the FPGA does not "
    "accept a new erase until it has been power cycled or reset. After the next "
    "power cycle the radio will most likely need the bootloader (jumper) method.")


class FlashError(Exception):
    pass


class Radio:
    def __init__(self, addr, data):
        self.addr = addr
        self.status = data[4]
        self.mac = data[5:11]
        self.board = data[11]
        self.protocol = data[12]
        self.code = data[13]
        self.num_rx = data[20] if len(data) > 20 else 0
        self.beta = data[23] if len(data) > 23 else 0

    @property
    def ip(self):
        return self.addr[0]

    @property
    def mac_str(self):
        return ":".join("%02x" % b for b in self.mac)

    @property
    def version(self):
        v = "%d.%d" % (self.code // 10, self.code % 10)
        if self.beta:
            v += ".%d" % self.beta
        return v

    @property
    def board_name(self):
        if self.board in BOARDS:
            return BOARDS[self.board][0]
        if self.board in UNSUPPORTED:
            return UNSUPPORTED[self.board]
        return "unknown board type %d" % self.board

    @property
    def running(self):
        return self.status == REPLY_RUNNING

    def describe(self):
        state = "in use by another host" if self.running else "idle"
        return ("%s  MAC %s  %s  firmware %s  protocol %d.%d  DDCs %d  (%s)"
                % (self.ip, self.mac_str, self.board_name, self.version,
                   self.protocol // 10, self.protocol % 10, self.num_rx, state))


class Image:
    """A firmware image, padded with 0xFF to whole 256 byte blocks."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            raise FlashError("%s is empty" % path)
        # An Altera/Intel .rbf starts with 0xFF padding followed by the 0x6A
        # sync byte. Catches accidentally selected .qar/.zip/.txt files.
        head = raw[:256]
        stripped = head.lstrip(b"\xff")
        if len(stripped) == len(head) or not stripped.startswith(b"\x6a"):
            raise FlashError("%s does not look like a raw FPGA image (.rbf)" % path)
        self.size = len(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.blocks = math.ceil(len(raw) / BLOCK)
        self.data = raw + b"\xff" * (self.blocks * BLOCK - len(raw))
        # running 16 bit sum after each block, as reported by the radio
        self.sums = []
        s = 0
        for i in range(self.blocks):
            s = (s + sum(self.block(i))) & 0xFFFF
            self.sums.append(s)

    @property
    def checksum(self):
        return self.sums[-1]

    def block(self, i):
        return self.data[i * BLOCK:(i + 1) * BLOCK]


def read_sha256_file(path):
    """Expected hash from '<file>.sha256' (sha256sum format), if present."""
    for candidate in (path + ".sha256", os.path.splitext(path)[0] + ".sha256"):
        try:
            with open(candidate) as f:
                word = f.read().split()
        except OSError:
            continue
        if word and len(word[0]) == 64:
            return word[0].lower()
    return None


def check_name(radio, path):
    hints = NAME_HINTS.get(radio.board)
    name = os.path.basename(path).lower()
    if not hints:
        return None
    if radio.board == 1 and "mk2pa" in name:
        return "file is a Mk2PA image, but the radio reports a non-Mk2PA Hermes board (type 1)"
    missing = [h for h in hints if h not in name]
    if missing:
        return "file name does not contain %s, expected for %s" % (
            " / ".join('"%s"' % m for m in missing), radio.board_name)
    return None


def make_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Deliberately NOT connect()ed: older firmware (e.g. HermesC10 v11.0.5)
    # answers from UDP source port 0 instead of 1024, a connected socket
    # would silently drop those replies.
    s.bind(("0.0.0.0", 0))
    return s


def recv_until(sock, deadline, accept):
    """Receive datagrams until accept(data, addr) returns a value other than
    None, or the deadline passes (returns None)."""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            return None
        result = accept(data, addr)
        if result is not None:
            return result


def discover(dest, port=P2_PORT, timeout=2.0, want_all=False):
    """Send a discovery packet (unicast or broadcast). Returns the first Radio,
    or with want_all a list of all radios that answered within the timeout."""
    radios = {}
    with make_socket() as sock:
        sock.sendto(struct.pack(">IB", 0, CMD_DISCOVERY) + bytes(55), (dest, port))

        def accept(data, addr):
            if len(data) < 14 or data[4] not in (REPLY_IDLE, REPLY_RUNNING):
                return None
            r = Radio(addr, data)
            radios[r.mac_str] = r
            return None if want_all else r

        first = recv_until(sock, time.monotonic() + timeout, accept)
    return list(radios.values()) if want_all else first


def resolve(host):
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        return socket.gethostbyname(host)


def validate(radio, image, force=False, ignore_name=False, max_size=None):
    if radio.board in UNSUPPORTED:
        raise FlashError("board type %d: %s" % (radio.board, UNSUPPORTED[radio.board]))
    if radio.board in BOARDS:
        limit = BOARDS[radio.board][1]
    elif max_size:
        limit = max_size
    else:
        raise FlashError("unknown board type %d - refusing. If you are sure, pass --max-size "
                         "with the size of the application flash area in bytes" % radio.board)
    if image.blocks * BLOCK > limit:
        raise FlashError("image (%d bytes) is larger than the flash area of this board (%d bytes)"
                         % (image.blocks * BLOCK, limit))
    if radio.running and not force:
        raise FlashError("radio is in use by another host - close the SDR program first")
    warning = check_name(radio, image.path)
    if warning and not ignore_name:
        raise FlashError(warning)


class Flasher:
    """Erase and program one radio. log(str) receives text messages,
    progress(phase, done, total) is called with phase 'erase' or 'program'."""

    def __init__(self, target, port=P2_PORT, log=print, progress=None,
                 ack_timeout=3.0, erase_timeout=90.0, block_timeout=5.0, resends=2):
        self.target = target
        self.port = port
        self.log = log
        self.progress = progress or (lambda phase, done, total: None)
        self.ack_timeout = ack_timeout
        self.erase_timeout = erase_timeout
        self.block_timeout = block_timeout
        self.resends = resends
        self.radio_checksum = None
        self.checksum_reported = None

    def erase(self):
        pkt = struct.pack(">IB", 0, CMD_ERASE) + bytes(55)

        def is_erase_reply(data, addr):
            if addr[0] == self.target and len(data) >= 5 and data[4] == REPLY_ERASE \
                    and data[:4] == b"\0\0\0\0":
                return True
            return None

        with make_socket() as sock:
            for attempt in range(3):
                sock.sendto(pkt, (self.target, self.port))
                if recv_until(sock, time.monotonic() + self.ack_timeout, is_erase_reply):
                    break
                self.log("no erase acknowledge, sending again (%d)" % (attempt + 1))
            else:
                raise FlashError("radio did not acknowledge the erase command. " + PORT0_HINT)
            t0 = time.monotonic()
            self.log("erasing flash ...")
            while True:
                elapsed = time.monotonic() - t0
                self.progress("erase", elapsed, self.erase_timeout)
                if recv_until(sock, time.monotonic() + 0.5, is_erase_reply):
                    break
                if elapsed > self.erase_timeout:
                    raise FlashError("erase not completed within %d s. %s"
                                     % (self.erase_timeout, WARN_ERASED))
            self.log("erase done after %.1f s" % (time.monotonic() - t0))

    def program(self, image):
        """Send all blocks. Each block is acknowledged with its sequence number
        and the running 16 bit sum of all bytes written so far."""
        t0 = time.monotonic()
        self.checksum_reported = None
        with make_socket() as sock:
            for seq in range(image.blocks):
                pkt = struct.pack(">IBI", seq, CMD_PROGRAM, image.blocks) + image.block(seq)
                sock.sendto(pkt, (self.target, self.port))
                resent = 0
                timeout = self.erase_timeout if seq == 0 else self.block_timeout
                while True:
                    reply = recv_until(sock, time.monotonic() + timeout,
                                       lambda d, a: self._program_reply(d, a, image, seq))
                    if reply is not None:
                        break
                    # No answer. Either our packet or the radio's reply was lost.
                    # Resending is only safe when the checksum can tell which one:
                    # a block that was written twice shows up as a wrong sum.
                    if not self.checksum_reported or resent >= self.resends:
                        raise FlashError("no reply for block %d of %d. %s"
                                         % (seq + 1, image.blocks, WARN_PARTIAL))
                    resent += 1
                    self.log("no reply for block %d, sending it again" % (seq + 1))
                    sock.sendto(pkt, (self.target, self.port))
                self.progress("program", seq + 1, image.blocks)
        self.log("programmed %d blocks in %.1f s" % (image.blocks, time.monotonic() - t0))

    def _program_reply(self, data, addr, image, seq):
        if addr[0] != self.target or len(data) < 15 or data[4] != REPLY_PROGRAM:
            return None
        rseq = struct.unpack(">I", data[:4])[0]
        rsum = struct.unpack(">H", data[13:15])[0]
        if self.checksum_reported is None:
            # firmware released before 2019 does not report a checksum (always 0)
            expected = image.sums[min(rseq, image.blocks - 1)]
            self.checksum_reported = not (rsum == 0 and expected != 0)
            if not self.checksum_reported:
                self.log("note: this firmware does not report a program checksum")
        if rseq > seq or rseq >= image.blocks:
            raise FlashError("radio acknowledged block %d, but only %d was sent. %s"
                             % (rseq + 1, seq + 1, WARN_PARTIAL))
        if self.checksum_reported and rsum != image.sums[rseq]:
            dup = (image.sums[rseq] + sum(image.block(rseq))) & 0xFFFF
            what = "block %d was written twice" % (rseq + 1) if rsum == dup else \
                "checksum mismatch after block %d: radio 0x%04x, file 0x%04x" % (
                    rseq + 1, rsum, image.sums[rseq])
            raise FlashError("%s. %s" % (what, WARN_PARTIAL))
        if rseq < seq:
            return None     # late duplicate of an earlier acknowledge
        self.radio_checksum = rsum
        return data

    def flash(self, image):
        self.erase()
        self.program(image)


def wait_for_radio(target, port, timeout, log=print):
    """Wait for the radio to come back after the automatic FPGA reload."""
    time.sleep(5)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = discover(target, port, 2.0)
        if r:
            return r
    return None


def fmt_bytes(n):
    return "{:,}".format(n).replace(",", ".")


# ---------------------------------------------------------------- command line

def cmd_discover(args):
    dest = resolve(args.target) if args.target else args.broadcast
    radios = discover(dest, args.port, args.timeout, want_all=True)
    if not radios:
        print("no radio answered at %s:%d" % (dest, args.port))
        if args.target:
            print(PORT0_HINT)
        return 1
    for r in radios:
        print(r.describe())
    return 0


def cmd_checksum(args):
    img = Image(args.file)
    print("file:     %s" % img.path)
    print("size:     %s bytes, %d blocks of 256 bytes" % (fmt_bytes(img.size), img.blocks))
    print("SHA-256:  %s" % img.sha256)
    print("sum16:    0x%04x  (the value the radio reports after programming)" % img.checksum)
    expected = args.sha256 or read_sha256_file(img.path)
    if expected:
        ok = expected.lower() == img.sha256
        print("expected: %s  %s" % (expected.lower(), "MATCH" if ok else "MISMATCH"))
        return 0 if ok else 1
    return 0


def cmd_flash(args):
    target = resolve(args.target)
    image = Image(args.file)

    print("file:     %s" % image.path)
    print("size:     %s bytes, %d blocks" % (fmt_bytes(image.size), image.blocks))
    print("SHA-256:  %s" % image.sha256)
    expected = args.sha256 or read_sha256_file(image.path)
    if expected:
        if expected.lower() != image.sha256:
            raise FlashError("SHA-256 of the file does not match the expected value %s"
                             % expected.lower())
        print("          matches the expected SHA-256")
    print("sum16:    0x%04x" % image.checksum)
    print()

    print("contacting %s ..." % target)
    radio = discover(target, args.port, args.timeout)
    if radio is None:
        raise FlashError("no discovery reply from %s. %s" % (target, PORT0_HINT))
    print("radio:    " + radio.describe())
    try:
        validate(radio, image, args.force, args.ignore_name, args.max_size)
    except FlashError as e:
        hint = " (--ignore-name if this really is the right file)" if "file name" in str(e) \
            or "Mk2PA" in str(e) else ""
        raise FlashError(str(e) + hint)
    print()
    if not args.yes:
        answer = input("Erase and reprogram %s (%s, firmware %s)? Type 'yes': "
                       % (target, radio.board_name, radio.version))
        if answer.strip().lower() != "yes":
            print("aborted, nothing changed")
            return 1

    last = [-1]

    def progress(phase, done, total):
        if phase == "program":
            pct = done * 100 // total
            if pct != last[0] and (pct % 5 == 0 or done == total):
                last[0] = pct
                bar = "#" * (pct // 4)
                print("\r  [%-25s] %3d %%  %d/%d" % (bar, pct, done, total),
                      end="\n" if done == total else "", flush=True)

    flasher = Flasher(target, args.port, log=lambda m: print("  " + m, flush=True),
                      progress=progress, erase_timeout=args.erase_timeout,
                      block_timeout=args.block_timeout)
    try:
        flasher.flash(image)
    except KeyboardInterrupt:
        raise FlashError("interrupted by user. " + WARN_PARTIAL)

    print()
    if flasher.checksum_reported:
        ok = flasher.radio_checksum == image.checksum
        print("checksum: radio 0x%04x, file 0x%04x  %s"
              % (flasher.radio_checksum, image.checksum, "OK" if ok else "MISMATCH"))
    else:
        print("checksum: not reported by this firmware")
    print("done - the radio reloads the new firmware by itself in a few seconds")
    if args.no_wait:
        return 0
    r = wait_for_radio(target, args.port, args.reboot_timeout)
    if r is None:
        print("the radio did not answer within %d s - power cycle it and check with "
              "'p2flash discover %s'" % (args.reboot_timeout, target))
        return 2
    print("radio:    " + r.describe())
    if (r.code, r.beta) == (radio.code, radio.beta):
        print("note: version number unchanged (same version flashed?)")
    return 0


def cmd_gui(args):
    import p2flash_gui
    return p2flash_gui.main(args.target, args.file)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="p2flash",
        description="Network firmware update for openHPSDR Protocol 2 radios "
                    "(ANAN-G2E, Angelia/Brick3, Orion, Hermes ...). Plain UDP, no root needed.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("--port", type=int, default=P2_PORT, help=argparse.SUPPRESS)
    p.add_argument("--timeout", type=float, default=2.0,
                   help="discovery timeout in seconds (default 2)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find radios (broadcast) or query one radio")
    d.add_argument("target", nargs="?", help="IP address of a radio (unicast, works over VPN)")
    d.add_argument("--broadcast", default="255.255.255.255",
                   help="broadcast address (default 255.255.255.255)")
    d.set_defaults(func=cmd_discover)

    c = sub.add_parser("checksum", help="show SHA-256 and radio checksum of a firmware file")
    c.add_argument("file")
    c.add_argument("--sha256", help="expected SHA-256 to compare with")
    c.set_defaults(func=cmd_checksum)

    f = sub.add_parser("flash", help="erase and program the firmware")
    f.add_argument("target", help="IP address or host name of the radio")
    f.add_argument("file", help="firmware image (.rbf)")
    f.add_argument("--sha256", help="expected SHA-256 of the file (default: read from "
                                    "<file>.sha256 if present)")
    f.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    f.add_argument("--force", action="store_true",
                   help="flash even if the radio reports being in use")
    f.add_argument("--ignore-name", action="store_true",
                   help="skip the file name / board type plausibility check")
    f.add_argument("--max-size", type=int, help="flash area size for unknown board types")
    f.add_argument("--erase-timeout", type=float, default=90.0,
                   help="seconds to wait for erase completion (default 90)")
    f.add_argument("--block-timeout", type=float, default=5.0,
                   help="seconds to wait for each block acknowledge (default 5)")
    f.add_argument("--reboot-timeout", type=float, default=60.0,
                   help="seconds to wait for the radio after the update (default 60)")
    f.add_argument("--no-wait", action="store_true",
                   help="do not wait for the radio to come back")
    f.set_defaults(func=cmd_flash)

    g = sub.add_parser("gui", help="start the graphical interface")
    g.add_argument("target", nargs="?")
    g.add_argument("file", nargs="?")
    g.set_defaults(func=cmd_gui)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (FlashError, OSError) as e:
        print("ERROR: %s" % e)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
