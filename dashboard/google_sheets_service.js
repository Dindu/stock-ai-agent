// 📊 Google Sheets Service - Replace Database with Google Sheets
// This service handles saving trades, exits, and AI decisions to Google Sheets

const { GoogleSpreadsheet } = require('google-spreadsheet');
const { JWT } = require('google-auth-library');
const { formatCSTTimestamp } = require('./timezone_utils');

const TRADES_HEADER_VALUES = [
    'Trade ID', 'Symbol', 'Entry Price', 'Exit Price', 'Strike', 'Direction',
    'Entry Time', 'Exit Time', 'Exit Reason', 'Status', 'Result', 'AI Generated',
    'AI Grade', 'AI Confidence', 'AI Note', 'Entry Vol Ratio', 'Entry RSI', 'Entry VWAP',
    'Entry EMA5', 'Entry EMA20', 'Entry EMA50', 'Market Condition', 'AI Mode',
    'Options Premium', 'Options Bid', 'Options Ask', 'Options Volume', 'Options Open Interest',
    'Options Expiration', 'Alpaca Order ID', 'P&L', 'P&L %', 'Duration', 'Created At', 'Updated At',
    'Discord Message ID', 'Discord Thread ID',
    // Appended columns (do not insert earlier): legacy shifted-row repair logic depends on stable indices
    'Contracts', 'Contracts Remaining', 'Stop Price', 'Trade Intent', 'Profile'
];

// Load environment variables
if (process.env.NODE_ENV !== 'production') {
    require('dotenv').config({ quiet: true });
}

// Import options price estimator for realistic P&L calculations (commented out if module was removed during cleanup)
// const OptionsPriceEstimator = require('./options_price_estimator');

class GoogleSheetsService {
    constructor() {
        this.doc = null;
        this.tradesSheet = null;
        this.aiDecisionsSheet = null;
        this.marketSnapshotsSheet = null;
        this.systemLogsSheet = null;
        this.botStatusSheet = null;  // For persistent briefing/EOD status tracking (Render.com cron)
        this.aiParamsSheet = null;   // For persistent AI learning params
        this.isInitialized = false;
        // this.optionsEstimator = new OptionsPriceEstimator(); // For realistic P&L calculations (commented out)
        this.optionsEstimator = null; // Disabled - using pure Yahoo Finance API for options data
        console.log('✅ GoogleSheetsService initialized with pure Yahoo Finance API integration (no estimators)');
        // Small in-memory cache/fallback for transient Google API hiccups.
        this._activeTradesCache = { trades: [], fetchedAtMs: 0 };
        this._activeTradesErrorLogAtMs = 0;
    }

    _sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    _isRetryableInitError(error) {
        const msg = (error && error.message) ? String(error.message) : '';
        // Common transient cases seen in Google APIs / google-spreadsheet:
        // - 500 internal errors
        // - 503 backend errors
        // - 429 rate limit
        // - ECONNRESET / ETIMEDOUT network blips
        if (/\[(500|503|429)\]/.test(msg)) return true;
        if (/internal error encountered/i.test(msg)) return true;
        if (/rate limit|quota|exceeded/i.test(msg)) return true;
        if (/ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND/i.test(msg)) return true;
        return false;
    }
    
    _logActiveTradesErrorOncePer(ms, message) {
        const now = Date.now();
        if ((now - this._activeTradesErrorLogAtMs) < ms) return;
        this._activeTradesErrorLogAtMs = now;
        console.error(message);
    }

    _isLegacyShiftedTradesRow(row) {
        // Legacy rows were written before the 'Result' column existed.
        // When the header row was rewritten to include 'Result', those rows became shifted by 1 column from K onward.
        // Signature:
        // - Result looks like boolean (legacy AI Generated)
        // - AI Generated looks like letter grade (legacy AI Grade)
        const boolLike = (v) => {
            const s = String(v || '').trim().toLowerCase();
            return s === 'true' || s === 'false';
        };
        const gradeLike = (v) => {
            const s = String(v || '').trim();
            return /^[A-F][+-]?$/.test(s);
        };

        const numericLike = (v) => {
            const s = String(v || '').trim();
            if (!s) return false;
            // Confidence/score-like values often show up here for shifted rows (e.g., 82.64)
            return /^-?\d+(\.\d+)?%?$/.test(s);
        };

        const status = String(row.get('Status') || '').trim().toLowerCase();
        const statusLooksValid = status === 'active' || status === 'closed' || status === 'partial' || status === 'exited';

        // Primary signature (original schema expectation)
        const sigA = boolLike(row.get('Result')) && gradeLike(row.get('AI Generated'));

        // Alternate signature (seen when the sheet header order drifted and/or Result header was appended elsewhere)
        // AI Generated contains a grade (e.g., B+), while AI Grade contains a numeric confidence/score.
        const sigB = gradeLike(row.get('AI Generated')) && numericLike(row.get('AI Grade'));

        return statusLooksValid && (sigA || sigB);
    }

    _getTradesCell(row, headerName) {
        if (!row) return null;
        if (!headerName) return null;

        if (!this._isLegacyShiftedTradesRow(row)) {
            return row.get(headerName);
        }

        const boolLike = (v) => {
            const s = String(v || '').trim().toLowerCase();
            return s === 'true' || s === 'false';
        };

        const resultIndex = TRADES_HEADER_VALUES.indexOf('Result');
        const idx = TRADES_HEADER_VALUES.indexOf(headerName);
        if (idx === -1 || resultIndex === -1) return row.get(headerName);

        // Legacy rows do not have a real 'Result' column.
        if (idx === resultIndex) return '';

        // IMPORTANT: In observed legacy data, the 1-column shift begins at 'Result'
        // but does NOT reliably apply to the entire tail of the schema.
        // The consistently-shifted block is the AI + entry-indicator block.
        // Keep later Market/Options/P&L fields aligned to avoid corrupting reads.
        const shiftedLegacyHeaders = new Set([
            'AI Grade',
            'AI Confidence',
            'AI Note',
            'Entry Vol Ratio',
            'Entry RSI',
            'Entry VWAP',
            'Entry EMA5',
            'Entry EMA20',
            'Entry EMA50'
        ]);

        // Special handling: in some shifted eras the boolean AI flag was stored in the cell that later became 'Result'.
        // In other eras it was not stored at all (AI Generated column holds grades like B+).
        if (headerName === 'AI Generated') {
            const maybe = row.get('Result');
            if (boolLike(maybe)) return maybe;
            // If the row looks legacy-shifted and we can't find a boolean, default to TRUE (these rows were AI-generated).
            return 'TRUE';
        }

        // For the shifted block only, legacy value lives one column to the left.
        if (idx > resultIndex && shiftedLegacyHeaders.has(headerName)) {
            const prevHeader = TRADES_HEADER_VALUES[idx - 1];
            return row.get(prevHeader);
        }

        // Everything else reads as-is.
        return row.get(headerName);
    }

    async initialize() {
        const MAX_ATTEMPTS = 3;
        const BASE_BACKOFF_MS = 600;

        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                if (this.isInitialized) return true;

            // Validate required environment variables
            if (!process.env.GOOGLE_SHEET_ID) {
                throw new Error('GOOGLE_SHEET_ID is not set in environment variables');
            }
            if (!process.env.GOOGLE_SERVICE_ACCOUNT_EMAIL) {
                throw new Error('GOOGLE_SERVICE_ACCOUNT_EMAIL is not set in environment variables');
            }
            if (!process.env.GOOGLE_PRIVATE_KEY) {
                throw new Error('GOOGLE_PRIVATE_KEY is not set in environment variables');
            }

            // Initialize Google Sheets with service account
            const serviceAccountAuth = new JWT({
                email: process.env.GOOGLE_SERVICE_ACCOUNT_EMAIL,
                key: process.env.GOOGLE_PRIVATE_KEY.replace(/\\n/g, '\n'),
                scopes: ['https://www.googleapis.com/auth/spreadsheets'],
            });

                this.doc = new GoogleSpreadsheet(process.env.GOOGLE_SHEET_ID, serviceAccountAuth);
                await this.doc.loadInfo();

            console.log('📊 Google Sheets connected:', this.doc.title);

                // Get or create sheets
                await this.setupSheets();
                
                this.isInitialized = true;
                return true;
            } catch (error) {
                const retryable = this._isRetryableInitError(error);
                const prefix = attempt < MAX_ATTEMPTS && retryable ? '⚠️' : '❌';
                console.error(`${prefix} Google Sheets initialization failed (attempt ${attempt}/${MAX_ATTEMPTS}):`, error.message);

                if (attempt < MAX_ATTEMPTS && retryable) {
                    const backoff = Math.round(BASE_BACKOFF_MS * Math.pow(2, attempt - 1));
                    await this._sleep(backoff);
                    continue;
                }
                return false;
            }
        }

        return false;
    }

    async setupSheets() {
        // Setup Trades sheet
        const tradesHeaderValues = TRADES_HEADER_VALUES;

        const columnIndexToA1 = (index) => {
            let n = index + 1;
            let s = '';
            while (n > 0) {
                const mod = (n - 1) % 26;
                s = String.fromCharCode(65 + mod) + s;
                n = Math.floor((n - 1) / 26);
            }
            return s;
        };
        
        if (this.doc.sheetsByTitle['Trades']) {
            this.tradesSheet = this.doc.sheetsByTitle['Trades'];
            // Ensure sheet has enough columns for headers
            if (this.tradesSheet.columnCount < tradesHeaderValues.length) {
                await this.tradesSheet.resize({ columnCount: tradesHeaderValues.length });
            }

            // CRITICAL: Never overwrite existing headers.
            // Overwriting the header row can silently remap columns and break reads like row.get('Options Expiration').
            // Instead, only append missing headers to the end.
            try {
                const maxColsToInspect = Math.max(this.tradesSheet.columnCount, tradesHeaderValues.length);
                const endCol = columnIndexToA1(maxColsToInspect - 1);
                await this.tradesSheet.loadCells(`A1:${endCol}1`);

                const existing = [];
                const existingSet = new Set();
                let lastNonEmptyIndex = -1;
                for (let i = 0; i < maxColsToInspect; i++) {
                    const val = this.tradesSheet.getCell(0, i).value;
                    const header = (val == null ? '' : String(val)).trim();
                    existing[i] = header;
                    if (header) {
                        existingSet.add(header);
                        lastNonEmptyIndex = i;
                    }
                }

                const missingHeaders = tradesHeaderValues.filter(h => !existingSet.has(h));
                if (missingHeaders.length > 0) {
                    let appendIndex = lastNonEmptyIndex + 1;
                    const neededCols = appendIndex + missingHeaders.length;
                    if (this.tradesSheet.columnCount < neededCols) {
                        await this.tradesSheet.resize({ columnCount: neededCols });
                        const newEndCol = columnIndexToA1(neededCols - 1);
                        await this.tradesSheet.loadCells(`A1:${newEndCol}1`);
                    }

                    for (let j = 0; j < missingHeaders.length; j++) {
                        this.tradesSheet.getCell(0, appendIndex + j).value = missingHeaders[j];
                    }
                    await this.tradesSheet.saveUpdatedCells();
                    console.log(`📄 Trades headers: appended ${missingHeaders.length} missing column(s): ${missingHeaders.join(', ')}`);
                }

                // Freeze first row (safe; does not affect mapping)
                await this.tradesSheet.updateProperties({
                    gridProperties: { frozenRowCount: 1 }
                });
            } catch (error) {
                console.log('⚠️ Header setup error for Trades sheet:', error.message);
            }
            
        } else {
            this.tradesSheet = await this.doc.addSheet({
                title: 'Trades',
                headerValues: tradesHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
        }

        // Setup AI Decisions sheet
        const aiDecisionsHeaderValues = [
            'Symbol', 'Decision', 'Confidence', 'Grade', 'Reasoning', 
            'Market Data', 'Created At'
        ];
        
        if (this.doc.sheetsByTitle['AI_Decisions']) {
            this.aiDecisionsSheet = this.doc.sheetsByTitle['AI_Decisions'];
            if (this.aiDecisionsSheet.columnCount < aiDecisionsHeaderValues.length) {
                await this.aiDecisionsSheet.resize({ columnCount: aiDecisionsHeaderValues.length });
            }
            
            try {
                await this.aiDecisionsSheet.loadCells('A1:G1');
                for (let i = 0; i < aiDecisionsHeaderValues.length; i++) {
                    this.aiDecisionsSheet.getCell(0, i).value = aiDecisionsHeaderValues[i];
                }
                await this.aiDecisionsSheet.saveUpdatedCells();
                await this.aiDecisionsSheet.updateProperties({
                    gridProperties: { frozenRowCount: 1 }
                });
            } catch (error) {
                console.log('⚠️ Header setup error for AI Decisions sheet:', error.message);
            }
            
        } else {
            this.aiDecisionsSheet = await this.doc.addSheet({
                title: 'AI_Decisions',
                headerValues: aiDecisionsHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
        }

        // Setup Market Snapshots sheet
        const marketSnapshotsHeaderValues = [
            'Symbol', 'Price', 'Volume', 'Volume Ratio', 'RSI', 'VWAP',
            'EMA5', 'EMA20', 'EMA50', 'Market Cap', 'Avg Volume', 'Created At'
        ];
        
        if (this.doc.sheetsByTitle['Market_Snapshots']) {
            this.marketSnapshotsSheet = this.doc.sheetsByTitle['Market_Snapshots'];
            if (this.marketSnapshotsSheet.columnCount < marketSnapshotsHeaderValues.length) {
                await this.marketSnapshotsSheet.resize({ columnCount: marketSnapshotsHeaderValues.length });
            }
            
            try {
                await this.marketSnapshotsSheet.loadCells('A1:L1');
                for (let i = 0; i < marketSnapshotsHeaderValues.length; i++) {
                    this.marketSnapshotsSheet.getCell(0, i).value = marketSnapshotsHeaderValues[i];
                }
                await this.marketSnapshotsSheet.saveUpdatedCells();
                await this.marketSnapshotsSheet.updateProperties({
                    gridProperties: { frozenRowCount: 1 }
                });
            } catch (error) {
                console.log('⚠️ Header setup error for Market Snapshots sheet:', error.message);
            }
            
        } else {
            this.marketSnapshotsSheet = await this.doc.addSheet({
                title: 'Market_Snapshots',
                headerValues: marketSnapshotsHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
        }

        // Setup System Logs sheet
        const systemLogsHeaderValues = ['Level', 'Message', 'Context', 'Created At'];
        
        if (this.doc.sheetsByTitle['System_Logs']) {
            this.systemLogsSheet = this.doc.sheetsByTitle['System_Logs'];
            if (this.systemLogsSheet.columnCount < systemLogsHeaderValues.length) {
                await this.systemLogsSheet.resize({ columnCount: systemLogsHeaderValues.length });
            }
            
            try {
                await this.systemLogsSheet.loadCells('A1:D1');
                for (let i = 0; i < systemLogsHeaderValues.length; i++) {
                    this.systemLogsSheet.getCell(0, i).value = systemLogsHeaderValues[i];
                }
                await this.systemLogsSheet.saveUpdatedCells();
                await this.systemLogsSheet.updateProperties({
                    gridProperties: { frozenRowCount: 1 }
                });
            } catch (error) {
                console.log('⚠️ Header setup error for System Logs sheet:', error.message);
            }
            
        } else {
            this.systemLogsSheet = await this.doc.addSheet({
                title: 'System_Logs',
                headerValues: systemLogsHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
        }

        // Setup Bot_Status sheet for persistent briefing/EOD/status tracking (Render.com cron compatibility)
        const botStatusHeaderValues = ['Key', 'Value', 'Updated At'];
        
        if (this.doc.sheetsByTitle['Bot_Status']) {
            this.botStatusSheet = this.doc.sheetsByTitle['Bot_Status'];
            if (this.botStatusSheet.columnCount < botStatusHeaderValues.length) {
                await this.botStatusSheet.resize({ columnCount: botStatusHeaderValues.length });
            }
            
            try {
                await this.botStatusSheet.loadCells('A1:C1');
                for (let i = 0; i < botStatusHeaderValues.length; i++) {
                    this.botStatusSheet.getCell(0, i).value = botStatusHeaderValues[i];
                }
                await this.botStatusSheet.saveUpdatedCells();
                await this.botStatusSheet.updateProperties({
                    gridProperties: { frozenRowCount: 1 }
                });
            } catch (error) {
                console.log('⚠️ Header setup error for Bot_Status sheet:', error.message);
            }
            
        } else {
            this.botStatusSheet = await this.doc.addSheet({
                title: 'Bot_Status',
                headerValues: botStatusHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
            console.log('📊 Created Bot_Status sheet for persistent status tracking');
        }

        // Setup AI Params sheet
        const aiParamsHeaderValues = ['Key', 'Value', 'Updated At'];
        if (this.doc.sheetsByTitle['AI_Params']) {
            this.aiParamsSheet = this.doc.sheetsByTitle['AI_Params'];
        } else {
            this.aiParamsSheet = await this.doc.addSheet({
                title: 'AI_Params',
                headerValues: aiParamsHeaderValues,
                gridProperties: { frozenRowCount: 1 }
            });
            console.log('📊 Created AI_Params sheet for persistent AI learning parameters');
        }

        console.log('📊 All Google Sheets initialized successfully');
    }

    async getAILearnedParams() {
        if (!this.aiParamsSheet) return null;
        try {
            const rows = await this.aiParamsSheet.getRows();
            if (!rows || rows.length === 0) return null;
            const params = {};
            for (const row of rows) {
                const key = row.get('Key');
                const val = row.get('Value');
                if (key && val !== undefined) {
                    params[key] = val;
                }
            }
            return Object.keys(params).length > 0 ? params : null;
        } catch (error) {
            console.error('⚠️ Failed to load AI Params from Sheets:', error.message);
            return null;
        }
    }

    async saveAILearnedParams(params) {
        if (!this.aiParamsSheet) return;
        try {
            await this.aiParamsSheet.clearRows();
            const rowsToAdd = [];
            const updatedAt = new Date().toISOString();
            
            // Expected params structure: { params: { volRatio, atrMin, ... }, lastTuneTime: ... }
            if (params.params) {
                for (const [key, value] of Object.entries(params.params)) {
                    rowsToAdd.push({ 'Key': key, 'Value': value, 'Updated At': updatedAt });
                }
            }
            if (params.lastTuneTime) {
                rowsToAdd.push({ 'Key': 'lastTuneTime', 'Value': params.lastTuneTime, 'Updated At': updatedAt });
            }
            
            if (rowsToAdd.length > 0) {
                await this.aiParamsSheet.addRows(rowsToAdd);
            }
        } catch (error) {
            console.error('⚠️ Failed to save AI Params to Sheets:', error.message);
        }
    }

    async saveTrade(tradeData) {
        try {
            // PRODUCTION SAFEGUARD: Block test records from being saved
            if (tradeData.tradeId && (
                tradeData.tradeId.startsWith('TEST_') || 
                tradeData.tradeId.startsWith('RESET_TEST_') ||
                tradeData.tradeId.includes('test') ||
                tradeData.tradeId.includes('TEST') ||
                tradeData.symbol === 'TEST'
            )) {
                console.log(`⚠️ PRODUCTION SAFEGUARD: Blocked test record: ${tradeData.tradeId}`);
                return false;
            }
            
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) {
                    console.error('❌ Google Sheets not initialized, cannot save trade');
                    return false;
                }
            }

            const row = {
                'Trade ID': tradeData.tradeId,
                'Symbol': tradeData.symbol,
                'Entry Price': tradeData.entryPrice,
                'Exit Price': tradeData.exitPrice || '',
                'Strike': tradeData.strike,
                'Direction': tradeData.direction,
                'Contracts': (tradeData.contracts ?? ''),
                'Contracts Remaining': (tradeData.contractsRemaining ?? tradeData.contracts ?? ''),
                'Stop Price': (tradeData.stopPrice ?? ''),
                'Trade Intent': (tradeData.tradeIntent ?? ''),
                'Profile': (tradeData.profile ?? ''),
                'Entry Time': tradeData.entryTime,
                'Exit Time': tradeData.exitTime || '',
                'Exit Reason': tradeData.exitReason || '',
                'Status': tradeData.status,
                'AI Generated': tradeData.aiGenerated,
                'AI Grade': tradeData.aiGrade || '',
                'AI Confidence': tradeData.aiConfidence || '',
                'AI Note': tradeData.aiNote || '',
                'Entry Vol Ratio': tradeData.entryVolRatio || '',
                'Entry RSI': tradeData.entryRSI || '',
                'Entry VWAP': tradeData.entryVWAP || '',
                'Entry EMA5': tradeData.entryEMA5 || '',
                'Entry EMA20': tradeData.entryEMA20 || '',
                'Entry EMA50': tradeData.entryEMA50 || '',
                'Market Condition': tradeData.marketCondition || '',
                'AI Mode': tradeData.aiMode || '',
                'Options Premium': tradeData.optionsPremium || tradeData.optionsPrice || '',
                'Options Bid': tradeData.optionsBid || '',
                'Options Ask': tradeData.optionsAsk || '',
                'Options Volume': tradeData.optionsVolume || '',
                'Options Open Interest': tradeData.optionsOpenInterest || '',
                'Options Expiration': (() => {
                    // CRITICAL: Store expiration date in MM/DD/YYYY format WITHOUT timezone conversion
                    // API returns YYYY-MM-DD string → convert to MM/DD/YYYY for Sheets
                    const exp = tradeData.optionsExpiration;
                    if (!exp) return '';
                    
                    // Expected format: "YYYY-MM-DD" string from Market Data API
                    if (typeof exp === 'string') {
                        const match = exp.match(/^(\d{4})-(\d{2})-(\d{2})/);
                        if (match) {
                            const [_, year, month, day] = match;
                            // Direct string manipulation - NO Date objects = NO timezone issues
                            return `${month}/${day}/${year}`;
                        }
                        console.warn(`⚠️ Unexpected expiration format: ${exp}`);
                    }
                    
                    // Should never reach here with current data flow
                    console.error(`❌ Expiration is not a string in YYYY-MM-DD format:`, exp);
                    return '';
                })(),
                'Alpaca Order ID': tradeData.alpacaOrderId || '',
                'P&L': await this.calculatePnL(tradeData),
                'P&L %': await this.calculatePnLPercent(tradeData),
                'Duration': this.calculateDuration(tradeData),
                'Created At': formatCSTTimestamp(),
                'Updated At': formatCSTTimestamp(),
                'Discord Message ID': tradeData.discordMessageId || '',
                'Discord Thread ID': tradeData.discordThreadId || ''
            };

            await this.tradesSheet.addRow(row);
            console.log(`📊 Trade saved to Google Sheets: ${tradeData.tradeId}`);
            return true;
        } catch (error) {
            console.error('❌ Failed to save trade to Google Sheets:', error.message);
            return false;
        }
    }

    async updateTrade(tradeId, updateData) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            await this.tradesSheet.loadCells();
            const rows = await this.tradesSheet.getRows();
            
            const tradeRow = rows.find(row => row.get('Trade ID') === tradeId);
            if (!tradeRow) {
                console.log(`⚠️ Trade ${tradeId} not found in Google Sheets for update`);
                return false;
            }

            // Update fields with logging
            if (updateData.exitPrice) {
                tradeRow.set('Exit Price', updateData.exitPrice);
                console.log(`   ↳ Setting Exit Price: ${updateData.exitPrice}`);
            }
            if (updateData.entryPrice !== undefined && updateData.entryPrice !== null) {
                tradeRow.set('Entry Price', updateData.entryPrice);
                console.log(`   ↳ Setting Entry Price: ${updateData.entryPrice}`);
            }
            if (updateData.exitTime) {
                tradeRow.set('Exit Time', updateData.exitTime);
                console.log(`   ↳ Setting Exit Time: ${updateData.exitTime}`);
            }
            if (updateData.exitReason) {
                tradeRow.set('Exit Reason', updateData.exitReason);
                console.log(`   ↳ Setting Exit Reason: ${updateData.exitReason}`);
            }
            if (updateData.status) {
                tradeRow.set('Status', updateData.status);
                console.log(`   ↳ Setting Status: ${updateData.status}`);
            }

            if (updateData.alpacaOrderId) {
                tradeRow.set('Alpaca Order ID', String(updateData.alpacaOrderId));
                console.log(`   ↳ Setting Alpaca Order ID: ${updateData.alpacaOrderId}`);
            }

            // Position sizing / state fields (used by v2 engine for broker-close sizing after reloads)
            if (updateData.contracts !== undefined && updateData.contracts !== null) {
                tradeRow.set('Contracts', updateData.contracts);
                console.log(`   ↳ Setting Contracts: ${updateData.contracts}`);
            }
            if (updateData.contractsRemaining !== undefined && updateData.contractsRemaining !== null) {
                tradeRow.set('Contracts Remaining', updateData.contractsRemaining);
                console.log(`   ↳ Setting Contracts Remaining: ${updateData.contractsRemaining}`);
            }
            if (updateData.stopPrice !== undefined && updateData.stopPrice !== null) {
                tradeRow.set('Stop Price', updateData.stopPrice);
                console.log(`   ↳ Setting Stop Price: ${updateData.stopPrice}`);
            }
            if (updateData.tradeIntent) {
                tradeRow.set('Trade Intent', updateData.tradeIntent);
                console.log(`   ↳ Setting Trade Intent: ${updateData.tradeIntent}`);
            }
            if (updateData.profile) {
                tradeRow.set('Profile', updateData.profile);
                console.log(`   ↳ Setting Profile: ${updateData.profile}`);
            }

            // Discord live-trades linkage (for thread/reply continuity across restarts)
            if (updateData.discordMessageId) {
                tradeRow.set('Discord Message ID', String(updateData.discordMessageId));
                console.log(`   ↳ Setting Discord Message ID: ${updateData.discordMessageId}`);
            }
            if (updateData.discordThreadId) {
                tradeRow.set('Discord Thread ID', String(updateData.discordThreadId));
                console.log(`   ↳ Setting Discord Thread ID: ${updateData.discordThreadId}`);
            }

            
            // Recalculate P&L and duration.
            // CRITICAL: Do NOT wipe out existing P&L when updateTrade is called for unrelated updates
            // (e.g., Discord message/thread ID updates) without exitPrice/exitTime.
            const existingEntryPrice = tradeRow.get('Entry Price');
            const existingExitPrice = tradeRow.get('Exit Price');
            const effectiveExitPrice = (updateData.exitPrice != null && String(updateData.exitPrice).trim() !== '')
                ? updateData.exitPrice
                : existingExitPrice;

            const existingExitTime = tradeRow.get('Exit Time');
            const effectiveExitTime = (updateData.exitTime != null && String(updateData.exitTime).trim() !== '')
                ? updateData.exitTime
                : existingExitTime;

            const tradeData = {
                entryPrice: (updateData.entryPrice != null) ? parseFloat(updateData.entryPrice) : parseFloat(existingEntryPrice),
                exitPrice: effectiveExitPrice != null && String(effectiveExitPrice).trim() !== '' ? parseFloat(effectiveExitPrice) : null,
                entryTime: tradeRow.get('Entry Time'),
                exitTime: effectiveExitTime || null,
                direction: tradeRow.get('Direction'),
                symbol: tradeRow.get('Symbol'),
                strike: tradeRow.get('Strike')
            };

            const pnl = await this.calculatePnL(tradeData);
            const pnlPercent = await this.calculatePnLPercent(tradeData);
            const duration = this.calculateDuration(tradeData);

            if (pnl !== '') tradeRow.set('P&L', pnl);
            if (pnlPercent !== '') tradeRow.set('P&L %', pnlPercent);
            if (duration !== '') tradeRow.set('Duration', duration);
            
            tradeRow.set('Updated At', formatCSTTimestamp());

            await tradeRow.save();
            console.log(`📊 Trade updated in Google Sheets: ${tradeId}`);
            return true;
        } catch (error) {
            console.error('❌ Failed to update trade in Google Sheets:', error.message);
            return false;
        }
    }

    async saveAIDecision(decisionData) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const row = {
                'Symbol': decisionData.symbol,
                'Decision': decisionData.decision,
                'Confidence': decisionData.confidence,
                'Grade': decisionData.grade,
                'Reasoning': decisionData.reasoning || '',
                'Market Data': JSON.stringify(decisionData.marketData || {}),
                'Created At': new Date().toISOString()
            };

            await this.aiDecisionsSheet.addRow(row);
            console.log(`📊 AI Decision saved to Google Sheets: ${decisionData.symbol} - ${decisionData.decision}`);
            return true;
        } catch (error) {
            console.error('❌ Failed to save AI decision to Google Sheets:', error.message);
            return false;
        }
    }

    async saveMarketSnapshot(snapshotData) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const row = {
                'Symbol': snapshotData.symbol,
                'Price': snapshotData.price,
                'Volume': snapshotData.volume || '',
                'Volume Ratio': snapshotData.volumeRatio || '',
                'RSI': snapshotData.rsi || '',
                'VWAP': snapshotData.vwap || '',
                'EMA5': snapshotData.ema5 || '',
                'EMA20': snapshotData.ema20 || '',
                'EMA50': snapshotData.ema50 || '',
                'Market Cap': snapshotData.marketCap || '',
                'Avg Volume': snapshotData.avgVolume || '',
                'Created At': new Date().toISOString()
            };

            await this.marketSnapshotsSheet.addRow(row);
            return true;
        } catch (error) {
            console.error('❌ Failed to save market snapshot to Google Sheets:', error.message);
            return false;
        }
    }

    async logSystem(level, message, context = {}) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const row = {
                'Level': level,
                'Message': message,
                'Context': JSON.stringify(context),
                'Created At': new Date().toISOString()
            };

            await this.systemLogsSheet.addRow(row);
            return true;
        } catch (error) {
            console.error('❌ Failed to save system log to Google Sheets:', error.message);
            return false;
        }
    }

    async clearAllTestRecords() {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const rows = await this.tradesSheet.getRows();
            const testRows = rows.filter(row => {
                const tradeId = row.get('Trade ID');
                const symbol = row.get('Symbol');
                return tradeId && (
                    tradeId.startsWith('TEST_') || 
                    tradeId.startsWith('RESET_TEST_') ||
                    symbol === 'TEST' ||
                    tradeId.includes('test') ||
                    tradeId.includes('TEST')
                );
            });

            console.log(`🗑️ Found ${testRows.length} test rows to physically delete`);
            
            for (const row of testRows) {
                const tradeId = row.get('Trade ID');
                try {
                    await row.delete();
                    console.log(`   ✅ Deleted: ${tradeId}`);
                } catch (deleteError) {
                    console.log(`   ❌ Failed to delete: ${tradeId} - ${deleteError.message}`);
                }
            }

            return testRows.length;
        } catch (error) {
            console.error('❌ Failed to clear test records:', error.message);
            return 0;
        }
    }

    async clearAllTrades() {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const rows = await this.tradesSheet.getRows();
            console.log(`🗑️ Found ${rows.length} total rows to physically delete`);
            
            for (const row of rows) {
                const tradeId = row.get('Trade ID');
                try {
                    await row.delete();
                    console.log(`   ✅ Deleted: ${tradeId}`);
                } catch (deleteError) {
                    console.log(`   ❌ Failed to delete: ${tradeId} - ${deleteError.message}`);
                }
            }

            console.log(`🧹 Cleared all ${rows.length} trades from Google Sheets`);
            return rows.length;
        } catch (error) {
            console.error('❌ Failed to clear all trades:', error.message);
            return 0;
        }
    }

    async deleteTradeRow(tradeId) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return false;
            }

            const rows = await this.tradesSheet.getRows();
            const targetRow = rows.find(row => row.get('Trade ID') === tradeId);
            
            if (targetRow) {
                await targetRow.delete();
                console.log(`✅ Physically deleted trade row: ${tradeId}`);
                return true;
            } else {
                console.log(`⚠️ Trade row not found: ${tradeId}`);
                return false;
            }
        } catch (error) {
            console.error(`❌ Failed to delete trade row ${tradeId}:`, error.message);
            return false;
        }
    }

    async getTradeEntryTime(tradeId) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return null;
            }

            const rows = await this.tradesSheet.getRows();
            const tradeRow = rows.find(row => row.get('Trade ID') === tradeId);
            
            if (tradeRow) {
                return tradeRow.get('Entry Time');
            } else {
                console.log(`⚠️ Trade ${tradeId} not found in Google Sheets`);
                return null;
            }
        } catch (error) {
            console.error(`❌ Failed to get entry time for trade ${tradeId}:`, error.message);
            return null;
        }
    }

    async calculatePnL(tradeData) {
        if (!tradeData.exitPrice || !tradeData.entryPrice) return '';
        
        try {
            // Try to use realistic options pricing if we have the required data and estimator is available
            if (this.optionsEstimator && tradeData.symbol && tradeData.strike && tradeData.direction && tradeData.entryTime) {
                const optionsResult = await this.optionsEstimator.calculateOptionsPN(
                    tradeData, 
                    tradeData.exitPrice, 
                    tradeData.exitTime
                );
                return optionsResult.dollarPnL.toFixed(2);
            }
        } catch (error) {
            console.log(`⚠️ Options P&L calculation failed, using stock-based: ${error.message}`);
        }
        
        // Options P&L calculation: You BUY to open, SELL to close
        // Profit = Exit Premium - Entry Premium (regardless of call/put direction)
        const entry = parseFloat(tradeData.entryPrice);
        const exit = parseFloat(tradeData.exitPrice);
        
        // Simple: exit - entry (premium went up = profit, down = loss)
        return (exit - entry).toFixed(2);
    }

    async calculatePnLPercent(tradeData) {
        if (!tradeData.exitPrice || !tradeData.entryPrice) return '';
        
        try {
            // Try to use realistic options pricing if we have the required data and estimator is available
            if (this.optionsEstimator && tradeData.symbol && tradeData.strike && tradeData.direction && tradeData.entryTime) {
                const optionsResult = await this.optionsEstimator.calculateOptionsPN(
                    tradeData, 
                    tradeData.exitPrice, 
                    tradeData.exitTime
                );
                return optionsResult.percentPnL.toFixed(1) + '%';
            }
        } catch (error) {
            console.log(`⚠️ Options P&L% calculation failed, using stock-based: ${error.message}`);
        }
        
        // Options P&L calculation: You BUY to open, SELL to close
        // Profit = (Exit Premium - Entry Premium) / Entry Premium * 100
        // This is the same for both calls and puts - you profit when premium goes UP
        const entry = parseFloat(tradeData.entryPrice);
        const exit = parseFloat(tradeData.exitPrice);
        
        const pnl = exit - entry;  // Premium increase = profit
        const pnlPercent = (pnl / entry) * 100;
        
        return pnlPercent.toFixed(2) + '%';
    }

    calculateDuration(tradeData) {
        if (!tradeData.exitTime || !tradeData.entryTime) return '';

        const parseDate = (v) => {
            if (!v) return null;
            if (v instanceof Date) return Number.isFinite(v.getTime()) ? v : null;
            const s = String(v).trim();
            if (!s) return null;

            // ISO timestamps (e.g., 2026-03-03T17:03:34.435Z)
            if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
                const d = new Date(s);
                return Number.isFinite(d.getTime()) ? d : null;
            }

            // Google Sheets CST-ish timestamps (e.g., 03/03/2026, 11:25:48)
            // Best-effort parse; if parsing fails, return null.
            const d = new Date(s);
            return Number.isFinite(d.getTime()) ? d : null;
        };

        const entryTime = parseDate(tradeData.entryTime);
        const exitTime = parseDate(tradeData.exitTime);
        if (!entryTime || !exitTime) return '';

        const duration = exitTime - entryTime;
        if (!Number.isFinite(duration) || duration < 0) return '';
        
        const minutes = Math.floor(duration / (1000 * 60));
        const hours = Math.floor(minutes / 60);
        const remainingMinutes = minutes % 60;
        
        if (hours > 0) {
            return `${hours}h ${remainingMinutes}m`;
        } else {
            return `${remainingMinutes}m`;
        }
    }

    async getActiveTrades() {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return [];
            }

            // Retry transient Google errors (500/503/429/network blips)
            const maxAttempts = 3;
            let rows = null;
            let lastError = null;

            for (let attempt = 1; attempt <= maxAttempts; attempt++) {
                try {
                    rows = await this.tradesSheet.getRows();
                    lastError = null;
                    break;
                } catch (err) {
                    lastError = err;
                    const retryable = this._isRetryableInitError(err);
                    if (!retryable || attempt === maxAttempts) break;

                    const backoffMs = 500 * attempt;
                    await this._sleep(backoffMs);
                }
            }

            if (!Array.isArray(rows)) {
                const msg = lastError?.message ? String(lastError.message) : 'Unknown error';
                const cacheAgeMs = Date.now() - (this._activeTradesCache.fetchedAtMs || 0);
                const canUseCache = Array.isArray(this._activeTradesCache.trades) && this._activeTradesCache.trades.length > 0 && cacheAgeMs < (5 * 60 * 1000);

                if (canUseCache) {
                    this._logActiveTradesErrorOncePer(60 * 1000, `⚠️ Google Sheets active-trades fetch failed (${msg}). Using cached active trades (${this._activeTradesCache.trades.length}).`);
                    return this._activeTradesCache.trades;
                }

                this._logActiveTradesErrorOncePer(60 * 1000, `❌ Failed to get active trades from Google Sheets: ${msg}`);
                return [];
            }

            const activeTrades = rows
                .filter(row => {
                    const status = String(this._getTradesCell(row, 'Status') || '').trim().toLowerCase();
                    // Only include trades that are ACTIVE (not closed, partial, or empty)
                    return status === 'active' && status !== 'closed' && status !== 'partial';
                })
                .map(row => {
                    const expiration = this._getTradesCell(row, 'Options Expiration') || this._getTradesCell(row, 'Options Expiry') || null;
                    const tradeId = this._getTradesCell(row, 'Trade ID');

                    if (!expiration) {
                        console.warn(`   ⚠️ Trade ${tradeId}: No expiration found in Sheets - exit checks may fail`);
                    }

                    return {
                        tradeId: tradeId,
                        symbol: this._getTradesCell(row, 'Symbol'),
                        entryPrice: parseFloat(this._getTradesCell(row, 'Entry Price')),
                        strike: this._getTradesCell(row, 'Strike'),
                        direction: this._getTradesCell(row, 'Direction'),
                        contracts: (() => {
                            const v = this._getTradesCell(row, 'Contracts');
                            const n = parseFloat(v);
                            return Number.isFinite(n) ? n : null;
                        })(),
                        contractsRemaining: (() => {
                            const v = this._getTradesCell(row, 'Contracts Remaining');
                            const n = parseFloat(v);
                            return Number.isFinite(n) ? n : null;
                        })(),
                        stopPrice: (() => {
                            const v = this._getTradesCell(row, 'Stop Price');
                            const n = parseFloat(v);
                            return Number.isFinite(n) ? n : null;
                        })(),
                        tradeIntent: this._getTradesCell(row, 'Trade Intent') || null,
                        profile: this._getTradesCell(row, 'Profile') || null,
                        alpacaOrderId: this._getTradesCell(row, 'Alpaca Order ID') || null,
                        entryTime: this._getTradesCell(row, 'Entry Time'),
                        status: this._getTradesCell(row, 'Status'),
                        marketCondition: this._getTradesCell(row, 'Market Condition') || null,
                        entryVolRatio: (() => {
                            const v = this._getTradesCell(row, 'Entry Vol Ratio');
                            const n = parseFloat(v);
                            return Number.isFinite(n) ? n : null;
                        })(),
                        entryVWAP: (() => {
                            const v = this._getTradesCell(row, 'Entry VWAP');
                            const n = parseFloat(v);
                            return Number.isFinite(n) ? n : null;
                        })(),
                        expiration: expiration,
                        discordMessageId: this._getTradesCell(row, 'Discord Message ID') || null,
                        discordThreadId: this._getTradesCell(row, 'Discord Thread ID') || null
                    };
                });

            // Cache result (even empty), so transient failures can reuse last known state.
            this._activeTradesCache = { trades: activeTrades, fetchedAtMs: Date.now() };

            return activeTrades;
        } catch (error) {
            const msg = error?.message ? String(error.message) : 'Unknown error';
            const cacheAgeMs = Date.now() - (this._activeTradesCache.fetchedAtMs || 0);
            const canUseCache = Array.isArray(this._activeTradesCache.trades) && this._activeTradesCache.trades.length > 0 && cacheAgeMs < (5 * 60 * 1000);

            if (canUseCache) {
                this._logActiveTradesErrorOncePer(60 * 1000, `⚠️ Google Sheets active-trades fetch failed (${msg}). Using cached active trades (${this._activeTradesCache.trades.length}).`);
                return this._activeTradesCache.trades;
            }

            this._logActiveTradesErrorOncePer(60 * 1000, `❌ Failed to get active trades from Google Sheets: ${msg}`);
            return [];
        }
    }

    async getAllTrades() {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) return {};
            }

            const rows = await this.tradesSheet.getRows();
            const trades = {};
            
            rows.forEach(row => {
                                const tradeId = this._getTradesCell(row, 'Trade ID');
                if (tradeId) {
                    trades[tradeId] = {
                        tradeId: tradeId,
                                                symbol: this._getTradesCell(row, 'Symbol'),
                                                entryPrice: parseFloat(this._getTradesCell(row, 'Entry Price')),
                                                exitPrice: this._getTradesCell(row, 'Exit Price') ? parseFloat(this._getTradesCell(row, 'Exit Price')) : null,
                                                strike: this._getTradesCell(row, 'Strike'),
                                                direction: this._getTradesCell(row, 'Direction'),
                                                entryTime: this._getTradesCell(row, 'Entry Time'),
                                                exitTime: this._getTradesCell(row, 'Exit Time') || null,
                                                exitReason: this._getTradesCell(row, 'Exit Reason') || null,
                                                status: this._getTradesCell(row, 'Status'),
                                                pnl: this._getTradesCell(row, 'P&L') ? parseFloat(this._getTradesCell(row, 'P&L')) : null,
                        // Compatibility with existing code
                                                time: this._getTradesCell(row, 'Entry Time'), // Compatibility with existing code
                                                aiGenerated: String(this._getTradesCell(row, 'AI Generated') || '').trim().toLowerCase() === 'true',
                                                aiGrade: this._getTradesCell(row, 'AI Grade'),
                                                aiConfidence: parseInt(this._getTradesCell(row, 'AI Confidence')) || null,
                                                aiNote: this._getTradesCell(row, 'AI Note') || null,
                                                entryVolRatio: parseFloat(this._getTradesCell(row, 'Entry Vol Ratio')) || null,
                                                entryRSI: parseFloat(this._getTradesCell(row, 'Entry RSI')) || null,
                                                entryVWAP: parseFloat(this._getTradesCell(row, 'Entry VWAP')) || null,
                                                entryEMA5: parseFloat(this._getTradesCell(row, 'Entry EMA5')) || null,
                                                entryEMA20: parseFloat(this._getTradesCell(row, 'Entry EMA20')) || null,
                                                entryEMA50: parseFloat(this._getTradesCell(row, 'Entry EMA50')) || null,
                                                marketCondition: this._getTradesCell(row, 'Market Condition'),
                                                aiMode: this._getTradesCell(row, 'AI Mode'),
                        optionsExpiration: (() => {
                          // CRITICAL: Store expiration date in MM/DD/YYYY format WITHOUT timezone conversion
                          // API returns YYYY-MM-DD string → convert to MM/DD/YYYY for Sheets
                                                    const exp = this._getTradesCell(row, 'Options Expiration');
                          if (!exp) return '';
                          
                          // Expected format: "YYYY-MM-DD" string from Market Data API
                          if (typeof exp === 'string') {
                            const match = exp.match(/^(\d{4})-(\d{2})-(\d{2})/);
                            if (match) {
                              const [_, year, month, day] = match;
                              // Direct string manipulation - NO Date objects = NO timezone issues
                              return `${month}/${day}/${year}`;
                            }
                          }
                          
                          return '';
                        })(),
                                                alpacaOrderId: this._getTradesCell(row, 'Alpaca Order ID') || null,
                                                pnlPercent: parseFloat(this._getTradesCell(row, 'P&L %')) || null
                    };
                    
                    // Add backward compatibility aliases for analysis tools
                    const trade = trades[tradeId];
                    trade.grade = trade.aiGrade;
                    trade.confidence = trade.aiConfidence;
                    trade.aiNote = trade.aiNote; // Already correct name
                    trade.volRatio = trade.entryVolRatio;
                    trade.rsi = trade.entryRSI;
                    trade.vwap = trade.entryVWAP;
                    trade.ema5 = trade.entryEMA5;
                    trade.ema20 = trade.entryEMA20;
                    trade.ema50 = trade.entryEMA50;
                    trade.mode = trade.aiMode;
                }
            });

            return trades;
        } catch (error) {
            console.error('❌ Failed to get all trades from Google Sheets:', error.message);
            return {};
        }
    }

    // ==================== BOT STATUS METHODS (Render.com Cron Persistence) ====================
    
    /**
     * Get a bot status value from Google Sheets (persistent across cron invocations)
     * @param {string} key - The status key (e.g., 'lastBriefing', 'lastEOD', 'lastStatus11', 'lastStatus13')
     * @returns {string|null} The stored value or null if not found
     */
    async getBotStatus(key) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) {
                    console.error('❌ Google Sheets not initialized, cannot get bot status');
                    return null;
                }
            }

            const rows = await this.botStatusSheet.getRows();
            const row = rows.find(r => r.get('Key') === key);
            
            if (row) {
                const value = row.get('Value');
                return value;
            }
            
            return null;
        } catch (error) {
            console.error(`❌ Failed to get bot status '${key}':`, error.message);
            return null;
        }
    }

    /**
     * Set a bot status value in Google Sheets (persistent across cron invocations)
     * @param {string} key - The status key (e.g., 'lastBriefing', 'lastEOD', 'lastStatus11', 'lastStatus13')
     * @param {string} value - The value to store (typically today's date)
     * @returns {boolean} Success status
     */
    async setBotStatus(key, value) {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) {
                    console.error('❌ Google Sheets not initialized, cannot set bot status');
                    return false;
                }
            }

            const rows = await this.botStatusSheet.getRows();
            const existingRow = rows.find(r => r.get('Key') === key);
            
            if (existingRow) {
                // Update existing row
                existingRow.set('Value', value);
                existingRow.set('Updated At', formatCSTTimestamp());
                await existingRow.save();
                console.log(`📊 Bot status updated: ${key} = ${value}`);
            } else {
                // Add new row
                await this.botStatusSheet.addRow({
                    'Key': key,
                    'Value': value,
                    'Updated At': formatCSTTimestamp()
                });
                console.log(`📊 Bot status created: ${key} = ${value}`);
            }
            
            return true;
        } catch (error) {
            console.error(`❌ Failed to set bot status '${key}':`, error.message);
            return false;
        }
    }

    /**
     * Get all bot status values from Google Sheets
     * @returns {Object} Object with all status key-value pairs
     */
    async getAllBotStatus() {
        try {
            if (!this.isInitialized) {
                const initialized = await this.initialize();
                if (!initialized) {
                    console.error('❌ Google Sheets not initialized, cannot get bot status');
                    return {};
                }
            }

            const rows = await this.botStatusSheet.getRows();
            const status = {};
            
            rows.forEach(row => {
                const key = row.get('Key');
                const value = row.get('Value');
                if (key) {
                    status[key] = value;
                }
            });
            
            return status;
        } catch (error) {
            console.error('❌ Failed to get all bot status:', error.message);
            return {};
        }
    }
}

module.exports = new GoogleSheetsService();