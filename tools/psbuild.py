#!/usr/bin/env python3
# SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
"""
psbuild - PocketSim DevKit build tool

  psbuild build  [project_dir] [--debug] [--clean]   compile + pack
  psbuild run    [project_dir] [sim options]          build, then open in pocketsim
  psbuild new    <project_dir> [--title "My Game"]    create a project from the template
  psbuild info   <file.bin|.mcs|.mcd>                 show what's inside a file / card
  psbuild install <file.mcs|.bin> <card.mcd> [--name FILENAME]
                                                       put a program onto a card image
  psbuild img2c  <image.png|.txt> <symbol> [-o out.h] 32x32 image -> C array
  psbuild doctor                                      check your setup
  psbuild setup                                       remember the kit folder (Windows)

Outputs (in <project>/build/):
  <name>.bin   raw file starting with "SC"  (emulators, raw save importers)
  <name>.mcs   single save                  (MemcardRex / MemCARDuino / PS2 tools)
  <name>.mcd   128 KB card with only this file (PCSX-Redux / DuckStation / card writers)
  <name>.elf / .map                         (debugging)

Only needs Python 3.8+ and the Arm GNU Toolchain (arm-none-eabi-gcc).
"""
import argparse
import configparser
import glob
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psformat as pf  # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.path.join(KIT, 'sdk')
IS_WIN = os.name == 'nt'

DEFAULTS = {
    'name': 'game',
    'title': 'PocketStation Game',
    'filename': '',
    'region': 'BI',
    'product': 'SCPS-P0000',
    'icon': 'icon.txt',
    'icon_delay': '6',
    'psx_icon': 'auto',
    'sources': 'src/*.c src/*.cpp src/*.S src/*.s',
    'include': '',
    'cflags': '-O2',
    'ldflags': '',
    'debug': '0',
    'stack': '0x200',
    'max_blocks': '15',
}


def die(msg):
    print('error: ' + msg, file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------ toolchain
def find_gcc():
    exe = 'arm-none-eabi-gcc' + ('.exe' if IS_WIN else '')
    cands = []
    if os.environ.get('PS_TOOLCHAIN'):
        cands.append(os.path.join(os.environ['PS_TOOLCHAIN'], 'bin', exe))
        cands.append(os.path.join(os.environ['PS_TOOLCHAIN'], exe))
    cands.append(os.path.join(KIT, 'toolchain', 'bin', exe))
    cands += sorted(glob.glob(os.path.join(KIT, 'toolchain', '*', 'bin', exe)), reverse=True)
    w = shutil.which('arm-none-eabi-gcc')
    if w:
        cands.append(w)
    if IS_WIN:
        for base in (os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     os.environ.get('ProgramFiles', r'C:\Program Files')):
            cands += sorted(glob.glob(os.path.join(base, 'Arm GNU Toolchain arm-none-eabi', '*', 'bin', exe)), reverse=True)
            cands += sorted(glob.glob(os.path.join(base, 'Arm', 'GNU Toolchain*', '*', 'bin', exe)), reverse=True)
            cands += sorted(glob.glob(os.path.join(base, 'GNU Arm Embedded Toolchain', '*', 'bin', exe)), reverse=True)
        cands += sorted(glob.glob(os.path.join(os.environ.get('APPDATA', ''), 'xPacks', '**', 'bin', exe), recursive=True), reverse=True)
        # devkitPro's devkitARM is an arm-none-eabi GCC too
        for var in ('DEVKITARM', 'DEVKITPRO'):
            v = os.environ.get(var, '')
            if v.startswith('/opt/devkitpro'):        # msys-style path set by the installer
                v = 'C:\\devkitPro' + v[len('/opt/devkitpro'):].replace('/', '\\')
            if v:
                cands.append(os.path.join(v, 'bin', exe))
                cands.append(os.path.join(v, 'devkitARM', 'bin', exe))
        cands.append(r'C:\devkitPro\devkitARM\bin' + '\\' + exe)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    die('arm-none-eabi-gcc not found.\n'
        '  Install the Arm GNU Toolchain (arm-none-eabi) and add its bin folder to PATH,\n'
        '  or set PS_TOOLCHAIN to the toolchain folder, or unpack it into %s' % os.path.join(KIT, 'toolchain'))


def tool(gcc, name):
    """arm-none-eabi-gcc[.exe] -> arm-none-eabi-<name>[.exe] in the same folder."""
    d, base = os.path.split(gcc)
    ext = '.exe' if base.lower().endswith('.exe') else ''
    return os.path.join(d, base[:len(base) - len(ext) - 3] + name + ext)


VERBOSE = False


def run(cmd, quiet=False, label=None):
    if VERBOSE:
        print('  ' + ' '.join(cmd))
    elif label and not quiet:
        print('  ' + label)
    r = subprocess.run(cmd)
    if r.returncode:
        die('command failed (%d)' % r.returncode)


# ------------------------------------------------------------------ project
def load_project(pdir):
    ini = os.path.join(pdir, 'project.ini')
    if not os.path.isfile(ini):
        die('%s not found (create one with: psbuild new %s)' % (ini, pdir))
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cp.read(ini, encoding='utf-8')
    sec = cp['project'] if cp.has_section('project') else {}
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in sec:
            cfg[k] = sec[k].strip()
    if not cfg['filename']:
        ident = ''.join(c for c in cfg['name'].upper() if c.isalnum())[:8]
        cfg['filename'] = (cfg['region'] + cfg['product'] + ident)[:20]
    fn = cfg['filename']
    if len(fn) > 20 or not fn.isascii():
        die('filename must be at most 20 ASCII characters: %r' % fn)
    if len(fn) < 8 or fn[7] != 'P':
        print('warning: filename %r has no "P" at position 7; the PS1/PS2 card manager will '
              'not treat it as a PocketStation application (e.g. BISCPS-P0000NAME)' % fn)
    return cfg


def gen_header(cfg, pdir, out_s):
    """Write the title sector + icons as an assembly file."""
    frames = [pf.load_mono(os.path.join(pdir, p.strip())) for p in cfg['icon'].split(',') if p.strip()]
    if not frames:
        die('project.ini: icon must name at least one 32x32 image')
    if cfg['psx_icon'] == 'auto':
        clut, psx_pix = pf.psx_icon_from_mono(frames[0])
    else:
        clut, psx_pix = pf.psx_icon_from_png(os.path.join(pdir, cfg['psx_icon']))
    title = pf.sjis_fullwidth(cfg['title'])
    delay = int(cfg['icon_delay'], 0)

    def bytes_dir(b):
        return '\n'.join('    .byte ' + ','.join('0x%02X' % x for x in b[i:i + 16])
                         for i in range(0, len(b), 16))

    L = []
    L.append('/* generated by psbuild from project.ini - do not edit */')
    L.append('    .section .ps_header, "a"')
    L.append('    .global ps__header')
    L.append('ps__header:')
    L.append('    .ascii "SC"')
    L.append('    .byte 0x11            /* PS1 icon: 1 static frame */')
    L.append('    .byte 1               /* blocks used: patched after link */')
    L.append('/* title (Shift-JIS) */')
    L.append(bytes_dir(title))
    L.append('    .space 12             /* 0x44-0x4F reserved */')
    L.append('    .hword %d              /* 0x50 file viewer mono icon frames */' % len(frames))
    L.append('    .ascii "MCX0"         /* 0x52 PocketStation executable */')
    L.append('    .byte 1               /* 0x56 executable icon list entries */')
    L.append('    .byte 0               /* 0x57 function table entries */')
    L.append('    .word 0               /* 0x58 reserved / function table */')
    L.append('    .word _start          /* 0x5C entry point */')
    L.append('/* 0x60 PS1 icon palette */')
    L.append('    .hword ' + ','.join('0x%04X' % c for c in clut))
    L.append('/* 0x80 PS1 icon 16x16x4bpp */')
    L.append(bytes_dir(psx_pix))
    L.append('/* 0x100 viewer mono icons */')
    L.append('ps__mono_icons:')
    for fr in frames:
        L.append('    .word ' + ','.join('0x%08X' % r for r in fr))
    L.append('/* executable mono icon list: frames, delay, pointer */')
    L.append('    .hword %d, %d' % (len(frames), delay))
    L.append('    .word ps__mono_icons')
    L.append('    .balign 128, 0')
    open(out_s, 'w').write('\n'.join(L) + '\n')


def collect_sources(cfg, pdir):
    srcs = []
    for pat in cfg['sources'].split():
        srcs += sorted(glob.glob(os.path.join(pdir, pat)))
    if not srcs:
        die('no sources match %r' % cfg['sources'])
    return srcs


def build(pdir, debug=None, clean=False, quiet=False):
    pdir = os.path.abspath(pdir)
    cfg = load_project(pdir)
    if debug is not None:
        cfg['debug'] = '1' if debug else '0'
    bdir = os.path.join(pdir, 'build')
    if clean and os.path.isdir(bdir):
        shutil.rmtree(bdir)
    odir = os.path.join(bdir, 'obj')
    os.makedirs(odir, exist_ok=True)
    gcc = find_gcc()
    objcopy = tool(gcc, 'objcopy')
    name = cfg['name']
    print('== %s  (%s)' % (cfg['title'], cfg['filename']))

    hdr_s = os.path.join(odir, 'ps_header.S')
    gen_header(cfg, pdir, hdr_s)

    arch = ['-mcpu=arm7tdmi', '-mthumb', '-mthumb-interwork', '-mfloat-abi=soft']
    cflags = arch + ['-ffreestanding', '-fno-builtin-printf', '-fno-jump-tables',
                     '-fno-tree-loop-distribute-patterns', '-fno-common',
                     '-ffunction-sections', '-fdata-sections', '-Wall', '-Wextra',
                     '-Wno-unused-parameter', '-g', '--param=min-pagesize=0',
                     '-I' + os.path.join(SDK, 'include'), '-I' + os.path.join(pdir, 'src')]
    for inc in cfg['include'].split():
        cflags.append('-I' + os.path.join(pdir, inc))
    if cfg['debug'] == '1':
        cflags.append('-DPS_DEBUG')
    cflags += cfg['cflags'].split()
    cxxflags = ['-fno-exceptions', '-fno-rtti', '-fno-threadsafe-statics']

    sdk_srcs = sorted(glob.glob(os.path.join(SDK, 'src', '*.c')) + glob.glob(os.path.join(SDK, 'src', '*.S')))
    all_srcs = [(s, 'sdk_') for s in sdk_srcs] + [(s, '') for s in collect_sources(cfg, pdir)] + [(hdr_s, '')]
    objs = []
    for src, pre in all_srcs:
        obj = os.path.join(odir, pre + os.path.splitext(os.path.basename(src))[0] + '.o')
        if obj in objs:
            die('two sources compile to the same object name: %s' % obj)
        cmd = [gcc] + cflags + (cxxflags if src.endswith(('.cpp', '.cc')) else []) + ['-c', src, '-o', obj]
        if src.endswith(('.S', '.s')):
            cmd = [gcc] + arch + ['-g', '-I' + os.path.join(SDK, 'include'), '-c', src, '-o', obj]
        run(cmd, quiet, 'cc   ' + os.path.relpath(src, KIT if pre else pdir))
        objs.append(obj)

    elf = os.path.join(bdir, name + '.elf')
    ld = [gcc] + arch + ['-nostartfiles', '-nostdlib', '-Wl,--gc-sections', '-Wl,--no-warn-rwx-segments',
                         '-Wl,--defsym=PS_STACK_SIZE=%s' % cfg['stack'],
                         '-Wl,--defsym=PS_MAX_BLOCKS=%s' % cfg['max_blocks'],
                         '-T', os.path.join(SDK, 'ld', 'pocketstation.ld'),
                         '-Wl,-Map=' + os.path.join(bdir, name + '.map'), '-o', elf] + objs + ['-lgcc'] + cfg['ldflags'].split()
    run(ld, quiet, 'link ' + name + '.elf')
    raw_path = os.path.join(odir, name + '.raw')
    run([objcopy, '-O', 'binary', elf, raw_path], quiet)
    data = bytearray(open(raw_path, 'rb').read())
    blocks = (len(data) + pf.BLOCK - 1) // pf.BLOCK
    data = data.ljust(blocks * pf.BLOCK, b'\0')
    data[3] = blocks
    if blocks > int(cfg['max_blocks']):
        die('program needs %d blocks, max_blocks is %s' % (blocks, cfg['max_blocks']))

    bin_path = os.path.join(bdir, name + '.bin')
    open(bin_path, 'wb').write(data)
    open(os.path.join(bdir, name + '.mcs'), 'wb').write(pf.make_mcs(cfg['filename'], bytes(data)))
    card = pf.blank_card()
    pf.card_install(card, cfg['filename'], bytes(data))
    open(os.path.join(bdir, name + '.mcd'), 'wb').write(card)

    # size report from the map symbols
    syms = read_symbols(tool(gcc, 'nm'), elf)
    flash_used = syms.get('__flash_end', 0x02000000 + len(data)) - 0x02000000
    ram_used = syms.get('__bss_end', 0x200) - 0x200
    print('   flash %6d bytes  (%d block%s of 8 KB, save area 512 bytes)' % (flash_used, blocks, 's' if blocks > 1 else ''))
    print('   ram   %6d bytes  of 1536 (+ stack %d)' % (ram_used, int(cfg['stack'], 0)))
    print('   -> build/%s.bin  build/%s.mcs  build/%s.mcd' % (name, name, name))
    return os.path.join(bdir, name), cfg


def read_symbols(nm, elf):
    out = subprocess.run([nm, elf], capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                syms[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    return syms


# ------------------------------------------------------------------ commands
TEMPLATE_MAIN = r'''#include <ps/ps.h>

int main(void)
{
    ps_init();
    int x = 14, y = 20;

    for (;;) {
        ps_input_update();
        if (ps_held(PS_BTN_LEFT)  && x > 0)  x--;
        if (ps_held(PS_BTN_RIGHT) && x < 28) x++;
        if (ps_held(PS_BTN_UP)    && y > 8)  y--;
        if (ps_held(PS_BTN_DOWN)  && y < 28) y++;
        if (ps_pressed(PS_BTN_FIRE)) ps_beep(880, 3);

        /* hold FIRE + UP for one second to quit */
        static int quit;
        quit = (ps_held(PS_BTN_FIRE) && ps_held(PS_BTN_UP)) ? quit + 1 : 0;
        if (quit > PS_FPS) break;

        ps_clear();
        ps_text(1, 1, "%TITLE%");
        ps_fill_rect(x, y, 4, 4, 1);
        ps_present();
        ps_wait_frame();
    }
    return 0;   /* back to the PocketStation menu */
}
'''

TEMPLATE_ICON = '\n'.join([
    '................................',
    '................................',
    '....########################....',
    '...#........................#...',
    '..#..######################..#..',
    '..#.#......................#.#..',
    '..#.#......................#.#..',
    '..#.#....##..........##....#.#..',
    '..#.#....##..........##....#.#..',
    '..#.#......................#.#..',
    '..#.#......................#.#..',
    '..#.#....#.............#...#.#..',
    '..#.#.....#...........#....#.#..',
    '..#.#......###########.....#.#..',
    '..#.#......................#.#..',
    '..#..######################..#..',
    '..#..........................#..',
    '..#.......#..........##......#..',
    '..#.......#.........#..#.....#..',
    '..#.....#####........##......#..',
    '..#.......#.......##.........#..',
    '..#.......#......#..#........#..',
    '..#...............##.........#..',
    '..#..........................#..',
    '..#..........................#..',
    '..#.........#......#.........#..',
    '..#..........#......#........#..',
    '..#..........................#..',
    '...#........................#...',
    '....########################....',
    '................................',
    '................................',
])


def cmd_new(args):
    pdir = os.path.abspath(args.dir)
    if os.path.exists(os.path.join(pdir, 'project.ini')):
        die('%s already has a project.ini' % pdir)
    os.makedirs(os.path.join(pdir, 'src'), exist_ok=True)
    name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in os.path.basename(pdir.rstrip('/\\'))) or 'game'
    ident = ''.join(c for c in name.upper() if c.isalnum())[:8] or 'GAME'
    title = args.title or name
    short = ''.join(c for c in title.upper() if c.isalnum() or c == ' ')[:7].strip() or 'HELLO'
    open(os.path.join(pdir, 'project.ini'), 'w').write(
        '[project]\n'
        'name      = %s\n'
        'title     = %s            ; shown in the PS1 memory card screen (max 31 chars)\n'
        'filename  = BISCPS-P0000%s ; memory card file name; keep the "P" at position 7\n'
        'icon      = icon.txt         ; 32x32 mono icon(s), .txt art or .png, comma separated for animation\n'
        'icon_delay = 6               ; animation delay in 1/30 s\n'
        'psx_icon  = auto             ; or a 16x16 PNG (max 16 colours)\n'
        'sources   = src/*.c\n'
        'cflags    = -O2\n'
        'debug     = 0                ; 1 = enable ps_log() output\n' % (name, title, ident))
    open(os.path.join(pdir, 'icon.txt'), 'w').write(TEMPLATE_ICON + '\n')
    open(os.path.join(pdir, 'src', 'main.c'), 'w').write(TEMPLATE_MAIN.replace('%TITLE%', short))
    write_bats(pdir)
    print('created %s  (edit src\\main.c, then run build.bat or: psbuild run %s)' % (pdir, args.dir))


BAT = r"""@echo off
rem {what} this PocketStation project (generated by psbuild new)
setlocal
set "KIT=%POCKETSIM_DEVKIT%"
if "%KIT%"=="" set "KIT={kit}"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% "%KIT%\tools\psbuild.py" {cmd} "%~dp0." %*
if errorlevel 1 pause
"""


def write_bats(pdir):
    kit = KIT if not IS_WIN else os.path.normpath(KIT)
    for fname, what, cmd in (('build.bat', 'Build', 'build'), ('run.bat', 'Build and run in pocketsim', 'run')):
        with open(os.path.join(pdir, fname), 'w', newline='\r\n') as f:
            f.write(BAT.format(what=what, kit=kit, cmd=cmd))


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def user_env(name):
    """Value saved in the user's Windows environment (registry), or None.
    That is what new terminals will see, unlike os.environ of this old one."""
    if not IS_WIN:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def cmd_setup(args):
    """Save POCKETSIM_DEVKIT for new terminals and remove the old PSKIT variable."""
    if not IS_WIN:
        print('ps setup is for Windows. Elsewhere add this to your shell profile:')
        print('  export POCKETSIM_DEVKIT="%s"' % KIT)
        return
    import winreg
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
        winreg.SetValueEx(k, 'POCKETSIM_DEVKIT', 0, winreg.REG_SZ, KIT)
        try:
            winreg.DeleteValue(k, 'PSKIT')
            print('removed old PSKIT variable')
        except OSError:
            pass
    # tell Windows (Explorer, new terminals) that the environment changed
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x0002, 2000, None)
    os.environ['POCKETSIM_DEVKIT'] = KIT
    os.environ.pop('PSKIT', None)
    print('POCKETSIM_DEVKIT = %s' % KIT)
    print('saved - open a new terminal window to use it there\n')
    cmd_doctor(args)


def cmd_doctor(args):
    ok = True
    print('python    : %s (%s)' % (sys.version.split()[0], sys.executable))
    if sys.version_info < (3, 8):
        print('            ! Python 3.8 or newer is required'); ok = False
    try:
        import tkinter  # noqa: F401
        print('tkinter   : ok (pocketsim window available)')
    except ImportError:
        print('tkinter   : missing - pocketsim works only with --headless (reinstall Python with "tcl/tk")')
    try:
        gcc = find_gcc()
        ver = subprocess.run([gcc, '--version'], capture_output=True, text=True).stdout.splitlines()[0]
        print('compiler  : %s\n            %s' % (gcc, ver))
        for t in ('objcopy', 'nm'):
            if not os.path.isfile(tool(gcc, t)):
                print('            ! %s missing next to gcc' % t); ok = False
    except SystemExit:
        ok = False
    print('devkit    : %s' % KIT)
    saved, saved_old = user_env('POCKETSIM_DEVKIT'), user_env('PSKIT')
    env = saved if saved is not None else os.environ.get('POCKETSIM_DEVKIT')
    old = saved_old if IS_WIN else os.environ.get('PSKIT')
    if not env:
        print('            ! POCKETSIM_DEVKIT is not set: run "ps setup" (or setup.bat)')
        ok = False
    elif not same_path(env, KIT):
        print('            ! POCKETSIM_DEVKIT points to %s' % env)
        print('              run "ps setup" in this folder to update it')
        ok = False
    if old:
        print('            ! old PSKIT variable found (%s): run "ps setup" to remove it' % old)
        ok = False
    if ok and IS_WIN and not same_path(os.environ.get('POCKETSIM_DEVKIT') or '', KIT):
        print('            (saved correctly; this terminal is older than the change,')
        print('             new terminal windows will see it)')
    print('\n' + ('all good - try:  ps run examples\\snake' if ok else 'fix the items above, then run "ps doctor" again'))
    sys.exit(0 if ok else 1)


def cmd_build(args):
    build(args.dir, debug=True if args.debug else None, clean=args.clean)


def cmd_run(args, extra):
    base, cfg = build(args.dir, debug=True if args.debug else None, clean=args.clean)
    sim = os.path.join(KIT, 'tools', 'pocketsim.py')
    cmd = [sys.executable, sim, base + '.bin'] + extra
    print('== pocketsim ' + ' '.join(extra))
    sys.exit(subprocess.run(cmd).returncode)


def cmd_info(args):
    raw = open(args.file, 'rb').read()
    if raw[:2] == b'MC' and len(raw) == pf.CARD_SIZE:
        print('%s: 128 KB memory card image' % args.file)
        for idx, fn, size, blocks in pf.card_files(raw):
            data = pf.card_read_file(raw, blocks, size)
            try:
                h = pf.parse_title_sector(data)
                kind = 'PocketStation app (%s, entry 0x%08X)' % (h['mcx_id'], h['entry']) if h['is_pocketstation'] else 'save'
                title = h['title']
            except ValueError:
                kind, title = 'data', ''
            print('  [%2d] %-20s %3d blocks  %-24s %s' % (idx, fn, len(blocks), title, kind))
        return
    fn, data = pf.read_any(args.file)
    h = pf.parse_title_sector(data)
    print('file      : %s%s' % (args.file, ('  (card name %s)' % fn) if fn else ''))
    print('title     : %s' % h['title'])
    print('size      : %d bytes, header says %d block(s)' % (len(data), h['blocks']))
    print('type      : %s' % ('PocketStation executable ' + h['mcx_id'] if h['is_pocketstation'] else 'plain PS1 save'))
    if h['is_pocketstation']:
        print('entry     : 0x%08X' % h['entry'])
        print('mono icons: %d viewer frame(s), %d exec icon list entr%s' % (
            h['viewer_frames'], h['exec_icons'], 'y' if h['exec_icons'] == 1 else 'ies'))
        if fn and (len(fn) < 8 or fn[7] != 'P'):
            print('warning   : card file name lacks "P" at position 7')
    if h['viewer_frames']:
        rows = struct.unpack_from('<32I', data, 0x100)
        for r in rows:
            print('   ' + ''.join('#' if (r >> x) & 1 else '.' for x in range(32)))


def cmd_install(args):
    fn, data = pf.read_any(args.file)
    name = args.name or fn
    if not name:
        die('--name FILENAME is required when installing a .bin (e.g. BISCPS-P0000GAME)')
    card = bytearray(open(args.card, 'rb').read()) if os.path.exists(args.card) else pf.blank_card()
    if len(card) != pf.CARD_SIZE or card[:2] != b'MC':
        die('%s is not a 128 KB raw memory card image' % args.card)
    idx = pf.card_install(card, name, data)
    open(args.card, 'wb').write(card)
    print('installed %s as %s in block %d of %s' % (args.file, name, idx, args.card))


def cmd_img2c(args):
    rows = pf.load_mono(args.image)
    sym = args.symbol
    lines = ['/* generated by psbuild img2c from %s */' % os.path.basename(args.image),
             '#include <stdint.h>',
             'static const uint32_t %s[32] = {' % sym]
    for r in rows:
        art = ''.join('#' if (r >> x) & 1 else '.' for x in range(32))
        lines.append('    0x%08Xu, /* %s */' % (r, art))
    lines.append('};')
    text = '\n'.join(lines) + '\n'
    if args.o:
        open(args.o, 'w').write(text)
        print('wrote ' + args.o)
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(prog='psbuild', description='PocketSim DevKit build tool')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('build'); p.add_argument('dir', nargs='?', default='.')
    p.add_argument('--debug', action='store_true', help='enable ps_log() (PS_DEBUG)')
    p.add_argument('--clean', action='store_true')
    p = sub.add_parser('run', help='build then start pocketsim (extra args go to pocketsim)')
    p.add_argument('dir', nargs='?', default='.')
    p.add_argument('--debug', action='store_true'); p.add_argument('--clean', action='store_true')
    p = sub.add_parser('new'); p.add_argument('dir'); p.add_argument('--title')
    p = sub.add_parser('info'); p.add_argument('file')
    p = sub.add_parser('install'); p.add_argument('file'); p.add_argument('card'); p.add_argument('--name')
    sub.add_parser('doctor', help='check Python, toolchain and simulator setup')
    sub.add_parser('setup', help='remember where the kit is (Windows) and run doctor')
    p = sub.add_parser('img2c'); p.add_argument('image'); p.add_argument('symbol'); p.add_argument('-o')
    ap.add_argument('-v', '--verbose', action='store_true', help='show full compiler commands')
    args, extra = ap.parse_known_args()
    global VERBOSE
    VERBOSE = args.verbose
    if args.cmd == 'run':
        cmd_run(args, extra)
    elif extra:
        ap.error('unrecognized arguments: ' + ' '.join(extra))
    else:
        {'build': cmd_build, 'new': cmd_new, 'doctor': cmd_doctor, 'setup': cmd_setup, 'info': cmd_info,
         'install': cmd_install, 'img2c': cmd_img2c}[args.cmd](args)


if __name__ == '__main__':
    main()
