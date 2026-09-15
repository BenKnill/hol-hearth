needs "calc_rat.ml";;
prioritize_real();;
needs "Library/ringtheory.ml";;
needs "Multivariate/realanalysis.ml";;
let HOL_WORKBENCH_HEAVY_WARMUP = prove(`T`,MESON_TAC[]);;
