/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * balloon_siege - "Balloon Siege", an original lift-and-archery shooter.
 *
 *   title    : FIRE = start, hold DOWN for 1 s = back to the menu
 *   in game  : UP / DOWN move the lift, FIRE shoots an arrow to the left
 *
 * You guard the tower on the right, riding a rope lift. Raiders drift down on
 * balloons: pop a balloon and its raider drops. Raiders who reach the ground
 * walk to the tower; when three are waiting there, they storm it and you
 * lose a life. Ride down to shoot the walkers. Floating raiders throw rocks at
 * you; move out of the way or shoot the rock. Clear a wave for a bonus.
 *
 * Shows: a moving player on a fixed axis, simple projectiles both ways, an
 * entity pool with several states (float, fall, walk, wait), waves with
 * rising difficulty, flash high score.
 */
#include <ps/ps.h>

#define PF_Y      6          /* first playfield row (rows 0-5 are the HUD)       */
#define GROUND    31         /* ground line                                       */
#define LIFT_X    26         /* gondola's left wall                               */
#define LIFT_TOP  8
#define LIFT_BOT  27         /* arrows then fly at row 28: walker height          */
#define NR        6          /* raiders on screen                                 */
#define NA        2          /* arrows                                            */
#define NROCK     3
#define WAIT_MAX  3          /* raiders waiting at the tower before it falls      */

enum { TITLE, INTRO, PLAY, OVER };
enum { R_OFF, R_FLOAT, R_FALL, R_WALK, R_WAIT };

/* raider: balloon rows y..y+2, string y+3, body y+4..y+6 (x..x+2) */
typedef struct { int16_t x, y; uint8_t st, t, sway, slot; } raider_t;
typedef struct { int16_t x, y; uint8_t on; } shot_t;
typedef struct { uint32_t magic, hiscore, best_wave; } save_t;
#define SAVE_MAGIC 0x424C4E31u   /* "BLN1" */

/* sprites: bit (row*3+col); uint16_t keeps them flash-safe */
static const uint16_t spr_balloon = 0x0BF;   /* ### / ### / .#.  */
static const uint16_t spr_pop     = 0x145;   /* #.# / ... / #.#  (burst) */
static const uint16_t spr_raider  = 0x17A;   /* .#. / ### / #.#  */
static const uint16_t spr_walk    = 0x0BA;   /* .#. / ### / .#.  */

static const ps_note_t snd_shoot[] = { {1400, 1}, {1800, 1}, {0, 0} };
static const ps_note_t snd_pop[]   = { {2200, 1}, {1500, 1}, {900, 1}, {0, 0} };
static const ps_note_t snd_land[]  = { {200, 2}, {0, 0} };
static const ps_note_t snd_throw[] = { {600, 1}, {0, 0} };
static const ps_note_t snd_block[] = { {300, 1}, {0, 0} };
static const ps_note_t snd_kill[]  = { {900, 2}, {450, 3}, {0, 0} };
static const ps_note_t snd_hurt[]  = { {500, 3}, {350, 3}, {250, 3}, {150, 8}, {0, 0} };
static const ps_note_t snd_storm[] = { {300, 2}, {300, 2}, {200, 2}, {200, 2}, {120, 10}, {0, 0} };
static const ps_note_t snd_wave[]  = { {659, 3}, {784, 3}, {988, 3}, {1319, 8}, {0, 0} };
static const ps_note_t snd_start[] = { {523, 3}, {659, 3}, {784, 3}, {1047, 6}, {0, 0} };
static const ps_note_t snd_over[]  = { {523, 6}, {415, 6}, {330, 6}, {262, 16}, {0, 0} };

static raider_t rd[NR];
static shot_t arrows[NA], rocks[NROCK];
static int ay, invuln, cool, lives, score, hiscore, best_wave, wave, state, timer;
static int to_spawn, remaining, spawn_timer, waiting;

/* ---- helpers ---------------------------------------------------------------- */
static void sprite(int x, int y, int rows, uint16_t bits, int flip)
{
    for (int r = 0; r < rows; r++)
        for (int c = 0; c < 3; c++)
            if ((bits >> (r * 3 + c)) & 1u) ps_pset(flip ? x + 2 - c : x + c, y + r, 1);
}

static int in_box(int px, int py, int x, int y, int w, int h)
{
    return px >= x && px < x + w && py >= y && py < y + h;
}

static void game_over(void)
{
    state = OVER; timer = 0;
    ps_play(snd_over);
    if (score > hiscore || wave + 1 > best_wave) {
        if (score > hiscore) hiscore = score;
        if (wave + 1 > best_wave) best_wave = wave + 1;
        save_t s = { SAVE_MAGIC, (uint32_t)hiscore, (uint32_t)best_wave };
        if (ps_save_write(&s, sizeof s)) ps_log("save failed");
    }
}

static void lose_life(const ps_note_t *snd)
{
    ps_play(snd);
    for (int i = 0; i < NROCK; i++) rocks[i].on = 0;
    if (--lives <= 0) { game_over(); return; }
    invuln = 60;
}

static void raider_done(raider_t *r)
{
    r->st = R_OFF;
    if (--remaining == 0) {                       /* wave cleared */
        score += 50;
        ps_play(snd_wave);
        wave++;
        state = INTRO; timer = 0;
    }
}

static void start_wave(void)
{
    for (int i = 0; i < NR; i++) rd[i].st = R_OFF;
    for (int i = 0; i < NA; i++) arrows[i].on = 0;
    for (int i = 0; i < NROCK; i++) rocks[i].on = 0;
    to_spawn = remaining = 8 + 2 * wave;
    spawn_timer = 30;
    waiting = 0;
}

/* ---- update ------------------------------------------------------------------- */
static void update_archer(void)
{
    if (invuln) invuln--;
    if (cool) cool--;
    if (ps_held(PS_BTN_UP) && ay > LIFT_TOP) ay--;
    if (ps_held(PS_BTN_DOWN) && ay < LIFT_BOT) ay++;
    if (ps_pressed(PS_BTN_FIRE) && !cool) {
        for (int i = 0; i < NA; i++) {
            if (arrows[i].on) continue;
            arrows[i].on = 1; arrows[i].x = LIFT_X - 4; arrows[i].y = (int16_t)(ay + 1);
            cool = 6;
            ps_play(snd_shoot);
            break;
        }
    }
}

static void update_arrows(void)
{
    for (int i = 0; i < NA; i++) {
        shot_t *a = &arrows[i];
        for (int step = 0; step < 2 && a->on; step++) {
            a->x--;
            if (a->x < -2) { a->on = 0; break; }
            int tip = a->x, y = a->y;
            for (int k = 0; k < NROCK; k++)        /* shoot rocks out of the air */
                if (rocks[k].on && rocks[k].y == y && rocks[k].x >= tip - 1 && rocks[k].x <= tip + 1) {
                    rocks[k].on = 0; a->on = 0; score += 2; ps_play(snd_block);
                }
            for (int j = 0; j < NR && a->on; j++) {
                raider_t *r = &rd[j];
                if (r->st == R_FLOAT) {
                    if (in_box(tip, y, r->x, r->y, 3, 3)) {          /* balloon: pop! */
                        r->st = R_FALL; r->t = 0;
                        score += 10 + (r->y < 16 ? 10 : 0);          /* more for high shots */
                        a->on = 0; ps_play(snd_pop);
                    } else if (in_box(tip, y, r->x, r->y + 4, 3, 3)) { /* body blocks arrows */
                        a->on = 0; ps_play(snd_block);
                    }
                } else if (r->st == R_WALK || r->st == R_WAIT) {
                    if (in_box(tip, y, r->x, GROUND - 3, 3, 3)) {
                        if (r->st == R_WAIT) waiting--;
                        score += 5; a->on = 0; ps_play(snd_kill);
                        raider_done(r);
                    }
                }
            }
        }
    }
}

static void update_rocks(void)
{
    for (int k = 0; k < NROCK; k++) {
        shot_t *s = &rocks[k];
        if (!s->on) continue;
        s->x++;
        if (s->x >= PS_W) { s->on = 0; continue; }
        if (in_box(s->x, s->y, LIFT_X, ay, 5, 4)) {
            s->on = 0;
            if (!invuln) lose_life(snd_hurt);
        }
    }
}

static void throw_rock(raider_t *r)
{
    for (int k = 0; k < NROCK; k++) {
        if (rocks[k].on) continue;
        rocks[k].on = 1; rocks[k].x = (int16_t)(r->x + 3); rocks[k].y = (int16_t)(r->y + 5);
        ps_play(snd_throw);
        return;
    }
}

static void storm(void)
{
    for (int i = 0; i < NR; i++)
        if (rd[i].st == R_WAIT) { rd[i].st = R_OFF; remaining--; }
    waiting = 0;
    lose_life(snd_storm);
    if (state == PLAY && remaining <= 0) {        /* that was the end of the wave */
        remaining = 1;
        raider_t dummy = { 0, 0, R_OFF, 0, 0, 0 };
        raider_done(&dummy);
    }
}

static void update_raiders(void)
{
    int fall_every = wave < 3 ? 4 : wave < 6 ? 3 : 2;
    for (int j = 0; j < NR; j++) {
        raider_t *r = &rd[j];
        r->t++;
        switch (r->st) {
        case R_FLOAT:
            if (r->t % fall_every == 0) r->y++;
            if ((r->t & 15) == 0) {                                     /* sway in the wind */
                int nx = r->x + (r->sway ? 1 : -1);
                if (nx < 0 || nx > 20) r->sway ^= 1; else r->x = (int16_t)nx;
                if (ps_rand_range(0, 3) == 0) r->sway ^= 1;
            }
            if (r->y > PF_Y && r->y < 20 && ps_rand_range(0, 400 - 25 * (wave < 10 ? wave : 10)) == 0)
                throw_rock(r);
            if (r->y + 6 >= GROUND - 1) { r->st = R_WALK; r->y = GROUND - 7; ps_play(snd_land); }
            break;
        case R_FALL:
            r->y += 2;
            if (r->y + 4 > GROUND) raider_done(r);
            break;
        case R_WALK: {
            int stop = LIFT_X - 5 - 4 * waiting;                        /* queue up at the tower */
            if (r->t % 3 == 0) r->x++;
            if (r->x >= stop) {
                r->x = (int16_t)stop; r->st = R_WAIT; waiting++;
                if (waiting >= WAIT_MAX) { storm(); return; }           /* they storm the tower */
            }
            break;
        }
        default:
            break;
        }
    }
    /* last raiders of the wave are all waiting: they don't wait for a third */
    if (waiting && !to_spawn) {
        for (int j = 0; j < NR; j++)
            if (rd[j].st == R_FLOAT || rd[j].st == R_FALL || rd[j].st == R_WALK) return;
        storm();
    }
}

static void update_spawner(void)
{
    if (!to_spawn) return;
    if (spawn_timer) { spawn_timer--; return; }
    for (int j = 0; j < NR; j++) {
        if (rd[j].st != R_OFF) continue;
        rd[j].st = R_FLOAT;
        rd[j].x = (int16_t)ps_rand_range(1, 19);
        rd[j].y = -1;                       /* appears from behind the HUD */
        rd[j].t = (uint8_t)ps_rand_range(0, 15);
        rd[j].sway = (uint8_t)ps_rand_range(0, 1);
        to_spawn--;
        int gap = 70 - 5 * wave;
        spawn_timer = ps_rand_range(gap < 20 ? 20 : gap, (gap < 20 ? 20 : gap) + 30);
        return;
    }
}

/* gondola walls + floor, archer inside with the bow at the left wall:
 *      ..#..     head          (row ay)
 *     )###.#     bow + arms    (row ay+1, the arrow row)
 *     #.#.#      body          (row ay+2)
 *     #####      floor         (row ay+3)              */
static void draw_lift(int y, int show_archer)
{
    int x = LIFT_X;
    ps_hline(x, x + 4, y + 3, 1);
    ps_vline(x, y + 1, y + 3, 1);
    ps_vline(x + 4, y + 1, y + 3, 1);
    ps_pset(x + 1, y - 1, 1); ps_pset(x + 3, y - 1, 1);       /* hanger */
    ps_hline(x + 1, x + 3, y - 1, 1);
    if (!show_archer) return;
    ps_pset(x + 2, y, 1);
    ps_hline(x - 1, x + 2, y + 1, 1);
    ps_pset(x + 2, y + 2, 1);
    ps_pset(x - 2, y, 1); ps_pset(x - 2, y + 2, 1);           /* bow */
}

/* ---- draw -------------------------------------------------------------------------- */
static void draw_world(void)
{
    uint32_t f = ps_frame_count();

    /* tower, pulley, rope, lift */
    ps_hline(LIFT_X - 1, 31, PF_Y, 1);
    ps_vline(31, PF_Y, GROUND, 1);
    ps_vline(LIFT_X + 2, PF_Y + 1, ay - 2, 1);
    draw_lift(ay, !(invuln && (f & 2)));

    /* ground */
    ps_hline(0, 31, GROUND, 1);

    for (int j = 0; j < NR; j++) {
        raider_t *r = &rd[j];
        switch (r->st) {
        case R_FLOAT:
            sprite(r->x, r->y, 3, spr_balloon, 0);
            ps_pset(r->x + 1, r->y + 3, 1);
            sprite(r->x, r->y + 4, 3, spr_raider, 0);
            break;
        case R_FALL:
            if (r->t < 4) sprite(r->x, r->y, 3, spr_pop, 0);
            sprite(r->x, r->y + 4, 3, (r->t & 2) ? spr_raider : spr_walk, r->t & 4);   /* tumbling */
            break;
        case R_WALK:
            sprite(r->x, GROUND - 3, 3, (r->t & 4) ? spr_walk : spr_raider, 0);
            break;
        case R_WAIT:
            sprite(r->x, GROUND - 3, 3, (f & 8) ? spr_raider : spr_walk, 0);         /* jumping, impatient */
            break;
        }
    }
    for (int i = 0; i < NA; i++)
        if (arrows[i].on) { ps_hline(arrows[i].x, arrows[i].x + 2, arrows[i].y, 1); ps_pset(arrows[i].x + 2, arrows[i].y - 1, 1); }
    for (int k = 0; k < NROCK; k++)
        if (rocks[k].on) { ps_pset(rocks[k].x, rocks[k].y, 1); ps_pset(rocks[k].x, rocks[k].y + 1, f & 1); }

    /* HUD on top (raiders may slide in behind it) */
    ps_fill_rect(0, 0, 32, PF_Y, 0);
    ps_number(0, 0, score);
    sprite(23, 0, 3, spr_balloon, 0);
    ps_pset(24, 3, 1);
    ps_number(29, 0, lives > 0 ? lives : 0);
    for (int k = 0; k < remaining && k < 18; k++) ps_pset(13 + (k % 9), (k < 9) ? 1 : 3, 1);   /* raiders left */
    ps_hline(0, 31, PF_Y - 1, 1);
}

static void centered(int y, const char *s) { ps_text((PS_W - ps_text_width(s)) / 2, y, s); }

/* ---- main --------------------------------------------------------------------------- */
int main(void)
{
    ps_init();
    save_t s;
    ps_save_read(&s, sizeof s);
    if (s.magic == SAVE_MAGIC) { hiscore = (int)s.hiscore; best_wave = (int)s.best_wave; }

    state = TITLE;
    int hold_down = 0;

    for (;;) {
        ps_input_update();
        ps_clear();
        timer++;

        switch (state) {
        case TITLE:
            if (ps_pressed(PS_BTN_FIRE)) {
                ps_srand(ps_rand() ^ ps_frame_count());
                score = 0; lives = 3; wave = 0; ay = 16; invuln = 0; cool = 0;
                state = INTRO; timer = 0;
                ps_play(snd_start);
                break;
            }
            hold_down = ps_held(PS_BTN_DOWN) ? hold_down + 1 : 0;
            if (hold_down > PS_FPS) return 0;                     /* back to the menu */
            if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked()) ps_standby();
            centered(0, "BALLOON");
            centered(6, "SIEGE");
            {
                int by = 13 + ((timer >> 3) & 1);                 /* bobbing raider */
                sprite(6, by, 3, spr_balloon, 0);
                ps_pset(7, by + 3, 1);
                sprite(6, by + 4, 3, spr_raider, 0);
            }
            ps_vline(LIFT_X + 2, 12, 13, 1);
            draw_lift(15, 1);
            if ((timer >> 1) % 14 < 10) ps_hline(LIFT_X - 6 - (timer >> 1) % 14, LIFT_X - 4 - (timer >> 1) % 14, 16, 1);
            ps_text(0, 26, "HI");
            ps_number(9, 26, hiscore);
            break;

        case INTRO:
            if (timer == 1) start_wave();
            centered(8, "WAVE");
            ps_number(wave + 1 < 10 ? 14 : 12, 16, wave + 1);
            if (timer > 2 * PS_FPS) { state = PLAY; timer = 0; }
            break;

        case PLAY:
        case OVER:
            if (state == PLAY) {
                update_archer();
                update_arrows();
                update_rocks();
                if (state == PLAY) update_raiders();
                if (state == PLAY) update_spawner();
            }
            if (state == OVER && timer > PS_FPS) {
                centered(2, "GAME");
                centered(8, "OVER");
                ps_text(0, 16, "W");
                ps_number(5, 16, wave + 1);
                ps_number(0, 23, score);
                if (score >= hiscore && score > 0) ps_text(17, 23, "BEST");
                if (timer > 2 * PS_FPS && ps_pressed(PS_BTN_FIRE)) { state = TITLE; timer = 0; }
                break;
            }
            draw_world();
            break;
        }

        ps_present();
        ps_wait_frame();
    }
}
