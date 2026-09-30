# hpsdr-p2flash

Network firmware update for **openHPSDR Protocol 2** radios (ANAN-G2E, Angelia/Brick3,
Orion, Orion MkII, Hermes …) from **Linux and macOS**, with a command line tool and
a small GUI.

* plain UDP to port 1024: **no root, no pcap, no jumper**
* works across routers and VPNs (unicast)
* progress bar, SHA-256 check of the firmware file, and verification of the
  checksum the radio reports for every written block
* single Python 3 file plus an optional Tk GUI, no dependencies beyond the
  Python standard library

> **Status:** version 0.1.0 has been tested against the included simulator, which is
> modelled on the HermesC10 FPGA source code. It has **not yet been tested on real
> hardware**. Until it has, keep the bootloader (jumper) method at hand as a fallback.

## Why

Once a radio runs Protocol 2 firmware, the FPGA itself can erase and reprogram its
flash over the network (openHPSDR Ethernet Protocol, "Erase" and "Program" commands).
The existing tools for this are Windows programs (Thetis/Apache Labs) or the TAPR
[OpenHPSDR-Protocol2-Programmers](https://github.com/TAPR/OpenHPSDR-Protocol2-Programmers)
from 2017. That repository is unmaintained, its command line source file is actually
an HTML page, it only discovers by broadcast, it has no timeouts, and it does not
know newer boards. The P1 `bootloader` (e.g. from piHPSDR) needs root, raw Ethernet,
the same L2 segment, and the bootloader jumper set inside the radio.

## Requirements

* Python 3.8 or newer. The GUI also needs Tk (`python3-tk` on Debian/Ubuntu,
  `tk` on Arch, included in the python.org macOS installer, `brew install python-tk`).
* The radio must already run **Protocol 2** firmware. To switch from Protocol 1 to
  Protocol 2 the first time, use the bootloader.
* No SDR program may be connected to the radio while flashing.

## Usage

### GUI

```
./p2flash.py gui                     # or: ./p2flash_gui.py
./p2flash.py gui 10.83.1.200 Hermes_Protocol_2_C10_v11.0.10_Mk2PA.rbf
```

1. Enter the radio's IP address and press **Query**. **Search LAN** only works in the
   local subnet, because broadcasts do not cross routers or VPNs.
2. Choose the `.rbf` file. The SHA-256 is shown. Paste the published hash into
   *Expected*, or put a `<file>.sha256` next to the firmware; it then shows
   ✔ match or ✘ MISMATCH.
3. Press **Flash firmware**. The progress bar follows erase and programming. At the end
   the tool compares the checksum reported by the radio with the file and waits until
   the radio is back with its new version.

### Command line

```
./p2flash.py discover                       # broadcast in the local LAN
./p2flash.py discover 10.83.1.200           # query one radio (works over VPN)
./p2flash.py checksum FILE.rbf [--sha256 HASH]
./p2flash.py flash 10.83.1.200 FILE.rbf [--sha256 HASH]
```

Example:

```
$ ./p2flash.py flash 10.83.1.200 Hermes_Protocol_2_C10_v11.0.10_Mk2PA.rbf \
      --sha256 dbaa6ec7d0f88024b69dbbc9f76c0cca33d61bf5787611b60ba12b3b2cc8cb49
file:     Hermes_Protocol_2_C10_v11.0.10_Mk2PA.rbf
size:     614.251 bytes, 2400 blocks
SHA-256:  dbaa6ec7d0f88024b69dbbc9f76c0cca33d61bf5787611b60ba12b3b2cc8cb49
          matches the expected SHA-256
sum16:    0xc0ee

contacting 10.83.1.200 ...
radio:    10.83.1.200  MAC 40:84:32:b0:7c:08  HermesC10 Mk2PA (ANAN-G2E)  firmware 11.0.5 ...

Erase and reprogram 10.83.1.200 (HermesC10 Mk2PA (ANAN-G2E), firmware 11.0.5)? Type 'yes': yes
  erasing flash ...
  erase done after 4.2 s
  [#########################] 100 %  2400/2400
  programmed 2400 blocks in 3.1 s

checksum: radio 0xc0ee, file 0xc0ee  OK
done - the radio reloads the new firmware by itself in a few seconds
radio:    10.83.1.200  MAC 40:84:32:b0:7c:08  HermesC10 Mk2PA (ANAN-G2E)  firmware 11.0.10 ...
```

(The erase and programming times in this example are illustrative, not measured.)

Options for `flash`: `-y/--yes` (no prompt), `--force` (radio reports "in use"),
`--ignore-name` (skip the file name check), `--max-size` (unknown board types),
`--erase-timeout`, `--block-timeout`, `--reboot-timeout`, `--no-wait`.

## Safety checks

Before anything is erased:

* the file must be a raw FPGA image (`.rbf`: 0xFF padding followed by the 0x6A sync byte)
* an optional SHA-256 comparison
* the radio must answer a discovery, must not be in use, and must be a known board
  type. Protocol 1-only boards, Hermes-Lite and Saturn/ANAN-G2 are refused.
* the image must fit into the board's application flash area
* the file name must match the board (e.g. `C10` and `Mk2PA` for the ANAN-G2E),
  as a guard against flashing an Angelia image into an Orion

While programming, every 256-byte block is acknowledged by the radio with its sequence
number and the running 16-bit sum of all bytes written so far. The tool compares both
after **every** block, not only at the end.

## What happens on errors

This follows from how the FPGA state machine works (`ASMI_interface.v`):

* The radio only writes the **application area** of the flash (e.g. 0x100000–0x1FFFFF
  on the HermesC10). The bootloader image in the lower area is never touched, so the
  jumper/bootloader method remains a way back.
* Until the update is complete, the radio keeps running the old firmware from RAM.
  **Do not switch it off** after the erase has started.
* **Error during erase:** nothing has been written yet. Run p2flash again.
* **Error during programming:** a lost packet is detected by a timeout. The tool
  resends the block (up to 2 times), and the reported checksum shows whether the block
  was missing (repaired, continue) or had already been written (block written twice,
  abort). If programming aborts half way, the FPGA does not accept a new erase until
  it is reset. After the next power cycle the radio will then most likely need the
  bootloader (jumper) method.
* Firmware older than 2019 does not report a checksum. With such firmware the tool
  cannot tell those cases apart, so it aborts instead of resending.

To minimise risk: flash from a machine in the **same LAN as the radio** (e.g. a VM
next to it) instead of across a lossy link. The protocol has no windowing, so every
block costs one round trip; that is fast on a LAN.

## Remote / VPN notes

* Unicast discovery, erase and program work over routed networks and VPNs. The packets
  are at most 265 bytes, so the MTU does not matter.
* Some firmware answers from UDP **source port 0** instead of 1024 (fixed for the
  HermesC10 in v11.0.10, see
  [TAPR/OpenHPSDR-Firmware#2](https://github.com/TAPR/OpenHPSDR-Firmware/issues/2)).
  Many firewalls drop such packets. OPNsense, for example, needs *Firewall → Settings →
  Advanced → Disable blocking of port 0*. On the local LAN this does not matter.
  p2flash deliberately uses an unconnected socket so that it accepts those replies.

## Supported boards

| Board type | Board | Application area |
|---|---|---|
| 1 | Hermes (ANAN-10/100), HermesC10 without Mk2PA | 1 MiB |
| 2 | Hermes (ANAN-10E/100B) | 1 MiB |
| 3 | Angelia (ANAN-100D, Brick3) | 2.25 MiB |
| 4 | Orion (ANAN-200D) | 4 MiB |
| 5 | Orion MkII (ANAN-7000DLE/8000DLE) | 4 MiB |
| 20 | HermesC10 Mk2PA (**ANAN-G2E**) | 1 MiB |

The limits were read from `ASMI_interface.v` of the current Protocol 2 sources in
[TAPR/OpenHPSDR-Firmware](https://github.com/TAPR/OpenHPSDR-Firmware).

## Firmware files

Official images are in [TAPR/OpenHPSDR-Firmware](https://github.com/TAPR/OpenHPSDR-Firmware)
under `Protocol 2/<board>/`. Use the files in the folders directly, because the
Releases page is outdated. For the ANAN-G2E, use the `…_Mk2PA.rbf`.

## How it works (protocol)

All packets go to UDP port 1024 of the radio:

| Packet | Bytes | Reply (byte 4) |
|---|---|---|
| Discovery | `seq[4] 0x02` + zeros (60 bytes) | 0x02 idle / 0x03 in use, MAC, board, versions |
| Erase | `seq[4]=0 0x04` + zeros (60 bytes) | 0x03 on receipt, 0x03 again when erased |
| Program | `seq[4] 0x05 nblocks[4] data[256]` (265 bytes) | 0x04, same seq, 16-bit sum at bytes 13–14 |

The last block is padded with 0xFF. After the last block the FPGA waits about 2 s and
reloads itself from flash (nCONFIG). Reference: *openHPSDR Ethernet Protocol v4.4*
(in the firmware repository under `Protocol 2/Documentation`), chapters "Erase Packet",
"Program Packet" and "Command Reply Packet".

## Testing

`p2sim.py` simulates the radio side (modelled on the HermesC10 FPGA code): erase
time, the final double acknowledge, the automatic reload, firmware without checksum,
and lost packets or replies:

```
python3 -m unittest discover -s tests -v

./p2sim.py --port 11024 --out flashed.bin &
./p2flash.py --port 11024 flash 127.0.0.1 FILE.rbf --yes
cmp flashed.bin FILE.rbf        # identical up to the 0xFF padding
```

## Kurzanleitung (Deutsch)

1. Alle SDR-Programme schließen, die das Radio nutzen.
2. `./p2flash.py gui` starten, die IP des Radios eintragen und **Query** drücken.
3. Die `.rbf`-Datei wählen und die veröffentlichte SHA-256 bei *Expected* eintragen.
4. **Flash firmware** drücken. Das Radio während des Vorgangs nicht ausschalten.
5. Am Ende muss „checksum: radio … = file …“ grün sein, und das Radio meldet sich
   mit der neuen Version zurück.

Falls es schiefgeht: siehe „What happens on errors“. Der Rückweg ist der Bootloader
mit Jumper.

## License

GPL-3.0-or-later. © 2026 Birger Kuepper, DG6EU

Protocol by the openHPSDR project (Phil Harman VK6PH et al.); firmware maintained by
Rick Koch N1GP. Thanks to all of them for their work.
