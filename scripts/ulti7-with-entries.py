//@version=6
// Volumatic VIDYA module (BigBeluga) is licensed under CC BY-NC-SA 4.0: https://creativecommons.org/licenses/by-nc-sa/4.0/
indicator("ULTI-7 v15 FINAL [Frozen + Enhanced Alerts]", "ULTI-7 v15 FINAL", overlay=true, max_bars_back=4900, max_boxes_count=500, max_labels_count=500, max_lines_count=500)

// ULTI-7 v15: V14 LATE4 winner is part of the real trade manager. Before TP1, after age >= 4 bars, two consecutive closes through both 5M EMA and VWAP exit early. Original entries, structure stop, scales, TP2, and runner remain unchanged.
// ULTI-7 v6 execution enhancement: optimized for a 5M chart. One-shot regime reversals, stricter V0 reversals, post-runner re-arm, strict continuation, and TP2 runner management.
// Mechanical merge of the seven supplied source modules. All module plots, labels, tables, zones, alerts, and calculations are retained; only declaration, namespacing, and legacy syntax are changed.

show_ema_module = input.bool(true, "Show EMA Module", group="Module Visibility")
show_vwap_module = input.bool(true, "Show VWAP Module", group="Module Visibility")
show_volume_module = input.bool(true, "Show Volume Module", group="Module Visibility")
show_poki_module = input.bool(true, "Show Poki Module", group="Module Visibility")
show_smc_module = input.bool(true, "Show Smart Money Module", group="Module Visibility")
show_ualgo_module = input.bool(true, "Show UAlgo Module", group="Module Visibility")
show_vidya_module = input.bool(true, "Show VIDYA Module", group="Module Visibility")
show_module_legend = input.bool(true, "Show Module Legend", group="Module Visibility")

// Shared entry-quality gate. This filters entry alerts only; exit alerts remain unchanged.
entry_quality_enabled = input.bool(true, "Enable Entry Quality Gate", group="Entry Quality")
entry_quality_min_momentum = input.float(20.0, "Minimum Momentum Score", minval=0.0, maxval=100.0, group="Entry Quality")
entry_quality_min_pattern = input.float(25.0, "Minimum Pattern Score", minval=0.0, maxval=100.0, group="Entry Quality")
entry_quality_strong_context_momentum = input.float(45.0, "Strong Context Momentum", minval=0.0, maxval=100.0, group="Entry Quality")
entry_quality_strong_context_pattern = input.float(45.0, "Strong Context Pattern", minval=0.0, maxval=100.0, group="Entry Quality")
entry_quality_max_vwap_extension = input.float(1.2, "Maximum VWAP Extension (%)", minval=0.0, maxval=10.0, step=0.1, group="Entry Quality")
entry_quality_require_rising_momentum = input.bool(true, "Require Non-Negative Momentum Change", group="Entry Quality")

// -----------------------------------------------------------------------------
// ULTI-7 V2 EXECUTION LAYER
// 15M = broader context, 5M = trade bias/risk, chart timeframe = entry timing.
// These filters are designed to remove weak/choppy entries without requiring
// every timeframe to point in the same direction.
// -----------------------------------------------------------------------------
v2_enabled = input.bool(true, "Enable V2 Execution Filter", group="V2 Execution")
v2_block_strong_mtf_opposite = input.bool(true, "Block Strong Opposite 5M + 15M", group="V2 Execution")
v2_allow_neutral_5m = input.bool(true, "Allow Neutral 5M Context", group="V2 Execution")
v2_min_room_atr = input.float(0.75, "Minimum Room To Opposing Structure / 5M ATR", minval=0.0, step=0.25, group="V2 Execution")
v2_max_extension_atr = input.float(1.80, "Maximum Entry Extension / 5M ATR", minval=0.5, step=0.10, group="V2 Execution")
v2_exhaust_rsi_high = input.float(72.0, "Bull Exhaustion RSI", minval=55.0, maxval=90.0, step=1.0, group="V2 Execution")
v2_exhaust_rsi_low = input.float(28.0, "Bear Exhaustion RSI", minval=10.0, maxval=45.0, step=1.0, group="V2 Execution")
v2_chop_ema_atr = input.float(0.18, "5M EMA/VWAP Chop Width / ATR", minval=0.01, step=0.01, group="V2 Execution")
v2_chop_return_atr = input.float(0.35, "5M Churn Return / ATR", minval=0.05, step=0.05, group="V2 Execution")
v2_allow_expansion_override = input.bool(true, "Allow Strong Expansion Override", group="V2 Execution")
v2_expansion_body_atr = input.float(0.85, "Expansion Body / Chart ATR", minval=0.25, step=0.05, group="V2 Execution")
v2_expansion_relvol = input.float(1.50, "Expansion Relative Volume", minval=1.0, step=0.05, group="V2 Execution")

v2_show_debug = input.bool(false, "Show V2 Debug HUD", group="V2 Execution")

// Unified execution engine: one trade signal from Poki/SMC/VIDYA consensus
// plus READY -> market-structure confirmation for moves the modules miss.
v3_unified_enabled = input.bool(true, "Enable Unified Entry Engine", group="V3 Unified Entries")
v3_show_ready = input.bool(true, "Show READY Labels", group="V3 Unified Entries")
v3_ready_expire = input.int(8, "READY Expiration (Bars)", minval=2, maxval=30, group="V3 Unified Entries")
v3_entry_cooldown = input.int(5, "Unified Entry Cooldown (Bars)", minval=1, maxval=30, group="V3 Unified Entries")
v3_min_source_votes = input.int(2, "Direct Entry Source Votes", minval=1, maxval=3, group="V3 Unified Entries")
v3_allow_single_source_with_5m = input.bool(true, "Allow 1 Source When 5M Agrees", group="V3 Unified Entries")
v3_structure_body_atr = input.float(0.35, "Structure Confirm Body / Chart ATR", minval=0.10, step=0.05, group="V3 Unified Entries")
v3_structure_relvol = input.float(0.90, "Structure Confirm Relative Volume", minval=0.0, step=0.05, group="V3 Unified Entries")
v3_require_bos_after_ready = input.bool(false, "Require BOS After READY", group="V3 Unified Entries")
v3_show_raw_module_entries = input.bool(false, "Show Raw Module ENTRY Labels", group="V3 Unified Entries")

// V4: 5M-focused reversal + one-shot continuation entries.
v4_require_5m = input.bool(true, "Optimize Entries For 5M Chart", group="V4 5M Entries")
v4_enable_reversal = input.bool(true, "Enable Strong Reversal Entries", group="V4 5M Entries")
v4_reversal_body_atr = input.float(0.55, "Reversal Body / 5M ATR", minval=0.20, step=0.05, group="V4 5M Entries")
v4_reversal_relvol = input.float(0.85, "Reversal Relative Volume", minval=0.0, step=0.05, group="V4 5M Entries")
v4_enable_continuation = input.bool(true, "Enable Pullback Continuation Entries", group="V4 5M Entries")
v4_pullback_zone_atr = input.float(0.35, "Continuation Pullback Zone / ATR", minval=0.05, step=0.05, group="V4 5M Entries")
v4_cont_body_atr = input.float(0.30, "Continuation Confirm Body / ATR", minval=0.10, step=0.05, group="V4 5M Entries")
v4_cont_relvol = input.float(0.75, "Continuation Relative Volume", minval=0.0, step=0.05, group="V4 5M Entries")
v4_cont_reset_bars = input.int(8, "Continuation Arm Expiration (Bars)", minval=2, maxval=30, group="V4 5M Entries")

// V5 targeted improvements. REV is intentionally faster than CONT; CONT remains strict.
v5_fast_rev_enabled = input.bool(true, "Enable Fast Trend-Failure REV", group="V5 Improvements")
v5_fast_rev_body_atr = input.float(0.45, "Fast REV Body / ATR", minval=0.20, step=0.05, group="V5 Improvements")
v5_fast_rev_relvol = input.float(0.70, "Fast REV Relative Volume", minval=0.0, step=0.05, group="V5 Improvements")
v5_fast_rev_break_lookback = input.int(3, "Fast REV Structure Lookback", minval=2, maxval=8, group="V5 Improvements")
v5_post_loss_reset = input.bool(true, "Require Fresh Reset After SL", group="V5 Improvements")
v5_runner_after_tp2 = false
v5_runner_lookback = input.int(3, "Runner Structure Lookback", minval=2, maxval=10, group="V5 Improvements")
v5_runner_buffer_atr = input.float(0.10, "Runner Structure Buffer / ATR", minval=0.0, step=0.05, group="V5 Improvements")

// V6: reduce duplicate/late reversals while preserving the strong early REV behavior.
v6_one_shot_rev = input.bool(true, "One REV Per Regime Transition", group="V6 Improvements")
v6_v0_strict = input.bool(true, "Use Stricter Rules For V0 REV", group="V6 Improvements")
v6_v0_body_atr = input.float(0.75, "V0 REV Minimum Body / ATR", minval=0.30, step=0.05, group="V6 Improvements")
v6_v0_relvol = input.float(1.00, "V0 REV Minimum Relative Volume", minval=0.0, step=0.05, group="V6 Improvements")
v6_v0_require_structure_break = input.bool(true, "V0 REV Requires Fresh Structure Break", group="V6 Improvements")
v6_rearm_after_runner = input.bool(true, "Require Re-Arm After Runner Exit", group="V6 Improvements")

// V8/V9 analytics. Analytics do not alter entry or exit logic unless V9 optimization is explicitly enabled.
v7_show_analytics = input.bool(true, "Show Performance Analytics", group="V8 Analytics")
v7_show_setup_breakdown = input.bool(true, "Show Setup Breakdown", group="V8 Analytics")
v7_show_mfe_mae = input.bool(true, "Track MFE / MAE", group="V8 Analytics")
v7_reset_daily = input.bool(false, "Reset Analytics Each Trading Day", group="V8 Analytics")
v8_show_deep_analytics = input.bool(true, "Show Deep Analytics Table", group="V8 Analytics")
v8_time_zone = input.string("America/Chicago", "Analytics Time Zone", options=["America/Chicago", "America/New_York"], group="V8 Analytics")

// V9 optional optimization controls. OFF by default so V9 reproduces the V6/V8.1 signal engine.
v9_enable_cont_optimization = input.bool(false, "Enable Data-Driven CONT Filter", group="V9 Optimization")
v9_cont_midday_min_mtf = input.int(3, "Midday CONT Minimum MTF Score", minval=0, maxval=4, group="V9 Optimization")
v9_cont_midday_min_votes = input.int(1, "Midday CONT Minimum Source Votes", minval=0, maxval=3, group="V9 Optimization")
v9_counter_cont_min_mtf = input.int(2, "Counter-Trend CONT Minimum MTF Score", minval=0, maxval=4, group="V9 Optimization")
v9_runner_profile = input.string("Current", "Runner Trail Profile", options=["Current", "Wider", "Tighter"], group="V9 Optimization")
v9_show_cont_time = input.bool(true, "Show CONT By Time Bucket", group="V9 Optimization")

// V9.1: audit runner math, optionally adapt CONT quality to current market conditions,
// and estimate realized expectancy from the visual trade-management path.
v91_cont_mode = input.string("Baseline", "CONT Quality Mode", options=["Baseline", "Adaptive"], group="V9.1 Optimization")
v91_adaptive_base_score = input.int(4, "Adaptive CONT Base Quality", minval=2, maxval=8, group="V9.1 Optimization")
v91_adaptive_midday_extra = input.int(1, "Adaptive CONT Midday Extra", minval=0, maxval=3, group="V9.1 Optimization")
v91_adaptive_counter_extra = input.int(1, "Adaptive Counter-Trend Extra", minval=0, maxval=3, group="V9.1 Optimization")
v91_adaptive_relvol_floor = input.float(0.85, "Adaptive CONT Relative Volume Floor", minval=0.0, step=0.05, group="V9.1 Optimization")
v91_tp1_scale_pct = 75.0
v91_tp2_scale_pct = 25.0
v91_runner_scale_pct = 0.0

// V9.3 compatibility presets (retained under V9.4). These let us test one change at a time
// while keeping the audited V9.2 accounting/runner math unchanged.
v93_preset = input.string("Adaptive CONT", "Optimization Preset", options=["Baseline", "Adaptive CONT"], group="V9.3 Optimization")
v93_cont_mode = v93_preset == "Baseline" ? "Baseline" : "Adaptive"
v93_runner_profile = "Current"
v93_tp1_scale_pct = 75.0
v93_tp2_scale_pct = 25.0
v93_runner_scale_pct = 0.0

// V9.4: setup-specific quality layer. STRUCTURE is intentionally untouched.
// REV requires a genuine direction-changing close/structure confirmation.
// CONT requires stronger 5M/15M participation, especially in midday/counter-trend conditions.
v94_mode = input.string("Off (V9.3)", "V9.4 Setup Filter", options=["Off (V9.3)", "Selective", "Strict"], group="V9.4 Selective Entries")
v94_rev_min_body_atr = input.float(0.55, "REV Min Body / ATR", minval=0.20, step=0.05, group="V9.4 Selective Entries")
v94_rev_min_relvol = input.float(0.85, "REV Min Relative Volume", minval=0.0, step=0.05, group="V9.4 Selective Entries")
v94_rev_close_location = input.float(0.65, "REV Min Close Location", minval=0.50, maxval=0.95, step=0.05, group="V9.4 Selective Entries")
v94_rev_require_vote_or_extreme = input.bool(true, "REV Require Vote Or Extreme Candle", group="V9.4 Selective Entries")
v94_rev_extreme_body_atr = input.float(0.85, "REV Extreme Body / ATR", minval=0.40, step=0.05, group="V9.4 Selective Entries")
v94_rev_extreme_relvol = input.float(1.10, "REV Extreme Relative Volume", minval=0.50, step=0.05, group="V9.4 Selective Entries")
v94_cont_min_mtf = input.int(3, "CONT Minimum MTF Score", minval=1, maxval=4, group="V9.4 Selective Entries")
v94_cont_min_relvol = input.float(0.90, "CONT Minimum Relative Volume", minval=0.0, step=0.05, group="V9.4 Selective Entries")
v94_cont_require_15m_or_votes = input.bool(true, "CONT Require 15M Agreement Or 2 Votes", group="V9.4 Selective Entries")
v94_cont_midday_extra_score = input.int(1, "CONT Midday Extra Quality", minval=0, maxval=2, group="V9.4 Selective Entries")
v94_show_blocked = input.bool(true, "Show Blocked Signal Counts", group="V9.4 Selective Entries")

// V9.5 diagnostics: no new entry filters. Segment realized expectancy by setup,
// time bucket, and 5M trend alignment so future filters can target only weak regimes.
v95_show_diagnostics = input.bool(true, "Show V9.5 Setup Segmentation", group="V9.5 Diagnostics")

v96_unified_alerts = input.bool(true, "Enable One Unified Alert Stream", group="V9.6 Alerts")
v96_include_levels = input.bool(true, "Include Entry / TP / SL Levels", group="V9.6 Alerts")
v96_shadow_json = input.bool(false, "Use Timestamped JSON For Shadow Parity", group="V9.6 Alerts")
v15_late4_enabled = input.bool(true, "Enable LATE4 Real Early Exit", group="V15 Risk Engine")
v15_show_audit = input.bool(true, "Show V15 Real-State Summary", group="V15 Risk Engine")
var int v15_early_fail_exits = 0
var int v15_early_fail_call_exits = 0
var int v15_early_fail_put_exits = 0
var float v15_early_fail_sum_r = 0.0

color_ema = input.color(color.blue, "EMA", group="Color Overrides")
color_ema_ma = input.color(color.yellow, "EMA Smoothing", group="Color Overrides")
color_vwap = input.color(#2962FF, "VWAP", group="Color Overrides")
color_vwap_band_1 = input.color(color.green, "VWAP Band 1", group="Color Overrides")
color_vwap_band_2 = input.color(color.olive, "VWAP Band 2", group="Color Overrides")
color_vwap_band_3 = input.color(color.teal, "VWAP Band 3", group="Color Overrides")
color_volume_ma = input.color(color.blue, "Volume MA", group="Color Overrides")
color_poki_up = input.color(color.blue, "Poki Bullish", group="Color Overrides")
color_poki_down = input.color(color.red, "Poki Bearish", group="Color Overrides")
color_signal_buy = input.color(color.green, "Buy Signals", group="Color Overrides")
color_signal_sell = input.color(color.red, "Sell Signals", group="Color Overrides")
color_vidya_up = input.color(#17dfad, "VIDYA Up", group="Color Overrides")
color_vidya_down = input.color(#dd326b, "VIDYA Down", group="Color Overrides")
width_ema = input.int(2, "EMA Width", minval=1, maxval=5, group="Line Width Overrides")
width_vwap = input.int(2, "VWAP Width", minval=1, maxval=5, group="Line Width Overrides")
width_volume_ma = input.int(2, "Volume MA Width", minval=1, maxval=5, group="Line Width Overrides")
width_poki = input.int(2, "Poki Width", minval=1, maxval=5, group="Line Width Overrides")
width_vidya = input.int(2, "VIDYA Width", minval=1, maxval=5, group="Line Width Overrides")
width_ualgo = input.int(2, "UAlgo Trendline Width", minval=1, maxval=5, group="Line Width Overrides")

// ============================================================================
// ORIGINAL MODULE: moving average exponential.pine
// ============================================================================
ema_len = input.int(9, minval=1, title="Length")
ema_src = input(close, title="Source")
ema_offset = input.int(title="Offset", defval=0, minval=-500, maxval=500, display = display.none)
ema_out = ta.ema(ema_src, ema_len)
plot(show_ema_module ? ema_out : na, title="EMA", color=color_ema, linewidth=width_ema, offset=ema_offset)

// Smoothing MA inputs
ema_GRP = "Smoothing"
ema_TT_BB = "Only applies when 'SMA + Bollinger Bands' is selected. Determines the distance between the SMA and the bands."
ema_maTypeInput = input.string("None", "Type", options = ["None", "SMA", "SMA + Bollinger Bands", "EMA", "SMMA (RMA)", "WMA", "VWMA"], group = ema_GRP, display = display.none)
var ema_isBB = ema_maTypeInput == "SMA + Bollinger Bands"
ema_maLengthInput = input.int(14, "Length", group = ema_GRP, display = display.none, active = ema_maTypeInput != "None")
ema_bbMultInput = input.float(2.0, "BB StdDev", minval = 0.001, maxval = 50, step = 0.5, tooltip = ema_TT_BB, group = ema_GRP, display = display.none, active = ema_isBB)
var ema_enableMA = ema_maTypeInput != "None"

// Smoothing MA Calculation
ema_ma(source, length, MAtype) =>
	switch MAtype
		"SMA"                   => ta.sma(source, length)
		"SMA + Bollinger Bands" => ta.sma(source, length)
		"EMA"                   => ta.ema(source, length)
		"SMMA (RMA)"            => ta.rma(source, length)
		"WMA"                   => ta.wma(source, length)
		"VWMA"                  => ta.vwma(source, length)

// Smoothing MA plots
ema_smoothingMA = ema_enableMA ? ema_ma(ema_out, ema_maLengthInput, ema_maTypeInput) : na
ema_smoothingStDev = ema_isBB ? ta.stdev(ema_out, ema_maLengthInput) * ema_bbMultInput : na
plot(show_ema_module ? ema_smoothingMA : na, "EMA-based MA", color=color_ema_ma, display = ema_enableMA and show_ema_module ? display.all : display.none, editable = ema_enableMA)
ema_bbUpperBand = plot(show_ema_module ? ema_smoothingMA + ema_smoothingStDev : na, title = "Upper Bollinger Band", color=color.green, display = ema_isBB and show_ema_module ? display.all : display.none, editable = ema_isBB)
ema_bbLowerBand = plot(show_ema_module ? ema_smoothingMA - ema_smoothingStDev : na, title = "Lower Bollinger Band", color=color.green, display = ema_isBB and show_ema_module ? display.all : display.none, editable = ema_isBB)
fill(ema_bbUpperBand, ema_bbLowerBand, color= ema_isBB ? color.new(color.green, 90) : na, title="Bollinger Bands Background Fill", display = ema_isBB ? display.all : display.none, editable = ema_isBB)

// ============================================================================
// ORIGINAL MODULE: VWAP.pine
// ============================================================================
vwap_hideonDWM = input(false, title="Hide VWAP on 1D or Above", group="VWAP Settings", display = display.none)
var vwap_anchor = input.string(defval = "Session", title="Anchor Period",
 options=["Session", "Week", "Month", "Quarter", "Year", "Decade", "Century", "Earnings", "Dividends", "Splits"], group="VWAP Settings")
vwap_src = input(title = "Source", defval = hlc3, group="VWAP Settings", display = display.none)
vwap_offset = input.int(0, title="Offset", group="VWAP Settings", display = display.none)

vwap_BANDS_GROUP = "Bands Settings"
vwap_CALC_MODE_TOOLTIP = "Determines the units used to calculate the distance of the bands. When 'Percentage' is selected, a multiplier of 1 means 1%."
vwap_calcModeInput = input.string("Standard Deviation", "Bands Calculation Mode", options = ["Standard Deviation", "Percentage"], group = vwap_BANDS_GROUP, tooltip = vwap_CALC_MODE_TOOLTIP, display = display.none)
vwap_showBand_1 = input(true, title = "", group = vwap_BANDS_GROUP, inline = "band_1", display = display.none)
vwap_bandMult_1 = input.float(1.0, title = "Bands Multiplier #1", group = vwap_BANDS_GROUP, inline = "band_1", step = 0.5, minval=0, display = display.none, active = vwap_showBand_1)
vwap_showBand_2 = input(false, title = "", group = vwap_BANDS_GROUP, inline = "band_2", display = display.none)
vwap_bandMult_2 = input.float(2.0, title = "Bands Multiplier #2", group = vwap_BANDS_GROUP, inline = "band_2", step = 0.5, minval=0, display = display.none, active = vwap_showBand_2)
vwap_showBand_3 = input(false, title = "", group = vwap_BANDS_GROUP, inline = "band_3", display = display.none)
vwap_bandMult_3 = input.float(3.0, title = "Bands Multiplier #3", group = vwap_BANDS_GROUP, inline = "band_3", step = 0.5, minval=0, display = display.none, active = vwap_showBand_3)

vwap_cumVolume = ta.cum(volume)
if barstate.islast and vwap_cumVolume == 0
    runtime.error("No volume is provided by the data vendor.")

vwap_isNewPeriod = switch vwap_anchor
	"Earnings" => 
		vwap_new_earnings_actual = request.earnings(syminfo.tickerid, earnings.actual, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		vwap_new_earnings_standardized = request.earnings(syminfo.tickerid, earnings.standardized, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(vwap_new_earnings_actual) or not na(vwap_new_earnings_standardized)
	"Dividends" => 
		vwap_new_dividends = request.dividends(syminfo.tickerid, dividends.gross, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(vwap_new_dividends)
	"Splits"    => 
		vwap_new_split = request.splits(syminfo.tickerid, splits.denominator, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(vwap_new_split)
	"Session"   => timeframe.change("D")
	"Week"      => timeframe.change("W")
	"Month"     => timeframe.change("M")
	"Quarter"   => timeframe.change("3M")
	"Year"      => timeframe.change("12M")
	"Decade"    => timeframe.change("12M") and year % 10 == 0
	"Century"   => timeframe.change("12M") and year % 100 == 0
	=> false

vwap_isEsdAnchor = vwap_anchor == "Earnings" or vwap_anchor == "Dividends" or vwap_anchor == "Splits"
if na(vwap_src[1]) and not vwap_isEsdAnchor
	vwap_isNewPeriod := true

float vwap_vwapValue = na
float vwap_upperBandValue1 = na
float vwap_lowerBandValue1 = na
float vwap_upperBandValue2 = na
float vwap_lowerBandValue2 = na
float vwap_upperBandValue3 = na
float vwap_lowerBandValue3 = na

if not (vwap_hideonDWM and timeframe.isdwm)
    [_vwap, _stdevUpper, _] = ta.vwap(vwap_src, vwap_isNewPeriod, 1)
	vwap_vwapValue := _vwap
    vwap_stdevAbs = _stdevUpper - _vwap
	vwap_bandBasis = vwap_calcModeInput == "Standard Deviation" ? vwap_stdevAbs : _vwap * 0.01
	vwap_upperBandValue1 := _vwap + vwap_bandBasis * vwap_bandMult_1
	vwap_lowerBandValue1 := _vwap - vwap_bandBasis * vwap_bandMult_1
	vwap_upperBandValue2 := _vwap + vwap_bandBasis * vwap_bandMult_2
	vwap_lowerBandValue2 := _vwap - vwap_bandBasis * vwap_bandMult_2
	vwap_upperBandValue3 := _vwap + vwap_bandBasis * vwap_bandMult_3
	vwap_lowerBandValue3 := _vwap - vwap_bandBasis * vwap_bandMult_3

plot(show_vwap_module ? vwap_vwapValue : na, title = "VWAP", color = color_vwap, linewidth=width_vwap, offset = vwap_offset)

vwap_displayBand1 = vwap_showBand_1 ? display.all : display.none
vwap_upperBand_1 = plot(show_vwap_module ? vwap_upperBandValue1 : na, title="Upper Band #1", color = color_vwap_band_1, offset = vwap_offset, display = vwap_displayBand1, editable = vwap_showBand_1)
vwap_lowerBand_1 = plot(show_vwap_module ? vwap_lowerBandValue1 : na, title="Lower Band #1", color = color_vwap_band_1, offset = vwap_offset, display = vwap_displayBand1, editable = vwap_showBand_1)
fill(vwap_upperBand_1, vwap_lowerBand_1,      title="Bands Fill #1", color = color.new(color_vwap_band_1, 95),   display = vwap_displayBand1, editable = vwap_showBand_1)

vwap_displayBand2 = vwap_showBand_2 ? display.all : display.none
vwap_upperBand_2 = plot(show_vwap_module ? vwap_upperBandValue2 : na, title="Upper Band #2", color = color_vwap_band_2, offset = vwap_offset, display = vwap_displayBand2, editable = vwap_showBand_2)
vwap_lowerBand_2 = plot(show_vwap_module ? vwap_lowerBandValue2 : na, title="Lower Band #2", color = color_vwap_band_2, offset = vwap_offset, display = vwap_displayBand2, editable = vwap_showBand_2)
fill(vwap_upperBand_2, vwap_lowerBand_2, title="Bands Fill #2", color = color.new(color_vwap_band_2, 95), display = vwap_displayBand2, editable = vwap_showBand_2)

vwap_displayBand3 = vwap_showBand_3 ? display.all : display.none
vwap_upperBand_3 = plot(show_vwap_module ? vwap_upperBandValue3 : na, title="Upper Band #3", color = color_vwap_band_3, offset = vwap_offset, display = vwap_displayBand3, editable = vwap_showBand_3)
vwap_lowerBand_3 = plot(show_vwap_module ? vwap_lowerBandValue3 : na, title="Lower Band #3", color = color_vwap_band_3, offset = vwap_offset, display = vwap_displayBand3, editable = vwap_showBand_3)
fill(vwap_upperBand_3, vwap_lowerBand_3, title="Bands Fill #3", color = color.new(color_vwap_band_3, 95), display = vwap_displayBand3, editable = vwap_showBand_3)

// ============================================================================
// ORIGINAL MODULE: volume.pine
// ============================================================================
// =====================================================
// INPUTS
// =====================================================

vol_volMALen = input.int(20, "Volume MA Length", minval=1)
vol_volSpikeMult = input.float(1.5, "Spike Multiplier", minval=0.1, step=0.1)
vol_showRawPlots = input.bool(false, "Show Raw Volume Plots", group="Volume Visuals")

// =====================================================
// VOLUME CALCULATIONS
// =====================================================

vol_volumeMA = ta.sma(volume, vol_volMALen)

vol_volumeRatio = vol_volumeMA > 0 ? volume / vol_volumeMA : 0.0

vol_volumeSpike = vol_volumeRatio >= vol_volSpikeMult

vol_volumeBull = close > open and vol_volumeSpike

vol_volumeBear = close < open and vol_volumeSpike

// =====================================================
// RAW VOLUME PLOTS
// =====================================================

plot(show_volume_module and vol_showRawPlots ? volume : na, title="Volume", style=plot.style_columns, color=close >= open ? color.green : color.red, display=show_volume_module and vol_showRawPlots ? display.pane : display.none)

plot(show_volume_module and vol_showRawPlots ? vol_volumeMA : na, title="Volume MA", color=color_volume_ma, linewidth=width_volume_ma, display=show_volume_module and vol_showRawPlots ? display.pane : display.none)

// =====================================================
// SPIKE MARKERS
// =====================================================

if vol_volumeBull
    label.new(bar_index, low, "VOL+", style=label.style_label_up, color=color.green, textcolor=color.white, size=size.tiny)
if vol_volumeBear
    label.new(bar_index, high, "VOL-", style=label.style_label_down, color=color.red, textcolor=color.white, size=size.tiny)

// Volume diagnostics remain calculated and feed the entry filters; data-window plots are omitted for Pine's plot budget.

// ============================================================================
// SHARED ENTRY QUALITY GATE
// ============================================================================
// These normalized components mirror the Python entry-quality filters. Pine's
// merged indicator has separate module signals rather than one weighted score,
// so strong context is represented by aligned VWAP/EMA direction.
entry_quality_clamp01(value) => math.max(0.0, math.min(1.0, value))

entry_quality_vwap = nz(vwap_vwapValue, close)
entry_quality_vwap_prev = nz(vwap_vwapValue[1], close[1])
entry_quality_rsi = ta.rsi(close, 14)
entry_quality_close_5 = nz(close[5], close)
entry_quality_return_5 = entry_quality_close_5 > 0 ? (close / entry_quality_close_5) - 1.0 : 0.0
entry_quality_moving_away_bull = close > entry_quality_vwap and (close - entry_quality_vwap) > (close[1] - entry_quality_vwap_prev)
entry_quality_moving_away_bear = close < entry_quality_vwap and (close - entry_quality_vwap) < (close[1] - entry_quality_vwap_prev)
entry_quality_strong_volume = vol_volumeRatio >= vol_volSpikeMult
entry_quality_bullish_candle = close > open
entry_quality_bearish_candle = close < open
entry_quality_recent_high = ta.highest(high, 20)[1]
entry_quality_recent_low = ta.lowest(low, 20)[1]

entry_quality_momentum_bull = 100.0 * (
         entry_quality_clamp01((entry_quality_rsi - 50.0) / 20.0) * 0.45
     + entry_quality_clamp01(entry_quality_return_5 / 0.01) * 0.35
     + (entry_quality_moving_away_bull ? 0.20 : 0.0))
entry_quality_momentum_bear = 100.0 * (
         entry_quality_clamp01((50.0 - entry_quality_rsi) / 20.0) * 0.45
     + entry_quality_clamp01((-entry_quality_return_5) / 0.01) * 0.35
     + (entry_quality_moving_away_bear ? 0.20 : 0.0))
entry_quality_pattern_bull = 100.0 * (
         (entry_quality_bullish_candle ? 0.35 : 0.0)
     + (close > entry_quality_recent_high ? 0.35 : 0.0)
     + (entry_quality_moving_away_bull ? 0.30 : 0.0))
entry_quality_pattern_bear = 100.0 * (
         (entry_quality_bearish_candle ? 0.35 : 0.0)
     + (close < entry_quality_recent_low ? 0.35 : 0.0)
     + (entry_quality_moving_away_bear ? 0.30 : 0.0))
entry_quality_vwap_extension = entry_quality_vwap > 0 ? math.abs(close - entry_quality_vwap) / entry_quality_vwap * 100.0 : 0.0
entry_quality_strong_bull_context = close > entry_quality_vwap and close > ema_out and ema_out > ema_out[1]
entry_quality_strong_bear_context = close < entry_quality_vwap and close < ema_out and ema_out < ema_out[1]
entry_quality_bull_momentum_rising = entry_quality_momentum_bull >= nz(entry_quality_momentum_bull[1], entry_quality_momentum_bull)
entry_quality_bear_momentum_rising = entry_quality_momentum_bear >= nz(entry_quality_momentum_bear[1], entry_quality_momentum_bear)

entry_quality_long_ok = not entry_quality_enabled or (
        entry_quality_vwap_extension <= entry_quality_max_vwap_extension
        and entry_quality_momentum_bull >= entry_quality_min_momentum
        and entry_quality_pattern_bull >= entry_quality_min_pattern
        and (not entry_quality_require_rising_momentum or entry_quality_bull_momentum_rising)
        and (not entry_quality_strong_bull_context or (
                entry_quality_momentum_bull >= entry_quality_strong_context_momentum
                and entry_quality_pattern_bull >= entry_quality_strong_context_pattern)))
entry_quality_short_ok = not entry_quality_enabled or (
        entry_quality_vwap_extension <= entry_quality_max_vwap_extension
        and entry_quality_momentum_bear >= entry_quality_min_momentum
        and entry_quality_pattern_bear >= entry_quality_min_pattern
        and (not entry_quality_require_rising_momentum or entry_quality_bear_momentum_rising)
        and (not entry_quality_strong_bear_context or (
                entry_quality_momentum_bear >= entry_quality_strong_context_momentum
                and entry_quality_pattern_bear >= entry_quality_strong_context_pattern)))


// Chart-timeframe values used by the V2 MTF/expansion layer.
// These are explicitly declared before the V2 calculations so the execution
// gate never references an undeclared series.
v2_1_atr = ta.atr(14)
v2_1_body_atr = v2_1_atr > 0 ? math.abs(close - open) / v2_1_atr : 0.0
v2_1_bull_candle = close > open
v2_1_bear_candle = close < open
v2_1_rel_vol = vol_volumeMA > 0 ? volume / vol_volumeMA : 0.0


// =============================================================================
// V2 MTF CONTEXT / REGIME / ROOM-TO-RUN
// =============================================================================
// Use the last completed 5M and 15M candles so context does not flip around during the active chart candle.
[v2_5_close, v2_5_ema, v2_5_vwap, v2_5_atr, v2_5_atr_avg, v2_5_swing_high, v2_5_swing_low, v2_5_close_3] = request.security(syminfo.tickerid, "5", [close[1], ta.ema(close, 20)[1], ta.vwap(hlc3)[1], ta.atr(14)[1], ta.sma(ta.atr(14), 20)[1], ta.highest(high, 12)[1], ta.lowest(low, 12)[1], close[4]], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)

[v2_15_close, v2_15_ema, v2_15_vwap] = request.security(syminfo.tickerid, "15", [close[1], ta.ema(close, 20)[1], ta.vwap(hlc3)[1]], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)

v2_5_bull = v2_5_close > v2_5_ema and v2_5_close > v2_5_vwap
v2_5_bear = v2_5_close < v2_5_ema and v2_5_close < v2_5_vwap
v2_15_bull = v2_15_close > v2_15_ema and v2_15_close > v2_15_vwap
v2_15_bear = v2_15_close < v2_15_ema and v2_15_close < v2_15_vwap

v2_call_mtf_score = (v2_5_bull ? 2 : 0) + (v2_15_bull ? 1 : 0) + ((not v2_5_bear and not v2_15_bear) ? 1 : 0)
v2_put_mtf_score = (v2_5_bear ? 2 : 0) + (v2_15_bear ? 1 : 0) + ((not v2_5_bull and not v2_15_bull) ? 1 : 0)

v2_call_mtf_ok = not v2_enabled or (v2_call_mtf_score >= 1 and (v2_allow_neutral_5m or not (not v2_5_bull and not v2_5_bear)) and (not v2_block_strong_mtf_opposite or not (v2_5_bear and v2_15_bear)))
v2_put_mtf_ok = not v2_enabled or (v2_put_mtf_score >= 1 and (v2_allow_neutral_5m or not (not v2_5_bull and not v2_5_bear)) and (not v2_block_strong_mtf_opposite or not (v2_5_bull and v2_15_bull)))

v2_call_room = v2_5_atr > 0 ? (v2_5_swing_high - close) / v2_5_atr : 999.0
v2_put_room = v2_5_atr > 0 ? (close - v2_5_swing_low) / v2_5_atr : 999.0
v2_room_call_ok = v2_call_room >= v2_min_room_atr or close > v2_5_swing_high
v2_room_put_ok = v2_put_room >= v2_min_room_atr or close < v2_5_swing_low

v2_extension = v2_5_atr > 0 ? math.abs(close - v2_5_vwap) / v2_5_atr : 0.0
v2_rsi = ta.rsi(close, 14)
v2_bull_exhaustion = v2_extension >= v2_max_extension_atr and v2_rsi >= v2_exhaust_rsi_high
v2_bear_exhaustion = v2_extension >= v2_max_extension_atr and v2_rsi <= v2_exhaust_rsi_low

v2_ema_vwap_width = v2_5_atr > 0 ? math.abs(v2_5_ema - v2_5_vwap) / v2_5_atr : 999.0
v2_churn_return = v2_5_atr > 0 ? math.abs(v2_5_close - v2_5_close_3) / v2_5_atr : 999.0
v2_chop = v2_ema_vwap_width <= v2_chop_ema_atr and v2_churn_return <= v2_chop_return_atr

v2_expansion_bull = v2_1_bull_candle and v2_1_body_atr >= v2_expansion_body_atr and v2_1_rel_vol >= v2_expansion_relvol and close > high[1]
v2_expansion_bear = v2_1_bear_candle and v2_1_body_atr >= v2_expansion_body_atr and v2_1_rel_vol >= v2_expansion_relvol and close < low[1]
v2_expansion_call_override = v2_allow_expansion_override and v2_expansion_bull and v2_5_bull and not (v2_5_bear and v2_15_bear)
v2_expansion_put_override = v2_allow_expansion_override and v2_expansion_bear and v2_5_bear and not (v2_5_bull and v2_15_bull)

// Final execution gates. They filter entry alerts/labels only; module exit logic
// remains unchanged.
v2_long_ok = entry_quality_long_ok and v2_call_mtf_ok and v2_room_call_ok and (not v2_bull_exhaustion or v2_expansion_call_override) and (not v2_chop or v2_expansion_call_override)
v2_short_ok = entry_quality_short_ok and v2_put_mtf_ok and v2_room_put_ok and (not v2_bear_exhaustion or v2_expansion_put_override) and (not v2_chop or v2_expansion_put_override)

// ============================================================================
// ORIGINAL MODULE: Poki.pine
// ============================================================================
// Parabolic SAR
poki_start = input.int(3, minval=0, maxval=10, title="Start - poki_Default = 2 - Multiplied by .01")
poki_increment = input.int(3, minval=0, maxval=10, title="Step Setting (Sensitivity) - poki_Default = 2 - Multiplied by .01" )
poki_maximum = input.int(3, minval=1, maxval=10, title="Maximum Step (Sensitivity) - poki_Default = 2 - Multiplied by .10")
poki_sus = input(true, "Show Up Trending Parabolic Sar")
poki_sds = input(true, "Show Down Trending Parabolic Sar")
poki_disc = input(false, title="Start and Step settings are *.01 so 2 = .02 etc, Maximum Step is *.10 so 2 = .2")
poki_startCalc = poki_start * .01
poki_incrementCalc = poki_increment * .01
poki_maximumCalc = poki_maximum * .10
poki_sarUp = ta.sar(poki_startCalc, poki_incrementCalc, poki_maximumCalc)
poki_sarDown = ta.sar(poki_startCalc, poki_incrementCalc, poki_maximumCalc)
poki_colUp = close >= poki_sarDown ? color_poki_up : na
poki_colDown = close <= poki_sarUp ? color_poki_down : na
plot(show_poki_module and poki_sus and not na(poki_sarUp) ? poki_sarUp : na, title="Up Trending SAR", style=plot.style_circles, linewidth=width_poki,color=poki_colUp)
plot(show_poki_module and poki_sds and not na(poki_sarDown) ? poki_sarDown : na, title="Up Trending SAR", style=plot.style_circles, linewidth=width_poki,color=poki_colDown)
// Input
poki_close_price = close[0]
poki_len = input.int(defval=50, minval=1, title="Linear Regression Length")

poki_linear_reg = ta.linreg(poki_close_price, poki_len, 0)
poki_linear_reg_prev = ta.linreg(close[1], poki_len, 0)
poki_slope = ((poki_linear_reg - poki_linear_reg_prev) / timeframe.in_seconds(timeframe.period))



poki_length = input.int(title="Bollinger Length", defval=20, minval=1)
poki_multiplier = input.float(title="Bollinger Deviation", defval=2, minval=1)
poki_overbought = input.int(title="Overbought", defval=1, minval=1)
poki_oversold = input.int(title="Oversold", defval=0, minval=0)
poki_custom_timeframe = input.bool(title="Use another Timeframe?", defval=false)
poki_highTimeFrame = input.timeframe(title="Select The Timeframe", defval="60")
poki_res1 = poki_custom_timeframe ? poki_highTimeFrame : timeframe.period

poki_smabasis = ta.sma(close, poki_length)
poki_stdev = ta.stdev(close, poki_length)
[poki_cierre, poki_alta, poki_baja, poki_basis1, poki_stdevb] = request.security(syminfo.tickerid, poki_res1, [close, high, low, ta.sma(close, poki_length), ta.stdev(close, poki_length)], lookahead=barmerge.lookahead_off)
poki_dev = poki_multiplier * poki_stdevb // ta.stdev(poki_cierre, poki_length)
poki_upper = poki_basis1 + poki_dev
poki_lower = poki_basis1 - poki_dev

poki_bbr = (poki_cierre - poki_lower)/(poki_upper - poki_lower)

// plot(poki_bbr)

// // MARCA LAS RESISTENCIAS
poki_pintarojo = 0.0
poki_pintarojo := nz(poki_pintarojo[1])
poki_pintarojo := poki_bbr[1] > poki_overbought and poki_bbr < poki_overbought ? poki_alta[1] :  nz(poki_pintarojo[1])
poki_p = plot(show_poki_module ? poki_pintarojo : na, color = color_poki_down, style=plot.style_circles, linewidth=2)

// // MARCA LOS SOPORTES
poki_pintaverde = 0.0
poki_pintaverde := nz(poki_pintaverde[1])
poki_pintaverde := poki_bbr[1] < poki_oversold and poki_bbr > poki_oversold ? poki_baja[1] :  nz(poki_pintaverde[1])
// Poki support circle plot omitted to stay under TradingView's 64-plot limit; support calculations remain active.
//
poki_method = input.string(defval="ATR", options=["ATR", "Traditional", "Part of Price"], title="Renko Assignment Method")
poki_methodvalue = input.float(defval=14.0, minval=0, title="Value")
poki_pricesource = input.string(defval="Close", options=["Close", "Open / Close", "High / Low"], title="Price Source")
poki_useClose = poki_pricesource == "Close"
poki_useOpenClose = poki_pricesource == "Open / Close" or poki_useClose
poki_useTrueRange = input.string(defval="Auto", options=["Always", "Auto", "Never"], title="Use True Range instead of Volume")
poki_isOscillating=input.bool(defval=true, title="Oscillating")
poki_normalize=input.bool(defval=false, title="Normalize")
poki_vol = poki_useTrueRange == "Always" or (poki_useTrueRange == "Auto" and na(volume))? ta.tr(true) : volume
poki_op = poki_useClose ? close : open
poki_hi = poki_useOpenClose ? close >= poki_op ? close : poki_op : high
poki_lo = poki_useOpenClose ? close <= poki_op ? close : poki_op : low

if poki_method == "ATR"
    poki_methodvalue := ta.atr(math.round(poki_methodvalue))
if poki_method == "Part of Price"
    poki_methodvalue := close/poki_methodvalue

var float poki_currclose = na
poki_prevclose = nz(poki_currclose[1])
poki_prevhigh = poki_prevclose + poki_methodvalue
poki_prevlow = poki_prevclose - poki_methodvalue
poki_currclose := poki_hi > poki_prevhigh ? poki_hi : poki_lo < poki_prevlow ? poki_lo : poki_prevclose

poki_direction = 0
poki_direction := poki_currclose > poki_prevclose ? 1 : poki_currclose < poki_prevclose ? -1 : nz(poki_direction[1])
poki_directionHasChanged = ta.change(poki_direction) != 0
poki_directionIsUp = poki_direction > 0
poki_directionIsDown = poki_direction < 0

poki_barcount = 1
poki_barcount := not poki_directionHasChanged and poki_normalize ? poki_barcount[1] + poki_barcount : poki_barcount
poki_vol := not poki_directionHasChanged ? poki_vol[1] + poki_vol : poki_vol
poki_res = poki_barcount > 1 ? poki_vol/poki_barcount : poki_vol


poki_x=poki_isOscillating and poki_directionIsDown ? -poki_res : poki_res

//
poki_z=input.int(title="Period", defval=32)


poki_n2ma=2*ta.wma(close,math.round(poki_z/2))
poki_nma=ta.wma(close,poki_z)
poki_diff=poki_n2ma-poki_nma
poki_sqn=math.round(math.sqrt(poki_z))


poki_n2ma1=2*ta.wma(close[1],math.round(poki_z/2))
poki_nma1=ta.wma(close[1],poki_z)
poki_diff1=poki_n2ma1-poki_nma1
poki_sqn1=math.round(math.sqrt(poki_z))


poki_n1=ta.wma(poki_diff,poki_sqn)
poki_n2=ta.wma(poki_diff1,poki_sqn)
poki_c=poki_n1>poki_n2?color.green:color.red



// Conditions

var bool poki_longCond = false
var bool poki_shortCond = false
poki_longCond :=  ta.crossover(poki_x,0) 
poki_shortCond :=  ta.crossunder(poki_x,0) 

// Count your long short conditions for more control with Pyramiding

poki_sectionLongs = 0
poki_sectionLongs := nz(poki_sectionLongs[1])
poki_sectionShorts = 0
poki_sectionShorts := nz(poki_sectionShorts[1])

if poki_longCond
    poki_sectionLongs := poki_sectionLongs + 1
    poki_sectionShorts := 0

if poki_shortCond
    poki_sectionLongs := 0
    poki_sectionShorts := poki_sectionShorts + 1
    
// Pyramiding

poki_pyrl = 1

// These check to see your signal and plot.style_cross references it against the pyramiding settings above

poki_longCondition = poki_longCond and poki_sectionLongs <= poki_pyrl 
poki_shortCondition = poki_shortCond and poki_sectionShorts <= poki_pyrl 

// Get the price of the last opened long or short

var float poki_last_open_longCondition = na
var float poki_last_open_shortCondition = na
poki_last_open_longCondition := poki_longCondition ? open : nz(poki_last_open_longCondition[1])
poki_last_open_shortCondition := poki_shortCondition ? open : nz(poki_last_open_shortCondition[1])

// Check if your last postion was a long or a short

var int poki_last_longCondition = na
var int poki_last_shortCondition = na
poki_last_longCondition := poki_longCondition ? time : nz(poki_last_longCondition[1])
poki_last_shortCondition := poki_shortCondition ? time : nz(poki_last_shortCondition[1])

poki_in_longCondition = poki_last_longCondition > poki_last_shortCondition
poki_in_shortCondition = poki_last_shortCondition > poki_last_longCondition

// Take profit

poki_isTPl = input(false, "Take Profit Long")
poki_isTPs = input(false, "Take Profit Short")
poki_tp = input.float(2, "Take Profit %")
poki_long_tp = poki_isTPl and ta.crossover(high, (1+(poki_tp/100))*poki_last_open_longCondition) and not poki_longCondition and poki_in_longCondition
poki_short_tp = poki_isTPs and ta.crossunder(low, (1-(poki_tp/100))*poki_last_open_shortCondition) and not poki_shortCondition and poki_in_shortCondition

// Stop Loss

poki_isSLl = input(false, "Stop Loss Long")
poki_isSLs = input(false, "Stop Loss Short")
poki_sl= 0.0
poki_sl := input.float(3, "Stop Loss %")
poki_long_sl = poki_isSLl and ta.crossunder(low, (1-(poki_sl/100))*poki_last_open_longCondition) and not poki_longCondition and poki_in_longCondition
poki_short_sl = poki_isSLs and ta.crossover(high, (1+(poki_sl/100))*poki_last_open_shortCondition) and not poki_shortCondition and poki_in_shortCondition

// Create a single close for all the different closing conditions.

poki_long_close = poki_long_tp or poki_long_sl
poki_short_close = poki_short_tp or poki_short_sl

// Get the time of the last close

var int poki_last_long_close = na
var int poki_last_short_close = na
poki_last_long_close := poki_long_close ? time : nz(poki_last_long_close[1])
poki_last_short_close := poki_short_close ? time : nz(poki_last_short_close[1])

//
// bullish signal rule: 
poki_bullishRule =poki_n1>poki_linear_reg
// bearish signal rule: 
poki_bearishRule =poki_n1<=poki_linear_reg
// current trading State
poki_ruleState = 0
poki_ruleState := poki_bullishRule ? 1 : poki_bearishRule ? -1 : nz(poki_ruleState[1])
// Alerts & Signals

poki_bton(b) => b ? 1 : 0
if (not v3_unified_enabled or v3_show_raw_module_entries) and poki_longCondition and v2_long_ok
    label.new(bar_index, low, "ENTRY CALL", style=label.style_label_up, color=color_signal_buy, textcolor=color.white, size=size.small)
if (not v3_unified_enabled or v3_show_raw_module_entries) and poki_shortCondition and v2_short_ok
    label.new(bar_index, high, "ENTRY PUT", style=label.style_label_down, color=color_signal_sell, textcolor=color.white, size=size.small)

if poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1])
    label.new(bar_index, high, "EXIT TAKE PROFIT", style=label.style_label_down, color=color.red, textcolor=color.white, size=size.small)
if poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1])
    label.new(bar_index, low, "EXIT TAKE PROFIT", style=label.style_label_up, color=color.lime, textcolor=color.white, size=size.small)

poki_ltp = poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1]) ? (1+(poki_tp/100))*poki_last_open_longCondition : na
plot(show_poki_module ? poki_ltp : na, style=plot.style_cross, linewidth=3, color = color.white, editable = false)
poki_stp = poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1]) ? (1-(poki_tp/100))*poki_last_open_shortCondition : na
plot(show_poki_module ? poki_stp : na, style = plot.style_cross, linewidth=3, color = color.white, editable = false)

if poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1])
    label.new(bar_index, high, "EXIT STOP LOSS", style=label.style_label_down, color=color.red, textcolor=color.white, size=size.small)
if poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1])
    label.new(bar_index, low, "EXIT STOP LOSS", style=label.style_label_up, color=color.lime, textcolor=color.white, size=size.small)

poki_lsl = poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1]) ? (1-(poki_sl/100))*poki_last_open_longCondition : na
plot(show_poki_module ? poki_lsl : na, style=plot.style_cross, linewidth=3, color = color.white, editable = false)
poki_ssl = poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1]) ? (1+(poki_sl/100))*poki_last_open_shortCondition : na
plot(show_poki_module ? poki_ssl : na, style = plot.style_cross, linewidth=3, color = color.white, editable = false)

alertcondition((not v3_unified_enabled) and poki_longCondition and v2_long_ok, title="Buy Alert")
alertcondition((not v3_unified_enabled) and poki_shortCondition and v2_short_ok, title="Sell Alert")
alertcondition(poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1]), title="Take Profit Long")
alertcondition(poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1]), title="Take Profit Short")
alertcondition(poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1]), title="Stop Loss Long")
alertcondition(poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1]), title="Stop Loss Short")

// ============================================================================
// ORIGINAL MODULE: smart money structure | gainzalgo.pine
// ============================================================================
// © GainzAlgo

smc_length = input.int(5, "Pivot Length", minval=1, maxval=20, step=1, tooltip="Number of bars to identify pivot highs and lows.")
smc_momentum_threshold_base = input.float(0.01, "Base Momentum Threshold (%)", minval=0.001, maxval=1.0, step=0.001, tooltip="Base percentage change for signals.")
smc_tp_points = input.int(10, "Take Profit (points)", minval=5, maxval=500, step=5)
smc_sl_points = input.int(10, "Stop Loss (points)", minval=5, maxval=500, step=5)
smc_min_signal_distance = input.int(5, "Min Signal Distance (bars)", minval=1, maxval=50, step=1)
smc_tp_box_height = input.float(0.5, "TP Box Height % (Optional)", minval=0.1, maxval=2.0, step=0.1)
smc_pre_momentum_factor_base = input.float(0.5, "Base Pre-Momentum Factor", minval=0.1, maxval=1.0, step=0.1, tooltip="Base factor for Get Ready signals.")
smc_shortTrendPeriod = input.int(30, title="Short Trend Period", minval=10, maxval=100)
smc_longTrendPeriod = input.int(100, title="Long Trend Period", minval=50, maxval=200)

smc_use_momentum_filter = input.bool(true, "Use Momentum Filter", group="Signal Filters", tooltip="Require price change to exceed momentum threshold.")
smc_use_trend_filter = input.bool(true, "Use Higher Timeframe Trend Filter", group="Signal Filters", tooltip="Require alignment with the selected higher timeframe trend.")
smc_higher_tf_choice = input.string("5M", "Higher Timeframe", options=["1M", "5M", "15M", "30M", "1H", "4H", "D"], group="Signal Filters", tooltip="Choose the timeframe for the higher timeframe filter.")
smc_use_lower_tf_filter = input.bool(true, "Use Lower Timeframe Filter", group="Signal Filters", tooltip="Prevent signals against the selected lower timeframe trend.")
smc_lower_tf_choice = input.string("5M", "Lower Timeframe", options=["1M", "5M", "15M", "30M", "1H", "4H", "D"], group="Signal Filters", tooltip="Choose the timeframe for the lower timeframe filter.")
smc_use_volume_filter = input.bool(true, "Use Volume Filter", group="Signal Filters", tooltip="Require volume above average (optional).")
smc_use_breakout_filter = input.bool(true, "Use Breakout Filter", group="Signal Filters", tooltip="Require price to break previous high/low (optional).")
smc_show_get_ready = input.bool(false, "Show Get Ready Signals", group="Signal Filters", tooltip="Enable or disable Get Ready signals.")
smc_restrict_repeated_signals = input.bool(true, "Restrict Repeated Signals", group="Signal Filters", tooltip="Prevent multiple signals in the same trend direction until trend changes.")
smc_restrict_trend_tf_choice = input.string("5M", "Restrict Trend Timeframe", options=["1M", "5M", "15M", "30M", "1H", "4H", "D"], group="Signal Filters", tooltip="Choose the timeframe to check trend for restricting repeated signals.")

smc_enable_liquidity_zones = input.bool(false, "Enable Liquidity Zone Detection", group="Advanced Analysis Tools", tooltip="Identifies potential liquidity pools and sweep zones")
smc_enable_market_profile = input.bool(true, "Enable Market Profile Analysis", group="Advanced Analysis Tools", tooltip="Shows order flow imbalance and institutional activity")
smc_enable_divergence_scanner = input.bool(true, "Enable Divergence Scanner", group="Advanced Analysis Tools", tooltip="Detects price and momentum divergences for reversal signals")
smc_enable_trend_analysis = input.bool(true, "Enable Trend Strength Matrix", group="Advanced Analysis Tools", tooltip="Show detailed predictions for future trends across timeframes.")

smc_volumeLongPeriod = input.int(50, "Long Volume Period", minval=10, maxval=100, group="Volume Filter Settings")
smc_volumeShortPeriod = input.int(5, "Short Volume Period", minval=1, maxval=20, group="Volume Filter Settings")
smc_breakoutPeriod = input.int(5, "Breakout Period", minval=1, maxval=50, group="Breakout Filter Settings")

smc_atr_raw = ta.atr(14)
atr = na(smc_atr_raw) and bar_index > 0 ? (high - low) : smc_atr_raw
smc_volatility_factor = atr / close
smc_momentum_threshold = smc_momentum_threshold_base * (1 + smc_volatility_factor * 2)
smc_pre_momentum_factor = smc_pre_momentum_factor_base * (1 - smc_volatility_factor * 0.5)
smc_pre_momentum_threshold = smc_momentum_threshold * smc_pre_momentum_factor

var float smc_raw_cvd = 0.0
smc_delta_volume = close > close[1] ? volume : close < close[1] ? -volume : 0
smc_raw_cvd := smc_raw_cvd + smc_delta_volume
smc_cvd_level = math.abs(smc_raw_cvd) < 10000 ? "Low" : math.abs(smc_raw_cvd) < 50000 ? "Medium" : "High"
smc_cvd_color = smc_raw_cvd > 0 ? color.lime : smc_raw_cvd < 0 ? color.red : color.yellow

smc_price_change = ((close - close[1]) / close[1]) * 100

smc_pivot_high = ta.pivothigh(high, smc_length, smc_length)
smc_pivot_low = ta.pivotlow(low, smc_length, smc_length)

var float smc_last_high = na
var float smc_last_low = na
if not na(smc_pivot_high)
    smc_last_high := smc_pivot_high
if not na(smc_pivot_low)
    smc_last_low := smc_pivot_low

var float smc_choch_sell_level = na
var float smc_choch_buy_level = na
var float smc_bos_sell_level = na
var float smc_bos_buy_level = na
var float smc_tp_sell_level = na
var float smc_tp_buy_level = na
var float smc_sl_sell_level = na
var float smc_sl_buy_level = na
var int smc_last_signal_bar = -smc_min_signal_distance - 1
var string smc_last_signal = "Neutral"
var int smc_last_trend = 0

[ema1M, vwap1M] = request.security(syminfo.tickerid, "1", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema5M, vwap5M, smc_momentum_5m, smc_volatility_5m, smc_volatility_avg_5m] = request.security(syminfo.tickerid, "5", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])
[ema15M, vwap15M, smc_momentum_15m, smc_volatility_15m, smc_volatility_avg_15m] = request.security(syminfo.tickerid, "15", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])
[ema30M, vwap30M, smc_momentum_30m, smc_volatility_30m, smc_volatility_avg_30m] = request.security(syminfo.tickerid, "30", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])
[ema1H, vwap1H, smc_momentum_1h, smc_volatility_1h, smc_volatility_avg_1h] = request.security(syminfo.tickerid, "60", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])
[ema4H, vwap4H, smc_momentum_4h, smc_volatility_4h, smc_volatility_avg_4h] = request.security(syminfo.tickerid, "240", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])
[emaD, vwapD, smc_momentum_d, smc_volatility_d, smc_volatility_avg_d] = request.security(syminfo.tickerid, "D", [ta.ema(close, 20), ta.vwap(hlc3), close - close[3], ta.atr(14), ta.sma(ta.atr(14), 20)])

smc_trend1M = close > ema1M and close > vwap1M ? 1 : close < ema1M and close < vwap1M ? -1 : 0
smc_trend5M = close > ema5M and close > vwap5M ? 1 : close < ema5M and close < vwap5M ? -1 : 0
smc_trend15M = close > ema15M and close > vwap15M ? 1 : close < ema15M and close < vwap15M ? -1 : 0
smc_trend30M = close > ema30M and close > vwap30M ? 1 : close < ema30M and close < vwap30M ? -1 : 0
smc_trend1H = close > ema1H and close > vwap1H ? 1 : close < ema1H and close < vwap1H ? -1 : 0
smc_trend4H = close > ema4H and close > vwap4H ? 1 : close < ema4H and close < vwap4H ? -1 : 0
smc_trendD = close > emaD and close > vwapD ? 1 : close < emaD and close < vwapD ? -1 : 0

smc_trend_strength_raw = smc_trend1M + smc_trend5M + smc_trend15M + smc_trend30M + smc_trend1H + smc_trend4H + smc_trendD
smc_trend_strength = (smc_trend_strength_raw / 7) * 100

var float smc_system_confidence = 50.0
if smc_trend_strength_raw == 7 or smc_trend_strength_raw == -7
    smc_system_confidence := 90.0
else if smc_trend_strength_raw >= 4 or smc_trend_strength_raw <= -4
    smc_system_confidence := 75.0
else if smc_trend_strength_raw >= 2 or smc_trend_strength_raw <= -2
    smc_system_confidence := 60.0
else
    smc_system_confidence := 50.0

var int smc_higher_tf_trend = 0
if smc_higher_tf_choice == "1M"
    smc_higher_tf_trend := smc_trend1M
else if smc_higher_tf_choice == "5M"
    smc_higher_tf_trend := smc_trend5M
else if smc_higher_tf_choice == "15M"
    smc_higher_tf_trend := smc_trend15M
else if smc_higher_tf_choice == "30M"
    smc_higher_tf_trend := smc_trend30M
else if smc_higher_tf_choice == "1H"
    smc_higher_tf_trend := smc_trend1H
else if smc_higher_tf_choice == "4H"
    smc_higher_tf_trend := smc_trend4H
else if smc_higher_tf_choice == "D"
    smc_higher_tf_trend := smc_trendD

smc_bullish_trend_ok = smc_higher_tf_trend == 1
smc_bearish_trend_ok = smc_higher_tf_trend == -1

var int smc_lower_tf_trend = 0
if smc_lower_tf_choice == "1M"
    smc_lower_tf_trend := smc_trend1M
else if smc_lower_tf_choice == "5M"
    smc_lower_tf_trend := smc_trend5M
else if smc_lower_tf_choice == "15M"
    smc_lower_tf_trend := smc_trend15M
else if smc_lower_tf_choice == "30M"
    smc_lower_tf_trend := smc_trend30M
else if smc_lower_tf_choice == "1H"
    smc_lower_tf_trend := smc_trend1H
else if smc_lower_tf_choice == "4H"
    smc_lower_tf_trend := smc_trend4H
else if smc_lower_tf_choice == "D"
    smc_lower_tf_trend := smc_trendD

smc_lower_tf_bullish = smc_lower_tf_trend == 1
smc_lower_tf_bearish = smc_lower_tf_trend == -1
smc_lower_tf_not_neutral = smc_lower_tf_trend != 0

var int smc_restrict_tf_trend = 0
if smc_restrict_trend_tf_choice == "1M"
    smc_restrict_tf_trend := smc_trend1M
else if smc_restrict_trend_tf_choice == "5M"
    smc_restrict_tf_trend := smc_trend5M
else if smc_restrict_trend_tf_choice == "15M"
    smc_restrict_tf_trend := smc_trend15M
else if smc_restrict_trend_tf_choice == "30M"
    smc_restrict_tf_trend := smc_trend30M
else if smc_restrict_trend_tf_choice == "1H"
    smc_restrict_tf_trend := smc_trend1H
else if smc_restrict_trend_tf_choice == "4H"
    smc_restrict_tf_trend := smc_trend4H
else if smc_restrict_trend_tf_choice == "D"
    smc_restrict_tf_trend := smc_trendD

smc_volAvg50 = ta.sma(volume, smc_volumeLongPeriod)
smc_volShort = ta.sma(volume, smc_volumeShortPeriod)
smc_volCondition = volume > smc_volAvg50 and ta.change(smc_volShort) > 0

smc_highestBreakout = ta.highest(high, smc_breakoutPeriod)
smc_lowestBreakout = ta.lowest(low, smc_breakoutPeriod)

smc_choch_sell = ta.crossunder(low, smc_last_high) and close < open
smc_choch_buy = ta.crossover(high, smc_last_low) and close > open
smc_bos_sell = ta.crossunder(low, smc_last_low[1]) and low < smc_last_low[1] and close < open
smc_bos_buy = ta.crossover(high, smc_last_high[1]) and high > smc_last_high[1] and close > open

smc_early_sell_signal = smc_use_momentum_filter ? smc_price_change < -smc_momentum_threshold : true
smc_early_buy_signal = smc_use_momentum_filter ? smc_price_change > smc_momentum_threshold : true

smc_sell_trend_ok = smc_use_trend_filter ? smc_bearish_trend_ok : true
smc_buy_trend_ok = smc_use_trend_filter ? smc_bullish_trend_ok : true

smc_sell_lower_tf_ok = smc_use_lower_tf_filter ? (not smc_lower_tf_bullish and smc_lower_tf_not_neutral) : true
smc_buy_lower_tf_ok = smc_use_lower_tf_filter ? (not smc_lower_tf_bearish and smc_lower_tf_not_neutral) : true

smc_sell_volume_ok = smc_use_volume_filter ? smc_volCondition : true
smc_buy_volume_ok = smc_use_volume_filter ? smc_volCondition : true

smc_sell_breakout_ok = smc_use_breakout_filter ? close < smc_lowestBreakout[1] : true
smc_buy_breakout_ok = smc_use_breakout_filter ? close > smc_highestBreakout[1] : true

smc_sell_allowed = not smc_restrict_repeated_signals or (smc_last_signal != "Sell" or (smc_last_signal == "Sell" and smc_restrict_tf_trend != smc_last_trend and smc_restrict_tf_trend != -1))
smc_buy_allowed = not smc_restrict_repeated_signals or (smc_last_signal != "Buy" or (smc_last_signal == "Buy" and smc_restrict_tf_trend != smc_last_trend and smc_restrict_tf_trend != 1))

smc_sell_condition = smc_early_sell_signal and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_sell_trend_ok and smc_sell_lower_tf_ok and smc_sell_volume_ok and smc_sell_breakout_ok and smc_sell_allowed
smc_buy_condition = smc_early_buy_signal and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_buy_trend_ok and smc_buy_lower_tf_ok and smc_buy_volume_ok and smc_buy_breakout_ok and smc_buy_allowed

alertcondition((not v3_unified_enabled) and smc_buy_condition and v2_long_ok, "SMC BUY", "SMC BUY on {{ticker}} {{interval}} at {{close}}")
alertcondition((not v3_unified_enabled) and smc_sell_condition and v2_short_ok, "SMC SELL", "SMC SELL on {{ticker}} {{interval}} at {{close}}")

smc_get_ready_sell = smc_use_momentum_filter ? (smc_price_change < -smc_pre_momentum_threshold and smc_price_change > -smc_momentum_threshold) : true and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_sell_trend_ok and smc_sell_lower_tf_ok and smc_sell_volume_ok and smc_sell_breakout_ok
smc_get_ready_buy = smc_use_momentum_filter ? (smc_price_change > smc_pre_momentum_threshold and smc_price_change < smc_momentum_threshold) : true and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_buy_trend_ok and smc_buy_lower_tf_ok and smc_buy_volume_ok and smc_buy_breakout_ok

if show_smc_module and smc_enable_liquidity_zones
    smc_lookback = 20
    smc_recent_high = ta.highest(high, smc_lookback)
    if high >= smc_recent_high * 0.9995 and high <= smc_recent_high * 1.0005 and bar_index > smc_lookback
        label.new(bar_index, high, "💧 LIQ", color=color.new(#FF6B35, 80), style=label.style_label_down, textcolor=#FF6B35, size=size.tiny)
    smc_recent_low = ta.lowest(low, smc_lookback)
    if low <= smc_recent_low * 1.0005 and low >= smc_recent_low * 0.9995 and bar_index > smc_lookback
        label.new(bar_index, low, "💧 LIQ", color=color.new(#FF6B35, 80), style=label.style_label_up, textcolor=#FF6B35, size=size.tiny)

if show_smc_module and smc_enable_market_profile
    var float smc_recent_buy_vol = 0.0
    var float smc_recent_sell_vol = 0.0
    if close > open
        smc_recent_buy_vol := ta.sma(volume, 20)
    else if close < open
        smc_recent_sell_vol := ta.sma(volume, 20)
    smc_vol_ratio = smc_recent_buy_vol > 0 and smc_recent_sell_vol > 0 ? smc_recent_buy_vol / (smc_recent_buy_vol + smc_recent_sell_vol) : 0.5
    smc_strong_buy_flow = smc_vol_ratio > 0.65 and volume > smc_volAvg50 * 1.5
    smc_strong_sell_flow = smc_vol_ratio < 0.35 and volume > smc_volAvg50 * 1.5
    if smc_strong_buy_flow
        label.new(bar_index, low, "🔥 BUY", color=color.new(#00D9FF, 70), style=label.style_label_up, textcolor=color.white, size=size.small)
    if smc_strong_sell_flow
        label.new(bar_index, high, "🔥 SELL", color=color.new(#FF006E, 70), style=label.style_label_down, textcolor=color.white, size=size.small)

if show_smc_module and smc_enable_divergence_scanner
    rsi = ta.rsi(close, 14)
    smc_price_lower_low = low < low[5] and low[5] < low[10]
    smc_rsi_higher_low = rsi > rsi[5] and rsi[5] > rsi[10]
    smc_bullish_divergence = smc_price_lower_low and smc_rsi_higher_low and rsi < 40
    smc_price_higher_high = high > high[5] and high[5] > high[10]
    smc_rsi_lower_high = rsi < rsi[5] and rsi[5] < rsi[10]
    smc_bearish_divergence = smc_price_higher_high and smc_rsi_lower_high and rsi > 60
    if smc_bullish_divergence
        label.new(bar_index, low, "⚡ BULL", color=color.new(#00F5FF, 60), style=label.style_label_up, textcolor=color.white, size=size.small)
    if smc_bearish_divergence
        label.new(bar_index, high, "⚡ BEAR", color=color.new(#C77DFF, 60), style=label.style_label_down, textcolor=color.white, size=size.small)

if show_smc_module and smc_show_get_ready and smc_get_ready_sell
    label.new(bar_index, high, "⚠ READY", color=color.new(#FFB627, 70), style=label.style_label_down, textcolor=color.white, size=size.small)
if show_smc_module and smc_show_get_ready and smc_get_ready_buy
    label.new(bar_index, low, "⚠ READY", color=color.new(#FFB627, 70), style=label.style_label_up, textcolor=color.white, size=size.small)

if show_smc_module and (not v3_unified_enabled or v3_show_raw_module_entries) and smc_sell_condition and v2_short_ok
    label.new(bar_index, high, "ENTRY PUT", color=color_signal_sell, style=label.style_label_down, textcolor=color.white, size=size.normal)
    smc_tp_sell_level := low - smc_tp_points
    smc_sl_sell_level := high + smc_sl_points
    smc_last_signal := "Sell"
    smc_last_signal_bar := bar_index
    smc_last_trend := smc_restrict_tf_trend

if show_smc_module and (not v3_unified_enabled or v3_show_raw_module_entries) and smc_buy_condition and v2_long_ok
    label.new(bar_index, low, "ENTRY CALL", color=color_signal_buy, style=label.style_label_up, textcolor=color.white, size=size.normal)
    smc_tp_buy_level := high + smc_tp_points
    smc_sl_buy_level := low - smc_sl_points
    smc_last_signal := "Buy"
    smc_last_signal_bar := bar_index
    smc_last_trend := smc_restrict_tf_trend

var line smc_choch_sell_line = na
var line smc_choch_buy_line = na
var line smc_bos_sell_line = na
var line smc_bos_buy_line = na
var box smc_choch_sell_box = na
var box smc_choch_buy_box = na
var box smc_bos_sell_box = na
var box smc_bos_buy_box = na

if show_smc_module and smc_choch_sell
    line.delete(smc_choch_sell_line)
    box.delete(smc_choch_sell_box)
    smc_choch_sell_level := smc_last_high
    smc_choch_sell_line := line.new(bar_index, smc_choch_sell_level, bar_index + 1, smc_choch_sell_level, color=color.new(#00E5FF, 0), style=line.style_solid, width=2)
    smc_choch_sell_box := box.new(bar_index - 2, smc_choch_sell_level * 1.001, bar_index + 5, smc_choch_sell_level * 0.999, bgcolor=color.new(#00E5FF, 95), border_color=color.new(#00E5FF, 80), border_width=1)
    label.new(bar_index, smc_choch_sell_level, "CHoCH", color=color.new(#00E5FF, 85), textcolor=#00E5FF, style=label.style_label_left, size=size.tiny)

if show_smc_module and smc_choch_buy
    line.delete(smc_choch_buy_line)
    box.delete(smc_choch_buy_box)
    smc_choch_buy_level := smc_last_low
    smc_choch_buy_line := line.new(bar_index, smc_choch_buy_level, bar_index + 1, smc_choch_buy_level, color=color.new(#76FF03, 0), style=line.style_solid, width=2)
    smc_choch_buy_box := box.new(bar_index - 2, smc_choch_buy_level * 1.001, bar_index + 5, smc_choch_buy_level * 0.999, bgcolor=color.new(#76FF03, 95), border_color=color.new(#76FF03, 80), border_width=1)
    label.new(bar_index, smc_choch_buy_level, "CHoCH", color=color.new(#76FF03, 85), textcolor=#76FF03, style=label.style_label_left, size=size.tiny)

if show_smc_module and smc_bos_sell
    line.delete(smc_bos_sell_line)
    box.delete(smc_bos_sell_box)
    smc_bos_sell_level := smc_last_low[1]
    smc_bos_sell_line := line.new(bar_index, smc_bos_sell_level, bar_index + 1, smc_bos_sell_level, color=color.new(#E040FB, 0), style=line.style_solid, width=2)
    smc_bos_sell_box := box.new(bar_index - 2, smc_bos_sell_level * 1.001, bar_index + 5, smc_bos_sell_level * 0.999, bgcolor=color.new(#E040FB, 95), border_color=color.new(#E040FB, 80), border_width=1)
    label.new(bar_index, smc_bos_sell_level, "BOS", color=color.new(#E040FB, 85), textcolor=#E040FB, style=label.style_label_left, size=size.tiny)

if show_smc_module and smc_bos_buy
    line.delete(smc_bos_buy_line)
    box.delete(smc_bos_buy_box)
    smc_bos_buy_level := smc_last_high[1]
    smc_bos_buy_line := line.new(bar_index, smc_bos_buy_level, bar_index + 1, smc_bos_buy_level, color=color.new(#00BFA5, 0), style=line.style_solid, width=2)
    smc_bos_buy_box := box.new(bar_index - 2, smc_bos_buy_level * 1.001, bar_index + 5, smc_bos_buy_level * 0.999, bgcolor=color.new(#00BFA5, 95), border_color=color.new(#00BFA5, 80), border_width=1)
    label.new(bar_index, smc_bos_buy_level, "BOS", color=color.new(#00BFA5, 85), textcolor=#00BFA5, style=label.style_label_left, size=size.tiny)

var line smc_sup = na
var line smc_res = na

if show_smc_module and barstate.islast
    float smc_lowest_y2 = 60000
    int smc_lowest_x2 = 0
    float smc_highest_y2 = 0
    int smc_highest_x2 = 0
    int smc_maxShortBars = math.min(smc_shortTrendPeriod, bar_index)
    for smc_i = 1 to smc_maxShortBars
        if low[smc_i] < smc_lowest_y2
            smc_lowest_y2 := low[smc_i]
            smc_lowest_x2 := smc_i
        if high[smc_i] > smc_highest_y2
            smc_highest_y2 := high[smc_i]
            smc_highest_x2 := smc_i
    float smc_lowest_y1 = 60000
    int smc_lowest_x1 = 0
    float smc_highest_y1 = 0
    int smc_highest_x1 = 0
    int smc_maxLongBars = math.min(smc_longTrendPeriod, bar_index)
    for smc_j = smc_shortTrendPeriod + 1 to smc_maxLongBars
        if low[smc_j] < smc_lowest_y1
            smc_lowest_y1 := low[smc_j]
            smc_lowest_x1 := smc_j
        if high[smc_j] > smc_highest_y1
            smc_highest_y1 := high[smc_j]
            smc_highest_x1 := smc_j
    int smc_trendStrength = smc_trend_strength_raw
    if smc_lowest_x1 > 0 and smc_lowest_x2 > 0
        line.delete(smc_sup)
        smc_sup := line.new(int(bar_index - smc_lowest_x1), smc_lowest_y1, int(bar_index - smc_lowest_x2), smc_lowest_y2, extend=extend.right, style=line.style_solid, width=3, color=smc_trendStrength >= 1 ? (smc_trendStrength >= 4 ? color.new(#00E676, 30) : smc_trendStrength >= 2 ? color.new(#76FF03, 40) : color.new(#FFEB3B, 50)) : color.new(color.gray, 60))
    if smc_highest_x1 > 0 and smc_highest_x2 > 0
        line.delete(smc_res)
        smc_res := line.new(int(bar_index - smc_highest_x1), smc_highest_y1, int(bar_index - smc_highest_x2), smc_highest_y2, extend=extend.right, style=line.style_solid, width=3, color=smc_trendStrength <= -1 ? (smc_trendStrength <= -4 ? color.new(#FF1744, 30) : smc_trendStrength <= -2 ? color.new(#E040FB, 40) : color.new(#FFEB3B, 50)) : color.new(color.gray, 60))

// SMC CHoCH/BOS levels remain visible through their source lines, boxes, and labels.

smc_score_5m = smc_trend5M + (smc_momentum_5m > 0 ? 0.5 : smc_momentum_5m < 0 ? -0.5 : 0) + (smc_volatility_5m > smc_volatility_avg_5m ? 0.5 : 0)
smc_score_15m = smc_trend15M + (smc_momentum_15m > 0 ? 0.5 : smc_momentum_15m < 0 ? -0.5 : 0) + (smc_volatility_15m > smc_volatility_avg_15m ? 0.5 : 0)
smc_score_30m = smc_trend30M + (smc_momentum_30m > 0 ? 0.5 : smc_momentum_30m < 0 ? -0.5 : 0) + (smc_volatility_30m > smc_volatility_avg_30m ? 0.5 : 0)
smc_score_1h = smc_trend1H + (smc_momentum_1h > 0 ? 0.5 : smc_momentum_1h < 0 ? -0.5 : 0) + (smc_volatility_1h > smc_volatility_avg_1h ? 0.5 : 0)
smc_score_4h = smc_trend4H + (smc_momentum_4h > 0 ? 0.5 : smc_momentum_4h < 0 ? -0.5 : 0) + (smc_volatility_4h > smc_volatility_avg_4h ? 0.5 : 0)
smc_score_d = smc_trendD + (smc_momentum_d > 0 ? 0.5 : smc_momentum_d < 0 ? -0.5 : 0) + (smc_volatility_d > smc_volatility_avg_d ? 0.5 : 0)

smc_pred_5m = smc_score_5m > 0.5 ? "▲" : smc_score_5m < -0.5 ? "▼" : "━"
smc_pred_15m = smc_score_15m > 0.5 ? "▲" : smc_score_15m < -0.5 ? "▼" : "━"
smc_pred_30m = smc_score_30m > 0.5 ? "▲" : smc_score_30m < -0.5 ? "▼" : "━"
smc_pred_1h = smc_score_1h > 0.5 ? "▲" : smc_score_1h < -0.5 ? "▼" : "━"
smc_pred_4h = smc_score_4h > 0.5 ? "▲" : smc_score_4h < -0.5 ? "▼" : "━"
smc_pred_d = smc_score_d > 0.5 ? "▲" : smc_score_d < -0.5 ? "▼" : "━"

var table smc_trendTable = table.new(position.top_right, columns=2, rows=11, bgcolor=color.new(#0A0E27, 15), border_width=1, border_color=color.new(#00D9FF, 50))

f_render_smc_stats() =>
    if show_smc_module and barstate.islast
        table.cell(smc_trendTable, 0, 0, "⚡ SMART MONEY", text_color=color.new(#00F5FF, 0), text_size=size.normal, bgcolor=color.new(#1A1F3A, 30))
        table.cell(smc_trendTable, 1, 0, "v3.0", text_color=color.new(#00D9FF, 40), text_size=size.small, bgcolor=color.new(#1A1F3A, 30))
        
        table.cell(smc_trendTable, 0, 1, "📊 Strength", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
        smc_strength_color = smc_trend_strength > 50 ? color.new(#00E676, 0) : smc_trend_strength > 0 ? color.new(#76FF03, 0) : smc_trend_strength > -50 ? color.new(#FF6B35, 0) : color.new(#FF1744, 0)
        table.cell(smc_trendTable, 1, 1, str.tostring(math.round(smc_trend_strength)), text_color=smc_strength_color, text_size=size.normal, bgcolor=color.new(#0F1629, 40))
        
        table.cell(smc_trendTable, 0, 2, "🎯 Confidence", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
        smc_conf_color = smc_system_confidence >= 75 ? color.new(#00E5FF, 0) : smc_system_confidence >= 60 ? color.new(#00D9FF, 20) : color.new(#FFB627, 0)
        table.cell(smc_trendTable, 1, 2, str.tostring(smc_system_confidence) + "%", text_color=smc_conf_color, text_size=size.normal, bgcolor=color.new(#0F1629, 40))
        
        table.cell(smc_trendTable, 0, 3, "💎 Volume", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
        smc_cvd_display = str.tostring(math.round(smc_raw_cvd / 1000)) + "K"
        table.cell(smc_trendTable, 1, 3, smc_cvd_display, text_color=smc_cvd_color, text_size=size.small, bgcolor=color.new(#0F1629, 40))
        
        table.cell(smc_trendTable, 0, 4, "1M", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 4, smc_trend1M == 1 ? "▲" : smc_trend1M == -1 ? "▼" : "━", text_color=smc_trend1M == 1 ? color.new(#76FF03, 0) : smc_trend1M == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 5, "5M", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 5, smc_trend5M == 1 ? "▲" : smc_trend5M == -1 ? "▼" : "━", text_color=smc_trend5M == 1 ? color.new(#76FF03, 0) : smc_trend5M == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 6, "15M", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 6, smc_trend15M == 1 ? "▲" : smc_trend15M == -1 ? "▼" : "━", text_color=smc_trend15M == 1 ? color.new(#76FF03, 0) : smc_trend15M == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 7, "30M", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 7, smc_trend30M == 1 ? "▲" : smc_trend30M == -1 ? "▼" : "━", text_color=smc_trend30M == 1 ? color.new(#76FF03, 0) : smc_trend30M == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 8, "1H", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 8, smc_trend1H == 1 ? "▲" : smc_trend1H == -1 ? "▼" : "━", text_color=smc_trend1H == 1 ? color.new(#76FF03, 0) : smc_trend1H == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 9, "4H", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 9, smc_trend4H == 1 ? "▲" : smc_trend4H == -1 ? "▼" : "━", text_color=smc_trend4H == 1 ? color.new(#76FF03, 0) : smc_trend4H == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))
        
        table.cell(smc_trendTable, 0, 10, "1D", text_color=color.new(#8B93FF, 30), text_size=size.tiny, bgcolor=color.new(#0A0E27, 50))
        table.cell(smc_trendTable, 1, 10, smc_trendD == 1 ? "▲" : smc_trendD == -1 ? "▼" : "━", text_color=smc_trendD == 1 ? color.new(#76FF03, 0) : smc_trendD == -1 ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.small, bgcolor=color.new(#0A0E27, 50))

f_render_smc_stats()

f_render_smc_ai() =>
    if show_smc_module and smc_enable_trend_analysis
        var table smc_ai_table = table.new(position.bottom_right, columns=7, rows=2, bgcolor=color.new(#0A0E27, 15), border_width=1, border_color=color.new(#E040FB, 50))
        
        if barstate.islast
            table.cell(smc_ai_table, 0, 0, "🔮 TREND", text_color=color.new(#E040FB, 0), text_size=size.small, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 1, 0, "5M", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 2, 0, "15M", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 3, 0, "30M", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 4, 0, "1H", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 5, 0, "4H", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            table.cell(smc_ai_table, 6, 0, "1D", text_color=color.new(#FFFFFF, 30), text_size=size.tiny, bgcolor=color.new(#1A1F3A, 30))
            
            table.cell(smc_ai_table, 0, 1, "Predict", text_color=color.new(#C77DFF, 20), text_size=size.tiny, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 1, 1, smc_pred_5m, text_color=smc_pred_5m == "▲" ? color.new(#00E676, 0) : smc_pred_5m == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 2, 1, smc_pred_15m, text_color=smc_pred_15m == "▲" ? color.new(#00E676, 0) : smc_pred_15m == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 3, 1, smc_pred_30m, text_color=smc_pred_30m == "▲" ? color.new(#00E676, 0) : smc_pred_30m == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 4, 1, smc_pred_1h, text_color=smc_pred_1h == "▲" ? color.new(#00E676, 0) : smc_pred_1h == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 5, 1, smc_pred_4h, text_color=smc_pred_4h == "▲" ? color.new(#00E676, 0) : smc_pred_4h == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))
            table.cell(smc_ai_table, 6, 1, smc_pred_d, text_color=smc_pred_d == "▲" ? color.new(#00E676, 0) : smc_pred_d == "▼" ? color.new(#FF1744, 0) : color.new(#FFB627, 40), text_size=size.normal, bgcolor=color.new(#0F1629, 40))

f_render_smc_ai()

// ============================================================================
// ORIGINAL MODULE: Price Action Toolkit Lite [UAlgo]
// ============================================================================
ualgo_zigzagBool = input.bool(false, "Show Market Structure", group="Price Action Toolkit Settings")
ualgo_zigzagLen = input.int(9, "ZigZag Length", group="Price Action Toolkit Settings")
ualgo_liquidityBool = input.bool(true, "Show Liquidity Sweeps", group="Liquidity Settings")
ualgo_liquidityLen = input.int(30, "Liquidity Length", minval=5, group="Liquidity Settings")
ualgo_orderblockBool = input.bool(true, "Show Order Blocks", group="Order Block Settings")
ualgo_numberObShow = input.int(2, "Number of Order Blocks to Show", minval=1, maxval=10, group="Order Block Settings")
ualgo_showTrendLines = input.bool(true, "Show Trend Lines", group="Misc")
ualgo_trendLineLength = input.int(20, "Trend Line Detection Sensitivity", minval=10, group="Misc")
ualgo_upTlColor = input.color(color.new(color.teal, 15), "Trend Line Colors", group="Misc", inline="ualgo_tl")
ualgo_downTlColor = input.color(color.new(color.red, 15), " ", group="Misc", inline="ualgo_tl")
ualgo_upColor = input.color(color.new(color.teal, 15), "Market/Liquidity Colors", group="Visual Settings", inline="ualgo_1")
ualgo_downColor = input.color(color.new(color.red, 15), " ", group="Visual Settings", inline="ualgo_1")
ualgo_bearishOrderblockColor = input.color(color.new(color.red, 80), "Order Block Colors ", group="Visual Settings", inline="ualgo_2")
ualgo_bullishOrderblockColor = input.color(color.new(color.teal, 80), " ", group="Visual Settings", inline="ualgo_2")
ualgo_supportFillColor = input.color(color.new(color.lime, 88), "Support Zone Fill", group="Visual Settings")
ualgo_supportLabelColor = input.color(color.new(color.green, 0), "Support Label", group="Visual Settings")
ualgo_resistanceLabelColor = input.color(color.new(color.red, 0), "Resistance Label", group="Visual Settings")
ualgo_hideWatermark = input.bool(false, "Hide Watermark", group="Visual Settings")

type ualgo_orderblock
    float value
    int barStart
    box block

type ualgo_liquidity
    float value
    line liquidityLine

var array<ualgo_orderblock> ualgo_bullishBlocks = array.new<ualgo_orderblock>()
var array<ualgo_orderblock> ualgo_bearishBlocks = array.new<ualgo_orderblock>()
var array<ualgo_liquidity> ualgo_bullishLiquidity = array.new<ualgo_liquidity>()
var array<ualgo_liquidity> ualgo_bearishLiquidity = array.new<ualgo_liquidity>()
var array<int> ualgo_highIndexes = array.new<int>()
var array<int> ualgo_lowIndexes = array.new<int>()
var array<float> ualgo_highValues = array.new<float>()
var array<float> ualgo_lowValues = array.new<float>()
var int ualgo_trend = 1
var bool ualgo_drawUp = false
var bool ualgo_drawDown = false
var string ualgo_lastState = na
var line ualgo_bearTrendline = na
var line ualgo_bullTrendline = na
var label ualgo_supportValueLabel = na
var label ualgo_supportValueLabel2 = na
var label ualgo_resistanceValueLabel = na
var label ualgo_resistanceValueLabel2 = na
var float ualgo_visibleSupportValue = na
var float ualgo_visibleSupportValue2 = na
var float ualgo_visibleResistanceValue = na
var float ualgo_visibleResistanceValue2 = na
var box ualgo_visibleSupportBlock = na
var box ualgo_visibleSupportBlock2 = na
var box ualgo_visibleResistanceBlock = na
var box ualgo_visibleResistanceBlock2 = na
ualgo_atr = ta.atr(14)
ualgo_toUp = high[ualgo_zigzagLen] >= ta.highest(high, ualgo_zigzagLen)
ualgo_toDown = low[ualgo_zigzagLen] <= ta.lowest(low, ualgo_zigzagLen)
ualgo_trend := ualgo_trend == 1 and ualgo_toDown ? -1 : ualgo_trend == -1 and ualgo_toUp ? 1 : ualgo_trend

if show_ualgo_module and ta.change(ualgo_trend) != 0 and ualgo_trend == 1
    array.push(ualgo_highIndexes, time[ualgo_zigzagLen])
    array.push(ualgo_highValues, high[ualgo_zigzagLen])
    if array.size(ualgo_lowValues) > 1
        if ualgo_zigzagBool
            line.new(array.get(ualgo_lowIndexes, array.size(ualgo_lowIndexes) - 1), array.get(ualgo_lowValues, array.size(ualgo_lowValues) - 1), array.get(ualgo_highIndexes, array.size(ualgo_highIndexes) - 1), array.get(ualgo_highValues, array.size(ualgo_highValues) - 1), xloc=xloc.bar_time, color=ualgo_upTlColor)
        ualgo_drawUp := false
    
if show_ualgo_module and ta.change(ualgo_trend) != 0 and ualgo_trend == -1
    array.push(ualgo_lowIndexes, time[ualgo_zigzagLen])
    array.push(ualgo_lowValues, low[ualgo_zigzagLen])
    if array.size(ualgo_highValues) > 1
        if ualgo_zigzagBool
            line.new(array.get(ualgo_highIndexes, array.size(ualgo_highIndexes) - 1), array.get(ualgo_highValues, array.size(ualgo_highValues) - 1), array.get(ualgo_lowIndexes, array.size(ualgo_lowIndexes) - 1), array.get(ualgo_lowValues, array.size(ualgo_lowValues) - 1), xloc=xloc.bar_time, color=ualgo_downTlColor)
        ualgo_drawDown := false

if show_ualgo_module and array.size(ualgo_lowValues) > 1 and not ualgo_drawDown and close < array.get(ualgo_lowValues, array.size(ualgo_lowValues) - 1)
    ualgo_level = array.get(ualgo_lowValues, array.size(ualgo_lowValues) - 1)
    ualgo_start = array.get(ualgo_lowIndexes, array.size(ualgo_lowIndexes) - 1)
    line.new(ualgo_start, ualgo_level, time, ualgo_level, xloc=xloc.bar_time, color=ualgo_downColor)
    label.new(bar_index, ualgo_level, na(ualgo_lastState) or ualgo_lastState == "up" ? "CHoCH" : "BoS", style=label.style_label_up, textcolor=ualgo_downColor, color=color.new(color.white, 100))
    ualgo_drawDown := true
    ualgo_lastState := "down"
    if ualgo_orderblockBool
        ualgo_block = ualgo_orderblock.new()
        ualgo_barMs = math.max(time - time[1], 1)
        ualgo_scanLength = math.min(4900, math.max(1, int((time - ualgo_start) / ualgo_barMs) - 1))
        float ualgo_maxHigh = 0.0
        int ualgo_maxHighTime = time
        for ualgo_scan = ualgo_scanLength to 0 by 1
            if high[ualgo_scan] > ualgo_maxHigh
                ualgo_maxHigh := high[ualgo_scan]
                ualgo_maxHighTime := time[ualgo_scan]
        ualgo_block.value := ualgo_maxHigh
        ualgo_block.barStart := ualgo_maxHighTime
        ualgo_block.block := box.new(ualgo_block.barStart, ualgo_block.value - ualgo_atr, time, ualgo_block.value, xloc=xloc.bar_time, bgcolor=ualgo_bearishOrderblockColor, border_color=ualgo_bearishOrderblockColor)
        array.push(ualgo_bearishBlocks, ualgo_block)
        if array.size(ualgo_bearishBlocks) > 20
            box.delete(array.shift(ualgo_bearishBlocks).block)

if show_ualgo_module and array.size(ualgo_highValues) > 1 and not ualgo_drawUp and close > array.get(ualgo_highValues, array.size(ualgo_highValues) - 1)
    ualgo_level = array.get(ualgo_highValues, array.size(ualgo_highValues) - 1)
    ualgo_start = array.get(ualgo_highIndexes, array.size(ualgo_highIndexes) - 1)
    line.new(ualgo_start, ualgo_level, time, ualgo_level, xloc=xloc.bar_time, color=ualgo_upColor)
    label.new(bar_index, ualgo_level, na(ualgo_lastState) or ualgo_lastState == "down" ? "CHoCH" : "BoS", style=label.style_label_down, textcolor=ualgo_upColor, color=color.new(color.white, 100))
    ualgo_drawUp := true
    ualgo_lastState := "up"
    if ualgo_orderblockBool
        ualgo_block = ualgo_orderblock.new()
        ualgo_barMs = math.max(time - time[1], 1)
        ualgo_scanLength = math.min(4900, math.max(1, int((time - ualgo_start) / ualgo_barMs) - 1))
        float ualgo_minLow = 999999999.0
        int ualgo_minLowTime = time
        for ualgo_scan = ualgo_scanLength to 0 by 1
            if low[ualgo_scan] < ualgo_minLow
                ualgo_minLow := low[ualgo_scan]
                ualgo_minLowTime := time[ualgo_scan]
        ualgo_block.value := ualgo_minLow
        ualgo_block.barStart := ualgo_minLowTime
        ualgo_block.block := box.new(ualgo_block.barStart, ualgo_block.value + ualgo_atr, time, ualgo_block.value, xloc=xloc.bar_time, bgcolor=ualgo_supportFillColor, border_color=ualgo_bullishOrderblockColor)
        array.push(ualgo_bullishBlocks, ualgo_block)
        if array.size(ualgo_bullishBlocks) > 20
            box.delete(array.shift(ualgo_bullishBlocks).block)

if show_ualgo_module and array.size(ualgo_bullishBlocks) > 0
    int ualgo_bullishVisible = 0
    ualgo_visibleSupportValue := na
    ualgo_visibleSupportValue2 := na
    ualgo_visibleSupportBlock := na
    ualgo_visibleSupportBlock2 := na
    for ualgo_i = array.size(ualgo_bullishBlocks) - 1 to 0
        ualgo_block = array.get(ualgo_bullishBlocks, ualgo_i)
        bool ualgo_isVisibleBullish = ualgo_bullishVisible < ualgo_numberObShow
        if ualgo_isVisibleBullish
            box.set_right(ualgo_block.block, time)
            ualgo_bullishVisible += 1
            if ualgo_bullishVisible == 1
                ualgo_visibleSupportBlock := ualgo_block.block
                ualgo_visibleSupportValue := box.get_bottom(ualgo_block.block)
            else if ualgo_bullishVisible == 2
                ualgo_visibleSupportBlock2 := ualgo_block.block
                ualgo_visibleSupportValue2 := box.get_bottom(ualgo_block.block)
        else
            box.set_right(ualgo_block.block, ualgo_block.barStart)
        if ualgo_isVisibleBullish and close < ualgo_block.value
            box.delete(ualgo_block.block)
            array.remove(ualgo_bullishBlocks, ualgo_i)

if show_ualgo_module and array.size(ualgo_bearishBlocks) > 0
    int ualgo_bearishVisible = 0
    ualgo_visibleResistanceValue := na
    ualgo_visibleResistanceValue2 := na
    ualgo_visibleResistanceBlock := na
    ualgo_visibleResistanceBlock2 := na
    for ualgo_i = array.size(ualgo_bearishBlocks) - 1 to 0
        ualgo_block = array.get(ualgo_bearishBlocks, ualgo_i)
        bool ualgo_isVisibleBearish = ualgo_bearishVisible < ualgo_numberObShow
        if ualgo_isVisibleBearish
            box.set_right(ualgo_block.block, time)
            ualgo_bearishVisible += 1
            if ualgo_bearishVisible == 1
                ualgo_visibleResistanceBlock := ualgo_block.block
                ualgo_visibleResistanceValue := box.get_top(ualgo_block.block)
            else if ualgo_bearishVisible == 2
                ualgo_visibleResistanceBlock2 := ualgo_block.block
                ualgo_visibleResistanceValue2 := box.get_top(ualgo_block.block)
        else
            box.set_right(ualgo_block.block, ualgo_block.barStart)
        if ualgo_isVisibleBearish and close > ualgo_block.value
            box.delete(ualgo_block.block)
            array.remove(ualgo_bearishBlocks, ualgo_i)

if barstate.islast
    label.delete(ualgo_supportValueLabel)
    label.delete(ualgo_supportValueLabel2)
    label.delete(ualgo_resistanceValueLabel)
    label.delete(ualgo_resistanceValueLabel2)
    if show_ualgo_module and not na(ualgo_visibleSupportValue)
        ualgo_supportValueLabel := label.new(time, box.get_bottom(ualgo_visibleSupportBlock), "UAlgo Support Low " + str.tostring(box.get_bottom(ualgo_visibleSupportBlock), format.mintick), xloc=xloc.bar_time, style=label.style_label_left, color=ualgo_supportLabelColor, textcolor=color.white, size=size.tiny)
        if not na(ualgo_visibleSupportValue2) and ualgo_numberObShow > 1
            ualgo_supportValueLabel2 := label.new(time, box.get_bottom(ualgo_visibleSupportBlock2), "UAlgo Support Low " + str.tostring(box.get_bottom(ualgo_visibleSupportBlock2), format.mintick), xloc=xloc.bar_time, style=label.style_label_left, color=ualgo_supportLabelColor, textcolor=color.white, size=size.tiny)
    if show_ualgo_module and not na(ualgo_visibleResistanceValue)
        ualgo_resistanceValueLabel := label.new(time, ualgo_visibleResistanceValue, "Resistance " + str.tostring(ualgo_visibleResistanceValue, format.mintick), xloc=xloc.bar_time, style=label.style_label_left, color=ualgo_resistanceLabelColor, textcolor=color.white, size=size.tiny)
        if not na(ualgo_visibleResistanceValue2) and ualgo_numberObShow > 1
            ualgo_resistanceValueLabel2 := label.new(time, ualgo_visibleResistanceValue2, "Resistance " + str.tostring(ualgo_visibleResistanceValue2, format.mintick), xloc=xloc.bar_time, style=label.style_label_left, color=ualgo_resistanceLabelColor, textcolor=color.white, size=size.tiny)

ualgo_ph = ta.pivothigh(high, ualgo_liquidityLen, ualgo_liquidityLen)
ualgo_pl = ta.pivotlow(low, ualgo_liquidityLen, ualgo_liquidityLen)
if show_ualgo_module and ualgo_liquidityBool and not na(ualgo_ph)
    ualgo_liq = ualgo_liquidity.new(high[ualgo_liquidityLen], line.new(time[ualgo_liquidityLen], high[ualgo_liquidityLen], time, high[ualgo_liquidityLen], xloc=xloc.bar_time, color=ualgo_downColor))
    array.push(ualgo_bearishLiquidity, ualgo_liq)
    if array.size(ualgo_bearishLiquidity) > 7
        line.delete(array.shift(ualgo_bearishLiquidity).liquidityLine)
if show_ualgo_module and ualgo_liquidityBool and not na(ualgo_pl)
    ualgo_liq = ualgo_liquidity.new(low[ualgo_liquidityLen], line.new(time[ualgo_liquidityLen], low[ualgo_liquidityLen], time, low[ualgo_liquidityLen], xloc=xloc.bar_time, color=ualgo_upColor))
    array.push(ualgo_bullishLiquidity, ualgo_liq)
    if array.size(ualgo_bullishLiquidity) > 7
        line.delete(array.shift(ualgo_bullishLiquidity).liquidityLine)

if show_ualgo_module and array.size(ualgo_bearishLiquidity) > 0
    for ualgo_i = array.size(ualgo_bearishLiquidity) - 1 to 0
        ualgo_liq = array.get(ualgo_bearishLiquidity, ualgo_i)
        line.set_x2(ualgo_liq.liquidityLine, time)
        if high > ualgo_liq.value
            line.set_style(ualgo_liq.liquidityLine, line.style_dashed)
            if close < ualgo_liq.value
                label.new(bar_index, high, "x", style=label.style_label_down, textcolor=ualgo_downColor, color=color.new(color.white, 100))
            array.remove(ualgo_bearishLiquidity, ualgo_i)

if show_ualgo_module and array.size(ualgo_bullishLiquidity) > 0
    for ualgo_i = array.size(ualgo_bullishLiquidity) - 1 to 0
        ualgo_liq = array.get(ualgo_bullishLiquidity, ualgo_i)
        line.set_x2(ualgo_liq.liquidityLine, time)
        if low < ualgo_liq.value
            line.set_style(ualgo_liq.liquidityLine, line.style_dashed)
            if close > ualgo_liq.value
                label.new(bar_index, low, "x", style=label.style_label_up, textcolor=ualgo_upColor, color=color.new(color.white, 100))
            array.remove(ualgo_bullishLiquidity, ualgo_i)

ualgo_getSlope(startIndex, startValue, endIndex, endValue) =>
    (endValue - startValue) / (endIndex - startIndex)

ualgo_extendTrendline(lineId, startIndex, startValue, endIndex, endValue) =>
    ualgo_slope = ualgo_getSlope(startIndex, startValue, endIndex, endValue)
    line.set_x2(lineId, bar_index)
    line.set_y2(lineId, startValue + ualgo_slope * (bar_index - startIndex))

if show_ualgo_module and ualgo_showTrendLines
    ualgo_phTrend = ta.pivothigh(high, ualgo_trendLineLength, ualgo_trendLineLength)
    ualgo_plTrend = ta.pivotlow(low, ualgo_trendLineLength, ualgo_trendLineLength)
    ualgo_bullStart = ta.valuewhen(not na(ualgo_plTrend), bar_index[ualgo_trendLineLength], 1)
    ualgo_bullEnd = ta.valuewhen(not na(ualgo_plTrend), bar_index[ualgo_trendLineLength], 0)
    ualgo_bearStart = ta.valuewhen(not na(ualgo_phTrend), bar_index[ualgo_trendLineLength], 1)
    ualgo_bearEnd = ta.valuewhen(not na(ualgo_phTrend), bar_index[ualgo_trendLineLength], 0)
    ualgo_bullStartValue = ta.valuewhen(not na(ualgo_plTrend), low[ualgo_trendLineLength], 1)
    ualgo_bullEndValue = ta.valuewhen(not na(ualgo_plTrend), low[ualgo_trendLineLength], 0)
    ualgo_bearStartValue = ta.valuewhen(not na(ualgo_phTrend), high[ualgo_trendLineLength], 1)
    ualgo_bearEndValue = ta.valuewhen(not na(ualgo_phTrend), high[ualgo_trendLineLength], 0)
    ualgo_hasBearPivots = not na(ualgo_bearStart) and not na(ualgo_bearEnd) and ualgo_bearStart != ualgo_bearEnd
    ualgo_hasBullPivots = not na(ualgo_bullStart) and not na(ualgo_bullEnd) and ualgo_bullStart != ualgo_bullEnd
    ualgo_bearSlope = ualgo_hasBearPivots ? ualgo_getSlope(ualgo_bearStart, ualgo_bearStartValue, ualgo_bearEnd, ualgo_bearEndValue) : na
    ualgo_bullSlope = ualgo_hasBullPivots ? ualgo_getSlope(ualgo_bullStart, ualgo_bullStartValue, ualgo_bullEnd, ualgo_bullEndValue) : na
    line.delete(ualgo_bearTrendline)
    line.delete(ualgo_bullTrendline)
    if ualgo_hasBearPivots and ualgo_bearSlope < 0
        ualgo_bearTrendline := line.new(ualgo_bearStart, ualgo_bearStartValue, bar_index, ualgo_bearEndValue, color=ualgo_downTlColor, width=width_ualgo)
    if ualgo_hasBullPivots and ualgo_bullSlope > 0
        ualgo_bullTrendline := line.new(ualgo_bullStart, ualgo_bullStartValue, bar_index, ualgo_bullEndValue, color=ualgo_upTlColor, width=width_ualgo)
    if not na(ualgo_bearTrendline)
        ualgo_extendTrendline(ualgo_bearTrendline, ualgo_bearStart, ualgo_bearStartValue, ualgo_bearEnd, ualgo_bearEndValue)
    if not na(ualgo_bullTrendline)
        ualgo_extendTrendline(ualgo_bullTrendline, ualgo_bullStart, ualgo_bullStartValue, ualgo_bullEnd, ualgo_bullEndValue)

f_render_ualgo_watermark() =>
    if show_ualgo_module and not ualgo_hideWatermark
        var table ualgo_watermark = table.new(position.top_center, 1, 1)
        if barstate.islast
            table.cell(ualgo_watermark, 0, 0, "UAlgo", text_color=color.orange, text_size=size.large)

f_render_ualgo_watermark()

// ============================================================================
// ORIGINAL MODULE: Volumatic Variable Index Dynamic Average [BigBeluga]
// ============================================================================
vv_length = input.int(10, 'VIDYA Length')
vv_momentum = input.int(20, 'VIDYA Momentum')
vv_band_distance = input.float(2, 'Distance factor for upper/lower bands', step = 0.1)
vv_pivot_left_bars = 3
vv_pivot_right_bars = vv_pivot_left_bars

vv_source = input.source(close, 'Source')

vv_up_trend_color = input(#17dfad, '+', group = 'VIDYA Color', inline = 'vv_c')
vv_down_trend_color = input(#dd326b, '-', group = 'VIDYA Color', inline = 'vv_c')
vv_shadow = input.bool(true, 'Shadow', group = 'VIDYA Color', inline = 'vv_c')

var line vv_pivot_line = na
var float vv_volume_value = na
float vv_smoothed_value = na
var bool vv_is_trend_up = false

var array<line> vv_liquidity_lines_low = array.new<line>()
var array<line> vv_liquidity_lines_high = array.new<line>()

var float vv_up_trend_volume = na
var float vv_down_trend_volume = na

vv_vidya_calc(src, length, momentum) =>
    float mom = ta.change(src)
    float sum_pos = math.sum(mom >= 0 ? mom : 0.0, momentum)
    float sum_neg = math.sum(mom >= 0 ? 0.0 : -mom, momentum)
    float abs_cmo = math.abs(100 * (sum_pos - sum_neg) / (sum_pos + sum_neg))
    float alpha = 2 / (length + 1)
    var float vidya = 0.0
    vidya := alpha * abs_cmo / 100 * src + (1 - alpha * abs_cmo / 100) * nz(vidya[1])
    ta.sma(vidya, 15)

method vv_extend_liquidity_lines(array<line> line_array, float price_level, bool is_cross, volume_val) =>
    if line_array.size() > 0 and last_bar_index - bar_index < 5000
        for i = 0 to line_array.size() - 1 by 1
            if i < line_array.size()
                line liquidity_line = line_array.get(i)
                float current_line_level = line.get_y2(liquidity_line)
                bool price_cross = is_cross ? price_level < current_line_level and price_level[1] >= current_line_level : price_level > current_line_level and price_level[1] <= current_line_level

                bool is_short_line = time - line.get_x1(liquidity_line) < 50 * math.max(time - time[1], 1)

                if price_cross and is_short_line
                    line.set_x2(liquidity_line, bar_index)
                    line_array.remove(i)

                    label.new(time[1], price_level[1], str.tostring(volume_val, format.volume), xloc=xloc.bar_time, color = color.rgb(0, 0, 0, 99), style = is_cross ? label.style_label_lower_left : label.style_label_upper_left, textcolor = chart.fg_color, size = size.small)

                    label.new(time[1], price_level[1], text = '◉', xloc=xloc.bar_time, color = #00000003, textcolor = is_cross ? vv_down_trend_color : vv_up_trend_color, style = label.style_label_center, size = size.normal)

float vv_atr_value = ta.atr(200)

vv_vidya_value = vv_vidya_calc(vv_source, vv_length, vv_momentum)

float vv_upper_band = vv_vidya_value + vv_atr_value * vv_band_distance
float vv_lower_band = vv_vidya_value - vv_atr_value * vv_band_distance

if ta.crossover(vv_source, vv_upper_band)
    vv_is_trend_up := true
    vv_is_trend_up
if ta.crossunder(vv_source, vv_lower_band)
    vv_is_trend_up := false
    vv_is_trend_up

if vv_is_trend_up
    vv_smoothed_value := vv_lower_band
    vv_smoothed_value
if not vv_is_trend_up
    vv_smoothed_value := vv_upper_band
    vv_smoothed_value
if ta.change(vv_is_trend_up)
    vv_smoothed_value := na
    vv_smoothed_value

bool vv_pivot_high = not na(ta.pivothigh(vv_pivot_left_bars, vv_pivot_right_bars))
bool vv_pivot_low = not na(ta.pivotlow(close, vv_pivot_left_bars, vv_pivot_right_bars))

if low[vv_pivot_right_bars] > vv_smoothed_value and vv_pivot_low
    vv_pivot_line := line.new(bar_index[vv_pivot_right_bars], low[vv_pivot_right_bars], bar_index[vv_pivot_right_bars] + 5, low[vv_pivot_right_bars], color = color.new(vv_up_trend_color, 50))

    vv_liquidity_lines_low.push(vv_pivot_line)
    vv_volume_value := math.sum(volume, vv_pivot_right_bars + vv_pivot_left_bars) / (vv_pivot_right_bars + vv_pivot_left_bars)
    vv_volume_value

if high[vv_pivot_right_bars] < vv_smoothed_value and vv_pivot_high
    vv_pivot_line := line.new(bar_index[vv_pivot_right_bars], high[vv_pivot_right_bars], bar_index[vv_pivot_right_bars] + 5, high[vv_pivot_right_bars], color = color.new(vv_down_trend_color, 50))

    vv_liquidity_lines_high.push(vv_pivot_line)
    vv_volume_value := math.sum(-volume, vv_pivot_right_bars + vv_pivot_left_bars) / (vv_pivot_right_bars + vv_pivot_left_bars)
    vv_volume_value

vv_liquidity_lines_high.vv_extend_liquidity_lines(vv_smoothed_value, true, vv_volume_value)
vv_liquidity_lines_low.vv_extend_liquidity_lines(vv_smoothed_value, false, vv_volume_value)

bool vv_trend_cross_up = not vv_is_trend_up[1] and vv_is_trend_up
bool vv_trend_cross_down = not vv_is_trend_up and vv_is_trend_up[1]

if ta.change(vv_trend_cross_up) or ta.change(vv_trend_cross_down)
    vv_up_trend_volume := 0
    vv_down_trend_volume := 0
    vv_down_trend_volume

if not(ta.change(vv_trend_cross_up) or ta.change(vv_trend_cross_down))
    vv_up_trend_volume := vv_up_trend_volume + (close > open ? volume : 0)
    vv_down_trend_volume := vv_down_trend_volume + (close < open ? volume : 0)
    vv_down_trend_volume

float vv_avg_volume_delta = (vv_up_trend_volume + vv_down_trend_volume) / 2

color vv_trend_color = vv_is_trend_up ? color_vidya_up : not vv_is_trend_up ? color_vidya_down : chart.fg_color

string vv_delta_volume = str.tostring((vv_up_trend_volume - vv_down_trend_volume) / vv_avg_volume_delta * 100, format.percent) == 'NaN%' ? '0%' : str.tostring((vv_up_trend_volume - vv_down_trend_volume) / vv_avg_volume_delta * 100, format.percent)

f_render_vidya_last_labels() =>
    if barstate.islast
        label.delete(label.new(bar_index, vv_smoothed_value, 'Buy: ' + str.tostring(vv_up_trend_volume, format.volume) + '\n Sell: ' + str.tostring(vv_down_trend_volume, format.volume) + '\nDelta Volume: ' + vv_delta_volume, color = color.new(vv_trend_color, 90), style = vv_is_trend_up ? label.style_label_upper_left : label.style_label_lower_left, textcolor = chart.fg_color)[1])

        label.delete(label.new(bar_index, vv_smoothed_value, text = '✪', color = #00000003, textcolor = vv_trend_color, style = label.style_label_center, size = size.large)[1])

f_render_vidya_last_labels()

vv_p1 = plot(show_vidya_module ? vv_smoothed_value : na, color = vv_trend_color, linewidth = width_vidya, style = plot.style_linebr)
vv_p2 = plot(show_vidya_module ? hl2 : na, display = display.none)

fill(vv_p1, vv_p2, vv_smoothed_value, hl2, color.new(vv_trend_color, vv_shadow ? 80 : 100), na)

plotshape(series = show_vidya_module and (not v3_unified_enabled or v3_show_raw_module_entries) and vv_trend_cross_up[1] and v2_long_ok ? vv_smoothed_value[0] : na, title = 'VIDYA Entry Call', style = shape.labelup, location = location.absolute, color = color.new(vv_up_trend_color, 50), text = 'ENTRY CALL', textcolor = chart.fg_color)
plotshape(series = show_vidya_module and (not v3_unified_enabled or v3_show_raw_module_entries) and vv_trend_cross_down[1] and v2_short_ok ? vv_smoothed_value[0] : na, title = 'VIDYA Entry Put', style = shape.labeldown, location = location.absolute, color = color.new(vv_down_trend_color, 50), text = 'ENTRY PUT', textcolor = chart.fg_color)

alertcondition((not v3_unified_enabled) and vv_trend_cross_up and v2_long_ok, "VIDYA Trend Up", "VIDYA trend turned up on {{ticker}} {{interval}}")
alertcondition((not v3_unified_enabled) and vv_trend_cross_down and v2_short_ok, "VIDYA Trend Down", "VIDYA trend turned down on {{ticker}} {{interval}}")

// =============================================================================
// V3 / V4 UNIFIED ENTRY ENGINE
// =============================================================================
// Raw module votes. These no longer create separate ENTRY labels while unified mode is on.
v3_poki_long = poki_longCondition and v2_long_ok
v3_poki_short = poki_shortCondition and v2_short_ok
v3_smc_long = smc_buy_condition and v2_long_ok
v3_smc_short = smc_sell_condition and v2_short_ok
v3_vidya_long = vv_trend_cross_up[1] and v2_long_ok
v3_vidya_short = vv_trend_cross_down[1] and v2_short_ok

v3_long_votes = (v3_poki_long ? 1 : 0) + (v3_smc_long ? 1 : 0) + (v3_vidya_long ? 1 : 0)
v3_short_votes = (v3_poki_short ? 1 : 0) + (v3_smc_short ? 1 : 0) + (v3_vidya_short ? 1 : 0)

// READY can be armed by CHoCH or an existing module recognizing the direction.
var int v3_ready_dir = 0
var int v3_ready_bar = na
var int v3_last_entry_bar = -999

v3_arm_long = v2_long_ok and (smc_choch_buy or v3_long_votes > 0)
v3_arm_short = v2_short_ok and (smc_choch_sell or v3_short_votes > 0)

if v3_arm_long and not v3_arm_short
    if v3_ready_dir != 1
        v3_ready_dir := 1
        v3_ready_bar := bar_index
        if v3_show_ready
            label.new(bar_index, low, "READY CALL", style=label.style_label_up, color=color.new(color.green, 65), textcolor=color.white, size=size.tiny)
else if v3_arm_short and not v3_arm_long
    if v3_ready_dir != -1
        v3_ready_dir := -1
        v3_ready_bar := bar_index
        if v3_show_ready
            label.new(bar_index, high, "READY PUT", style=label.style_label_down, color=color.new(color.red, 65), textcolor=color.white, size=size.tiny)

if not na(v3_ready_bar) and bar_index - v3_ready_bar > v3_ready_expire
    v3_ready_dir := 0
    v3_ready_bar := na

// Current chart timeframe displacement. On the recommended 5M chart these are 5M candles.
v3_bull_displacement = v2_1_bull_candle and v2_1_body_atr >= v3_structure_body_atr and v2_1_rel_vol >= v3_structure_relvol
v3_bear_displacement = v2_1_bear_candle and v2_1_body_atr >= v3_structure_body_atr and v2_1_rel_vol >= v3_structure_relvol
v3_bull_break = smc_bos_buy or (not v3_require_bos_after_ready and close > high[1] and v3_bull_displacement)
v3_bear_break = smc_bos_sell or (not v3_require_bos_after_ready and close < low[1] and v3_bear_displacement)

v3_structure_call = v3_ready_dir == 1 and v2_long_ok and v3_bull_break and v3_bull_displacement
v3_structure_put = v3_ready_dir == -1 and v2_short_ok and v3_bear_break and v3_bear_displacement

// Existing modules can still produce an entry through one unified vote gate.
v3_consensus_call = v3_long_votes >= v3_min_source_votes
v3_consensus_put = v3_short_votes >= v3_min_source_votes
v3_context_call = v3_allow_single_source_with_5m and v3_long_votes >= 1 and v2_5_bull and not v2_chop
v3_context_put = v3_allow_single_source_with_5m and v3_short_votes >= 1 and v2_5_bear and not v2_chop

// -----------------------------------------------------------------------------
// V4 STRONG REVERSAL ENTRY
// Purpose: catch a genuine 5M reversal before the 15M context has time to flip.
// We intentionally do NOT require the normal MTF permission here. Room, quality,
// exhaustion and a strong direction-changing candle still have to pass.
// -----------------------------------------------------------------------------
v4_is_5m = timeframe.isminutes and timeframe.multiplier == 5
v4_chart_ok = not v4_require_5m or v4_is_5m
v4_ema21 = ta.ema(close, 21)
v4_atr = ta.atr(14)

v4_reversal_bull_disp = close > open and v2_1_body_atr >= v4_reversal_body_atr and v2_1_rel_vol >= v4_reversal_relvol
v4_reversal_bear_disp = close < open and v2_1_body_atr >= v4_reversal_body_atr and v2_1_rel_vol >= v4_reversal_relvol

// Prior bearish/bullish pressure is required so this is a reversal, not another continuation label.
v4_prior_bearish = close[1] < ema_out[1] or close[1] < v4_ema21[1] or v2_5_bear
v4_prior_bullish = close[1] > ema_out[1] or close[1] > v4_ema21[1] or v2_5_bull

v4_reversal_call_trigger = (smc_choch_buy or ta.crossover(close, ema_out) or (close > high[1] and close[1] <= ema_out[1]))
v4_reversal_put_trigger = (smc_choch_sell or ta.crossunder(close, ema_out) or (close < low[1] and close[1] >= ema_out[1]))

v4_reversal_call = v4_chart_ok and v4_enable_reversal and v4_prior_bearish and v4_reversal_call_trigger and v4_reversal_bull_disp and close > ema_out and close > entry_quality_vwap and entry_quality_long_ok and v2_room_call_ok and not v2_bull_exhaustion
v4_reversal_put = v4_chart_ok and v4_enable_reversal and v4_prior_bullish and v4_reversal_put_trigger and v4_reversal_bear_disp and close < ema_out and close < entry_quality_vwap and entry_quality_short_ok and v2_room_put_ok and not v2_bear_exhaustion

// V5 fast trend-failure reversal. This path intentionally does not wait for 15M
// or the full entry-quality gate to flip. It still requires a real 5M structure
// failure, meaningful candle displacement, usable volume, room, and no exhaustion.
v5_prev_bull_regime = v2_5_bull or close[1] > v4_ema21[1] or vv_is_trend_up[1]
v5_prev_bear_regime = v2_5_bear or close[1] < v4_ema21[1] or not vv_is_trend_up[1]
v5_break_high = close > ta.highest(high, v5_fast_rev_break_lookback)[1]
v5_break_low = close < ta.lowest(low, v5_fast_rev_break_lookback)[1]
v5_fast_bull_disp = close > open and v2_1_body_atr >= v5_fast_rev_body_atr and v2_1_rel_vol >= v5_fast_rev_relvol
v5_fast_bear_disp = close < open and v2_1_body_atr >= v5_fast_rev_body_atr and v2_1_rel_vol >= v5_fast_rev_relvol
v5_fast_rev_call = v5_fast_rev_enabled and v4_chart_ok and v4_enable_reversal and v5_prev_bear_regime and v5_fast_bull_disp and close > ema_out and (smc_choch_buy or v5_break_high or ta.crossover(close, v4_ema21)) and v2_room_call_ok and not v2_bull_exhaustion
v5_fast_rev_put = v5_fast_rev_enabled and v4_chart_ok and v4_enable_reversal and v5_prev_bull_regime and v5_fast_bear_disp and close < ema_out and (smc_choch_sell or v5_break_low or ta.crossunder(close, v4_ema21)) and v2_room_put_ok and not v2_bear_exhaustion

// V6 one-shot regime transition logic. A REV is allowed once in a direction,
// then that direction stays locked until the market genuinely resets to the opposite regime.
var int v6_last_rev_dir = 0  // 1 = last accepted REV was CALL, -1 = PUT

v6_bull_regime_reset = v2_5_bull and close > v4_ema21 and vv_is_trend_up
v6_bear_regime_reset = v2_5_bear and close < v4_ema21 and not vv_is_trend_up

if v6_last_rev_dir == -1 and v6_bull_regime_reset
    v6_last_rev_dir := 0
if v6_last_rev_dir == 1 and v6_bear_regime_reset
    v6_last_rev_dir := 0

// Source votes are already calculated by the unified engine. V0 reversals are
// permitted, but only with materially stronger displacement/volume and a fresh break.
v6_call_votes = v3_long_votes
v6_put_votes = v3_short_votes
v6_v0_call_strong = v2_1_body_atr >= v6_v0_body_atr and v2_1_rel_vol >= v6_v0_relvol and (not v6_v0_require_structure_break or v5_break_high or smc_choch_buy)
v6_v0_put_strong = v2_1_body_atr >= v6_v0_body_atr and v2_1_rel_vol >= v6_v0_relvol and (not v6_v0_require_structure_break or v5_break_low or smc_choch_sell)

v6_rev_call_quality_ok = not v6_v0_strict or v6_call_votes > 0 or v6_v0_call_strong
v6_rev_put_quality_ok = not v6_v0_strict or v6_put_votes > 0 or v6_v0_put_strong
v6_rev_call_one_shot_ok = not v6_one_shot_rev or v6_last_rev_dir != 1
v6_rev_put_one_shot_ok = not v6_one_shot_rev or v6_last_rev_dir != -1

v6_reversal_call_raw = (v4_reversal_call or v5_fast_rev_call) and v6_rev_call_quality_ok and v6_rev_call_one_shot_ok
v6_reversal_put_raw = (v4_reversal_put or v5_fast_rev_put) and v6_rev_put_quality_ok and v6_rev_put_one_shot_ok

// V9.4 REV confirmation. Avoid wick-heavy/EMA-only flips while preserving strong
// structural reversals. A no-vote reversal is still allowed on an extreme candle.
f_v94_rev_pass(_isLong) =>
    _range = math.max(high - low, syminfo.mintick)
    _closeLoc = _isLong ? (close - low) / _range : (high - close) / _range
    _structure = _isLong ? (smc_choch_buy or v5_break_high or (close > high[1] and close > v4_ema21)) : (smc_choch_sell or v5_break_low or (close < low[1] and close < v4_ema21))
    _votes = _isLong ? v3_long_votes : v3_short_votes
    _extreme = v2_1_body_atr >= v94_rev_extreme_body_atr and v2_1_rel_vol >= v94_rev_extreme_relvol
    _base = _structure and v2_1_body_atr >= v94_rev_min_body_atr and v2_1_rel_vol >= v94_rev_min_relvol and _closeLoc >= v94_rev_close_location and (not v94_rev_require_vote_or_extreme or _votes > 0 or _extreme)
    _mtf = _isLong ? v2_call_mtf_score : v2_put_mtf_score
    _strict = _base and (_mtf >= 2 or _extreme) and not v2_chop
    v94_mode == "Off (V9.3)" or (v94_mode == "Selective" ? _base : _strict)

v94_rev_call_pass = f_v94_rev_pass(true)
v94_rev_put_pass = f_v94_rev_pass(false)
v6_reversal_call = v6_reversal_call_raw and v94_rev_call_pass
v6_reversal_put = v6_reversal_put_raw and v94_rev_put_pass

// -----------------------------------------------------------------------------
// V4 ONE-SHOT CONTINUATION ENTRY
// Arm only after a real pullback inside an established 5M trend. One entry is
// allowed per pullback; another continuation cannot occur until a fresh pullback.
// -----------------------------------------------------------------------------
var bool v4_cont_call_armed = false
var bool v4_cont_put_armed = false
var int v4_cont_call_arm_bar = na
var int v4_cont_put_arm_bar = na

v4_bull_trend = v2_5_bull and close > v4_ema21 and close > entry_quality_vwap
v4_bear_trend = v2_5_bear and close < v4_ema21 and close < entry_quality_vwap

v4_call_pullback = v4_bull_trend and low <= ema_out + v4_pullback_zone_atr * v4_atr and low >= entry_quality_vwap - v4_pullback_zone_atr * v4_atr
v4_put_pullback = v4_bear_trend and high >= ema_out - v4_pullback_zone_atr * v4_atr and high <= entry_quality_vwap + v4_pullback_zone_atr * v4_atr

if v4_enable_continuation and v4_chart_ok and v4_call_pullback
    v4_cont_call_armed := true
    v4_cont_call_arm_bar := bar_index
    v4_cont_put_armed := false
    v4_cont_put_arm_bar := na

if v4_enable_continuation and v4_chart_ok and v4_put_pullback
    v4_cont_put_armed := true
    v4_cont_put_arm_bar := bar_index
    v4_cont_call_armed := false
    v4_cont_call_arm_bar := na

if v4_cont_call_armed and not na(v4_cont_call_arm_bar) and bar_index - v4_cont_call_arm_bar > v4_cont_reset_bars
    v4_cont_call_armed := false
    v4_cont_call_arm_bar := na
if v4_cont_put_armed and not na(v4_cont_put_arm_bar) and bar_index - v4_cont_put_arm_bar > v4_cont_reset_bars
    v4_cont_put_armed := false
    v4_cont_put_arm_bar := na

v4_cont_bull_disp = close > open and v2_1_body_atr >= v4_cont_body_atr and v2_1_rel_vol >= v4_cont_relvol
v4_cont_bear_disp = close < open and v2_1_body_atr >= v4_cont_body_atr and v2_1_rel_vol >= v4_cont_relvol

v4_cont_call = v4_chart_ok and v4_enable_continuation and v4_cont_call_armed and v4_bull_trend and close > high[1] and close > ema_out and v4_cont_bull_disp and v2_long_ok
v4_cont_put = v4_chart_ok and v4_enable_continuation and v4_cont_put_armed and v4_bear_trend and close < low[1] and close < ema_out and v4_cont_bear_disp and v2_short_ok

// V5 post-loss reset. After an SL, the same direction is blocked until a fresh
// CHoCH/structure event or a real pullback re-arms the thesis.
var int v5_loss_block_dir = 0   // 1 = CALL blocked, -1 = PUT blocked
var int v5_loss_block_bar = na

if v5_post_loss_reset and v5_loss_block_dir == 1 and not na(v5_loss_block_bar) and bar_index > v5_loss_block_bar and (smc_choch_buy or v4_call_pullback or v5_break_high)
    v5_loss_block_dir := 0
    v5_loss_block_bar := na
if v5_post_loss_reset and v5_loss_block_dir == -1 and not na(v5_loss_block_bar) and bar_index > v5_loss_block_bar and (smc_choch_sell or v4_put_pullback or v5_break_low)
    v5_loss_block_dir := 0
    v5_loss_block_bar := na

v5_call_reset_ok = not v5_post_loss_reset or v5_loss_block_dir != 1
v5_put_reset_ok = not v5_post_loss_reset or v5_loss_block_dir != -1

// V6 runner-exit re-arm. After a successful runner closes, the same direction
// cannot immediately re-enter. A fresh pullback or structure reset must occur first.
var int v6_runner_block_dir = 0  // 1 = CALL blocked, -1 = PUT blocked
var int v6_runner_block_bar = na

if v6_rearm_after_runner and v6_runner_block_dir == 1 and not na(v6_runner_block_bar) and bar_index > v6_runner_block_bar and (v4_call_pullback or smc_choch_buy)
    v6_runner_block_dir := 0
    v6_runner_block_bar := na
if v6_rearm_after_runner and v6_runner_block_dir == -1 and not na(v6_runner_block_bar) and bar_index > v6_runner_block_bar and (v4_put_pullback or smc_choch_sell)
    v6_runner_block_dir := 0
    v6_runner_block_bar := na

v6_call_rearm_ok = not v6_rearm_after_runner or v6_runner_block_dir != 1
v6_put_rearm_ok = not v6_rearm_after_runner or v6_runner_block_dir != -1

// V9 optional CONT quality filter. The default is OFF, preserving V6/V8.1 behavior.
v9_hour = hour(time, v8_time_zone)
v9_is_midday = v9_hour >= 11 and v9_hour < 13
v9_cont_call_pass = not v9_enable_cont_optimization or ((not v9_is_midday or (v2_call_mtf_score >= v9_cont_midday_min_mtf and v3_long_votes >= v9_cont_midday_min_votes)) and (not v2_5_bear or v2_call_mtf_score >= v9_counter_cont_min_mtf))
v9_cont_put_pass = not v9_enable_cont_optimization or ((not v9_is_midday or (v2_put_mtf_score >= v9_cont_midday_min_mtf and v3_short_votes >= v9_cont_midday_min_votes)) and (not v2_5_bull or v2_put_mtf_score >= v9_counter_cont_min_mtf))

// Adaptive CONT does not touch REV or STRUCTURE. It raises the bar only when
// the current continuation is midday, counter-trend, low-volume, or choppy.
v91_call_quality_score = v2_call_mtf_score + math.min(v3_long_votes, 2) + (v2_1_rel_vol >= 1.0 ? 1 : 0) + (not v2_chop ? 1 : 0)
v91_put_quality_score = v2_put_mtf_score + math.min(v3_short_votes, 2) + (v2_1_rel_vol >= 1.0 ? 1 : 0) + (not v2_chop ? 1 : 0)
v91_call_required_score = v91_adaptive_base_score + (v9_is_midday ? v91_adaptive_midday_extra : 0) + (v2_5_bear ? v91_adaptive_counter_extra : 0)
v91_put_required_score = v91_adaptive_base_score + (v9_is_midday ? v91_adaptive_midday_extra : 0) + (v2_5_bull ? v91_adaptive_counter_extra : 0)
v91_adaptive_call_pass = v93_cont_mode == "Baseline" or (v91_call_quality_score >= v91_call_required_score and v2_1_rel_vol >= v91_adaptive_relvol_floor)
v91_adaptive_put_pass = v93_cont_mode == "Baseline" or (v91_put_quality_score >= v91_put_required_score and v2_1_rel_vol >= v91_adaptive_relvol_floor)

// V9.4 CONT confirmation. 5M trend remains mandatory from V4; this layer
// requires better MTF participation/volume and raises quality during midday.
f_v94_cont_pass(_isLong) =>
    _mtf = _isLong ? v2_call_mtf_score : v2_put_mtf_score
    _votes = _isLong ? v3_long_votes : v3_short_votes
    _htf = _isLong ? v2_15_bull : v2_15_bear
    _needed = math.min(v94_cont_min_mtf + (v9_is_midday ? v94_cont_midday_extra_score : 0), 4)
    _base = _mtf >= _needed and v2_1_rel_vol >= v94_cont_min_relvol and (not v94_cont_require_15m_or_votes or _htf or _votes >= 2) and not v2_chop
    _strict = _base and _htf and _votes >= 1 and v2_1_rel_vol >= math.max(v94_cont_min_relvol, 1.0)
    v94_mode == "Off (V9.3)" or (v94_mode == "Selective" ? _base : _strict)

v94_cont_call_pass = f_v94_cont_pass(true)
v94_cont_put_pass = f_v94_cont_pass(false)
v9_cont_call_raw = v4_cont_call and v9_cont_call_pass and v91_adaptive_call_pass
v9_cont_put_raw = v4_cont_put and v9_cont_put_pass and v91_adaptive_put_pass
v9_cont_call = v9_cont_call_raw and v94_cont_call_pass
v9_cont_put = v9_cont_put_raw and v94_cont_put_pass

// Count filtered candidate bars. This is diagnostic only and never feeds trading state.
var int v94_blocked_rev = 0
var int v94_blocked_cont = 0
if v94_mode != "Off (V9.3)"
    if (v6_reversal_call_raw and not v94_rev_call_pass) or (v6_reversal_put_raw and not v94_rev_put_pass)
        v94_blocked_rev += 1
    if (v9_cont_call_raw and not v94_cont_call_pass) or (v9_cont_put_raw and not v94_cont_put_pass)
        v94_blocked_cont += 1

// Unified candidate set. V6 priority is explicit:
// REV > STRUCTURE > CONT > CONSENSUS > CONTEXT.
v3_call_candidate = v3_unified_enabled and v5_call_reset_ok and v6_call_rearm_ok and (v6_reversal_call or (v2_long_ok and (v3_structure_call or v9_cont_call or v3_consensus_call or v3_context_call)))
v3_put_candidate = v3_unified_enabled and v5_put_reset_ok and v6_put_rearm_ok and (v6_reversal_put or (v2_short_ok and (v3_structure_put or v9_cont_put or v3_consensus_put or v3_context_put)))
v3_can_enter = bar_index - v3_last_entry_bar >= v3_entry_cooldown

v3_entry_call = v3_can_enter and v3_call_candidate and not v3_put_candidate
v3_entry_put = v3_can_enter and v3_put_candidate and not v3_call_candidate

// Tie-breaker uses the same explicit priority on both sides.
if v3_can_enter and v3_call_candidate and v3_put_candidate
    v3_entry_call := v6_reversal_call and not v6_reversal_put ? true : v6_reversal_put and not v6_reversal_call ? false : v3_structure_call and not v3_structure_put ? true : v3_structure_put and not v3_structure_call ? false : v9_cont_call and not v9_cont_put ? true : v9_cont_put and not v9_cont_call ? false : v3_consensus_call and not v3_consensus_put ? true : v3_consensus_put and not v3_consensus_call ? false : v3_long_votes > v3_short_votes ? true : v3_short_votes > v3_long_votes ? false : v2_5_bull and not v2_5_bear
    v3_entry_put := not v3_entry_call

if v3_entry_call or v3_entry_put
    v3_last_entry_bar := bar_index
    v3_ready_dir := 0
    v3_ready_bar := na
    if v3_entry_call and v6_reversal_call
        v6_last_rev_dir := 1
    if v3_entry_put and v6_reversal_put
        v6_last_rev_dir := -1
    if v3_entry_call and v9_cont_call
        v4_cont_call_armed := false
        v4_cont_call_arm_bar := na
    if v3_entry_put and v9_cont_put
        v4_cont_put_armed := false
        v4_cont_put_arm_bar := na

v3_call_reason = v6_reversal_call ? "REV CALL" : v3_structure_call ? "STRUCTURE CALL" : v9_cont_call ? "CONT CALL" : v3_consensus_call ? "CONSENSUS CALL" : "CONTEXT CALL"
v3_put_reason = v6_reversal_put ? "REV PUT" : v3_structure_put ? "STRUCTURE PUT" : v9_cont_put ? "CONT PUT" : v3_consensus_put ? "CONSENSUS PUT" : "CONTEXT PUT"

alertcondition(v3_entry_call, "ULTI-7 V6 CALL", "ULTI-7 V6 CALL on {{ticker}} {{interval}} at {{close}}")
alertcondition(v3_entry_put, "ULTI-7 V6 PUT", "ULTI-7 V6 PUT on {{ticker}} {{interval}} at {{close}}")

// =============================================================================
// V2 DEBUG HUD
// =============================================================================
var table v2_debug = table.new(position.top_left, 2, 12, bgcolor=color.new(color.black, 25), border_width=1)
f_render_v2_debug() =>
    if barstate.islast and v2_show_debug
        table.cell(v2_debug, 0, 0, "ULTI-7 V2", text_color=color.white)
        table.cell(v2_debug, 1, 0, "EXECUTION", text_color=color.white)
        table.cell(v2_debug, 0, 1, "5M", text_color=color.gray)
        table.cell(v2_debug, 1, 1, v2_5_bull ? "BULL" : v2_5_bear ? "BEAR" : "NEUTRAL", text_color=v2_5_bull ? color.lime : v2_5_bear ? color.red : color.yellow)
        table.cell(v2_debug, 0, 2, "15M", text_color=color.gray)
        table.cell(v2_debug, 1, 2, v2_15_bull ? "BULL" : v2_15_bear ? "BEAR" : "NEUTRAL", text_color=v2_15_bull ? color.lime : v2_15_bear ? color.red : color.yellow)
        table.cell(v2_debug, 0, 3, "CALL MTF", text_color=color.gray)
        table.cell(v2_debug, 1, 3, str.tostring(v2_call_mtf_score) + "/4", text_color=color.lime)
        table.cell(v2_debug, 0, 4, "PUT MTF", text_color=color.gray)
        table.cell(v2_debug, 1, 4, str.tostring(v2_put_mtf_score) + "/4", text_color=color.red)
        table.cell(v2_debug, 0, 5, "Room C / P", text_color=color.gray)
        table.cell(v2_debug, 1, 5, str.tostring(v2_call_room, "#.0") + " / " + str.tostring(v2_put_room, "#.0"), text_color=color.white)
        table.cell(v2_debug, 0, 6, "Extension", text_color=color.gray)
        table.cell(v2_debug, 1, 6, str.tostring(v2_extension, "#.00") + " ATR", text_color=v2_bull_exhaustion or v2_bear_exhaustion ? color.red : color.white)
        table.cell(v2_debug, 0, 7, "Regime", text_color=color.gray)
        table.cell(v2_debug, 1, 7, v2_chop ? "CHOP" : "TREND", text_color=v2_chop ? color.orange : color.lime)
        table.cell(v2_debug, 0, 8, "Gate C / P", text_color=color.gray)
        table.cell(v2_debug, 1, 8, (v2_long_ok ? "OK" : "BLOCK") + " / " + (v2_short_ok ? "OK" : "BLOCK"), text_color=color.white)
        table.cell(v2_debug, 0, 9, "Votes C / P", text_color=color.gray)
        table.cell(v2_debug, 1, 9, str.tostring(v3_long_votes) + " / " + str.tostring(v3_short_votes), text_color=color.white)
        table.cell(v2_debug, 0, 10, "READY", text_color=color.gray)
        table.cell(v2_debug, 1, 10, v3_ready_dir == 1 ? "CALL" : v3_ready_dir == -1 ? "PUT" : "NONE", text_color=v3_ready_dir == 1 ? color.lime : v3_ready_dir == -1 ? color.red : color.white)
        table.cell(v2_debug, 0, 11, "V6 Entry", text_color=color.gray)
        table.cell(v2_debug, 1, 11, v3_entry_call ? v3_call_reason : v3_entry_put ? v3_put_reason : "WAIT", text_color=v3_entry_call ? color.lime : v3_entry_put ? color.red : color.white)

f_render_v2_debug()

// ============================================================================
// VISUAL TRADE LABELS ONLY
// ============================================================================
// This mirrors the ScalpRot TP1/TP2/final-TP/SL labels for the underlying chart.
// It does not place orders, create alerts, or change the existing module logic.
visual_trade_labels_enabled = input.bool(true, "Show TP/SL Trade Labels", group="Trade Labels")
visual_tp1_atr = input.float(0.75, "TP1 ATR Multiplier", minval=0.0, step=0.25, group="Trade Labels")
visual_tp2_atr = input.float(1.50, "TP2 ATR Multiplier", minval=0.0, step=0.25, group="Trade Labels")
visual_tp3_atr = input.float(2.25, "Final TP ATR Multiplier", minval=0.0, step=0.25, group="Trade Labels")
visual_sl_atr = input.float(0.50, "Stop Loss ATR Multiplier", minval=0.0, step=0.25, group="Trade Labels")
visual_breakeven_atr = input.float(0.05, "TP1 Breakeven Offset ATR", minval=0.0, step=0.05, group="Trade Labels")

visual_atr_1m = ta.atr(14)
[visual_risk_atr_5m, visual_5m_low, visual_5m_high] = request.security(syminfo.tickerid, "5", [ta.atr(14)[1], ta.lowest(low, 6)[1], ta.highest(high, 6)[1]], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)
visual_atr = nz(visual_risk_atr_5m, visual_atr_1m)
visual_long_entry = v3_unified_enabled ? v3_entry_call : ((poki_longCondition and v2_long_ok) or (smc_buy_condition and v2_long_ok) or (vv_trend_cross_up[1] and v2_long_ok))
visual_short_entry = v3_unified_enabled ? v3_entry_put : ((poki_shortCondition and v2_short_ok) or (smc_sell_condition and v2_short_ok) or (vv_trend_cross_down[1] and v2_short_ok))

// ============================================================================
// V9 PERFORMANCE ANALYTICS
// Counts the same trades managed by the visual trade manager below.
// A trade that reaches TP2 also counts as having reached TP1.
// "SL < TP1" means the stop was reached before TP1.
// ============================================================================
var int v7_total_entries = 0
var int v7_closed_trades = 0
var int v7_call_entries = 0
var int v7_put_entries = 0
var int v7_tp1_hits = 0
var int v7_tp2_hits = 0
var int v7_runner_exits = 0
var int v7_sl_before_tp1 = 0
var int v7_tp1_only_closes = 0
var int v7_fixed_target_closes = 0
var int v7_with_trend_entries = 0
var int v7_counter_trend_entries = 0
var int v7_neutral_entries = 0
var int v7_with_trend_tp1 = 0
var int v7_counter_trend_tp1 = 0

var int v7_rev_entries = 0
var int v7_rev_tp1 = 0
var int v7_structure_entries = 0
var int v7_structure_tp1 = 0
var int v7_cont_entries = 0
var int v7_cont_tp1 = 0
var int v7_consensus_entries = 0
var int v7_consensus_tp1 = 0
var int v7_context_entries = 0
var int v7_context_tp1 = 0

// V8 per-setup outcome counts.
var int v8_rev_tp2 = 0
var int v8_rev_sl = 0
var int v8_structure_tp2 = 0
var int v8_structure_sl = 0
var int v8_cont_tp2 = 0
var int v8_cont_sl = 0
var int v8_consensus_tp2 = 0
var int v8_consensus_sl = 0
var int v8_context_tp2 = 0
var int v8_context_sl = 0

// V8 direction-by-setup counts (entries / TP1).
var int v8_rev_call_e = 0
var int v8_rev_call_t1 = 0
var int v8_rev_put_e = 0
var int v8_rev_put_t1 = 0
var int v8_struct_call_e = 0
var int v8_struct_call_t1 = 0
var int v8_struct_put_e = 0
var int v8_struct_put_t1 = 0
var int v8_cont_call_e = 0
var int v8_cont_call_t1 = 0
var int v8_cont_put_e = 0
var int v8_cont_put_t1 = 0
var int v8_cons_call_e = 0
var int v8_cons_call_t1 = 0
var int v8_cons_put_e = 0
var int v8_cons_put_t1 = 0
var int v8_context_call_e = 0
var int v8_context_call_t1 = 0
var int v8_context_put_e = 0
var int v8_context_put_t1 = 0

// V8 per-setup excursion sums, finalized only when a trade closes.
var float v8_rev_mfe_sum = 0.0
var float v8_rev_mae_sum = 0.0
var int v8_rev_closed = 0
var float v8_struct_mfe_sum = 0.0
var float v8_struct_mae_sum = 0.0
var int v8_struct_closed = 0
var float v8_cont_mfe_sum = 0.0
var float v8_cont_mae_sum = 0.0
var int v8_cont_closed = 0
var float v8_cons_mfe_sum = 0.0
var float v8_cons_mae_sum = 0.0
var int v8_cons_closed = 0
var float v8_context_mfe_sum = 0.0
var float v8_context_mae_sum = 0.0
var int v8_context_closed = 0

// V8 runner analytics. All values are normalized by entry ATR.
var float v8_runner_tp2_price = na
var float v8_runner_best_price = na
var float v8_runner_gain_sum = 0.0
var float v8_runner_max_gain_sum = 0.0
var float v8_runner_giveback_sum = 0.0
var float v8_runner_best_gain = 0.0
var int v8_runner_added_half_atr = 0
var int v8_runner_giveback_half_atr = 0
var int v81_runner_samples = 0

// V9.2 audit-safe runner analytics and exactly-once expectancy accounting.
var int v91_runner_violations = 0
var float v91_runner_direct_giveback_sum = 0.0
var float v91_expectancy_sum_r = 0.0
var int v91_expectancy_count = 0
var float v91_rev_expectancy_sum_r = 0.0
var int v91_rev_expectancy_count = 0
var float v91_struct_expectancy_sum_r = 0.0
var int v91_struct_expectancy_count = 0
var float v91_cont_expectancy_sum_r = 0.0
var int v91_cont_expectancy_count = 0
var float v91_cons_expectancy_sum_r = 0.0
var int v91_cons_expectancy_count = 0
var float v91_context_expectancy_sum_r = 0.0
var int v91_context_expectancy_count = 0
var float v91_initial_sl_price = na
var float v91_initial_risk = na

// V9.5 diagnostic collections. Indices: time 0..3 = 08:30-09:30, 09:30-11:00,
// 11:00-13:00, 13:00+; alignment 0..2 = with-trend, counter-trend, neutral.
var array<float> v95_rev_tod_sum = array.new_float(4, 0.0)
var array<int> v95_rev_tod_n = array.new_int(4, 0)
var array<float> v95_cont_tod_sum = array.new_float(4, 0.0)
var array<int> v95_cont_tod_n = array.new_int(4, 0)
var array<float> v95_rev_align_sum = array.new_float(3, 0.0)
var array<int> v95_rev_align_n = array.new_int(3, 0)
var array<float> v95_cont_align_sum = array.new_float(3, 0.0)
var array<int> v95_cont_align_n = array.new_int(3, 0)

// V8 time-of-day buckets: 08:30-09:30, 09:30-11:00, 11:00-13:00, 13:00+.
var int v8_tod1_e = 0
var int v8_tod1_t1 = 0
var int v8_tod2_e = 0
var int v8_tod2_t1 = 0
var int v8_tod3_e = 0
var int v8_tod3_t1 = 0
var int v8_tod4_e = 0
var int v8_tod4_t1 = 0
var int v8_trade_tod = 0

// V9 CONT-only time buckets, used to validate the weaker midday continuation window.
var int v9_cont_tod1_e = 0
var int v9_cont_tod1_t1 = 0
var int v9_cont_tod2_e = 0
var int v9_cont_tod2_t1 = 0
var int v9_cont_tod3_e = 0
var int v9_cont_tod3_t1 = 0
var int v9_cont_tod4_e = 0
var int v9_cont_tod4_t1 = 0

var float v7_sum_mfe_atr = 0.0
var float v7_sum_mae_atr = 0.0
var float v7_trade_mfe = 0.0
var float v7_trade_mae = 0.0
var float v7_trade_atr = na
var string v7_trade_reason = ""
var int v7_trade_dir = 0
var int v7_trade_alignment = 0
var bool v7_trade_counted_tp1 = false
var bool v7_trade_counted_tp2 = false

f_v95_reset(_sums, _counts) =>
    for _i = 0 to array.size(_sums) - 1
        array.set(_sums, _i, 0.0)
        array.set(_counts, _i, 0)

v7_new_day = timeframe.change("D")
if v7_reset_daily and v7_new_day
    v7_total_entries := 0
    v7_closed_trades := 0
    v7_call_entries := 0
    v7_put_entries := 0
    v7_tp1_hits := 0
    v7_tp2_hits := 0
    v7_runner_exits := 0
    v7_sl_before_tp1 := 0
    v7_tp1_only_closes := 0
    v7_fixed_target_closes := 0
    v7_with_trend_entries := 0
    v7_counter_trend_entries := 0
    v7_neutral_entries := 0
    v7_with_trend_tp1 := 0
    v7_counter_trend_tp1 := 0
    v7_rev_entries := 0
    v7_rev_tp1 := 0
    v7_structure_entries := 0
    v7_structure_tp1 := 0
    v7_cont_entries := 0
    v7_cont_tp1 := 0
    v7_consensus_entries := 0
    v7_consensus_tp1 := 0
    v7_context_entries := 0
    v7_context_tp1 := 0
    v7_sum_mfe_atr := 0.0
    v7_sum_mae_atr := 0.0
    v8_rev_tp2 := 0
    v8_rev_sl := 0
    v8_structure_tp2 := 0
    v8_structure_sl := 0
    v8_cont_tp2 := 0
    v8_cont_sl := 0
    v8_consensus_tp2 := 0
    v8_consensus_sl := 0
    v8_context_tp2 := 0
    v8_context_sl := 0
    v8_rev_call_e := 0
    v8_rev_call_t1 := 0
    v8_rev_put_e := 0
    v8_rev_put_t1 := 0
    v8_struct_call_e := 0
    v8_struct_call_t1 := 0
    v8_struct_put_e := 0
    v8_struct_put_t1 := 0
    v8_cont_call_e := 0
    v8_cont_call_t1 := 0
    v8_cont_put_e := 0
    v8_cont_put_t1 := 0
    v8_cons_call_e := 0
    v8_cons_call_t1 := 0
    v8_cons_put_e := 0
    v8_cons_put_t1 := 0
    v8_context_call_e := 0
    v8_context_call_t1 := 0
    v8_context_put_e := 0
    v8_context_put_t1 := 0
    v8_rev_mfe_sum := 0.0
    v8_rev_mae_sum := 0.0
    v8_rev_closed := 0
    v8_struct_mfe_sum := 0.0
    v8_struct_mae_sum := 0.0
    v8_struct_closed := 0
    v8_cont_mfe_sum := 0.0
    v8_cont_mae_sum := 0.0
    v8_cont_closed := 0
    v8_cons_mfe_sum := 0.0
    v8_cons_mae_sum := 0.0
    v8_cons_closed := 0
    v8_context_mfe_sum := 0.0
    v8_context_mae_sum := 0.0
    v8_context_closed := 0
    v8_runner_gain_sum := 0.0
    v8_runner_max_gain_sum := 0.0
    v8_runner_giveback_sum := 0.0
    v8_runner_best_gain := 0.0
    v8_runner_added_half_atr := 0
    v8_runner_giveback_half_atr := 0
    v81_runner_samples := 0
    v91_runner_violations := 0
    v91_runner_direct_giveback_sum := 0.0
    v91_expectancy_sum_r := 0.0
    v91_expectancy_count := 0
    v91_rev_expectancy_sum_r := 0.0
    v91_rev_expectancy_count := 0
    v91_struct_expectancy_sum_r := 0.0
    v91_struct_expectancy_count := 0
    v91_cont_expectancy_sum_r := 0.0
    v91_cont_expectancy_count := 0
    v91_cons_expectancy_sum_r := 0.0
    v91_cons_expectancy_count := 0
    v91_context_expectancy_sum_r := 0.0
    v91_context_expectancy_count := 0
    v8_tod1_e := 0
    v8_tod1_t1 := 0
    v8_tod2_e := 0
    v8_tod2_t1 := 0
    v8_tod3_e := 0
    v8_tod3_t1 := 0
    v8_tod4_e := 0
    v8_tod4_t1 := 0
    v9_cont_tod1_e := 0
    v9_cont_tod1_t1 := 0
    v9_cont_tod2_e := 0
    v9_cont_tod2_t1 := 0
    v9_cont_tod3_e := 0
    v9_cont_tod3_t1 := 0
    v9_cont_tod4_e := 0
    v9_cont_tod4_t1 := 0
    f_v95_reset(v95_rev_tod_sum, v95_rev_tod_n)
    f_v95_reset(v95_cont_tod_sum, v95_cont_tod_n)
    f_v95_reset(v95_rev_align_sum, v95_rev_align_n)
    f_v95_reset(v95_cont_align_sum, v95_cont_align_n)

v7_pct(num, den) => den > 0 ? 100.0 * num / den : 0.0
v7_setup_is(reason, prefix) => str.contains(reason, prefix)
v8_safe_avg(sum, count) => count > 0 ? sum / count : 0.0
v8_tod_bucket() =>
    v8_minutes = hour(time, v8_time_zone) * 60 + minute(time, v8_time_zone)
    v8_minutes >= 510 and v8_minutes < 570 ? 1 : v8_minutes >= 570 and v8_minutes < 660 ? 2 : v8_minutes >= 660 and v8_minutes < 780 ? 3 : 4

f_v95_add(_sums, _counts, _idx, _r) =>
    if _idx >= 0 and _idx < array.size(_sums)
        array.set(_sums, _idx, array.get(_sums, _idx) + _r)
        array.set(_counts, _idx, array.get(_counts, _idx) + 1)

f_v95_record(_reason, _tod, _align, _r) =>
    _todIdx = math.max(0, math.min(3, _tod - 1))
    _alignIdx = _align == 1 ? 0 : _align == -1 ? 1 : 2
    if v7_setup_is(_reason, "REV")
        f_v95_add(v95_rev_tod_sum, v95_rev_tod_n, _todIdx, _r)
        f_v95_add(v95_rev_align_sum, v95_rev_align_n, _alignIdx, _r)
    else if v7_setup_is(_reason, "CONT ")
        f_v95_add(v95_cont_tod_sum, v95_cont_tod_n, _todIdx, _r)
        f_v95_add(v95_cont_align_sum, v95_cont_align_n, _alignIdx, _r)

f_v95_avg(_sums, _counts, _idx) =>
    _n = array.get(_counts, _idx)
    _n > 0 ? array.get(_sums, _idx) / _n : 0.0

var string visual_position = "FLAT"
var float visual_entry_price = na
var float visual_tp1_price = na
var float visual_tp2_price = na
var float visual_tp3_price = na
var float visual_sl_price = na
var bool visual_hit_tp1 = false
var bool visual_hit_tp2 = false
var bool visual_runner_active = false
var int visual_entry_bar = na
varip int v96_trade_id = 0
varip bool v96_entry_sent = false
varip bool v96_tp1_sent = false
varip bool v96_tp2_sent = false
varip bool v96_exit_sent = false
var int v95_parity_entry_side = 0
var int v95_parity_entry_setup = 0
var int v95_parity_exit_code = 0
float v95_parity_runner_candidate = na
float v95_parity_stop_pre_exit = na
bool v95_parity_stop_hit = false
v95_parity_entry_side := 0
v95_parity_entry_setup := 0
v95_parity_exit_code := 0

f_v95_setup_code(_reason) =>
    str.contains(_reason, "REV") ? 1 : str.contains(_reason, "STRUCTURE") ? 2 : str.contains(_reason, "CONSENSUS") ? 4 : str.contains(_reason, "CONTEXT") ? 5 : str.contains(_reason, "CONT") ? 3 : 5

f_v96_alert_message(_event, _direction, _reason, _eventPrice, _entry, _tp1, _tp2, _sl) =>
    _setup = _event == "ENTRY" ? f_v95_setup_code(_reason) : 0
    _shadow_json = "{\"event\":\"" + _event + "\",\"symbol\":\"" + syminfo.ticker + "\",\"timeframe\":\"" + timeframe.period + "\",\"bar_time_ms\":" + str.tostring(time) + ",\"trade_id\":" + str.tostring(v96_trade_id) + ",\"direction\":\"" + _direction + "\",\"setup\":" + str.tostring(_setup) + ",\"event_price\":" + str.tostring(_eventPrice, format.mintick) + ",\"entry\":" + str.tostring(_entry, format.mintick) + ",\"tp1\":" + str.tostring(_tp1, format.mintick) + ",\"tp2\":" + str.tostring(_tp2, format.mintick) + ",\"stop\":" + str.tostring(_sl, format.mintick) + "}"
    _risk_ok = not na(v91_initial_risk) and v91_initial_risk > 0
    _leg_r = _risk_ok ? ((_eventPrice - _entry) * v7_trade_dir) / v91_initial_risk : na
    _tp1_r = _risk_ok ? ((_tp1 - _entry) * v7_trade_dir) / v91_initial_risk : na
    _tp2_r = _risk_ok ? ((_tp2 - _entry) * v7_trade_dir) / v91_initial_risk : na
    _total_pct = math.max(v93_tp1_scale_pct + v93_tp2_scale_pct + v93_runner_scale_pct, 0.0001)
    _w1 = v93_tp1_scale_pct / _total_pct
    _w2 = v93_tp2_scale_pct / _total_pct
    _w3 = v93_runner_scale_pct / _total_pct
    _trade_r = not _risk_ok ? na : not visual_hit_tp1 ? _leg_r : not visual_hit_tp2 ? (_w1 * _tp1_r + (_w2 + _w3) * _leg_r) : (_w1 * _tp1_r + _w2 * _tp2_r + _w3 * _leg_r)
    _trade_r_txt = na(_trade_r) ? "NA" : str.tostring(_trade_r, "#.00") + "R"
    _runner_r_txt = na(_leg_r) ? "NA" : str.tostring(_leg_r, "#.00") + "R"
    _prefix = syminfo.ticker + " " + _direction + " | "
    v96_shadow_json ? _shadow_json : _event == "ENTRY" ? _prefix + "ENTRY @ " + str.tostring(_entry, format.mintick) + " | TP1 " + str.tostring(_tp1, format.mintick) + " | TP2 " + str.tostring(_tp2, format.mintick) + " | SL " + str.tostring(_sl, format.mintick) :
    _event == "TP1" ? _prefix + "TP1 HIT @ " + str.tostring(_eventPrice, format.mintick) + " | 75% TAKEN" :
    (_event == "TP2_RUNNER_START" or _event == "TP2") ? _prefix + "TP2 HIT @ " + str.tostring(_eventPrice, format.mintick) + " | REMAINING 25% CLOSED" :
     _event == "RUNNER_EXIT" ? _prefix + "RUNNER EXIT @ " + str.tostring(_eventPrice, format.mintick) + " | Runner " + _runner_r_txt + " | TOTAL " + _trade_r_txt :
     _event == "EARLY_FAIL_EXIT" ? _prefix + "LATE4 EXIT @ " + str.tostring(_eventPrice, format.mintick) + " | TOTAL " + _trade_r_txt :
     _event == "SL" ? _prefix + "SL HIT @ " + str.tostring(_eventPrice, format.mintick) + " | TOTAL " + _trade_r_txt :
     _event == "FINAL_TP" ? _prefix + "FINAL TP @ " + str.tostring(_eventPrice, format.mintick) + " | TOTAL " + _trade_r_txt :
     _prefix + _event + " @ " + str.tostring(_eventPrice, format.mintick)

f_v96_fire(_event, _direction, _reason, _eventPrice, _entry, _tp1, _tp2, _sl) =>
    if v96_unified_alerts
        if v96_shadow_json
            alert(f_v96_alert_message(_event, _direction, _reason, _eventPrice, _entry, _tp1, _tp2, _sl), alert.freq_all)
        else
            alert(f_v96_alert_message(_event, _direction, _reason, _eventPrice, _entry, _tp1, _tp2, _sl), alert.freq_once_per_bar)

v91_leg_r(price) =>
    not na(v91_initial_risk) and v91_initial_risk > 0 ? ((price - visual_entry_price) * v7_trade_dir) / v91_initial_risk : 0.0

v91_realized_r(exit_price) =>
    v91_total_pct = math.max(v93_tp1_scale_pct + v93_tp2_scale_pct + v93_runner_scale_pct, 0.0001)
    v91_w1 = v93_tp1_scale_pct / v91_total_pct
    v91_w2 = v93_tp2_scale_pct / v91_total_pct
    v91_w3 = v93_runner_scale_pct / v91_total_pct
    v91_exit_r = v91_leg_r(exit_price)
    v91_tp1_r = v91_leg_r(visual_tp1_price)
    v91_tp2_r = v91_leg_r(visual_tp2_price)
    not visual_hit_tp1 ? v91_exit_r : not visual_hit_tp2 ? v91_w1 * v91_tp1_r + (v91_w2 + v91_w3) * v91_exit_r : v91_w1 * v91_tp1_r + v91_w2 * v91_tp2_r + v91_w3 * v91_exit_r

if visual_trade_labels_enabled and visual_position == "FLAT" and visual_long_entry
    visual_position := "LONG"
    v96_trade_id += 1
    v96_entry_sent := false
    v96_tp1_sent := false
    v96_tp2_sent := false
    v96_exit_sent := false
    v95_parity_entry_side := 1
    v95_parity_entry_setup := v3_unified_enabled ? f_v95_setup_code(v3_call_reason) : 6
    visual_entry_price := close
    visual_tp1_price := close + visual_tp1_atr * visual_atr
    visual_tp2_price := close + visual_tp2_atr * visual_atr
    visual_tp3_price := close + visual_tp3_atr * visual_atr
    visual_sl_price := math.min(close - visual_sl_atr * visual_atr, visual_5m_low - 0.10 * visual_atr)
    v91_initial_sl_price := visual_sl_price
    v91_initial_risk := math.abs(close - visual_sl_price)
    visual_hit_tp1 := false
    visual_hit_tp2 := false
    visual_runner_active := false
    visual_entry_bar := bar_index
    v7_trade_dir := 1
    v7_total_entries += 1
    v7_call_entries += 1
    v7_trade_reason := v3_unified_enabled ? v3_call_reason : "RAW CALL"
    if not v96_entry_sent
        f_v96_fire("ENTRY", "CALL", v7_trade_reason, visual_entry_price, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
        v96_entry_sent := true
    v8_trade_tod := v8_tod_bucket()
    if v8_trade_tod == 1
        v8_tod1_e += 1
    else if v8_trade_tod == 2
        v8_tod2_e += 1
    else if v8_trade_tod == 3
        v8_tod3_e += 1
    else
        v8_tod4_e += 1
    if v7_setup_is(v7_trade_reason, "REV")
        v8_rev_call_e += 1
    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
        v8_struct_call_e += 1
    else if v7_setup_is(v7_trade_reason, "CONT ")
        v8_cont_call_e += 1
    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
        v8_cons_call_e += 1
    else
        v8_context_call_e += 1
    v8_runner_tp2_price := na
    v8_runner_best_price := na
    v7_trade_dir := 1
    v7_trade_atr := visual_atr
    v7_trade_mfe := 0.0
    v7_trade_mae := 0.0
    v7_trade_counted_tp1 := false
    v7_trade_counted_tp2 := false
    v7_trade_alignment := v2_5_bull ? 1 : v2_5_bear ? -1 : 0
    if v7_trade_alignment == 1
        v7_with_trend_entries += 1
    else if v7_trade_alignment == -1
        v7_counter_trend_entries += 1
    else
        v7_neutral_entries += 1
    if v7_setup_is(v7_trade_reason, "REV")
        v7_rev_entries += 1
    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
        v7_structure_entries += 1
    else if v7_setup_is(v7_trade_reason, "CONT ")
        v7_cont_entries += 1
    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
        v7_consensus_entries += 1
    else
        v7_context_entries += 1
    if v7_setup_is(v7_trade_reason, "CONT ")
        if v8_trade_tod == 1
            v9_cont_tod1_e += 1
        else if v8_trade_tod == 2
            v9_cont_tod2_e += 1
        else if v8_trade_tod == 3
            v9_cont_tod3_e += 1
        else
            v9_cont_tod4_e += 1
    if v3_unified_enabled
        label.new(bar_index, low, v3_call_reason + "\nV" + str.tostring(v3_long_votes) + " MTF" + str.tostring(v2_call_mtf_score) + "/4", style=label.style_label_up, color=color_signal_buy, textcolor=color.white, size=size.small)

if visual_trade_labels_enabled and visual_position == "FLAT" and visual_short_entry
    visual_position := "SHORT"
    v96_trade_id += 1
    v96_entry_sent := false
    v96_tp1_sent := false
    v96_tp2_sent := false
    v96_exit_sent := false
    v95_parity_entry_side := -1
    v95_parity_entry_setup := v3_unified_enabled ? f_v95_setup_code(v3_put_reason) : 6
    visual_entry_price := close
    visual_tp1_price := close - visual_tp1_atr * visual_atr
    visual_tp2_price := close - visual_tp2_atr * visual_atr
    visual_tp3_price := close - visual_tp3_atr * visual_atr
    visual_sl_price := math.max(close + visual_sl_atr * visual_atr, visual_5m_high + 0.10 * visual_atr)
    v91_initial_sl_price := visual_sl_price
    v91_initial_risk := math.abs(close - visual_sl_price)
    visual_hit_tp1 := false
    visual_hit_tp2 := false
    visual_runner_active := false
    visual_entry_bar := bar_index
    v7_trade_dir := -1
    v7_total_entries += 1
    v7_put_entries += 1
    v7_trade_reason := v3_unified_enabled ? v3_put_reason : "RAW PUT"
    if not v96_entry_sent
        f_v96_fire("ENTRY", "PUT", v7_trade_reason, visual_entry_price, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
        v96_entry_sent := true
    v8_trade_tod := v8_tod_bucket()
    if v8_trade_tod == 1
        v8_tod1_e += 1
    else if v8_trade_tod == 2
        v8_tod2_e += 1
    else if v8_trade_tod == 3
        v8_tod3_e += 1
    else
        v8_tod4_e += 1
    if v7_setup_is(v7_trade_reason, "REV")
        v8_rev_put_e += 1
    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
        v8_struct_put_e += 1
    else if v7_setup_is(v7_trade_reason, "CONT ")
        v8_cont_put_e += 1
    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
        v8_cons_put_e += 1
    else
        v8_context_put_e += 1
    v8_runner_tp2_price := na
    v8_runner_best_price := na
    v7_trade_dir := -1
    v7_trade_atr := visual_atr
    v7_trade_mfe := 0.0
    v7_trade_mae := 0.0
    v7_trade_counted_tp1 := false
    v7_trade_counted_tp2 := false
    v7_trade_alignment := v2_5_bear ? 1 : v2_5_bull ? -1 : 0
    if v7_trade_alignment == 1
        v7_with_trend_entries += 1
    else if v7_trade_alignment == -1
        v7_counter_trend_entries += 1
    else
        v7_neutral_entries += 1
    if v7_setup_is(v7_trade_reason, "REV")
        v7_rev_entries += 1
    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
        v7_structure_entries += 1
    else if v7_setup_is(v7_trade_reason, "CONT ")
        v7_cont_entries += 1
    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
        v7_consensus_entries += 1
    else
        v7_context_entries += 1
    if v7_setup_is(v7_trade_reason, "CONT ")
        if v8_trade_tod == 1
            v9_cont_tod1_e += 1
        else if v8_trade_tod == 2
            v9_cont_tod2_e += 1
        else if v8_trade_tod == 3
            v9_cont_tod3_e += 1
        else
            v9_cont_tod4_e += 1
    if v3_unified_enabled
        label.new(bar_index, high, v3_put_reason + "\nV" + str.tostring(v3_short_votes) + " MTF" + str.tostring(v2_put_mtf_score) + "/4", style=label.style_label_down, color=color_signal_sell, textcolor=color.white, size=size.small)

v9_runner_lb = v93_runner_profile == "Wider" ? 5 : v93_runner_profile == "Tighter" ? 2 : v5_runner_lookback
v9_runner_buf = v93_runner_profile == "Wider" ? math.max(v5_runner_buffer_atr, 0.15) : v93_runner_profile == "Tighter" ? math.min(v5_runner_buffer_atr, 0.05) : v5_runner_buffer_atr
v9_runner_low = ta.lowest(low, v9_runner_lb)[1]
v9_runner_high = ta.highest(high, v9_runner_lb)[1]

if visual_trade_labels_enabled and visual_position == "LONG" and bar_index > visual_entry_bar
    // After TP2, trail behind completed 5M structure instead of forcing a fixed final exit.
    if visual_hit_tp2 and v5_runner_after_tp2
        visual_runner_active := true
        v5_long_runner_stop = v9_runner_low - v9_runner_buf * visual_atr
        v95_parity_runner_candidate := v5_long_runner_stop
        if not na(v5_long_runner_stop)
            visual_sl_price := math.max(visual_sl_price, v5_long_runner_stop)
    v15_long_fail_now = close < v2_5_ema and close < v2_5_vwap
    v15_long_fail_prev = close[1] < v2_5_ema[1] and close[1] < v2_5_vwap[1]
    v15_long_struct_stop = low <= visual_sl_price
    v95_parity_stop_pre_exit := visual_sl_price
    v95_parity_stop_hit := v15_long_struct_stop
    v15_long_early_exit = v15_late4_enabled and not visual_hit_tp1 and high < visual_tp1_price and bar_index - visual_entry_bar >= 4 and v15_long_fail_now and v15_long_fail_prev and not v15_long_struct_stop
    if v15_long_struct_stop or v15_long_early_exit
        v81_exit_fill_long = v15_long_early_exit ? close : (open < visual_sl_price ? open : visual_sl_price)
        if v7_show_mfe_mae and v7_trade_atr > 0
            if v15_long_early_exit
                v7_trade_mfe := math.max(v7_trade_mfe, math.max((high - visual_entry_price) / v7_trade_atr, 0.0))
                v7_trade_mae := math.max(v7_trade_mae, math.max((visual_entry_price - low) / v7_trade_atr, 0.0))
            else
                v7_trade_mae := math.max(v7_trade_mae, math.max((visual_entry_price - v81_exit_fill_long) / v7_trade_atr, 0.0))
        if v15_long_early_exit
            v95_parity_exit_code := 7
            if not v96_exit_sent
                f_v96_fire("EARLY_FAIL_EXIT", "CALL", v7_trade_reason, v81_exit_fill_long, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
                v96_exit_sent := true
            label.new(bar_index, low, "LATE4 EXIT", style=label.style_label_up, color=color.orange, textcolor=color.white, size=size.small)
            v15_early_fail_exits += 1
            v15_early_fail_call_exits += 1
            if v5_post_loss_reset and v81_exit_fill_long < visual_entry_price
                v5_loss_block_dir := 1
                v5_loss_block_bar := bar_index
        else if visual_runner_active
            v95_parity_exit_code := 5
            if not v96_exit_sent
                f_v96_fire("RUNNER_EXIT", "CALL", v7_trade_reason, v81_exit_fill_long, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
                v96_exit_sent := true
            label.new(bar_index, low, "RUNNER EXIT", style=label.style_label_up, color=color.orange, textcolor=color.white, size=size.small)
            if v6_rearm_after_runner
                v6_runner_block_dir := 1
                v6_runner_block_bar := bar_index
        else
            v95_parity_exit_code := 4
            if not v96_exit_sent
                f_v96_fire("SL", "CALL", v7_trade_reason, v81_exit_fill_long, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
                v96_exit_sent := true
            label.new(bar_index, low, "🛑 SL", style=label.style_label_up, color=color.red, textcolor=color.white, size=size.small)
            if v5_post_loss_reset and not visual_hit_tp1
                v5_loss_block_dir := 1
                v5_loss_block_bar := bar_index
        v7_closed_trades += 1
        if visual_runner_active
            v7_runner_exits += 1
        else if not visual_hit_tp1
            v7_sl_before_tp1 += 1
        else if visual_hit_tp1 and not visual_hit_tp2
            v7_tp1_only_closes += 1
        if not visual_hit_tp1
            if v7_setup_is(v7_trade_reason, "REV")
                v8_rev_sl += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v8_structure_sl += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v8_cont_sl += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v8_consensus_sl += 1
            else
                v8_context_sl += 1
        if v7_show_mfe_mae
            v7_sum_mfe_atr += v7_trade_mfe
            v7_sum_mae_atr += v7_trade_mae
            if v7_setup_is(v7_trade_reason, "REV")
                v8_rev_mfe_sum += v7_trade_mfe
                v8_rev_mae_sum += v7_trade_mae
                v8_rev_closed += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v8_struct_mfe_sum += v7_trade_mfe
                v8_struct_mae_sum += v7_trade_mae
                v8_struct_closed += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v8_cont_mfe_sum += v7_trade_mfe
                v8_cont_mae_sum += v7_trade_mae
                v8_cont_closed += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v8_cons_mfe_sum += v7_trade_mfe
                v8_cons_mae_sum += v7_trade_mae
                v8_cons_closed += 1
            else
                v8_context_mfe_sum += v7_trade_mfe
                v8_context_mae_sum += v7_trade_mae
                v8_context_closed += 1
        if visual_runner_active and not na(v8_runner_tp2_price) and not na(v8_runner_best_price) and v7_trade_atr > 0
            // V8.1: runner metrics use TP2 as the zero point. Stop fills are gap-aware.
            v8_runner_exit_price = v81_exit_fill_long
            // The realized exit itself is an observed favorable price if it is above TP2.
            // Include it in best-price before auditing so maxAfterTP2 can never be below realized exitAfterTP2.
            v8_runner_best_price := math.max(v8_runner_best_price, v8_runner_exit_price)
            v8_runner_gain_raw = (v8_runner_exit_price - v8_runner_tp2_price) / v7_trade_atr
            v8_runner_max_gain_raw = (v8_runner_best_price - v8_runner_tp2_price) / v7_trade_atr
            v8_runner_max_gain = math.max(v8_runner_max_gain_raw, 0.0)
            // Bar data cannot tell whether the favorable extreme or stop happened first.
            // Clamp realized runner gain to the observed favorable maximum.
            v8_runner_gain = math.min(v8_runner_gain_raw, v8_runner_max_gain)
            v8_runner_giveback = math.max(v8_runner_max_gain - v8_runner_gain, 0.0)
            v8_runner_gain_sum += v8_runner_gain
            v8_runner_max_gain_sum += v8_runner_max_gain
            v8_runner_giveback_sum += v8_runner_giveback
            v8_runner_best_gain := math.max(v8_runner_best_gain, v8_runner_max_gain)
            v81_runner_samples += 1
            if v8_runner_max_gain >= 0.50
                v8_runner_added_half_atr += 1
            if v8_runner_giveback >= 0.50
                v8_runner_giveback_half_atr += 1
            v91_runner_direct_giveback_sum += v8_runner_giveback
            if v8_runner_gain_raw > v8_runner_max_gain + 0.001
                v91_runner_violations += 1
        v91_trade_r = v91_realized_r(v81_exit_fill_long)
        if v15_long_early_exit
            v15_early_fail_sum_r += v91_trade_r
        v91_expectancy_sum_r += v91_trade_r
        v91_expectancy_count += 1
        if v7_setup_is(v7_trade_reason, "REV")
            v91_rev_expectancy_sum_r += v91_trade_r
            v91_rev_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "STRUCTURE")
            v91_struct_expectancy_sum_r += v91_trade_r
            v91_struct_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "CONT ")
            v91_cont_expectancy_sum_r += v91_trade_r
            v91_cont_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "CONSENSUS")
            v91_cons_expectancy_sum_r += v91_trade_r
            v91_cons_expectancy_count += 1
        else
            v91_context_expectancy_sum_r += v91_trade_r
            v91_context_expectancy_count += 1
        f_v95_record(v7_trade_reason, v8_trade_tod, v7_trade_alignment, v91_trade_r)
        visual_position := "FLAT"
        visual_runner_active := false
    else
        // V8.1: only use full bar extremes when the stop was NOT touched on this bar.
        if v7_show_mfe_mae and v7_trade_atr > 0
            v7_trade_mfe := math.max(v7_trade_mfe, math.max((high - visual_entry_price) / v7_trade_atr, 0.0))
            v7_trade_mae := math.max(v7_trade_mae, math.max((visual_entry_price - low) / v7_trade_atr, 0.0))
        if visual_runner_active and not na(v8_runner_tp2_price)
            v8_runner_best_price := na(v8_runner_best_price) ? high : math.max(v8_runner_best_price, high)
        if not visual_hit_tp1 and high >= visual_tp1_price
            visual_hit_tp1 := true
            v95_parity_exit_code := 1
            visual_sl_price := visual_entry_price + visual_breakeven_atr * visual_atr
            if not v7_trade_counted_tp1
                v7_trade_counted_tp1 := true
                v7_tp1_hits += 1
                if v8_trade_tod == 1
                    v8_tod1_t1 += 1
                else if v8_trade_tod == 2
                    v8_tod2_t1 += 1
                else if v8_trade_tod == 3
                    v8_tod3_t1 += 1
                else
                    v8_tod4_t1 += 1
                if v7_trade_dir == 1
                    if v7_setup_is(v7_trade_reason, "REV")
                        v8_rev_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                        v8_struct_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONT ")
                        v8_cont_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                        v8_cons_call_t1 += 1
                    else
                        v8_context_call_t1 += 1
                else
                    if v7_setup_is(v7_trade_reason, "REV")
                        v8_rev_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                        v8_struct_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONT ")
                        v8_cont_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                        v8_cons_put_t1 += 1
                    else
                        v8_context_put_t1 += 1
                if v7_trade_alignment == 1
                    v7_with_trend_tp1 += 1
                else if v7_trade_alignment == -1
                    v7_counter_trend_tp1 += 1
                if v7_setup_is(v7_trade_reason, "REV")
                    v7_rev_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v7_structure_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v7_cont_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v7_consensus_tp1 += 1
                else
                    v7_context_tp1 += 1
                if v7_setup_is(v7_trade_reason, "CONT ")
                    if v8_trade_tod == 1
                        v9_cont_tod1_t1 += 1
                    else if v8_trade_tod == 2
                        v9_cont_tod2_t1 += 1
                    else if v8_trade_tod == 3
                        v9_cont_tod3_t1 += 1
                    else
                        v9_cont_tod4_t1 += 1
            label.new(bar_index, high, "TP1", style=label.style_label_down, color=color.new(color.green, 20), textcolor=color.white, size=size.small)
        if visual_hit_tp1 and not visual_hit_tp2 and high >= visual_tp2_price
            visual_hit_tp2 := true
            v95_parity_exit_code := v95_parity_exit_code == 1 ? 3 : 2
            visual_runner_active := false
            if not v96_tp2_sent
                f_v96_fire("TP2", "CALL", v7_trade_reason, visual_tp2_price, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
                v96_tp2_sent := true
            if not v7_trade_counted_tp2
                v7_trade_counted_tp2 := true
                v7_tp2_hits += 1
                if v7_setup_is(v7_trade_reason, "REV")
                    v8_rev_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v8_structure_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v8_cont_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v8_consensus_tp2 += 1
                else
                    v8_context_tp2 += 1
                v8_runner_tp2_price := visual_tp2_price
                v8_runner_best_price := visual_tp2_price
            label.new(bar_index, high, "TP2", style=label.style_label_down, color=color.new(color.teal, 10), textcolor=color.white, size=size.small)
        if not v5_runner_after_tp2 and visual_hit_tp2 and high >= visual_tp2_price
            if v95_parity_exit_code == 0
                v95_parity_exit_code := 2
            label.new(bar_index, high, "TP2 EXIT", style=label.style_label_down, color=color.green, textcolor=color.white, size=size.normal)
            v7_fixed_target_closes += 1
            v7_closed_trades += 1
            if v7_show_mfe_mae
                v7_sum_mfe_atr += v7_trade_mfe
                v7_sum_mae_atr += v7_trade_mae
                if v7_setup_is(v7_trade_reason, "REV")
                    v8_rev_mfe_sum += v7_trade_mfe
                    v8_rev_mae_sum += v7_trade_mae
                    v8_rev_closed += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v8_struct_mfe_sum += v7_trade_mfe
                    v8_struct_mae_sum += v7_trade_mae
                    v8_struct_closed += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v8_cont_mfe_sum += v7_trade_mfe
                    v8_cont_mae_sum += v7_trade_mae
                    v8_cont_closed += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v8_cons_mfe_sum += v7_trade_mfe
                    v8_cons_mae_sum += v7_trade_mae
                    v8_cons_closed += 1
                else
                    v8_context_mfe_sum += v7_trade_mfe
                    v8_context_mae_sum += v7_trade_mae
                    v8_context_closed += 1
            v91_trade_r = v91_realized_r(visual_tp2_price)
            v91_expectancy_sum_r += v91_trade_r
            v91_expectancy_count += 1
            if v7_setup_is(v7_trade_reason, "REV")
                v91_rev_expectancy_sum_r += v91_trade_r
                v91_rev_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v91_struct_expectancy_sum_r += v91_trade_r
                v91_struct_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v91_cont_expectancy_sum_r += v91_trade_r
                v91_cont_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v91_cons_expectancy_sum_r += v91_trade_r
                v91_cons_expectancy_count += 1
            else
                v91_context_expectancy_sum_r += v91_trade_r
                v91_context_expectancy_count += 1
            f_v95_record(v7_trade_reason, v8_trade_tod, v7_trade_alignment, v91_trade_r)
            visual_position := "FLAT"
            visual_runner_active := false

if visual_trade_labels_enabled and visual_position == "SHORT" and bar_index > visual_entry_bar
    // After TP2, trail behind completed 5M structure instead of forcing a fixed final exit.
    if visual_hit_tp2 and v5_runner_after_tp2
        visual_runner_active := true
        v5_short_runner_stop = v9_runner_high + v9_runner_buf * visual_atr
        if not na(v5_short_runner_stop)
            visual_sl_price := math.min(visual_sl_price, v5_short_runner_stop)
    if high >= visual_sl_price
        v81_exit_fill_short = open > visual_sl_price ? open : visual_sl_price
        if v7_show_mfe_mae and v7_trade_atr > 0
            v7_trade_mae := math.max(v7_trade_mae, math.max((v81_exit_fill_short - visual_entry_price) / v7_trade_atr, 0.0))
        if visual_runner_active
            v95_parity_exit_code := 5
            label.new(bar_index, high, "RUNNER EXIT", style=label.style_label_down, color=color.orange, textcolor=color.white, size=size.small)
            if v6_rearm_after_runner
                v6_runner_block_dir := -1
                v6_runner_block_bar := bar_index
        else
            v95_parity_exit_code := 4
            label.new(bar_index, high, "🛑 SL", style=label.style_label_down, color=color.red, textcolor=color.white, size=size.small)
            if v5_post_loss_reset and not visual_hit_tp1
                v5_loss_block_dir := -1
                v5_loss_block_bar := bar_index
        v7_closed_trades += 1
        if visual_runner_active
            v7_runner_exits += 1
        else if not visual_hit_tp1
            v7_sl_before_tp1 += 1
        else if visual_hit_tp1 and not visual_hit_tp2
            v7_tp1_only_closes += 1
        if not visual_hit_tp1
            if v7_setup_is(v7_trade_reason, "REV")
                v8_rev_sl += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v8_structure_sl += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v8_cont_sl += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v8_consensus_sl += 1
            else
                v8_context_sl += 1
        if v7_show_mfe_mae
            v7_sum_mfe_atr += v7_trade_mfe
            v7_sum_mae_atr += v7_trade_mae
            if v7_setup_is(v7_trade_reason, "REV")
                v8_rev_mfe_sum += v7_trade_mfe
                v8_rev_mae_sum += v7_trade_mae
                v8_rev_closed += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v8_struct_mfe_sum += v7_trade_mfe
                v8_struct_mae_sum += v7_trade_mae
                v8_struct_closed += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v8_cont_mfe_sum += v7_trade_mfe
                v8_cont_mae_sum += v7_trade_mae
                v8_cont_closed += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v8_cons_mfe_sum += v7_trade_mfe
                v8_cons_mae_sum += v7_trade_mae
                v8_cons_closed += 1
            else
                v8_context_mfe_sum += v7_trade_mfe
                v8_context_mae_sum += v7_trade_mae
                v8_context_closed += 1
        if visual_runner_active and not na(v8_runner_tp2_price) and not na(v8_runner_best_price) and v7_trade_atr > 0
            // V8.1: runner metrics use TP2 as the zero point. Stop fills are gap-aware.
            v8_runner_exit_price = v81_exit_fill_short
            // The realized exit itself is an observed favorable price if it is below TP2.
            // Include it in best-price before auditing so maxAfterTP2 can never be below realized exitAfterTP2.
            v8_runner_best_price := math.min(v8_runner_best_price, v8_runner_exit_price)
            v8_runner_gain_raw = (v8_runner_tp2_price - v8_runner_exit_price) / v7_trade_atr
            v8_runner_max_gain_raw = (v8_runner_tp2_price - v8_runner_best_price) / v7_trade_atr
            v8_runner_max_gain = math.max(v8_runner_max_gain_raw, 0.0)
            v8_runner_gain = math.min(v8_runner_gain_raw, v8_runner_max_gain)
            v8_runner_giveback = math.max(v8_runner_max_gain - v8_runner_gain, 0.0)
            v8_runner_gain_sum += v8_runner_gain
            v8_runner_max_gain_sum += v8_runner_max_gain
            v8_runner_giveback_sum += v8_runner_giveback
            v8_runner_best_gain := math.max(v8_runner_best_gain, v8_runner_max_gain)
            v81_runner_samples += 1
            if v8_runner_max_gain >= 0.50
                v8_runner_added_half_atr += 1
            if v8_runner_giveback >= 0.50
                v8_runner_giveback_half_atr += 1
            v91_runner_direct_giveback_sum += v8_runner_giveback
            if v8_runner_gain_raw > v8_runner_max_gain + 0.001
                v91_runner_violations += 1
        v91_trade_r = v91_realized_r(v81_exit_fill_short)
        v91_expectancy_sum_r += v91_trade_r
        v91_expectancy_count += 1
        if v7_setup_is(v7_trade_reason, "REV")
            v91_rev_expectancy_sum_r += v91_trade_r
            v91_rev_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "STRUCTURE")
            v91_struct_expectancy_sum_r += v91_trade_r
            v91_struct_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "CONT ")
            v91_cont_expectancy_sum_r += v91_trade_r
            v91_cont_expectancy_count += 1
        else if v7_setup_is(v7_trade_reason, "CONSENSUS")
            v91_cons_expectancy_sum_r += v91_trade_r
            v91_cons_expectancy_count += 1
        else
            v91_context_expectancy_sum_r += v91_trade_r
            v91_context_expectancy_count += 1
        f_v95_record(v7_trade_reason, v8_trade_tod, v7_trade_alignment, v91_trade_r)
        visual_position := "FLAT"
        visual_runner_active := false
    else
        if v7_show_mfe_mae and v7_trade_atr > 0
            v7_trade_mfe := math.max(v7_trade_mfe, math.max((visual_entry_price - low) / v7_trade_atr, 0.0))
            v7_trade_mae := math.max(v7_trade_mae, math.max((high - visual_entry_price) / v7_trade_atr, 0.0))
        if visual_runner_active and not na(v8_runner_tp2_price)
            v8_runner_best_price := na(v8_runner_best_price) ? low : math.min(v8_runner_best_price, low)
        if not visual_hit_tp1 and low <= visual_tp1_price
            visual_hit_tp1 := true
            v95_parity_exit_code := 1
            visual_sl_price := visual_entry_price - visual_breakeven_atr * visual_atr
            if not v7_trade_counted_tp1
                v7_trade_counted_tp1 := true
                v7_tp1_hits += 1
                if v8_trade_tod == 1
                    v8_tod1_t1 += 1
                else if v8_trade_tod == 2
                    v8_tod2_t1 += 1
                else if v8_trade_tod == 3
                    v8_tod3_t1 += 1
                else
                    v8_tod4_t1 += 1
                if v7_trade_dir == 1
                    if v7_setup_is(v7_trade_reason, "REV")
                        v8_rev_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                        v8_struct_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONT ")
                        v8_cont_call_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                        v8_cons_call_t1 += 1
                    else
                        v8_context_call_t1 += 1
                else
                    if v7_setup_is(v7_trade_reason, "REV")
                        v8_rev_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                        v8_struct_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONT ")
                        v8_cont_put_t1 += 1
                    else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                        v8_cons_put_t1 += 1
                    else
                        v8_context_put_t1 += 1
                if v7_trade_alignment == 1
                    v7_with_trend_tp1 += 1
                else if v7_trade_alignment == -1
                    v7_counter_trend_tp1 += 1
                if v7_setup_is(v7_trade_reason, "REV")
                    v7_rev_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v7_structure_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v7_cont_tp1 += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v7_consensus_tp1 += 1
                else
                    v7_context_tp1 += 1
                if v7_setup_is(v7_trade_reason, "CONT ")
                    if v8_trade_tod == 1
                        v9_cont_tod1_t1 += 1
                    else if v8_trade_tod == 2
                        v9_cont_tod2_t1 += 1
                    else if v8_trade_tod == 3
                        v9_cont_tod3_t1 += 1
                    else
                        v9_cont_tod4_t1 += 1
            label.new(bar_index, low, "TP1", style=label.style_label_up, color=color.new(color.green, 20), textcolor=color.white, size=size.small)
        if visual_hit_tp1 and not visual_hit_tp2 and low <= visual_tp2_price
            visual_hit_tp2 := true
            v95_parity_exit_code := v95_parity_exit_code == 1 ? 3 : 2
            visual_runner_active := false
            if not v96_tp2_sent
                f_v96_fire("TP2", "PUT", v7_trade_reason, visual_tp2_price, visual_entry_price, visual_tp1_price, visual_tp2_price, visual_sl_price)
                v96_tp2_sent := true
            if not v7_trade_counted_tp2
                v7_trade_counted_tp2 := true
                v7_tp2_hits += 1
                if v7_setup_is(v7_trade_reason, "REV")
                    v8_rev_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v8_structure_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v8_cont_tp2 += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v8_consensus_tp2 += 1
                else
                    v8_context_tp2 += 1
                v8_runner_tp2_price := visual_tp2_price
                v8_runner_best_price := visual_tp2_price
            label.new(bar_index, low, "TP2", style=label.style_label_up, color=color.new(color.teal, 10), textcolor=color.white, size=size.small)
        if not v5_runner_after_tp2 and visual_hit_tp2 and low <= visual_tp2_price
            if v95_parity_exit_code == 0
                v95_parity_exit_code := 2
            label.new(bar_index, low, "TP2 EXIT", style=label.style_label_up, color=color.green, textcolor=color.white, size=size.normal)
            v7_fixed_target_closes += 1
            v7_closed_trades += 1
            if v7_show_mfe_mae
                v7_sum_mfe_atr += v7_trade_mfe
                v7_sum_mae_atr += v7_trade_mae
                if v7_setup_is(v7_trade_reason, "REV")
                    v8_rev_mfe_sum += v7_trade_mfe
                    v8_rev_mae_sum += v7_trade_mae
                    v8_rev_closed += 1
                else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                    v8_struct_mfe_sum += v7_trade_mfe
                    v8_struct_mae_sum += v7_trade_mae
                    v8_struct_closed += 1
                else if v7_setup_is(v7_trade_reason, "CONT ")
                    v8_cont_mfe_sum += v7_trade_mfe
                    v8_cont_mae_sum += v7_trade_mae
                    v8_cont_closed += 1
                else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                    v8_cons_mfe_sum += v7_trade_mfe
                    v8_cons_mae_sum += v7_trade_mae
                    v8_cons_closed += 1
                else
                    v8_context_mfe_sum += v7_trade_mfe
                    v8_context_mae_sum += v7_trade_mae
                    v8_context_closed += 1
            v91_trade_r = v91_realized_r(visual_tp2_price)
            v91_expectancy_sum_r += v91_trade_r
            v91_expectancy_count += 1
            if v7_setup_is(v7_trade_reason, "REV")
                v91_rev_expectancy_sum_r += v91_trade_r
                v91_rev_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "STRUCTURE")
                v91_struct_expectancy_sum_r += v91_trade_r
                v91_struct_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "CONT ")
                v91_cont_expectancy_sum_r += v91_trade_r
                v91_cont_expectancy_count += 1
            else if v7_setup_is(v7_trade_reason, "CONSENSUS")
                v91_cons_expectancy_sum_r += v91_trade_r
                v91_cons_expectancy_count += 1
            else
                v91_context_expectancy_sum_r += v91_trade_r
                v91_context_expectancy_count += 1
            f_v95_record(v7_trade_reason, v8_trade_tod, v7_trade_alignment, v91_trade_r)
            visual_position := "FLAT"
            visual_runner_active := false



// ============================================================================
// V9.2 AUDIT-SAFE ANALYTICS HUD
// ============================================================================
v7_avg_mfe = v7_closed_trades > 0 ? v7_sum_mfe_atr / v7_closed_trades : 0.0
v7_avg_mae = v7_closed_trades > 0 ? v7_sum_mae_atr / v7_closed_trades : 0.0
v7_open_trades = visual_position == "FLAT" ? 0 : 1

var table v7_stats = table.new(position.middle_right, 3, 19, bgcolor=color.new(color.black, 15), border_width=1)
f_render_v7_stats() =>
    if barstate.islast and v7_show_analytics
        table.cell(v7_stats, 0, 0, "ULTI-7 v9.2", text_color=color.white)
        table.cell(v7_stats, 1, 0, "COUNT", text_color=color.white)
        table.cell(v7_stats, 2, 0, "RATE", text_color=color.white)
        table.cell(v7_stats, 0, 1, "Entries", text_color=color.gray)
        table.cell(v7_stats, 1, 1, str.tostring(v7_total_entries), text_color=color.white)
        table.cell(v7_stats, 2, 1, "Open " + str.tostring(v7_open_trades), text_color=color.white)
        table.cell(v7_stats, 0, 2, "TP1 reached", text_color=color.gray)
        table.cell(v7_stats, 1, 2, str.tostring(v7_tp1_hits), text_color=color.lime)
        table.cell(v7_stats, 2, 2, str.tostring(v7_pct(v7_tp1_hits, v7_total_entries), "#.0") + "%", text_color=color.lime)
        table.cell(v7_stats, 0, 3, "TP2 reached", text_color=color.gray)
        table.cell(v7_stats, 1, 3, str.tostring(v7_tp2_hits), text_color=color.aqua)
        table.cell(v7_stats, 2, 3, str.tostring(v7_pct(v7_tp2_hits, v7_total_entries), "#.0") + "%", text_color=color.aqua)
        table.cell(v7_stats, 0, 4, "SL < TP1", text_color=color.gray)
        table.cell(v7_stats, 1, 4, str.tostring(v7_sl_before_tp1), text_color=color.red)
        table.cell(v7_stats, 2, 4, str.tostring(v7_pct(v7_sl_before_tp1, v7_closed_trades), "#.0") + "%", text_color=color.red)
        table.cell(v7_stats, 0, 5, "Runner exits", text_color=color.gray)
        table.cell(v7_stats, 1, 5, str.tostring(v7_runner_exits), text_color=color.orange)
        table.cell(v7_stats, 2, 5, str.tostring(v7_pct(v7_runner_exits, v7_closed_trades), "#.0") + "%", text_color=color.orange)
        table.cell(v7_stats, 0, 6, "CALL / PUT", text_color=color.gray)
        table.cell(v7_stats, 1, 6, str.tostring(v7_call_entries) + " / " + str.tostring(v7_put_entries), text_color=color.white)
        table.cell(v7_stats, 2, 6, "", text_color=color.white)
        table.cell(v7_stats, 0, 7, "With-trend TP1", text_color=color.gray)
        table.cell(v7_stats, 1, 7, str.tostring(v7_with_trend_tp1) + "/" + str.tostring(v7_with_trend_entries), text_color=color.lime)
        table.cell(v7_stats, 2, 7, str.tostring(v7_pct(v7_with_trend_tp1, v7_with_trend_entries), "#.0") + "%", text_color=color.lime)
        table.cell(v7_stats, 0, 8, "Counter TP1", text_color=color.gray)
        table.cell(v7_stats, 1, 8, str.tostring(v7_counter_trend_tp1) + "/" + str.tostring(v7_counter_trend_entries), text_color=color.yellow)
        table.cell(v7_stats, 2, 8, str.tostring(v7_pct(v7_counter_trend_tp1, v7_counter_trend_entries), "#.0") + "%", text_color=color.yellow)
        table.cell(v7_stats, 0, 9, "Avg MFE / MAE", text_color=color.gray)
        table.cell(v7_stats, 1, 9, str.tostring(v7_avg_mfe, "#.00") + " / " + str.tostring(v7_avg_mae, "#.00"), text_color=color.white)
        table.cell(v7_stats, 2, 9, "ATR", text_color=color.gray)
        if v7_show_setup_breakdown
            table.cell(v7_stats, 0, 10, "REV TP1", text_color=color.gray)
            table.cell(v7_stats, 1, 10, str.tostring(v7_rev_tp1) + "/" + str.tostring(v7_rev_entries), text_color=color.white)
            table.cell(v7_stats, 2, 10, str.tostring(v7_pct(v7_rev_tp1, v7_rev_entries), "#.0") + "%", text_color=color.white)
            table.cell(v7_stats, 0, 11, "STRUCT TP1", text_color=color.gray)
            table.cell(v7_stats, 1, 11, str.tostring(v7_structure_tp1) + "/" + str.tostring(v7_structure_entries), text_color=color.white)
            table.cell(v7_stats, 2, 11, str.tostring(v7_pct(v7_structure_tp1, v7_structure_entries), "#.0") + "%", text_color=color.white)
            table.cell(v7_stats, 0, 12, "CONT TP1", text_color=color.gray)
            table.cell(v7_stats, 1, 12, str.tostring(v7_cont_tp1) + "/" + str.tostring(v7_cont_entries), text_color=color.white)
            table.cell(v7_stats, 2, 12, str.tostring(v7_pct(v7_cont_tp1, v7_cont_entries), "#.0") + "%", text_color=color.white)
            table.cell(v7_stats, 0, 13, "CONSENSUS TP1", text_color=color.gray)
            table.cell(v7_stats, 1, 13, str.tostring(v7_consensus_tp1) + "/" + str.tostring(v7_consensus_entries), text_color=color.white)
            table.cell(v7_stats, 2, 13, str.tostring(v7_pct(v7_consensus_tp1, v7_consensus_entries), "#.0") + "%", text_color=color.white)
            table.cell(v7_stats, 0, 14, "CONTEXT TP1", text_color=color.gray)
            table.cell(v7_stats, 1, 14, str.tostring(v7_context_tp1) + "/" + str.tostring(v7_context_entries), text_color=color.white)
            table.cell(v7_stats, 2, 14, str.tostring(v7_pct(v7_context_tp1, v7_context_entries), "#.0") + "%", text_color=color.white)
        table.cell(v7_stats, 0, 15, "Closed", text_color=color.gray)
        table.cell(v7_stats, 1, 15, str.tostring(v7_closed_trades), text_color=color.white)
        table.cell(v7_stats, 2, 15, "", text_color=color.white)
        table.cell(v7_stats, 0, 16, "TP1-only close", text_color=color.gray)
        table.cell(v7_stats, 1, 16, str.tostring(v7_tp1_only_closes), text_color=color.white)
        table.cell(v7_stats, 2, 16, str.tostring(v7_pct(v7_tp1_only_closes, v7_closed_trades), "#.0") + "%", text_color=color.white)
        table.cell(v7_stats, 0, 17, "Neutral entries", text_color=color.gray)
        table.cell(v7_stats, 1, 17, str.tostring(v7_neutral_entries), text_color=color.white)
        table.cell(v7_stats, 2, 17, "", text_color=color.white)
        table.cell(v7_stats, 0, 18, "Current", text_color=color.gray)
        table.cell(v7_stats, 1, 18, visual_position, text_color=visual_position == "LONG" ? color.lime : visual_position == "SHORT" ? color.red : color.white)
        table.cell(v7_stats, 2, 18, visual_position == "FLAT" ? "" : v7_trade_reason, text_color=color.white)

f_render_v7_stats()

// ============================================================================
// V8 DEEP ANALYTICS HUD
// ============================================================================
v8_runner_avg_gain = v8_safe_avg(v8_runner_gain_sum, v81_runner_samples)
v8_runner_avg_max = v8_safe_avg(v8_runner_max_gain_sum, v81_runner_samples)
v8_runner_avg_giveback_direct = v8_safe_avg(v8_runner_giveback_sum, v81_runner_samples)
// Display giveback from the same-sample identity; the direct sum remains visible as an audit.
v8_runner_avg_giveback = math.max(v8_runner_avg_max - v8_runner_avg_gain, 0.0)
v91_runner_direct_delta = math.abs(v8_runner_avg_giveback_direct - v8_runner_avg_giveback)
v91_expectancy_avg_r = v8_safe_avg(v91_expectancy_sum_r, v91_expectancy_count)
v91_rev_expectancy_avg_r = v8_safe_avg(v91_rev_expectancy_sum_r, v91_rev_expectancy_count)
v91_struct_expectancy_avg_r = v8_safe_avg(v91_struct_expectancy_sum_r, v91_struct_expectancy_count)
v91_cont_expectancy_avg_r = v8_safe_avg(v91_cont_expectancy_sum_r, v91_cont_expectancy_count)
v91_cons_expectancy_avg_r = v8_safe_avg(v91_cons_expectancy_sum_r, v91_cons_expectancy_count)
v91_context_expectancy_avg_r = v8_safe_avg(v91_context_expectancy_sum_r, v91_context_expectancy_count)

v8_rev_avg_mfe = v8_safe_avg(v8_rev_mfe_sum, v8_rev_closed)
v8_rev_avg_mae = v8_safe_avg(v8_rev_mae_sum, v8_rev_closed)
v8_struct_avg_mfe = v8_safe_avg(v8_struct_mfe_sum, v8_struct_closed)
v8_struct_avg_mae = v8_safe_avg(v8_struct_mae_sum, v8_struct_closed)
v8_cont_avg_mfe = v8_safe_avg(v8_cont_mfe_sum, v8_cont_closed)
v8_cont_avg_mae = v8_safe_avg(v8_cont_mae_sum, v8_cont_closed)
v8_cons_avg_mfe = v8_safe_avg(v8_cons_mfe_sum, v8_cons_closed)
v8_cons_avg_mae = v8_safe_avg(v8_cons_mae_sum, v8_cons_closed)
v8_context_avg_mfe = v8_safe_avg(v8_context_mfe_sum, v8_context_closed)
v8_context_avg_mae = v8_safe_avg(v8_context_mae_sum, v8_context_closed)

var table v8_deep = table.new(position.bottom_left, 5, 42, bgcolor=color.new(color.black, 12), border_width=1)
f_render_v8_deep() =>
    if barstate.islast and v8_show_deep_analytics
        table.cell(v8_deep, 0, 0, "V9.5 DIAGNOSTIC", text_color=color.white)
        table.cell(v8_deep, 1, 0, "ENTRY/TP1", text_color=color.white)
        table.cell(v8_deep, 2, 0, "TP2/SL", text_color=color.white)
        table.cell(v8_deep, 3, 0, "CALL/PUT TP1", text_color=color.white)
        table.cell(v8_deep, 4, 0, "MFE/MAE", text_color=color.white)

        table.cell(v8_deep, 0, 1, "REV", text_color=color.gray)
        table.cell(v8_deep, 1, 1, str.tostring(v7_rev_tp1) + "/" + str.tostring(v7_rev_entries) + " " + str.tostring(v7_pct(v7_rev_tp1, v7_rev_entries), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 2, 1, str.tostring(v8_rev_tp2) + "/" + str.tostring(v8_rev_sl), text_color=color.white)
        table.cell(v8_deep, 3, 1, str.tostring(v7_pct(v8_rev_call_t1, v8_rev_call_e), "#.0") + "% / " + str.tostring(v7_pct(v8_rev_put_t1, v8_rev_put_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 4, 1, str.tostring(v8_rev_avg_mfe, "#.00") + "/" + str.tostring(v8_rev_avg_mae, "#.00"), text_color=color.white)

        table.cell(v8_deep, 0, 2, "STRUCT", text_color=color.gray)
        table.cell(v8_deep, 1, 2, str.tostring(v7_structure_tp1) + "/" + str.tostring(v7_structure_entries) + " " + str.tostring(v7_pct(v7_structure_tp1, v7_structure_entries), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 2, 2, str.tostring(v8_structure_tp2) + "/" + str.tostring(v8_structure_sl), text_color=color.white)
        table.cell(v8_deep, 3, 2, str.tostring(v7_pct(v8_struct_call_t1, v8_struct_call_e), "#.0") + "% / " + str.tostring(v7_pct(v8_struct_put_t1, v8_struct_put_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 4, 2, str.tostring(v8_struct_avg_mfe, "#.00") + "/" + str.tostring(v8_struct_avg_mae, "#.00"), text_color=color.white)

        table.cell(v8_deep, 0, 3, "CONT", text_color=color.gray)
        table.cell(v8_deep, 1, 3, str.tostring(v7_cont_tp1) + "/" + str.tostring(v7_cont_entries) + " " + str.tostring(v7_pct(v7_cont_tp1, v7_cont_entries), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 2, 3, str.tostring(v8_cont_tp2) + "/" + str.tostring(v8_cont_sl), text_color=color.white)
        table.cell(v8_deep, 3, 3, str.tostring(v7_pct(v8_cont_call_t1, v8_cont_call_e), "#.0") + "% / " + str.tostring(v7_pct(v8_cont_put_t1, v8_cont_put_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 4, 3, str.tostring(v8_cont_avg_mfe, "#.00") + "/" + str.tostring(v8_cont_avg_mae, "#.00"), text_color=color.white)

        table.cell(v8_deep, 0, 4, "CONSENSUS", text_color=color.gray)
        table.cell(v8_deep, 1, 4, str.tostring(v7_consensus_tp1) + "/" + str.tostring(v7_consensus_entries) + " " + str.tostring(v7_pct(v7_consensus_tp1, v7_consensus_entries), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 2, 4, str.tostring(v8_consensus_tp2) + "/" + str.tostring(v8_consensus_sl), text_color=color.white)
        table.cell(v8_deep, 3, 4, str.tostring(v7_pct(v8_cons_call_t1, v8_cons_call_e), "#.0") + "% / " + str.tostring(v7_pct(v8_cons_put_t1, v8_cons_put_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 4, 4, str.tostring(v8_cons_avg_mfe, "#.00") + "/" + str.tostring(v8_cons_avg_mae, "#.00"), text_color=color.white)

        table.cell(v8_deep, 0, 5, "CONTEXT", text_color=color.gray)
        table.cell(v8_deep, 1, 5, str.tostring(v7_context_tp1) + "/" + str.tostring(v7_context_entries) + " " + str.tostring(v7_pct(v7_context_tp1, v7_context_entries), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 2, 5, str.tostring(v8_context_tp2) + "/" + str.tostring(v8_context_sl), text_color=color.white)
        table.cell(v8_deep, 3, 5, str.tostring(v7_pct(v8_context_call_t1, v8_context_call_e), "#.0") + "% / " + str.tostring(v7_pct(v8_context_put_t1, v8_context_put_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 4, 5, str.tostring(v8_context_avg_mfe, "#.00") + "/" + str.tostring(v8_context_avg_mae, "#.00"), text_color=color.white)

        table.cell(v8_deep, 0, 7, "RUNNER", text_color=color.orange)
        table.cell(v8_deep, 1, 7, "Avg after TP2 " + str.tostring(v8_runner_avg_gain, "#.00") + " ATR", text_color=color.orange)
        table.cell(v8_deep, 2, 7, "Avg max " + str.tostring(v8_runner_avg_max, "#.00"), text_color=color.orange)
        table.cell(v8_deep, 3, 7, "Giveback " + str.tostring(v8_runner_avg_giveback, "#.00"), text_color=color.orange)
        table.cell(v8_deep, 4, 7, "Best +" + str.tostring(v8_runner_best_gain, "#.00"), text_color=color.orange)
        table.cell(v8_deep, 0, 8, "Runner >.5 ATR", text_color=color.gray)
        table.cell(v8_deep, 1, 8, str.tostring(v8_runner_added_half_atr) + "/" + str.tostring(v81_runner_samples), text_color=color.white)
        table.cell(v8_deep, 2, 8, str.tostring(v7_pct(v8_runner_added_half_atr, v81_runner_samples), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 3, 8, "Giveback >.5", text_color=color.gray)
        table.cell(v8_deep, 4, 8, str.tostring(v7_pct(v8_runner_giveback_half_atr, v81_runner_samples), "#.0") + "%", text_color=color.white)

        table.cell(v8_deep, 0, 10, "TIME (" + v8_time_zone + ")", text_color=color.aqua)
        table.cell(v8_deep, 1, 10, "Entries", text_color=color.white)
        table.cell(v8_deep, 2, 10, "TP1", text_color=color.white)
        table.cell(v8_deep, 3, 10, "Rate", text_color=color.white)
        table.cell(v8_deep, 4, 10, "", text_color=color.white)
        table.cell(v8_deep, 0, 11, "08:30-09:30", text_color=color.gray)
        table.cell(v8_deep, 1, 11, str.tostring(v8_tod1_e), text_color=color.white)
        table.cell(v8_deep, 2, 11, str.tostring(v8_tod1_t1), text_color=color.white)
        table.cell(v8_deep, 3, 11, str.tostring(v7_pct(v8_tod1_t1, v8_tod1_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 0, 12, "09:30-11:00", text_color=color.gray)
        table.cell(v8_deep, 1, 12, str.tostring(v8_tod2_e), text_color=color.white)
        table.cell(v8_deep, 2, 12, str.tostring(v8_tod2_t1), text_color=color.white)
        table.cell(v8_deep, 3, 12, str.tostring(v7_pct(v8_tod2_t1, v8_tod2_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 0, 13, "11:00-13:00", text_color=color.gray)
        table.cell(v8_deep, 1, 13, str.tostring(v8_tod3_e), text_color=color.white)
        table.cell(v8_deep, 2, 13, str.tostring(v8_tod3_t1), text_color=color.white)
        table.cell(v8_deep, 3, 13, str.tostring(v7_pct(v8_tod3_t1, v8_tod3_e), "#.0") + "%", text_color=color.white)
        table.cell(v8_deep, 0, 14, "13:00+", text_color=color.gray)
        table.cell(v8_deep, 1, 14, str.tostring(v8_tod4_e), text_color=color.white)
        table.cell(v8_deep, 2, 14, str.tostring(v8_tod4_t1), text_color=color.white)
        table.cell(v8_deep, 3, 14, str.tostring(v7_pct(v8_tod4_t1, v8_tod4_e), "#.0") + "%", text_color=color.white)

        table.cell(v8_deep, 0, 15, "MFE/MAE raw", text_color=color.gray)
        table.cell(v8_deep, 1, 15, str.tostring(v7_avg_mfe, "#.00") + "/" + str.tostring(v7_avg_mae, "#.00"), text_color=color.white)
        table.cell(v8_deep, 2, 15, "Closed " + str.tostring(v7_closed_trades), text_color=color.white)
        table.cell(v8_deep, 3, 15, "ATR@entry; exit-bar safe", text_color=color.gray)
        table.cell(v8_deep, 4, 15, "", text_color=color.white)

        if v9_show_cont_time
            table.cell(v8_deep, 0, 17, "CONT BY TIME", text_color=color.aqua)
            table.cell(v8_deep, 1, 17, "Entries", text_color=color.white)
            table.cell(v8_deep, 2, 17, "TP1", text_color=color.white)
            table.cell(v8_deep, 3, 17, "Rate", text_color=color.white)
            table.cell(v8_deep, 4, 17, v9_enable_cont_optimization ? "FILTER ON" : "BASELINE", text_color=v9_enable_cont_optimization ? color.lime : color.gray)
            table.cell(v8_deep, 0, 18, "08:30-09:30", text_color=color.gray)
            table.cell(v8_deep, 1, 18, str.tostring(v9_cont_tod1_e), text_color=color.white)
            table.cell(v8_deep, 2, 18, str.tostring(v9_cont_tod1_t1), text_color=color.white)
            table.cell(v8_deep, 3, 18, str.tostring(v7_pct(v9_cont_tod1_t1, v9_cont_tod1_e), "#.0") + "%", text_color=color.white)
            table.cell(v8_deep, 0, 19, "09:30-11:00", text_color=color.gray)
            table.cell(v8_deep, 1, 19, str.tostring(v9_cont_tod2_e), text_color=color.white)
            table.cell(v8_deep, 2, 19, str.tostring(v9_cont_tod2_t1), text_color=color.white)
            table.cell(v8_deep, 3, 19, str.tostring(v7_pct(v9_cont_tod2_t1, v9_cont_tod2_e), "#.0") + "%", text_color=color.white)
            table.cell(v8_deep, 0, 20, "11:00-13:00", text_color=color.gray)
            table.cell(v8_deep, 1, 20, str.tostring(v9_cont_tod3_e), text_color=color.white)
            table.cell(v8_deep, 2, 20, str.tostring(v9_cont_tod3_t1), text_color=color.white)
            table.cell(v8_deep, 3, 20, str.tostring(v7_pct(v9_cont_tod3_t1, v9_cont_tod3_e), "#.0") + "%", text_color=color.white)
            table.cell(v8_deep, 0, 21, "13:00+", text_color=color.gray)
            table.cell(v8_deep, 1, 21, str.tostring(v9_cont_tod4_e), text_color=color.white)
            table.cell(v8_deep, 2, 21, str.tostring(v9_cont_tod4_t1), text_color=color.white)
            table.cell(v8_deep, 3, 21, str.tostring(v7_pct(v9_cont_tod4_t1, v9_cont_tod4_e), "#.0") + "%", text_color=color.white)

        // V9.1 runner audit: same population, same TP2 zero point, explicit violation count.
        v9_runner_identity_err = math.abs(v8_runner_avg_max - (v8_runner_avg_gain + v8_runner_avg_giveback))
        table.cell(v8_deep, 4, 15, "Δ " + str.tostring(v9_runner_identity_err, "#.000") + " V=" + str.tostring(v91_runner_violations), text_color=(v9_runner_identity_err <= 0.01 and v91_runner_violations == 0) ? color.lime : color.red)
        table.cell(v8_deep, 0, 23, "EXPECTANCY (R)", text_color=color.yellow)
        table.cell(v8_deep, 4, 23, "V9.5 / " + v94_mode, text_color=color.aqua)
        table.cell(v8_deep, 1, 23, "All " + str.tostring(v91_expectancy_avg_r, "#.00"), text_color=v91_expectancy_avg_r >= 0 ? color.lime : color.red)
        table.cell(v8_deep, 2, 23, "N=" + str.tostring(v91_expectancy_count), text_color=color.white)
        table.cell(v8_deep, 3, 23, str.tostring(v93_tp1_scale_pct, "#.0") + "/" + str.tostring(v93_tp2_scale_pct, "#.0") + "/" + str.tostring(v93_runner_scale_pct, "#.0") + " norm", text_color=color.gray)
        table.cell(v8_deep, 4, 23, "GB audit Δ " + str.tostring(v91_runner_direct_delta, "#.000"), text_color=v91_runner_direct_delta <= 0.01 ? color.lime : color.red)
        table.cell(v8_deep, 0, 24, "REV", text_color=color.gray)
        table.cell(v8_deep, 1, 24, str.tostring(v91_rev_expectancy_avg_r, "#.00") + "R", text_color=v91_rev_expectancy_avg_r >= 0 ? color.lime : color.red)
        table.cell(v8_deep, 2, 24, "N=" + str.tostring(v91_rev_expectancy_count), text_color=color.white)
        table.cell(v8_deep, 0, 25, "STRUCT", text_color=color.gray)
        table.cell(v8_deep, 1, 25, str.tostring(v91_struct_expectancy_avg_r, "#.00") + "R", text_color=v91_struct_expectancy_avg_r >= 0 ? color.lime : color.red)
        table.cell(v8_deep, 2, 25, "N=" + str.tostring(v91_struct_expectancy_count), text_color=color.white)
        table.cell(v8_deep, 0, 26, "CONT", text_color=color.gray)
        table.cell(v8_deep, 1, 26, str.tostring(v91_cont_expectancy_avg_r, "#.00") + "R", text_color=v91_cont_expectancy_avg_r >= 0 ? color.lime : color.red)
        table.cell(v8_deep, 2, 26, "N=" + str.tostring(v91_cont_expectancy_count), text_color=color.white)
        table.cell(v8_deep, 3, 26, "Mode " + v93_cont_mode, text_color=v93_cont_mode == "Adaptive" ? color.aqua : color.gray)
        table.cell(v8_deep, 0, 27, "CONS/CTX", text_color=color.gray)
        table.cell(v8_deep, 1, 27, str.tostring(v91_cons_expectancy_avg_r, "#.00") + "/" + str.tostring(v91_context_expectancy_avg_r, "#.00") + "R", text_color=color.white)
        table.cell(v8_deep, 2, 27, "N=" + str.tostring(v91_cons_expectancy_count + v91_context_expectancy_count), text_color=color.white)
        table.cell(v8_deep, 3, 27, "Scale " + str.tostring(v93_tp1_scale_pct, "#.0") + "/" + str.tostring(v93_tp2_scale_pct, "#.0") + "/" + str.tostring(v93_runner_scale_pct, "#.0"), text_color=color.gray)
        v92_reason_sum = v91_rev_expectancy_count + v91_struct_expectancy_count + v91_cont_expectancy_count + v91_cons_expectancy_count + v91_context_expectancy_count
        v92_closed_match = v91_expectancy_count == v7_closed_trades
        v92_reason_match = v92_reason_sum == v91_expectancy_count
        table.cell(v8_deep, 0, 28, "ACCOUNTING", text_color=color.yellow)
        table.cell(v8_deep, 1, 28, "Closed " + str.tostring(v7_closed_trades), text_color=color.white)
        table.cell(v8_deep, 2, 28, "Exp N " + str.tostring(v91_expectancy_count), text_color=v92_closed_match ? color.lime : color.red)
        table.cell(v8_deep, 3, 28, "Reason N " + str.tostring(v92_reason_sum), text_color=v92_reason_match ? color.lime : color.red)
        table.cell(v8_deep, 4, 28, (v92_closed_match and v92_reason_match) ? "MATCH" : "MISMATCH", text_color=(v92_closed_match and v92_reason_match) ? color.lime : color.red)
        table.cell(v8_deep, 0, 29, "RUNNER AUDIT", text_color=color.yellow)
        table.cell(v8_deep, 1, 29, "Samples " + str.tostring(v81_runner_samples), text_color=color.white)
        table.cell(v8_deep, 2, 29, "Violations " + str.tostring(v91_runner_violations), text_color=v91_runner_violations == 0 ? color.lime : color.red)
        table.cell(v8_deep, 3, 29, "Identity Δ " + str.tostring(v9_runner_identity_err, "#.000"), text_color=v9_runner_identity_err <= 0.01 ? color.lime : color.red)
        table.cell(v8_deep, 4, 29, "GB Δ " + str.tostring(v91_runner_direct_delta, "#.000"), text_color=v91_runner_direct_delta <= 0.01 ? color.lime : color.red)
        if v94_show_blocked
            table.cell(v8_deep, 0, 30, "V9.4 BLOCKED", text_color=color.yellow)
            table.cell(v8_deep, 1, 30, "REV " + str.tostring(v94_blocked_rev), text_color=color.white)
            table.cell(v8_deep, 2, 30, "CONT " + str.tostring(v94_blocked_cont), text_color=color.white)
            table.cell(v8_deep, 3, 30, "Mode " + v94_mode, text_color=color.aqua)
            table.cell(v8_deep, 4, 30, "STRUCT unchanged", text_color=color.lime)

        if v95_show_diagnostics
            table.cell(v8_deep, 0, 31, "V9.5 REV x TIME", text_color=color.aqua)
            table.cell(v8_deep, 1, 31, "08:30 " + str.tostring(f_v95_avg(v95_rev_tod_sum, v95_rev_tod_n, 0), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 2, 31, "09:30 " + str.tostring(f_v95_avg(v95_rev_tod_sum, v95_rev_tod_n, 1), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 3, 31, "11:00 " + str.tostring(f_v95_avg(v95_rev_tod_sum, v95_rev_tod_n, 2), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 4, 31, "13:00 " + str.tostring(f_v95_avg(v95_rev_tod_sum, v95_rev_tod_n, 3), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 0, 32, "REV N", text_color=color.gray)
            table.cell(v8_deep, 1, 32, str.tostring(array.get(v95_rev_tod_n, 0)), text_color=color.gray)
            table.cell(v8_deep, 2, 32, str.tostring(array.get(v95_rev_tod_n, 1)), text_color=color.gray)
            table.cell(v8_deep, 3, 32, str.tostring(array.get(v95_rev_tod_n, 2)), text_color=color.gray)
            table.cell(v8_deep, 4, 32, str.tostring(array.get(v95_rev_tod_n, 3)), text_color=color.gray)

            table.cell(v8_deep, 0, 34, "V9.5 CONT x TIME", text_color=color.aqua)
            table.cell(v8_deep, 1, 34, "08:30 " + str.tostring(f_v95_avg(v95_cont_tod_sum, v95_cont_tod_n, 0), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 2, 34, "09:30 " + str.tostring(f_v95_avg(v95_cont_tod_sum, v95_cont_tod_n, 1), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 3, 34, "11:00 " + str.tostring(f_v95_avg(v95_cont_tod_sum, v95_cont_tod_n, 2), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 4, 34, "13:00 " + str.tostring(f_v95_avg(v95_cont_tod_sum, v95_cont_tod_n, 3), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 0, 35, "CONT N", text_color=color.gray)
            table.cell(v8_deep, 1, 35, str.tostring(array.get(v95_cont_tod_n, 0)), text_color=color.gray)
            table.cell(v8_deep, 2, 35, str.tostring(array.get(v95_cont_tod_n, 1)), text_color=color.gray)
            table.cell(v8_deep, 3, 35, str.tostring(array.get(v95_cont_tod_n, 2)), text_color=color.gray)
            table.cell(v8_deep, 4, 35, str.tostring(array.get(v95_cont_tod_n, 3)), text_color=color.gray)

            table.cell(v8_deep, 0, 37, "REV x TREND", text_color=color.aqua)
            table.cell(v8_deep, 1, 37, "WITH " + str.tostring(f_v95_avg(v95_rev_align_sum, v95_rev_align_n, 0), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 2, 37, "COUNTER " + str.tostring(f_v95_avg(v95_rev_align_sum, v95_rev_align_n, 1), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 3, 37, "NEUT " + str.tostring(f_v95_avg(v95_rev_align_sum, v95_rev_align_n, 2), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 4, 37, "N " + str.tostring(array.sum(v95_rev_align_n)), text_color=color.gray)
            table.cell(v8_deep, 0, 38, "REV trend N", text_color=color.gray)
            table.cell(v8_deep, 1, 38, str.tostring(array.get(v95_rev_align_n, 0)), text_color=color.gray)
            table.cell(v8_deep, 2, 38, str.tostring(array.get(v95_rev_align_n, 1)), text_color=color.gray)
            table.cell(v8_deep, 3, 38, str.tostring(array.get(v95_rev_align_n, 2)), text_color=color.gray)

            table.cell(v8_deep, 0, 40, "CONT x TREND", text_color=color.aqua)
            table.cell(v8_deep, 1, 40, "WITH " + str.tostring(f_v95_avg(v95_cont_align_sum, v95_cont_align_n, 0), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 2, 40, "COUNTER " + str.tostring(f_v95_avg(v95_cont_align_sum, v95_cont_align_n, 1), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 3, 40, "NEUT " + str.tostring(f_v95_avg(v95_cont_align_sum, v95_cont_align_n, 2), "#.00") + "R", text_color=color.white)
            table.cell(v8_deep, 4, 40, "N " + str.tostring(array.sum(v95_cont_align_n)), text_color=color.gray)
            table.cell(v8_deep, 0, 41, "CONT trend N", text_color=color.gray)
            table.cell(v8_deep, 1, 41, str.tostring(array.get(v95_cont_align_n, 0)), text_color=color.gray)
            table.cell(v8_deep, 2, 41, str.tostring(array.get(v95_cont_align_n, 1)), text_color=color.gray)
            table.cell(v8_deep, 3, 41, str.tostring(array.get(v95_cont_align_n, 2)), text_color=color.gray)

f_render_v8_deep()

// Pine Logs parity export. Use the 5-minute chart and copy these records from Pine Logs.
// Entry side: 1=CALL, -1=PUT. Setup: 1=REV, 2=STRUCTURE, 3=CONT,
// 4=CONSENSUS, 5=CONTEXT, 6=RAW. Exit: 1=TP1, 2=TP2, 3=TP1+TP2 same bar,
// 4=SL, 5=RUNNER, 6=FINAL_TP, 7=LATE4.
// Context bits: 1=confirmed, 2/4=5M bull/bear, 8/16=15M bull/bear,
// 32/64=CALL/PUT MTF pass, 128/256=CALL/PUT execution pass, 512=chop,
// 1024/2048=CALL/PUT exhaustion, 4096/8192=CALL/PUT expansion override,
// 16384/32768=CALL/PUT room pass.
v95_parity_context_flags =
         (barstate.isconfirmed ? 1 : 0)
     + (v2_5_bull ? 2 : 0)
     + (v2_5_bear ? 4 : 0)
     + (v2_15_bull ? 8 : 0)
     + (v2_15_bear ? 16 : 0)
     + (v2_call_mtf_ok ? 32 : 0)
     + (v2_put_mtf_ok ? 64 : 0)
     + (v2_long_ok ? 128 : 0)
     + (v2_short_ok ? 256 : 0)
     + (v2_chop ? 512 : 0)
     + (v2_bull_exhaustion ? 1024 : 0)
     + (v2_bear_exhaustion ? 2048 : 0)
     + (v2_expansion_call_override ? 4096 : 0)
     + (v2_expansion_put_override ? 8192 : 0)
     + (v2_room_call_ok ? 16384 : 0)
     + (v2_room_put_ok ? 32768 : 0)

// Setup bits: 1/2=READY CALL/PUT, 4/8=CONT armed CALL/PUT,
// 16/32=REV, 64/128=STRUCTURE, 256/512=CONT, 1024/2048=CONSENSUS,
// 4096/8192=CONTEXT, 16384/32768=candidate, 65536/131072=entry CALL/PUT.
v95_parity_setup_flags =
         (v3_ready_dir == 1 ? 1 : 0)
     + (v3_ready_dir == -1 ? 2 : 0)
     + (v4_cont_call_armed ? 4 : 0)
     + (v4_cont_put_armed ? 8 : 0)
     + (v6_reversal_call ? 16 : 0)
     + (v6_reversal_put ? 32 : 0)
     + (v3_structure_call ? 64 : 0)
     + (v3_structure_put ? 128 : 0)
     + (v9_cont_call ? 256 : 0)
     + (v9_cont_put ? 512 : 0)
     + (v3_consensus_call ? 1024 : 0)
     + (v3_consensus_put ? 2048 : 0)
     + (v3_context_call ? 4096 : 0)
     + (v3_context_put ? 8192 : 0)
     + (v3_call_candidate ? 16384 : 0)
     + (v3_put_candidate ? 32768 : 0)
     + (v3_entry_call ? 65536 : 0)
     + (v3_entry_put ? 131072 : 0)

// Trade bits: 1/2=LONG/SHORT, 4/8=TP1/TP2, 16=runner,
// 32/64/128/256=entry/TP1/TP2/exit alert sent. Latched bits are zeroed while flat.
v95_parity_has_trade_state = visual_position != "FLAT" or v95_parity_exit_code != 0
v95_parity_trade_flags =
         (visual_position == "LONG" ? 1 : 0)
         + (visual_position == "SHORT" ? 2 : 0)
         + (v95_parity_has_trade_state and visual_hit_tp1 ? 4 : 0)
         + (v95_parity_has_trade_state and visual_hit_tp2 ? 8 : 0)
         + (v95_parity_has_trade_state and visual_runner_active ? 16 : 0)
         + (v95_parity_has_trade_state and v96_entry_sent ? 32 : 0)
         + (v95_parity_has_trade_state and v96_tp1_sent ? 64 : 0)
         + (v95_parity_has_trade_state and v96_tp2_sent ? 128 : 0)
         + (v95_parity_has_trade_state and v96_exit_sent ? 256 : 0)

// Source bits: 1/2=Poki CALL/PUT, 4/8=SMC CALL/PUT, 16/32=VIDYA CALL/PUT.
v95_parity_source_flags =
         (v3_poki_long ? 1 : 0)
         + (v3_poki_short ? 2 : 0)
         + (v3_smc_long ? 4 : 0)
         + (v3_smc_short ? 8 : 0)
         + (v3_vidya_long ? 16 : 0)
         + (v3_vidya_short ? 32 : 0)

// SMC bits: 1/2=BUY/SELL condition, 4/8=BUY/SELL repeated-signal allowed,
// 16/32=BUY/SELL trend, 64/128=BUY/SELL lower-TF, 256/512=BUY/SELL volume,
// 1024/2048=BUY/SELL breakout, 4096/8192=BUY/SELL CHoCH,
// 16384/32768=BUY/SELL BOS.
v95_parity_smc_flags =
         (smc_buy_condition ? 1 : 0)
         + (smc_sell_condition ? 2 : 0)
         + (smc_buy_allowed ? 4 : 0)
         + (smc_sell_allowed ? 8 : 0)
         + (smc_buy_trend_ok ? 16 : 0)
         + (smc_sell_trend_ok ? 32 : 0)
         + (smc_buy_lower_tf_ok ? 64 : 0)
         + (smc_sell_lower_tf_ok ? 128 : 0)
         + (smc_buy_volume_ok ? 256 : 0)
         + (smc_sell_volume_ok ? 512 : 0)
         + (smc_buy_breakout_ok ? 1024 : 0)
         + (smc_sell_breakout_ok ? 2048 : 0)
         + (smc_choch_buy ? 4096 : 0)
         + (smc_choch_sell ? 8192 : 0)
         + (smc_bos_buy ? 16384 : 0)
         + (smc_bos_sell ? 32768 : 0)

v95_parity_ready_age = na(v3_ready_bar) ? 0 : bar_index - v3_ready_bar
v95_parity_has_trade_levels = v95_parity_has_trade_state

v95_parity_header = "V15P_HEADER|symbol|timeframe|time_ms|open|high|low|close|volume|ema|vwap|ema5|vwap5|atr5|trade_atr|call_mtf|put_mtf|call_room|put_room|extension|ema_vwap_width|churn|body_atr|rel_vol|momentum_call|momentum_put|pattern_call|pattern_put|vwap_extension_pct|votes_call|votes_put|ready_dir|ready_age|context_flags|setup_flags|trade_flags|entry_side|entry_setup|exit_code|entry_price|tp1|tp2|tp3|stop|source_flags|smc_flags|smc_volume_filter_enabled|smc_volume_long_length|smc_volume_short_length|smc_volume_avg_long|smc_volume_short_sma|smc_volume_short_change|smc_volume_condition|runner_stop_candidate|stop_pre_exit|stop_hit"
v95_parity_row_a = "V15P|" + syminfo.ticker + "|" + timeframe.period + "|" + str.tostring(time) + "|" + str.tostring(open) + "|" + str.tostring(high) + "|" + str.tostring(low) + "|" + str.tostring(close) + "|" + str.tostring(volume)
v95_parity_row_b = "|" + str.tostring(ema_out) + "|" + str.tostring(vwap_vwapValue) + "|" + str.tostring(v2_5_ema) + "|" + str.tostring(v2_5_vwap) + "|" + str.tostring(v2_5_atr) + "|" + str.tostring(visual_atr) + "|" + str.tostring(v2_call_mtf_score) + "|" + str.tostring(v2_put_mtf_score) + "|" + str.tostring(v2_call_room) + "|" + str.tostring(v2_put_room) + "|" + str.tostring(v2_extension) + "|" + str.tostring(v2_ema_vwap_width) + "|" + str.tostring(v2_churn_return)
v95_parity_row_c = "|" + str.tostring(v2_1_body_atr) + "|" + str.tostring(v2_1_rel_vol) + "|" + str.tostring(entry_quality_momentum_bull) + "|" + str.tostring(entry_quality_momentum_bear) + "|" + str.tostring(entry_quality_pattern_bull) + "|" + str.tostring(entry_quality_pattern_bear) + "|" + str.tostring(entry_quality_vwap_extension) + "|" + str.tostring(v3_long_votes) + "|" + str.tostring(v3_short_votes) + "|" + str.tostring(v3_ready_dir) + "|" + str.tostring(v95_parity_ready_age)
v95_parity_row_d = "|" + str.tostring(v95_parity_context_flags) + "|" + str.tostring(v95_parity_setup_flags) + "|" + str.tostring(v95_parity_trade_flags) + "|" + str.tostring(v95_parity_entry_side) + "|" + str.tostring(v95_parity_entry_setup) + "|" + str.tostring(v95_parity_exit_code)
v95_parity_row_e = "|" + str.tostring(v95_parity_has_trade_levels ? visual_entry_price : na) + "|" + str.tostring(v95_parity_has_trade_levels ? visual_tp1_price : na) + "|" + str.tostring(v95_parity_has_trade_levels ? visual_tp2_price : na) + "|" + str.tostring(v95_parity_has_trade_levels ? visual_tp3_price : na) + "|" + str.tostring(v95_parity_has_trade_levels ? visual_sl_price : na)
v95_parity_row_f = "|" + str.tostring(v95_parity_source_flags) + "|" + str.tostring(v95_parity_smc_flags)
v95_parity_row_g = "|" + str.tostring(smc_use_volume_filter) + "|" + str.tostring(smc_volumeLongPeriod) + "|" + str.tostring(smc_volumeShortPeriod) + "|" + str.tostring(smc_volAvg50) + "|" + str.tostring(smc_volShort) + "|" + str.tostring(ta.change(smc_volShort)) + "|" + str.tostring(smc_volCondition) + "|" + str.tostring(v95_parity_runner_candidate) + "|" + str.tostring(v95_parity_stop_pre_exit) + "|" + str.tostring(v95_parity_stop_hit)

if barstate.islastconfirmedhistory and timeframe.isminutes and timeframe.multiplier == 5
    log.info(v95_parity_header)
if barstate.isconfirmed and timeframe.isminutes and timeframe.multiplier == 5
    log.info(v95_parity_row_a + v95_parity_row_b + v95_parity_row_c + v95_parity_row_d + v95_parity_row_e + v95_parity_row_f + v95_parity_row_g)
if v96_shadow_json and barstate.isconfirmed and timeframe.isminutes and timeframe.multiplier == 5
    v96_shadow_bar_message = "{\"event\":\"BAR\",\"symbol\":\"" + syminfo.ticker + "\",\"timeframe\":\"" + timeframe.period + "\",\"bar_time_ms\":" + str.tostring(time) + ",\"open\":" + str.tostring(open, format.mintick) + ",\"high\":" + str.tostring(high, format.mintick) + ",\"low\":" + str.tostring(low, format.mintick) + ",\"close\":" + str.tostring(close, format.mintick) + ",\"volume\":" + str.tostring(volume) + ",\"entry_side\":" + str.tostring(v95_parity_entry_side) + ",\"entry_setup\":" + str.tostring(v95_parity_entry_setup) + ",\"exit_code\":" + str.tostring(v95_parity_exit_code) + ",\"trade_flags\":" + str.tostring(v95_parity_trade_flags) + "}"
    alert(v96_shadow_bar_message, alert.freq_all)
