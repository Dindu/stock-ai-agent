//@version=6
indicator("Simple Volume & MA", overlay = false)

// Get volume length input
maLength = input.int(20, title="Volume MA Length")

// Calculate Simple Moving Average of Volume
volMA = ta.sma(volume, maLength)

// Determine bar color based on close vs open
volColor = close >= open ? color.new(color.green, 50) : color.new(color.red, 50)

// Plot Volume as columns and MA as a line
plot(volume, title="Volume", style=plot.style_columns, color=volColor)
plot(volMA, title="Volume MA", color=color.blue, linewidth=2)
