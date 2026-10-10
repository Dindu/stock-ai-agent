const fs = require('node:fs');
const path = require('node:path');

const packageRoot = process.env.PINETS_PROBE_DIR;
if (!packageRoot) throw new Error('Set PINETS_PROBE_DIR to the isolated PineTS installation');
const { PineTS } = require(path.join(packageRoot, 'node_modules/pinets'));
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const baseBars = payload.bars || [];
if (!baseBars.length) throw new Error('No IEX candles supplied');

function aggregateBars(timeframe) {
    if (timeframe === '5' || timeframe === '1') return baseBars;
    const duration = ({ '15': 15, '30': 30, '60': 60, '120': 120, '240': 240 })[timeframe];
    const buckets = new Map();
    for (const bar of baseBars) {
        const bucket = duration
            ? Math.floor(bar.openTime / (duration * 60000)) * duration * 60000
            : new Date(bar.openTime).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
        const key = String(bucket);
        const current = buckets.get(key);
        if (current) {
            current.high = Math.max(current.high, bar.high);
            current.low = Math.min(current.low, bar.low);
            current.close = bar.close;
            current.volume += bar.volume;
            current.closeTime = bar.closeTime;
        } else {
            buckets.set(key, { ...bar, openTime: duration ? bucket : bar.openTime });
        }
    }
    return [...buckets.values()];
}

const symbolInfo = {
    ticker: 'SPY', tickerid: 'NYSE:SPY', main_tickerid: 'NYSE:SPY',
    prefix: 'NYSE', root: 'SPY', description: 'SPDR S&P 500 ETF Trust', type: 'stock',
    timezone: 'America/New_York', session: '0930-1600', currency: 'USD', basecurrency: 'USD',
    mincontract: 1, minmove: 1, mintick: 0.01, pricescale: 100, pointvalue: 1, volumetype: 'shares',
};
const provider = {
    configure() {},
    async getSymbolInfo() { return symbolInfo; },
    async getMarketData(_ticker, timeframe, limit, start, end) {
        let bars = aggregateBars(String(timeframe));
        if (start != null) bars = bars.filter(bar => bar.openTime >= start);
        if (end != null) bars = bars.filter(bar => bar.openTime <= end);
        if (limit) bars = bars.slice(-limit);
        return bars;
    },
};

async function main() {
    const source = fs.readFileSync('scripts/ulti7-with-entries.py', 'utf8')
        + '\nplot(v95_parity_entry_side, "audit_entry_side")'
        + '\nplot(v95_parity_entry_setup, "audit_entry_setup")';
    const pine = new PineTS(provider, 'SPY', '5', baseBars.length);
    pine.setAlertMode('all');
    const originalLog = console.log;
    const originalInfo = console.info;
    console.log = () => {};
    console.info = () => {};
    let context;
    try {
        context = await pine.run(source);
    } finally {
        console.log = originalLog;
        console.info = originalInfo;
    }

    const pinetsEntries = (context.alerts || [])
        .filter(event => event.type === 'alert' && /\| ENTRY @/.test(event.message || ''))
        .map(event => ({
            time: Number(event.time),
            side: /\bCALL\s+\|\s+ENTRY @/.test(event.message) ? 1 : -1,
            message: event.message,
        }));
    const pythonByTime = new Map((payload.pythonEntries || []).map(entry => [Number(entry.time), entry]));
    const pineByTime = new Map(pinetsEntries.map(entry => [entry.time, entry]));
    const times = new Set([...pythonByTime.keys(), ...pineByTime.keys()]);
    const matched = [];
    const pythonOnly = [];
    const pineOnly = [];
    const sideDifferences = [];
    for (const time of [...times].sort((a, b) => a - b)) {
        const python = pythonByTime.get(time);
        const pineEntry = pineByTime.get(time);
        if (python && pineEntry) {
            if (Number(python.side) === pineEntry.side) matched.push({ time, side: pineEntry.side });
            else sideDifferences.push({ time, python: python.side, pinets: pineEntry.side });
        } else if (python) pythonOnly.push(python);
        else pineOnly.push(pineEntry);
    }
    const plotComparisons = {};
    for (const [plotName, field] of [['audit_entry_side', 'side'], ['audit_entry_setup', 'setup']]) {
        const values = context.plots[plotName]?.data;
        if (!values || values.length !== (payload.pythonEvents || []).length) {
            throw new Error(`Missing/incomplete per-bar series: ${plotName}`);
        }
        const differences = [];
        for (let position = 0; position < values.length; position++) {
            const pinetsValue = Number(values[position].value || 0);
            const pythonValue = Number(payload.pythonEvents[position][field] || 0);
            if (pinetsValue !== pythonValue) {
                differences.push({
                    bar: position,
                    time: baseBars[position].openTime,
                    python: pythonValue,
                    pinets: pinetsValue,
                });
            }
        }
        plotComparisons[plotName] = { comparedBars: values.length, mismatches: differences.length, first: differences.slice(0, 8) };
    }
    console.log(JSON.stringify({
        chartBars: context.marketData.length,
        pythonEntryCount: (payload.pythonEntries || []).length,
        pineTsEntryCount: pinetsEntries.length,
        matchedCount: matched.length,
        sideDifferences,
        perBarPlots: plotComparisons,
        pythonOnly,
        pineOnly,
        warnings: (context.warnings || []).slice(0, 10),
        caveats: [
            'IEX 5-minute data only; 15-minute candles are aggregated from those bars.',
            'The script requests 1-minute SMC data; this provider returns 5-minute bars for that unused default 5M-selected context.',
            'PineTS output is not a TradingView reference export; this is not proof of TV parity.',
        ],
    }, null, 2));
    if (pythonOnly.length || pineOnly.length || sideDifferences.length
        || Object.values(plotComparisons).some(result => result.mismatches)) process.exitCode = 1;
}

main().catch(error => { console.error(error.stack); process.exitCode = 1; });