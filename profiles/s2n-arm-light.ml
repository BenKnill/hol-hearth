needs "arm/proofs/base.ml";;
needs "calc_rat.ml";;
prioritize_real();;
let HOL_WORKBENCH_REAL_POLY_RING_TAC = CONV_TAC REAL_RING;;
needs "Library/ringtheory.ml";;
let HOL_WORKBENCH_ABSTRACT_RING_TAC = RING_TAC;;
let RING_TAC = HOL_WORKBENCH_REAL_POLY_RING_TAC;;
let HOL_WORKBENCH_S2N_ARM_LIGHT_RING_SMOKE = prove(`!x y:real. (x + y) pow 2 = x pow 2 + &2 * x * y + y pow 2`,RING_TAC);;
let HOL_WORKBENCH_S2N_ARM_LIGHT_WARMUP = prove(`T`,MESON_TAC[]);;
