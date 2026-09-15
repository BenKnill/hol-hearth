(* Intentional false statement: demonstrates failure without a theorem claim. *)
let HEARTH_FALSE_SQUARE = prove
 (`!x y:real. (x + y) pow 2 = x pow 2 + y pow 2`,
  CONV_TAC REAL_RING);;
