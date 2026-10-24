/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * ps/ps.h - PocketSim DevKit runtime (libps)
 *
 *   #include <ps/ps.h>
 *
 *   int main(void) {
 *       ps_init();
 *       for (;;) {
 *           ps_input_update();
 *           if (ps_pressed(PS_BTN_FIRE)) ps_beep(880, 3);
 *           ps_clear();
 *           ps_text(2, 2, "HELLO");
 *           ps_present();
 *           ps_wait_frame();
 *       }
 *   }
 *
 * The screen is 32x32, 1 bit per pixel. Drawing goes to a RAM framebuffer;
 * ps_present() copies it to the LCD. Returning from main() goes back to the
 * PocketStation menu.
 */
#ifndef PS_PS_H
#define PS_PS_H

#include <stdint.h>
#include <stddef.h>
#include "hw.h"
#include "bios.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Core ---------------------------------------------------------------- */
#define PS_W 32
#define PS_H 32

#ifndef PS_FPS
#define PS_FPS 30            /* frame rate driven by Timer 0 */
#endif

/* System clock used for timer maths at CLK_MODE=7 (Hz). */
#define PS_SYSCLK_HZ 3997696u

void     ps_init(void);              /* clocks, LCD, frame timer, IRQ hook */
void     ps_wait_frame(void);        /* block until next frame tick */
uint32_t ps_frame_count(void);       /* frames since ps_init() */
int      ps_is_docked(void);         /* 1 if inserted in a PlayStation */
void     ps_exit(void);              /* return to the PocketStation menu */
void     ps_standby(void);           /* sleep until any button is pressed (handheld only) */
uint32_t ps_boot_param(void);        /* r0 passed by the kernel at start */
void     ps_led(int on);             /* red LED */

/* Per-frame user hook called from the frame IRQ. Keep it tiny. */
void     ps_set_frame_hook(void (*fn)(void));

/* ---- Input --------------------------------------------------------------- */
#define PS_BTN_FIRE   IRQ_BTN_FIRE
#define PS_BTN_RIGHT  IRQ_BTN_RIGHT
#define PS_BTN_LEFT   IRQ_BTN_LEFT
#define PS_BTN_DOWN   IRQ_BTN_DOWN
#define PS_BTN_UP     IRQ_BTN_UP

void     ps_input_update(void);      /* call once per frame */
uint32_t ps_held(uint32_t mask);     /* currently down */
uint32_t ps_pressed(uint32_t mask);  /* went down this frame */
uint32_t ps_released(uint32_t mask); /* went up this frame */
uint32_t ps_idle_frames(void);       /* frames since any button changed */

/* ---- Graphics (RAM framebuffer, 32 rows x 32 bits, bit0 = left) ---------- */
extern uint32_t ps_fb[PS_H];

void ps_clear(void);
void ps_fill(int black);
void ps_present(void);                       /* copy framebuffer to the LCD */
void ps_pset(int x, int y, int black);
int  ps_pget(int x, int y);
void ps_pxor(int x, int y);
void ps_hline(int x0, int x1, int y, int black);
void ps_vline(int x, int y0, int y1, int black);
void ps_line(int x0, int y0, int x1, int y1, int black);
void ps_rect(int x, int y, int w, int h, int black);       /* outline */
void ps_fill_rect(int x, int y, int w, int h, int black);
void ps_invert_rect(int x, int y, int w, int h);
/* Draw a 1bpp sprite: rows[] holds one uint32_t per row, bit0 = leftmost.
 * Set pixels are drawn according to 'mode'. */
#define PS_BLIT_SET  0   /* set bits -> black */
#define PS_BLIT_CLR  1   /* set bits -> white */
#define PS_BLIT_XOR  2
void ps_blit(int x, int y, const uint32_t *rows, int w, int h, int mode);
/* Copy a full 32x32 image (32 words, e.g. from ps-img2c) into the framebuffer. */
void ps_draw_image(const uint32_t img[PS_H]);

/* 3x5 pixel font: digits, A-Z (lowercase is folded), and  :-.!?/+=<>%  */
int  ps_text(int x, int y, const char *s);   /* returns x after last char */
int  ps_number(int x, int y, int32_t n);
int  ps_text_width(const char *s);

/* ---- Sound (square wave on Timer 1) -------------------------------------- */
void ps_tone(uint32_t hz);               /* start tone; 0 = silence */
void ps_beep(uint32_t hz, uint32_t frames);
void ps_sound_off(void);
int  ps_sound_busy(void);
/* Sequence of (hz, frames) pairs ending with frames == 0. hz 0 = rest. */
typedef struct { uint16_t hz; uint16_t frames; } ps_note_t;
void ps_play(const ps_note_t *notes);

/* ---- Save data (flash inside your own file) ------------------------------ */
/* Declare up to PS_SAVE_MAX bytes with PS_SAVE_AREA; it lives in flash in the
 * file's save sectors. Read it like normal const data (16/32-bit accesses),
 * write it with ps_save_write(). Each write rewrites whole 128-byte sectors. */
#define PS_SAVE_MAX 512
int  ps_save_write(const void *ram_src, uint32_t size);   /* 0 = ok */
int  ps_save_read(void *ram_dst, uint32_t size);          /* flash-safe copy */
const void *ps_save_ptr(void);

/* ---- Time & random ------------------------------------------------------- */
typedef struct { uint8_t hour, min, sec, dow, day, month; uint16_t year; } ps_time_t;
void     ps_get_time(ps_time_t *t);
void     ps_srand(uint32_t seed);
uint32_t ps_rand(void);                       /* xorshift32 */
int      ps_rand_range(int lo, int hi);       /* inclusive */
static inline uint32_t ps_bcd2bin(uint32_t v) { return (v >> 4) * 10u + (v & 15u); }

/* ---- Flash-safe memory helpers ------------------------------------------- */
/* FLASH and BIOS ROM return garbage for 8-bit reads, so never dereference a
 * uint8_t/char pointer into const data directly. Use these instead. */
static inline uint8_t ps_flash_u8(const void *p)
{
    uintptr_t a = (uintptr_t)p;
    uint16_t h = *(const volatile uint16_t *)(a & ~(uintptr_t)1);
    return (uint8_t)((a & 1) ? (h >> 8) : h);
}
void *ps_memcpy(void *dst, const void *src, size_t n);   /* flash-safe */
void *ps_memset(void *dst, int c, size_t n);
size_t ps_strlen(const char *s);                          /* flash-safe */

/* ---- Debug (only active when built with debug=1 / -DPS_DEBUG) ------------ */
/* Prints via the SN Systems TTY opcode (shown by pocketsim).
 * On real hardware the kernel's undefined-instruction vector is patched to
 * skip it, and restored by ps_exit(). */
#ifdef PS_DEBUG
void ps_log(const char *s);
void ps_log_hex(const char *label, uint32_t v);
#else
#define ps_log(s) ((void)0)
#define ps_log_hex(l, v) ((void)0)
#endif

#ifdef __cplusplus
}
#endif
#endif /* PS_PS_H */
