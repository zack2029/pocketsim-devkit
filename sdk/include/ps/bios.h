/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * ps/bios.h - PocketStation kernel (BIOS) calls via SWI
 *
 * Parameters go in r0..r2, the result comes back in r0; the kernel preserves
 * every other register. Numbers and semantics follow psx-spx (nocash), with
 * Sony's official names in comments where known.
 *
 * Never call these from inside an IRQ/FIQ callback: the kernel shares one
 * stack between SWI and IRQ handling.
 */
#ifndef PS_BIOS_H
#define PS_BIOS_H

#include <stdint.h>

#define PS__SWI(num, a, b, c) __extension__({                               \
    register uint32_t _r0 __asm__("r0") = (uint32_t)(a);                    \
    register uint32_t _r1 __asm__("r1") = (uint32_t)(b);                    \
    register uint32_t _r2 __asm__("r2") = (uint32_t)(c);                    \
    __asm__ volatile("svc %[n]"                                             \
                     : "+r"(_r0)                                            \
                     : [n] "I"(num), "r"(_r1), "r"(_r2)                     \
                     : "memory", "cc");                                     \
    _r0; })

/* Callback slots for bios_set_callback() */
#define PS_CB_SWI2      0  /* code run privileged via bios_custom_swi2() */
#define PS_CB_IRQ       1  /* IRQ hook: must preserve r2-r11, return with BX LR */
#define PS_CB_FIQ       2  /* FIQ hook (timer 2 / COM)                          */
#define PS_CB_DOWNLOAD  3  /* PSX is about to transfer data (mute sound)        */

/* SWI 01h - SetCallbacks (official: "Set user callback"). Returns old proc. */
static inline uint32_t bios_set_callback(uint32_t slot, void (*proc)(void))
{ return PS__SWI(0x01, slot, (uint32_t)proc, 0); }

/* SWI 02h - run the SWI2 callback in supervisor mode. */
static inline uint32_t bios_custom_swi2(uint32_t a, uint32_t b, uint32_t c)
{ return PS__SWI(0x02, a, b, c); }

/* SWI 03h - write 128 bytes to a sector of the running file (sector 0 = title
 * sector). 'src' must be in RAM. Returns 0 = ok, 1 = failed. */
static inline uint32_t bios_flash_write_virtual(uint32_t sector, const void *src)
{ return PS__SWI(0x03, sector, (uint32_t)src, 0); }

/* SWI 04h - set CPU clock (PS_CPU_xxx). Also programs flash waitstates.
 * Returns previous speed. */
static inline uint32_t bios_set_cpu_speed(uint32_t speed)
{ return PS__SWI(0x04, speed, 0, 0); }

/* SWI 05h - re-sense docking, re-enable communication. GUI calls it per frame. */
static inline void bios_sense_auto_com(void) { (void)PS__SWI(0x05, 0, 0, 0); }

/* SWI 06h - pointer to the 32-bit ComFlags word (usually 0x000000C0). */
static inline volatile uint32_t *bios_get_com_flags(void)
{ return (volatile uint32_t *)PS__SWI(0x06, 0, 0, 0); }
#define PS_COMFLAG_DOCKED_COM   (1u << 9)   /* comms on and docked: blocks DoExecute */
#define PS_COMFLAG_START_REQ    (1u << 11)  /* PSX asked us to start another file   */

/* SWI 07h - which docking side effects the kernel handles automatically. */
#define PS_AUTODOCK_DAC    (1u << 16)
#define PS_AUTODOCK_IRDA   (1u << 17)
#define PS_AUTODOCK_LCD    (1u << 18)
static inline uint32_t bios_change_auto_docking(uint32_t flags)
{ return PS__SWI(0x07, flags, 0, 0); }

/* SWI 08h - PrepareExecute(flag, dir_index, param). dir_index 0 = GUI. */
static inline uint32_t bios_prepare_execute(uint32_t flag, uint32_t dir_index, uint32_t param)
{ return PS__SWI(0x08, flag, dir_index, param); }

/* SWI 09h - DoExecute. Only returns on failure (e.g. while docked with comms on). */
static inline uint32_t bios_do_execute(uint32_t snapshot_flag)
{ return PS__SWI(0x09, snapshot_flag, 0, 0); }

/* SWI 0Ah - 32-bit flash serial number. */
static inline uint32_t bios_read_serial(void) { return PS__SWI(0x0A, 0, 0, 0); }

/* SWI 0Ch/0Dh/0Eh - BCD date/time.
 * date: 0xYYYYMMDD (with century), time: 0xWWHHMMSS (WW 1=Sun..7=Sat). */
static inline void     bios_set_date_time(uint32_t date, uint32_t time) { (void)PS__SWI(0x0C, date, time, 0); }
static inline uint32_t bios_get_date(void) { return PS__SWI(0x0D, 0, 0, 0); }
static inline uint32_t bios_get_time(void) { return PS__SWI(0x0E, 0, 0, 0); }

/* SWI 10h - write 128 bytes to an absolute flash sector (0..0x3FF). Dangerous. */
static inline uint32_t bios_flash_write_physical(uint32_t sector, const void *src)
{ return PS__SWI(0x10, sector, (uint32_t)src, 0); }

/* SWI 11h - enable (1) / disable (0) PSX communication while running. */
static inline void bios_set_com(uint32_t on) { (void)PS__SWI(0x11, on, 0, 0); }

/* SWI 12h - 1 if the file at dir_index holds a resumable MCX1 snapshot. */
static inline uint32_t bios_test_snapshot(uint32_t dir_index) { return PS__SWI(0x12, dir_index, 0, 0); }

/* SWI 13h - pointer to the 64-bit user-interface status (alarm, volume, font). */
static inline volatile uint32_t *bios_get_ui_status(void)
{ return (volatile uint32_t *)PS__SWI(0x13, 0, 0, 0); }
#define PS_UI_VOLUME(st)  (((st)[0] >> 18) & 3u)  /* 0 = loud, 1 = low, 2 = off */

/* SWI 16h - directory index (first block, 1..15) of the running file. */
static inline uint32_t bios_get_dir_index(void) { return PS__SWI(0x16, 0, 0, 0); }

/* SWI 17h - pointer to the 16-bit FUNC3 handler address (offset in file). */
static inline volatile uint16_t *bios_get_func3_ptr(void)
{ return (volatile uint16_t *)PS__SWI(0x17, 0, 0, 0); }

/* SWI 00h (Reset) is intentionally not wrapped: it erases the RTC. */

#endif /* PS_BIOS_H */
