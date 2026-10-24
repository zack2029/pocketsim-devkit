/* SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors */
/*
 * crt0.S - PocketStation executable entry point
 *
 * The kernel maps the file at 0x02000000 and jumps to the entry address stored
 * in the title sector (offset 0x5C) in ARM state, User mode, with r0 = the
 * parameter given to PrepareExecute. User RAM (0x200-0x7FF) is already zeroed.
 */
    .syntax unified
    .arm
    .section .text.crt0, "ax", %progbits
    .global _start
    .type   _start, %function
_start:
    ldr     sp, =__stack_top
    ldr     r1, =ps__boot_param
    str     r0, [r1]

    /* copy .data from flash (word copies only: flash is 16/32-bit readable) */
    ldr     r0, =__data_load
    ldr     r1, =__data_start
    ldr     r2, =__data_end
1:  cmp     r1, r2
    ldrlo   r3, [r0], #4
    strlo   r3, [r1], #4
    blo     1b

    /* zero .bss */
    ldr     r1, =__bss_start
    ldr     r2, =__bss_end
    mov     r3, #0
2:  cmp     r1, r2
    strlo   r3, [r1], #4
    blo     2b

    /* run C++ static constructors, if any */
    ldr     r4, =__init_array_start
    ldr     r5, =__init_array_end
3:  cmp     r4, r5
    bhs     4f
    ldr     r3, [r4], #4
    mov     lr, pc
    bx      r3
    b       3b
4:
    ldr     r3, =main
    mov     lr, pc
    bx      r3

    /* main returned: back to the menu */
    ldr     r3, =ps_exit
    mov     lr, pc
    bx      r3
5:  b       5b
    .size   _start, . - _start
    .pool

/*
 * IRQ hook installed with SWI 01h (slot 1).
 * The kernel only saves r0, r1, r12 around the callback, so preserve r2/r3/lr
 * before calling C (C itself preserves r4-r11).
 */
    .global ps__irq_entry
    .type   ps__irq_entry, %function
ps__irq_entry:
    stmfd   sp!, {r2, r3, lr}
    ldr     r2, =ps__irq_handler
    mov     lr, pc
    bx      r2
    ldmfd   sp!, {r2, r3, lr}
    bx      lr
    .size   ps__irq_entry, . - ps__irq_entry
    .pool

/* TTY debug opcode understood by pocketsim (SN Systems convention): prints chr(r0). */
    .global ps__tty_putc
    .type   ps__tty_putc, %function
ps__tty_putc:
    .word   0xE6000010
    bx      lr
    .size   ps__tty_putc, . - ps__tty_putc

    .bss
    .align 2
    .global ps__boot_param
ps__boot_param:
    .space 4
