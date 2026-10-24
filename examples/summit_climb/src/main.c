/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * summit_climb - "Summit Climb", an original vertical climbing platformer.
 *
 *   title    : FIRE = start, hold DOWN for 1 s = back to the menu
 *   in game  : LEFT / RIGHT walk, UP jumps, FIRE swings the hammer
 *
 * Jump into the ice floor above to smash a hole, then jump through it.
 * Checkered blocks are rock and cannot be broken. Snow crabs walk the floors
 * and patch holes; birds swoop in higher up. The hammer knocks both away.
 * Once you climb, the screen does not scroll back down: fall off the bottom
 * and you lose a life. Every 10 floors is a peak bonus and an extra life.
 *
 * Shows: fixed-point jump physics, one-way platforms, a scrolling world kept
 * in a small ring buffer, procedural floors, enemies, flash high score.
 */
#include <ps/ps.h>

/* ---- world geometry (world y grows upward, in pixels) --------------------- */
#define PF_Y      6          /* first playfield screen row                     */
#define GAP       7          /* floor k occupies world rows k*7 and k*7+1      */
#define CELLS     16         /* 16 cells of 2 px per floor                     */
#define RING      8          /* floors kept in memory (power of two)           */
#define PW        3          /* player 3 x 4 px                                */
#define PH        4
#define NCRAB     3
#define FIX       16         /* sub-pixel units for vertical motion            */
#define GRAV      4
#define JUMP_V    34         /* apex about 9 px: enough to pass a floor        */
#define MAX_FALL  40

enum { TITLE, PLAY, OVER };

typedef struct { int16_t floor, x; int8_t dir; uint8_t on, fix, anim; } crab_t;
typedef struct { int16_t x, y; int8_t dir; uint8_t on, anim; } bird_t;
typedef struct { uint32_t magic, hiscore, best_floor; } save_t;
#define SAVE_MAGIC 0x534D5431u   /* "SMT1" */

/* ---- sprites (bit (row*3+col), rows top to bottom), uint16_t = flash-safe -- */
static const uint16_t spr_stand = 0xABA;   /* .#. / ### / .#. / #.#  (4 rows) */
static const uint16_t spr_walk  = 0x4BA;   /* .#. / ### / .#. / .#.  (stride) */
static const uint16_t spr_crab  = 0x3D;    /* #.# / ###                        */
static const uint16_t spr_bird1 = 0x15;    /* #.# / .#.                        */
static const uint16_t spr_bird2 = 0x2A;    /* .#. / #.#                        */

/* ---- sounds ------------------------------------------------------------------- */
static const ps_note_t snd_jump[]  = { {700, 1}, {900, 1}, {1100, 1}, {0, 0} };
static const ps_note_t snd_break[] = { {1800, 1}, {900, 1}, {300, 2}, {0, 0} };
static const ps_note_t snd_rock[]  = { {150, 2}, {0, 0} };
static const ps_note_t snd_swing[] = { {400, 1}, {0, 0} };
static const ps_note_t snd_kill[]  = { {1200, 2}, {600, 2}, {300, 3}, {0, 0} };
static const ps_note_t snd_hurt[]  = { {500, 3}, {350, 3}, {250, 3}, {150, 8}, {0, 0} };
static const ps_note_t snd_peak[]  = { {784, 3}, {988, 3}, {1175, 3}, {1568, 3}, {1175, 3}, {1568, 8}, {0, 0} };
static const ps_note_t snd_start[] = { {523, 3}, {659, 3}, {784, 3}, {1047, 6}, {0, 0} };
static const ps_note_t snd_over[]  = { {523, 6}, {415, 6}, {330, 6}, {262, 16}, {0, 0} };
static const ps_note_t snd_patch[] = { {250, 1}, {0, 0} };

/* ---- state (fits easily in the 1.5 KB of user RAM) --------------------------- */
static uint16_t ice[RING], rock[RING];    /* per floor: bit c = cell c         */
static int top_floor;                      /* highest floor generated           */
static int cam_y, cam_target;              /* world row shown at screen row 31  */
static int px, py_fix, vy, on_ground, ground_floor, facing, swing, invuln, walk_t;
static int lives, score, hiscore, best_floor, max_floor, state, timer, banner;
static crab_t crabs[NCRAB];
static bird_t bird;
static int bird_timer;

/* ---- floors -------------------------------------------------------------------- */
static uint16_t cells_at(int x, int w)        /* cells covered by pixels x..x+w-1 */
{
    int c0 = x >> 1, c1 = (x + w - 1) >> 1;
    if (c0 < 0) c0 = 0;
    if (c1 > CELLS - 1) c1 = CELLS - 1;
    return (uint16_t)(((1u << (c1 + 1)) - 1u) & ~((1u << c0) - 1u));
}

static int floor_known(int k) { return k >= 0 && k <= top_floor && k > top_floor - RING; }

static void gen_floor(int k)
{
    int i = k & (RING - 1);
    if (k == 0) { ice[i] = 0; rock[i] = 0xFFFF; return; }   /* solid ground */
    uint16_t s = 0xFFFF, r = 0;
    int holes = ps_rand_range(0, 2);
    for (int h = 0; h < holes; h++) s &= (uint16_t)~(3u << ps_rand_range(1, 13));
    int nrock = k / 4 < 7 ? k / 4 : 7;                       /* harder as you climb */
    for (int n = 0; n < nrock; n++) r |= (uint16_t)(1u << ps_rand_range(0, 15));
    r &= s;
    int c = ps_rand_range(0, 14);                            /* always a breakable 2-cell spot */
    r &= (uint16_t)~(3u << c);
    ice[i] = (uint16_t)(s & ~r);
    rock[i] = r;

    if (k >= 2 && ps_rand_range(0, 99) < 25 + (k > 30 ? 30 : k)) {   /* maybe a crab */
        for (int j = 0; j < NCRAB; j++) {
            if (crabs[j].on) continue;
            int cx = ps_rand_range(0, 13) * 2;
            if (((ice[i] | rock[i]) & cells_at(cx, 3)) != cells_at(cx, 3)) break;
            crabs[j].on = 1; crabs[j].floor = (int16_t)k; crabs[j].x = (int16_t)cx;
            crabs[j].dir = ps_rand_range(0, 1) ? 1 : -1; crabs[j].fix = 0; crabs[j].anim = 0;
            break;
        }
    }
}

static uint16_t solid_of(int k)
{
    if (k < 0) return 0xFFFF;
    if (!floor_known(k)) return 0;
    return (uint16_t)(ice[k & (RING - 1)] | rock[k & (RING - 1)]);
}

static int supported(int k, int x, int w) { return (solid_of(k) & cells_at(x, w)) != 0; }

/* ---- player ---------------------------------------------------------------------- */
static int feet(void) { return py_fix >> 4; }   /* world row of the player's lowest pixel */

static void respawn(int k)
{
    /* stand on floor k (or the lowest floor on screen) where nothing is nearby */
    int low = (cam_y + 6) / GAP;
    if (k < low) k = low;
    for (int tries = 0; tries < 40; tries++) {
        int x = (tries < 15) ? 14 + ((tries & 1) ? tries : -tries) : ps_rand_range(0, 29);
        if (x < 0 || x > 29 || !supported(k, x, PW)) continue;
        if ((solid_of(k) & cells_at(x, PW)) != cells_at(x, PW)) continue;
        int clear = 1;
        for (int j = 0; j < NCRAB; j++)
            if (crabs[j].on && crabs[j].floor == k && crabs[j].x > x - 8 && crabs[j].x < x + 8) clear = 0;
        if (!clear) continue;
        px = x;
        break;
    }
    py_fix = (k * GAP + 2) * FIX;
    vy = 0; on_ground = 1; ground_floor = k; invuln = 60; swing = 0;
}

static void game_over(void)
{
    state = OVER; timer = 0;
    ps_play(snd_over);
    if (score > hiscore || max_floor > best_floor) {
        if (score > hiscore) hiscore = score;
        if (max_floor > best_floor) best_floor = max_floor;
        save_t s = { SAVE_MAGIC, (uint32_t)hiscore, (uint32_t)best_floor };
        if (ps_save_write(&s, sizeof s)) ps_log("save failed");
    }
}

static void hurt(int force)
{
    if (invuln && !force) return;
    ps_play(snd_hurt);
    if (--lives <= 0) { game_over(); return; }
    respawn(ground_floor);
}

static void land_on(int k)
{
    on_ground = 1; vy = 0; ground_floor = k;
    py_fix = (k * GAP + 2) * FIX;
    if (k > max_floor) {
        score += 10 * (k - max_floor);
        max_floor = k;
        if (k % 10 == 0) {                       /* peak bonus */
            score += 50; banner = 2 * PS_FPS;
            if (lives < 9) lives++;
            ps_play(snd_peak);
        }
    }
    if (k * GAP - 2 > cam_target) cam_target = k * GAP - 2;
}

static void move_vertical(void)
{
    int old = feet();
    py_fix += vy;
    int target = feet();
    py_fix = old * FIX + (py_fix & (FIX - 1));   /* walk pixel by pixel */
    int f = old;
    while (f < target) {                     /* rising: head may hit the floor above */
        int head = f + PH;                   /* row the head moves into */
        if (head % GAP == 0) {               /* bottom row of floor head/7 */
            int k = head / GAP;
            uint16_t span = cells_at(px, PW);
            uint16_t hit_ice = ice[k & (RING - 1)] & span, hit_rock = rock[k & (RING - 1)] & span;
            if (floor_known(k) && (hit_ice | hit_rock)) {
                if (hit_ice) { ice[k & (RING - 1)] &= (uint16_t)~hit_ice; score += 1; ps_play(snd_break); }
                else ps_play(snd_rock);
                vy = 0;
                break;
            }
        }
        f++;
    }
    while (f > target) {                     /* falling: land on floor tops */
        if (f % GAP == 2 && supported(f / GAP, px, PW)) { land_on(f / GAP); return; }
        f--;
    }
    py_fix = f * FIX + (py_fix & (FIX - 1));
}

static void update_player(void)
{
    if (invuln) invuln--;
    if (swing) swing--;

    int dir = ps_held(PS_BTN_LEFT) ? -1 : ps_held(PS_BTN_RIGHT) ? 1 : 0;
    if (dir) {
        facing = dir;
        if (!swing && ((++walk_t & 1) || !on_ground)) {   /* 15 px/s walking, 30 px/s in the air */
            int nx = px + dir;
            if (nx >= 0 && nx <= PS_W - PW) px = nx;
        }
    }

    if (on_ground) {
        if (!supported(ground_floor, px, PW)) { on_ground = 0; vy = 0; }   /* walked into a hole */
        else if (ps_pressed(PS_BTN_UP)) { on_ground = 0; vy = JUMP_V; ps_play(snd_jump); }
        else if (ps_pressed(PS_BTN_FIRE) && !swing) { swing = 8; ps_play(snd_swing); }
    }
    if (!on_ground) {
        vy -= GRAV;
        if (vy < -MAX_FALL) vy = -MAX_FALL;
        move_vertical();
    }
    if (feet() < cam_y) hurt(1);             /* fell below the bottom of the screen */
}

static int overlap(int ax, int ay, int aw, int ah, int bx, int by, int bw, int bh)
{
    return ax < bx + bw && bx < ax + aw && ay < by + bh && by < ay + ah;
}

static void hammer_box(int *hx, int *hy)
{
    *hx = facing > 0 ? px + PW : px - 3;
    *hy = feet() + 1;
}

/* ---- enemies ------------------------------------------------------------------------ */
static void update_crabs(void)
{
    int speed = max_floor > 25 ? 2 : 3;
    for (int j = 0; j < NCRAB; j++) {
        crab_t *c = &crabs[j];
        if (!c->on) continue;
        if (c->floor * GAP + 4 < cam_y || !floor_known(c->floor)) { c->on = 0; continue; }
        c->anim++;
        int i = c->floor & (RING - 1);
        if (c->fix) {                        /* patching a hole */
            if (--c->fix == 0) {
                int lead = c->dir > 0 ? c->x + 3 : c->x - 1;
                if (lead >= 0 && lead < PS_W) { ice[i] |= (uint16_t)(1u << (lead >> 1)); ps_play(snd_patch); }
            }
        } else if (c->anim % speed == 0) {
            int nx = c->x + c->dir;
            if (nx < 0 || nx > PS_W - 3) { c->dir = (int8_t)-c->dir; }
            else {
                int lead = c->dir > 0 ? nx + 2 : nx;
                if (!((ice[i] | rock[i]) & (1u << (lead >> 1)))) c->fix = 20;   /* hole ahead: fix it */
                else c->x = (int16_t)nx;
            }
        }
        int cy = c->floor * GAP + 2;
        if (swing >= 2 && swing <= 5) {
            int hx, hy; hammer_box(&hx, &hy);
            if (overlap(hx, hy, 3, 2, c->x, cy, 3, 2)) { c->on = 0; score += 5; ps_play(snd_kill); continue; }
        }
        if (state == PLAY && overlap(px, feet(), PW, PH, c->x, cy, 3, 2)) hurt(0);
    }
}

static void update_bird(void)
{
    if (!bird.on) {
        if (max_floor < 6 || --bird_timer > 0) return;
        bird.on = 1;
        bird.dir = ps_rand_range(0, 1) ? 1 : -1;
        bird.x = (int16_t)(bird.dir > 0 ? -3 : PS_W);
        bird.y = (int16_t)(cam_y + ps_rand_range(6, 20));
        bird.anim = 0;
        return;
    }
    bird.anim++;
    if (bird.anim % (max_floor > 20 ? 2 : 3) == 0) bird.x += bird.dir;
    if ((bird.anim & 15) == 0) bird.y += (bird.y < feet() + 1) ? 1 : -1;   /* drift toward the player */
    if (bird.x < -4 || bird.x > PS_W + 1 || bird.y < cam_y) {
        bird.on = 0;
        bird_timer = ps_rand_range(60, 150);
        return;
    }
    if (swing >= 2 && swing <= 5) {
        int hx, hy; hammer_box(&hx, &hy);
        if (overlap(hx, hy, 3, 2, bird.x, bird.y, 3, 2)) {
            bird.on = 0; bird_timer = 120; score += 10; ps_play(snd_kill);
            return;
        }
    }
    if (state == PLAY && overlap(px, feet(), PW, PH, bird.x, bird.y, 3, 2)) hurt(0);
}

/* ---- drawing -------------------------------------------------------------------------- */
static int scr(int wy) { return 31 - (wy - cam_y); }     /* world row -> screen row */

static void draw_sprite(int x, int wy_bottom, int rows, uint16_t bits, int flip)
{
    for (int r = 0; r < rows; r++) {
        int sy = scr(wy_bottom + rows - 1 - r);
        if (sy < PF_Y) continue;
        for (int c = 0; c < 3; c++)
            if ((bits >> (r * 3 + c)) & 1u) ps_pset(flip ? x + 2 - c : x + c, sy, 1);
    }
}

static void draw_world(void)
{
    /* floors: ice solid, rock checkered, ground below floor 0 solid */
    for (int sy = PF_Y; sy < PS_H; sy++) {
        int wy = cam_y + (31 - sy);
        if (wy < 0) { ps_fb[sy] = 0xFFFFFFFFu; continue; }
        int k = wy / GAP;
        if (wy % GAP > 1 || !floor_known(k)) continue;
        if (k == 0) { ps_fb[sy] = 0xFFFFFFFFu; continue; }   /* ground */
        uint32_t row = 0, tex = (sy & 1) ? 0xAAAAAAAAu : 0x55555555u;
        uint16_t ic = ice[k & (RING - 1)], rk = rock[k & (RING - 1)];
        for (int c = 0; c < CELLS; c++) {
            if (ic & (1u << c)) row |= 3u << (2 * c);
            else if (rk & (1u << c)) row |= (3u << (2 * c)) & tex;
        }
        ps_fb[sy] |= row;
    }

    for (int j = 0; j < NCRAB; j++) {
        crab_t *c = &crabs[j];
        if (!c->on) continue;
        uint16_t bits = (c->fix && (c->anim & 2)) ? 0x3F : spr_crab;    /* flashes while patching */
        draw_sprite(c->x, c->floor * GAP + 2, 2, bits, 0);
    }
    if (bird.on) draw_sprite(bird.x, bird.y, 2, (bird.anim & 4) ? spr_bird1 : spr_bird2, 0);

    if (state == PLAY && !(invuln && (timer & 2))) {
        uint16_t bits = (!on_ground) ? spr_walk : (walk_t & 4) && (ps_held(PS_BTN_LEFT | PS_BTN_RIGHT)) ? spr_walk : spr_stand;
        draw_sprite(px, feet(), PH, bits, facing < 0);
        if (swing) {                                         /* hammer: up, then forward */
            int hx = facing > 0 ? px + PW : px - 3, f = feet();
            if (swing > 5) { ps_pset(px + 1, scr(f + PH), 1); ps_pset(px + 1, scr(f + PH + 1), 1); }
            else { ps_hline(hx, hx + 2, scr(f + 2), 1); ps_pset(facing > 0 ? hx + 2 : hx, scr(f + 1), 1); }
        }
    }

    /* HUD */
    ps_fill_rect(0, 0, 32, PF_Y, 0);
    ps_number(0, 0, score);
    for (int r = 0; r < PH; r++)                           /* mini climber at the top right */
        for (int c = 0; c < 3; c++)
            if ((spr_stand >> (r * 3 + c)) & 1u) ps_pset(25 + c, r, 1);
    ps_number(29, 0, lives > 0 ? lives : 0);
    ps_hline(0, 31, PF_Y - 1, 1);

    if (banner && (banner & 8)) {
        ps_fill_rect(3, 12, 26, 8, 0);
        ps_rect(3, 12, 26, 8, 1);
        ps_text(6, 14, "PEAK");
        ps_number(23, 14, max_floor / 10);
    }
}

static void centered(int y, const char *s) { ps_text((PS_W - ps_text_width(s)) / 2, y, s); }

static void start_game(void)
{
    for (int j = 0; j < NCRAB; j++) crabs[j].on = 0;
    bird.on = 0; bird_timer = 90;
    top_floor = -1;
    cam_y = cam_target = -2;
    for (int k = 0; k < 5; k++) { top_floor = k; gen_floor(k); }
    px = 4; facing = 1; walk_t = 0;
    lives = 3; score = 0; max_floor = 0; banner = 0;
    py_fix = 2 * FIX; vy = 0; on_ground = 1; ground_floor = 0; swing = 0; invuln = 0;
    state = PLAY; timer = 0;
    ps_play(snd_start);
}

/* ---- main ------------------------------------------------------------------------------- */
int main(void)
{
    ps_init();
    save_t s;
    ps_save_read(&s, sizeof s);
    if (s.magic == SAVE_MAGIC) { hiscore = (int)s.hiscore; best_floor = (int)s.best_floor; }

    state = TITLE;
    int hold_down = 0;

    for (;;) {
        ps_input_update();
        ps_clear();
        timer++;

        if (state == TITLE) {
            if (ps_pressed(PS_BTN_FIRE)) { ps_srand(ps_rand() ^ ps_frame_count()); start_game(); }
            hold_down = ps_held(PS_BTN_DOWN) ? hold_down + 1 : 0;
            if (hold_down > PS_FPS) return 0;                     /* back to the menu */
            if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked()) ps_standby();
            centered(0, "SUMMIT");
            centered(6, "CLIMB");
            ps_line(4, 23, 13, 13, 1);                            /* a little mountain */
            ps_line(13, 13, 18, 18, 1);
            ps_line(18, 18, 21, 15, 1);
            ps_line(21, 15, 28, 23, 1);
            ps_hline(11, 15, 15, 1);
            ps_hline(4, 28, 23, 1);
            ps_text(0, 26, "HI");
            ps_number(9, 26, hiscore);
            if (timer & 16) { ps_pset(13, 11, 1); ps_pset(13, 12, 1); ps_pset(14, 11, 1); }  /* flag */
        } else {
            if (state == PLAY) {
                update_player();
                if (cam_y < cam_target) cam_y++;                  /* scroll up, never down */
                while (top_floor < (cam_y + 32) / GAP + 1) { top_floor++; gen_floor(top_floor); }
                if (banner) banner--;
            }
            if (state != OVER || timer < PS_FPS) { update_crabs(); update_bird(); }

            if (state == OVER && timer > PS_FPS) {
                centered(2, "GAME");
                centered(8, "OVER");
                ps_text(0, 16, "F");
                ps_number(5, 16, max_floor);
                ps_number(0, 23, score);
                if (score >= hiscore && score > 0) ps_text(17, 23, "BEST");
                if (timer > 2 * PS_FPS && ps_pressed(PS_BTN_FIRE)) { state = TITLE; timer = 0; }
            } else {
                draw_world();
            }
        }

        ps_present();
        ps_wait_frame();
    }
}
