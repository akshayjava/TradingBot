/**
 * Shared utilities for Portfolio Bot web UI.
 */

/**
 * Open a Server-Sent Events stream and call `onEvent` for each parsed JSON event.
 * Automatically closes on 'done' or 'error' events.
 *
 * @param {string} url  - SSE endpoint URL
 * @param {function} onEvent - called with each parsed event object
 * @param {function} [onClose] - called when stream ends
 */
function streamSSE(url, onEvent, onClose) {
  const es = new EventSource(url);

  es.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    onEvent(data);
    if (data.type === 'done' || data.type === 'error') {
      es.close();
      if (onClose) onClose(data);
    }
  };

  es.onerror = (e) => {
    es.close();
    onEvent({ type: 'error', msg: 'SSE connection error' });
    if (onClose) onClose({ type: 'error' });
  };

  return es;
}

/**
 * Format a number with thousand separators.
 */
function fmtNumber(n, decimals = 0) {
  return parseFloat(n).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * Format dollars with sign.
 */
function fmtDollar(n, signed = false) {
  const prefix = signed && n >= 0 ? '+$' : n < 0 ? '-$' : '$';
  return prefix + fmtNumber(Math.abs(n), 2);
}
