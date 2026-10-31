#!/usr/bin/env python3
# SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
"""
vmasm - assembler for the Dreamcast VMU CPU (Sanyo LC86K / "Potato").

  python vmasm.py game.s -o game.bin [-I include_dir] [--listing game.lst]

The syntax follows the common VMU homebrew style (Marcus Comstedt's assembler and
waterbear), so existing sources mostly assemble unchanged:

  label:            global label            .local:   label local to the last global
  NAME EQU $100     constant                NAME = 12 constant
  .org $200         set address             .include "sfr.i"
  .byte 1, $ff, "text"                      .word label, $1234      (little endian)
  .text 16 "Title"  (space padded)          .string 16 "x"         (zero padded)
  .cnop add, mult   pad to a multiple       .incbin "file.bin"
  numbers: 123  $7b  0x7b  %1111011  0b1111011  'A'
  operators: + - * / % & | ^ << >> ~ ( )   and unary < (low byte) > (high byte)
  operands:  #imm   @R0..@R3   direct (d9 / address)

Every instruction of the LC86K is supported; see docs at the end of this file.
No external dependencies: standard library only.
"""
import os
import re
import sys

# ------------------------------------------------------------------ errors
class AsmError(Exception):
    def __init__(self, msg, where=None):
        super().__init__(msg)
        self.where = where

    def __str__(self):
        if self.where:
            return '%s:%d: %s' % (self.where[0], self.where[1], self.args[0])
        return self.args[0]


# ------------------------------------------------------------------ expressions
TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<num>\$[0-9a-fA-F]+|0[xX][0-9a-fA-F]+|%[01]+|0[bB][01]+|[0-9]+)
     |(?P<chr>'(?:\\.|[^'])')
     |(?P<name>\.?[A-Za-z_][A-Za-z0-9_.]*)
     |(?P<op><<|>>|[-+*/%&|^~()<>])
    )""", re.X)


def tokenize(text, where):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise AsmError('cannot parse expression near %r' % text[pos:], where)
        pos = m.end()
        if m.group('num'):
            t = m.group('num')
            if t[0] == '$': v = int(t[1:], 16)
            elif t[:2].lower() == '0x': v = int(t[2:], 16)
            elif t[0] == '%': v = int(t[1:], 2)
            elif t[:2].lower() == '0b': v = int(t[2:], 2)
            else: v = int(t, 10)
            out.append(('num', v))
        elif m.group('chr'):
            c = m.group('chr')[1:-1]
            if c.startswith('\\'):
                c = {'n': '\n', 't': '\t', '0': '\0'}.get(c[1], c[1])
            out.append(('num', ord(c)))
        elif m.group('name'):
            out.append(('name', m.group('name')))
        else:
            out.append(('op', m.group('op')))
    return out


class Expr:
    """Tiny precedence-climbing evaluator. Unknown names raise KeyError."""
    BINARY = {'|': 1, '^': 2, '&': 3, '<<': 4, '>>': 4, '+': 5, '-': 5, '*': 6, '/': 6, '%': 6}

    def __init__(self, text, where):
        self.text = text
        self.where = where
        self.toks = tokenize(text, where)
        if not self.toks:
            raise AsmError('missing value', where)

    def eval(self, lookup):
        self.i = 0
        self.lookup = lookup
        v = self._binary(0)
        if self.i != len(self.toks):
            raise AsmError('unexpected %r in expression %r' % (self.toks[self.i][1], self.text), self.where)
        return v

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _binary(self, minprec):
        left = self._unary()
        while True:
            kind, val = self._peek()
            if kind != 'op' or val not in self.BINARY or self.BINARY[val] < minprec:
                return left
            prec = self.BINARY[val]
            self.i += 1
            right = self._binary(prec + 1)
            if val == '+': left = left + right
            elif val == '-': left = left - right
            elif val == '*': left = left * right
            elif val in ('/', '%'):
                if right == 0:
                    raise AsmError('division by zero in %r' % self.text, self.where)
                left = int(left / right) if val == '/' else left % right
            elif val == '&': left = left & right
            elif val == '|': left = left | right
            elif val == '^': left = left ^ right
            elif val == '<<': left = left << right
            elif val == '>>': left = left >> right

    def _unary(self):
        kind, val = self._peek()
        if kind is None:
            raise AsmError('incomplete expression %r' % self.text, self.where)
        self.i += 1
        if kind == 'num':
            return val
        if kind == 'name':
            return self.lookup(val)
        if val == '(':
            v = self._binary(0)
            if self._peek() != ('op', ')'):
                raise AsmError('missing ) in %r' % self.text, self.where)
            self.i += 1
            return v
        if val == '-': return -self._unary()
        if val == '+': return self._unary()
        if val == '~': return ~self._unary()
        if val == '<': return self._unary() & 0xFF
        if val == '>': return (self._unary() >> 8) & 0xFF
        raise AsmError('unexpected %r in %r' % (val, self.text), self.where)


# ------------------------------------------------------------------ instruction table
# Each mnemonic maps operand "shapes" to an encoder. Shapes:
#   'i8' immediate (#x), 'd9' direct, 'ri' indirect @Rn, 'b3' bit number,
#   'r8' / 'r16' relative target, 'a12' / 'a16' absolute target
SIMPLE = {'nop': 0x00, 'ret': 0xA0, 'reti': 0xB0, 'mul': 0x30, 'div': 0x40,
          'rol': 0xE0, 'rolc': 0xF0, 'ror': 0xC0, 'rorc': 0xD0, 'ldc': 0xC1,
          'ldf': 0x50, 'stf': 0x51}
# accumulator ops with #i8 / d9 / @Ri forms: base opcode
ALU = {'add': 0x80, 'addc': 0x90, 'sub': 0xA0, 'subc': 0xB0,
       'and': 0xE0, 'or': 0xD0, 'xor': 0xF0}
LDST = {'ld': 0x00, 'st': 0x10, 'inc': 0x60, 'dec': 0x70, 'xch': 0xC0}   # d9 / @Ri only
BITOP = {'set1': 0xE8, 'clr1': 0xC8, 'not1': 0xA8}                       # d9, b3
BITBR = {'bp': 0x68, 'bpc': 0x48, 'bn': 0x88}                            # d9, b3, r8


def size_of(mn, shapes):
    if mn in SIMPLE: return 1
    if mn in ALU: return 2 if shapes[0] in ('i8', 'd9') else 1
    if mn in LDST: return 2 if shapes[0] == 'd9' else 1
    if mn in ('push', 'pop'): return 2
    if mn == 'mov': return 3 if shapes[1] == 'd9' else 2
    if mn in BITOP: return 2
    if mn in BITBR: return 3
    if mn in ('bz', 'bnz', 'br'): return 2
    if mn == 'dbnz': return 3 if shapes[0] == 'd9' else 2
    if mn in ('be', 'bne'): return 3
    if mn in ('jmp', 'call'): return 2
    if mn in ('jmpf', 'callf', 'brf', 'callr'): return 3
    raise KeyError(mn)


def expected_shapes(mn):
    """Allowed operand shape tuples for a mnemonic (for parsing/validation)."""
    if mn in SIMPLE: return [()]
    if mn in ALU: return [('i8',), ('d9',), ('ri',)]
    if mn in LDST: return [('d9',), ('ri',)]
    if mn in ('push', 'pop'): return [('d9',)]
    if mn == 'mov': return [('i8', 'd9'), ('i8', 'ri')]
    if mn in BITOP: return [('d9', 'b3')]
    if mn in BITBR: return [('d9', 'b3', 'r8')]
    if mn in ('bz', 'bnz', 'br'): return [('r8',)]
    if mn == 'dbnz': return [('d9', 'r8'), ('ri', 'r8')]
    if mn in ('be', 'bne'): return [('i8', 'r8'), ('d9', 'r8'), ('ri', 'i8', 'r8')]
    if mn in ('jmp', 'call'): return [('a12',)]
    if mn in ('jmpf', 'callf'): return [('a16',)]
    if mn in ('brf', 'callr'): return [('r16',)]
    return None


def classify(op):
    op = op.strip()
    if op.startswith('#'):
        return 'i8'
    if re.fullmatch(r'@[Rr][0-3]', op):
        return 'ri'
    return 'x'      # direct-ish: d9 / b3 / address, decided by position


def match_shapes(mn, ops, where):
    kinds = [classify(o) for o in ops]
    for shapes in expected_shapes(mn):
        if len(shapes) != len(kinds):
            continue
        ok = True
        for want, got in zip(shapes, kinds):
            if want == 'i8' and got != 'i8': ok = False
            elif want == 'ri' and got != 'ri': ok = False
            elif want not in ('i8', 'ri') and got != 'x': ok = False
        if ok:
            return shapes
    forms = ['%s %s' % (mn, ', '.join(s)) if s else mn for s in expected_shapes(mn)]
    raise AsmError('bad operands for %s: expected one of: %s' % (mn, ' | '.join(forms)), where)


def encode(mn, shapes, vals, pc_next, where):
    """vals: evaluated operand values (ri -> register number)."""
    def chk(v, lo, hi, what):
        if v < lo or v > hi:
            raise AsmError('%s out of range: %d (allowed %d..%d)' % (what, v, lo, hi), where)
        return v

    def d9(v):
        return chk(v, 0, 0x1FF, 'address (d9)')

    def i8(v):
        return chk(v, -128, 255, 'immediate') & 0xFF

    def b3(v):
        return chk(v, 0, 7, 'bit number')

    def rel8(target):
        off = target - pc_next
        if off < -128 or off > 127:
            raise AsmError('branch target too far (%d bytes); use brf/jmpf or invert the branch' % off, where)
        return off & 0xFF

    if mn in SIMPLE:
        return [SIMPLE[mn]]
    if mn in ALU:
        base = ALU[mn]
        if shapes[0] == 'i8': return [base | 0x01, i8(vals[0])]
        if shapes[0] == 'd9': v = d9(vals[0]); return [base | 0x02 | (v >> 8), v & 0xFF]
        return [base | 0x04 | vals[0]]
    if mn in LDST:
        base = LDST[mn]
        if shapes[0] == 'd9': v = d9(vals[0]); return [base | 0x02 | (v >> 8), v & 0xFF]
        return [base | 0x04 | vals[0]]
    if mn in ('push', 'pop'):
        v = d9(vals[0])
        return [(0x60 if mn == 'push' else 0x70) | (v >> 8), v & 0xFF]
    if mn == 'mov':
        if shapes[1] == 'd9':
            v = d9(vals[1]); return [0x22 | (v >> 8), v & 0xFF, i8(vals[0])]
        return [0x24 | vals[1], i8(vals[0])]
    if mn in BITOP:
        v = d9(vals[0])
        return [BITOP[mn] | ((v >> 8) << 4) | b3(vals[1]), v & 0xFF]
    if mn in BITBR:
        v = d9(vals[0])
        return [BITBR[mn] | ((v >> 8) << 4) | b3(vals[1]), v & 0xFF, rel8(vals[2])]
    if mn in ('bz', 'bnz', 'br'):
        return [{'bz': 0x80, 'bnz': 0x90, 'br': 0x01}[mn], rel8(vals[0])]
    if mn == 'dbnz':
        if shapes[0] == 'd9':
            v = d9(vals[0]); return [0x52 | (v >> 8), v & 0xFF, rel8(vals[1])]
        return [0x54 | vals[0], rel8(vals[1])]
    if mn in ('be', 'bne'):
        base = 0x30 if mn == 'be' else 0x40
        if shapes[0] == 'i8': return [base | 0x01, i8(vals[0]), rel8(vals[1])]
        if shapes[0] == 'd9':
            v = d9(vals[0]); return [base | 0x02 | (v >> 8), v & 0xFF, rel8(vals[1])]
        return [base | 0x04 | vals[0], i8(vals[1]), rel8(vals[2])]
    if mn in ('jmp', 'call'):
        t = chk(vals[0], 0, 0xFFFF, 'address')
        if (t & 0xF000) != (pc_next & 0xF000):
            raise AsmError('%s target $%04X is outside the current 4 KB page; use %sf' % (mn, t, mn), where)
        base = 0x28 if mn == 'jmp' else 0x08
        return [base | ((t >> 11) & 1) << 4 | ((t >> 8) & 7), t & 0xFF]
    if mn in ('jmpf', 'callf'):
        t = chk(vals[0], 0, 0xFFFF, 'address')
        return [0x21 if mn == 'jmpf' else 0x20, (t >> 8) & 0xFF, t & 0xFF]
    if mn in ('brf', 'callr'):
        off = (vals[0] - pc_next + 1) & 0xFFFF
        return [0x11 if mn == 'brf' else 0x10, off & 0xFF, (off >> 8) & 0xFF]
    raise AsmError('unknown instruction %r' % mn, where)


ALL_MNEMONICS = set(SIMPLE) | set(ALU) | set(LDST) | set(BITOP) | set(BITBR) | {
    'push', 'pop', 'mov', 'bz', 'bnz', 'br', 'dbnz', 'be', 'bne',
    'jmp', 'call', 'jmpf', 'callf', 'brf', 'callr'}

DIRECTIVES = {'.org', '.byte', '.db', '.word', '.dw', '.text', '.string', '.include',
              '.cnop', '.incbin', '.equ', '.align', '.ds', '.space'}


# ------------------------------------------------------------------ source handling
def split_operands(s):
    """Split on commas outside quotes."""
    out, cur, q = [], '', None
    for ch in s:
        if q:
            cur += ch
            if ch == q: q = None
        elif ch in '"\'':
            q = ch; cur += ch
        elif ch == ',':
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def strip_comment(line):
    q = None
    for i, ch in enumerate(line):
        if q:
            if ch == q: q = None
        elif ch in '"\'':
            # a lone ' used as char literal: 'x'
            q = ch
        elif ch == ';':
            return line[:i]
    return line


class Assembler:
    def __init__(self, include_dirs=()):
        self.include_dirs = list(include_dirs)
        self.symbols = {}          # lowercased name -> value
        self.symbol_names = {}     # lowercased -> original spelling
        self.items = []            # parsed statements

    # ---- pass 0: read files, expand includes
    def read(self, path, where=None, depth=0):
        if depth > 16:
            raise AsmError('includes nested too deeply', where)
        resolved = self.find(path, where)
        base = os.path.dirname(resolved)
        with open(resolved, encoding='utf-8', errors='replace') as f:
            for n, raw in enumerate(f, 1):
                w = (resolved, n)
                line = strip_comment(raw.rstrip('\r\n'))
                if not line.strip() or line.lstrip().startswith('*'):
                    continue
                inc = re.match(r'\s*\.include\s+(?:\w+\s+)?"([^"]+)"', line, re.I)
                if inc:
                    self.include_dirs.insert(0, base)
                    try:
                        self.read(inc.group(1), w, depth + 1)
                    finally:
                        self.include_dirs.pop(0)
                    continue
                self.parse_line(line, w)

    def find(self, path, where):
        if os.path.isabs(path) and os.path.exists(path):
            return path
        for d in [''] + self.include_dirs:
            p = os.path.join(d, path)
            if os.path.exists(p):
                return os.path.abspath(p)
        raise AsmError('file not found: %s' % path, where)

    def parse_line(self, line, w):
        s = line.strip()
        # label definitions:  name:   .local:   or a bare .local on its own line
        m = re.match(r'^(\.?[A-Za-z_][A-Za-z0-9_.]*):\s*(.*)$', s)
        if m and not s.lower().startswith(('.byte', '.word', '.text', '.string')):
            self.items.append(('label', m.group(1), w))
            s = m.group(2).strip()
            if not s:
                return
        # constants:  NAME EQU expr  /  NAME = expr  /  .equ NAME, expr
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s+(?:equ|EQU|Equ)\s+(.+)$', s) or \
            re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', s)
        if m:
            self.items.append(('equ', m.group(1), m.group(2).strip(), w))
            return
        m = re.match(r'^\.equ\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(.+)$', s, re.I)
        if m:
            self.items.append(('equ', m.group(1), m.group(2).strip(), w))
            return
        parts = s.split(None, 1)
        head = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ''
        if head.startswith('.') and head not in DIRECTIVES:
            # bare local label without a colon (".Loop")
            if re.fullmatch(r'\.[A-Za-z_][A-Za-z0-9_]*', parts[0]) and not rest:
                self.items.append(('label', parts[0], w))
                return
            raise AsmError('unknown directive %s' % parts[0], w)
        if head in DIRECTIVES:
            self.items.append(('dir', head, rest, w))
            return
        if head in ALL_MNEMONICS:
            ops = split_operands(rest)
            shapes = match_shapes(head, ops, w)
            self.items.append(('ins', head, shapes, ops, w))
            return
        # a global label with no colon at the start of a line, followed by an instruction
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s+(\S.*)$', s)
        if m and m.group(2).split()[0].lower() in ALL_MNEMONICS | DIRECTIVES:
            self.items.append(('label', m.group(1), w))
            self.parse_line(m.group(2), w)
            return
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', s):
            self.items.append(('label', s, w))
            return
        raise AsmError('cannot understand %r' % s, w)

    # ---- symbol helpers
    def scoped(self, name, glob):
        return (glob + name).lower() if name.startswith('.') else name.lower()

    def define(self, name, value, w):
        key = name.lower()
        if key in self.symbols and self.symbols[key] != value:
            raise AsmError('%s defined twice' % name, w)
        self.symbols[key] = value
        self.symbol_names[key] = name

    def lookup_fn(self, glob, allow_missing):
        def look(name):
            key = self.scoped(name, glob)
            if key in self.symbols:
                return self.symbols[key]
            if allow_missing:
                return 0
            raise KeyError(name)
        return look

    def eval(self, text, glob, w, allow_missing=False):
        try:
            return Expr(text, w).eval(self.lookup_fn(glob, allow_missing))
        except KeyError as e:
            raise AsmError('unknown symbol %s' % e.args[0], w)

    # ---- data directive sizes/emitters
    def data_bytes(self, head, rest, glob, w, final):
        ev = (lambda t: self.eval(t, glob, w, allow_missing=not final))
        if head in ('.byte', '.db'):
            out = []
            for op in split_operands(rest):
                if op.startswith('"'):
                    out += list(parse_string(op, w))
                else:
                    v = ev(op)
                    if final and (v < -128 or v > 255):
                        raise AsmError('byte value out of range: %d' % v, w)
                    out.append(v & 0xFF)
            return out
        if head in ('.word', '.dw'):
            out = []
            for op in split_operands(rest):
                v = ev(op)
                if final and (v < -32768 or v > 0xFFFF):
                    raise AsmError('word value out of range: %d' % v, w)
                out += [v & 0xFF, (v >> 8) & 0xFF]
            return out
        if head in ('.text', '.string'):
            m = re.match(r'^(\S+)\s+(".*")\s*$', rest.strip())
            if not m:
                raise AsmError('%s needs a length and a "string"' % head, w)
            n = self.eval(m.group(1), glob, w)
            data = list(parse_string(m.group(2), w))
            if len(data) > n:
                raise AsmError('string is %d bytes, longer than %d' % (len(data), n), w)
            return data + [0x20 if head == '.text' else 0] * (n - len(data))
        if head in ('.ds', '.space'):
            ops = split_operands(rest)
            n = self.eval(ops[0], glob, w)
            fill = self.eval(ops[1], glob, w) & 0xFF if len(ops) > 1 else 0
            return [fill] * n
        if head == '.incbin':
            path = parse_string(rest.strip(), w).decode('latin-1')
            return list(open(self.find(path, w), 'rb').read())
        raise AsmError('not a data directive: %s' % head, w)

    # ---- passes
    def layout(self, final, out=None, listing=None):
        pc = 0
        glob = ''
        hi = 0
        for it in self.items:
            kind = it[0]
            if kind == 'label':
                name, w = it[1], it[2]
                if not name.startswith('.'):
                    glob = name
                key = self.scoped(name, glob)
                if not final:
                    if key in self.symbols and self.symbol_addr_pass.get(key) != pc and key in self.symbol_addr_pass:
                        raise AsmError('label %s defined twice' % name, w)
                    self.symbols[key] = pc
                    self.symbol_addr_pass[key] = pc
                    self.symbol_names[key] = name if not name.startswith('.') else glob + name
            elif kind == 'equ':
                name, expr, w = it[1], it[2], it[3]
                v = self.eval(expr, glob, w, allow_missing=not final)
                self.symbols[name.lower()] = v
                self.symbol_names[name.lower()] = name
            elif kind == 'dir':
                head, rest, w = it[1], it[2], it[3]
                if head == '.org':
                    pc = self.eval(rest, glob, w)
                    if pc < 0 or pc > 0x1FFFF:
                        raise AsmError('.org address out of range', w)
                elif head in ('.cnop', '.align'):
                    ops = split_operands(rest)
                    if head == '.align':
                        add, mult = 0, self.eval(ops[0], glob, w)
                    else:
                        add, mult = self.eval(ops[0], glob, w), self.eval(ops[1], glob, w)
                    if mult > 0:
                        pc = ((pc + mult - 1) // mult) * mult + add
                else:
                    data = self.data_bytes(head, rest, glob, w, final)
                    if final:
                        self.put(out, pc, data, w)
                        if listing is not None:
                            listing.append((pc, data, w))
                    pc += len(data)
            elif kind == 'ins':
                mn, shapes, ops, w = it[1], it[2], it[3], it[4]
                size = size_of(mn, shapes)
                if final:
                    vals = []
                    for shape, op in zip(shapes, ops):
                        if shape == 'ri':
                            vals.append(int(op.strip()[2]))
                        else:
                            text = op.strip()[1:] if shape == 'i8' else op
                            vals.append(self.eval(text, glob, w))
                    data = encode(mn, shapes, vals, pc + size, w)
                    assert len(data) == size, (mn, data)
                    self.put(out, pc, data, w)
                    if listing is not None:
                        listing.append((pc, data, w))
                pc += size
            hi = max(hi, pc)
        return hi

    def put(self, out, pc, data, w):
        end = pc + len(data)
        if end > len(out):
            out.extend(b'\0' * (end - len(out)))
        for i, b in enumerate(data):
            if self.written[pc + i]:
                raise AsmError('code/data overlaps at $%04X (check your .org addresses)' % (pc + i), w)
            self.written[pc + i] = 1
            out[pc + i] = b & 0xFF

    def assemble(self, path):
        self.read(path)
        self.symbol_addr_pass = {}
        # iterate layout until label addresses settle (sizes do not depend on values,
        # so two passes suffice; a third guards equates defined after use)
        for _ in range(3):
            self.layout(final=False)
        out = bytearray()
        self.written = bytearray(0x20000)
        listing = []
        size = self.layout(final=True, out=out, listing=listing)
        if len(out) < size:                      # trailing .cnop/.align padding
            out.extend(b'\0' * (size - len(out)))
        del out[size:]
        self.listing = listing
        return bytes(out)

    def write_listing(self, path, binary):
        with open(path, 'w') as f:
            for pc, data, w in self.listing:
                f.write('%04X  %-12s %s:%d\n' % (pc, ' '.join('%02X' % b for b in data[:4]) +
                                                  ('..' if len(data) > 4 else ''),
                                                  os.path.basename(w[0]), w[1]))
            f.write('\nsymbols:\n')
            for key in sorted(self.symbols, key=lambda k: self.symbols[k]):
                f.write('  %-32s $%04X\n' % (self.symbol_names.get(key, key), self.symbols[key] & 0xFFFF))


def parse_string(tok, where):
    tok = tok.strip()
    if len(tok) < 2 or tok[0] != '"' or tok[-1] != '"':
        raise AsmError('expected a "string": %s' % tok, where)
    body, out, i = tok[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == '\\' and i + 1 < len(body):
            i += 1
            c = {'n': '\n', 't': '\t', '0': '\0', '"': '"', '\\': '\\'}.get(body[i], body[i])
        out += c.encode('latin-1', errors='replace')
        i += 1
    return bytes(out)


def assemble_file(path, include_dirs=(), listing=None):
    a = Assembler(include_dirs)
    binary = a.assemble(path)
    if listing:
        a.write_listing(listing, binary)
    return binary, a


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='LC86K (Dreamcast VMU) assembler')
    ap.add_argument('source')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('-I', '--include', action='append', default=[])
    ap.add_argument('--listing')
    args = ap.parse_args(argv)
    try:
        binary, _ = assemble_file(args.source, args.include, args.listing)
    except AsmError as e:
        print('error: %s' % e, file=sys.stderr)
        return 1
    open(args.output, 'wb').write(binary)
    print('%s: %d bytes' % (args.output, len(binary)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
