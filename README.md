# PocketSim DevKit

A homebrew SDK for the PocketStation handheld memory card (SCPH-4000): write games in C,
build them on Windows with one command, test them in the bundled simulator, and put them
on a real unit. It also builds games for the **Dreamcast VMU** (Visual Memory Unit), written
in assembly, with the same `ps build` / `ps run` workflow (see [section 12](#12-dreamcast-vmu-games)).

> PocketSim DevKit is an independent, community-driven open-source project and is not
> affiliated with, authorized, or endorsed by Sony Interactive Entertainment or SEGA.

Free to use, change and share under the [MIT License](LICENSE).

```
pocketsim-devkit/
  LICENSE              MIT license
  setup.bat            one-time setup + health check
  ps.bat               command line: ps build | run | new | info | install | img2c | doctor | setup
  sim.bat              run any .bin / .mcs / .mcd in the simulator
  sdk/
    include/ps/ps.h    the runtime API (graphics, input, sound, saves, time)
    include/ps/hw.h    every hardware register
    include/ps/bios.h  kernel calls (SWI 01h-18h)
    src/               crt0, IRQ hook, drawing, font, sound, save code
    ld/                linker script for the MCX0 file layout
  vmu/
    include/vmu.i      Dreamcast VMU runtime: RAM map, buttons, notes, calls
    include/sfr.i      every VMU register
    lib/vmu.s          VMU runtime: init, frame timer, input, drawing, 3x5 font, buzzer
  tools/
    psbuild.py         build driver, header/icon generator, card image tools
    pocketsim.py       simulator window / headless runner (PocketStation and VMU)
    psformat.py        memory card formats (.bin / .mcs / .mcd), PNG reader
    vmasm.py           LC86K (VMU) assembler
    vmusim.py          VMU simulator core (LC86K CPU, LCD, timers, buzzer)
    vmupack.py         VMU game packaging (.vms / .vmi, header, icon)
  examples/
    hello/             clock, sprites, sound, standby, debug log
    snake/             a full game with a high score saved to flash
    tank_battle/       "Tank Battle": tanks, destructible walls, enemy AI, 3 stages
    summit_climb/      "Summit Climb": vertical platformer, smash ice floors and climb
    balloon_siege/     "Balloon Siege": ride a lift, pop raiders' balloons with arrows
    wing_dash/         "Wing Dash": one-button flyer through gaps in stone pillars
    vmu_snake/         Snake for the Dreamcast VMU, in assembly
```

Everything except the compiler is plain Python 3 with no extra packages. VMU games need no
compiler at all: the assembler is part of the kit.

---

## 1. Setup (Windows)

1. **Python 3.8+**: `winget install Python.Python.3.12`, or the installer from
   python.org with *Add python.exe to PATH* and *tcl/tk* ticked (Tk is the simulator window).
2. **Arm GNU Toolchain** (`arm-none-eabi`): from Arm's *Arm GNU Toolchain Downloads* page,
   take the Windows-hosted installer for the **AArch32 bare-metal target (arm-none-eabi)**
   and tick *Add path to environment variable* on the last page.
   No installer rights? Unzip any `arm-none-eabi` toolchain into `pocketsim-devkit\toolchain\`
   (the kit finds `toolchain\bin` or `toolchain\*\bin`), or set `PS_TOOLCHAIN` to its folder.
3. Double-click **`setup.bat`**. It runs `ps setup`, which saves the kit location in `POCKETSIM_DEVKIT` and runs `ps doctor`,
   which checks Python, Tk and the compiler.

## 2. Quick start

```bat
cd pocketsim-devkit
ps run examples\snake          :: build + open the simulator
ps run examples\tank_battle    :: the tank game
ps run examples\summit_climb   :: the climbing game
ps run examples\balloon_siege  :: the balloon shooter
ps run examples\wing_dash      :: the one-button flyer
ps run examples\vmu_snake      :: Snake on the Dreamcast VMU
ps build examples\hello        :: build only
ps new C:\dev\mygame --title "My Game"
cd C:\dev\mygame
run.bat                        :: every new project gets build.bat and run.bat
```

Simulator keys: **arrows** = d-pad, **Z / X / Space / Enter** = the action button,
**D** = insert/remove from a PlayStation, **M** = mute, **R** = restart, **F12** = screenshot, **Esc** = quit.
On Windows the window plays the game's sound through your speakers (`--mute` to start silent).

**Blank screen after a while?** That is standby, not a crash. The example games switch the
screen off after 20 seconds without input on their title screen, to save the battery on a
real unit. Press any button to wake it; the status bar says so while it sleeps. If you
switched to another window, click the pocketsim window first so it receives your keys.

Each build writes to `<project>\build\`:

| File | What it is | Use it with |
|---|---|---|
| `name.bin` | the raw file, starting with `SC` | emulators and card tools that import raw saves |
| `name.mcs` | 128-byte directory entry + file | MemcardRex, PS1/PS2 save managers |
| `name.mcd` | 128 KB card holding only this program | emulators, card writers |
| `name.elf` / `name.map` | symbols, sizes | debugging, `arm-none-eabi-objdump -d` |

## 3. Your first program

```c
#include <ps/ps.h>

int main(void)
{
    ps_init();                                  /* clock, LCD, 30 fps frame timer */
    int x = 14;
    for (;;) {
        ps_input_update();
        if (ps_held(PS_BTN_LEFT))  x--;
        if (ps_held(PS_BTN_RIGHT)) x++;
        if (ps_pressed(PS_BTN_FIRE)) ps_beep(880, 3);

        ps_clear();
        ps_text(1, 1, "HELLO");
        ps_fill_rect(x, 20, 4, 4, 1);
        ps_present();                           /* framebuffer -> LCD */
        ps_wait_frame();
    }
}
```

Returning from `main()` (or calling `ps_exit()`) goes back to the PocketStation menu with
your program selected.

## 4. project.ini

```ini
[project]
name       = snake                ; output file name
title      = Snake                ; title on the PS1 memory card screen (ASCII, max 31 chars)
filename   = BISCPS-P0002SNAKE    ; card file name, max 20 chars, "P" at position 7
icon       = icon.txt             ; 32x32 mono icon; "a.txt, b.txt" animates
icon_delay = 6                    ; animation speed in 1/30 s
psx_icon   = auto                 ; 16x16 colour icon for the PS1 menu; auto = shrink the mono icon
sources    = src/*.c              ; also .cpp, .S
include    =                      ; extra include folders
cflags     = -O2
debug      = 0                    ; 1 = ps_log() prints in pocketsim
stack      = 0x200                ; bytes reserved for the stack
max_blocks = 15                   ; fail the build if the program grows past this
```

Icons are text art (`#` = black pixel, anything else = white, one line per row) or PNGs.
`ps img2c sprite.png my_sprite -o src/sprite.h` turns a 32x32 image into a C array for
`ps_draw_image()`.

## 5. API summary (`#include <ps/ps.h>`)

| Area | Functions |
|---|---|
| Core | `ps_init`, `ps_wait_frame`, `ps_frame_count`, `ps_is_docked`, `ps_exit`, `ps_standby`, `ps_led`, `ps_set_frame_hook`, `ps_boot_param` |
| Input | `ps_input_update`, `ps_held`, `ps_pressed`, `ps_released`, `ps_idle_frames`; buttons `PS_BTN_FIRE/UP/DOWN/LEFT/RIGHT` |
| Drawing | `ps_clear`, `ps_fill`, `ps_present`, `ps_pset`, `ps_pget`, `ps_pxor`, `ps_hline`, `ps_vline`, `ps_line`, `ps_rect`, `ps_fill_rect`, `ps_invert_rect`, `ps_blit`, `ps_draw_image`; framebuffer `ps_fb[32]` |
| Text | `ps_text`, `ps_number`, `ps_text_width` (3x5 font: digits, A-Z, punctuation) |
| Sound | `ps_tone`, `ps_beep`, `ps_play` (note list), `ps_sound_off`, `ps_sound_busy`; respects the menu's volume setting |
| Saves | `ps_save_read`, `ps_save_write`, `ps_save_ptr` (512 bytes inside your own file) |
| Time / random | `ps_get_time`, `ps_rand`, `ps_rand_range`, `ps_srand`, `ps_bcd2bin` |
| Memory | `ps_flash_u8`, `ps_memcpy`, `ps_memset`, `ps_strlen` (all flash-safe) |
| Debug | `ps_log`, `ps_log_hex` (only with `debug = 1`) |
| Kernel | `bios_*` in `ps/bios.h`: callbacks, CPU speed, flash writes, date/time, docking, execute |
| Registers | `ps/hw.h`: LCD, interrupts, timers, RTC, DAC, IR, COM, clock control |

The framebuffer matches the LCD: `ps_fb[y]` is one row, bit 0 is the leftmost pixel,
1 is black.

## 6. The examples

| Example | Shows | Controls |
|---|---|---|
| `hello` | text, the real-time clock, a sprite, a jingle, standby, `ps_log` | d-pad moves the smiley, FIRE plays a jingle; hold FIRE+DOWN to quit |
| `snake` | a complete game loop, flash high score, sound effects on start / eat / speed-up / death | d-pad steers, FIRE pauses; on the title hold DOWN to quit |
| `tank_battle` | pixel collision against a destructible map, enemy AI, a fixed pool of tanks / bullets / explosions, three stage layouts, a HUD | d-pad drives (the tank turns first, then moves), FIRE shoots; on the title hold DOWN to quit |
| `summit_climb` | fixed-point jump physics, one-way platforms, a scrolling world in a small ring buffer, procedural levels, enemies that change the level | LEFT / RIGHT walk, UP jumps, FIRE swings the hammer; on the title hold DOWN to quit |
| `balloon_siege` | a player on a fixed axis, projectiles in both directions, enemies with several states (float, fall, walk, wait), waves with rising difficulty | UP / DOWN move the lift, FIRE shoots; on the title hold DOWN to quit |
| `wing_dash` | fixed-point gravity, sub-pixel scrolling, recycled obstacles, rectangle collision, a small state machine (title, ready, play, crash, score) | FIRE or UP flaps; on the title hold DOWN to quit |

### Tank Battle (`examples/tank_battle`)

An original top-down tank game made for the 32x32 screen. Destroy every enemy tank on the stage while keeping them away from your base,
the small block at the bottom centre. You lose when all your lives are gone or when the
base is shot. Your own shots can also hit it.

- **HUD**: score on the left, one dot per enemy still to destroy, then your lives.
- **Walls**: solid blocks are bricks, which bullets break two cells at a time. Checkered
  blocks are steel, which stops bullets.
- **Flow**: 3 stages with 8, 10 and 12 enemies, up to 3 on screen at once. Clearing a stage
  gives a bonus life. After stage 3 the layouts repeat with faster enemies and double points.
- **Fairness**: you respawn with a short blinking shield. Enemies fire when lined up with you.
- **Saves**: the high score and best stage are saved in the game's own file.

To make your own stage, edit one of the strings in `levels[]` in `src/main.c`. Each stage
is 13 rows of 16 cells, and each cell is 2x2 pixels. Use `#` for brick, `S` for steel,
`B` for the base and `.` for empty. Corridors must be at least 2 cells wide, because tanks
are 3 pixels across. Keep the top two rows clear at columns 0-1, 7-8 and 14-15, where
enemies appear, and columns 4-5 of the bottom two rows, where you appear. To add a fourth
stage, raise `NLEVELS` and add a string.

### Summit Climb (`examples/summit_climb`)

An original vertical climbing platformer. Climb as high as you can up an endless mountain
of ice floors.

- **Climbing**: jump (UP) into the floor above to smash the ice and open a hole, then jump
  through it and steer left or right in the air to land beside the hole. The hole is
  4 pixels wide and you are 3, so landing straight back down through it is easy to do by
  accident.
- **Blocks**: solid blocks are ice, which breaks when you jump into it from below.
  Checkered blocks are rock, which never breaks, so walk to a spot without rock above you.
  Higher floors have more rock.
- **Enemies**: snow crabs walk the floors and patch up holes; a crab flashes while it is
  patching. Birds start swooping in from floor 6. Knock either away with the hammer
  (FIRE). Touching them costs a life.
- **Scrolling**: the screen scrolls up as you climb and never scrolls back down. Falling
  below the bottom edge costs a life, and you reappear on the lowest floor on screen with
  a short blinking shield.
- **Scoring**: 10 points per new floor, 1 per block smashed, 5 per crab, 10 per bird. Every
  10 floors is a peak, worth +50 and an extra life. The high score and best floor are
  saved.

Floors are made up as you climb, so every game is different. Tune the difficulty in
`gen_floor()` in `src/main.c`, which sets the number of holes, rocks and crabs, and the
physics with `GRAV`, `JUMP_V` and `MAX_FALL` at the top of the file.

### Balloon Siege (`examples/balloon_siege`)

An original lift-and-archery shooter. You guard a tower on the right edge of the screen,
riding a rope lift up and down and shooting arrows to the left.

- **Raiders** drift down on balloons. Hit a balloon to pop it and send its raider falling.
  An arrow that hits the raider's body below the balloon is blocked.
- **The ground**: raiders who land walk to the foot of the tower and wait. When three are
  waiting, they storm the tower and you lose a life. The last raiders of a wave storm
  without waiting for a third. Ride the lift to the bottom to shoot walkers before they
  get there.
- **Rocks**: floating raiders throw rocks at the lift. Move out of the way, or shoot the
  rock out of the air.
- **Waves**: each wave has 2 more raiders than the last, starting at 8, and raiders fall
  and arrive faster. The dots in the HUD show how many are left. Clearing a wave is
  worth +50.
- **Scoring**: a popped balloon is worth 10, or 20 near the top of the screen. A walker
  is worth 5, and a rock 2. The high score and best wave are saved.

Tune it at the top of `src/main.c`: `WAIT_MAX` sets how many raiders it takes to storm
the tower, `LIFT_TOP` / `LIFT_BOT` set the lift's range, and `update_spawner()` /
`update_raiders()` set the spawn gap, fall speed and how often rocks are thrown.

### Wing Dash (`examples/wing_dash`)

An original one-button flying game. Tap FIRE (or UP) to flap; let go and the bird
falls.

- **Pillars**: fly through the gaps between the stone pillars. Each pillar you pass is
  a point.
- **Difficulty**: the gap starts at 12 pixels and narrows by one pixel every 8 points,
  down to 8. The pillars speed up every 10 points.
- **Crashing**: touching a pillar or the ground ends the run. The bird tumbles to the
  ground, then a score card shows your score and best. A new best plays a fanfare.
- **Saves**: the best score and the number of games played are saved.

Tune the feel at the top of `src/main.c`: `GRAV`, `FLAP_V` and `MAX_FALL` control the
flapping physics, `PIL_GAP_X` the distance between pillars, `gap_height()` how the gaps
narrow, and `speed_fix` the scroll speed in 1/16 pixel per frame.

## 7. Hardware at a glance

| | |
|---|---|
| CPU | ARM7TDMI, 4 MHz by default (up to 8 MHz), code runs from flash in Thumb mode |
| RAM | 2 KB, of which **1.5 KB is yours** (0x200-0x7FF) for globals and the stack |
| Program | up to 15 x 8 KB memory card blocks, mapped at 0x02000000 |
| Screen | 32x32, 1 bit; rotated 180 degrees when docked (the kit handles this) |
| Input | Fire (action) + 4 directions, read from `INT_INPUT` |
| Sound | 1-bit-ish speaker driven by writing levels to a DAC; tones come from a timer IRQ |
| Clock | battery-backed RTC with BCD date/time |

## 8. Testing

### pocketsim (included)

Runs your actual ARM binary. The Sony kernel is replaced by Python versions of its SWI
calls, so **no BIOS dump is needed**. It also catches the classic PocketStation bugs:

- 8-bit reads from flash (they return garbage on hardware) are reported with the PC
- IRQ callbacks that clobber r2-r11 are reported
- unmapped memory accesses stop with a data-abort report
- writes into kernel RAM (stack overflow, stray pointers) are reported
- returning to the menu with a parameter that would erase the clock is reported
- flash saves persist between runs in `name.simsave` (`--fresh` clears them)

Scripted / headless runs for automated tests:

```bat
sim build\snake.bin --headless --frames 300 --input "10:fire,11:,60:down,61:" ^
    --shot 100:mid.png --screenshot end.png --wav sound.wav --ascii
```

`--input` is a list of `frame:buttons` (buttons joined with `+`, empty = release all).
The exit code is 2 if the program crashed, so it works in CI.

The simulator is not cycle-accurate and does not emulate the PSX link, infrared or FIQ
sound. Treat it as a fast first check.

### Emulators

- **no$gba** (nocash) is where the PocketStation emulation lives (not no$psx). It has a
  built-in BIOS clone, or uses the real kernel if you put a 16 KB dump named
  `POCKSTAT.ROM` next to it. See its documentation for loading files.
- **PCSX-Redux** pulls in a PocketStation core (the `pcsx-redux/pocketstation` module,
  which also contains OpenPSK, an open-source stand-in kernel). It is new, so use a recent
  build and check its memory card settings; give it the `.mcd`.

Emulators that use the real kernel need a BIOS dump from your own PocketStation (see below).

## 9. Running on a real PocketStation

The PocketStation behaves like a normal memory card to anything that writes cards, so any
PS1 card writer can install your program.

**PC card writer (MemCARDuino).** An Arduino-compatible board wired to a memory card
connector, driven by MemcardRex on Windows or its Python script. Its script also has
PocketStation commands, including `--psbios` to dump the kernel for emulators.

Writers transfer a whole 128 KB card, so to keep the saves already on the PocketStation:

```bat
:: 1. read the PocketStation into card.mcd with your writer
:: 2. add your program to that image
ps install build\snake.mcs card.mcd
:: 3. write card.mcd back
ps info card.mcd               :: check what is on it before writing
```

Writing `build\name.mcd` directly replaces everything on the PocketStation with just your
program.

**Without a PC card writer.** Copy `name.mcs` through any PS1 save tool you already have
(MemcardRex supports many formats and devices), or a PS1/PS2 save manager that imports
`.mcs`.

On the PocketStation, open the file list, select your program and press the action button.
Your app keeps running when docked in a PlayStation; it shows the screen upright either way.

## 10. Gotchas (read these once)

1. **Never read bytes from flash.** Flash and BIOS only answer 16/32-bit reads; `LDRB`
   returns garbage. Keep const tables as `uint16_t`/`uint32_t`, read strings with
   `ps_flash_u8()`, and let `ps_text()` handle literals. Byte arrays in RAM are fine.
   The build passes `-fno-jump-tables` because GCC's Thumb switch tables use byte loads.
2. **RAM is 1.5 KB** for globals and the stack together. The build prints usage and fails
   if you exceed it. Large constant data belongs in flash (`static const`).
3. **IRQ callbacks** run on a 100-byte stack and must preserve r2-r11; the kernel saves
   only r0, r1, r12. Use `ps_set_frame_hook()` (already wrapped) instead of installing
   your own, and never call `bios_*` from it.
4. **Returning to the menu**: the kernel erases the clock for most GUI parameters.
   `ps_exit()` uses a safe one; if you call `bios_prepare_execute(1, 0, p)` yourself, use
   `0x10` (clock screen) or `0x30 | dir_index` (file list).
5. **Battery**: there is no power switch. Call `ps_standby()` after some idle time in
   handheld mode (all the examples do); it turns the screen off and wakes on any button
   or on docking. Keep the CPU at 4 MHz unless you need 8.
6. **Flash writes** take a few milliseconds each and wear the chip; save on events
   (game over), not every frame.
7. **Docking**: when inserted in a PlayStation, the kernel may cut sound and the PS1 can
   talk to the card. `bios_set_com(1)` lets the PS1 access the card while you run, but
   then the kernel blocks `ps_exit()` until communication is turned off again (the kit
   does that for you).

## 11. What has been verified

- The SDK, all six examples and a generated project build cleanly with arm-none-eabi-gcc 14.
- All of them run in pocketsim: drawing, input, sound (checked as a WAV), standby, docking
  rotation, flash saves surviving a restart, and exiting to the menu.
- Tank Battle was played by a script in pocketsim. It checked shooting, enemy kills, losing
  lives, game over and restarting, and clearing all three stages into the faster second
  loop.
- Summit Climb was played by a script in pocketsim for several thousand frames per run.
  It reached floor 26, and checked breaking ice and jumping through, rock blocking, crabs
  patching holes, birds, the hammer, peak bonuses and extra lives, falling off the bottom,
  game over and restarting.
- Balloon Siege was played by two scripts in pocketsim. A good player cleared waves 1-5
  and checked popping balloons, rocks hitting the lift, and wave bonuses. A player that
  never shoots checked raiders landing, queuing and storming the tower, the end-of-wave
  storm, game over and restarting.
- Wing Dash was played by a script in pocketsim for 28 runs, scoring up to 16. It checked
  flapping, passing pillars, scoring, crashing into pillars and the ground, the score
  card, and the best score surviving a restart.
- Snake's sounds (start, eat, speed-up, game over) were also confirmed in another
  PocketStation emulator (pokket-desktop).
- The `.bin`, `.mcs` and `.mcd` files round-trip through `ps info` and the card tools.
- VMU: the assembler reproduces a published VMU homebrew binary from its source (the
  ADVM audio driver demo). vmu_snake was played by a script in pocketsim (eating, growing,
  speeding up, crashing, game over, best score). It was also run in libevmu, the emulator
  core of ElysianVMU, which loaded the `.vms` + `.vmi` pair and drew the same screens as
  pocketsim frame for frame (title, play, food placement, game over, exit).

**Not yet confirmed on real hardware or a BIOS-accurate emulator** (the register docs
leave these open): the exact frame-timer rate (if your game runs at the wrong speed,
change `PS_SYSCLK_HZ` in `ps.h`), the best `LCD_MODE` refresh setting, the DAC bit range
(the kit's levels work under both documented interpretations), and the mono-icon bit
order (same as the LCD by design). Please test on a real unit before relying on them.

## 12. Dreamcast VMU games

The kit can also build games for SEGA's VMU, the memory card with a screen that plugs into
a Dreamcast controller. A project becomes a VMU project with one line in `project.ini`:

```ini
[project]
target      = vmu                 ; Dreamcast VMU game (LC86K assembly)
name        = vmu_snake           ; output file name
title       = SNAKE               ; shown on the VMU (max 16 chars)
description = Snake - PocketSim DevKit demo   ; shown in the Dreamcast file manager (max 32)
icon        = icon.txt            ; 32x32: .txt art (2 colours) or .png (max 16 colours), up to 3 frames
icon_delay  = 10                  ; animation speed when there are several frames
sources     = src/*.s
vmu_filename =                    ; name on the VMU, max 12 chars (default: from name)
creator     = PocketSim DevKit    ; creator field of the file header
```

```bat
ps new C:\dev\myvmu --target vmu --title "My VMU Game"
cd C:\dev\myvmu
run.bat                           :: builds and opens it in pocketsim
```

**Why assembly?** The VMU has an 8-bit Sanyo LC86K CPU running at about 146 kHz, 512 bytes
of RAM (half of it for your game) and no C compiler that targets it. Every VMU game is
written in assembly, so the kit gives you an assembler plus a runtime that does the fiddly
parts. C projects cannot be retargeted; the build says so if a VMU project lists `.c` files.

What `ps build` does for a VMU project:

- assembles your `src/*.s` with the kit's LC86K assembler (`tools/vmasm.py`; the syntax of
  the common VMU homebrew assembler, so most existing sources work)
- adds the startup code: interrupt vectors, the BIOS clock hook at `$130`, the exit hook at
  `$1F0`, the file header with your titles and icon, and the runtime
- writes `build\name.vms` (the game), `build\name.vmi` (the info file that goes with it) and
  `build\name.lst` (every address and symbol, for debugging)

Your code starts at the label `main`, with the runtime already set up. A minimal game:

```asm
        .include "vmu.i"

x       = VM_USER               ; your variables: RAM bank 1, $14-$FF

main:   mov     #20, x
.loop:  callf   vm_wait_frame   ; 30 frames per second, sleeps in between
        callf   vm_input        ; vm_keys = held, vm_new = just pressed; MODE = back to menu
        ld      x
        st      B
        mov     #16, C
        callf   vm_pclr         ; erase the dot
        bn      vm_keys, KB_LEFT, .r
        dec     x
.r:     bn      vm_keys, KB_RIGHT, .d
        inc     x
.d:     ld      x
        st      B
        callf   vm_pset         ; draw it again
        br      .loop
```

Runtime calls (all with `callf`; the full list with registers is in `vmu/include/vmu.i`):

| Area | Calls |
|---|---|
| Frames / input | `vm_wait_frame`, `vm_input` (sets `vm_keys`, `vm_new`; bits `K_UP` ... `K_SLEEP`, bit numbers `KB_*`), `vm_exit` |
| Drawing (48x32) | `vm_cls`, `vm_pset`, `vm_pclr`, `vm_pxor`, `vm_ptest`, `vm_plot`, `vm_hline`, `vm_fill` (B = x, C = y; `vm_ink` 0 / 1 / 2 = clear / set / invert) |
| Text | `vm_char`, `vm_text` (string at TRH:TRL), `vm_number` (3x5 font) |
| Sound | `vm_beep` (ACC = note `N_C3` ... `N_C6`, B = frames; stops by itself), `vm_quiet` |
| Random | `vm_rand`, `vm_rand_n` |

MODE always returns to the VMU menu and SLEEP blanks the screen until a button is pressed;
`vm_input` handles both. The frame timer uses HALT between frames, which is what keeps the
VMU's small coin cells alive.

**Testing.** `ps run` opens VMU games in pocketsim (`sim game.vms` works too). Keys: arrows =
d-pad, **Z / Space** = A, **X** = B, **Enter** = MODE, **Backspace** = SLEEP, **M** = mute,
**R** = restart, **F12** = screenshot. Headless runs take the button names `up down left
right a b mode sleep`:

```bat
sim examples\vmu_snake\build\vmu_snake.vms --headless --frames 300 --input "20:a,22:,60:down,62:" --ascii --wav snake.wav
```

pocketsim emulates the CPU, the LCD, both timers, the buzzer and HALT, and stands in for the
BIOS clock and exit routines, so no BIOS dump is needed. It stops with a message on a stack
overflow, an invalid opcode or a jump into BIOS code it does not simulate. Flash writes from
games (BIOS `fm_wrt_ex`) are not simulated. Then check in **ElysianVMU** (open the `.vms`).

**On a real VMU.** A VMU holds one game at a time. Ways to get the `.vms` + `.vmi` pair onto it:

- a Dreamcast with an SD card adapter and a file manager such as DreamShell, which copies
  VMU files from the SD card to a VMU
- the Dreamcast web browser downloading the `.vmi` (it fetches the `.vms` named inside it)
  from a web server you control
- a VMU-to-VMU copy from a unit that already has the game

**VMU gotchas**

1. **RAM**: games run with RAM bank 1 selected; `$00-$13` belongs to the runtime, `$14-$FF`
   is yours. The stack lives in bank 0 and holds about 60 nested calls.
2. **Branch range**: `bz`, `bnz`, `bp`, `bn`, `be`, `bne`, `br` and `dbnz` reach -128..+127
   bytes. For anything further use `jmpf` (or `brf`); the assembler tells you when a target
   is out of range.
3. **Speed**: about 100,000 instructions per second. Draw what changes instead of redrawing
   the whole screen each frame (vmu_snake only touches the head and tail cells).
4. **Pitch**: the buzzer divides 5461 Hz, so high notes are only approximate.
5. **Timing**: games run on the VMU's RC oscillator, which differs a few percent between
   units, so the 30 fps frame rate is approximate on hardware.

## 13. References

- psx-spx, *Pocketstation* chapter (nocash's register, kernel and file-format reference):
  https://psx-spx.consoledev.net/pocketstation/
- GBATEK, for ARM7TDMI instruction details: https://problemkaputt.de/gbatek.htm
- PCSX-Redux PocketStation core and OpenPSK kernel: https://github.com/pcsx-redux/pocketstation
- MemCARDuino card reader/writer: https://github.com/ShendoXT/memcarduino
- Marcus Comstedt's VMU pages (hardware, instruction set, file formats): https://mc.pp.se/dc/
- waterbear (VMU assembler/emulator, instruction reference): https://github.com/wtetzner/waterbear
- libevmu / ElysianVMU (VMU emulator): https://github.com/gyrovorbis/libevmu

## License and trademarks

PocketSim DevKit is released under the [MIT License](LICENSE): anyone may use, copy,
modify, merge, publish, distribute, sublicense and sell it, including in commercial
projects, as long as the copyright notice and license text are kept. It comes with no
warranty. Games you build with it are yours; the license notice only needs to travel with
copies of the kit's own source.

PocketSim DevKit is an independent, community-driven open-source project and is not
affiliated with, authorized, or endorsed by Sony Interactive Entertainment or SEGA.
"PlayStation" and "PocketStation" are trademarks of Sony Interactive Entertainment Inc.;
"Dreamcast" and "Visual Memory" (VMU) are trademarks of SEGA Corporation. They are used
here only to describe the hardware the kit targets.

The kit contains no Sony or SEGA code or BIOS. The simulator replaces the PocketStation
kernel and the VMU BIOS routines with its own implementations, and emulators that need the
real firmware require a dump from your own unit.
