# SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
"""
psformat.py - PocketStation / PS1 memory card file formats (no dependencies).

  * title sector + PocketStation fields (MCX0)
  * raw .bin  - the file itself, starting with "SC" (raw save format)
  * .mcs      - 128-byte directory frame + file (MemcardRex, MemCARDuino, PS2 tools)
  * .mcd/.mcr - full 128 KB card image (emulators, card writers)
  * minimal PNG reader and text-art reader for 32x32 mono / 16x16 colour icons
"""
import struct
import zlib

BLOCK = 0x2000
FRAME = 0x80
CARD_SIZE = 0x20000

# ---------------------------------------------------------------- helpers
def xor_checksum(frame):
    c = 0
    for b in frame[:0x7F]:
        c ^= b
    return c


def sjis_fullwidth(text, max_bytes=64):
    """ASCII title -> Shift-JIS full-width, as the PS1 memory card manager expects."""
    out = bytearray()
    for ch in text:
        o = ord(ch)
        if ch == ' ':
            code = 0x8140
        elif '0' <= ch <= '9':
            code = 0x824F + (o - 0x30)
        elif 'A' <= ch <= 'Z':
            code = 0x8260 + (o - 0x41)
        elif 'a' <= ch <= 'z':
            code = 0x8281 + (o - 0x61)
        else:
            table = {'!': 0x8149, '"': 0x8168, '#': 0x8194, '$': 0x8190, '%': 0x8193,
                     '&': 0x8195, "'": 0x8166, '(': 0x8169, ')': 0x816A, '*': 0x8196,
                     '+': 0x817B, ',': 0x8143, '-': 0x817C, '.': 0x8144, '/': 0x815E,
                     ':': 0x8146, ';': 0x8147, '<': 0x8183, '=': 0x8181, '>': 0x8184,
                     '?': 0x8148, '@': 0x8197, '[': 0x816D, ']': 0x816E, '_': 0x8151}
            code = table.get(ch, 0x8148)
        out += struct.pack('>H', code)
    if len(out) > max_bytes - 2:
        raise ValueError('title too long: max %d characters' % ((max_bytes - 2) // 2))
    return bytes(out).ljust(max_bytes, b'\0')


def sjis_to_ascii(raw):
    """Best-effort decode of a title for display."""
    raw = raw.split(b'\0\0')[0]
    try:
        s = raw.decode('shift_jis', errors='replace')
    except LookupError:
        return raw.hex()
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(' ')
        elif ch == '\0':
            continue
        else:
            out.append(ch)
    return ''.join(out)

# ---------------------------------------------------------------- images
def read_png(path):
    """Return (width, height, rows) with rows of (r,g,b,a) tuples.
    Supports non-interlaced PNGs: greyscale, RGB, palette, grey+alpha, RGBA,
    bit depths 1/2/4/8 (16-bit is reduced to 8)."""
    data = open(path, 'rb').read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('%s: not a PNG' % path)
    pos, idat, plte, trns = 8, b'', None, None
    while pos < len(data):
        ln, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b'IHDR':
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE':
            plte = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IDAT':
            idat += body
        elif typ == b'IEND':
            break
    if interlace:
        raise ValueError('%s: interlaced PNG not supported' % path)
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, chans * depth // 8)
    stride = (w * chans * depth + 7) // 8
    raw = zlib.decompress(idat)
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(h):
        f = raw[i]; line = bytearray(raw[i + 1:i + 1 + stride]); i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        prev = line
        # unpack samples
        if depth < 8:
            samples = []
            for byte in line:
                for k in range(8 // depth):
                    samples.append((byte >> (8 - depth * (k + 1))) & ((1 << depth) - 1))
            samples = samples[:w * chans]
        elif depth == 16:
            samples = [line[k] for k in range(0, len(line), 2)]
        else:
            samples = list(line)
        px = []
        for x in range(w):
            s = samples[x * chans:(x + 1) * chans]
            if ctype == 3:
                r, g, b = plte[s[0]]
                a = trns[s[0]] if trns and s[0] < len(trns) else 255
            elif ctype == 0:
                v = s[0] * 255 // ((1 << depth) - 1) if depth < 8 else s[0]
                r = g = b = v; a = 255
            elif ctype == 4:
                r = g = b = s[0]; a = s[1]
            elif ctype == 2:
                r, g, b = s; a = 255
            else:
                r, g, b, a = s
            px.append((r, g, b, a))
        rows.append(px)
    return w, h, rows


def write_png(path, width, height, pixels_rgb):
    """pixels_rgb: list of rows of (r,g,b)."""
    raw = b''.join(b'\0' + bytes(c for p in row for c in p) for row in pixels_rgb)
    def chunk(t, b):
        return struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xFFFFFFFF)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


def load_mono(path, size=32):
    """Load a size x size mono image -> list of row ints (bit0 = left, 1 = black).
    .txt: one line per row, '#','X','x','1','@' = black. .png: dark pixels = black."""
    rows = []
    if path.lower().endswith('.png'):
        w, h, px = read_png(path)
        if (w, h) != (size, size):
            raise ValueError('%s: must be %dx%d, is %dx%d' % (path, size, size, w, h))
        for y in range(size):
            v = 0
            for x in range(size):
                r, g, b, a = px[y][x]
                if a >= 128 and (r * 299 + g * 587 + b * 114) < 128000:
                    v |= 1 << x
            rows.append(v)
    else:
        lines = [l.rstrip('\r\n') for l in open(path, encoding='utf-8')]
        lines = [l for l in lines if not l.startswith(';')]
        if len(lines) < size:
            lines += [''] * (size - len(lines))
        for y in range(size):
            v = 0
            for x, ch in enumerate(lines[y][:size]):
                if ch in '#Xx1@':
                    v |= 1 << x
            rows.append(v)
    return rows


def load_frames(spec, size=32):
    """Comma separated list of files -> list of frames."""
    return [load_mono(p.strip(), size) for p in spec.split(',') if p.strip()]


def mono_to_bytes(rows):
    return b''.join(struct.pack('<I', r) for r in rows)


def psx_icon_from_mono(rows32):
    """Make the 16x16 16-colour PS1 menu icon by 2x2 downscaling the mono icon."""
    clut = [0x7FFF, 0x8000] + [0] * 14            # white bg, opaque black
    pix = bytearray(128)
    for y in range(16):
        for x in range(16):
            blk = 0
            for dy in (0, 1):
                for dx in (0, 1):
                    blk += (rows32[2 * y + dy] >> (2 * x + dx)) & 1
            idx = 1 if blk >= 2 else 0
            o = y * 8 + x // 2
            pix[o] |= idx << (4 * (x & 1))
    return clut, bytes(pix)


def psx_icon_from_png(path):
    w, h, px = read_png(path)
    if (w, h) != (16, 16):
        raise ValueError('%s: PS1 icon must be 16x16' % path)
    colours = []
    idx = []
    for y in range(16):
        for x in range(16):
            r, g, b, a = px[y][x]
            c = 0 if a < 128 else (0x8000 | (b >> 3) << 10 | (g >> 3) << 5 | (r >> 3))
            if c not in colours:
                if len(colours) == 16:
                    raise ValueError('%s: more than 16 colours' % path)
                colours.append(c)
            idx.append(colours.index(c))
    pix = bytearray(128)
    for i, v in enumerate(idx):
        pix[i // 2] |= v << (4 * (i & 1))
    return colours + [0] * (16 - len(colours)), bytes(pix)

# ---------------------------------------------------------------- headers
def parse_title_sector(sec):
    """Decode the first 0x80 bytes of a file."""
    if sec[:2] != b'SC':
        raise ValueError('not a memory card file (no "SC" magic)')
    info = {
        'icon_flag': sec[2],
        'blocks': sec[3],
        'title': sjis_to_ascii(sec[4:0x44]),
        'viewer_frames': struct.unpack_from('<H', sec, 0x50)[0],
        'mcx_id': sec[0x52:0x56].decode('ascii', 'replace'),
        'exec_icons': sec[0x56],
        'func_count': sec[0x57],
        'entry': struct.unpack_from('<I', sec, 0x5C)[0],
    }
    info['is_pocketstation'] = info['mcx_id'] in ('MCX0', 'MCX1')
    return info

# ---------------------------------------------------------------- directory frames
def dir_frame(state, size, link, filename=b''):
    f = bytearray(FRAME)
    struct.pack_into('<IIH', f, 0, state, size, link)
    f[0x0A:0x0A + len(filename)] = filename
    f[0x7F] = xor_checksum(f)
    return bytes(f)


def make_mcs(filename, data):
    fn = filename.encode('ascii')
    if len(fn) > 20:
        raise ValueError('filename longer than 20 characters')
    return dir_frame(0x51, len(data), 0xFFFF, fn) + data


def read_any(path):
    """Return (filename or None, file bytes) from .bin/.mcs/.psx-ish input."""
    raw = open(path, 'rb').read()
    if raw[:2] == b'SC':
        return None, raw
    if len(raw) >= FRAME + 2 and raw[0] == 0x51 and raw[FRAME:FRAME + 2] == b'SC':
        fn = raw[0x0A:0x1E].split(b'\0')[0].decode('ascii', 'replace')
        size = struct.unpack_from('<I', raw, 4)[0]
        return fn, raw[FRAME:FRAME + size]
    raise ValueError('%s: unrecognised file (expected .bin starting with SC, or .mcs)' % path)


def blank_card():
    card = bytearray(CARD_SIZE)
    hdr = bytearray(FRAME); hdr[0:2] = b'MC'; hdr[0x7F] = xor_checksum(hdr)
    card[0:FRAME] = hdr
    for i in range(1, 16):
        card[i * FRAME:(i + 1) * FRAME] = dir_frame(0xA0, 0, 0xFFFF)
    for i in range(16, 36):
        f = bytearray(FRAME)
        struct.pack_into('<IIH', f, 0, 0xFFFFFFFF, 0, 0xFFFF)
        f[0x7F] = xor_checksum(f)
        card[i * FRAME:(i + 1) * FRAME] = f
    card[63 * FRAME:64 * FRAME] = hdr
    return card


def card_files(card):
    """List (dir_index, filename, size, blocks[]) for files on a card image."""
    out = []
    for i in range(1, 16):
        f = card[i * FRAME:(i + 1) * FRAME]
        state, size, link = struct.unpack_from('<IIH', f, 0)
        if state != 0x51:
            continue
        fn = f[0x0A:0x1E].split(b'\0')[0].decode('ascii', 'replace')
        blocks, b, guard = [i], link, 0
        while b != 0xFFFF and guard < 15:
            blocks.append(b + 1)
            b = struct.unpack_from('<H', card, (b + 1) * FRAME + 8)[0]
            guard += 1
        out.append((i, fn, size, blocks))
    return out


def card_read_file(card, blocks, size):
    return b''.join(bytes(card[b * BLOCK:(b + 1) * BLOCK]) for b in blocks)[:size]


def card_install(card, filename, data, replace=True):
    """Write a file into a card image (bytearray). Returns the dir index used."""
    fn = filename.encode('ascii')
    nblk = (len(data) + BLOCK - 1) // BLOCK
    data = data.ljust(nblk * BLOCK, b'\0')
    if replace:
        for idx, name, size, blocks in card_files(card):
            if name == filename:
                for b in blocks:
                    card[b * FRAME:(b + 1) * FRAME] = dir_frame(0xA0, 0, 0xFFFF)
    free = [i for i in range(1, 16) if card[i * FRAME] in (0xA0, 0xA1, 0xA2, 0xA3)]
    if len(free) < nblk:
        raise ValueError('card full: need %d blocks, %d free' % (nblk, len(free)))
    use = free[:nblk]
    for k, b in enumerate(use):
        last = k == nblk - 1
        state = 0x51 if k == 0 else (0x53 if last else 0x52)
        if k == 0 and nblk == 1:
            state = 0x51
        link = 0xFFFF if last else use[k + 1] - 1
        size = len(data) if k == 0 else 0
        name = fn if k == 0 else b''
        card[b * FRAME:(b + 1) * FRAME] = dir_frame(state, size, link, name)
        card[b * BLOCK:(b + 1) * BLOCK] = data[k * BLOCK:(k + 1) * BLOCK]
    return use[0]
