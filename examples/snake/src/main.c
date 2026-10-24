/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * snake - a complete little game for PocketSim DevKit
 *
 *   title screen : FIRE = start, hold DOWN for 1 s = back to the menu
 *   in game      : d-pad steers, FIRE pauses
 *   high score is saved to flash inside the game's own file
 */
#include <ps/ps.h>

#define GW      16          /* grid: 16 x 13 cells of 2x2 pixels */
#define GH      13
#define OY      6           /* playfield starts at pixel row 6 */
#define MAXLEN  (GW * GH)

enum { TITLE, PLAY, PAUSE, OVER };

typedef struct {
    uint32_t magic;
    uint32_t hiscore;
    uint32_t games;
} save_t;
#define SAVE_MAGIC 0x534E4B31u   /* "SNK1" */

static uint8_t body[MAXLEN];     /* ring buffer of cell indices, head at 'head' */
static int head, len, dir, next_dir, food;
static int score, hiscore, games, speed, tick;
static int state;
static uint8_t grid[MAXLEN];     /* 1 = snake occupies the cell */

static const ps_note_t snd_start[] = { {392, 3}, {523, 3}, {659, 3}, {784, 5}, {0, 0} };
static const ps_note_t snd_die[]   = { {392, 4}, {330, 4}, {262, 4}, {196, 10}, {0, 0} };
static const ps_note_t snd_best[]  = { {784, 3}, {0, 2}, {784, 3}, {1047, 8}, {0, 0} };
static const ps_note_t snd_eat[]   = { {988, 2}, {1319, 3}, {0, 0} };              /* ate a dot  */
static const ps_note_t snd_faster[] = { {1047, 2}, {1319, 2}, {1568, 2}, {2093, 4}, {0, 0} }; /* speed up */

static const int dx[4] = { 1, 0, -1, 0 };   /* right, down, left, up */
static const int dy[4] = { 0, 1, 0, -1 };

static void load_save(void)
{
    save_t s;
    ps_save_read(&s, sizeof s);
    if (s.magic == SAVE_MAGIC) { hiscore = (int)s.hiscore; games = (int)s.games; }
    else                       { hiscore = 0; games = 0; }
}

static void write_save(void)
{
    save_t s = { SAVE_MAGIC, (uint32_t)hiscore, (uint32_t)games };
    if (ps_save_write(&s, sizeof s) != 0) ps_log("save failed");
}

static void place_food(void)
{
    int free_cells = MAXLEN - len;
    if (free_cells <= 0) { food = -1; return; }
    int k = ps_rand_range(0, free_cells - 1);
    for (int i = 0; i < MAXLEN; i++) {
        if (!grid[i] && k-- == 0) { food = i; return; }
    }
}

static void new_game(void)
{
    ps_memset(grid, 0, sizeof grid);
    len = 4;
    head = len - 1;
    for (int i = 0; i < len; i++) {
        body[i] = (uint8_t)(6 * GW + 3 + i);
        grid[body[i]] = 1;
    }
    dir = next_dir = 0;
    score = 0;
    speed = 6;          /* frames per step */
    tick = 0;
    place_food();
    ps_play(snd_start);
    state = PLAY;
}

static void game_over(void)
{
    games++;
    if (score > hiscore) {
        hiscore = score;
        ps_play(snd_best);
    } else {
        ps_play(snd_die);
    }
    write_save();
    state = OVER;
    tick = 0;
}

static void step(void)
{
    dir = next_dir;
    int h = body[head];
    int x = h % GW + dx[dir], y = h / GW + dy[dir];
    if (x < 0 || x >= GW || y < 0 || y >= GH) { game_over(); return; }
    int n = y * GW + x;
    int tail = body[(head - len + 1 + MAXLEN) % MAXLEN];
    int grow = (n == food);
    if (grid[n] && !(n == tail && !grow)) { game_over(); return; }
    if (!grow) grid[tail] = 0;
    head = (head + 1) % MAXLEN;
    body[head] = (uint8_t)n;
    grid[n] = 1;
    if (grow) {
        len++;
        score++;
        if (speed > 2 && score % 5 == 0) {
            speed--;
            ps_play(snd_faster);        /* every 5 dots: longer jingle */
        } else {
            ps_play(snd_eat);           /* normal dot: quick two-note chirp */
        }
        place_food();
    }
}

static void cell(int c, int black)
{
    ps_fill_rect((c % GW) * 2, OY + (c / GW) * 2, 2, 2, black);
}

static void draw_field(void)
{
    ps_number(0, 0, score);
    ps_hline(0, 31, OY - 1, 1);
    for (int i = 0; i < len; i++) cell(body[(head - i + MAXLEN) % MAXLEN], 1);
    if (food >= 0 && (ps_frame_count() & 8)) cell(food, 1);   /* blinking food */
    else if (food >= 0) {
        ps_pset((food % GW) * 2, OY + (food / GW) * 2, 1);
        ps_pset((food % GW) * 2 + 1, OY + (food / GW) * 2 + 1, 1);
    }
}

static void centered(int y, const char *s)
{
    ps_text((PS_W - ps_text_width(s)) / 2, y, s);
}

int main(void)
{
    ps_init();
    load_save();
    state = TITLE;
    int hold_down = 0;

    for (;;) {
        ps_input_update();
        ps_clear();

        switch (state) {
        case TITLE:
            if (ps_pressed(PS_BTN_FIRE)) { new_game(); break; }
            hold_down = ps_held(PS_BTN_DOWN) ? hold_down + 1 : 0;
            if (hold_down > PS_FPS) return 0;                  /* back to the menu */
            if (ps_idle_frames() > 20 * PS_FPS && !ps_is_docked()) ps_standby();
            centered(3, "SNAKE");
            ps_hline(6, 25, 9, 1);
            ps_text(1, 13, "HI");
            ps_number(10, 13, hiscore);
            ps_text(1, 19, "GAMES");
            ps_number(22, 19, games);
            if (ps_frame_count() & 16) centered(26, "FIRE");
            break;

        case PLAY:
            if (ps_pressed(PS_BTN_RIGHT) && dir != 2) next_dir = 0;
            if (ps_pressed(PS_BTN_DOWN)  && dir != 3) next_dir = 1;
            if (ps_pressed(PS_BTN_LEFT)  && dir != 0) next_dir = 2;
            if (ps_pressed(PS_BTN_UP)    && dir != 1) next_dir = 3;
            if (ps_pressed(PS_BTN_FIRE)) { state = PAUSE; break; }
            if (++tick >= speed) { tick = 0; step(); }
            draw_field();
            break;

        case PAUSE:
            if (ps_pressed(PS_BTN_FIRE)) state = PLAY;
            draw_field();
            ps_fill_rect(4, 14, 24, 9, 0);
            ps_rect(4, 14, 24, 9, 1);
            centered(16, "PAUSE");
            break;

        case OVER:
            tick++;
            centered(3, "GAME");
            centered(9, "OVER");
            ps_number(1, 17, score);
            if (score >= hiscore && score > 0) ps_text(14, 17, "BEST");
            if (tick > PS_FPS && ps_pressed(PS_BTN_FIRE)) state = TITLE;
            if (tick > PS_FPS && (ps_frame_count() & 16)) centered(26, "FIRE");
            break;
        }

        ps_present();
        ps_wait_frame();
    }
}
