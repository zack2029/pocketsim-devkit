; SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
; vmu.s - PocketSim DevKit runtime for Dreamcast VMU games (LC86K assembly).
; Added to every VMU build by psbuild; see include/vmu.i for the calls.

; ---------------------------------------------------------------- setup
vm_init:
        mov     #$81, OCR           ; RC oscillator, cycle = 1/6: ~146k cycles per second
        mov     #$09, MCR           ; LCD graphics mode, refresh on
        mov     #$80, VCCR          ; LCD on
        clr1    P3INT, 0            ; buttons are polled, no button interrupt
        ; buzzer: timer 1 low byte as PWM on P1.7
        mov     #$80, P1FCR
        clr1    P1, 7
        mov     #$80, P1DDR
        mov     #$FF, T1LR
        mov     #$FF, T1LC
        mov     #$D0, T1CNT
        ; timer 0 low byte: frame interrupt, 256 * 19 cycles = ~30 Hz
        mov     #0, T0PRR
        mov     #$ED, T0LR
        mov     #$ED, T0L
        mov     #$41, T0CON         ; T0L run + interrupt
        mov     #0, vm_frame
        mov     #0, vm_snd
        mov     #0, vm_new
        mov     #1, vm_ink
        mov     #$A5, vm_seed
        mov     #$3C, vm_seed+1
        ld      P3                  ; buttons still held from the menu are not "new"
        xor     #$FF
        st      vm_old
        st      vm_keys
        callf   vm_cls
        set1    IE, 7
        ret

; ---------------------------------------------------------------- frames and input
vm_wait_frame:
        ld      vm_frame
        st      vm_t0
.halt:  set1    PCON, 0             ; HALT until an interrupt (saves battery)
        ld      vm_frame
        be      vm_t0, .halt
        ret

vm_input:
        callf   vm_rand             ; keep the random generator moving
        ld      P3
        xor     #$FF
        st      vm_keys
        ld      vm_old
        xor     #$FF
        and     vm_keys
        st      vm_new
        ld      vm_keys
        st      vm_old
        bp      vm_new, KB_MODE, vm_exit
        bp      vm_new, KB_SLEEP, vm_sleep
        ret

vm_exit:
        callf   vm_quiet
        jmpf    vm__goodbye         ; back to the VMU menu (BIOS)

vm_sleep:
        callf   vm_quiet
        clr1    VCCR, 7             ; screen off
.rel:   callf   vm_wait_frame       ; wait until SLEEP is released
        ld      P3
        bne     #$FF, .rel
.prs:   callf   vm_wait_frame       ; then for any button
        ld      P3
        be      #$FF, .prs
        xor     #$FF
        st      vm_old              ; that press only wakes the VMU
        st      vm_keys
        mov     #0, vm_new
        set1    VCCR, 7
        ret

; ---------------------------------------------------------------- frame interrupt
vm__t0irq:
        push    ACC
        push    PSW
        clr1    T0CON, 1            ; acknowledge T0L overflow
        inc     vm_frame
        ld      vm_snd
        bz      .done
        dec     vm_snd
        ld      vm_snd
        bnz     .done
        mov     #$FF, T1LR          ; tone finished
        mov     #$FF, T1LC
.done:  pop     PSW
        pop     ACC
        reti

; ---------------------------------------------------------------- sound
vm_beep:                            ; ACC = note (T1LR value), B = frames
        st      T1LR
        set1    PSW, 7
        rorc                        ; compare = $80 | note/2: 50% duty
        st      T1LC
        ld      B
        st      vm_snd
        ret

vm_quiet:
        mov     #$FF, T1LR
        mov     #$FF, T1LC
        mov     #0, vm_snd
        ret

; ---------------------------------------------------------------- random
vm_rand:                            ; 16-bit Galois LFSR, 8 steps -> ACC
        mov     #8, vm_t0
.step:  ld      vm_seed
        clr1    PSW, 7
        rorc
        st      vm_seed
        ld      vm_seed+1
        rorc
        st      vm_seed+1
        bn      PSW, 7, .nox
        ld      vm_seed
        xor     #$B4
        st      vm_seed
.nox:   dbnz    vm_t0, .step
        ld      vm_seed+1
        xor     vm_seed
        ret

vm_rand_n:                          ; ACC = n -> ACC = 0..n-1
        push    B
        push    C
        st      B
        callf   vm_rand
        st      C
        mov     #0, ACC
        mul                         ; ACC = rand * n / 256
        pop     C
        pop     B
        ret

; ---------------------------------------------------------------- screen
vm_cls:
        push    XBNK
        mov     #0, XBNK
        callf   .clr
        mov     #1, XBNK
        callf   .clr
        pop     XBNK
        ret
.clr:   mov     #$80, R2
.loop:  mov     #0, @R2
        inc     R2
        ld      R2
        bnz     .loop
        ret

; B = x (0-47), C = y (0-31) -> XBNK, R2 = LCD byte, ACC = bit mask
; (off screen: ACC = 0 so nothing changes). Uses vm_t0, vm_t1.
vm_pixaddr:
        ld      B
        sub     #VM_W
        bn      PSW, 7, .clip       ; no borrow: x >= 48
        ld      C
        sub     #VM_H
        bn      PSW, 7, .clip
        mov     #0, XBNK
        bn      C, 4, .top
        mov     #1, XBNK            ; rows 16-31 are in bank 1
.top:   ld      C
        and     #$0E
        rol
        rol
        rol                         ; (y & 14) * 8: two rows per 16 bytes
        bn      C, 0, .even
        add     #6                  ; odd rows: second half
.even:  st      vm_t0
        ld      B
        ror
        ror
        ror
        and     #$1F                ; x / 8
        add     vm_t0
        add     #$80                ; @R2 addresses $100 + R2 = $180...
        st      R2
        ld      B
        and     #7
        inc     ACC
        st      vm_t1
        mov     #1, ACC
.mask:  ror
        dbnz    vm_t1, .mask        ; $80 >> (x & 7): leftmost pixel is bit 7
        ret
.clip:  mov     #$80, R2
        mov     #0, ACC
        ret

vm_plot:                            ; draw with vm_ink: 0 clear, 1 set, 2 invert
        ld      vm_ink
        bz      vm_pclr
        be      #1, vm_pset
        br      vm_pxor
vm_pset:
        callf   vm_pixaddr
        or      @R2
        st      @R2
        ret
vm_pclr:
        callf   vm_pixaddr
        xor     #$FF
        and     @R2
        st      @R2
        ret
vm_pxor:
        callf   vm_pixaddr
        xor     @R2
        st      @R2
        ret
vm_ptest:
        callf   vm_pixaddr
        and     @R2
        ret

vm_hline:                           ; B = x, C = y, vm_t3 = width; ink = vm_ink
        push    B
        ld      vm_t3
        bz      .done
        st      vm_t2
.loop:  callf   vm_plot
        inc     B
        dbnz    vm_t2, .loop
.done:  pop     B
        ret

vm_fill:                            ; B = x, C = y, vm_t3 = width, vm_t4 = height
        push    C
        ld      vm_t4
        bz      .done
        push    vm_t4
.row:   callf   vm_hline
        inc     C
        dbnz    vm_t4, .row
        pop     vm_t4
.done:  pop     C
        ret

; ---------------------------------------------------------------- text
vm_char:                            ; B = x, C = y, ACC = character; B += 4
        bn      ACC, 7, .ascii
        mov     #32, ACC
.ascii: st      vm_t2
        sub     #97                 ; lower case -> upper case
        bp      PSW, 7, .notlow
        ld      vm_t2
        sub     #123
        bn      PSW, 7, .notlow
        ld      vm_t2
        sub     #32
        st      vm_t2
.notlow:
        ld      vm_t2
        sub     #91
        bp      PSW, 7, .inrange    ; < 91
        mov     #32, vm_t2
.inrange:
        ld      vm_t2
        sub     #32
        bn      PSW, 7, .glyph      ; >= 32
        mov     #0, ACC
.glyph: push    B
        push    C
        st      C
        mov     #0, ACC
        mov     #5, B
        mul                         ; ACC:C = (char - 32) * 5
        st      vm_t2
        ld      C
        add     #<vm_font
        st      TRL
        ld      vm_t2
        addc    #>vm_font
        st      TRH
        pop     C
        pop     B
        mov     #0, vm_t3           ; row
.row:   ld      vm_t3
        ldc
        st      vm_t4               ; row bits, leftmost = bit 7
        push    B
        mov     #4, vm_t2           ; 3 columns + 1 blank
.col:   bn      vm_t4, 7, .off
        callf   vm_pset
        br      .next
.off:   callf   vm_pclr
.next:  ld      vm_t4
        rol
        and     #$FE
        st      vm_t4
        inc     B
        dbnz    vm_t2, .col
        pop     B
        inc     C
        inc     vm_t3
        ld      vm_t3
        bne     #5, .row
        ld      C
        sub     #5
        st      C
        ld      B
        add     #4
        st      B
        ret

vm_text:                            ; B = x, C = y, TRH:TRL = 0-terminated string
        ld      TRL
        st      vm_t5
        ld      TRH
        st      vm_t6
.next:  ld      vm_t5
        st      TRL
        ld      vm_t6
        st      TRH
        mov     #0, ACC
        ldc
        bz      .end
        callf   vm_char
        inc     vm_t5
        ld      vm_t5
        bnz     .next
        inc     vm_t6
        br      .next
.end:   ret

vm_number:                          ; B = x, C = y, ACC = 0-255
        st      vm_t7
        push    B
        push    C
        ld      vm_t7
        st      C
        mov     #0, ACC
        mov     #100, B
        div                         ; C = value / 100, B = remainder
        ld      C
        st      vm_t5
        ld      B
        st      C
        mov     #0, ACC
        mov     #10, B
        div
        ld      C
        st      vm_t6
        ld      B
        st      vm_t7
        pop     C
        pop     B
        ld      vm_t5
        bz      .no100
        add     #48
        callf   vm_char
        ld      vm_t6
        br      .p10
.no100: ld      vm_t6
        bz      .p1
.p10:   add     #48
        callf   vm_char
.p1:    ld      vm_t7
        add     #48
        callf   vm_char
        ret

vm_font:          ; 3x5 font, characters 32-90, one byte per row (bits 7-5)
        .byte $00,$00,$00,$00,$00   ; space
        .byte $40,$40,$40,$00,$40   ; !
        .byte $A0,$A0,$00,$00,$00   ; "
        .byte $A0,$E0,$A0,$E0,$A0   ; #
        .byte $60,$C0,$40,$60,$C0   ; $
        .byte $80,$20,$40,$80,$20   ; %
        .byte $40,$A0,$40,$A0,$60   ; &
        .byte $40,$40,$00,$00,$00   ; '
        .byte $20,$40,$40,$40,$20   ; (
        .byte $80,$40,$40,$40,$80   ; )
        .byte $00,$A0,$40,$A0,$00   ; *
        .byte $00,$40,$E0,$40,$00   ; +
        .byte $00,$00,$00,$40,$80   ; ,
        .byte $00,$00,$E0,$00,$00   ; -
        .byte $00,$00,$00,$00,$40   ; .
        .byte $20,$20,$40,$80,$80   ; /
        .byte $E0,$A0,$A0,$A0,$E0   ; 0
        .byte $40,$C0,$40,$40,$E0   ; 1
        .byte $E0,$20,$E0,$80,$E0   ; 2
        .byte $E0,$20,$60,$20,$E0   ; 3
        .byte $A0,$A0,$E0,$20,$20   ; 4
        .byte $E0,$80,$E0,$20,$E0   ; 5
        .byte $E0,$80,$E0,$A0,$E0   ; 6
        .byte $E0,$20,$20,$40,$40   ; 7
        .byte $E0,$A0,$E0,$A0,$E0   ; 8
        .byte $E0,$A0,$E0,$20,$E0   ; 9
        .byte $00,$40,$00,$40,$00   ; :
        .byte $00,$40,$00,$40,$80   ; ;
        .byte $20,$40,$80,$40,$20   ; <
        .byte $00,$E0,$00,$E0,$00   ; =
        .byte $80,$40,$20,$40,$80   ; >
        .byte $E0,$20,$60,$00,$40   ; ?
        .byte $40,$A0,$A0,$80,$60   ; @
        .byte $40,$A0,$E0,$A0,$A0   ; A
        .byte $C0,$A0,$C0,$A0,$C0   ; B
        .byte $60,$80,$80,$80,$60   ; C
        .byte $C0,$A0,$A0,$A0,$C0   ; D
        .byte $E0,$80,$C0,$80,$E0   ; E
        .byte $E0,$80,$C0,$80,$80   ; F
        .byte $60,$80,$A0,$A0,$60   ; G
        .byte $A0,$A0,$E0,$A0,$A0   ; H
        .byte $E0,$40,$40,$40,$E0   ; I
        .byte $20,$20,$20,$A0,$40   ; J
        .byte $A0,$A0,$C0,$A0,$A0   ; K
        .byte $80,$80,$80,$80,$E0   ; L
        .byte $A0,$E0,$E0,$A0,$A0   ; M
        .byte $C0,$A0,$A0,$A0,$A0   ; N
        .byte $40,$A0,$A0,$A0,$40   ; O
        .byte $C0,$A0,$C0,$80,$80   ; P
        .byte $40,$A0,$A0,$C0,$60   ; Q
        .byte $C0,$A0,$C0,$A0,$A0   ; R
        .byte $60,$80,$40,$20,$C0   ; S
        .byte $E0,$40,$40,$40,$40   ; T
        .byte $A0,$A0,$A0,$A0,$E0   ; U
        .byte $A0,$A0,$A0,$A0,$40   ; V
        .byte $A0,$A0,$E0,$E0,$A0   ; W
        .byte $A0,$A0,$40,$A0,$A0   ; X
        .byte $A0,$A0,$40,$40,$40   ; Y
        .byte $E0,$20,$40,$80,$E0   ; Z
