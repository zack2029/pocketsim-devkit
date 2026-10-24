/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * hello - tour of the PocketSim DevKit basics
 *   - draws text, the real-time clock, and a bouncing ball
 *   - d-pad moves a cursor, FIRE plays a short jingle and inverts the screen
 *   - goes to standby after 20 s without input (handheld only)
 *   - hold FIRE + DOWN for one second to return to the PocketStation menu
 */
#include <ps/ps.h>

static const ps_note_t jingle[] = {
    { 523, 3 }, { 659, 3 }, { 784, 3 }, { 1047, 6 }, { 0, 0 },
};

/* 8x8 smiley, one uint32_t per row, bit0 = left */
static const uint32_t smiley[8] = {
    0x3C, 0x42, 0xA5, 0x81, 0xA5, 0x99, 0x42, 0x3C,
};

static void two_digits(int x, int y, int v)
{
    ps_number(x, y, v / 10);
    ps_number(x + 4, y, v % 10);
}

int main(void)
{
    ps_init();
    ps_log("hello: started");

    int bx = 4, by = 12, dx = 1, dy = 1;
    int cx = 22, cy = 22;
    int inverted = 0, quit = 0;

    for (;;) {
        ps_input_update();

        if (ps_held(PS_BTN_LEFT)  && cx > 0)  cx--;
        if (ps_held(PS_BTN_RIGHT) && cx < 24) cx++;
        if (ps_held(PS_BTN_UP)    && cy > 9)  cy--;
        if (ps_held(PS_BTN_DOWN)  && cy < 24) cy++;
        if (ps_pressed(PS_BTN_FIRE)) {
            inverted = !inverted;
            ps_play(jingle);
            ps_log_hex("fire at frame", ps_frame_count());
        }

        quit = (ps_held(PS_BTN_FIRE) && ps_held(PS_BTN_DOWN)) ? quit + 1 : 0;
        if (quit > PS_FPS) break;

        if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked())
            ps_standby();

        /* ball */
        bx += dx; by += dy;
        if (bx <= 0 || bx >= 30) dx = -dx;
        if (by <= 8 || by >= 30) dy = -dy;

        /* draw */
        ps_clear();
        ps_time_t t;
        ps_get_time(&t);
        two_digits(1, 1, t.hour);
        ps_text(9, 1, ":");
        two_digits(12, 1, t.min);
        ps_text(20, 1, ":");
        two_digits(23, 1, t.sec);
        ps_hline(0, 31, 7, 1);

        ps_fill_rect(bx, by, 2, 2, 1);
        ps_blit(cx, cy, smiley, 8, 8, PS_BLIT_XOR);
        if (ps_is_docked()) ps_text(1, 26, "PSX");
        if (inverted) ps_invert_rect(0, 0, 32, 32);

        ps_present();
        ps_wait_frame();
    }
    return 0;
}
