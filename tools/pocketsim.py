#!/usr/bin/env python3
# SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
"""
pocketsim - a small PocketStation and Dreamcast VMU simulator for quick testing (pure Python).

  python pocketsim.py game.bin                      open a window
  python pocketsim.py game.mcd                      run the first PocketStation app on a card
  python pocketsim.py game.vms                      run a Dreamcast VMU game (see vmusim.py)
  python pocketsim.py game.bin --headless --frames 300 --input "60:right,90:fire" \
        --shot 120:a.png --screenshot end.png --wav sound.wav

Keys: arrows = d-pad, Z / X / Space / Enter = FIRE (action button), D = dock/undock, M = mute,
      any key wakes the unit from standby,
      R = restart, F12 = screenshot, Esc = quit.
VMU:  arrows = d-pad, Z / Space = A, X = B, Enter = MODE, Backspace = SLEEP.
      Headless button names: up down left right a b mode sleep (fire = a).

It runs your ARM code on an ARM7TDMI interpreter and replaces the Sony kernel with
Python implementations of the SWI calls (high-level emulation), so no BIOS dump is
needed. It is meant for fast iteration and catching bugs, not cycle accuracy:
always confirm on no$gba / PCSX-Redux and on real hardware.

Bug catchers built in:
  * 8-bit reads from FLASH/BIOS return garbage (like the hardware) and are reported
  * IRQ callbacks that clobber r2-r11 are reported (the kernel only saves r0,r1,r12)
  * reads from unmapped memory stop with a data-abort report
  * user RAM overflow into kernel RAM (writes below 0x200) are reported
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psformat as pf  # noqa: E402
import vmusim  # noqa: E402

M32 = 0xFFFFFFFF
FLASH = 0x02000000
IRQ_RETURN = 0x04000100          # magic address inside "BIOS": IRQ callback return
CPU_HZ = {1: 63488, 2: 126976, 3: 253952, 4: 507904, 5: 1015808,
          6: 1998848, 7: 3997696, 8: 7995392}
BTN = {'fire': 1, 'right': 2, 'left': 4, 'down': 8, 'up': 16}
COND_NAMES = None


class SimStop(Exception):
    pass


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


class PocketStation:
    width, height = 32, 32
    btn = BTN
    keymap = {'Left': 'left', 'Right': 'right', 'Up': 'up', 'Down': 'down',
              'z': 'fire', 'x': 'fire', 'space': 'fire', 'Return': 'fire'}
    help = 'arrows: d-pad  Z/Space: fire  D: dock  M: mute  R: restart  F12: shot'
    colors = ('#1c2418', '#a9b596', '#9aa58a')       # ink, paper, bezel
    cpu_hz = CPU_HZ[7]

    @property
    def seconds(self):
        return self.cycles / CPU_HZ[7]

    def advance_to(self, t):
        self.run_until(int(t * CPU_HZ[7]))

    def __init__(self, image, dir_index=1, save_path=None, quiet=False):
        if len(image) % 4:
            image = image + b'\0' * (4 - len(image) % 4)
        self.flash = bytearray(image)
        self.flash_w = memoryview(self.flash).cast('I')
        self.flash_h = memoryview(self.flash).cast('H')
        self.flash_len = len(self.flash)
        self.dir_index = dir_index
        self.save_path = save_path
        self.quiet = quiet
        hdr = pf.parse_title_sector(bytes(self.flash[:0x80]))
        if not hdr['is_pocketstation']:
            raise ValueError('file is not a PocketStation executable (no MCX0/MCX1 id)')
        self.entry = hdr['entry']
        self.title = hdr['title']
        self.warned = set()
        self.tty = ''
        self.reset()

    # ------------------------------------------------------------ setup
    def reset(self):
        self.ram = bytearray(0x800)
        self.ram_w = memoryview(self.ram).cast('I')
        self.ram_h = memoryview(self.ram).cast('H')
        self.r = [0] * 16
        self.N = self.Z = self.C = self.V = 0
        self.T = 0
        self.I = 0                    # IRQs enabled in user mode
        self.mode = 0x10              # user
        self.in_irq = False
        self.irq_saved = None
        self.cycles = 0
        self.clk = 7
        self.callbacks = [0, 0, 0, 0]
        self.int_raw = 0              # latched requests (unmasked)
        self.int_mask = 0
        self.buttons = 0
        self.docked = 0
        self.timers = [{'reload': 0xFFFF, 'mode': 0, 'next': None, 'start': 0} for _ in range(3)]
        self.lcd_mode = 0
        self.vram = [0] * 32
        self.dac_ctrl = 0
        self.dac_data = 0
        self.iop = 0x20               # speaker off, LED on? (bit1=0 -> LED on); start LED off
        self.iop |= 0x02
        self.audio = []               # (cycle, level)
        self.audio_pos = 0            # GUI streaming: cycle rendered so far
        self.audio_lvl = 0
        self.stores = 0
        self.loop_t = -1
        self.loop_stores = -1
        self.loop_regs = None
        self.loop_hits = 0
        self.irq_check = True
        self.exited = None
        self.next_event = 1 << 62
        self.prepared = (0, 0)
        self.clock_offset = 0
        # kernel RAM contents the user can see
        struct.pack_into('<II', self.ram, 0xD8, 0x00800000, 0)    # UI status: volume loud, RTC set
        struct.pack_into('<I', self.ram, 0x04, 0xEAFFFFFE)       # undefined vector: hang (like BIOS)
        self.r[13] = 0x800
        self.r[0] = 0
        self.r[15] = self.entry & ~1
        self.T = self.entry & 1
        self.load_saves()

    # ------------------------------------------------------------ persistence
    def load_saves(self):
        self.saves = {}
        if not self.save_path or not os.path.exists(self.save_path):
            return
        raw = open(self.save_path, 'rb').read()
        if raw[:8] != b'PSIMSAV1':
            return
        size, entry = struct.unpack_from('<II', raw, 8)
        if size != self.flash_len or entry != self.entry:
            return                    # different build layout: start fresh
        pos = 16
        while pos + 132 <= len(raw):
            sec = struct.unpack_from('<I', raw, pos)[0]
            data = raw[pos + 4:pos + 132]
            self.flash[sec * 128:sec * 128 + 128] = data
            self.saves[sec] = data
            pos += 132

    def store_saves(self):
        if not self.save_path:
            return
        out = bytearray(b'PSIMSAV1') + struct.pack('<II', self.flash_len, self.entry)
        for sec, data in sorted(self.saves.items()):
            out += struct.pack('<I', sec) + data
        open(self.save_path, 'wb').write(out)

    # ------------------------------------------------------------ reporting
    def warn(self, key, msg):
        if key in self.warned:
            return
        self.warned.add(key)
        print('pocketsim: ' + msg, file=sys.stderr)

    def cur_pc(self):
        return self.cur

    def fault(self, msg):
        raise SimStop('%s at PC=0x%08X (%s mode)' % (msg, self.cur, 'THUMB' if self.T else 'ARM'))

    # ------------------------------------------------------------ CPSR
    def get_cpsr(self):
        return (self.N << 31) | (self.Z << 30) | (self.C << 29) | (self.V << 28) | \
               (self.I << 7) | (1 << 6) | (self.T << 5) | self.mode

    def set_cpsr(self, v, flags_only=False):
        self.N = (v >> 31) & 1; self.Z = (v >> 30) & 1; self.C = (v >> 29) & 1; self.V = (v >> 28) & 1
        if not flags_only and self.mode != 0x10:
            self.I = (v >> 7) & 1
            self.T = (v >> 5) & 1

    # ------------------------------------------------------------ memory
    def r32(self, a):
        reg = a >> 24
        if reg == 0:
            return self.ram_w[(a & 0x7FF) >> 2]
        if reg == 2:
            o = a - FLASH
            if o < self.flash_len:
                return self.flash_w[o >> 2]
            self.fault('data abort: read 0x%08X beyond the file (virtual flash)' % a)
        if reg >= 6 and reg <= 0x0D:
            return self.io_read(a & ~3)
        if reg == 4:
            return 0
        if reg == 8:
            return 0xFFFFFFFF
        self.fault('data abort: read32 from unmapped 0x%08X' % a)

    def r16(self, a):
        reg = a >> 24
        if reg == 0:
            return self.ram_h[(a & 0x7FF) >> 1]
        if reg == 2:
            o = a - FLASH
            if o < self.flash_len:
                return self.flash_h[o >> 1]
            self.fault('data abort: read 0x%08X beyond the file (virtual flash)' % a)
        if reg >= 6 and reg <= 0x0D:
            v = self.io_read(a & ~3)
            return (v >> 16) & 0xFFFF if a & 2 else v & 0xFFFF
        if reg == 4 or reg == 8:
            return 0
        self.fault('data abort: read16 from unmapped 0x%08X' % a)

    def r8(self, a):
        reg = a >> 24
        if reg == 0:
            return self.ram[a & 0x7FF]
        if reg in (2, 4, 8):
            self.warn(('ldrb', self.cur),
                      'WARNING 8-bit read from FLASH/ROM at 0x%08X (PC=0x%08X) returns garbage on real '
                      'hardware. Use 16/32-bit data or ps_flash_u8().' % (a, self.cur))
            return ((a * 0x9D) ^ 0x5A) & 0xFF
        if reg >= 6 and reg <= 0x0D:
            return (self.io_read(a & ~3) >> (8 * (a & 3))) & 0xFF
        self.fault('data abort: read8 from unmapped 0x%08X' % a)

    def check_ram_write(self, a):
        a &= 0x7FF
        if a < 0x200 and not self.in_irq and a not in (4, 5, 6, 7):
            self.warn(('kram', self.cur), 'WARNING write to kernel RAM 0x%03X from PC=0x%08X '
                      '(stack overflow or stray pointer?)' % (a, self.cur))

    def w32(self, a, v):
        self.stores += 1
        reg = a >> 24
        if reg == 0:
            if (a & 0x7FF) < 0x200:
                self.check_ram_write(a)
            self.ram_w[(a & 0x7FF) >> 2] = v & M32
            return
        if reg >= 6 and reg <= 0x0D:
            self.io_write(a & ~3, v & M32)
            return
        if reg in (2, 8):
            self.fault('write to flash 0x%08X (use bios_flash_write_virtual)' % a)
        self.fault('data abort: write32 to unmapped 0x%08X' % a)

    def w16(self, a, v):
        self.stores += 1
        if a >> 24 == 0:
            if (a & 0x7FF) < 0x200:
                self.check_ram_write(a)
            self.ram_h[(a & 0x7FF) >> 1] = v & 0xFFFF
            return
        if (a >> 24) >= 6 and (a >> 24) <= 0x0D:
            self.io_write(a & ~3, v & 0xFFFF)
            return
        self.fault('write16 to 0x%08X' % a)

    def w8(self, a, v):
        self.stores += 1
        if a >> 24 == 0:
            if (a & 0x7FF) < 0x200:
                self.check_ram_write(a)
            self.ram[a & 0x7FF] = v & 0xFF
            return
        if (a >> 24) >= 6 and (a >> 24) <= 0x0D:
            self.io_write(a & ~3, v & 0xFF)
            return
        self.fault('write8 to 0x%08X' % a)

    # ------------------------------------------------------------ I/O
    def timer_hz(self, n):
        div = (2, 32, 512, 2)[self.timers[n]['mode'] & 3]
        return CPU_HZ.get(self.clk, CPU_HZ[8]) / div

    def timer_period(self, n):
        # in CPU cycles; the timer clock and CPU clock scale together
        div = (2, 32, 512, 2)[self.timers[n]['mode'] & 3]
        return (self.timers[n]['reload'] + 1) * div

    def timer_restart(self, n):
        t = self.timers[n]
        if t['mode'] & 4:
            t['start'] = self.cycles
            t['next'] = self.cycles + self.timer_period(n)
        else:
            t['next'] = None
        self.recalc_events()

    def recalc_events(self):
        nxt = 1 << 62
        for t in self.timers:
            if t['next'] is not None and t['next'] < nxt:
                nxt = t['next']
        self.next_event = nxt

    def run_events(self):
        for n, t in enumerate(self.timers):
            if t['next'] is not None and self.cycles >= t['next']:
                period = self.timer_period(n)
                missed = (self.cycles - t['next']) // period + 1
                t['next'] += missed * period
                self.int_raw |= (1 << (7, 8, 13)[n])
                self.irq_check = True
        self.recalc_events()

    def int_input(self):
        return self.buttons | (self.docked << 11) | ((self.cycles // (CPU_HZ.get(self.clk, CPU_HZ[7]) // 2)) & 1) << 9

    def io_read(self, a):
        if a == 0x0A000000:                      # INT_LATCH (masked)
            return (self.int_raw | self.buttons) & self.int_mask
        if a == 0x0A000004:
            return self.int_input()
        if a == 0x0A000008 or (0x0A000014 <= a < 0x0A800000):
            return self.int_mask
        if 0x0A800000 <= a < 0x0A800030:
            n = (a - 0x0A800000) >> 4
            k = a & 0xF
            t = self.timers[n]
            if k == 0:
                return t['reload']
            if k == 4:
                if t['next'] is None:
                    return t['reload']
                div = (2, 32, 512, 2)[t['mode'] & 3]
                return max(0, (t['next'] - self.cycles) // div) & 0xFFFF
            if k == 8:
                return t['mode']
            return t['reload']
        if a == 0x0B000000:
            return self.clk | 0x10
        if a == 0x0B800008:
            return self.bcd_time()
        if a == 0x0B80000C:
            return self.bcd_date() & 0xFFFFFF
        if a == 0x0D000000:
            return self.lcd_mode
        if 0x0D000100 <= a < 0x0D000180:
            return self.vram[(a - 0x0D000100) >> 2]
        if a == 0x0D80000C:
            return 0x10 | (self.iop & 0x0F) | (self.docked << 4)
        if a == 0x0D800010:
            return self.dac_ctrl
        if a == 0x06000300:
            return 0x426C6BE7
        return 0

    def io_write(self, a, v):
        if a == 0x0A000008:
            self.int_mask |= v & 0x3FFF; self.irq_check = True
        elif a == 0x0A00000C:
            self.int_mask &= ~v; self.irq_check = True
        elif a == 0x0A000010:
            self.int_raw &= ~v; self.irq_check = True
        elif 0x0A800000 <= a < 0x0A800030:
            n = (a - 0x0A800000) >> 4
            k = a & 0xF
            t = self.timers[n]
            if k == 0:
                t['reload'] = v & 0xFFFF
                if t['mode'] & 4:
                    self.timer_restart(n)
            elif k == 8:
                was = t['mode'] & 4
                t['mode'] = v & 7
                if (v & 4) and not was or not (v & 4):
                    self.timer_restart(n)
        elif a == 0x0B000000:
            self.clk = v & 0xF or 7
        elif a == 0x0B000004:
            if v & 1:
                self.standby()
        elif a == 0x0D000000:
            self.lcd_mode = v
        elif 0x0D000100 <= a < 0x0D000180:
            self.vram[(a - 0x0D000100) >> 2] = v & M32
        elif a == 0x0D800004:
            self.iop |= v
        elif a == 0x0D800008:
            self.iop &= ~v
        elif a == 0x0D800010:
            self.dac_ctrl = v & 1
            self.audio.append((self.cycles, self.dac_level()))
        elif a == 0x0D800014:
            self.dac_data = v
            self.audio.append((self.cycles, self.dac_level()))

    def dac_level(self):
        if not (self.dac_ctrl & 1) or (self.iop & 0x20):
            return 0
        v = self.dac_data & 0xFFFF
        if v & 0x8000:
            v -= 0x10000
        return v

    def take_audio(self, rate):
        """Render speaker output since the last call as 16-bit samples (consumes events)."""
        step = CPU_HZ.get(self.clk, CPU_HZ[7]) / float(rate)
        n = int((self.cycles - self.audio_pos) / step)
        ev, k, lvl, cyc = self.audio, 0, self.audio_lvl, self.audio_pos
        out = [0] * n
        for i in range(n):
            cyc += step
            while k < len(ev) and ev[k][0] <= cyc:
                lvl = ev[k][1]
                k += 1
            out[i] = lvl
        del ev[:k]
        self.audio_pos, self.audio_lvl = cyc, lvl
        return out

    def tone_hz(self):
        """Frequency of the square wave the program is currently playing (0 = silent)."""
        t = self.timers[1]
        if not (self.dac_ctrl & 1) or (self.iop & 0x20) or not (t['mode'] & 4) or not (self.int_mask & (1 << 8)):
            return 0
        return CPU_HZ.get(self.clk, CPU_HZ[7]) / (2.0 * self.timer_period(1))

    def standby(self):
        """CLK_STOP: sleep until an enabled button / dock interrupt."""
        self.sleeping = True

    # ------------------------------------------------------------ RTC
    def now(self):
        return time.localtime(time.time() + self.clock_offset)

    def bcd_time(self):
        t = self.now()
        dow = (t.tm_wday + 1) % 7 + 1          # 1 = Sunday
        return (dow << 24) | (bcd(t.tm_hour) << 16) | (bcd(t.tm_min) << 8) | bcd(t.tm_sec)

    def bcd_date(self):
        t = self.now()
        return (bcd(t.tm_year // 100) << 24) | (bcd(t.tm_year % 100) << 16) | (bcd(t.tm_mon) << 8) | bcd(t.tm_mday)

    # ------------------------------------------------------------ kernel (HLE)
    def swi(self, n):
        r = self.r
        n &= 0xFF
        a0, a1, a2 = r[0], r[1], r[2]
        res = 0
        if n == 0x01:
            if a0 > 3:
                self.warn('swi1', 'SWI 01h with bad slot %d' % a0)
            else:
                res = self.callbacks[a0]; self.callbacks[a0] = a1
        elif n == 0x02:
            self.fault('SWI 02h (CustomSwi2) is not supported by pocketsim')
        elif n in (0x03, 0x10):
            res = self.flash_write(a0, a1, physical=(n == 0x10))
        elif n == 0x04:
            res = self.clk; self.clk = (a0 & 0xF) or 7
        elif n == 0x05:
            res = 0
        elif n == 0x06:
            res = 0xC0
        elif n == 0x07:
            res = a0 & 0x70000
        elif n == 0x08:
            res = self.prepared[0]
            if a0 == 1:
                self.prepared = (a1, a2); res = a1
        elif n == 0x09:
            flags = struct.unpack_from('<I', self.ram, 0xC0)[0]
            if flags & (1 << 9):
                res = a0                         # refused while docked with comms on
            else:
                d, p = self.prepared
                raise_exit = ('returned to the PocketStation menu' if d == 0 else 'started file %d' % d)
                if d == 0 and (p & 0xF0) not in (0x10, 0x20, 0x30):
                    self.warn('rtc', 'NOTE DoExecute(GUI) with param 0x%X erases the RTC on real hardware' % p)
                self.exited = '%s (param 0x%X)' % (raise_exit, p)
                raise SimStop(self.exited)
        elif n == 0x0A:
            res = 0x426C6BE7
        elif n == 0x0B:
            res = 0
        elif n == 0x0C:
            res = 0
        elif n == 0x0D:
            res = self.bcd_date()
        elif n == 0x0E:
            res = self.bcd_time()
        elif n == 0x11:
            flags = struct.unpack_from('<I', self.ram, 0xC0)[0]
            if a0 and self.docked:
                flags |= 1 << 9
            else:
                flags &= ~(1 << 9)
            struct.pack_into('<I', self.ram, 0xC0, flags)
            res = 0
        elif n == 0x12:
            res = 0
        elif n == 0x13:
            res = 0xD8
        elif n == 0x14:
            res = 0xE0
        elif n == 0x15:
            res = a1 if a0 == 1 else 0
        elif n == 0x16:
            res = self.dir_index
        elif n == 0x17:
            res = 0xC8
        elif n == 0x18:
            res = 0
        elif n == 0x00:
            self.fault('SWI 00h Reset() called - erases the RTC on real hardware')
        else:
            self.warn(('swi', n), 'unknown SWI 0x%02X at PC=0x%08X' % (n, self.cur))
        r[0] = res & M32

    def flash_write(self, sector, src, physical):
        if physical:
            self.warn('phys', 'SWI 10h FlashWritePhysical used: simulated as a virtual write')
        off = sector * 128
        if off + 128 > self.flash_len:
            return 1
        if (src >> 24) != 0:
            self.warn('fsrc', 'flash write source 0x%08X is not in RAM' % src)
        if (src >> 24) == 0:
            data = bytes(self.ram[(src + i) & 0x7FF] for i in range(128))
        else:
            data = b''.join(struct.pack('<H', self.r16((src & ~1) + i)) for i in range(0, 128, 2))
        self.flash[off:off + 128] = data
        self.saves[sector] = data
        self.store_saves()
        self.cycles += 4000 * 8
        return 0

    # ------------------------------------------------------------ interrupts
    def maybe_irq(self):
        self.irq_check = False
        if self.in_irq or self.I:
            return
        pending = (self.int_raw | self.buttons) & self.int_mask & ~((1 << 6) | (1 << 13) | (1 << 11) | (1 << 9))
        if not pending:
            return
        cb = self.callbacks[1]
        if not cb:
            # kernel default: nothing handles it -> it would fire forever; drop it
            self.int_raw &= ~pending
            self.warn('nocb', 'IRQ 0x%X pending but no IRQ callback installed' % pending)
            return
        self.irq_saved = (list(self.r), self.get_cpsr(), self.T, self.cur_next_pc())
        self.in_irq = True
        self.r[13] = 0x170
        self.r[14] = IRQ_RETURN
        self.T = cb & 1
        self.r[15] = cb & ~1
        self.cycles += 40

    def cur_next_pc(self):
        return self.r[15]

    def irq_return(self):
        regs, cpsr, t, pc = self.irq_saved
        for i in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
            if self.r[i] != regs[i]:
                self.warn('clobber', 'BUG IRQ callback changed r%d (the kernel only preserves r0, r1, r12): '
                          'wrap C handlers with an assembly stub that saves r2/r3/lr' % i)
                break
        keep = list(self.r)
        self.r = list(regs)
        for i in range(2, 12):                   # like the real kernel: only r0,r1,r12 restored
            self.r[i] = keep[i]
        self.set_cpsr(cpsr)
        self.T = t
        self.r[15] = pc
        self.in_irq = False
        self.irq_check = True
        self.cycles += 30

    # ------------------------------------------------------------ helpers
    def shift(self, op, val, amt, carry, by_reg):
        if op == 0:      # LSL
            if amt == 0:
                return val, carry
            if amt < 32:
                return (val << amt) & M32, (val >> (32 - amt)) & 1
            if amt == 32:
                return 0, val & 1
            return 0, 0
        if op == 1:      # LSR
            if amt == 0:
                if by_reg:
                    return val, carry
                amt = 32
            if amt < 32:
                return val >> amt, (val >> (amt - 1)) & 1
            if amt == 32:
                return 0, val >> 31
            return 0, 0
        if op == 2:      # ASR
            if amt == 0:
                if by_reg:
                    return val, carry
                amt = 32
            if amt >= 32:
                return (M32 if val >> 31 else 0), val >> 31
            sv = val - (1 << 32) if val >> 31 else val
            return (sv >> amt) & M32, (val >> (amt - 1)) & 1
        # ROR / RRX
        if amt == 0:
            if by_reg:
                return val, carry
            return (carry << 31) | (val >> 1), val & 1
        amt &= 31
        if amt == 0:
            return val, val >> 31
        res = ((val >> amt) | (val << (32 - amt))) & M32
        return res, res >> 31

    def cond(self, c):
        if c == 14: return True
        if c == 0: return self.Z == 1
        if c == 1: return self.Z == 0
        if c == 2: return self.C == 1
        if c == 3: return self.C == 0
        if c == 4: return self.N == 1
        if c == 5: return self.N == 0
        if c == 6: return self.V == 1
        if c == 7: return self.V == 0
        if c == 8: return self.C == 1 and self.Z == 0
        if c == 9: return self.C == 0 or self.Z == 1
        if c == 10: return self.N == self.V
        if c == 11: return self.N != self.V
        if c == 12: return self.Z == 0 and self.N == self.V
        if c == 13: return self.Z == 1 or self.N != self.V
        return False

    def add(self, a, b, c, setf):
        res = a + b + c
        r = res & M32
        if setf:
            self.N = r >> 31; self.Z = 1 if r == 0 else 0; self.C = 1 if res > M32 else 0
            self.V = ((~(a ^ b) & (a ^ r)) >> 31) & 1
        return r

    def sub(self, a, b, c, setf):     # a - b - (1-c)
        return self.add(a, (~b) & M32, c, setf)

    def logic_flags(self, r, carry):
        self.N = r >> 31; self.Z = 1 if r == 0 else 0; self.C = carry

    def alu(self, op, a, b, setf, carry):
        """Data processing; returns (result, writes_result)."""
        if op == 0:   r = a & b;  w = 1
        elif op == 1: r = a ^ b;  w = 1
        elif op == 2: return self.sub(a, b, 1, setf), 1
        elif op == 3: return self.sub(b, a, 1, setf), 1
        elif op == 4: return self.add(a, b, 0, setf), 1
        elif op == 5: return self.add(a, b, self.C, setf), 1
        elif op == 6: return self.sub(a, b, self.C, setf), 1
        elif op == 7: return self.sub(b, a, self.C, setf), 1
        elif op == 8: r = a & b;  w = 0
        elif op == 9: r = a ^ b;  w = 0
        elif op == 10: self.sub(a, b, 1, True); return 0, 0
        elif op == 11: self.add(a, b, 0, True); return 0, 0
        elif op == 12: r = a | b; w = 1
        elif op == 13: r = b;     w = 1
        elif op == 14: r = a & ~b & M32; w = 1
        else: r = (~b) & M32; w = 1
        if setf:
            self.logic_flags(r, carry)
        return r, w

    def ldr_rot(self, addr):
        v = self.r32(addr & ~3)
        rot = (addr & 3) * 8
        if rot:
            v = ((v >> rot) | (v << (32 - rot))) & M32
        return v

    # ------------------------------------------------------------ ARM
    def exec_arm(self, op):
        r = self.r
        if (op >> 28) != 14 and not self.cond(op >> 28):
            return
        top = (op >> 25) & 7
        if top == 0 or top == 1:
            if top == 0:
                if (op & 0x0FFFFFF0) == 0x012FFF10:              # BX
                    t = r[op & 15]
                    if (op & 15) == 15:
                        t = (self.cur + 8) & M32
                    self.T = t & 1
                    r[15] = t & ~1
                    self.cycles += 2
                    return
                if (op & 0x0FC000F0) == 0x00000090:              # MUL/MLA
                    rd = (op >> 16) & 15; rn = (op >> 12) & 15; rs = (op >> 8) & 15; rm = op & 15
                    res = (r[rm] * r[rs]) & M32
                    if op & (1 << 21):
                        res = (res + r[rn]) & M32
                    r[rd] = res
                    if op & (1 << 20):
                        self.N = res >> 31; self.Z = 1 if res == 0 else 0
                    self.cycles += 3
                    return
                if (op & 0x0F8000F0) == 0x00800090:              # long multiply
                    hi = (op >> 16) & 15; lo = (op >> 12) & 15; rs = (op >> 8) & 15; rm = op & 15
                    a, b = r[rm], r[rs]
                    if op & (1 << 22):
                        a = a - (1 << 32) if a >> 31 else a
                        b = b - (1 << 32) if b >> 31 else b
                    res = a * b
                    if op & (1 << 21):
                        res += (r[hi] << 32) | r[lo]
                    res &= (1 << 64) - 1
                    r[lo] = res & M32; r[hi] = res >> 32
                    if op & (1 << 20):
                        self.N = (res >> 63) & 1; self.Z = 1 if res == 0 else 0
                    self.cycles += 4
                    return
                if (op & 0x0FB00FF0) == 0x01000090:              # SWP
                    rn = (op >> 16) & 15; rd = (op >> 12) & 15; rm = op & 15
                    a = r[rn]
                    if op & (1 << 22):
                        t = self.r8(a); self.w8(a, r[rm]); r[rd] = t
                    else:
                        t = self.ldr_rot(a); self.w32(a & ~3, r[rm]); r[rd] = t
                    return
                if (op & 0x0E000090) == 0x00000090 and (op & 0x60):  # halfword / signed
                    self.arm_halfword(op)
                    return
                if (op & 0x0FBF0FFF) == 0x010F0000:              # MRS
                    r[(op >> 12) & 15] = self.get_cpsr()
                    return
                if (op & 0x0FB0FFF0) == 0x0120F000:              # MSR reg
                    self.arm_msr(op)
                    return
                rm = op & 15
                b = r[rm]
                if rm == 15:
                    b = (self.cur + (12 if op & 0x10 else 8)) & M32
                st = (op >> 5) & 3
                if op & 0x10:
                    amt = r[(op >> 8) & 15] & 0xFF
                    b, carry = self.shift(st, b, amt, self.C, True)
                    self.cycles += 1
                else:
                    b, carry = self.shift(st, b, (op >> 7) & 31, self.C, False)
            else:
                if (op & 0x0DB0F000) == 0x0120F000:
                    self.arm_msr(op)
                    return
                imm = op & 0xFF
                rot = ((op >> 8) & 15) * 2
                b = ((imm >> rot) | (imm << (32 - rot))) & M32 if rot else imm
                carry = b >> 31 if rot else self.C
            opc = (op >> 21) & 15
            rn = (op >> 16) & 15
            a = r[rn]
            if rn == 15:
                a = (self.cur + (12 if (top == 0 and op & 0x10) else 8)) & M32
            setf = (op >> 20) & 1
            rd = (op >> 12) & 15
            if rd == 15 and setf:
                # MOVS PC etc. (exception return) - only valid in privileged modes
                res, w = self.alu(opc, a, b, False, carry)
                if w:
                    r[15] = res & ~1 if not self.T else res
                    self.cycles += 2
                return
            res, w = self.alu(opc, a, b, setf, carry)
            if w:
                r[rd] = res
                if rd == 15:
                    r[15] = res & ~3
                    self.cycles += 2
            return
        if top == 2 or top == 3:
            if top == 3 and op & 0x10:
                self.undefined(op)
                return
            rn = (op >> 16) & 15; rd = (op >> 12) & 15
            if top == 2:
                off = op & 0xFFF
            else:
                off, _ = self.shift((op >> 5) & 3, r[op & 15], (op >> 7) & 31, self.C, False)
            base = r[rn]
            if rn == 15:
                base = (self.cur + 8) & M32
            up = op & (1 << 23)
            pre = op & (1 << 24)
            addr = (base + off if up else base - off) & M32
            ea = addr if pre else base
            load = op & (1 << 20)
            byte = op & (1 << 22)
            if load:
                v = self.r8(ea) if byte else self.ldr_rot(ea)
                self.cycles += 2
            else:
                sv = r[rd]
                if rd == 15:
                    sv = (self.cur + 12) & M32
                if byte:
                    self.w8(ea, sv)
                else:
                    self.w32(ea & ~3, sv)
                self.cycles += 1
            if (not pre) or (op & (1 << 21)):
                if not (load and rn == rd):
                    r[rn] = addr
            if load:
                r[rd] = v
                if rd == 15:
                    self.T = v & 1
                    r[15] = v & ~1
            return
        if top == 4:                                            # LDM/STM
            rn = (op >> 16) & 15
            rlist = op & 0xFFFF
            n = bin(rlist).count('1')
            base = r[rn]
            up = op & (1 << 23); pre = op & (1 << 24)
            load = op & (1 << 20); wb = op & (1 << 21)
            if up:
                start = base + 4 if pre else base
                newb = base + 4 * n
            else:
                start = base - 4 * n + (0 if pre else 4)
                newb = base - 4 * n
            addr = start & M32
            regs = [i for i in range(16) if rlist >> i & 1]
            if load:
                if wb:
                    r[rn] = newb & M32
                for i in regs:
                    v = self.r32(addr)
                    if i == 15:
                        if op & (1 << 22) and self.mode != 0x10:
                            pass
                        self.T = v & 1
                        r[15] = v & ~1
                    else:
                        r[i] = v
                    addr = (addr + 4) & M32
            else:
                for k, i in enumerate(regs):
                    v = r[i]
                    if i == 15:
                        v = (self.cur + 12) & M32
                    self.w32(addr, v)
                    addr = (addr + 4) & M32
                if wb:
                    r[rn] = newb & M32
            self.cycles += n + 1
            return
        if top == 5:                                            # B / BL
            off = op & 0xFFFFFF
            if off & 0x800000:
                off -= 0x1000000
            target = (self.cur + 8 + off * 4) & M32
            if op & (1 << 24):
                r[14] = (self.cur + 4) & M32
            self.branch(target)
            return
        if top == 7 and op & (1 << 24):
            self.swi(op & 0xFFFFFF)
            self.cycles += 20
            return
        self.undefined(op)

    def arm_msr(self, op):
        if op & (1 << 25):
            imm = op & 0xFF; rot = ((op >> 8) & 15) * 2
            v = ((imm >> rot) | (imm << (32 - rot))) & M32 if rot else imm
        else:
            v = self.r[op & 15]
        if op & (1 << 22):
            return                         # SPSR: ignore
        if op & (1 << 19):
            self.set_cpsr(v, flags_only=True)
        if (op & (1 << 16)) and self.mode != 0x10:
            self.I = (v >> 7) & 1

    def arm_halfword(self, op):
        r = self.r
        rn = (op >> 16) & 15; rd = (op >> 12) & 15
        if op & (1 << 22):
            off = ((op >> 4) & 0xF0) | (op & 0xF)
        else:
            off = r[op & 15]
        base = r[rn]
        if rn == 15:
            base = (self.cur + 8) & M32
        up = op & (1 << 23); pre = op & (1 << 24)
        addr = (base + off if up else base - off) & M32
        ea = addr if pre else base
        sh = (op >> 5) & 3
        if op & (1 << 20):
            if sh == 1:
                v = self.r16(ea & ~1)
            elif sh == 2:
                v = self.r8(ea)
                if v & 0x80: v |= 0xFFFFFF00
            else:
                v = self.r16(ea & ~1)
                if v & 0x8000: v |= 0xFFFF0000
            self.cycles += 2
        else:
            self.w16(ea & ~1, r[rd])
            self.cycles += 1
        if (not pre) or (op & (1 << 21)):
            if not ((op & (1 << 20)) and rn == rd):
                r[rn] = addr
        if op & (1 << 20):
            r[rd] = v & M32

    def undefined(self, op):
        if op == 0xE6000010 or (self.T == 0 and op & 0x0FFFFFFF == 0x06000010):
            c = self.r[0] & 0xFF
            if c == 10:
                print('[tty] ' + self.tty)
                sys.stdout.flush()
                self.tty = ''
            else:
                self.tty += chr(c)
            return
        vec = struct.unpack_from('<I', self.ram, 4)[0]
        if vec == 0xE1B0F00E:
            return
        self.fault('undefined instruction 0x%08X' % op)

    def branch(self, target):
        """Taken branch with idle-loop detection (spin loops are fast-forwarded)."""
        if target <= self.cur and self.cur - target <= 40:
            # A real spin loop (waiting for an IRQ) changes nothing: same registers,
            # no stores. Loops that count or compute must run normally.
            regs = tuple(self.r[:15])
            if target == self.loop_t and self.stores == self.loop_stores and regs == self.loop_regs:
                self.loop_hits += 1
                if self.loop_hits >= 3 and self.next_event < self.cycle_limit:
                    self.cycles = max(self.cycles, self.next_event)
                elif self.loop_hits >= 3:
                    self.cycles = max(self.cycles, self.cycle_limit)
            else:
                self.loop_t = target
                self.loop_stores = self.stores
                self.loop_regs = regs
                self.loop_hits = 0
        self.r[15] = target
        self.cycles += 2

    # ------------------------------------------------------------ THUMB
    def exec_thumb(self, op):
        r = self.r
        top = op >> 11
        if top < 3:                                             # shift imm
            rs = (op >> 3) & 7; rd = op & 7; amt = (op >> 6) & 31
            res, c = self.shift(top, r[rs], amt, self.C, False)
            r[rd] = res; self.logic_flags(res, c)
            return
        if top == 3:                                            # add/sub
            rs = (op >> 3) & 7; rd = op & 7
            v = (op >> 6) & 7
            b = v if op & 0x400 else r[v]
            r[rd] = self.sub(r[rs], b, 1, True) if op & 0x200 else self.add(r[rs], b, 0, True)
            return
        if top < 8:                                             # mov/cmp/add/sub imm8
            rd = (op >> 8) & 7; imm = op & 0xFF; o = (op >> 11) & 3
            if o == 0:
                r[rd] = imm; self.N = 0; self.Z = 1 if imm == 0 else 0
            elif o == 1:
                self.sub(r[rd], imm, 1, True)
            elif o == 2:
                r[rd] = self.add(r[rd], imm, 0, True)
            else:
                r[rd] = self.sub(r[rd], imm, 1, True)
            return
        if top == 8:
            if op & 0x400 == 0:                                 # ALU
                o = (op >> 6) & 15; rs = (op >> 3) & 7; rd = op & 7
                a = r[rd]; b = r[rs]
                if o == 0: res = a & b; self.logic_flags(res, self.C); r[rd] = res
                elif o == 1: res = a ^ b; self.logic_flags(res, self.C); r[rd] = res
                elif o == 2: res, c = self.shift(0, a, b & 0xFF, self.C, True); r[rd] = res; self.logic_flags(res, c)
                elif o == 3: res, c = self.shift(1, a, b & 0xFF, self.C, True); r[rd] = res; self.logic_flags(res, c)
                elif o == 4: res, c = self.shift(2, a, b & 0xFF, self.C, True); r[rd] = res; self.logic_flags(res, c)
                elif o == 5: r[rd] = self.add(a, b, self.C, True)
                elif o == 6: r[rd] = self.sub(a, b, self.C, True)
                elif o == 7: res, c = self.shift(3, a, b & 0xFF, self.C, True); r[rd] = res; self.logic_flags(res, c)
                elif o == 8: res = a & b; self.logic_flags(res, self.C)
                elif o == 9: r[rd] = self.sub(0, b, 1, True)
                elif o == 10: self.sub(a, b, 1, True)
                elif o == 11: self.add(a, b, 0, True)
                elif o == 12: res = a | b; self.logic_flags(res, self.C); r[rd] = res
                elif o == 13:
                    res = (a * b) & M32; r[rd] = res; self.N = res >> 31; self.Z = 1 if res == 0 else 0
                    self.cycles += 3
                elif o == 14: res = a & ~b & M32; self.logic_flags(res, self.C); r[rd] = res
                else: res = ~b & M32; self.logic_flags(res, self.C); r[rd] = res
                return
            # hi register ops / BX
            o = (op >> 8) & 3
            rs = ((op >> 3) & 7) | ((op >> 3) & 8)
            rd = (op & 7) | ((op >> 4) & 8)
            vs = (self.cur + 4) & M32 if rs == 15 else r[rs]
            vd = (self.cur + 4) & M32 if rd == 15 else r[rd]
            if o == 0:
                res = (vd + vs) & M32
                if rd == 15: self.branch(res & ~1)
                else: r[rd] = res
            elif o == 1:
                self.sub(vd, vs, 1, True)
            elif o == 2:
                if rd == 15: self.branch(vs & ~1)
                else: r[rd] = vs
            else:
                if rs == 15:
                    vs = (self.cur + 4) & ~2 & M32
                self.T = vs & 1
                r[15] = vs & ~1
                self.cycles += 2
            return
        if top == 9:                                            # ldr pc-relative
            rd = (op >> 8) & 7
            r[rd] = self.r32(((self.cur + 4) & ~3) + (op & 0xFF) * 4)
            self.cycles += 2
            return
        if top < 12:                                            # reg offset
            ro = (op >> 6) & 7; rb = (op >> 3) & 7; rd = op & 7
            a = (r[rb] + r[ro]) & M32
            o = (op >> 9) & 7
            if o == 0: self.w32(a & ~3, r[rd])
            elif o == 1: self.w16(a & ~1, r[rd])
            elif o == 2: self.w8(a, r[rd])
            elif o == 3:
                v = self.r8(a); r[rd] = v | 0xFFFFFF00 if v & 0x80 else v
            elif o == 4: r[rd] = self.ldr_rot(a)
            elif o == 5: r[rd] = self.r16(a & ~1)
            elif o == 6: r[rd] = self.r8(a)
            else:
                v = self.r16(a & ~1); r[rd] = v | 0xFFFF0000 if v & 0x8000 else v
            self.cycles += 1
            return
        if top < 16:                                            # imm offset word/byte
            rb = (op >> 3) & 7; rd = op & 7; imm = (op >> 6) & 31
            byte = top & 2; load = top & 1
            if byte:
                a = (r[rb] + imm) & M32
                if load: r[rd] = self.r8(a)
                else: self.w8(a, r[rd])
            else:
                a = (r[rb] + imm * 4) & M32
                if load: r[rd] = self.ldr_rot(a)
                else: self.w32(a & ~3, r[rd])
            self.cycles += 1
            return
        if top < 18:                                            # halfword imm
            rb = (op >> 3) & 7; rd = op & 7
            a = (r[rb] + ((op >> 6) & 31) * 2) & M32
            if top & 1: r[rd] = self.r16(a & ~1)
            else: self.w16(a & ~1, r[rd])
            self.cycles += 1
            return
        if top < 20:                                            # sp-relative
            rd = (op >> 8) & 7
            a = (r[13] + (op & 0xFF) * 4) & M32
            if top & 1: r[rd] = self.ldr_rot(a)
            else: self.w32(a & ~3, r[rd])
            self.cycles += 1
            return
        if top < 22:                                            # load address
            rd = (op >> 8) & 7
            base = r[13] if top & 1 else ((self.cur + 4) & ~3)
            r[rd] = (base + (op & 0xFF) * 4) & M32
            return
        if top < 24:
            if (op >> 8) == 0xB0:                               # add sp
                v = (op & 0x7F) * 4
                r[13] = (r[13] - v if op & 0x80 else r[13] + v) & M32
                return
            if (op >> 9) & 3 == 2:                              # push/pop
                rl = op & 0xFF
                if op & 0x800:                                  # POP
                    a = r[13]
                    for i in range(8):
                        if rl >> i & 1:
                            r[i] = self.r32(a); a += 4
                    if op & 0x100:
                        v = self.r32(a); a += 4
                        r[13] = a & M32
                        self.T = v & 1             # ARMv4T POP {pc} does not change state;
                        if not (v & 1):            # GCC only uses it for Thumb returns
                            self.warn(('popcp', self.cur), 'POP {pc} to ARM address 0x%08X (ARMv4T stays in Thumb)' % v)
                            self.T = 1
                        r[15] = v & ~1
                        self.cycles += 2
                    r[13] = a & M32
                    self.cycles += bin(rl).count('1') + 1
                else:                                           # PUSH
                    n = bin(rl).count('1') + (1 if op & 0x100 else 0)
                    a = (r[13] - 4 * n) & M32
                    r[13] = a
                    for i in range(8):
                        if rl >> i & 1:
                            self.w32(a, r[i]); a += 4
                    if op & 0x100:
                        self.w32(a, r[14])
                    self.cycles += n + 1
                return
            self.undefined(op)
            return
        if top < 26:                                            # ldmia/stmia
            rb = (op >> 8) & 7; rl = op & 0xFF
            a = r[rb]
            if top & 1:
                for i in range(8):
                    if rl >> i & 1:
                        r[i] = self.r32(a); a += 4
                if not (rl >> rb & 1):
                    r[rb] = a & M32
            else:
                for i in range(8):
                    if rl >> i & 1:
                        self.w32(a, r[i]); a += 4
                r[rb] = a & M32
            self.cycles += bin(rl).count('1') + 1
            return
        if top < 28:                                            # cond branch / swi
            c = (op >> 8) & 15
            if c == 15:
                self.swi(op & 0xFF)
                self.cycles += 20
                return
            if c == 14:
                self.undefined(op)
                return
            if self.cond(c):
                off = op & 0xFF
                if off & 0x80: off -= 0x100
                self.branch((self.cur + 4 + off * 2) & M32)
            return
        if top == 28:                                           # B
            off = op & 0x7FF
            if off & 0x400: off -= 0x800
            self.branch((self.cur + 4 + off * 2) & M32)
            return
        if top == 30:                                           # BL high
            off = op & 0x7FF
            if off & 0x400: off -= 0x800
            r[14] = (self.cur + 4 + (off << 12)) & M32
            return
        if top == 31:                                           # BL low
            t = (r[14] + ((op & 0x7FF) << 1)) & M32
            r[14] = ((self.cur + 2) | 1) & M32
            r[15] = t
            self.cycles += 2
            return
        self.undefined(op)

    # ------------------------------------------------------------ main loop
    def run_until(self, cycle_limit):
        self.cycle_limit = cycle_limit
        if getattr(self, 'sleeping', False):
            if (self.buttons | (self.docked << 11)) & self.int_mask:
                self.sleeping = False
                self.irq_check = True
            else:
                self.cycles = cycle_limit
                self.run_events()
                return
        r = self.r
        flash_h = self.flash_h
        flash_w = self.flash_w
        flen = self.flash_len
        while self.cycles < cycle_limit:
            if self.cycles >= self.next_event:
                self.run_events()
            if self.irq_check:
                self.maybe_irq()
            r = self.r
            pc = r[15]
            self.cur = pc
            if pc == IRQ_RETURN and self.in_irq:
                self.irq_return()
                continue
            o = pc - FLASH
            if self.T:
                if 0 <= o < flen:
                    op = flash_h[o >> 1]
                else:
                    op = self.fetch16(pc)
                r[15] = pc + 2
                self.cycles += 1
                self.exec_thumb(op)
            else:
                if 0 <= o < flen:
                    op = flash_w[o >> 2]
                else:
                    op = self.fetch32(pc)
                r[15] = pc + 4
                self.cycles += 2
                self.exec_arm(op)
            if getattr(self, 'sleeping', False):
                self.cycles = cycle_limit
                break

    def fetch16(self, pc):
        if pc >> 24 == 0:
            return self.ram_h[(pc & 0x7FF) >> 1]
        self.fault('jump to non-executable address 0x%08X' % pc)

    def fetch32(self, pc):
        if pc >> 24 == 0:
            return self.ram_w[(pc & 0x7FF) >> 2]
        self.fault('jump to non-executable address 0x%08X' % pc)

    # ------------------------------------------------------------ output
    def screen(self):
        """32 rows of 32 booleans as the viewer sees the device (True = black)."""
        on = self.lcd_mode & 0x40
        rows = [[bool(on and (self.vram[y] >> x) & 1) for x in range(32)] for y in range(32)]
        rotate = ((self.lcd_mode >> 7) & 1) != self.docked
        if rotate:
            rows = [row[::-1] for row in rows[::-1]]
        return rows

    def screen_text(self):
        return '\n'.join(''.join('#' if p else '.' for p in row) for row in self.screen())

    def save_png(self, path, scale=8):
        on_col, off_col = (24, 30, 26), (176, 190, 160)
        rows = self.screen()
        px = []
        for row in rows:
            line = []
            for p in row:
                line += [on_col if p else off_col] * scale
            px += [line] * scale
        pf.write_png(path, 32 * scale, 32 * scale, px)

    def save_wav(self, path, rate=22050):
        hz = CPU_HZ.get(self.clk, CPU_HZ[7])
        total = int(self.cycles / hz * rate)
        samples = bytearray(total * 2)
        ev = self.audio
        lvl, k = 0, 0
        for i in range(total):
            cyc = i * hz / rate
            while k < len(ev) and ev[k][0] <= cyc:
                lvl = ev[k][1]; k += 1
            struct.pack_into('<h', samples, i * 2, max(-32768, min(32767, lvl)))
        hdr = b'RIFF' + struct.pack('<I', 36 + len(samples)) + b'WAVEfmt ' + struct.pack(
            '<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) + b'data' + struct.pack('<I', len(samples))
        open(path, 'wb').write(hdr + samples)


# ---------------------------------------------------------------- loading
def load_program(path):
    raw = open(path, 'rb').read()
    if path.lower().endswith('.vmi'):
        vms = os.path.splitext(path)[0] + '.vms'
        if not os.path.exists(vms):
            raise SystemExit('%s is only the info file; the game is the .vms next to it' % path)
        return load_program(vms)
    if path.lower().endswith('.vms') or vmusim.is_vmu_game(raw):
        return raw, 'vmu', None
    if raw[:2] == b'MC' and len(raw) == pf.CARD_SIZE:
        for idx, fn, size, blocks in pf.card_files(raw):
            data = pf.card_read_file(raw, blocks, size)
            try:
                if pf.parse_title_sector(data)['is_pocketstation']:
                    return data, idx, fn
            except ValueError:
                pass
        raise SystemExit('no PocketStation application on %s' % path)
    fn, data = pf.read_any(path)
    return data, 1, fn


def parse_input(spec, btn=BTN):
    """'60:right,90:fire+up,120:' -> sorted [(frame, mask)]."""
    out = []
    for part in filter(None, (spec or '').split(',')):
        f, _, keys = part.partition(':')
        mask = 0
        for k in filter(None, keys.split('+')):
            if k not in btn:
                raise SystemExit('unknown button %r (use %s)' % (k, ', '.join(sorted(btn))))
            mask |= btn[k]
        out.append((int(f), mask))
    return sorted(out)


def headless(sim, args):
    script = parse_input(args.input, sim.btn)
    shots = {}
    for s in args.shot or []:
        f, _, p = s.partition(':')
        shots[int(f)] = p
    t0 = time.time()
    status = 'ran %d frames' % args.frames
    failed = False
    try:
        for frame in range(args.frames):
            while script and script[0][0] <= frame:
                sim.buttons = script.pop(0)[1]
                sim.irq_check = True
            if args.dock_at is not None and frame == args.dock_at:
                sim.docked = 1
            sim.advance_to((frame + 1) / 30.0)
            if frame in shots:
                sim.save_png(shots[frame])
    except (SimStop, vmusim.VmuStop) as e:
        status = str(e)
        failed = not sim.exited
    dt = time.time() - t0
    print('pocketsim: %s  [%.1f s host time, %.1f emulated s]' % (status, dt, sim.seconds))
    if sim.tty:
        print('[tty] ' + sim.tty)
    if args.screenshot:
        sim.save_png(args.screenshot)
    if args.ascii:
        print(sim.screen_text())
    if args.wav:
        sim.save_wav(args.wav)
    return 2 if failed else 0


class AudioOut:
    """Streams the simulated speaker to the PC sound card (Windows waveOut via ctypes,
    no extra packages). Plays exactly what the program writes to the DAC, so short
    notes and sequences sound like they do in other emulators."""
    RATE = 22050
    CHUNK = 735          # 1/30 s
    NBUF = 16
    CUSHION = 3          # chunks queued ahead to ride out GUI hiccups

    def __init__(self, enabled=True):
        self.ok = False
        self.muted = not enabled
        self.pending = []
        if os.name != 'nt':
            return
        try:
            self._open()
            self.ok = True
        except Exception as e:                       # no sound card, driver error...
            print('pocketsim: audio disabled (%s)' % e, file=sys.stderr)

    def _open(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ct = ctypes

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [('wFormatTag', wt.WORD), ('nChannels', wt.WORD), ('nSamplesPerSec', wt.DWORD),
                        ('nAvgBytesPerSec', wt.DWORD), ('nBlockAlign', wt.WORD),
                        ('wBitsPerSample', wt.WORD), ('cbSize', wt.WORD)]

        class WAVEHDR(ctypes.Structure):
            pass
        WAVEHDR._fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', wt.DWORD),
                            ('dwBytesRecorded', wt.DWORD), ('dwUser', ctypes.c_size_t),
                            ('dwFlags', wt.DWORD), ('dwLoops', wt.DWORD),
                            ('lpNext', ctypes.POINTER(WAVEHDR)), ('reserved', ctypes.c_size_t)]
        self.WAVEHDR = WAVEHDR
        w = ctypes.WinDLL('winmm')
        self.w = w
        w.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.UINT, ctypes.POINTER(WAVEFORMATEX),
                                  ctypes.c_size_t, ctypes.c_size_t, wt.DWORD]
        for fn in ('waveOutPrepareHeader', 'waveOutUnprepareHeader', 'waveOutWrite'):
            getattr(w, fn).argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wt.UINT]
            getattr(w, fn).restype = wt.UINT
        w.waveOutOpen.restype = wt.UINT
        w.waveOutReset.argtypes = [ctypes.c_void_p]
        w.waveOutClose.argtypes = [ctypes.c_void_p]
        fmt = WAVEFORMATEX(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)
        self.h = ctypes.c_void_p()
        err = w.waveOutOpen(ctypes.byref(self.h), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0)
        if err:
            raise OSError('waveOutOpen error %d' % err)
        self.bufs = [ctypes.create_string_buffer(self.CHUNK * 2) for _ in range(self.NBUF)]
        self.hdrs = [WAVEHDR() for _ in range(self.NBUF)]
        self.busy = [False] * self.NBUF
        self.sz = ctypes.sizeof(WAVEHDR)

    def _queued(self):
        n = 0
        for i, h in enumerate(self.hdrs):
            if self.busy[i]:
                if h.dwFlags & 1:                      # WHDR_DONE
                    self.w.waveOutUnprepareHeader(self.h, self.ct.byref(h), self.sz)
                    self.busy[i] = False
                else:
                    n += 1
        return n

    def _write(self, samples):
        for i in range(self.NBUF):
            if not self.busy[i]:
                break
        else:
            return                                     # we're far ahead: drop
        data = struct.pack('<%dh' % len(samples), *samples)
        self.ct.memmove(self.bufs[i], data, len(data))
        h = self.hdrs[i]
        h.lpData = self.ct.cast(self.bufs[i], self.ct.c_void_p)
        h.dwBufferLength = len(data)
        h.dwFlags = 0
        self.w.waveOutPrepareHeader(self.h, self.ct.byref(h), self.sz)
        self.w.waveOutWrite(self.h, self.ct.byref(h), self.sz)
        self.busy[i] = True

    def feed(self, sim, silent=False):
        samples = sim.take_audio(self.RATE)            # always consume, even when muted
        if not self.ok:
            return
        if self.muted or silent:
            samples = [0] * len(samples)
        self.pending += [max(-32767, min(32767, v // 2)) for v in samples]
        q = self._queued()
        if q == 0 and len(self.pending) < self.CHUNK * self.CUSHION:
            self.pending = [0] * (self.CHUNK * self.CUSHION - len(self.pending)) + self.pending  # refill cushion
        while len(self.pending) >= self.CHUNK:
            self._write(self.pending[:self.CHUNK])
            del self.pending[:self.CHUNK]

    def toggle_mute(self):
        self.muted = not self.muted

    def stop(self):
        if self.ok:
            self.ok = False
            self.w.waveOutReset(self.h)
            self._queued()
            self.w.waveOutClose(self.h)


def gui(sim, args, path):
    import tkinter as tk
    player = AudioOut(enabled=not args.mute)
    W, H = sim.width, sim.height
    ink, paper, bezel = sim.colors
    is_vmu = isinstance(sim, vmusim.VMU)
    scale = 9 if W <= 32 else 7
    root = tk.Tk()
    root.title('pocketsim - ' + sim.title + ('  (Dreamcast VMU)' if is_vmu else ''))
    root.configure(bg='#2b2d31')
    cv = tk.Canvas(root, width=W * scale + 40, height=H * scale + 40, bg='#d9d9d4', highlightthickness=0)
    cv.pack(padx=12, pady=(12, 4))
    cv.create_rectangle(14, 14, W * scale + 26, H * scale + 26, fill=bezel, outline='#666')
    pix = [[cv.create_rectangle(20 + x * scale, 20 + y * scale, 20 + (x + 1) * scale - 1, 20 + (y + 1) * scale - 1,
                                fill=paper, outline='') for x in range(W)] for y in range(H)]
    status = tk.Label(root, text='', fg='#ddd', bg='#2b2d31', font=('Consolas', 10), anchor='w', justify='left')
    status.pack(fill='x', padx=12, pady=(0, 10))
    keys = sim.keymap
    BTNS = sim.btn
    held = set()
    tapped = set()          # keys pressed since the last tick, so quick taps are never lost
    state = {'last': None, 'stopped': None, 'frame': 0, 't0': time.time(), 'sec0': 0.0, 'focus': True}
    sleep_text = cv.create_text(20 + W // 2 * scale, 20 + H // 2 * scale, text='', fill=ink,
                                font=('Consolas', 11, 'bold'), justify='center')

    def press(e):
        k = e.keysym
        if k in keys:
            held.add(keys[k])
            tapped.add(keys[k])
        elif k in ('d', 'D') and not is_vmu:
            sim.docked ^= 1
        elif k in ('r', 'R'):
            sim.reset(); state['stopped'] = None; state['t0'] = time.time(); state['sec0'] = 0.0
        elif k == 'F12':
            p = os.path.splitext(path)[0] + '_%d.png' % int(time.time())
            sim.save_png(p); print('saved ' + p)
        elif k in ('m', 'M'):
            player.toggle_mute()
        elif k == 'Escape':
            player.stop()
            root.destroy()

    def release(e):
        if e.keysym in keys:
            held.discard(keys[e.keysym])

    def focus_in(e):
        state['focus'] = True

    def focus_out(e):
        # Key releases are not delivered while another window has focus, so forget
        # held keys; otherwise a button could stay "stuck down" when you come back.
        state['focus'] = False
        held.clear()

    root.bind('<KeyPress>', press)
    root.bind('<KeyRelease>', release)
    root.bind('<FocusIn>', focus_in)
    root.bind('<FocusOut>', focus_out)
    cv.bind('<Button-1>', lambda e: root.focus_force())
    try:
        root.focus_force()
    except Exception:
        pass

    def tick():
        mask = 0
        for b in held | tapped:
            mask |= BTNS[b]
        tapped.clear()
        sleeping = getattr(sim, 'sleeping', False)
        if sleeping and mask:
            # convenience: any key wakes the simulated unit, even programs built with an
            # older kit whose standby only listened to the action button
            mask |= BTNS['fire']
        sim.buttons = mask
        sim.irq_check = True
        if not state['stopped']:
            target = state['sec0'] + (time.time() - state['t0'])
            target = min(target, sim.seconds + 0.1)          # don't spiral if the host is slow
            try:
                sim.advance_to(target)
            except (SimStop, vmusim.VmuStop) as e:
                state['stopped'] = str(e)
                print('pocketsim: ' + state['stopped'])
            if sim.seconds < target - 0.05:                  # host too slow: resync clock
                state['t0'] = time.time(); state['sec0'] = sim.seconds
        rows = sim.screen()
        if rows != state['last']:
            for y in range(H):
                for x in range(W):
                    if state['last'] is None or rows[y][x] != state['last'][y][x]:
                        cv.itemconfig(pix[y][x], fill=ink if rows[y][x] else paper)
            state['last'] = rows
        player.feed(sim, silent=bool(state['stopped']))
        snd = 'SND' if sim.dac_level() else '   '
        led = 'LED' if not (sim.iop & 2) else '   '
        sleeping = getattr(sim, 'sleeping', False)
        cv.itemconfig(sleep_text, text='standby\n\npress any key\nto wake' if sleeping else '')
        if state['stopped']:
            hint = state['stopped']
        elif not state['focus']:
            hint = 'click this window to control the game'
        elif sleeping:
            hint = 'STANDBY (saving battery): press any key to wake'
        else:
            hint = sim.help
        where = 'VMU' if is_vmu else ('DOCKED' if sim.docked else 'handheld')
        text = '%s  %s %s  %s\n%s' % (where, snd, led, 'sleep' if sleeping else '', hint)
        status.config(text=text)
        root.after(16, tick)

    root.protocol('WM_DELETE_WINDOW', lambda: (player.stop(), root.destroy()))
    tick()
    root.mainloop()
    player.stop()


def main():
    ap = argparse.ArgumentParser(description='PocketStation / Dreamcast VMU simulator (HLE, pure Python)')
    ap.add_argument('file', help='.bin, .mcs or .mcd (PocketStation), .vms (VMU)')
    ap.add_argument('--headless', action='store_true', help='no window; use with --frames')
    ap.add_argument('--frames', type=int, default=300, help='headless: frames (1/30 s) to run')
    ap.add_argument('--input', help='headless: button script, e.g. "30:right,45:,60:fire+up"')
    ap.add_argument('--dock-at', type=int, help='headless: insert into PSX at this frame')
    ap.add_argument('--shot', action='append', metavar='FRAME:FILE.png', help='headless: screenshot at frame')
    ap.add_argument('--screenshot', help='screenshot at the end (PNG)')
    ap.add_argument('--ascii', action='store_true', help='print the final screen as text')
    ap.add_argument('--wav', help='write the speaker output to a WAV file')
    ap.add_argument('--fresh', action='store_true', help='ignore saved data from earlier runs')
    ap.add_argument('--mute', action='store_true', help='window mode: no sound through the PC speakers')
    args = ap.parse_args()

    data, idx, fn = load_program(args.file)
    if idx == 'vmu':
        try:
            sim = vmusim.VMU(data)
        except ValueError as e:
            raise SystemExit('pocketsim: %s' % e)
        if args.headless:
            sys.exit(headless(sim, args))
        gui(sim, args, args.file)
        return
    save = os.path.splitext(args.file)[0] + '.simsave'
    if args.fresh and os.path.exists(save):
        os.remove(save)
    sim = PocketStation(data, dir_index=idx, save_path=save)
    if args.headless:
        sys.exit(headless(sim, args))
    gui(sim, args, args.file)


if __name__ == '__main__':
    main()
