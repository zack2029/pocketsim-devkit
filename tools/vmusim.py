# SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
"""
vmusim.py - Dreamcast VMU simulator core used by pocketsim (pure Python).

Emulates the Sanyo LC86K CPU, RAM banks, SFRs, the 48x32 LCD (XRAM), the
buttons (P3), timer 0 (frame timer / interrupts), timer 1 (piezo buzzer),
the base timer, HALT, and the BIOS entry points a game uses (clock handler
at $130, exit at $1F0) with Python stand-ins, so no BIOS dump is needed.

Semantics follow the waterbear instruction reference and libevmu
(ElysianVMU, MIT licensed, by Falco Girgis), used as references.
Not cycle-exact; use it for quick iteration, then check on ElysianVMU or a
real VMU.
"""
import struct
import sys
import time

RC_HZ = 879236
QUARTZ_HZ = 32768
CF_HZ = 6000000

# SFR addresses (offset from $100)
ACC, PSW, B, C, TRL, TRH, SP, PCON, IE, IP = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09
EXT, OCR = 0x0D, 0x0E
T0CON, T0PRR, T0L, T0LR, T0H, T0HR = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15
T1CNT, T1LC, T1L, T1HC, T1H = 0x18, 0x1A, 0x1B, 0x1C, 0x1D
MCR, XBNK, VCCR = 0x20, 0x25, 0x27
P1, P1DDR, P1FCR, P3, P3DDR, P3INT, P7 = 0x44, 0x45, 0x46, 0x4C, 0x4D, 0x4E, 0x5C
BTCR = 0x7F

# button bits in P3 (0 = pressed on the hardware)
BTN = {'up': 0x01, 'down': 0x02, 'left': 0x04, 'right': 0x08,
       'a': 0x10, 'b': 0x20, 'mode': 0x40, 'sleep': 0x80, 'fire': 0x10}

# XRAM row base offsets (row 0..15 within a bank): 6 bytes per row, rows in pairs,
# then 4 unused bytes
ROW_OFS = [(r >> 1) * 16 + (r & 1) * 6 for r in range(16)]

CYCLES = [1] * 256        # most instructions: 1 cycle; overridden below


class VmuStop(Exception):
    pass


class VMU:
    width, height = 48, 32
    btn = BTN
    keymap = {'Left': 'left', 'Right': 'right', 'Up': 'up', 'Down': 'down',
              'z': 'a', 'Z': 'a', 'space': 'a', 'x': 'b', 'X': 'b',
              'Return': 'mode', 'BackSpace': 'sleep'}
    help = 'arrows: d-pad  Z/Space: A  X: B  Enter: MODE  M: mute  R: restart  F12: shot'
    colors = ('#1e2646', '#9fb0cc', '#7f90b0')       # ink, paper, bezel

    def __init__(self, image, quiet=False):
        if len(image) < 0x280:
            raise ValueError('not a VMU game: file is too small to have a header')
        self.flash = bytearray(image) + bytearray(max(0, 0x10000 - len(image)))
        self.title = image[0x200:0x210].decode('shift_jis', 'replace').strip(' \0') or 'VMU game'
        self.quiet = quiet
        self.warned = set()
        self.tty = ''
        self.reset()

    # ------------------------------------------------------------ state
    def reset(self):
        self.ram = [bytearray(256), bytearray(256)]
        self.sfr = bytearray(0x80)
        self.xram = [bytearray(0x80), bytearray(0x80), bytearray(0x80)]
        self.pc = 0
        self.cycles = 0
        self.buttons = 0             # our mask: 1 = pressed
        self.in_irq = 0
        self.halted = False
        self.exited = None
        self.sleeping = False
        self.docked = 0
        self.iop = 0x02              # pocketsim GUI compatibility (LED off)
        self.t0_acc = 0              # sub-tick cycle accumulator
        self.bt_acc = 0
        self.bt_count = 0
        self.tone_events = [(0.0, 0.0)]
        self.all_tones = [(0.0, 0.0)]
        self.tone_now = 0.0
        self.cur = 0
        self.spin = False
        self.t_base = 0.0            # emulated seconds at cyc_base
        self.cyc_base = 0
        self.audio_t = 0.0
        # state as left by the BIOS when it starts a game
        s = self.sfr
        s[SP] = 0x7F
        s[PSW] = 0x02                # RAM bank 1 for the game
        s[IE] = 0xFF
        s[EXT] = 0x01                # running from flash
        s[OCR] = 0xA3                # quartz clock, /6 (games normally speed this up)
        s[P1DDR] = 0xFF
        s[P1FCR] = 0xBF
        s[P3INT] = 0xFD
        s[P7] = 0x02
        s[BTCR] = 0x41               # base timer running, int0 every 0.5 s
        s[MCR] = 0x08
        s[VCCR] = 0x80
        s[XBNK] = 0
        self.xram[2][0x01 + 4] = 0x10  # GAME icon on
        self.t1lr = 0
        self.t0lr = 0
        self.t0hr = 0

    def warn(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            print('pocketsim (vmu): ' + msg, file=sys.stderr)

    # ------------------------------------------------------------ clocks
    def cycle_hz(self):
        ocr = self.sfr[OCR]
        div = 6 if ocr & 0x80 else 12
        sel = (ocr >> 4) & 3
        if sel == 2:
            return QUARTZ_HZ / div
        if sel & 1:
            return CF_HZ / div           # ceramic oscillator (only when docked, in practice)
        return RC_HZ / div

    @property
    def cpu_hz(self):
        return int(self.cycle_hz())

    @property
    def seconds(self):
        """Emulated time (the clock can change speed while the game runs)."""
        return self.t_base + (self.cycles - self.cyc_base) / self.cycle_hz()

    def advance_to(self, t):
        """Run until the emulated clock reaches t seconds."""
        need = t - self.seconds
        if need > 0:
            self.run_until(self.cycles + max(1, int(need * self.cycle_hz())))

    # ------------------------------------------------------------ memory
    def ramb(self):
        return self.ram[(self.sfr[PSW] >> 1) & 1]

    def read(self, a):
        a &= 0x1FF
        if a < 0x100:
            return self.ramb()[a]
        if a < 0x180:
            o = a - 0x100
            if o == P3:
                return (~self.buttons) & 0xFF
            if o == PSW:
                acc = self.sfr[ACC]
                parity = bin(acc).count('1') & 1
                return (self.sfr[PSW] & 0xFE) | parity
            if o == T1L:
                return self.sfr[T1L]
            return self.sfr[o]
        bank = self.sfr[XBNK]
        if bank > 2:
            return 0
        return self.xram[bank][a - 0x180]

    def write(self, a, v):
        a &= 0x1FF
        v &= 0xFF
        if a < 0x100:
            self.ramb()[a] = v
            return
        if a < 0x180:
            o = a - 0x100
            if o == T0LR:
                self.t0lr = v; self.sfr[o] = v; return
            if o == T0HR:
                self.t0hr = v; self.sfr[o] = v; return
            if o == T1L:                # write = reload register
                self.t1lr = v; self.tone_update(); return
            if o in (T1CNT, T1LC, P1, P1DDR, P1FCR):
                self.sfr[o] = v; self.tone_update(); return
            if o == T0CON:
                old = self.sfr[o]
                self.sfr[o] = v
                if (v & 0x40) and not (old & 0x40):
                    self.sfr[T0L] = self.t0lr
                if (v & 0x80) and not (old & 0x80):
                    self.sfr[T0H] = self.t0hr
                return
            if o == PCON:
                if v & 0x01:
                    self.halted = True
                if v & 0x02:
                    self.sleeping = True    # HOLD: wait for a key
                self.sfr[o] = 0
                return
            if o == OCR:
                self.t_base = self.seconds
                self.cyc_base = self.cycles
                self.sfr[o] = v
                return
            if o == XBNK:
                if v > 2:
                    self.warn('xbnk', 'XBNK set to %d (only 0-2 exist) at PC=$%04X' % (v, self.cur))
            self.sfr[o] = v
            return
        bank = self.sfr[XBNK]
        if bank <= 2:
            self.xram[bank][a - 0x180] = v

    def rd_flash(self, a):
        return self.flash[a & 0xFFFF]

    def push(self, v):
        sp = (self.sfr[SP] + 1) & 0xFF
        if sp < 0x80:
            raise VmuStop('stack overflow/underflow (SP=$%02X) at PC=$%04X' % (sp, self.cur))
        self.sfr[SP] = sp
        self.ram[0][sp] = v & 0xFF

    def pop(self):
        sp = self.sfr[SP]
        if sp < 0x80:
            raise VmuStop('stack underflow at PC=$%04X (more pops/rets than pushes/calls)' % self.cur)
        v = self.ram[0][sp]
        self.sfr[SP] = (sp - 1) & 0xFF
        return v

    def ind(self, j):
        irbk = (self.sfr[PSW] >> 3) & 3
        return self.ramb()[(irbk << 2) | j] | ((j & 2) << 7)

    # ------------------------------------------------------------ flags
    def setf(self, cy=None, ac=None, ov=None):
        p = self.sfr[PSW]
        if cy is not None: p = (p | 0x80) if cy else (p & ~0x80)
        if ac is not None: p = (p | 0x40) if ac else (p & ~0x40)
        if ov is not None: p = (p | 0x04) if ov else (p & ~0x04)
        self.sfr[PSW] = p & 0xFF

    def cy(self):
        return (self.sfr[PSW] >> 7) & 1

    def add(self, b, carry):
        a = self.sfr[ACC]
        c = self.cy() if carry else 0
        r = a + b + c
        self.setf(cy=r > 0xFF, ac=(a & 0xF) + (b & 0xF) + c > 0xF,
                  ov=bool(0x80 & ~(a ^ b) & (a ^ r)))
        self.sfr[ACC] = r & 0xFF

    def sub(self, b, carry):
        a = self.sfr[ACC]
        c = self.cy() if carry else 0
        r = a - b - c
        self.setf(cy=r < 0, ac=(a & 0xF) - (b & 0xF) - c < 0,
                  ov=bool(0x80 & (a ^ b) & (a ^ r)))
        self.sfr[ACC] = r & 0xFF

    # ------------------------------------------------------------ sound
    def tone_update(self):
        s = self.sfr
        hz = 0.0
        if (s[T1CNT] & 0x40) and self.t1lr != 0xFF:
            period = 256 - self.t1lr
            lc = s[T1LC]
            if period > 0 and lc != 0xFF and lc >= self.t1lr:
                hz = QUARTZ_HZ / 6.0 / period
        if hz != self.tone_now:
            self.tone_now = hz
            self.tone_events.append((self.seconds, hz))
            self.all_tones.append((self.seconds, hz))

    def dac_level(self):
        return 1 if self.tone_now else 0

    def take_audio(self, rate):
        """Square-wave samples for the time emulated since the last call."""
        now = self.seconds
        n = int((now - self.audio_t) * rate)
        ev = self.tone_events
        out = [0] * n
        t = self.audio_t
        k = 0
        f = ev[0][1]
        dt = 1.0 / rate
        for i in range(n):
            t += dt
            while k + 1 < len(ev) and ev[k + 1][0] <= t:
                k += 1
                f = ev[k][1]
            if f:
                out[i] = 9000 if (t * f) % 1.0 < 0.5 else -9000
        while k + 1 < len(ev) and ev[k + 1][0] <= t:
            k += 1
        self.audio_t = t
        self.tone_events = ev[k:]
        return out

    # ------------------------------------------------------------ timers & interrupts
    def tick_timers(self, cyc):
        s = self.sfr
        # timer 0: clocked by the cycle clock through the prescaler
        con = s[T0CON]
        if con & 0xC0:
            self.t0_acc += cyc
            div = 256 - s[T0PRR]
            ticks, self.t0_acc = divmod(self.t0_acc, div)
            if ticks:
                if con & 0x20:            # 16-bit mode: T0H:T0L
                    if con & 0x40:
                        v = (s[T0H] << 8 | s[T0L]) + ticks
                        while v > 0xFFFF:
                            v = v - 0x10000 + (self.t0hr << 8 | self.t0lr)
                            s[T0CON] |= 0x08
                        s[T0H], s[T0L] = (v >> 8) & 0xFF, v & 0xFF
                else:
                    if con & 0x40:
                        v = s[T0L] + ticks
                        if v > 0xFF:
                            span = 256 - self.t0lr or 256
                            v = self.t0lr + (v - 256) % span
                            s[T0CON] |= 0x02
                        s[T0L] = v & 0xFF
                    if con & 0x80:
                        v = s[T0H] + ticks
                        if v > 0xFF:
                            span = 256 - self.t0hr or 256
                            v = self.t0hr + (v - 256) % span
                            s[T0CON] |= 0x08
                        s[T0H] = v & 0xFF
        # base timer: 32768 Hz quartz, int0 every 0x4000 ticks (0.5 s) by default
        if s[BTCR] & 0x40:
            self.bt_acc += cyc * QUARTZ_HZ
            hz = self.cycle_hz()
            t = int(self.bt_acc // hz)
            if t:
                self.bt_acc -= t * hz
                rate0 = 0x40 if s[BTCR] & 0x80 else 0x4000
                old = self.bt_count
                self.bt_count += t
                if self.bt_count // rate0 != old // rate0:
                    s[BTCR] |= 0x02

    def pending_irq(self):
        s = self.sfr
        if not (s[IE] & 0x80) or self.in_irq:
            return None
        if (s[T0CON] & 0x03) == 0x03:
            return 0x13
        if (s[BTCR] & 0x03) == 0x03:
            return 0x1B
        if (s[T0CON] & 0x0C) == 0x0C:
            return 0x23
        if (s[P3INT] & 0x03) == 0x03:
            return 0x4B
        return None

    # ------------------------------------------------------------ BIOS stand-ins
    def bios(self, pc):
        """A jmpf with EXT bit0 cleared: the game hands over to the BIOS ROM."""
        if pc == 0x130:           # clock / base timer service
            self.sfr[BTCR] &= ~0x0A & 0xFF
            self.sfr[EXT] |= 0x01
            self.pc = 0x139
            return
        if pc == 0x1F0:
            self.exited = 'returned to the VMU menu'
            raise VmuStop(self.exited)
        if pc in (0x100, 0x108, 0x110, 0x120):
            raise VmuStop('BIOS flash routine $%03X is not simulated by pocketsim' % pc)
        if pc == 0x140:
            self.exited = 'went to sleep (BIOS sleep)'
            raise VmuStop(self.exited)
        raise VmuStop('jumped into the BIOS at $%04X, which pocketsim does not simulate' % pc)

    # ------------------------------------------------------------ CPU
    def step(self):
        s = self.sfr
        pc = self.pc
        self.cur = pc
        f = self.flash
        op = f[pc]
        hi = op >> 4
        lo = op & 0x0F
        cyc = 1

        def nxt(n):
            self.pc = (pc + n) & 0xFFFF

        def rel8(at):
            o = f[at]
            return o - 256 if o & 0x80 else o

        # ---- decode by opcode groups
        if op == 0x00:                                    # nop
            nxt(1)
        elif op == 0x01:                                  # br r8
            nxt(2); self.pc = (self.pc + rel8(pc + 1)) & 0xFFFF; cyc = 2
        elif hi == 0x0 and lo in (2, 3):                  # ld d9
            self.sfr[ACC] = self.read(((op & 1) << 8) | f[pc + 1]); nxt(2)
        elif hi == 0x0 and lo >= 4:                       # ld @Ri
            if lo >= 8:
                self.call12(op, pc)
                cyc = 2
            else:
                self.sfr[ACC] = self.read(self.ind(op & 3)); nxt(1)
        elif hi == 0x0:
            self.call12(op, pc); cyc = 2
        elif op == 0x10:                                  # callr r16
            r = f[pc + 1] | f[pc + 2] << 8
            ret = (pc + 3) & 0xFFFF
            self.push(ret & 0xFF); self.push(ret >> 8)
            self.pc = (ret + r - 1) & 0xFFFF; cyc = 4
        elif op == 0x11:                                  # brf r16
            r = f[pc + 1] | f[pc + 2] << 8
            self.pc = (pc + 3 + r - 1) & 0xFFFF; cyc = 4
        elif hi == 0x1 and lo in (2, 3):                  # st d9
            self.write(((op & 1) << 8) | f[pc + 1], s[ACC]); nxt(2)
        elif hi == 0x1 and 4 <= lo < 8:                   # st @Ri
            self.write(self.ind(op & 3), s[ACC]); nxt(1)
        elif hi == 0x1:
            self.call12(op, pc); cyc = 2
        elif op == 0x20:                                  # callf a16
            ret = (pc + 3) & 0xFFFF
            self.push(ret & 0xFF); self.push(ret >> 8)
            self.pc = f[pc + 1] << 8 | f[pc + 2]; cyc = 2
        elif op == 0x21:                                  # jmpf a16
            self.pc = f[pc + 1] << 8 | f[pc + 2]; cyc = 2
            if not (s[EXT] & 1):                          # not1 EXT,0 + jmpf = call into the BIOS
                self.bios(self.pc)
            elif self.pc == pc:
                self.idle_loop()
        elif hi == 0x2 and lo in (2, 3):                  # mov #i8, d9
            self.write(((op & 1) << 8) | f[pc + 1], f[pc + 2]); nxt(3); cyc = 2
        elif hi == 0x2 and 4 <= lo < 8:                   # mov #i8, @Ri
            self.write(self.ind(op & 3), f[pc + 1]); nxt(2)
        elif hi in (0x2, 0x3) and lo >= 8:                # jmp a12
            t = ((pc + 2) & 0xF000) | ((op & 0x10) << 7) | ((op & 7) << 8) | f[pc + 1]
            self.pc = t; cyc = 2
            if t == pc:
                self.idle_loop()
        elif op == 0x30:                                  # mul
            v = (s[C] | s[ACC] << 8) * s[B]
            s[C], s[ACC], s[B] = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
            self.setf(cy=False, ov=v > 0xFFFF); nxt(1); cyc = 7
        elif op == 0x31:                                  # be #i8, r8
            self.cmp_branch(s[ACC], f[pc + 1], True, pc, 3); cyc = 2
        elif hi == 0x3 and lo in (2, 3):                  # be d9, r8
            self.cmp_branch(s[ACC], self.read(((op & 1) << 8) | f[pc + 1]), True, pc, 3); cyc = 2
        elif hi == 0x3 and 4 <= lo < 8:                   # be @Ri, #i8, r8
            self.cmp_branch(self.read(self.ind(op & 3)), f[pc + 1], True, pc, 3); cyc = 2
        elif op == 0x40:                                  # div
            d = s[B]
            if d:
                v = s[C] | s[ACC] << 8
                q, r = divmod(v, d)
                s[B], s[C], s[ACC] = r, q & 0xFF, (q >> 8) & 0xFF
            else:
                s[ACC] = 0xFF
            self.setf(cy=False, ov=not d); nxt(1); cyc = 7
        elif op == 0x41:                                  # bne #i8, r8
            self.cmp_branch(s[ACC], f[pc + 1], False, pc, 3); cyc = 2
        elif hi == 0x4 and lo in (2, 3):                  # bne d9, r8
            self.cmp_branch(s[ACC], self.read(((op & 1) << 8) | f[pc + 1]), False, pc, 3); cyc = 2
        elif hi == 0x4 and 4 <= lo < 8:                   # bne @Ri, #i8, r8
            self.cmp_branch(self.read(self.ind(op & 3)), f[pc + 1], False, pc, 3); cyc = 2
        elif hi in (0x4, 0x5, 0x6, 0x7, 0x8, 0x9, 0xA, 0xB, 0xC, 0xD, 0xE, 0xF) and lo >= 8:
            cyc = self.bit_ops(op, pc)
        elif op == 0x50:                                  # ldf
            self.warn('ldf', 'LDF (flash read) is not simulated'); nxt(1)
        elif op == 0x51:
            self.warn('stf', 'STF (flash write) is not simulated'); nxt(1)
        elif hi == 0x5 and lo in (2, 3):                  # dbnz d9, r8
            a = ((op & 1) << 8) | f[pc + 1]
            v = (self.read(a) - 1) & 0xFF
            self.write(a, v); nxt(3); cyc = 2
            if v:
                self.pc = (self.pc + rel8(pc + 2)) & 0xFFFF
        elif hi == 0x5 and 4 <= lo < 8:                   # dbnz @Ri, r8
            a = self.ind(op & 3)
            v = (self.read(a) - 1) & 0xFF
            self.write(a, v); nxt(2); cyc = 2
            if v:
                self.pc = (self.pc + rel8(pc + 1)) & 0xFFFF
        elif hi == 0x6 and lo in (0, 1):                  # push d9
            self.push(self.read(((op & 1) << 8) | f[pc + 1])); nxt(2); cyc = 2
        elif hi == 0x6 and lo in (2, 3):                  # inc d9
            a = ((op & 1) << 8) | f[pc + 1]
            self.write(a, self.read(a) + 1); nxt(2)
        elif hi == 0x6 and 4 <= lo < 8:                   # inc @Ri
            a = self.ind(op & 3)
            self.write(a, self.read(a) + 1); nxt(1)
        elif hi == 0x7 and lo in (0, 1):                  # pop d9
            self.write(((op & 1) << 8) | f[pc + 1], self.pop()); nxt(2); cyc = 2
        elif hi == 0x7 and lo in (2, 3):                  # dec d9
            a = ((op & 1) << 8) | f[pc + 1]
            self.write(a, self.read(a) - 1); nxt(2)
        elif hi == 0x7 and 4 <= lo < 8:                   # dec @Ri
            a = self.ind(op & 3)
            self.write(a, self.read(a) - 1); nxt(1)
        elif op == 0x80:                                  # bz r8
            nxt(2); cyc = 2
            if s[ACC] == 0:
                self.pc = (self.pc + rel8(pc + 1)) & 0xFFFF
        elif op == 0x90:                                  # bnz r8
            nxt(2); cyc = 2
            if s[ACC] != 0:
                self.pc = (self.pc + rel8(pc + 1)) & 0xFFFF
        elif op == 0xA0:                                  # ret
            h = self.pop(); l = self.pop()
            self.pc = h << 8 | l; cyc = 2
        elif op == 0xB0:                                  # reti
            h = self.pop(); l = self.pop()
            self.pc = h << 8 | l; cyc = 2
            self.in_irq = max(0, self.in_irq - 1)
        elif op == 0xC0:                                  # ror
            a = s[ACC]; s[ACC] = ((a >> 1) | (a << 7)) & 0xFF; nxt(1)
        elif op == 0xC1:                                  # ldc
            s[ACC] = self.rd_flash(((s[TRH] << 8 | s[TRL]) + s[ACC]) & 0xFFFF); nxt(1); cyc = 2
        elif op == 0xD0:                                  # rorc
            a = s[ACC]; c = self.cy()
            s[ACC] = (a >> 1) | (c << 7); self.setf(cy=a & 1); nxt(1)
        elif op == 0xE0:                                  # rol
            a = s[ACC]; s[ACC] = ((a << 1) | (a >> 7)) & 0xFF; nxt(1)
        elif op == 0xF0:                                  # rolc
            a = s[ACC]; c = self.cy()
            s[ACC] = ((a << 1) | c) & 0xFF; self.setf(cy=a >> 7); nxt(1)
        elif hi in (0x8, 0x9, 0xA, 0xB, 0xD, 0xE, 0xF) and lo < 8:
            self.alu(op, pc)
        elif hi == 0xC and lo in (2, 3):                  # xch d9
            a = ((op & 1) << 8) | f[pc + 1]
            t = self.read(a); self.write(a, s[ACC]); s[ACC] = t; nxt(2)
        elif hi == 0xC and 4 <= lo < 8:                   # xch @Ri
            a = self.ind(op & 3)
            t = self.read(a); self.write(a, s[ACC]); s[ACC] = t; nxt(1)
        else:
            raise VmuStop('invalid opcode $%02X at PC=$%04X' % (op, pc))
        return cyc

    def call12(self, op, pc):
        f = self.flash
        ret = (pc + 2) & 0xFFFF
        self.push(ret & 0xFF); self.push(ret >> 8)
        self.pc = (ret & 0xF000) | ((op & 0x10) << 7) | ((op & 7) << 8) | f[pc + 1]

    def cmp_branch(self, v1, v2, equal, pc, size):
        self.setf(cy=v1 < v2)
        self.pc = (pc + size) & 0xFFFF
        if (v1 == v2) == equal:
            o = self.flash[pc + size - 1]
            self.pc = (self.pc + (o - 256 if o & 0x80 else o)) & 0xFFFF

    def bit_ops(self, op, pc):
        f = self.flash
        a = ((op >> 4) & 1) << 8 | f[pc + 1]
        bit = 1 << (op & 7)
        group = op & 0xE0
        if group in (0x60, 0x40, 0x80):                  # bp / bpc / bn: d9, b3, r8
            v = self.read(a)
            self.pc = (pc + 3) & 0xFFFF
            o = f[pc + 2]
            off = o - 256 if o & 0x80 else o
            if group == 0x60 and v & bit:
                self.pc = (self.pc + off) & 0xFFFF
            elif group == 0x40 and v & bit:
                self.write(a, v & ~bit)
                self.pc = (self.pc + off) & 0xFFFF
            elif group == 0x80 and not (v & bit):
                self.pc = (self.pc + off) & 0xFFFF
            if self.pc == pc:
                self.idle_loop()
            return 2
        v = self.read(a)
        if group == 0xE0: v |= bit                       # set1
        elif group == 0xC0: v &= ~bit                    # clr1
        elif group == 0xA0: v ^= bit                     # not1
        else:
            raise VmuStop('invalid opcode $%02X at PC=$%04X' % (op, pc))
        self.write(a, v)
        self.pc = (pc + 2) & 0xFFFF
        return 1

    def alu(self, op, pc):
        f = self.flash
        lo = op & 0x0F
        if lo == 1:
            b = f[pc + 1]; self.pc = (pc + 2) & 0xFFFF
        elif lo in (2, 3):
            b = self.read(((op & 1) << 8) | f[pc + 1]); self.pc = (pc + 2) & 0xFFFF
        elif 4 <= lo < 8:
            b = self.read(self.ind(op & 3)); self.pc = (pc + 1) & 0xFFFF
        else:
            raise VmuStop('invalid opcode $%02X at PC=$%04X' % (op, pc))
        g = op & 0xF0
        s = self.sfr
        if g == 0x80: self.add(b, False)
        elif g == 0x90: self.add(b, True)
        elif g == 0xA0: self.sub(b, False)
        elif g == 0xB0: self.sub(b, True)
        elif g == 0xE0: s[ACC] &= b
        elif g == 0xD0: s[ACC] |= b
        elif g == 0xF0: s[ACC] ^= b

    def idle_loop(self):
        """A branch to itself (busy wait): skip ahead a little before trying again."""
        self.spin = True

    # ------------------------------------------------------------ run
    def run_until(self, limit):
        self.cycle_limit = limit
        if self.sleeping:
            if self.buttons:
                self.sleeping = False
            else:
                self.cycles = limit
                return
        while self.cycles < limit:
            vec = self.pending_irq()
            if vec is not None:
                self.halted = False
                self.push(self.pc & 0xFF); self.push(self.pc >> 8)
                self.pc = vec
                self.in_irq += 1
            if self.halted or self.spin:
                # nothing to do until a timer fires: fast-forward in small steps
                self.spin = False
                step = 64
                self.cycles += step
                self.tick_timers(step)
                continue
            c = self.step()
            self.cycles += c
            self.tick_timers(c)
            if self.sleeping:
                self.cycles = limit
                break

    # ------------------------------------------------------------ output
    def screen(self):
        on = (self.sfr[VCCR] & 0x80) and (self.sfr[MCR] & 0x08)
        rows = []
        for y in range(32):
            bank = self.xram[y >> 4]
            base = ROW_OFS[y & 15]
            row = []
            for xb in range(6):
                v = bank[base + xb] if on else 0
                for bit in range(7, -1, -1):
                    row.append(bool(v >> bit & 1))
            rows.append(row)
        return rows

    def screen_text(self):
        return '\n'.join(''.join('#' if p else '.' for p in row) for row in self.screen())

    def save_png(self, path, scale=6):
        import psformat as pf
        on_col, off_col = (30, 38, 70), (150, 170, 200)
        px = []
        for row in self.screen():
            line = []
            for p in row:
                line += [on_col if p else off_col] * scale
            px += [line] * scale
        pf.write_png(path, 48 * scale, 32 * scale, px)

    def save_wav(self, path, rate=22050):
        self.audio_t = 0.0
        self.tone_events = self.all_tones
        samples = self.take_audio(rate)
        data = struct.pack('<%dh' % len(samples), *samples)
        hdr = b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' + struct.pack(
            '<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) + b'data' + struct.pack('<I', len(data))
        open(path, 'wb').write(hdr + data)


def is_vmu_game(data):
    """Heuristic: a VMU game has its vectors at 0 and a header at $200."""
    if len(data) < 0x280 or data[:2] == b'SC' or data[:2] == b'MC':
        return False
    return data[0] in (0x21, 0x2A, 0x28, 0x29, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x38, 0x39,
                       0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F)
