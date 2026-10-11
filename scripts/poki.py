//@version=3
study("poki", shorttitle="poki", overlay=true)
// Parabolic SAR
start = input(3, minval=0, maxval=10, title="Start - Default = 2 - Multiplied by .01")
increment = input(3, minval=0, maxval=10, title="Step Setting (Sensitivity) - Default = 2 - Multiplied by .01" )
maximum = input(3, minval=1, maxval=10, title="Maximum Step (Sensitivity) - Default = 2 - Multiplied by .10")
sus = input(true, "Show Up Trending Parabolic Sar")
sds = input(true, "Show Down Trending Parabolic Sar")
disc = input(false, title="Start and Step settings are *.01 so 2 = .02 etc, Maximum Step is *.10 so 2 = .2")
startCalc = start * .01
incrementCalc = increment * .01
maximumCalc = maximum * .10
sarUp = sar(startCalc, incrementCalc, maximumCalc)
sarDown = sar(startCalc, incrementCalc, maximumCalc)
colUp = close >= sarDown ? blue : na
colDown = close <= sarUp ? red : na
plot(sus and sarUp ? sarUp : na, title="Up Trending SAR", style=circles, linewidth=2,color=colUp)
plot(sds and sarDown ? sarDown : na, title="Up Trending SAR", style=circles, linewidth=2,color=colDown)
// Input
close_price = close[0]
len = input(defval=50, minval=1, title="Linear Regression length")

linear_reg = linreg(close_price, len, 0)
linear_reg_prev = linreg(close[1], len, 0)
slope = ((linear_reg - linear_reg_prev) / interval)



length = input(title="Bollinger Length", type=integer, defval=20, minval=1)
multiplier = input(title="Bollinger Deviation", type=float, defval=2, minval=1)
overbought = input(title="Overbought", type=integer, defval=1, minval=1)
oversold = input(title="Oversold", type=integer, defval=0, minval=1)
custom_timeframe = input(title="Use another Timeframe?", type=bool, defval=false)
highTimeFrame = input(title="Select The Timeframe", type=resolution, defval="60")
res1 = custom_timeframe ? highTimeFrame : period

smabasis = sma(close, length)
stdev = stdev(close, length)
cierre = security(tickerid, res1, close, false)
alta = security(tickerid, res1, high, false)
baja = security(tickerid, res1, low, false)
basis1 = security(tickerid, res1, smabasis, false)
stdevb = security(tickerid, res1, stdev, false)
dev = multiplier * stdevb // stdev(cierre, length)
upper = basis1 + dev
lower = basis1 - dev

bbr = (cierre - lower)/(upper - lower)

// plot(bbr)

// // MARCA LAS RESISTENCIAS
pintarojo = 0.0
pintarojo := nz(pintarojo[1])
pintarojo := bbr[1] > overbought and bbr < overbought ? alta[1] :  nz(pintarojo[1])
p = plot(pintarojo, color = red, style=circles, linewidth=2)

// // MARCA LOS SOPORTES
pintaverde = 0.0
pintaverde := nz(pintaverde[1])
pintaverde := bbr[1] < oversold and bbr > oversold ? baja[1] :  nz(pintaverde[1])
g = plot(pintaverde, color = blue, style=circles, linewidth=2)
//
method = input(defval="ATR", options=["ATR", "Traditional", "Part of Price"], title="Renko Assignment Method")
methodvalue = input(defval=14.0, type=float, minval=0, title="Value")
pricesource = input(defval="Close", options=["Close", "Open / Close", "High / Low"], title="Price Source")
useClose = pricesource == "Close"
useOpenClose = pricesource == "Open / Close" or useClose
useTrueRange = input(defval="Auto", options=["Always", "Auto", "Never"], title="Use True Range instead of Volume")
isOscillating=input(defval=true, type=bool, title="Oscillating")
normalize=input(defval=false, type=bool, title="Normalize")
vol = useTrueRange == "Always" or (useTrueRange == "Auto" and na(volume))? tr : volume
op = useClose ? close : open
hi = useOpenClose ? close >= op ? close : op : high
lo = useOpenClose ? close <= op ? close : op : low

if method == "ATR"
    methodvalue := atr(round(methodvalue))
if method == "Part of Price"
    methodvalue := close/methodvalue

currclose = na
prevclose = nz(currclose[1])
prevhigh = prevclose + methodvalue
prevlow = prevclose - methodvalue
currclose := hi > prevhigh ? hi : lo < prevlow ? lo : prevclose

direction = na
direction := currclose > prevclose ? 1 : currclose < prevclose ? -1 : nz(direction[1])
directionHasChanged = change(direction) != 0
directionIsUp = direction > 0
directionIsDown = direction < 0

barcount = 1
barcount := not directionHasChanged and normalize ? barcount[1] + barcount : barcount
vol := not directionHasChanged ? vol[1] + vol : vol
res = barcount > 1 ? vol/barcount : vol


x=isOscillating and directionIsDown ? -res : res

//
z=input(title="period",type=integer,defval=32)


n2ma=2*wma(close,round(z/2))
nma=wma(close,z)
diff=n2ma-nma
sqn=round(sqrt(z))


n2ma1=2*wma(close[1],round(z/2))
nma1=wma(close[1],z)
diff1=n2ma1-nma1
sqn1=round(sqrt(z))


n1=wma(diff,sqn)
n2=wma(diff1,sqn)
c=n1>n2?green:red



// Conditions

longCond = na
shortCond = na
longCond :=  crossover(x,0) 
shortCond :=  crossunder(x,0) 

// Count your long short conditions for more control with Pyramiding

sectionLongs = 0
sectionLongs := nz(sectionLongs[1])
sectionShorts = 0
sectionShorts := nz(sectionShorts[1])

if longCond
    sectionLongs := sectionLongs + 1
    sectionShorts := 0

if shortCond
    sectionLongs := 0
    sectionShorts := sectionShorts + 1
    
// Pyramiding

pyrl = 1

// These check to see your signal and cross references it against the pyramiding settings above

longCondition = longCond and sectionLongs <= pyrl 
shortCondition = shortCond and sectionShorts <= pyrl 

// Get the price of the last opened long or short

last_open_longCondition = na
last_open_shortCondition = na
last_open_longCondition := longCondition ? open : nz(last_open_longCondition[1])
last_open_shortCondition := shortCondition ? open : nz(last_open_shortCondition[1])

// Check if your last postion was a long or a short

last_longCondition = na
last_shortCondition = na
last_longCondition := longCondition ? time : nz(last_longCondition[1])
last_shortCondition := shortCondition ? time : nz(last_shortCondition[1])

in_longCondition = last_longCondition > last_shortCondition
in_shortCondition = last_shortCondition > last_longCondition

// Take profit

isTPl = input(false, "Take Profit Long")
isTPs = input(false, "Take Profit Short")
tp = input(2, "Take Profit %", type=float)
long_tp = isTPl and crossover(high, (1+(tp/100))*last_open_longCondition) and longCondition == 0 and in_longCondition  == 1 
short_tp = isTPs and crossunder(low, (1-(tp/100))*last_open_shortCondition) and shortCondition == 0 and in_shortCondition == 1 

// Stop Loss

isSLl = input(false, "Stop Loss Long")
isSLs = input(false, "Stop Loss Short")
sl= 0.0
sl := input(3, "Stop Loss %", type=float)
long_sl = isSLl and crossunder(low, (1-(sl/100))*last_open_longCondition) and longCondition == 0 and in_longCondition == 1
short_sl = isSLs and crossover(high, (1+(sl/100))*last_open_shortCondition) and shortCondition == 0 and in_shortCondition == 1

// Create a single close for all the different closing conditions.

long_close = long_tp or long_sl ? 1 : 0
short_close = short_tp or short_sl ? 1 : 0

// Get the time of the last close

last_long_close = na
last_short_close = na
last_long_close := long_close ? time : nz(last_long_close[1])
last_short_close := short_close ? time : nz(last_short_close[1])

//
showZones   = input(true, title="Show Bullish/Bearish Zones")

// bullish signal rule: 
bullishRule =n1>linear_reg
// bearish signal rule: 
bearishRule =n1<=linear_reg
// current trading State
ruleState = 0
ruleState := bullishRule ? 1 : bearishRule ? -1 : nz(ruleState[1])
bgcolor(showZones ? ( ruleState==1 ? blue : ruleState==-1 ? red : gray ) : na , title=" Bullish/Bearish Zones", transp=90)
// Alerts & Signals

bton(b) => b ? 1 : 0
plotshape(longCondition, title = "Buy Signal", text = "B", style=shape.triangleup, location=location.belowbar, color = green, editable = false, transp = 0)
plotshape(shortCondition, title = "Sell Signal", text = "S", style=shape.triangledown, location=location.abovebar, color = red, editable = false, transp = 0)

plotshape(long_tp and last_longCondition > nz(last_long_close[1]), text ="TP", title="Take Profit Long", style=shape.triangledown, 
   location=location.abovebar, color = red, editable = false, transp = 0) 
plotshape(short_tp and last_shortCondition > nz(last_short_close[1]) , text ="TP", title="Take Profit Short", style=shape.triangleup, 
   location=location.belowbar, color = lime, editable = false, transp = 0) 

ltp = iff(long_tp and last_longCondition > nz(last_long_close[1]), (1+(tp/100))*last_open_longCondition, na)
plot(ltp, style=cross, linewidth=3, color = white, editable = false)
stp = iff(short_tp and last_shortCondition > nz(last_short_close[1]), (1-(tp/100))*last_open_shortCondition, na)
plot(stp, style = cross, linewidth=3, color = white, editable = false)

plotshape(long_sl and last_longCondition > nz(last_long_close[1]), text ="SL", title="Stop Loss Long", style=shape.triangledown, 
   location=location.abovebar, color = red, editable = false, transp = 0) 
plotshape(short_sl and last_shortCondition > nz(last_short_close[1]), text ="SL", title="Stop Loss Short", style=shape.triangleup, 
   location=location.belowbar, color = lime, editable = false, transp = 0) 

lsl = iff(long_sl and last_longCondition > nz(last_long_close[1]), (1-(sl/100))*last_open_longCondition, na)
plot(lsl, style=cross, linewidth=3, color = white, editable = false)
ssl = iff(short_sl and last_shortCondition > nz(last_short_close[1]), (1+(sl/100))*last_open_shortCondition, na)
plot(ssl, style = cross, linewidth=3, color = white, editable = false)

alertcondition(bton(longCondition), title="Buy Alert")
alertcondition(bton(shortCondition), title="Sell Alert")
alertcondition(bton(long_tp and last_longCondition > nz(last_long_close[1])), title="Take Profit Long")
alertcondition(bton(short_tp and last_shortCondition > nz(last_short_close[1])), title="Take Profit Short")
alertcondition(bton(long_sl and last_longCondition > nz(last_long_close[1])), title="Stop Loss Long")
alertcondition(bton(short_sl and last_shortCondition > nz(last_short_close[1])), title="Stop Loss Short")

