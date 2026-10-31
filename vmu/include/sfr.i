; SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
; sfr.i - Dreamcast VMU (Sanyo LC86K) special function registers
; Names follow the usual VMU homebrew spelling, so existing sources work.

ACC     = $100      ; accumulator
PSW     = $101      ; status: CY(7) AC(6) IRBK1(4) IRBK0(3) OV(2) RAMBK0(1) P(0)
B       = $102      ; B register
C       = $103      ; C register
TRL     = $104      ; table pointer (ldc) low
TRH     = $105      ; table pointer high
SP      = $106      ; stack pointer (stack is RAM bank 0, $80-$FF)
PCON    = $107      ; power: bit0 HALT, bit1 HOLD
IE      = $108      ; interrupt enable: bit7 master
IP      = $109      ; interrupt priority
EXT     = $10D      ; bit0: 1 = run from flash, 0 = run from BIOS ROM
OCR     = $10E      ; oscillator control
T0CON   = $110      ; timer 0 control
T0PRR   = $111      ; timer 0 prescaler reload
T0L     = $112
T0LR    = $113      ; timer 0 low reload
T0H     = $114
T0HR    = $115      ; timer 0 high reload
T1CNT   = $118      ; timer 1 control (buzzer)
T1LC    = $11A      ; timer 1 low compare (buzzer duty)
T1L     = $11B
T1LR    = $11B      ; timer 1 low reload (buzzer pitch)
T1HC    = $11C
T1H     = $11D
T1HR    = $11D
MCR     = $120      ; LCD mode
STAD    = $122      ; LCD start address
CNR     = $123      ; LCD character count
TDR     = $124      ; LCD time division
XBNK    = $125      ; XRAM bank: 0 = rows 0-15, 1 = rows 16-31, 2 = icons
VCCR    = $127      ; LCD control: bit7 display on
SCON0   = $130
SBUF0   = $131
SBR     = $132
SCON1   = $134
SBUF1   = $135
P1      = $144      ; port 1 (bit7 = buzzer)
P1DDR   = $145
P1FCR   = $146
P3      = $14C      ; buttons, 0 = pressed: Up Down Left Right A B MODE SLEEP (bits 0-7)
P3DDR   = $14D
P3INT   = $14E
P7      = $15C      ; bit0: connected to a Dreamcast, bit1: low battery
I01CR   = $15D
I23CR   = $15E
ISL     = $15F
VSEL    = $163
VRMAD1  = $164
VRMAD2  = $165
VTRBF   = $166
VLREG   = $167
BTCR    = $17F      ; base timer control
XRAM    = $180      ; LCD memory window $180-$1FB
