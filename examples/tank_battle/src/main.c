/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * tank_battle - "Tank Battle", an original top-down tank shooter
 * squeezed onto the 32x32 screen.
 *
 *   title       : FIRE = start, hold DOWN for 1 s = back to the menu
 *   in game     : d-pad moves (the tank turns first), FIRE shoots
 *   goal        : destroy every enemy tank; keep them away from your base
 *   game over   : lose all 3 lives, or let the base at the bottom get shot
 *   high score  : saved to flash inside the game's own file
 *
 * Shows: pixel collision against a destructible map, simple enemy AI,
 * a fixed entity pool (no malloc, all in 1.5 KB of RAM), sound effects,
 * flash-safe level data and save data.
 */
#include <ps/ps.h>

/* ---- layout -------------------------------------------------------------- */
#define PF_Y      6          /* playfield starts at this screen row         */
#define PF_H      26         /* 26 pixel rows = 13 map rows of 2x2 cells     */
#define MAP_W     16
#define MAP_H     13
#define TS        3          /* tanks are 3x3 pixels                        */
#define NT        4          /* tank 0 = player, 1..3 = enemies on screen   */
#define NEXP      4
#define NLEVELS   3

enum { UP, RIGHT, DOWN, LEFT };
static const int DX[4] = { 0, 1, 0, -1 };
static const int DY[4] = { -1, 0, 1, 0 };

enum { T_DEAD, T_SPAWN, T_ALIVE };
enum { TITLE, INTRO, PLAY, CLEAR, OVER };

typedef struct {
    int16_t x, y;
    uint8_t dir, state, timer, cool, shield, move;
} tank_t;

typedef struct { int16_t x, y; uint8_t dir, on; } bullet_t;   /* bullet i belongs to tank i */
typedef struct { int16_t x, y; uint8_t t; } boom_t;

typedef struct { uint32_t magic, hiscore, best_stage; } save_t;
#define SAVE_MAGIC 0x544E4B31u   /* "TNK1" */

/* ---- levels: '#' brick, 'S' steel, 'B' base, '.' empty (16 x 13 cells) ---
 * Strings live in flash: read them only with ps_flash_u8(). Keep rows 0-1
 * clear at the spawn columns (0-1, 7-8, 14-15) and cols 4-5 of rows 11-12
 * clear for the player. */
static const char *const levels[NLEVELS] = {
    "................"
    "................"
    "..##..#..#..##.."
    "..##..#..#..##.."
    "..##..#SS#..##.."
    "................"
    "SS....####....SS"
    "................"
    "..##..#..#..##.."
    "..##..#..#..##.."
    "..##........##.."
    "......####......"
    "......#BB#......",

    "................"
    "................"
    ".SS..######..SS."
    "......#..#......"
    "###...#..#...###"
    "................"
    "..####....####.."
    "..#..........#.."
    "..#...SSSS...#.."
    "................"
    "##..##....##..##"
    "......####......"
    "SS....#BB#....SS",

    "................"
    "................"
    "..#..#....#..#.."
    "..#..#.SS.#..#.."
    "..#..#....#..#.."
    "SSS..........SSS"
    "....########...."
    "................"
    ".##.##....##.##."
    ".##.##....##.##."
    "................"
    "..SS..####..SS.."
    "......#BB#......",
};

/* 3x3 tank shapes facing UP; bit (v*3+u). uint16_t so reads are flash-safe. */
static const uint16_t shape_player = 0x1FA;   /* .#. / ### / ###  */
static const uint16_t shape_enemy  = 0x17A;   /* .#. / ### / #.#  */

/* ---- sounds ---------------------------------------------------------------- */
static const ps_note_t snd_shot[]   = { {1600, 1}, {1100, 1}, {0, 0} };
static const ps_note_t snd_brick[]  = { {180, 2}, {0, 0} };
static const ps_note_t snd_steel[]  = { {2400, 1}, {0, 0} };
static const ps_note_t snd_kill[]   = { {400, 2}, {220, 2}, {120, 4}, {0, 0} };
static const ps_note_t snd_hurt[]   = { {600, 3}, {400, 3}, {250, 3}, {150, 8}, {0, 0} };
static const ps_note_t snd_intro[]  = { {523, 4}, {659, 4}, {784, 4}, {659, 3}, {784, 3}, {1047, 8}, {0, 0} };
static const ps_note_t snd_clear[]  = { {784, 3}, {988, 3}, {1175, 3}, {1568, 10}, {0, 0} };
static const ps_note_t snd_over[]   = { {392, 6}, {311, 6}, {262, 6}, {196, 16}, {0, 0} };

/* ---- state (all of this has to fit in the 1.5 KB of user RAM) ------------- */
static uint32_t brick[PF_H], steel[PF_H], base_px[PF_H];
static tank_t   tk[NT];
static bullet_t bl[NT];
static boom_t   boom[NEXP];
static int state, timer, stage, loops, lives, score, hiscore, best_stage;
static int to_spawn, to_kill, spawn_timer, spawn_slot, base_alive;

/* ---- map ------------------------------------------------------------------- */
static void load_level(int n)
{
    const char *m = levels[n % NLEVELS];
    for (int r = 0; r < PF_H; r++) brick[r] = steel[r] = base_px[r] = 0;
    for (int cy = 0; cy < MAP_H; cy++) {
        for (int cx = 0; cx < MAP_W; cx++) {
            uint8_t ch = ps_flash_u8(m + cy * MAP_W + cx);
            uint32_t bits = 3u << (2 * cx);
            uint32_t *dst = ch == '#' ? brick : ch == 'S' ? steel : ch == 'B' ? base_px : 0;
            if (dst) { dst[2 * cy] |= bits; dst[2 * cy + 1] |= bits; }
        }
    }
    base_alive = 1;
}

static uint32_t span(int x, int w) { return ((1u << w) - 1u) << x; }

static int blocked_by_map(int x, int y)
{
    if (x < 0 || y < 0 || x + TS > PS_W || y + TS > PF_H) return 1;
    uint32_t m = span(x, TS);
    for (int r = y; r < y + TS; r++)
        if ((brick[r] | steel[r] | base_px[r]) & m) return 1;
    return 0;
}

static int blocked_by_tank(int self, int x, int y)
{
    for (int j = 0; j < NT; j++) {
        if (j == self || tk[j].state == T_DEAD) continue;
        if (x < tk[j].x + TS && tk[j].x < x + TS && y < tk[j].y + TS && tk[j].y < y + TS) return 1;
    }
    return 0;
}

static int free_at(int i, int x, int y) { return !blocked_by_map(x, y) && !blocked_by_tank(i, x, y); }

/* move one pixel; slides around corners by up to 1 pixel so 3-pixel tanks
 * slip into 4-pixel corridors without pixel-perfect steering */
static int try_move(int i)
{
    tank_t *t = &tk[i];
    int nx = t->x + DX[t->dir], ny = t->y + DY[t->dir];
    if (free_at(i, nx, ny)) { t->x = nx; t->y = ny; return 1; }
    for (int s = -1; s <= 1; s += 2) {
        int px = DY[t->dir] ? s : 0, py = DX[t->dir] ? s : 0;   /* perpendicular nudge */
        if (free_at(i, t->x + px, t->y + py) && free_at(i, nx + px, ny + py)) {
            t->x += px; t->y += py;
            return 1;
        }
    }
    return 0;
}

/* ---- effects ---------------------------------------------------------------- */
static void explode(int x, int y)
{
    int k = 0;
    for (int i = 1; i < NEXP; i++) if (boom[i].t < boom[k].t) k = i;
    boom[k].x = (int16_t)x; boom[k].y = (int16_t)y; boom[k].t = 12;
}

/* ---- tanks ------------------------------------------------------------------- */
static void spawn_player(void)
{
    tank_t *p = &tk[0];
    p->x = 8; p->y = 22; p->dir = UP;
    p->state = T_SPAWN; p->timer = 24; p->shield = 0; p->cool = 0;
}

static void fire(int i)
{
    tank_t *t = &tk[i];
    if (bl[i].on || t->cool) return;
    static const int tipx[4] = { 1, 3, 1, -1 }, tipy[4] = { -1, 1, 3, 1 };
    bl[i].x = (int16_t)(t->x + tipx[t->dir]);
    bl[i].y = (int16_t)(t->y + tipy[t->dir]);
    bl[i].dir = t->dir;
    bl[i].on = 1;
    t->cool = i ? 18 : 6;
    if (i == 0) ps_play(snd_shot);
}

static void game_over(void)
{
    state = OVER;
    timer = 0;
    ps_play(snd_over);
    if (score > hiscore || stage + 1 > best_stage) {
        if (score > hiscore) hiscore = score;
        if (stage + 1 > best_stage) best_stage = stage + 1;
        save_t s = { SAVE_MAGIC, (uint32_t)hiscore, (uint32_t)best_stage };
        if (ps_save_write(&s, sizeof s)) ps_log("save failed");
    }
}

static void hit_tank(int j)
{
    tank_t *t = &tk[j];
    explode(t->x + 1, t->y + 1);
    if (j == 0) {
        if (t->shield) return;
        ps_play(snd_hurt);
        t->state = T_DEAD;
        bl[0].on = 0;
        if (--lives <= 0) { game_over(); return; }
        t->timer = 30;                       /* respawn delay, counted while dead */
    } else {
        ps_play(snd_kill);
        t->state = T_DEAD;
        score += 10 * (loops + 1);
        if (--to_kill == 0) { state = CLEAR; timer = 0; ps_play(snd_clear); }
    }
}

/* destroy the bricks a bullet touches: the cell it hit plus its neighbour
 * across the bullet's path, so shots carve tank-sized holes */
static void break_bricks(int x, int y, int dir)
{
    int vertical = DY[dir] != 0;
    for (int d = -1; d <= 1; d++) {
        int px = vertical ? x + d : x, py = vertical ? y : y + d;
        if (px < 0 || px >= PS_W || py < 0 || py >= PF_H) continue;
        int cx = px >> 1, cy = py >> 1;
        uint32_t m = ~(3u << (2 * cx));
        brick[2 * cy] &= m;
        brick[2 * cy + 1] &= m;
    }
}

static void bullet_step(int i)
{
    bullet_t *b = &bl[i];
    b->x += DX[b->dir];
    b->y += DY[b->dir];
    int x = b->x, y = b->y;
    if (x < 0 || x >= PS_W || y < 0 || y >= PF_H) { b->on = 0; return; }
    uint32_t bit = 1u << x;

    if (base_px[y] & bit) {                  /* base destroyed */
        b->on = 0;
        base_alive = 0;
        explode(15, 24);
        explode(16, 25);
        game_over();
        return;
    }
    if (steel[y] & bit) { b->on = 0; if (i == 0) ps_play(snd_steel); return; }
    if (brick[y] & bit) {
        b->on = 0;
        break_bricks(x, y, b->dir);
        if (i == 0) ps_play(snd_brick);
        return;
    }
    for (int j = 0; j < NT; j++) {           /* player shoots enemies, enemies shoot the player */
        if (tk[j].state != T_ALIVE || (i == 0) == (j == 0)) continue;
        if (x >= tk[j].x && x < tk[j].x + TS && y >= tk[j].y && y < tk[j].y + TS) {
            b->on = 0;
            hit_tank(j);
            return;
        }
    }
    for (int k = 0; k < NT; k++) {           /* opposing bullets cancel out */
        if (k == i || !bl[k].on || (i == 0) == (k == 0)) continue;
        int ddx = bl[k].x - x, ddy = bl[k].y - y;
        if (ddx >= -1 && ddx <= 1 && ddy >= -1 && ddy <= 1) { b->on = 0; bl[k].on = 0; return; }
    }
}

static void update_player(void)
{
    tank_t *p = &tk[0];
    if (p->state == T_DEAD) {
        if (lives > 0 && --p->timer == 0) spawn_player();
        return;
    }
    if (p->state == T_SPAWN) {
        if (--p->timer == 0) { p->state = T_ALIVE; p->shield = 60; }
        return;
    }
    if (p->shield) p->shield--;
    if (p->cool) p->cool--;

    /* the most recently pressed direction wins; keep moving while it is held */
    static const uint32_t btn[4] = { PS_BTN_UP, PS_BTN_RIGHT, PS_BTN_DOWN, PS_BTN_LEFT };
    int want = -1;
    for (int d = 0; d < 4; d++) if (ps_pressed(btn[d])) want = d;
    if (want < 0 && ps_held(btn[p->dir])) want = p->dir;
    if (want < 0) for (int d = 0; d < 4; d++) if (ps_held(btn[d])) want = d;

    if (want >= 0) {
        if (want != p->dir) { p->dir = (uint8_t)want; p->move = 0; }   /* turn first */
        else if (++p->move >= 2) { p->move = 0; try_move(0); }
    }
    if (ps_pressed(PS_BTN_FIRE)) fire(0);
}

static int toward(int from, int to, int neg, int pos) { return to < from ? neg : pos; }

static void update_enemy(int i)
{
    tank_t *t = &tk[i];
    if (t->state == T_SPAWN) {
        if (--t->timer == 0) { t->state = T_ALIVE; t->dir = DOWN; t->cool = 20; }
        return;
    }
    if (t->state != T_ALIVE) return;
    if (t->cool) t->cool--;

    int speed = loops ? 2 : 3;
    if (++t->move >= speed) {
        t->move = 0;
        if (ps_rand_range(0, 23) == 0 || !try_move(i)) {
            int r = ps_rand_range(0, 9);
            if (r < 3)       t->dir = DOWN;                                     /* head for the base */
            else if (r < 5)  t->dir = (uint8_t)toward(t->x, 14, LEFT, RIGHT);
            else if (r < 7 && tk[0].state == T_ALIVE)
                             t->dir = (uint8_t)toward(t->x, tk[0].x, LEFT, RIGHT); /* hunt the player */
            else             t->dir = (uint8_t)ps_rand_range(0, 3);
        }
    }

    /* fire now and then, and almost always when lined up with the player */
    int lined = 0;
    if (tk[0].state == T_ALIVE) {
        int ddx = tk[0].x - t->x, ddy = tk[0].y - t->y;
        if (ddx >= -1 && ddx <= 1) lined = (t->dir == DOWN && ddy > 0) || (t->dir == UP && ddy < 0);
        if (ddy >= -1 && ddy <= 1) lined |= (t->dir == RIGHT && ddx > 0) || (t->dir == LEFT && ddx < 0);
    }
    if (ps_rand_range(0, lined ? 3 : 30) == 0) fire(i);
}

static void update_spawner(void)
{
    if (to_spawn == 0) return;
    if (spawn_timer) { spawn_timer--; return; }
    static const int sx[3] = { 0, 14, 29 };
    for (int i = 1; i < NT; i++) {
        if (tk[i].state != T_DEAD) continue;
        for (int tries = 0; tries < 3; tries++) {
            int x = sx[spawn_slot];
            spawn_slot = (spawn_slot + 1) % 3;
            if (free_at(i, x, 0)) {
                tk[i].x = (int16_t)x; tk[i].y = 0;
                tk[i].state = T_SPAWN; tk[i].timer = 24; tk[i].move = 0;
                to_spawn--;
                spawn_timer = loops ? 40 : 60;
                return;
            }
        }
        return;
    }
}

static void start_stage(void)
{
    load_level(stage);
    for (int i = 0; i < NT; i++) { tk[i].state = T_DEAD; bl[i].on = 0; }
    for (int i = 0; i < NEXP; i++) boom[i].t = 0;
    to_spawn = to_kill = 8 + 2 * (stage % NLEVELS);     /* 8, 10, 12 */
    spawn_timer = 20;
    spawn_slot = 0;
    spawn_player();
    state = INTRO;
    timer = 0;
    ps_play(snd_intro);
}

/* ---- drawing ---------------------------------------------------------------- */
static void draw_tank(int x, int y, int dir, uint16_t shape)
{
    for (int v = 0; v < 3; v++) {
        for (int u = 0; u < 3; u++) {
            if (!((shape >> (v * 3 + u)) & 1u)) continue;
            int sx, sy;                          /* rotate the UP shape */
            switch (dir) {
            case RIGHT: sx = 2 - v; sy = u;     break;
            case DOWN:  sx = 2 - u; sy = 2 - v; break;
            case LEFT:  sx = v;     sy = 2 - u; break;
            default:    sx = u;     sy = v;     break;
            }
            ps_pset(x + sx, PF_Y + y + sy, 1);
        }
    }
}

static void draw_world(void)
{
    uint32_t f = ps_frame_count();

    /* HUD: score, enemies left (dots), lives */
    ps_number(0, 0, score);
    for (int k = 0; k < to_kill && k < 12; k++)
        ps_pset(13 + (k % 6) * 2, (k < 6) ? 1 : 3, 1);
    draw_tank(25, 1 - PF_Y, UP, shape_player);
    ps_number(29, 0, lives > 0 ? lives : 0);
    ps_hline(0, 31, PF_Y - 1, 1);

    /* map: bricks solid, steel with a checker texture so the two read differently */
    for (int r = 0; r < PF_H; r++) {
        uint32_t tex = (r & 1) ? 0xAAAAAAAAu : 0x55555555u;
        ps_fb[PF_Y + r] |= brick[r] | (steel[r] & tex);
    }
    /* base: a little flag, or rubble once destroyed */
    for (int r = 0; r < PF_H; r++) {
        if (!base_px[r]) continue;
        int x0 = 0;
        while (!((base_px[r] >> x0) & 1u)) x0++;
        int top = (r == 0 || !base_px[r - 1]);
        uint32_t pat = base_alive ? (top ? 0x6u : 0xFu) : (top ? 0x0u : 0x5u);
        ps_fb[PF_Y + r] |= pat << x0;
    }

    /* tanks */
    for (int i = 0; i < NT; i++) {
        tank_t *t = &tk[i];
        if (t->state == T_SPAWN) {                       /* twinkling spawn marker */
            int cx = t->x + 1, cy = PF_Y + t->y + 1;
            ps_pset(cx, cy, 1);
            if (f & 4) { ps_pset(cx - 1, cy, 1); ps_pset(cx + 1, cy, 1); ps_pset(cx, cy - 1, 1); ps_pset(cx, cy + 1, 1); }
            else       { ps_pset(cx - 1, cy - 1, 1); ps_pset(cx + 1, cy + 1, 1); ps_pset(cx + 1, cy - 1, 1); ps_pset(cx - 1, cy + 1, 1); }
        } else if (t->state == T_ALIVE) {
            if (i == 0 && t->shield && (f & 2)) continue;     /* blink while shielded */
            draw_tank(t->x, t->y, t->dir, i ? shape_enemy : shape_player);
        }
    }

    /* bullets */
    for (int i = 0; i < NT; i++) if (bl[i].on) ps_pset(bl[i].x, PF_Y + bl[i].y, 1);

    /* explosions: a growing ring */
    for (int i = 0; i < NEXP; i++) {
        if (!boom[i].t) continue;
        int r = boom[i].t > 8 ? 1 : boom[i].t > 4 ? 2 : 3;
        int cx = boom[i].x, cy = PF_Y + boom[i].y;
        ps_pxor(cx - r, cy); ps_pxor(cx + r, cy); ps_pxor(cx, cy - r); ps_pxor(cx, cy + r);
        if (r > 1) { ps_pxor(cx - r + 1, cy - r + 1); ps_pxor(cx + r - 1, cy + r - 1);
                     ps_pxor(cx + r - 1, cy - r + 1); ps_pxor(cx - r + 1, cy + r - 1); }
    }
}

static void centered(int y, const char *s) { ps_text((PS_W - ps_text_width(s)) / 2, y, s); }

/* ---- main -------------------------------------------------------------------- */
int main(void)
{
    ps_init();

    save_t s;
    ps_save_read(&s, sizeof s);
    if (s.magic == SAVE_MAGIC) { hiscore = (int)s.hiscore; best_stage = (int)s.best_stage; }

    state = TITLE;
    int hold_down = 0;

    for (;;) {
        ps_input_update();
        ps_clear();
        timer++;

        switch (state) {
        case TITLE:
            if (ps_pressed(PS_BTN_FIRE)) {
                score = 0; lives = 3; stage = 0; loops = 0;
                ps_srand(ps_rand() ^ ps_frame_count());
                start_stage();
                break;
            }
            hold_down = ps_held(PS_BTN_DOWN) ? hold_down + 1 : 0;
            if (hold_down > PS_FPS) return 0;                       /* back to the menu */
            if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked()) ps_standby();
            centered(1, "TANK");
            centered(7, "BATTLE");
            draw_tank(5, 7, RIGHT, shape_player);                   /* little duel */
            draw_tank(24, 7, LEFT, shape_enemy);
            if ((timer >> 1) % 12 < 8) ps_pset(9 + (timer >> 1) % 12, PF_Y + 8, 1);
            ps_text(1, 19, "HI");
            ps_number(10, 19, hiscore);
            if (timer & 16) centered(26, "FIRE");
            break;

        case INTRO:
            centered(6, "STAGE");
            ps_number(14, 14, stage + 1);
            if (timer > 2 * PS_FPS) { state = PLAY; timer = 0; }
            break;

        case PLAY:
        case CLEAR:
        case OVER:
            if (state == PLAY) {
                update_player();
                for (int i = 1; i < NT; i++) update_enemy(i);
                update_spawner();
            }
            if (state != OVER || timer < PS_FPS) {
                for (int step = 0; step < 2; step++)            /* bullets move 2 px a frame */
                    for (int i = 0; i < NT; i++)
                        if (bl[i].on && state != OVER) bullet_step(i);
                for (int i = 0; i < NEXP; i++) if (boom[i].t) boom[i].t--;
            }

            if (state == OVER && timer > PS_FPS) {
                centered(3, "GAME");
                centered(9, "OVER");
                ps_number(1, 17, score);
                if (score >= hiscore && score > 0) ps_text(17, 17, "BEST");
                if (timer > 2 * PS_FPS && (timer & 16)) centered(26, "FIRE");
                if (timer > 2 * PS_FPS && ps_pressed(PS_BTN_FIRE)) { state = TITLE; timer = 0; }
                break;
            }
            draw_world();
            if (state == CLEAR && timer > 2 * PS_FPS) {
                stage++;
                if (stage % NLEVELS == 0) loops++;                  /* faster enemies each loop */
                if (lives < 9) lives++;                             /* bonus life per stage */
                start_stage();
            }
            break;
        }

        ps_present();
        ps_wait_frame();
    }
}
