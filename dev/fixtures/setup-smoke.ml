(* A quadratic invariant of the integer matrix [[2,1],[1,1]]. *)
let HEARTH_CAT_INVARIANT = prove
 (`!x y:real.
     (&2 * x + y) pow 2 - (&2 * x + y) * (x + y) - (x + y) pow 2 =
     x pow 2 - x * y - y pow 2`,
  CONV_TAC REAL_RING);;

let HEARTH_CAT_INVERSE = prove
 (`!x y:real. &2 * (x - y) + (&2 * y - x) = x /\
              (x - y) + (&2 * y - x) = y`,
  REAL_ARITH_TAC);;
