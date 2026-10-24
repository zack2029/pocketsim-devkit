/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * sound.c - square-wave tones on the speaker.
 * Timer 1 interrupts at twice the tone frequency and flips the DAC level.
 * Durations are counted in frames by the frame IRQ.
 */
#include <ps/ps.h>

#define TONE_TIMER_HZ (PS_SYSCLK_HZ / 2u)    /* T_DIV2 */

static volatile int32_t  s_level;            /* current DAC amplitude */
static volatile uint32_t s_phase;
static volatile uint32_t s_frames_left;      /* 0 = play until stopped */
static volatile uint8_t  s_on;
static const ps_note_t *volatile s_seq;

static int32_t amplitude(void)
{
    volatile uint32_t *ui = bios_get_ui_status();
    switch (PS_UI_VOLUME(ui)) {              /* honour the menu's volume setting */
    case 0:  return 0x5000;
    case 1:  return 0x1400;
    default: return 0;
    }
}

static void hw_on(void)
{
    if (s_on) return;
    DAC_CTRL = 1;
    IOP_START = IOP_SPEAKER;
    s_on = 1;
}

static void hw_off(void)
{
    INT_MASK_CLR = IRQ_TIMER1;
    T_MODE(1) = 0;
    DAC_DATA = 0;
    DAC_CTRL = 0;
    IOP_STOP = IOP_SPEAKER;
    s_on = 0;
}

static void set_freq(uint32_t hz)
{
    INT_MASK_CLR = IRQ_TIMER1;
    T_MODE(1) = 0;
    if (hz < 16 || hz > 20000) { DAC_DATA = 0; return; }
    if (!s_level) return;
    T_RELOAD(1) = TONE_TIMER_HZ / (hz * 2u) - 1u;
    T_MODE(1)   = T_DIV2 | T_ENABLE;
    INT_ACK      = IRQ_TIMER1;
    INT_MASK_SET = IRQ_TIMER1;
}

void ps__sound_irq(void)
{
    s_phase ^= 1u;
    DAC_DATA = (uint32_t)(s_phase ? s_level : -s_level);
}

void ps__sound_shutdown(void)
{
    s_seq = 0;
    s_frames_left = 0;
    hw_off();
}

static void start_note(const ps_note_t *n)
{
    uint16_t hz = n->hz, fr = n->frames;     /* 16-bit loads: flash-safe */
    if (fr == 0) { s_seq = 0; hw_off(); return; }
    s_frames_left = fr;
    set_freq(hz);
}

/* called from the frame IRQ */
void ps__sound_tick_frame(void)
{
    if (!s_frames_left) return;
    if (--s_frames_left) return;
    if (s_seq) {
        s_seq++;
        start_note(s_seq);
    } else {
        hw_off();
    }
}

void ps_tone(uint32_t hz)
{
    s_seq = 0;
    s_frames_left = 0;
    if (!hz) { hw_off(); return; }
    s_level = amplitude();
    hw_on();
    set_freq(hz);
}

void ps_beep(uint32_t hz, uint32_t frames)
{
    s_seq = 0;
    s_level = amplitude();
    hw_on();
    s_frames_left = frames ? frames : 1;
    set_freq(hz);
}

void ps_play(const ps_note_t *notes)
{
    s_level = amplitude();
    hw_on();
    s_seq = notes;
    start_note(notes);
}

void ps_sound_off(void) { ps__sound_shutdown(); }
int  ps_sound_busy(void) { return s_frames_left != 0; }
