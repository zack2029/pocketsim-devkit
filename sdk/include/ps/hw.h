/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * ps/hw.h - PocketStation (SCPH-4000) hardware register map
 *
 * Source: psx-spx "Pocketstation" chapter (nocash / psx-spx.consoledev.net).
 * All I/O registers are 32-bit and should be accessed as 32-bit words.
 *
 * IMPORTANT BUS RULE: FLASH (0x02000000 / 0x08000000) and BIOS ROM can only be
 * read with 16-bit or 32-bit loads. An 8-bit load (LDRB) from FLASH returns
 * garbage. See ps_flash_u8() in ps/ps.h and the README "Gotchas" section.
 */
#ifndef PS_HW_H
#define PS_HW_H

#include <stdint.h>

#define PS_REG(addr) (*(volatile uint32_t *)(addr))

/* ---- Memory map --------------------------------------------------------- */
#define PS_RAM_BASE          0x00000000u  /* 2 KB, 0x000-0x1FF kernel, 0x200-0x7FF user */
#define PS_USER_RAM_BASE     0x00000200u
#define PS_USER_RAM_END      0x00000800u  /* user stack top */
#define PS_FLASH_VIRT_BASE   0x02000000u  /* currently executed file, title sector first */
#define PS_BIOS_BASE         0x04000000u  /* 16 KB kernel + GUI */
#define PS_FLASH_PHYS_BASE   0x08000000u  /* 128 KB physical flash */
#define PS_FLASH_SECTOR_SIZE 0x80u        /* 128-byte flash sector / memcard frame */
#define PS_BLOCK_SIZE        0x2000u      /* one memory card block */

/* ---- Flash / memory control -------------------------------------------- */
#define F_CTRL        PS_REG(0x06000000u)
#define F_STAT        PS_REG(0x06000004u)
#define F_BANK_FLG    PS_REG(0x06000008u)
#define F_WAIT1       PS_REG(0x0600000Cu)
#define F_WAIT2       PS_REG(0x06000010u)
#define F_BANK_VAL(n) PS_REG(0x06000100u + 4u * (n))
#define F_SN_LO       (*(volatile uint16_t *)0x06000300u) /* 16-bit access only */
#define F_SN_HI       (*(volatile uint16_t *)0x06000302u)
#define F_CAL         (*(volatile uint16_t *)0x06000308u)

/* ---- Interrupt controller ----------------------------------------------- */
#define INT_LATCH     PS_REG(0x0A000000u)  /* R: latched requests            */
#define INT_INPUT     PS_REG(0x0A000004u)  /* R: raw levels (buttons, dock)  */
#define INT_MASK      PS_REG(0x0A000008u)  /* R: current enable mask         */
#define INT_MASK_SET  PS_REG(0x0A000008u)  /* W: 1 = enable                  */
#define INT_MASK_CLR  PS_REG(0x0A00000Cu)  /* W: 1 = disable                 */
#define INT_ACK       PS_REG(0x0A000010u)  /* W: 1 = acknowledge             */

/* INT_* bit numbers */
#define IRQ_BTN_FIRE   (1u << 0)
#define IRQ_BTN_RIGHT  (1u << 1)
#define IRQ_BTN_LEFT   (1u << 2)
#define IRQ_BTN_DOWN   (1u << 3)
#define IRQ_BTN_UP     (1u << 4)
#define IRQ_COM        (1u << 6)   /* FIQ */
#define IRQ_TIMER0     (1u << 7)
#define IRQ_TIMER1     (1u << 8)
#define IRQ_RTC        (1u << 9)
#define IRQ_BATT_LOW   (1u << 10)
#define IRQ_DOCKED     (1u << 11)  /* level: 1 = inserted in PSX */
#define IRQ_IR_RX      (1u << 12)
#define IRQ_TIMER2     (1u << 13)  /* FIQ */
#define IRQ_BUTTONS    0x1Fu

/* ---- Timers (3 channels, 16-bit down counters) -------------------------- */
#define T_RELOAD(n)   PS_REG(0x0A800000u + 0x10u * (n))
#define T_COUNT(n)    PS_REG(0x0A800004u + 0x10u * (n))
#define T_MODE(n)     PS_REG(0x0A800008u + 0x10u * (n))
#define T_DIV2        0u
#define T_DIV32       1u
#define T_DIV512      2u
#define T_ENABLE      (1u << 2)

/* ---- Clock / power ------------------------------------------------------ */
#define CLK_MODE      PS_REG(0x0B000000u)  /* bits0-3 speed, bit4 ready (R) */
#define CLK_STOP      PS_REG(0x0B000004u)  /* write 1 = stop until IRQ     */

/* CLK_MODE speed values */
#define PS_CPU_63KHZ  1
#define PS_CPU_127KHZ 2
#define PS_CPU_254KHZ 3
#define PS_CPU_508KHZ 4
#define PS_CPU_1MHZ   5
#define PS_CPU_2MHZ   6
#define PS_CPU_4MHZ   7   /* default for games */
#define PS_CPU_8MHZ   8   /* needs flash waitstates; drains battery */

/* ---- Real-time clock ---------------------------------------------------- */
#define RTC_MODE      PS_REG(0x0B800000u)
#define RTC_ADJUST    PS_REG(0x0B800004u)
#define RTC_TIME      PS_REG(0x0B800008u)  /* BCD ss mm hh dow   */
#define RTC_DATE      PS_REG(0x0B80000Cu)  /* BCD dd mm .. yy    */

/* ---- PSX link (memory card port) ---------------------------------------- */
#define COM_MODE      PS_REG(0x0C000000u)
#define COM_STAT1     PS_REG(0x0C000004u)
#define COM_DATA      PS_REG(0x0C000008u)
#define COM_CTRL1     PS_REG(0x0C000010u)
#define COM_STAT2     PS_REG(0x0C000014u)
#define COM_CTRL2     PS_REG(0x0C000018u)

/* ---- Infrared ----------------------------------------------------------- */
#define IRDA_MODE     PS_REG(0x0C800000u)
#define IRDA_DATA     PS_REG(0x0C800004u)

/* ---- LCD ---------------------------------------------------------------- */
#define LCD_MODE      PS_REG(0x0D000000u)
#define LCD_CAL       PS_REG(0x0D000004u)
#define LCD_VRAM      ((volatile uint32_t *)0x0D000100u)  /* 32 rows x 32 bits */

#define LCD_ROWS_ALL  0u
#define LCD_CPEN      (1u << 3)
#define LCD_RATE_64HZ (1u << 4)
#define LCD_RATE_32HZ (2u << 4)
#define LCD_RATE_16HZ (3u << 4)
#define LCD_ON        (1u << 6)
#define LCD_ROTATE    (1u << 7)   /* set when docked in the PSX */

/* ---- GPIO / LED / speaker / battery ------------------------------------- */
#define IOP_CTRL      PS_REG(0x0D800000u)
#define IOP_STOP      PS_REG(0x0D800004u)  /* write 1 bits = set   (LED off, speaker off) */
#define IOP_START     PS_REG(0x0D800008u)  /* write 1 bits = clear (LED on,  speaker on)  */
#define IOP_DATA      PS_REG(0x0D80000Cu)
#define IOP_LED       (1u << 1)
#define IOP_SPEAKER   (1u << 5)

#define DAC_CTRL      PS_REG(0x0D800010u)  /* bit0 = audio on */
#define DAC_DATA      PS_REG(0x0D800014u)  /* signed level in the upper bits of the low halfword */
#define BATT_CTRL     PS_REG(0x0D800020u)

/* ---- Kernel RAM locations (read-mostly; restore before exit) ------------ */
#define PS_KRAM_UNDEF_VECTOR  ((volatile uint32_t *)0x00000004u)

#endif /* PS_HW_H */
