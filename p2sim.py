#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# p2sim - simulates the Protocol 2 discovery/erase/program side of an
# openHPSDR FPGA (modelled on HermesC10 sdr_receive.v / ASMI_interface.v),
# for testing p2flash without a radio.
#
#   ./p2sim.py --port 11024 --out flashed.bin
#   ./p2flash.py --port 11024 flash 127.0.0.1 firmware.rbf
#
# Copyright (C) 2026 Birger Kuepper, DG6EU

import argparse
import socket
import struct
import threading
import time


class SimRadio:
    def __init__(self, port=11024, board=20, code=110, beta=5, running=False,
                 erase_time=1.0, flash_size=0x100000, checksum=True,
                 drop_reply=(), drop_packet=(), bind="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.port = self.sock.getsockname()[1]
        self.mac = bytes([0x40, 0x84, 0x32, 0xb0, 0x7c, 0x08])
        self.board, self.code, self.beta = board, code, beta
        self.running = running
        self.erase_time = erase_time
        self.flash = bytearray(b"\xff" * flash_size)
        self.checksum_enabled = checksum
        self.drop_reply = set(drop_reply)       # block numbers whose ack gets lost
        self.drop_packet = set(drop_packet)     # block numbers whose packet gets lost
        self.state = "idle"                     # idle, erasing, programming, reloading
        self.page = 0
        self.num_blocks = 0
        self.checksum = 0
        self.erase_count = 0
        self.programmed = None                  # bytes written in the last complete run
        self.log = []
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop = True
        self.sock.close()

    def _reply(self, addr, seq, code, tail):
        pkt = struct.pack(">IB", seq, code) + self.mac + tail
        self.sock.sendto(pkt + bytes(60 - len(pkt)), addr)

    def _run(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if len(data) < 5 or self.state == "reloading":
                continue
            seq, cmd = struct.unpack(">IB", data[:5])
            if cmd == 0x02:
                status = 0x03 if self.running else 0x02
                tail = bytes([self.board, 44, self.code]) + bytes(6) + bytes([2, 1, 0, self.beta])
                self._reply(addr, 0, status, tail)
            elif cmd == 0x04:
                self.log.append("erase")
                # acknowledge is sent in any case (sdr_send: erase_done | erase)
                self._reply(addr, 0, 0x03, bytes([self.code, self.board]))
                if self.state == "idle":        # ASMI only accepts erase in state 0
                    self.state = "erasing"
                    threading.Thread(target=self._erase, args=(addr,), daemon=True).start()
            elif cmd == 0x05 and len(data) == 265:
                if seq in self.drop_packet:
                    self.drop_packet.discard(seq)
                    self.log.append("lost packet %d" % seq)
                    continue
                self._program(addr, seq, data)

    def _erase(self, addr):
        time.sleep(self.erase_time)
        self.flash[:] = b"\xff" * len(self.flash)
        self.erase_count += 1
        self.state = "idle"
        self.page = 0
        self.checksum = 0
        self._reply(addr, 0, 0x03, bytes([self.code, self.board]))

    def _program(self, addr, seq, data):
        if self.state == "erasing":
            time.sleep(self.erase_time)         # data waits in the FIFO
        if self.state == "idle":
            self.state = "programming"
            self.page = 0
            self.checksum = 0
            self.num_blocks = struct.unpack(">I", data[5:9])[0] & 0x3FFF
        chunk = data[9:265]
        self.flash[self.page * 256:(self.page + 1) * 256] = chunk
        self.checksum = (self.checksum + sum(chunk)) & 0xFFFF
        self.page += 1
        if self.page == self.num_blocks:
            self.programmed = bytes(self.flash[:self.page * 256])
        csum = self.checksum if self.checksum_enabled else 0
        tail = bytes([self.code, self.board]) + struct.pack(">H", csum)
        if seq in self.drop_reply:
            self.drop_reply.discard(seq)
            self.log.append("lost reply %d" % seq)
        else:
            self._reply(addr, seq, 0x04, tail)
        if self.page == self.num_blocks:
            self._reply(addr, seq, 0x04, tail)  # final "send more" (ASMI state 8)
            self.state = "reloading"
            threading.Thread(target=self._reload, daemon=True).start()

    def _reload(self):
        time.sleep(2)                           # ASMI state 10, then nCONFIG
        self.code, self.beta = 110, 10          # pretend the new image is v11.0.10
        self.state = "idle"


def main():
    p = argparse.ArgumentParser(description="openHPSDR Protocol 2 flash simulator")
    p.add_argument("--port", type=int, default=11024)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--board", type=int, default=20)
    p.add_argument("--erase-time", type=float, default=3.0)
    p.add_argument("--out", help="write the programmed image to this file")
    p.add_argument("--no-checksum", action="store_true")
    p.add_argument("--drop-reply", type=int, action="append", default=[])
    p.add_argument("--drop-packet", type=int, action="append", default=[])
    a = p.parse_args()
    sim = SimRadio(a.port, a.board, erase_time=a.erase_time, checksum=not a.no_checksum,
                   drop_reply=a.drop_reply, drop_packet=a.drop_packet, bind=a.bind).start()
    print("simulated radio on %s:%d (board type %d)" % (a.bind, sim.port, a.board))
    written = None
    try:
        while True:
            time.sleep(0.5)
            if sim.programmed is not None and sim.programmed is not written:
                written = sim.programmed
                print("programmed %d bytes, checksum 0x%04x" % (len(written), sum(written) & 0xFFFF))
                if a.out:
                    with open(a.out, "wb") as f:
                        f.write(written)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
