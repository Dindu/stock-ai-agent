// ==================== TIMEZONE UTILITIES ====================
// Critical for production deployment on Render.com (UTC servers)

function nowCT() {
  return new Date().toLocaleString('en-US', { timeZone: 'America/Chicago' });
}

function nowCTDate() {
  // Get current time in Central Time (handles CST/CDT automatically)
  const utcNow = new Date();
  
  // Convert to Central Time using Intl.DateTimeFormat
  const options = {
    timeZone: 'America/Chicago',
    year: 'numeric',
    month: '2-digit', 
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  };
  
  const formatter = new Intl.DateTimeFormat('en-CA', options);
  const parts = formatter.formatToParts(utcNow);
  
  // Reconstruct date in Central Time
  const year = parseInt(parts.find(p => p.type === 'year').value);
  const month = parseInt(parts.find(p => p.type === 'month').value) - 1; // Month is 0-indexed
  const day = parseInt(parts.find(p => p.type === 'day').value);
  const hour = parseInt(parts.find(p => p.type === 'hour').value);
  const minute = parseInt(parts.find(p => p.type === 'minute').value);
  const second = parseInt(parts.find(p => p.type === 'second').value);
  
  return new Date(year, month, day, hour, minute, second);
}

function getCSTTime() {
  // Alternative method for getting CST time - more reliable for production
  const now = new Date();
  return new Date(now.toLocaleString('en-US', { timeZone: 'America/Chicago' }));
}

function formatCSTTime(date) {
  return date.toLocaleString('en-US', { 
    timeZone: 'America/Chicago',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit', 
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
}

function getCSTTimestamp() {
  // Returns Central Time in MM/DD/YYYY, HH:MM:SS format
  const now = new Date();
  const cstTime = now.toLocaleString('en-US', { 
    timeZone: 'America/Chicago',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
  return cstTime; // Returns format: MM/DD/YYYY, HH:MM:SS
}

function formatCSTTimestamp(date = null) {
  // Converts any date to Central Time in MM/DD/YYYY, HH:MM:SS format
  const dateToFormat = date ? new Date(date) : new Date();
  const cstTime = dateToFormat.toLocaleString('en-US', { 
    timeZone: 'America/Chicago',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
  return cstTime; // Returns format: MM/DD/YYYY, HH:MM:SS
}

function getCSTISOString() {
  // Returns Central Standard Time in ISO format (always -06:00 CST)
  // This converts to actual CST time regardless of daylight saving
  const now = new Date();
  
  // Convert to CST (UTC-6) by subtracting 6 hours from UTC
  const cstTime = new Date(now.getTime() - (6 * 60 * 60 * 1000));
  
  // Format as ISO string and replace Z with -06:00 to indicate CST
  const isoString = cstTime.toISOString().replace('Z', '-06:00');
  
  return isoString;
}

function isRenderEnvironment() {
  return !!(process.env.RENDER || process.argv.includes('--render'));
}

module.exports = {
  nowCT,
  nowCTDate,
  getCSTTime,
  formatCSTTime,
  getCSTTimestamp,
  formatCSTTimestamp,
  getCSTISOString,
  isRenderEnvironment
};
