; SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
; vmu.i - PocketSim DevKit VMU runtime: constants, RAM map and calls.
; Include it at the top of your game:      .include "vmu.i"
;
; The build (ps build with target = vmu) adds the interrupt vectors, the BIOS
; hooks, the file header + icon and this runtime, then jumps to your label
; "main" with the runtime already set up (vm_init has run).
;
; ---------------------------------------------------------------- RAM map
; Games run with RAM bank 1 selected. The stack is in bank 0, so all of
; bank 1 ($00-$FF) is yours except what the runtime uses:
;
;   $00-$03  R0-R3  pointers for @R0..@R3 (R2/R3 point into $100-$1FF:
;                   SFRs and the LCD memory)
;   $04-$0F  runtime variables (below)
;   $10-$13  more runtime variables (vm_ink, scratch)
;   $14-$FF  free for your game  (VM_USER is the first free address)

        .include "sfr.i"

R0          = $00
R1          = $01
R2          = $02
R3          = $03
vm_frame    = $04       ; +1 every frame (30 per second), wraps at 256
vm_keys     = $05       ; buttons held, 1 = pressed (K_* bits)
vm_new      = $06       ; buttons pressed since the previous vm_input
vm_old      = $07
vm_snd      = $08       ; frames left of the current tone
vm_seed     = $09       ; 2 bytes, random generator state
vm_t0       = $0B       ; scratch used by the runtime; free between calls
vm_t1       = $0C
vm_t2       = $0D
vm_t3       = $0E
vm_t4       = $0F
vm_ink      = $10       ; drawing mode for vm_plot/vm_hline/vm_fill: 0 clear, 1 set, 2 invert
vm_t5       = $11
vm_t6       = $12
vm_t7       = $13
VM_USER     = $14

; ---------------------------------------------------------------- buttons
K_UP        = $01
K_DOWN      = $02
K_LEFT      = $04
K_RIGHT     = $08
K_A         = $10
K_B         = $20
K_MODE      = $40
K_SLEEP     = $80
; bit numbers, for  bp vm_keys,KB_A,label
KB_UP       = 0
KB_DOWN     = 1
KB_LEFT     = 2
KB_RIGHT    = 3
KB_A        = 4
KB_B        = 5
KB_MODE     = 6
KB_SLEEP    = 7

; ---------------------------------------------------------------- screen
VM_W        = 48
VM_H        = 32
VM_FPS      = 30

; ---------------------------------------------------------------- notes
; Values for vm_beep (timer 1 reload). The buzzer divides 5461 Hz, so high
; notes are approximate.
N_C3        = $D6
N_E3        = $DF
N_G3        = $E4
N_A3        = $E7
N_C4        = $EB
N_D4        = $ED
N_E4        = $EF
N_F4        = $F0
N_G4        = $F2
N_A4        = $F4
N_B4        = $F5
N_C5        = $F6
N_E5        = $F8
N_G5        = $F9
N_C6        = $FB

; ---------------------------------------------------------------- calls
; (all with callf; registers not listed as results are kept, except ACC and
; the vm_t* scratch bytes)
;
;   vm_wait_frame         sleep (HALT) until the next 1/30 s frame
;   vm_input              read buttons into vm_keys / vm_new
;                         MODE goes back to the VMU menu, SLEEP blanks the
;                         screen until a button is pressed
;   vm_cls                clear the screen
;   vm_pset   B=x C=y     set a pixel          (x 0-47, y 0-31)
;   vm_pclr   B=x C=y     clear a pixel
;   vm_pxor   B=x C=y     invert a pixel
;   vm_ptest  B=x C=y     ACC = 0 if the pixel is clear
;   vm_plot   B=x C=y                 draw a pixel with vm_ink (0 clear, 1 set, 2 invert)
;   vm_hline  B=x C=y vm_t3=width     horizontal line with vm_ink
;   vm_fill   B=x C=y vm_t3=w vm_t4=h  filled rectangle with vm_ink
;   vm_char   B=x C=y ACC=char        draw a 3x5 character on a cleared 4x5 box, B += 4
;   vm_text   B=x C=y TRH:TRL=string  draw a 0-terminated string ($text label)
;   vm_number B=x C=y ACC=0-255       draw a number (no leading zeros)
;   vm_beep   ACC=note B=frames       start a tone, stops by itself
;   vm_quiet                          stop the tone now
;   vm_rand   -> ACC                  random byte
;   vm_rand_n ACC=n -> ACC            random 0..n-1 (n 1-255)
;   vm_exit                           back to the VMU menu (never returns)
