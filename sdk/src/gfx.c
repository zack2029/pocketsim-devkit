/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * gfx.c - 1bpp framebuffer drawing and a 3x5 font.
 * Framebuffer layout matches LCD VRAM: one uint32_t per row, bit0 = left pixel,
 * 1 = black.
 */
#include <ps/ps.h>

uint32_t ps_fb[PS_H];

#define INB(x, y) ((unsigned)(x) < PS_W && (unsigned)(y) < PS_H)

void ps_clear(void) { for (int i = 0; i < PS_H; i++) ps_fb[i] = 0; }
void ps_fill(int black) { uint32_t v = black ? 0xFFFFFFFFu : 0; for (int i = 0; i < PS_H; i++) ps_fb[i] = v; }

void ps_pset(int x, int y, int black)
{
    if (!INB(x, y)) return;
    if (black) ps_fb[y] |= 1u << x; else ps_fb[y] &= ~(1u << x);
}
int  ps_pget(int x, int y) { return INB(x, y) ? (int)((ps_fb[y] >> x) & 1u) : 0; }
void ps_pxor(int x, int y) { if (INB(x, y)) ps_fb[y] ^= 1u << x; }

/* bits x0..x1 inclusive, already clipped to 0..31 */
static uint32_t span(int x0, int x1)
{
    uint32_t hi = (x1 >= 31) ? 0xFFFFFFFFu : ((1u << (x1 + 1)) - 1u);
    uint32_t lo = (1u << x0) - 1u;
    return hi & ~lo;
}

void ps_hline(int x0, int x1, int y, int black)
{
    if ((unsigned)y >= PS_H) return;
    if (x0 > x1) { int t = x0; x0 = x1; x1 = t; }
    if (x1 < 0 || x0 >= PS_W) return;
    if (x0 < 0) x0 = 0;
    if (x1 >= PS_W) x1 = PS_W - 1;
    uint32_t m = span(x0, x1);
    if (black) ps_fb[y] |= m; else ps_fb[y] &= ~m;
}

void ps_vline(int x, int y0, int y1, int black)
{
    if ((unsigned)x >= PS_W) return;
    if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
    if (y0 < 0) y0 = 0;
    if (y1 >= PS_H) y1 = PS_H - 1;
    for (int y = y0; y <= y1; y++) ps_pset(x, y, black);
}

void ps_line(int x0, int y0, int x1, int y1, int black)
{
    int dx = x1 > x0 ? x1 - x0 : x0 - x1, sx = x0 < x1 ? 1 : -1;
    int dy = y1 > y0 ? y0 - y1 : y1 - y0, sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;
    for (;;) {
        ps_pset(x0, y0, black);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
}

void ps_rect(int x, int y, int w, int h, int black)
{
    if (w <= 0 || h <= 0) return;
    ps_hline(x, x + w - 1, y, black);
    ps_hline(x, x + w - 1, y + h - 1, black);
    ps_vline(x, y, y + h - 1, black);
    ps_vline(x + w - 1, y, y + h - 1, black);
}

void ps_fill_rect(int x, int y, int w, int h, int black)
{
    for (int j = 0; j < h; j++) ps_hline(x, x + w - 1, y + j, black);
}

void ps_invert_rect(int x, int y, int w, int h)
{
    int x0 = x < 0 ? 0 : x, x1 = x + w - 1 >= PS_W ? PS_W - 1 : x + w - 1;
    if (w <= 0 || x1 < x0) return;
    uint32_t m = span(x0, x1);
    for (int j = 0; j < h; j++) {
        int yy = y + j;
        if ((unsigned)yy < PS_H) ps_fb[yy] ^= m;
    }
}

void ps_blit(int x, int y, const uint32_t *rows, int w, int h, int mode)
{
    uint32_t wmask = (w >= 32) ? 0xFFFFFFFFu : ((1u << w) - 1u);
    for (int j = 0; j < h; j++) {
        int yy = y + j;
        if ((unsigned)yy >= PS_H) continue;
        uint32_t r = rows[j] & wmask, m;
        if (x >= 0) { if (x >= 32) continue; m = r << x; }
        else        { if (x <= -32) continue; m = r >> -x; }
        if (mode == PS_BLIT_SET) ps_fb[yy] |= m;
        else if (mode == PS_BLIT_CLR) ps_fb[yy] &= ~m;
        else ps_fb[yy] ^= m;
    }
}

void ps_draw_image(const uint32_t img[PS_H])
{
    for (int i = 0; i < PS_H; i++) ps_fb[i] = img[i];
}

/* ---- 3x5 font ----------------------------------------------------------- */
/* Each glyph: 5 rows x 3 bits; row value bit2 = left column. Packed into 15
 * bits, row 0 in bits 14..12. Stored as uint16_t so reads are flash-safe. */
#define G(a, b, c, d, e) (uint16_t)(((a) << 12) | ((b) << 9) | ((c) << 6) | ((d) << 3) | (e))

static const uint16_t font35[64] = {
    /* ' ' */ G(0,0,0,0,0), /* ! */ G(2,2,2,0,2), /* " */ G(5,5,0,0,0), /* # */ G(5,7,5,7,5),
    /* $ */ G(3,6,2,3,6),   /* % */ G(5,1,2,4,5), /* & */ G(2,5,2,5,3), /* ' */ G(2,2,0,0,0),
    /* ( */ G(1,2,2,2,1),   /* ) */ G(4,2,2,2,4), /* * */ G(5,2,7,2,5), /* + */ G(0,2,7,2,0),
    /* , */ G(0,0,0,2,4),   /* - */ G(0,0,7,0,0), /* . */ G(0,0,0,0,2), /* / */ G(1,1,2,4,4),
    /* 0 */ G(7,5,5,5,7),   /* 1 */ G(2,6,2,2,7), /* 2 */ G(7,1,7,4,7), /* 3 */ G(7,1,7,1,7),
    /* 4 */ G(5,5,7,1,1),   /* 5 */ G(7,4,7,1,7), /* 6 */ G(7,4,7,5,7), /* 7 */ G(7,1,2,2,2),
    /* 8 */ G(7,5,7,5,7),   /* 9 */ G(7,5,7,1,7), /* : */ G(0,2,0,2,0), /* ; */ G(0,2,0,2,4),
    /* < */ G(1,2,4,2,1),   /* = */ G(0,7,0,7,0), /* > */ G(4,2,1,2,4), /* ? */ G(6,1,2,0,2),
    /* @ */ G(7,5,7,4,7),   /* A */ G(2,5,7,5,5), /* B */ G(6,5,6,5,6), /* C */ G(3,4,4,4,3),
    /* D */ G(6,5,5,5,6),   /* E */ G(7,4,6,4,7), /* F */ G(7,4,6,4,4), /* G */ G(3,4,5,5,3),
    /* H */ G(5,5,7,5,5),   /* I */ G(7,2,2,2,7), /* J */ G(1,1,1,5,2), /* K */ G(5,5,6,5,5),
    /* L */ G(4,4,4,4,7),   /* M */ G(5,7,7,5,5), /* N */ G(6,5,5,5,5), /* O */ G(2,5,5,5,2),
    /* P */ G(6,5,6,4,4),   /* Q */ G(2,5,5,7,3), /* R */ G(6,5,6,5,5), /* S */ G(3,4,2,1,6),
    /* T */ G(7,2,2,2,2),   /* U */ G(5,5,5,5,7), /* V */ G(5,5,5,5,2), /* W */ G(5,5,7,7,5),
    /* X */ G(5,5,2,5,5),   /* Y */ G(5,5,2,2,2), /* Z */ G(7,1,2,4,7), /* [ */ G(3,2,2,2,3),
    /* \ */ G(4,4,2,1,1),   /* ] */ G(6,2,2,2,6), /* ^ */ G(2,5,0,0,0), /* _ */ G(0,0,0,0,7),
};

static void draw_glyph(int x, int y, uint32_t c)
{
    if (c >= 'a' && c <= 'z') c -= 32;
    if (c < 32 || c > 95) c = '?';
    uint32_t g = font35[c - 32];
    for (int row = 0; row < 5; row++) {
        uint32_t bits = (g >> (12 - 3 * row)) & 7u;
        for (int col = 0; col < 3; col++)
            if (bits & (4u >> col)) ps_pset(x + col, y + row, 1);
    }
}

int ps_text(int x, int y, const char *s)
{
    for (size_t i = 0; ; i++) {
        uint8_t c = ps_flash_u8(s + i);   /* string literals live in flash */
        if (!c) break;
        if (c == '\n') { y += 6; continue; }
        draw_glyph(x, y, c);
        x += 4;
    }
    return x;
}

int ps_text_width(const char *s)
{
    int n = (int)ps_strlen(s);
    return n ? n * 4 - 1 : 0;
}

int ps_number(int x, int y, int32_t n)
{
    char buf[12];
    int i = 11;
    uint32_t u = n < 0 ? (uint32_t)(-n) : (uint32_t)n;
    buf[i] = 0;
    do { buf[--i] = (char)('0' + u % 10u); u /= 10u; } while (u);
    if (n < 0) buf[--i] = '-';
    return ps_text(x, y, &buf[i]);
}
