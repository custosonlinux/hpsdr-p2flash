# SPDX-License-Identifier: GPL-3.0-or-later
# Run with:  python3 -m unittest discover -s tests -v

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import p2flash as core           # noqa: E402
from p2sim import SimRadio       # noqa: E402


def make_rbf(size=20000, seed=1):
    """Fake FPGA image with a valid .rbf header."""
    body = bytes((i * 7 + seed) & 0xFF for i in range(size - 33))
    return b"\xff" * 32 + b"\x6a" + body


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "Hermes_Protocol_2_C10_v11.0.10_Mk2PA.rbf")
        with open(self.path, "wb") as f:
            f.write(make_rbf())
        self.image = core.Image(self.path)
        self.sims = []

    def tearDown(self):
        for s in self.sims:
            s.stop()
        self.tmp.cleanup()

    def sim(self, **kw):
        kw.setdefault("port", 0)
        kw.setdefault("erase_time", 0.2)
        s = SimRadio(**kw).start()
        self.sims.append(s)
        return s

    def flasher(self, sim, **kw):
        self.messages = []
        kw.setdefault("block_timeout", 0.5)
        kw.setdefault("erase_timeout", 5)
        return core.Flasher("127.0.0.1", sim.port, log=self.messages.append, **kw)


class TestImage(Base):
    def test_padding_and_checksum(self):
        img = self.image
        self.assertEqual(len(img.data) % 256, 0)
        self.assertEqual(img.blocks, (20000 + 255) // 256)
        self.assertTrue(img.data.endswith(b"\xff" * (img.blocks * 256 - 20000)))
        self.assertEqual(img.checksum, sum(img.data) & 0xFFFF)

    def test_rejects_non_rbf(self):
        bad = os.path.join(self.tmp.name, "x.qar")
        with open(bad, "wb") as f:
            f.write(b"PK\x03\x04" + bytes(100))
        with self.assertRaises(core.FlashError):
            core.Image(bad)

    def test_sha256_file(self):
        with open(self.path + ".sha256", "w") as f:
            f.write("%s  %s\n" % (self.image.sha256, os.path.basename(self.path)))
        self.assertEqual(core.read_sha256_file(self.path), self.image.sha256)


class TestValidate(Base):
    def radio(self, sim):
        return core.discover("127.0.0.1", sim.port, 1.0)

    def test_discover(self):
        r = self.radio(self.sim())
        self.assertEqual(r.board, 20)
        self.assertEqual(r.version, "11.0.5")
        self.assertEqual(r.mac_str, "40:84:32:b0:7c:08")

    def test_running_radio_refused(self):
        r = self.radio(self.sim(running=True))
        with self.assertRaises(core.FlashError):
            core.validate(r, self.image)
        core.validate(r, self.image, force=True)

    def test_wrong_board_name(self):
        r = self.radio(self.sim(board=3))       # Angelia
        with self.assertRaisesRegex(core.FlashError, "angelia"):
            core.validate(r, self.image)
        core.validate(r, self.image, ignore_name=True)

    def test_unsupported_board(self):
        r = self.radio(self.sim(board=10))      # Saturn
        with self.assertRaises(core.FlashError):
            core.validate(r, self.image, ignore_name=True, force=True)

    def test_image_too_large(self):
        big = os.path.join(self.tmp.name, "Hermes_C10_Mk2PA_big.rbf")
        with open(big, "wb") as f:
            f.write(make_rbf(0x100001))
        r = self.radio(self.sim())
        with self.assertRaisesRegex(core.FlashError, "larger"):
            core.validate(r, core.Image(big))


class TestFlash(Base):
    def test_clean_flash(self):
        sim = self.sim()
        progress = []
        f = self.flasher(sim, progress=lambda p, d, t: progress.append((p, d, t)))
        f.flash(self.image)
        self.assertEqual(sim.programmed, self.image.data)
        self.assertTrue(f.checksum_reported)
        self.assertEqual(f.radio_checksum, self.image.checksum)
        self.assertEqual(progress[-1], ("program", self.image.blocks, self.image.blocks))
        self.assertEqual(sim.erase_count, 1)
        r = core.wait_for_radio("127.0.0.1", sim.port, 10)
        self.assertEqual(r.version, "11.0.10")

    def test_lost_reply_is_recovered(self):
        # the radio wrote block 5, only its acknowledge got lost:
        # resending would write it twice -> must be detected, not ignored
        sim = self.sim(drop_reply=[5])
        with self.assertRaisesRegex(core.FlashError, "written twice"):
            self.flasher(sim).flash(self.image)

    def test_lost_packet_is_recovered(self):
        # block 5 never reached the radio: resending it is correct
        sim = self.sim(drop_packet=[5])
        f = self.flasher(sim)
        f.flash(self.image)
        self.assertEqual(sim.programmed, self.image.data)
        self.assertTrue(any("sending it again" in m for m in self.messages))

    def test_no_resend_without_checksum(self):
        sim = self.sim(checksum=False, drop_packet=[5])
        with self.assertRaisesRegex(core.FlashError, "no reply for block 6"):
            self.flasher(sim).flash(self.image)

    def test_old_firmware_without_checksum(self):
        sim = self.sim(checksum=False)
        f = self.flasher(sim)
        f.flash(self.image)
        self.assertFalse(f.checksum_reported)
        self.assertEqual(sim.programmed, self.image.data)

    def test_no_radio(self):
        s = SimRadio(port=0)
        port = s.port
        s.sock.close()
        f = core.Flasher("127.0.0.1", port, log=lambda m: None, ack_timeout=0.2)
        with self.assertRaisesRegex(core.FlashError, "acknowledge"):
            f.flash(self.image)


class TestCli(Base):
    def test_cli_flash(self):
        sim = self.sim()
        rc = core.main(["--port", str(sim.port), "flash", "127.0.0.1", self.path,
                        "--yes", "--reboot-timeout", "10"])
        self.assertEqual(rc, 0)
        self.assertEqual(sim.programmed, self.image.data)

    def test_cli_sha_mismatch(self):
        sim = self.sim()
        rc = core.main(["--port", str(sim.port), "flash", "127.0.0.1", self.path,
                        "--yes", "--sha256", "0" * 64])
        self.assertEqual(rc, 1)
        self.assertIsNone(sim.programmed)
        self.assertEqual(sim.erase_count, 0)


if __name__ == "__main__":
    unittest.main()
