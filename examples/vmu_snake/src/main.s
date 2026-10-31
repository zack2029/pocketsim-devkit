; SPDX-License-Identifier: MIT - Copyright (c) 2026 PocketSim DevKit contributors
;
; vmu_snake - Snake for the Dreamcast VMU, in LC86K assembly.
;
;   title    : A = play, MODE = back to the VMU menu
;   in game  : d-pad steers, MODE quits, SLEEP blanks the screen
;
; The field is 24 x 12 cells of 2 x 2 pixels under a score bar. Eat the
; food (the dotted cell) to grow; the snake speeds up every 5 points.
; Hitting the edge or yourself ends the run. The best score is kept while
; the game runs.
;
; Shows: the runtime calls (vm_wait_frame, vm_input, drawing, text, sound,
; random numbers), a ring buffer walked with @R0/@R1, and incremental
; drawing - only the head and the tail cell change each step, which keeps
; a 146 kHz CPU relaxed.

        .include "vmu.i"

; ---------------------------------------------------------------- variables (RAM bank 1)
score   = VM_USER
hiscore = VM_USER+1
dir     = VM_USER+2         ; 0 right, 1 down, 2 left, 3 up
nextdir = VM_USER+3
head    = VM_USER+4         ; ring index of the head (0-63)
len     = VM_USER+5
delay   = VM_USER+6         ; frames per step
tick    = VM_USER+7
cx      = VM_USER+8         ; cell being worked on
cy      = VM_USER+9
food_x  = VM_USER+10
food_y  = VM_USER+11
keys    = VM_USER+12
count   = VM_USER+13
ate     = VM_USER+14

BODY_X  = $80               ; ring buffer: x of each segment at $80-$BF,
BODY_Y  = $C0               ;              y at $C0-$FF
MAXLEN  = 64
COLS    = 24
ROWS    = 12
FIELD_Y = 8                 ; first pixel row of the field

; ---------------------------------------------------------------- title
main:
        mov     #0, hiscore
title:
        callf   vm_cls
        mov     #1, vm_ink
        mov     #14, B
        mov     #2, C
        mov     #<s_title, TRL
        mov     #>s_title, TRH
        callf   vm_text
        ; a little snake heading for its food
        mov     #6, B
        mov     #11, C
        mov     #24, vm_t3
        mov     #2, vm_t4
        callf   vm_fill
        mov     #28, B
        mov     #9, C
        mov     #2, vm_t3
        mov     #4, vm_t4
        callf   vm_fill
        mov     #28, B
        mov     #9, C
        mov     #8, vm_t3
        mov     #2, vm_t4
        callf   vm_fill
        mov     #38, B
        mov     #9, C
        callf   vm_pset
        mov     #39, B
        mov     #10, C
        callf   vm_pset
        mov     #10, B
        mov     #18, C
        mov     #<s_start, TRL
        mov     #>s_start, TRH
        callf   vm_text
        mov     #14, B
        mov     #25, C
        mov     #<s_hi, TRL
        mov     #>s_hi, TRH
        callf   vm_text
        ld      hiscore
        callf   vm_number
.wait:  callf   vm_wait_frame
        callf   vm_input
        bn      vm_new, KB_A, .wait

; ---------------------------------------------------------------- new game
start:
        callf   vm_cls
        mov     #N_C4, ACC
        mov     #4, B
        callf   vm_beep
        mov     #1, vm_ink
        mov     #0, B
        mov     #6, C
        mov     #VM_W, vm_t3
        callf   vm_hline            ; line under the score bar
        mov     #0, score
        mov     #0, dir
        mov     #0, nextdir
        mov     #6, delay
        mov     #6, tick
        mov     #3, len
        mov     #2, head
        mov     #BODY_X, R0         ; three segments at (4,6) (5,6) (6,6)
        mov     #BODY_Y, R1
        mov     #4, cx
        mov     #6, cy
        mov     #3, count
.init:  ld      cx
        st      @R0
        ld      cy
        st      @R1
        inc     R0
        inc     R1
        callf   fill_cell
        inc     cx
        dbnz    count, .init
        callf   place_food
        callf   show_score

; ---------------------------------------------------------------- game loop
play:
        callf   vm_wait_frame
        callf   vm_input
        ld      vm_keys             ; held keys, plus taps shorter than a frame
        or      vm_new
        st      keys
        bn      keys, KB_RIGHT, .k1
        ld      dir
        be      #2, .k1             ; no U-turns
        mov     #0, nextdir
.k1:    bn      keys, KB_DOWN, .k2
        ld      dir
        be      #3, .k2
        mov     #1, nextdir
.k2:    bn      keys, KB_LEFT, .k3
        ld      dir
        be      #0, .k3
        mov     #2, nextdir
.k3:    bn      keys, KB_UP, .k4
        ld      dir
        be      #1, .k4
        mov     #3, nextdir
.k4:    dbnz    tick, play
        ld      delay
        st      tick

        ; ---- one step: new head = old head + direction
        ld      nextdir
        st      dir
        ld      head
        add     #BODY_X
        st      R0
        add     #BODY_Y-BODY_X
        st      R1
        ld      @R0
        st      cx
        ld      @R1
        st      cy
        ld      dir
        bnz     .notr
        inc     cx
        br      .moved
.notr:  be      #1, .down
        be      #2, .left
        dec     cy
        br      .moved
.down:  inc     cy
        br      .moved
.left:  dec     cx
.moved:
        ld      cx                  ; off the field? (-1 is 255, so one test covers both sides)
        sub     #COLS
        bp      PSW, 7, .inx
        jmpf    dead
.inx:   ld      cy
        sub     #ROWS
        bp      PSW, 7, .iny
        jmpf    dead
.iny:
        ; ---- food?
        mov     #0, ate
        ld      cx
        bne     food_x, .erase
        ld      cy
        bne     food_y, .erase
        mov     #1, ate
        inc     score
        mov     #N_E5, ACC
        mov     #3, B
        callf   vm_beep
        ld      len
        be      #MAXLEN, .erase     ; full length: keep moving the tail
        inc     len
        br      .check
.erase:                             ; tail index = head - len + 1
        ld      head
        sub     len
        inc     ACC
        and     #MAXLEN-1
        add     #BODY_X
        st      R0
        add     #BODY_Y-BODY_X
        st      R1
        push    cx
        push    cy
        ld      @R0
        st      cx
        ld      @R1
        st      cy
        mov     #0, vm_ink
        callf   fill_cell
        mov     #1, vm_ink
        pop     cy
        pop     cx
.check:                             ; the tail is gone, so following it is fine
        ld      ate
        bnz     .free
        callf   cell_xy
        callf   vm_ptest
        bz      .free
        jmpf    dead
.free:
        ld      head                ; store and draw the new head
        inc     ACC
        and     #MAXLEN-1
        st      head
        add     #BODY_X
        st      R0
        add     #BODY_Y-BODY_X
        st      R1
        ld      cx
        st      @R0
        ld      cy
        st      @R1
        callf   fill_cell
        ld      ate
        bz      .next
        callf   place_food
        callf   show_score
        callf   set_speed
.next:  jmpf    play

; ---------------------------------------------------------------- game over
dead:
        mov     #N_C3, ACC
        mov     #20, B
        callf   vm_beep
        ld      hiscore
        sub     score
        bn      PSW, 7, .nohi       ; no borrow: score <= best
        ld      score
        st      hiscore
        callf   show_score
.nohi:
        mov     #0, vm_ink          ; clear a box, then frame it
        mov     #4, B
        mov     #11, C
        mov     #40, vm_t3
        mov     #17, vm_t4
        callf   vm_fill
        mov     #1, vm_ink
        mov     #40, vm_t3
        callf   vm_hline
        mov     #27, C
        callf   vm_hline
        mov     #11, C
        mov     #1, vm_t3
        mov     #17, vm_t4
        callf   vm_fill
        mov     #43, B
        callf   vm_fill
        mov     #6, B
        mov     #13, C
        mov     #<s_over, TRL
        mov     #>s_over, TRH
        callf   vm_text
        mov     #8, B
        mov     #20, C
        mov     #<s_score, TRL
        mov     #>s_score, TRH
        callf   vm_text
        ld      score
        callf   vm_number
        mov     #30, count          ; 1 s pause so a late steer doesn't skip this
.w1:    callf   vm_wait_frame
        callf   vm_input
        dbnz    count, .w1
.w2:    callf   vm_wait_frame
        callf   vm_input
        bn      vm_new, KB_A, .w2
        jmpf    title

; ---------------------------------------------------------------- helpers
cell_xy:                            ; cx, cy -> B, C = top-left pixel of the cell
        ld      cx
        add     cx
        st      B
        ld      cy
        add     cy
        add     #FIELD_Y
        st      C
        ret

fill_cell:                          ; 2x2 block at cx, cy with vm_ink
        callf   cell_xy
        mov     #2, vm_t3
        mov     #2, vm_t4
        callf   vm_fill
        ret

draw_food:                          ; food = two diagonal pixels
        ld      food_x
        st      cx
        ld      food_y
        st      cy
        callf   cell_xy
        callf   vm_pset
        inc     B
        inc     C
        callf   vm_pset
        ret

place_food:                         ; random free cell
        mov     #COLS, ACC
        callf   vm_rand_n
        st      food_x
        mov     #ROWS, ACC
        callf   vm_rand_n
        st      food_y
.try:   ld      food_x
        st      cx
        ld      food_y
        st      cy
        callf   cell_xy
        callf   vm_ptest
        bz      .free
        inc     food_x              ; taken: try the next cell
        ld      food_x
        bne     #COLS, .try
        mov     #0, food_x
        inc     food_y
        ld      food_y
        bne     #ROWS, .try
        mov     #0, food_y
        br      .try
.free:  callf   draw_food
        ret

show_score:
        mov     #1, B
        mov     #0, C
        ld      score
        callf   vm_number
        mov     #24, B
        mov     #0, C
        mov     #<s_hi, TRL
        mov     #>s_hi, TRH
        callf   vm_text
        ld      hiscore
        callf   vm_number
        ret

set_speed:                          ; delay = 6 - score / 5, at least 2 frames
        ld      score
        st      C
        mov     #0, ACC
        mov     #5, B
        div
        ld      C
        st      vm_t0
        mov     #6, ACC
        sub     vm_t0
        bp      PSW, 7, .min
        st      delay
        sub     #2
        bp      PSW, 7, .min
        ret
.min:   mov     #2, delay
        ret

; ---------------------------------------------------------------- text
s_title: .byte  "SNAKE", 0
s_start: .byte  "A START", 0
s_hi:    .byte  "HI ", 0
s_over:  .byte  "GAME OVER", 0
s_score: .byte  "SCORE ", 0
