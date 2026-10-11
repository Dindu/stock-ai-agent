// This Pine Script® code is subject to the terms of the Mozilla Public License 2.0 at https://mozilla.org/MPL/2.0/
// © dindusam

//@version=6
// Faithful seven-indicator merge. Original calculations, inputs, visual styles, alerts, labels, lines, boxes, tables and zones are retained.
// The only structural change is that all modules share one script and names are namespaced to avoid collisions.
// Price-chart modules are overlaid; the standalone Volume plots are intentionally omitted.
indicator("ULTI-7 | Faithful Seven-Indicator Merge", "Seven-Indicator Merge sep 27", overlay=true, max_bars_back=4900, max_lines_count=500, max_labels_count=500, max_boxes_count=500)

f_limit_labels(array<label> label_ids, label new_label, int max_count) =>
    array.push(label_ids, new_label)
    if array.size(label_ids) > max_count
        label.delete(array.shift(label_ids))
    new_label


// ============================================================================
// ORIGINAL MODULE: EMA
// ============================================================================
ema_len = input.int(9, minval=1, title="Length")
ema_src = input(close, title="Source")
ema_offset = input.int(title="Offset", defval=0, minval=-500, maxval=500, display = display.none)
ema_out = ta.ema(ema_src, ema_len)
plot(ema_out, title="EMA", color=color.blue, offset=ema_offset, force_overlay=true)

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
plot(ema_smoothingMA, "EMA-based MA", color=color.yellow, display = ema_enableMA ? display.all : display.none, editable = ema_enableMA, force_overlay=true)
ema_bbUpperBand = plot(ema_smoothingMA + ema_smoothingStDev, title = "Upper Bollinger Band", color=color.green, display = ema_isBB ? display.all : display.none, editable = ema_isBB, force_overlay=true)
ema_bbLowerBand = plot(ema_smoothingMA - ema_smoothingStDev, title = "Lower Bollinger Band", color=color.green, display = ema_isBB ? display.all : display.none, editable = ema_isBB, force_overlay=true)
fill(ema_bbUpperBand, ema_bbLowerBand, color=ema_isBB ? color.new(color.green, 90) : na, title="Bollinger Bands Background Fill", display=ema_isBB ? display.all : display.none, editable=ema_isBB)

// ============================================================================
// ORIGINAL MODULE: VWAP
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
		new_earnings_actual = request.earnings(syminfo.tickerid, earnings.actual, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		new_earnings_standardized = request.earnings(syminfo.tickerid, earnings.standardized, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(new_earnings_actual) or not na(new_earnings_standardized)
	"Dividends" =>
		new_dividends = request.dividends(syminfo.tickerid, dividends.gross, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(new_dividends)
	"Splits"    =>
		new_split = request.splits(syminfo.tickerid, splits.denominator, barmerge.gaps_on, barmerge.lookahead_on, ignore_invalid_symbol=true)
		not na(new_split)
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
    stdevAbs = _stdevUpper - _vwap
	bandBasis = vwap_calcModeInput == "Standard Deviation" ? stdevAbs : _vwap * 0.01
	vwap_upperBandValue1 := _vwap + bandBasis * vwap_bandMult_1
	vwap_lowerBandValue1 := _vwap - bandBasis * vwap_bandMult_1
	vwap_upperBandValue2 := _vwap + bandBasis * vwap_bandMult_2
	vwap_lowerBandValue2 := _vwap - bandBasis * vwap_bandMult_2
	vwap_upperBandValue3 := _vwap + bandBasis * vwap_bandMult_3
	vwap_lowerBandValue3 := _vwap - bandBasis * vwap_bandMult_3

plot(vwap_vwapValue, title = "VWAP", color = #2962FF, offset = vwap_offset, force_overlay=true)

vwap_displayBand1 = vwap_showBand_1 ? display.all : display.none
vwap_upperBand_1 = plot(vwap_upperBandValue1, title="Upper Band #1", color = color.green, offset = vwap_offset, display = vwap_displayBand1, editable = vwap_showBand_1, force_overlay=true)
vwap_lowerBand_1 = plot(vwap_lowerBandValue1, title="Lower Band #1", color = color.green, offset = vwap_offset, display = vwap_displayBand1, editable = vwap_showBand_1, force_overlay=true)
fill(vwap_upperBand_1, vwap_lowerBand_1, title="Bands Fill #1", color=color.new(color.green, 95), display=vwap_displayBand1, editable=vwap_showBand_1)

vwap_displayBand2 = vwap_showBand_2 ? display.all : display.none
vwap_upperBand_2 = plot(vwap_upperBandValue2, title="Upper Band #2", color = color.olive, offset = vwap_offset, display = vwap_displayBand2, editable = vwap_showBand_2, force_overlay=true)
vwap_lowerBand_2 = plot(vwap_lowerBandValue2, title="Lower Band #2", color = color.olive, offset = vwap_offset, display = vwap_displayBand2, editable = vwap_showBand_2, force_overlay=true)
fill(vwap_upperBand_2, vwap_lowerBand_2, title="Bands Fill #2", color=color.new(color.olive, 95), display=vwap_displayBand2, editable=vwap_showBand_2)

vwap_displayBand3 = vwap_showBand_3 ? display.all : display.none
vwap_upperBand_3 = plot(vwap_upperBandValue3, title="Upper Band #3", color = color.teal, offset = vwap_offset, display = vwap_displayBand3, editable = vwap_showBand_3, force_overlay=true)
vwap_lowerBand_3 = plot(vwap_lowerBandValue3, title="Lower Band #3", color = color.teal, offset = vwap_offset, display = vwap_displayBand3, editable = vwap_showBand_3, force_overlay=true)
fill(vwap_upperBand_3, vwap_lowerBand_3, title="Bands Fill #3", color=color.new(color.teal, 95), display=vwap_displayBand3, editable=vwap_showBand_3)

// ============================================================================
// ORIGINAL MODULE: Poki
// ============================================================================
// Parabolic SAR
poki_start = input.int(3, minval=0, maxval=10, title="Start - Default = 2 - Multiplied by .01")
poki_increment = input.int(3, minval=0, maxval=10, title="Step Setting (Sensitivity) - Default = 2 - Multiplied by .01" )
poki_maximum = input.int(3, minval=1, maxval=10, title="Maximum Step (Sensitivity) - Default = 2 - Multiplied by .10")
poki_sus = input(true, "Show Up Trending Parabolic Sar")
poki_sds = input(true, "Show Down Trending Parabolic Sar")
poki_disc = input(false, title="Start and Step settings are *.01 so 2 = .02 etc, Maximum Step is *.10 so 2 = .2")
poki_startCalc = poki_start * .01
poki_incrementCalc = poki_increment * .01
poki_maximumCalc = poki_maximum * .10
poki_sarUp = ta.sar(poki_startCalc, poki_incrementCalc, poki_maximumCalc)
poki_sarDown = ta.sar(poki_startCalc, poki_incrementCalc, poki_maximumCalc)
poki_colUp = close >= poki_sarDown ? color.blue : na
poki_colDown = close <= poki_sarUp ? color.red : na
plot(poki_sus and not na(poki_sarUp) ? poki_sarUp : na, title="Up Trending SAR", style=plot.style_circles, linewidth=2,color=poki_colUp, force_overlay=true)
plot(poki_sds and not na(poki_sarDown) ? poki_sarDown : na, title="Up Trending SAR", style=plot.style_circles, linewidth=2,color=poki_colDown, force_overlay=true)
// Input
poki_close_price = close[0]
poki_len = input.int(defval=50, minval=1, title="Linear Regression length")

poki_linear_reg = ta.linreg(poki_close_price, poki_len, 0)
poki_linear_reg_prev = ta.linreg(close[1], poki_len, 0)
poki_slope = ((poki_linear_reg - poki_linear_reg_prev) / timeframe.in_seconds(timeframe.period))



poki_length = input.int(20, title="Bollinger Length", minval=1)
poki_multiplier = input.float(2, title="Bollinger Deviation", minval=1)
poki_overbought = input.int(1, title="Overbought", minval=1)
poki_oversold = input.int(0, title="Oversold", minval=0)
poki_custom_timeframe = input.bool(false, title="Use another Timeframe?")
poki_highTimeFrame = input.timeframe("60", title="Select The Timeframe")
poki_res1 = poki_custom_timeframe ? poki_highTimeFrame : timeframe.period

poki_smabasis = ta.sma(close, poki_length)
poki_stdev = ta.stdev(close, poki_length)
poki_cierre = request.security(syminfo.tickerid, poki_res1, close, gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
poki_alta = request.security(syminfo.tickerid, poki_res1, high, gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
poki_baja = request.security(syminfo.tickerid, poki_res1, low, gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
poki_basis1 = request.security(syminfo.tickerid, poki_res1, poki_smabasis, gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
poki_stdevb = request.security(syminfo.tickerid, poki_res1, poki_stdev, gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_off)
poki_dev = poki_multiplier * poki_stdevb // ta.stdev(poki_cierre, poki_length)
poki_upper = poki_basis1 + poki_dev
poki_lower = poki_basis1 - poki_dev

poki_bbr = (poki_cierre - poki_lower)/(poki_upper - poki_lower)

// plot(poki_bbr, force_overlay=true)

// // MARCA LAS RESISTENCIAS
poki_pintarojo = 0.0
poki_pintarojo := nz(poki_pintarojo[1])
poki_pintarojo := poki_bbr[1] > poki_overbought and poki_bbr < poki_overbought ? poki_alta[1] :  nz(poki_pintarojo[1])
poki_p = plot(poki_pintarojo, color = color.red, style=plot.style_circles, linewidth=2, force_overlay=true)

// // MARCA LOS SOPORTES
poki_pintaverde = 0.0
poki_pintaverde := nz(poki_pintaverde[1])
poki_pintaverde := poki_bbr[1] < poki_oversold and poki_bbr > poki_oversold ? poki_baja[1] :  nz(poki_pintaverde[1])
poki_g = plot(poki_pintaverde, color = color.blue, style=plot.style_circles, linewidth=2, force_overlay=true)
//
poki_method = input.string("ATR", options=["ATR", "Traditional", "Part of Price"], title="Renko Assignment Method")
poki_methodvalue = input.float(14.0, minval=0, title="Value")
poki_pricesource = input.string("Close", options=["Close", "Open / Close", "High / Low"], title="Price Source")
poki_useClose = poki_pricesource == "Close"
poki_useOpenClose = poki_pricesource == "Open / Close" or poki_useClose
poki_useTrueRange = input.string("Auto", options=["Always", "Auto", "Never"], title="Use True Range instead of Volume")
poki_isOscillating=input.bool(true, title="Oscillating")
poki_normalize=input.bool(false, title="Normalize")
poki_vol = poki_useTrueRange == "Always" or (poki_useTrueRange == "Auto" and na(volume))? ta.tr(true) : volume
poki_op = poki_useClose ? close : open
poki_hi = poki_useOpenClose ? close >= poki_op ? close : poki_op : high
poki_lo = poki_useOpenClose ? close <= poki_op ? close : poki_op : low

poki_methodvalueCalc = poki_methodvalue
if poki_method == "ATR"
    poki_methodvalueCalc := ta.atr(math.round(poki_methodvalue))
if poki_method == "Part of Price"
    poki_methodvalueCalc := close/poki_methodvalue

var float poki_currclose = na
poki_prevclose = nz(poki_currclose[1])
poki_prevhigh = poki_prevclose + poki_methodvalueCalc
poki_prevlow = poki_prevclose - poki_methodvalueCalc
poki_currclose := poki_hi > poki_prevhigh ? poki_hi : poki_lo < poki_prevlow ? poki_lo : poki_prevclose

var int poki_direction = 0
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
poki_z=input.int(32, title="period")


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

var int poki_sectionLongs = 0
poki_sectionLongs := nz(poki_sectionLongs[1])
var int poki_sectionShorts = 0
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

// Check if your last postion was poki_a long or poki_a short

var int poki_last_longCondition = na
var int poki_last_shortCondition = na
poki_last_longCondition := poki_longCondition ? time : nz(poki_last_longCondition[1])
poki_last_shortCondition := poki_shortCondition ? time : nz(poki_last_shortCondition[1])

poki_in_longCondition = poki_last_longCondition > poki_last_shortCondition
poki_in_shortCondition = poki_last_shortCondition > poki_last_longCondition

// Take profit

poki_isTPl = input.bool(false, "Take Profit Long")
poki_isTPs = input.bool(false, "Take Profit Short")
poki_tp = input.float(2, "Take Profit %")
poki_long_tp = poki_isTPl and ta.crossover(high, (1+(poki_tp/100))*poki_last_open_longCondition) and not poki_longCondition and poki_in_longCondition
poki_short_tp = poki_isTPs and ta.crossunder(low, (1-(poki_tp/100))*poki_last_open_shortCondition) and not poki_shortCondition and poki_in_shortCondition

// Stop Loss

poki_isSLl = input.bool(false, "Stop Loss Long")
poki_isSLs = input.bool(false, "Stop Loss Short")
poki_sl= 0.0
poki_sl := input.float(3, "Stop Loss %")
poki_long_sl = poki_isSLl and ta.crossunder(low, (1-(poki_sl/100))*poki_last_open_longCondition) and not poki_longCondition and poki_in_longCondition
poki_short_sl = poki_isSLs and ta.crossover(high, (1+(poki_sl/100))*poki_last_open_shortCondition) and not poki_shortCondition and poki_in_shortCondition

// Create poki_a single close for all the different closing conditions.

poki_long_close = poki_long_tp or poki_long_sl
poki_short_close = poki_short_tp or poki_short_sl

// Get the time of the last close

var int poki_last_long_close = na
var int poki_last_short_close = na
poki_last_long_close := poki_long_close ? time : nz(poki_last_long_close[1])
poki_last_short_close := poki_short_close ? time : nz(poki_last_short_close[1])

//
poki_showZones   = input.bool(true, title="Show Bullish/Bearish Zones")

// bullish signal rule:
poki_bullishRule =poki_n1>poki_linear_reg
// bearish signal rule:
poki_bearishRule =poki_n1<=poki_linear_reg
// current trading State
poki_ruleState = 0
poki_ruleState := poki_bullishRule ? 1 : poki_bearishRule ? -1 : nz(poki_ruleState[1])
bgcolor(poki_showZones ? (poki_ruleState == 1 ? color.new(color.blue, 90) : poki_ruleState == -1 ? color.new(color.red, 90) : color.new(color.gray, 90)) : na, title=" Bullish/Bearish Zones", force_overlay=true)
// Alerts & Signals

plotshape(poki_longCondition, title="Buy Signal", text="B", style=shape.triangleup, location=location.belowbar, color=color.green, editable=false, force_overlay=true)
plotshape(poki_shortCondition, title="Sell Signal", text="S", style=shape.triangledown, location=location.abovebar, color=color.red, editable=false, force_overlay=true)

poki_ltp = (poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1]) ? (1+(poki_tp/100))*poki_last_open_longCondition : na)
poki_stp = (poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1]) ? (1-(poki_tp/100))*poki_last_open_shortCondition : na)
poki_lsl = (poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1]) ? (1-(poki_sl/100))*poki_last_open_longCondition : na)
poki_ssl = (poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1]) ? (1+(poki_sl/100))*poki_last_open_shortCondition : na)
plotshape(poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1]), text="TP", title="Take Profit Long", style=shape.triangledown, location=location.abovebar, color=color.red, editable=false, force_overlay=true)
plotshape(poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1]), text="TP", title="Take Profit Short", style=shape.triangleup, location=location.belowbar, color=color.lime, editable=false, force_overlay=true)
plot(poki_ltp, style=plot.style_cross, linewidth=3, color=color.white, editable=false, force_overlay=true)
plot(poki_stp, style=plot.style_cross, linewidth=3, color=color.white, editable=false, force_overlay=true)
plotshape(poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1]), text="SL", title="Stop Loss Long", style=shape.triangledown, location=location.abovebar, color=color.red, editable=false, force_overlay=true)
plotshape(poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1]), text="SL", title="Stop Loss Short", style=shape.triangleup, location=location.belowbar, color=color.lime, editable=false, force_overlay=true)
plot(poki_lsl, style=plot.style_cross, linewidth=3, color=color.white, editable=false, force_overlay=true)
plot(poki_ssl, style=plot.style_cross, linewidth=3, color=color.white, editable=false, force_overlay=true)

alertcondition(poki_longCondition, title="Buy Alert")
alertcondition(poki_shortCondition, title="Sell Alert")
alertcondition(poki_long_tp and poki_last_longCondition > nz(poki_last_long_close[1]), title="Take Profit Long")
alertcondition(poki_short_tp and poki_last_shortCondition > nz(poki_last_short_close[1]), title="Take Profit Short")
alertcondition(poki_long_sl and poki_last_longCondition > nz(poki_last_long_close[1]), title="Stop Loss Long")
alertcondition(poki_short_sl and poki_last_shortCondition > nz(poki_last_short_close[1]), title="Stop Loss Short")


// ============================================================================
// ORIGINAL MODULE: Price Action Toolkit Lite [UAlgo]
// ============================================================================
// This Pine Script™ code is subject to the terms of the Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0) https://creativecommons.org/licenses/by-nc-sa/4.0/
// © UAlgo

ualgo_zigzagBool = input.bool(false, 'Show Market Structure', group = 'Price Action Toolkit Settings')
ualgo_zigzagLen = input.int(9, 'ZigZag Length', group = 'Price Action Toolkit Settings')
ualgo_liquidityBool = input.bool(true, 'Show Liquidity Sweeps', group = 'Liquidity Settings')
ualgo_liquidity_len = input.int(30, 'Liquidity Length', minval = 5, group = 'Liquidity Settings')
ualgo_orderblockBool = input.bool(true, 'Show Order Blocks', group = 'Order Block Settings')
ualgo_numberObShow = input.int(2, 'Number of Order Blocks to Show', group = 'Order Block Settings', minval = 1, maxval = 10)
ualgo_showTrendLines = input.bool(true, 'Show Trend Lines', group = 'Misc')
ualgo_trendLineLength = input.int(20, 'Trend Line Detection Sensitivity', group = 'Misc', minval = 10)
ualgo_upTlColor = input.color(color.new(color.teal, 15), title = 'Trend Line Colors', group = 'Misc', inline = 'tl')
ualgo_downTlColor = input.color(color.new(color.red, 15), title = ' ', group = 'Misc', inline = 'tl')
ualgo_upColor = input.color(color.new(color.teal, 15), title = 'Market/Liquidity Colors', group = 'Visual Settings', inline = '1')
ualgo_downColor = input.color(color.new(color.red, 15), title = ' ', group = 'Visual Settings', inline = '1')
ualgo_bearishOrderblockColor = input.color(color.new(color.red, 80), title = 'Order Block Colors ', group = 'Visual Settings', inline = '2')
ualgo_bullishOrderblockColor = input.color(color.new(color.teal, 80), title = ' ', group = 'Visual Settings', inline = '2')
ualgo_hideWatermark = input.bool(false, title = 'Hide Watermark', group = 'Visual Settings')

type ualgo_orderblock
    float value
    int barStart
    int barEnd
    box block
    bool broken

type ualgo_liquidity
    float value
    int barStart
    int barEnd
    line liquidityLine
    bool broken
    label sweep

type ualgo_zone
    float value
    int barStart
    int barEnd
    box block
    string zoneType



var array<ualgo_orderblock> ualgo_bullishOrderblock = array.new<ualgo_orderblock>()
var array<ualgo_orderblock> ualgo_bearishOrderblock = array.new<ualgo_orderblock>()
var array<ualgo_liquidity> ualgo_bullishLiquidity = array.new<ualgo_liquidity>()
var array<ualgo_liquidity> ualgo_bearishLiquidity = array.new<ualgo_liquidity>()
var array<int> ualgo_highValIndex = array.new<int>()
var array<int> ualgo_lowValIndex = array.new<int>()
var array<float> ualgo_highVal = array.new_float()
var array<float> ualgo_lowVal = array.new_float()
var array<label> ualgo_label_ids = array.new<label>()

var bool ualgo_drawUp = false
var bool ualgo_drawDown = false
var string ualgo_lastState = na
var bool ualgo_to_up = false
var bool ualgo_to_down = false
var int ualgo_trend = 1
var float ualgo_fibTopVal = na
var int ualgo_fibTopIndex = na
var float ualgo_fibBotVal = na
var int ualgo_fibBotIndex = na
var float ualgo_premiumVal = na
var float ualgo_discountVal = na
var int ualgo_premiumValIndex = na
var int ualgo_discountValIndex = na
var box ualgo_premiumBox = na
var box ualgo_discountBox = na
var label ualgo_premiumLabel = na
var label ualgo_discountLabel = na
var string ualgo_textOfPremium = na
var string ualgo_textOfDiscount = na
var line ualgo_newBearishTrendline = na
var line ualgo_newBullishTrendline = na

ualgo_atr = ta.atr(14)

ualgo_to_up := high[ualgo_zigzagLen] >= ta.highest(high, ualgo_zigzagLen)
ualgo_to_down := low[ualgo_zigzagLen] <= ta.lowest(low, ualgo_zigzagLen)

ualgo_trend := ualgo_trend == 1 and ualgo_to_down ? -1 : ualgo_trend == -1 and ualgo_to_up ? 1 : ualgo_trend

ualgo_drawZigzag(x1, y1, x2, y2) =>
    line.new(x1 = x1, y1 = y1, x2 = x2, y2 = y2, xloc = xloc.bar_time, width = 1, force_overlay=true)

if ta.change(ualgo_trend) != 0 and ualgo_trend == 1
    array.push(ualgo_highValIndex, time[ualgo_zigzagLen])
    array.push(ualgo_highVal, high[ualgo_zigzagLen])
    if array.size(ualgo_lowVal) > 1
        lastLowVal = array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1)
        lastLowIndex = array.get(ualgo_lowValIndex, array.size(ualgo_lowValIndex) - 1)
        lastHighIndex = array.get(ualgo_highValIndex, array.size(ualgo_highValIndex) - 1)
        lastHighVal = array.get(ualgo_highVal, array.size(ualgo_highVal) - 1)
        if ualgo_zigzagBool
            ualgo_drawZigzag(x1 = lastLowIndex, y1 = lastLowVal, x2 = lastHighIndex, y2 = lastHighVal)
        ualgo_drawUp := false
        ualgo_drawUp
    ualgo_fibTopIndex := time[ualgo_zigzagLen]
    ualgo_fibTopVal := high[ualgo_zigzagLen]
    ualgo_fibTopVal

if ta.change(ualgo_trend) != 0 and ualgo_trend == -1
    array.push(ualgo_lowValIndex, time[ualgo_zigzagLen])
    array.push(ualgo_lowVal, low[ualgo_zigzagLen])
    if array.size(ualgo_highVal) > 1
        lastHighVal = array.get(ualgo_highVal, array.size(ualgo_highVal) - 1)
        lastHighIndex = array.get(ualgo_highValIndex, array.size(ualgo_highValIndex) - 1)
        lastLowIndex = array.get(ualgo_lowValIndex, array.size(ualgo_lowValIndex) - 1)
        lastLowVal = array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1)
        if ualgo_zigzagBool
            ualgo_drawZigzag(x1 = lastHighIndex, y1 = lastHighVal, x2 = lastLowIndex, y2 = lastLowVal)
        ualgo_drawDown := false
        ualgo_drawDown
    ualgo_fibBotIndex := time[ualgo_zigzagLen]
    ualgo_fibBotVal := low[ualgo_zigzagLen]
    ualgo_fibBotVal

if array.size(ualgo_lowVal) > 1 and ualgo_drawDown == false
    if close < array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1)

        line.new(x1 = array.get(ualgo_lowValIndex, array.size(ualgo_lowValIndex) - 1), y1 = array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1), x2 = time, y2 = array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1), width = 1, xloc = xloc.bar_time, force_overlay=true)
        f_limit_labels(ualgo_label_ids, label.new(x = (array.get(ualgo_lowValIndex, array.size(ualgo_lowValIndex) - 1) + time) / 2, y = array.get(ualgo_lowVal, array.size(ualgo_lowVal) - 1), text = na(ualgo_lastState) or ualgo_lastState == 'up' ? 'CHoCH' : 'BoS', xloc = xloc.bar_time, style = label.style_label_up, textcolor = ualgo_downColor, color = color.new(color.white, 100), force_overlay=true), 50)
        ualgo_drawDown := true
        ualgo_lastState := 'down'
        if ualgo_orderblockBool
            ualgo_orderblock newOrderblock = ualgo_orderblock.new()
            float max = 0
            int bar = na
            for i = (time - array.get(ualgo_lowValIndex, array.size(ualgo_lowValIndex) - 1) - (time - time[1])) / (time - time[1]) to 0 by 1
                if high[i] > max
                    max := high[i]
                    bar := time[i]
                    bar
            newOrderblock.barStart := bar
            newOrderblock.barEnd := time
            newOrderblock.broken := false
            newOrderblock.value := max
            newOrderblock.block := box.new(left = newOrderblock.barStart, top = newOrderblock.value - ualgo_atr, right = newOrderblock.barEnd, bottom = newOrderblock.value, xloc = xloc.bar_time, bgcolor = ualgo_bearishOrderblockColor, border_width = 1, border_color = ualgo_bearishOrderblockColor, force_overlay=true)
            array.push(ualgo_bearishOrderblock, newOrderblock)
            if array.size(ualgo_bearishOrderblock) > 20
                array.shift(ualgo_bearishOrderblock)

if array.size(ualgo_highVal) > 1 and ualgo_drawUp == false
    if close > array.get(ualgo_highVal, array.size(ualgo_highVal) - 1)

        line.new(x1 = array.get(ualgo_highValIndex, array.size(ualgo_highValIndex) - 1), y1 = array.get(ualgo_highVal, array.size(ualgo_highVal) - 1), x2 = time, y2 = array.get(ualgo_highVal, array.size(ualgo_highVal) - 1), width = 1, xloc = xloc.bar_time, force_overlay=true)
        f_limit_labels(ualgo_label_ids, label.new(x = (array.get(ualgo_highValIndex, array.size(ualgo_highValIndex) - 1) + time) / 2, y = array.get(ualgo_highVal, array.size(ualgo_highVal) - 1), text = na(ualgo_lastState) or ualgo_lastState == 'down' ? 'CHoCH' : 'BoS', xloc = xloc.bar_time, style = label.style_label_down, textcolor = ualgo_upColor, color = color.new(color.white, 100), force_overlay=true), 50)
        ualgo_drawUp := true
        ualgo_lastState := 'up'
        if ualgo_orderblockBool
            ualgo_orderblock newOrderblock = ualgo_orderblock.new()
            float min = 999999999
            int bar = na
            for i = (time - array.get(ualgo_highValIndex, array.size(ualgo_highValIndex) - 1) - (time - time[1])) / (time - time[1]) to 0 by 1
                if low[i] < min
                    min := low[i]
                    bar := time[i]
                    bar

            newOrderblock.barStart := bar
            newOrderblock.barEnd := time
            newOrderblock.broken := false
            newOrderblock.value := min
            newOrderblock.block := box.new(left = newOrderblock.barStart, top = newOrderblock.value + ualgo_atr, right = newOrderblock.barEnd, bottom = newOrderblock.value, xloc = xloc.bar_time, bgcolor = ualgo_bullishOrderblockColor, border_width = 1, border_color = ualgo_bullishOrderblockColor, force_overlay=true)
            array.push(ualgo_bullishOrderblock, newOrderblock)
            if array.size(ualgo_bullishOrderblock) > 20
                array.shift(ualgo_bullishOrderblock)

// Order block

//update ualgo_orderblock
if array.size(ualgo_bullishOrderblock) > 0
    ualgo_orderblock testOrderblock = na
    int counter = 0
    for i = array.size(ualgo_bullishOrderblock) - 1 to 0 by 1
        testOrderblock := array.get(ualgo_bullishOrderblock, i)
        if counter < ualgo_numberObShow
            testOrderblock.block.set_right(time)
            if close < testOrderblock.value
                testOrderblock.block.delete()
                array.remove(ualgo_bullishOrderblock, i)
            counter := counter + 1
            counter
        else
            testOrderblock.block.set_right(testOrderblock.barStart)

if array.size(ualgo_bearishOrderblock) > 0
    ualgo_orderblock testOrderblock = na
    int counter = 0
    for i = array.size(ualgo_bearishOrderblock) - 1 to 0 by 1
        testOrderblock := array.get(ualgo_bearishOrderblock, i)
        if counter < ualgo_numberObShow
            testOrderblock.block.set_right(time)
            if close > testOrderblock.value
                testOrderblock.block.delete()
                array.remove(ualgo_bearishOrderblock, i)
            counter := counter + 1
            counter
        else
            testOrderblock.block.set_right(testOrderblock.barStart)

// Liquidity

phLiquidity = ta.pivothigh(high, ualgo_liquidity_len, ualgo_liquidity_len)
plLiquidity = ta.pivotlow(low, ualgo_liquidity_len, ualgo_liquidity_len)

if not na(phLiquidity) and ualgo_liquidityBool
    ualgo_liquidity newLiquidity = ualgo_liquidity.new()
    newLiquidity.value := high[ualgo_liquidity_len]
    newLiquidity.barStart := time[ualgo_liquidity_len]
    newLiquidity.barEnd := time
    newLiquidity.broken := false
    newLiquidity.liquidityLine := line.new(x1 = newLiquidity.barStart, y1 = newLiquidity.value, x2 = newLiquidity.barEnd, y2 = newLiquidity.value, xloc = xloc.bar_time, color = ualgo_downColor, width = 1, force_overlay=true)
    array.push(ualgo_bearishLiquidity, newLiquidity)
    if array.size(ualgo_bearishLiquidity) > 7
        deletedLiquidity = array.shift(ualgo_bearishLiquidity)
        deletedLiquidity.liquidityLine.delete()

if not na(plLiquidity) and ualgo_liquidityBool
    ualgo_liquidity newLiquidity = ualgo_liquidity.new()
    newLiquidity.value := low[ualgo_liquidity_len]
    newLiquidity.barStart := time[ualgo_liquidity_len]
    newLiquidity.barEnd := time
    newLiquidity.broken := false
    newLiquidity.liquidityLine := line.new(x1 = newLiquidity.barStart, y1 = newLiquidity.value, x2 = newLiquidity.barEnd, y2 = newLiquidity.value, xloc = xloc.bar_time, color = ualgo_upColor, width = 1, force_overlay=true)
    array.push(ualgo_bullishLiquidity, newLiquidity)
    if array.size(ualgo_bullishLiquidity) > 7
        deletedLiquidity = array.shift(ualgo_bullishLiquidity)
        deletedLiquidity.liquidityLine.delete()

// Update ualgo_liquidity
if array.size(ualgo_bearishLiquidity) > 0
    ualgo_liquidity testLiquidity = na
    for i = array.size(ualgo_bearishLiquidity) - 1 to 0 by 1
        testLiquidity := array.get(ualgo_bearishLiquidity, i)
        if high > testLiquidity.value
            testLiquidity.liquidityLine.set_x2(time)
            testLiquidity.liquidityLine.set_style(line.style_dashed)
            array.remove(ualgo_bearishLiquidity, i)
            if close < testLiquidity.value
                testLiquidity.sweep := f_limit_labels(ualgo_label_ids, label.new(x = time, y = high, text = 'x', xloc = xloc.bar_time, style = label.style_label_down, size = size.normal, textcolor = color.new(color.purple, 0), color = color.new(color.white, 100), force_overlay=true), 50)
                testLiquidity.sweep

        else
            testLiquidity.liquidityLine.set_x2(time)

if array.size(ualgo_bullishLiquidity) > 0
    ualgo_liquidity testLiquidity = na
    for i = array.size(ualgo_bullishLiquidity) - 1 to 0 by 1
        testLiquidity := array.get(ualgo_bullishLiquidity, i)
        if low < testLiquidity.value
            testLiquidity.liquidityLine.set_x2(time)
            testLiquidity.liquidityLine.set_style(line.style_dashed)
            array.remove(ualgo_bullishLiquidity, i)
            if close > testLiquidity.value
                testLiquidity.sweep := f_limit_labels(ualgo_label_ids, label.new(x = time, y = low, text = 'x', xloc = xloc.bar_time, style = label.style_label_up, size = size.normal, textcolor = color.new(color.teal, 0), color = color.new(color.white, 100), force_overlay=true), 50)
                testLiquidity.sweep

        else
            testLiquidity.liquidityLine.set_x2(time)

//Watermark
if not ualgo_hideWatermark
    var tableData = table.new(position = position.top_right, columns = 1, rows = 1, frame_color = color.orange, frame_width = 0, force_overlay=true)
    table.cell(tableData, 0, 0, 'UAlgo', text_color = color.new(color.orange, 0), text_size = size.large)

// Trendlines

ualgo_extendTrendline(lineId, startIndex, startValue, endIndex, endValue) =>
    slope = (endValue - startValue) / (endIndex - startIndex)
    newEndIndex = bar_index
    newEndValue = startValue + slope * (newEndIndex - startIndex)
    line.set_x2(lineId, newEndIndex)
    line.set_y2(lineId, newEndValue)

ualgo_getSlope(startIndex, startValue, endIndex, endValue) =>
    slope = (endValue - startValue) / (endIndex - startIndex)
    slope
if ualgo_showTrendLines
    phTrend = ta.pivothigh(high, ualgo_trendLineLength, ualgo_trendLineLength)
    plTrend = ta.pivotlow(low, ualgo_trendLineLength, ualgo_trendLineLength)

    bullishTrendLineStart = ta.valuewhen(not na(plTrend), bar_index[ualgo_trendLineLength], 1)
    bullishTrendLineEnd = ta.valuewhen(not na(plTrend), bar_index[ualgo_trendLineLength], 0)
    bearishTrendLineStart = ta.valuewhen(not na(phTrend), bar_index[ualgo_trendLineLength], 1)
    bearishTrendLineEnd = ta.valuewhen(not na(phTrend), bar_index[ualgo_trendLineLength], 0)

    bullishTrendLineStartVal = ta.valuewhen(not na(plTrend), low[ualgo_trendLineLength], 1)
    bullishTrendLineEndVal = ta.valuewhen(not na(plTrend), low[ualgo_trendLineLength], 0)
    bearishTrendLineStartVal = ta.valuewhen(not na(phTrend), high[ualgo_trendLineLength], 1)
    bearishTrendLineEndVal = ta.valuewhen(not na(phTrend), high[ualgo_trendLineLength], 0)

    line.delete(ualgo_newBearishTrendline)
    line.delete(ualgo_newBullishTrendline)

    slopeBearish = ualgo_getSlope(bearishTrendLineStart, bearishTrendLineStartVal, bearishTrendLineEnd, bearishTrendLineEndVal)
    slopeBullish = ualgo_getSlope(bullishTrendLineStart, bullishTrendLineStartVal, bullishTrendLineEnd, bullishTrendLineEndVal)

    if slopeBearish < 0
        ualgo_newBearishTrendline := line.new(x1 = bearishTrendLineStart, y1 = bearishTrendLineStartVal, x2 = bar_index, y2 = bearishTrendLineEndVal, xloc = xloc.bar_index, color = ualgo_downTlColor, width = 2, force_overlay=true)
        ualgo_newBearishTrendline
    if slopeBullish > 0
        ualgo_newBullishTrendline := line.new(x1 = bullishTrendLineStart, y1 = bullishTrendLineStartVal, x2 = bar_index, y2 = bullishTrendLineEndVal, xloc = xloc.bar_index, color = ualgo_upTlColor, width = 2, force_overlay=true)
        ualgo_newBullishTrendline

    // Extend the trendlines
    if not na(ualgo_newBearishTrendline)
        ualgo_extendTrendline(ualgo_newBearishTrendline, bearishTrendLineStart, bearishTrendLineStartVal, bearishTrendLineEnd, bearishTrendLineEndVal)

    if not na(ualgo_newBullishTrendline)
        ualgo_extendTrendline(ualgo_newBullishTrendline, bullishTrendLineStart, bullishTrendLineStartVal, bullishTrendLineEnd, bullishTrendLineEndVal)

// ============================================================================
// ORIGINAL MODULE: Smart Money Structure | GainzAlgo
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
smc_atr = na(smc_atr_raw) and bar_index > 0 ? (high - low) : smc_atr_raw
smc_volatility_factor = smc_atr / close
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
var array<label> smc_label_ids = array.new<label>()

[ema1M, vwap1M] = request.security(syminfo.tickerid, "1", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema5M, vwap5M] = request.security(syminfo.tickerid, "5", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema15M, vwap15M] = request.security(syminfo.tickerid, "15", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema30M, vwap30M] = request.security(syminfo.tickerid, "30", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema1H, vwap1H] = request.security(syminfo.tickerid, "60", [ta.ema(close, 20), ta.vwap(hlc3)])
[ema4H, vwap4H] = request.security(syminfo.tickerid, "240", [ta.ema(close, 20), ta.vwap(hlc3)])
[emaD, vwapD] = request.security(syminfo.tickerid, "D", [ta.ema(close, 20), ta.vwap(hlc3)])

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
smc_volShortChange = ta.change(smc_volShort)
smc_volCondition = volume > smc_volAvg50 and smc_volShortChange > 0

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

smc_get_ready_sell = smc_use_momentum_filter ? (smc_price_change < -smc_pre_momentum_threshold and smc_price_change > -smc_momentum_threshold) : true and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_sell_trend_ok and smc_sell_lower_tf_ok and smc_sell_volume_ok and smc_sell_breakout_ok
smc_get_ready_buy = smc_use_momentum_filter ? (smc_price_change > smc_pre_momentum_threshold and smc_price_change < smc_momentum_threshold) : true and (bar_index - smc_last_signal_bar >= smc_min_signal_distance) and smc_buy_trend_ok and smc_buy_lower_tf_ok and smc_buy_volume_ok and smc_buy_breakout_ok

if smc_enable_liquidity_zones
    lookback = 20
    recent_high = ta.highest(high, lookback)
    if high >= recent_high * 0.9995 and high <= recent_high * 1.0005 and bar_index > lookback
        f_limit_labels(smc_label_ids, label.new(bar_index, high, "💧 LIQ", color=color.new(#FF6B35, 80), style=label.style_label_down, textcolor=#FF6B35, size=size.tiny, force_overlay=true), 50)
    recent_low = ta.lowest(low, lookback)
    if low <= recent_low * 1.0005 and low >= recent_low * 0.9995 and bar_index > lookback
        f_limit_labels(smc_label_ids, label.new(bar_index, low, "💧 LIQ", color=color.new(#FF6B35, 80), style=label.style_label_up, textcolor=#FF6B35, size=size.tiny, force_overlay=true), 50)

if smc_enable_market_profile
    var float recent_buy_vol = 0.0
    var float recent_sell_vol = 0.0
    if close > open
        recent_buy_vol := ta.sma(volume, 20)
    else if close < open
        recent_sell_vol := ta.sma(volume, 20)
    vol_ratio = recent_buy_vol > 0 and recent_sell_vol > 0 ? recent_buy_vol / (recent_buy_vol + recent_sell_vol) : 0.5
    strong_buy_flow = vol_ratio > 0.65 and volume > smc_volAvg50 * 1.5
    strong_sell_flow = vol_ratio < 0.35 and volume > smc_volAvg50 * 1.5
    if strong_buy_flow
        f_limit_labels(smc_label_ids, label.new(bar_index, low, "🔥 BUY", color=color.new(#00D9FF, 70), style=label.style_label_up, textcolor=color.white, size=size.small, force_overlay=true), 50)
    if strong_sell_flow
        f_limit_labels(smc_label_ids, label.new(bar_index, high, "🔥 SELL", color=color.new(#FF006E, 70), style=label.style_label_down, textcolor=color.white, size=size.small, force_overlay=true), 50)

if smc_enable_divergence_scanner
    rsi = ta.rsi(close, 14)
    price_lower_low = low < low[5] and low[5] < low[10]
    rsi_higher_low = rsi > rsi[5] and rsi[5] > rsi[10]
    bullish_divergence = price_lower_low and rsi_higher_low and rsi < 40
    price_higher_high = high > high[5] and high[5] > high[10]
    rsi_lower_high = rsi < rsi[5] and rsi[5] < rsi[10]
    bearish_divergence = price_higher_high and rsi_lower_high and rsi > 60
    if bullish_divergence
        f_limit_labels(smc_label_ids, label.new(bar_index, low, "⚡ BULL", color=color.new(#00F5FF, 60), style=label.style_label_up, textcolor=color.white, size=size.small, force_overlay=true), 50)
    if bearish_divergence
        f_limit_labels(smc_label_ids, label.new(bar_index, high, "⚡ BEAR", color=color.new(#C77DFF, 60), style=label.style_label_down, textcolor=color.white, size=size.small, force_overlay=true), 50)

if smc_show_get_ready and smc_get_ready_sell
    f_limit_labels(smc_label_ids, label.new(bar_index, high, "⚠ READY", color=color.new(#FFB627, 70), style=label.style_label_down, textcolor=color.white, size=size.small, force_overlay=true), 50)
if smc_show_get_ready and smc_get_ready_buy
    f_limit_labels(smc_label_ids, label.new(bar_index, low, "⚠ READY", color=color.new(#FFB627, 70), style=label.style_label_up, textcolor=color.white, size=size.small, force_overlay=true), 50)

if smc_sell_condition
    f_limit_labels(smc_label_ids, label.new(bar_index, high, "🔴 SELL", color=color.new(#FF1744, 0), style=label.style_label_down, textcolor=color.white, size=size.normal, force_overlay=true), 50)
    smc_tp_sell_level := low - smc_tp_points
    smc_sl_sell_level := high + smc_sl_points
    smc_last_signal := "Sell"
    smc_last_signal_bar := bar_index
    smc_last_trend := smc_restrict_tf_trend

if smc_buy_condition
    f_limit_labels(smc_label_ids, label.new(bar_index, low, "🟢 BUY", color=color.new(#00E676, 0), style=label.style_label_up, textcolor=color.white, size=size.normal, force_overlay=true), 50)
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

if smc_choch_sell
    line.delete(smc_choch_sell_line)
    box.delete(smc_choch_sell_box)
    smc_choch_sell_level := smc_last_high
    smc_choch_sell_line := line.new(bar_index, smc_choch_sell_level, bar_index + 1, smc_choch_sell_level, color=color.new(#00E5FF, 0), style=line.style_solid, width=2, force_overlay=true)
    smc_choch_sell_box := box.new(bar_index - 2, smc_choch_sell_level * 1.001, bar_index + 5, smc_choch_sell_level * 0.999, bgcolor=color.new(#00E5FF, 95), border_color=color.new(#00E5FF, 80), border_width=1, force_overlay=true)
    f_limit_labels(smc_label_ids, label.new(bar_index, smc_choch_sell_level, "CHoCH", color=color.new(#00E5FF, 85), textcolor=#00E5FF, style=label.style_label_left, size=size.tiny, force_overlay=true), 50)

if smc_choch_buy
    line.delete(smc_choch_buy_line)
    box.delete(smc_choch_buy_box)
    smc_choch_buy_level := smc_last_low
    smc_choch_buy_line := line.new(bar_index, smc_choch_buy_level, bar_index + 1, smc_choch_buy_level, color=color.new(#76FF03, 0), style=line.style_solid, width=2, force_overlay=true)
    smc_choch_buy_box := box.new(bar_index - 2, smc_choch_buy_level * 1.001, bar_index + 5, smc_choch_buy_level * 0.999, bgcolor=color.new(#76FF03, 95), border_color=color.new(#76FF03, 80), border_width=1, force_overlay=true)
    f_limit_labels(smc_label_ids, label.new(bar_index, smc_choch_buy_level, "CHoCH", color=color.new(#76FF03, 85), textcolor=#76FF03, style=label.style_label_left, size=size.tiny, force_overlay=true), 50)

if smc_bos_sell
    line.delete(smc_bos_sell_line)
    box.delete(smc_bos_sell_box)
    smc_bos_sell_level := smc_last_low[1]
    smc_bos_sell_line := line.new(bar_index, smc_bos_sell_level, bar_index + 1, smc_bos_sell_level, color=color.new(#E040FB, 0), style=line.style_solid, width=2, force_overlay=true)
    smc_bos_sell_box := box.new(bar_index - 2, smc_bos_sell_level * 1.001, bar_index + 5, smc_bos_sell_level * 0.999, bgcolor=color.new(#E040FB, 95), border_color=color.new(#E040FB, 80), border_width=1, force_overlay=true)
    f_limit_labels(smc_label_ids, label.new(bar_index, smc_bos_sell_level, "BOS", color=color.new(#E040FB, 85), textcolor=#E040FB, style=label.style_label_left, size=size.tiny, force_overlay=true), 50)

if smc_bos_buy
    line.delete(smc_bos_buy_line)
    box.delete(smc_bos_buy_box)
    smc_bos_buy_level := smc_last_high[1]
    smc_bos_buy_line := line.new(bar_index, smc_bos_buy_level, bar_index + 1, smc_bos_buy_level, color=color.new(#00BFA5, 0), style=line.style_solid, width=2, force_overlay=true)
    smc_bos_buy_box := box.new(bar_index - 2, smc_bos_buy_level * 1.001, bar_index + 5, smc_bos_buy_level * 0.999, bgcolor=color.new(#00BFA5, 95), border_color=color.new(#00BFA5, 80), border_width=1, force_overlay=true)
    f_limit_labels(smc_label_ids, label.new(bar_index, smc_bos_buy_level, "BOS", color=color.new(#00BFA5, 85), textcolor=#00BFA5, style=label.style_label_left, size=size.tiny, force_overlay=true), 50)

var line smc_sup = na
var line smc_res = na

if barstate.islast
    float lowest_y2 = 60000
    int lowest_x2 = 0
    float highest_y2 = 0
    int highest_x2 = 0
    int maxShortBars = math.min(smc_shortTrendPeriod, bar_index)
    for i = 1 to maxShortBars
        if low[i] < lowest_y2
            lowest_y2 := low[i]
            lowest_x2 := i
        if high[i] > highest_y2
            highest_y2 := high[i]
            highest_x2 := i
    float lowest_y1 = 60000
    int lowest_x1 = 0
    float highest_y1 = 0
    int highest_x1 = 0
    int maxLongBars = math.min(smc_longTrendPeriod, bar_index)
    for j = smc_shortTrendPeriod + 1 to maxLongBars
        if low[j] < lowest_y1
            lowest_y1 := low[j]
            lowest_x1 := j
        if high[j] > highest_y1
            highest_y1 := high[j]
            highest_x1 := j
    int trendStrength = smc_trend_strength_raw
    if lowest_x1 > 0 and lowest_x2 > 0
        line.delete(smc_sup)
        smc_sup := line.new(int(bar_index - lowest_x1), lowest_y1, int(bar_index - lowest_x2), lowest_y2, extend=extend.right, style=line.style_solid, width=3, color=trendStrength >= 1 ? (trendStrength >= 4 ? color.new(#00E676, 30) : trendStrength >= 2 ? color.new(#76FF03, 40) : color.new(#FFEB3B, 50)) : color.new(color.gray, 60), force_overlay=true)
    if highest_x1 > 0 and highest_x2 > 0
        line.delete(smc_res)
        smc_res := line.new(int(bar_index - highest_x1), highest_y1, int(bar_index - highest_x2), highest_y2, extend=extend.right, style=line.style_solid, width=3, color=trendStrength <= -1 ? (trendStrength <= -4 ? color.new(#FF1744, 30) : trendStrength <= -2 ? color.new(#E040FB, 40) : color.new(#FFEB3B, 50)) : color.new(color.gray, 60), force_overlay=true)

plot(smc_choch_sell_level, "CHoCH Sell", color=color.new(#00E5FF, 70), style=plot.style_circles, linewidth=1, force_overlay=true)
plot(smc_choch_buy_level, "CHoCH Buy", color=color.new(#76FF03, 70), style=plot.style_circles, linewidth=1, force_overlay=true)
plot(smc_bos_sell_level, "BOS Sell", color=color.new(#E040FB, 70), style=plot.style_circles, linewidth=1, force_overlay=true)
plot(smc_bos_buy_level, "BOS Buy", color=color.new(#00BFA5, 70), style=plot.style_circles, linewidth=1, force_overlay=true)

smc_momentum_5m = request.security(syminfo.tickerid, "5", close - close[3])
smc_momentum_15m = request.security(syminfo.tickerid, "15", close - close[3])
smc_momentum_30m = request.security(syminfo.tickerid, "30", close - close[3])
smc_momentum_1h = request.security(syminfo.tickerid, "60", close - close[3])
smc_momentum_4h = request.security(syminfo.tickerid, "240", close - close[3])
smc_momentum_d = request.security(syminfo.tickerid, "D", close - close[3])

smc_volatility_5m = request.security(syminfo.tickerid, "5", ta.atr(14))
smc_volatility_15m = request.security(syminfo.tickerid, "15", ta.atr(14))
smc_volatility_30m = request.security(syminfo.tickerid, "30", ta.atr(14))
smc_volatility_1h = request.security(syminfo.tickerid, "60", ta.atr(14))
smc_volatility_4h = request.security(syminfo.tickerid, "240", ta.atr(14))
smc_volatility_d = request.security(syminfo.tickerid, "D", ta.atr(14))

smc_volatility_avg_5m = request.security(syminfo.tickerid, "5", ta.sma(ta.atr(14), 20))
smc_volatility_avg_15m = request.security(syminfo.tickerid, "15", ta.sma(ta.atr(14), 20))
smc_volatility_avg_30m = request.security(syminfo.tickerid, "30", ta.sma(ta.atr(14), 20))
smc_volatility_avg_1h = request.security(syminfo.tickerid, "60", ta.sma(ta.atr(14), 20))
smc_volatility_avg_4h = request.security(syminfo.tickerid, "240", ta.sma(ta.atr(14), 20))
smc_volatility_avg_d = request.security(syminfo.tickerid, "D", ta.sma(ta.atr(14), 20))

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

var table smc_trendTable = table.new(position.top_right, columns=2, rows=11, bgcolor=color.new(#0A0E27, 15), border_width=1, border_color=color.new(#00D9FF, 50), force_overlay=true)

if barstate.islast
    table.cell(smc_trendTable, 0, 0, "⚡ SMART MONEY", text_color=color.new(#00F5FF, 0), text_size=size.normal, bgcolor=color.new(#1A1F3A, 30))
    table.cell(smc_trendTable, 1, 0, "v3.0", text_color=color.new(#00D9FF, 40), text_size=size.small, bgcolor=color.new(#1A1F3A, 30))

    table.cell(smc_trendTable, 0, 1, "📊 Strength", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
    strength_color = smc_trend_strength > 50 ? color.new(#00E676, 0) : smc_trend_strength > 0 ? color.new(#76FF03, 0) : smc_trend_strength > -50 ? color.new(#FF6B35, 0) : color.new(#FF1744, 0)
    table.cell(smc_trendTable, 1, 1, str.tostring(math.round(smc_trend_strength)), text_color=strength_color, text_size=size.normal, bgcolor=color.new(#0F1629, 40))

    table.cell(smc_trendTable, 0, 2, "🎯 Confidence", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
    conf_color = smc_system_confidence >= 75 ? color.new(#00E5FF, 0) : smc_system_confidence >= 60 ? color.new(#00D9FF, 20) : color.new(#FFB627, 0)
    table.cell(smc_trendTable, 1, 2, str.tostring(smc_system_confidence) + "%", text_color=conf_color, text_size=size.normal, bgcolor=color.new(#0F1629, 40))

    table.cell(smc_trendTable, 0, 3, "💎 Volume", text_color=color.new(#FFFFFF, 20), text_size=size.small, bgcolor=color.new(#0F1629, 40))
    cvd_display = str.tostring(math.round(smc_raw_cvd / 1000)) + "K"
    table.cell(smc_trendTable, 1, 3, cvd_display, text_color=smc_cvd_color, text_size=size.small, bgcolor=color.new(#0F1629, 40))

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

if smc_enable_trend_analysis
    var table smc_ai_table = table.new(position.bottom_right, columns=7, rows=2, bgcolor=color.new(#0A0E27, 15), border_width=1, border_color=color.new(#E040FB, 50), force_overlay=true)

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

// ============================================================================
// ORIGINAL MODULE: Volumatic Variable Index Dynamic Average [BigBeluga]
// ============================================================================
// This work is licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
// https://creativecommons.org/licenses/by-nc-sa/4.0/
// © BigBeluga



// ＩＮＰＵＴＳ ――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――{
// Input parameters for length, momentum, and vidya_source data
int vidya_vidya_length = input.int(10, 'VIDYA Length') // Length of the VIDYA calculation
int vidya_vidya_momentum = input.int(20, 'VIDYA Momentum') // Momentum length for VIDYA
float vidya_band_distance = input.float(2, 'Distance factor for upper/lower bands', step = 0.1) // Distance factor for upper/lower bands
// Define pivot parameters
int vidya_pivot_left_bars = 3 // Left side pivot bars
int vidya_pivot_right_bars = vidya_pivot_left_bars // Right side pivot bars

float vidya_source = input.source(close, 'Source') // Source for VIDYA calculation

// Define colors for up and down trends
color vidya_up_trend_color = input(#17dfad, '+', group = 'Color', inline = 'c') // Color for uptrend
color vidya_down_trend_color = input(#dd326b, '-', group = 'Color', inline = 'c') // Color for downtrend
bool vidya_shadow = input.bool(true, 'Shadow', group = 'Color', inline = 'c')

// Initialize variables for line, volume, and trend state
var line vidya_pivot_line = na // Variable for storing line references
var float vidya_volume_value = na // Variable for storing volume data
float vidya_smoothed_value = na // Smoothing variable for VIDYA trend levels
var bool vidya_is_trend_up = false // Boolean variable for tracking trend direction

// Initialize arrays for storing line and volume information
var array<line> vidya_liquidity_lines_low = array.new<line>(500) // Array for storing lines for lows
var array<line> vidya_liquidity_lines_high = array.new<line>(500) // Array for storing lines for highs
var float vidya_up_trend_volume = na // Volume accumulated during uptrend
var float vidya_down_trend_volume = na // Volume accumulated during downtrend
// }


// ＦＵＮＣＴＩＯＮＳ―――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――{

// Define VIDYA (Variable Index Dynamic Average) function
vidya_vidya_calc(src, vidya_vidya_length, vidya_vidya_momentum) =>
    float momentum = ta.change(src)
    float sum_pos_momentum = math.sum(momentum >= 0 ? momentum : 0.0, vidya_vidya_momentum)
    float sum_neg_momentum = math.sum(momentum >= 0 ? 0.0 : -momentum, vidya_vidya_momentum)
    float abs_cmo = math.abs(100 * (sum_pos_momentum - sum_neg_momentum) / (sum_pos_momentum + sum_neg_momentum))
    float alpha = 2 / (vidya_vidya_length + 1)
    var float vidya_vidya_value = 0.0
    vidya_vidya_value := alpha * abs_cmo / 100 * src + (1 - alpha * abs_cmo / 100) * nz(vidya_vidya_value[1])

    ta.sma(vidya_vidya_value, 15)

// Method to extend lines and add labels for liquidity levels
method vidya_extend_liquidity_lines(array<line> line_array, float price_level, bool is_cross, volume_val) =>
    if line_array.size() > 0 and last_bar_index - bar_index < 5000
        for i = 0 to line_array.size() - 1 by 1
            if i < line_array.size()
                line liquidity_line = line_array.get(i)
                float current_line_level = line.get_y2(liquidity_line)
                bool price_cross = is_cross ? price_level < current_line_level and price_level[1] >= current_line_level : price_level > current_line_level and price_level[1] <= current_line_level

                bool is_short_line = bar_index - line.get_x1(liquidity_line) < 50

                if price_cross and is_short_line
                    line.set_x2(liquidity_line, bar_index)
                    line_array.remove(i)

                    // Add volume label to the liquidity zone
                    label.new(bar_index - 1, price_level[1], str.tostring(volume_val, format.volume), color = color.rgb(0, 0, 0, 99), style = is_cross ? label.style_label_lower_left : label.style_label_upper_left, textcolor = chart.fg_color, size = size.small, force_overlay=true)

                    // Add a circle label to represent liquidity zone
                    label.new(bar_index - 1, price_level[1], text = '◉', color = #00000003, textcolor = is_cross ? vidya_down_trend_color : vidya_up_trend_color, style = label.style_label_center, size = size.normal, force_overlay=true)
                    // }


// ＣＡＬＣＵＬＡＴＩＯＮＳ――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――{
// Calculate the Average True Range (ATR)
float vidya_atr_value = ta.atr(200) // ATR calculation with length of 200

// Calculate the VIDYA (Variable Index Dynamic Average)
vidya_vidya_value = vidya_vidya_calc(vidya_source, vidya_vidya_length, vidya_vidya_momentum)

// Calculate upper and lower bands based on VIDYA and ATR
float vidya_upper_band = vidya_vidya_value + vidya_atr_value * vidya_band_distance
float vidya_lower_band = vidya_vidya_value - vidya_atr_value * vidya_band_distance

// Detect trend direction using crossovers of vidya_source with bands
if ta.crossover(vidya_source, vidya_upper_band)
    vidya_is_trend_up := true
    vidya_is_trend_up
if ta.crossunder(vidya_source, vidya_lower_band)
    vidya_is_trend_up := false
    vidya_is_trend_up

// Set trend-based smoothing variable
if vidya_is_trend_up
    vidya_smoothed_value := vidya_lower_band
    vidya_smoothed_value
if not vidya_is_trend_up
    vidya_smoothed_value := vidya_upper_band
    vidya_smoothed_value
if ta.change(vidya_is_trend_up)
    vidya_smoothed_value := na
    vidya_smoothed_value

// Calculate pivot highs and lows for price action
bool vidya_pivot_high = not na(ta.pivothigh(vidya_pivot_left_bars, vidya_pivot_right_bars))
bool vidya_pivot_low = not na(ta.pivotlow(close, vidya_pivot_left_bars, vidya_pivot_right_bars))

// Create and store lines for pivot lows (support zones)
if low[vidya_pivot_right_bars] > vidya_smoothed_value and vidya_pivot_low
    vidya_pivot_line := line.new(bar_index[vidya_pivot_right_bars], low[vidya_pivot_right_bars], bar_index[vidya_pivot_right_bars] + 5, low[vidya_pivot_right_bars], color = color.new(vidya_up_trend_color, 50), force_overlay=true)

    vidya_liquidity_lines_low.push(vidya_pivot_line)
    vidya_volume_value := math.sum(volume, vidya_pivot_right_bars + vidya_pivot_left_bars) / (vidya_pivot_right_bars + vidya_pivot_left_bars)
    vidya_volume_value

// Create and store lines for pivot highs (resistance zones)
if high[vidya_pivot_right_bars] < vidya_smoothed_value and vidya_pivot_high
    vidya_pivot_line := line.new(bar_index[vidya_pivot_right_bars], high[vidya_pivot_right_bars], bar_index[vidya_pivot_right_bars] + 5, high[vidya_pivot_right_bars], color = color.new(vidya_down_trend_color, 50), force_overlay=true)

    vidya_liquidity_lines_high.push(vidya_pivot_line)
    vidya_volume_value := math.sum(-volume, vidya_pivot_right_bars + vidya_pivot_left_bars) / (vidya_pivot_right_bars + vidya_pivot_left_bars)
    vidya_volume_value

// Extend lines to track price movements
vidya_liquidity_lines_high.vidya_extend_liquidity_lines(vidya_smoothed_value, true, vidya_volume_value)
vidya_liquidity_lines_low.vidya_extend_liquidity_lines(vidya_smoothed_value, false, vidya_volume_value)

// Detect changes in the trend direction
bool vidya_trend_cross_up = not vidya_is_trend_up[1] and vidya_is_trend_up
bool vidya_trend_cross_down = not vidya_is_trend_up and vidya_is_trend_up[1]

// Reset volume counters when trend changes
if ta.change(vidya_trend_cross_up) or ta.change(vidya_trend_cross_down)
    vidya_up_trend_volume := 0
    vidya_down_trend_volume := 0
    vidya_down_trend_volume

// Accumulate volume during trends
if not(ta.change(vidya_trend_cross_up) or ta.change(vidya_trend_cross_down))
    vidya_up_trend_volume := vidya_up_trend_volume + (close > open ? volume : 0)
    vidya_down_trend_volume := vidya_down_trend_volume + (close < open ? volume : 0)
    vidya_down_trend_volume

// Calculate average volume
float vidya_avg_volume_delta = (vidya_up_trend_volume + vidya_down_trend_volume) / 2

// Determine the color of the trend
color vidya_trend_color = vidya_is_trend_up ? vidya_up_trend_color : not vidya_is_trend_up ? vidya_down_trend_color : chart.fg_color

// Calculate delta volume percentage
string vidya_delta_volume = str.tostring((vidya_up_trend_volume - vidya_down_trend_volume) / vidya_avg_volume_delta * 100, format.percent) == 'NaN%' ? '0%' : str.tostring((vidya_up_trend_volume - vidya_down_trend_volume) / vidya_avg_volume_delta * 100, format.percent)

// }


// ＰＬＯＴ ――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――{
// Display labels for volume and trend statistics on the last bar
if barstate.islast
    label.delete(label.new(bar_index, vidya_smoothed_value, 'Buy: ' + str.tostring(vidya_up_trend_volume, format.volume) + '\n Sell: ' + str.tostring(vidya_down_trend_volume, format.volume) + '\nDelta Volume: ' + vidya_delta_volume, color = color.new(vidya_trend_color, 90), style = vidya_is_trend_up ? label.style_label_upper_left : label.style_label_lower_left, textcolor = chart.fg_color, force_overlay=true)[1])

    label.delete(label.new(bar_index, vidya_smoothed_value, text = '✪', color = #00000003, textcolor = vidya_trend_color, style = label.style_label_center, size = size.large, force_overlay=true)[1])

// Plot the VIDYA trend line
vidya_p1 = plot(vidya_smoothed_value, color = vidya_trend_color, linewidth = 2, style = plot.style_linebr, force_overlay=true)

vidya_p2 = plot(hl2, display=display.none, force_overlay=true)
fill(vidya_p1, vidya_p2, color.new(vidya_trend_color, vidya_shadow ? 80 : 100))
plotshape(series=vidya_trend_cross_up[1] ? vidya_smoothed_value[0] : na, title="Trend Up", style=shape.labelup, location=location.absolute, color=color.new(vidya_up_trend_color, 50), text="▲", textcolor=chart.fg_color, force_overlay=true)
plotshape(series=vidya_trend_cross_down[1] ? vidya_smoothed_value[0] : na, title="Trend Down", style=shape.labeldown, location=location.absolute, color=color.new(vidya_down_trend_color, 50), text="▼", textcolor=chart.fg_color, force_overlay=true)
// }

// ============================================================================