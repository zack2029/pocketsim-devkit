/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * wing_dash - "Wing Dash", an original one-button flying game.
 *
 *   title    : FIRE = play, hold DOWN for 1 s = back to the menu
 *   in game  : FIRE (or UP) flaps; let go to fall
 *
 * Flap through the gaps between the stone pillars. Each pillar you pass is a
 * point; the gaps narrow and the pillars speed up as your score grows.
 * Touching a pillar or the ground ends the run. The best score is saved.
 *
 * Shows: fixed-point gravity, horizontal scrolling with sub-pixel speed,
 * a ring of recycled obstacles, rectangle collision, a tiny state machine.
 */
#include <ps/ps.h>

#define GROUND     29        /* first ground row (29-31)                          */
#define BIRD_X     7
#define BW         5         /* bird body 5 x 3 for collisions (+ beak, wingtip)  */
#define BH         3
#define NPIL       3
#define PIL_W      4
#define PIL_GAP_X  15        /* distance between pillars                          */
#define FIX        16
#define GRAV       3
#define FLAP_V     (-24)
#define MAX_FALL   36

enum { TITLE, READY, PLAY, DEAD, SCORE };

typedef struct { int16_t x, gap_y, gap_h; uint8_t passed; } pillar_t;
typedef struct { uint32_t magic, best, plays; } save_t;
#define SAVE_MAGIC 0x57444831u   /* "WDH1" */

/* bird frames, 5 x 4, bit (row*5+col), uint16_t = flash-safe; the head is
 * on the right, a beak pixel is added at column 5 */
static const uint16_t bird_up   = 0x3BF9;  /* #..## / ##### / .###. / .....  wing up   */
static const uint16_t bird_mid  = 0x3BF8;  /* ...## / ##### / .###. / .....            */
static const uint16_t bird_down = 0xBFF8;  /* ...## / ##### / ####. / #....  wing down */

static const ps_note_t snd_flap[]  = { {900, 1}, {1300, 1}, {0, 0} };
static const ps_note_t snd_point[] = { {1568, 2}, {2093, 3}, {0, 0} };
static const ps_note_t snd_hit[]   = { {200, 2}, {150, 3}, {0, 0} };
static const ps_note_t snd_fall[]  = { {700, 2}, {600, 2}, {500, 2}, {400, 2}, {300, 2}, {200, 4}, {0, 0} };
static const ps_note_t snd_best[]  = { {784, 3}, {988, 3}, {1175, 3}, {1568, 8}, {0, 0} };

static pillar_t pil[NPIL];
static int by_fix, vy, state, timer, score, best, plays, speed_fix, scroll_fix, ground_off, new_best;

/* ---- helpers ------------------------------------------------------------------ */
static int bird_y(void) { return by_fix / FIX; }

static void draw_bird(int x, int y, uint16_t bits)
{
    for (int r = 0; r < 4; r++)
        for (int c = 0; c < 5; c++)
            if ((bits >> (r * 5 + c)) & 1u) ps_pset(x + c, y + r, 1);
    ps_pset(x + 5, y + 1, 1);           /* beak */
}

static int gap_height(void)
{
    int h = 12 - score / 8;             /* 12 px, shrinking to 8 */
    return h < 8 ? 8 : h;
}

static void place_pillar(pillar_t *p, int x)
{
    int h = gap_height();
    p->x = (int16_t)x;
    p->gap_h = (int16_t)h;
    p->gap_y = (int16_t)ps_rand_range(3, GROUND - 3 - h);
    p->passed = 0;
}

static void reset_run(void)
{
    by_fix = 14 * FIX; vy = 0;
    score = 0; new_best = 0;
    speed_fix = 8;                      /* 0.5 px per frame */
    scroll_fix = 0;
    for (int i = 0; i < NPIL; i++) place_pillar(&pil[i], 34 + i * PIL_GAP_X);
}

static void save_now(void)
{
    save_t s = { SAVE_MAGIC, (uint32_t)best, (uint32_t)plays };
    if (ps_save_write(&s, sizeof s)) ps_log("save failed");
}

static int hits_pillar(int y)
{
    for (int i = 0; i < NPIL; i++) {
        pillar_t *p = &pil[i];
        if (BIRD_X + BW <= p->x - 1 || BIRD_X >= p->x + PIL_W + 1) continue;   /* lips are 1 px wider */
        if (y < p->gap_y || y + BH > p->gap_y + p->gap_h) return 1;
    }
    return 0;
}

static void die(void)
{
    state = DEAD; timer = 0;
    ps_play(snd_hit);
    plays++;
    if (score > best) { best = score; new_best = 1; }
    save_now();
}

/* ---- drawing -------------------------------------------------------------------- */
static void draw_pillars(void)
{
    for (int i = 0; i < NPIL; i++) {
        pillar_t *p = &pil[i];
        int top_end = p->gap_y - 1, bot_start = p->gap_y + p->gap_h;
        for (int y = 0; y < GROUND; y++) {
            if (y > top_end && y < bot_start) continue;
            int lip = (y == top_end || y == top_end - 1 || y == bot_start || y == bot_start + 1);
            if (lip) ps_hline(p->x - 1, p->x + PIL_W, y, 1);
            else {                                            /* hollow column with a stripe */
                ps_pset(p->x, y, 1);
                ps_pset(p->x + PIL_W - 1, y, 1);
                if ((y & 3) == 0) ps_pset(p->x + 1, y, 1);
            }
        }
    }
}

static void draw_ground(void)
{
    ps_hline(0, 31, GROUND, 1);
    for (int x = 0; x < 32; x++) if (((x + ground_off) & 3) == 0) ps_pset(x, GROUND + 1, 1);
    ps_hline(0, 31, GROUND + 2, 1);
}

static void draw_score_top(int v)
{
    char buf[6]; int n = 0;
    if (v == 0) buf[n++] = '0';
    while (v && n < 5) { buf[n++] = (char)('0' + v % 10); v /= 10; }
    int w = n * 4 - 1, x = (PS_W - w) / 2;
    ps_fill_rect(x - 1, 0, w + 2, 7, 0);
    for (int i = 0; i < n; i++) {
        char s[2] = { buf[n - 1 - i], 0 };
        ps_text(x + i * 4, 1, s);
    }
}

static void centered(int y, const char *s) { ps_text((PS_W - ps_text_width(s)) / 2, y, s); }

/* ---- main ------------------------------------------------------------------------ */
int main(void)
{
    ps_init();
    save_t s;
    ps_save_read(&s, sizeof s);
    if (s.magic == SAVE_MAGIC) { best = (int)s.best; plays = (int)s.plays; }

    state = TITLE;
    reset_run();
    int hold_down = 0;

    for (;;) {
        ps_input_update();
        ps_clear();
        timer++;
        uint32_t f = ps_frame_count();
        int tap = ps_pressed(PS_BTN_FIRE | PS_BTN_UP) != 0;
        uint16_t frame = (f & 4) ? bird_up : bird_down;

        switch (state) {
        case TITLE:
            if (tap) { reset_run(); state = READY; timer = 0; break; }
            hold_down = ps_held(PS_BTN_DOWN) ? hold_down + 1 : 0;
            if (hold_down > PS_FPS) return 0;                    /* back to the menu */
            if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked()) ps_standby();
            ground_off++;
            centered(1, "WING");
            centered(7, "DASH");
            draw_bird(13, 15 + ((f >> 3) & 1), frame);
            ps_text(1, 22, "BEST");
            ps_number(19, 22, best);
            draw_ground();
            break;

        case READY:                                              /* bob until the first flap */
            ground_off++;
            by_fix = (14 + ((f >> 3) & 1)) * FIX;
            draw_bird(BIRD_X, bird_y(), frame);
            if (timer & 16) centered(20, "TAP");
            draw_score_top(0);
            draw_ground();
            if (tap) { state = PLAY; vy = FLAP_V; ps_play(snd_flap); }
            break;

        case PLAY: {
            if (tap) { vy = FLAP_V; ps_play(snd_flap); }
            vy += GRAV;
            if (vy > MAX_FALL) vy = MAX_FALL;
            by_fix += vy;
            if (by_fix < 0) { by_fix = 0; vy = 0; }

            /* scroll: speed in 1/16 px per frame, carried in scroll_fix */
            scroll_fix += speed_fix;
            int dx = scroll_fix / FIX;
            scroll_fix %= FIX;
            ground_off += dx;
            for (int i = 0; i < NPIL; i++) {
                pillar_t *p = &pil[i];
                p->x = (int16_t)(p->x - dx);
                if (!p->passed && p->x + PIL_W < BIRD_X) {
                    p->passed = 1; score++;
                    ps_play(snd_point);
                    if (score % 10 == 0 && speed_fix < 14) speed_fix++;   /* faster every 10 */
                }
                if (p->x < -PIL_W - 2) {                                  /* recycle to the right */
                    int far = 0;
                    for (int j = 0; j < NPIL; j++) if (pil[j].x > far) far = pil[j].x;
                    place_pillar(p, far + PIL_GAP_X);
                }
            }

            int y = bird_y();
            uint16_t b = vy < -8 ? bird_up : vy > 12 ? bird_down : bird_mid;
            draw_pillars();
            draw_bird(BIRD_X, y, (f & 2) && vy < 0 ? bird_down : b);
            draw_ground();
            draw_score_top(score);
            if (hits_pillar(y) || y + BH > GROUND) die();
            break;
        }

        case DEAD: {                                             /* tumble to the ground */
            if (timer == 8) ps_play(snd_fall);
            if (timer > 6) {
                vy += GRAV;
                if (vy > MAX_FALL) vy = MAX_FALL;
                by_fix += vy;
            }
            int y = bird_y();
            if (y + BH > GROUND) { y = GROUND - BH; by_fix = y * FIX; }
            draw_pillars();
            draw_bird(BIRD_X, y, bird_down);                     /* wings limp */
            draw_ground();
            draw_score_top(score);
            if (timer < 3) ps_invert_rect(0, 0, 32, 32);        /* hit flash */
            if (timer > PS_FPS + 10 && y + BH >= GROUND) {
                state = SCORE; timer = 0;
                if (new_best) ps_play(snd_best);
            }
            break;
        }

        case SCORE:
            ps_rect(1, 1, 30, 26, 1);
            centered(3, "SCORE");
            ps_number(score < 10 ? 14 : score < 100 ? 12 : 10, 9, score);
            ps_text(3, 15, "BEST");
            ps_number(20, 15, best);
            if (new_best && (timer & 8)) centered(21, "NEW");
            draw_ground();
            if (timer > PS_FPS && tap) { state = TITLE; timer = 0; }
            break;
        }

        ps_present();
        ps_wait_frame();
    }
}
