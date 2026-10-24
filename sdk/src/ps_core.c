/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * ps_core.c - init, frame timer, IRQ dispatch, input, exit/standby, time,
 *             random, flash-safe memory helpers, save data, debug log.
 */
#include <ps/ps.h>

/* provided by crt0.S */
extern void ps__irq_entry(void);
extern void ps__tty_putc(uint32_t c);
extern uint32_t ps__boot_param;

/* provided by sound.c */
extern void ps__sound_tick_frame(void);
extern void ps__sound_irq(void);
extern void ps__sound_shutdown(void);

/* provided by the linker script */
extern const uint32_t __ps_save_start[];

static volatile uint32_t s_frames;
static void (*volatile s_frame_hook)(void);
static uint32_t s_lcd_base;
static uint32_t s_btn_now, s_btn_prev, s_idle;
static uint32_t s_saved_undef;
static uint8_t  s_undef_patched;

#define FRAME_TIMER_HZ  (PS_SYSCLK_HZ / 32u)          /* T_DIV32 */
#define FRAME_RELOAD    (FRAME_TIMER_HZ / PS_FPS - 1u)

/* ---- IRQ ---------------------------------------------------------------- */
void ps__irq_handler(void)
{
    uint32_t st = INT_LATCH;

    if (st & IRQ_TIMER1) {               /* audio square wave */
        INT_ACK = IRQ_TIMER1;
        ps__sound_irq();
    }
    if (st & IRQ_TIMER0) {               /* frame tick */
        INT_ACK = IRQ_TIMER0;
        s_frames++;
        ps__sound_tick_frame();
        if (s_frame_hook) s_frame_hook();
    }
    if (st & IRQ_BUTTONS) {              /* standby wake-up: level IRQs, mask them */
        INT_MASK_CLR = IRQ_BUTTONS;
    }
}

/* ---- Core --------------------------------------------------------------- */
void ps_init(void)
{
    bios_set_cpu_speed(PS_CPU_4MHZ);

    INT_MASK_CLR = IRQ_TIMER0 | IRQ_TIMER1;
    bios_set_callback(PS_CB_IRQ, ps__irq_entry);

    T_MODE(0)   = 0;
    T_RELOAD(0) = FRAME_RELOAD;
    T_MODE(0)   = T_DIV32 | T_ENABLE;
    INT_ACK      = IRQ_TIMER0;
    INT_MASK_SET = IRQ_TIMER0;

    s_lcd_base = LCD_ON | LCD_CPEN | LCD_RATE_64HZ;
    ps_clear();
    ps_present();

#ifdef PS_DEBUG
    /* Make the TTY opcode harmless on hardware: undefined vector -> MOVS PC,LR */
    s_saved_undef = *PS_KRAM_UNDEF_VECTOR;
    *PS_KRAM_UNDEF_VECTOR = 0xE1B0F00Eu;
    s_undef_patched = 1;
#endif

    ps_time_t t;
    ps_get_time(&t);
    ps_srand(0x9E3779B9u ^ ((uint32_t)t.sec << 24) ^ ((uint32_t)t.min << 16)
             ^ ((uint32_t)t.hour << 8) ^ t.day ^ bios_read_serial());

    s_btn_now = s_btn_prev = INT_INPUT & IRQ_BUTTONS;
    s_idle = 0;
}

void ps_wait_frame(void)
{
    uint32_t f = s_frames;
    while (s_frames == f) { /* spin: timer 0 IRQ advances s_frames */ }
}

uint32_t ps_frame_count(void) { return s_frames; }
uint32_t ps_boot_param(void)  { return ps__boot_param; }
int      ps_is_docked(void)   { return (INT_INPUT & IRQ_DOCKED) ? 1 : 0; }
void     ps_set_frame_hook(void (*fn)(void)) { s_frame_hook = fn; }

void ps_led(int on)
{
    if (on) IOP_START = IOP_LED; else IOP_STOP = IOP_LED;
}

void ps_present(void)
{
    /* rotate 180 degrees when docked so the image stays upright */
    LCD_MODE = s_lcd_base | (ps_is_docked() ? LCD_ROTATE : 0u);
    volatile uint32_t *v = LCD_VRAM;
    for (int i = 0; i < PS_H; i++) v[i] = ps_fb[i];
}

void ps_exit(void)
{
    ps__sound_shutdown();
    INT_MASK_CLR = IRQ_TIMER0 | IRQ_TIMER1;
    T_MODE(0) = 0;
    T_MODE(1) = 0;
    bios_set_callback(PS_CB_IRQ, 0);
    if (s_undef_patched) { *PS_KRAM_UNDEF_VECTOR = s_saved_undef; s_undef_patched = 0; }

    bios_set_com(0);                       /* DoExecute refuses while docked+com */
    uint32_t dir = bios_get_dir_index() & 15u;
    /* GUI command 3x: file list with our file selected. 1x = clock screen.
     * Other values erase the RTC, so never pass 0 here. */
    uint32_t param = dir ? (0x30u | dir) : 0x10u;
    bios_prepare_execute(1, 0, param);
    bios_do_execute(0);
    for (;;) { }                           /* not reached */
}

void ps_standby(void)
{
    if (ps_is_docked()) return;
    ps__sound_shutdown();
    uint32_t lcd = LCD_MODE;
    LCD_MODE = 0;
    INT_MASK_CLR = IRQ_TIMER0 | IRQ_TIMER1;
    BATT_CTRL = BATT_CTRL & ~3u;
    INT_MASK_SET = IRQ_BUTTONS | IRQ_DOCKED;  /* any button (or docking) wakes us */
    CLK_STOP = 1;                          /* sleeps until FIRE or docking */
    BATT_CTRL = 0;
    INT_MASK_CLR = IRQ_BUTTONS;
    while (INT_INPUT & IRQ_BUTTONS) { }    /* swallow the wake-up press */
    INT_ACK = IRQ_TIMER0;
    INT_MASK_SET = IRQ_TIMER0;
    LCD_MODE = lcd;
    s_btn_now = s_btn_prev = INT_INPUT & IRQ_BUTTONS;
    s_idle = 0;
}

/* ---- Input -------------------------------------------------------------- */
void ps_input_update(void)
{
    s_btn_prev = s_btn_now;
    s_btn_now  = INT_INPUT & IRQ_BUTTONS;
    if (s_btn_now != s_btn_prev) s_idle = 0; else if (s_idle != 0xFFFFFFFFu) s_idle++;
}
uint32_t ps_held(uint32_t m)     { return s_btn_now & m; }
uint32_t ps_pressed(uint32_t m)  { return s_btn_now & ~s_btn_prev & m; }
uint32_t ps_released(uint32_t m) { return ~s_btn_now & s_btn_prev & m; }
uint32_t ps_idle_frames(void)    { return s_idle; }

/* ---- Time & random ------------------------------------------------------ */
void ps_get_time(ps_time_t *t)
{
    uint32_t tm = bios_get_time();
    uint32_t dt = bios_get_date();
    t->sec   = (uint8_t)ps_bcd2bin(tm & 0xFF);
    t->min   = (uint8_t)ps_bcd2bin((tm >> 8) & 0xFF);
    t->hour  = (uint8_t)ps_bcd2bin((tm >> 16) & 0xFF);
    t->dow   = (uint8_t)((tm >> 24) & 0xFF);
    t->day   = (uint8_t)ps_bcd2bin(dt & 0xFF);
    t->month = (uint8_t)ps_bcd2bin((dt >> 8) & 0x1F);
    t->year  = (uint16_t)(ps_bcd2bin((dt >> 24) & 0xFF) * 100u + ps_bcd2bin((dt >> 16) & 0xFF));
}

static uint32_t s_rng = 2463534242u;
void ps_srand(uint32_t seed) { s_rng = seed ? seed : 2463534242u; }
uint32_t ps_rand(void)
{
    uint32_t x = s_rng;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return s_rng = x;
}
int ps_rand_range(int lo, int hi)
{
    if (hi <= lo) return lo;
    return lo + (int)(ps_rand() % (uint32_t)(hi - lo + 1));
}

/* ---- Memory helpers (also back GCC's implicit memcpy/memset) ------------ */
static int in_rom(const void *p) { return (uintptr_t)p >= 0x01000000u; }

void *ps_memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;
    if ((((uintptr_t)d | (uintptr_t)s | n) & 3u) == 0) {
        uint32_t *dw = (uint32_t *)d; const uint32_t *sw = (const uint32_t *)s;
        for (size_t i = 0; i < n / 4; i++) dw[i] = sw[i];
        return dst;
    }
    if (in_rom(s)) { for (size_t i = 0; i < n; i++) d[i] = ps_flash_u8(s + i); }
    else           { for (size_t i = 0; i < n; i++) d[i] = s[i]; }
    return dst;
}

void *ps_memset(void *dst, int c, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    for (size_t i = 0; i < n; i++) d[i] = (uint8_t)c;
    return dst;
}

size_t ps_strlen(const char *s)
{
    size_t n = 0;
    while (ps_flash_u8(s + n)) n++;
    return n;
}

void *memcpy(void *d, const void *s, size_t n)  __attribute__((alias("ps_memcpy")));
void *memset(void *d, int c, size_t n)          __attribute__((alias("ps_memset")));
void *memmove(void *dst, const void *src, size_t n)
{
    uint8_t *d = (uint8_t *)dst; const uint8_t *s = (const uint8_t *)src;
    if (d == s || n == 0) return dst;
    if (d < s || d >= s + n) return ps_memcpy(dst, src, n);
    while (n--) d[n] = s[n];   /* overlapping copies only happen in RAM */
    return dst;
}

/* ---- Save data ---------------------------------------------------------- */
const void *ps_save_ptr(void) { return __ps_save_start; }

int ps_save_read(void *dst, uint32_t size)
{
    if (size > PS_SAVE_MAX) return -1;
    ps_memcpy(dst, __ps_save_start, size);
    return 0;
}

int ps_save_write(const void *src, uint32_t size)
{
    if (size > PS_SAVE_MAX) return -1;
    uint32_t base = ((uint32_t)__ps_save_start - PS_FLASH_VIRT_BASE) / PS_FLASH_SECTOR_SIZE;
    uint32_t buf[PS_FLASH_SECTOR_SIZE / 4];
    for (uint32_t off = 0; off < size; off += PS_FLASH_SECTOR_SIZE) {
        uint32_t n = size - off;
        if (n > PS_FLASH_SECTOR_SIZE) n = PS_FLASH_SECTOR_SIZE;
        ps_memcpy(buf, (const uint8_t *)__ps_save_start + off, PS_FLASH_SECTOR_SIZE);
        ps_memcpy(buf, (const uint8_t *)src + off, n);
        if (bios_flash_write_virtual(base + off / PS_FLASH_SECTOR_SIZE, buf)) return -1;
    }
    return 0;
}

/* ---- Debug -------------------------------------------------------------- */
#ifdef PS_DEBUG
void ps_log(const char *s)
{
    for (size_t i = 0; ; i++) {
        uint8_t c = ps_flash_u8(s + i);
        if (!c) break;
        ps__tty_putc(c);
    }
    ps__tty_putc('\n');
}

void ps_log_hex(const char *label, uint32_t v)
{
    for (size_t i = 0; ; i++) {
        uint8_t c = ps_flash_u8(label + i);
        if (!c) break;
        ps__tty_putc(c);
    }
    ps__tty_putc(' ');
    for (int sh = 28; sh >= 0; sh -= 4) {
        uint32_t d = (v >> sh) & 15u;
        ps__tty_putc(d < 10 ? '0' + d : 'A' + d - 10);
    }
    ps__tty_putc('\n');
}
#endif
