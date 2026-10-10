const fs = require('node:fs');
const path = require('node:path');

const packageRoot = process.env.PINETS_PROBE_DIR;
if (!packageRoot) throw new Error('Set PINETS_PROBE_DIR to the isolated PineTS installation');
const { PineTS } = require(path.join(packageRoot, 'node_modules/pinets'));
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
if (!payload.bars?.length) throw new Error('No candles supplied');
const source = fs.readFileSync('scripts/ulti7-with-entries.py', 'utf8');
function section(start, end) {
    const startIndex = source.indexOf(start);
    const endIndex = source.indexOf(end, startIndex);
    if (startIndex < 0 || endIndex < 0) throw new Error(`Missing Pine section: ${start}`);
    return source.slice(startIndex, endIndex);
}
const code = [
    '//@version=6',
    'indicator("Offline 5M calculation audit")',
    section('poki_method = input.string', 'poki_z=input.int'),
    section('var bool poki_longCond', '// Get the price of the last opened'),
    "vv_length = 10\nvv_momentum = 20\nvv_source = close\nvv_band_distance = 2\nvar bool vv_is_trend_up = false",
    section('vv_vidya_calc(src, length, momentum) =>', 'method vv_extend_liquidity_lines'),
    section('float vv_atr_value = ta.atr(200)', 'if vv_is_trend_up\n'),
    'vv_trend_cross_up = not vv_is_trend_up[1] and vv_is_trend_up',
    'vv_trend_cross_down = not vv_is_trend_up and vv_is_trend_up[1]',
    'smc_volAvg50 = ta.sma(volume, 50)',
    'smc_volShort = ta.sma(volume, 5)',
    section('smc_volCondition =', 'smc_highestBreakout ='),
    'audit_volume_change = ta.change(smc_volShort)',
    'plot(ta.ema(close, 9), "ema9")',
    'plot(ta.atr(14), "atr14")',
    'plot(ta.rsi(close, 14), "rsi")',
    'plot(volume / ta.sma(volume, 20), "relvol")',
    'plot(smc_volCondition ? 1 : 0, "smc_volume")',
    'plot(volume > smc_volAvg50 and audit_volume_change > 0 ? 1 : 0, "smc_volume_eager")',
    'plot(poki_longCondition ? 1 : 0, "poki_call")',
    'plot(poki_shortCondition ? 1 : 0, "poki_put")',
    'plot(vv_trend_cross_up[1] ? 1 : 0, "vidya_call")',
    'plot(vv_trend_cross_down[1] ? 1 : 0, "vidya_put")',
].join('\n');

async function main() {
    const pine = new PineTS(payload.bars, 'SPY', '5');
    const context = await pine.run(code);
    let mismatches = 0;
    for (const [name, expected] of Object.entries(payload.expected)) {
        const actual = context.plots[name]?.data;
        if (!actual || actual.length !== expected.length) throw new Error(`Missing/truncated series: ${name}`);
        const differences = [];
        for (let position = payload.warmup; position < expected.length; position++) {
            const reference = expected[position];
            const value = actual[position].value;
            if (reference === null && (value == null || !Number.isFinite(value))) continue;
            if (reference === null || !Number.isFinite(value) || Math.abs(value - reference) > 1e-7) {
                differences.push({ bar: position, time: payload.bars[position].openTime, python: reference, pinets: value });
            }
        }
        mismatches += differences.length;
        console.log(JSON.stringify({ series: name, compared: expected.length - payload.warmup, mismatches: differences.length, first: differences.slice(0, 3) }));
    }
    console.log('Scope: selected single-timeframe calculations only; NOT full TradingView alert parity.');
    if (mismatches) process.exitCode = 1;
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });