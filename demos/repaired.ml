(* Add the missing cross term, then save this source in a live session. *)
let HEARTH_SQUARE_REPAIRED = prove
 (`!x y:real. (x + y) pow 2 = x pow 2 + &2 * x * y + y pow 2`,
  CONV_TAC REAL_RING);;
