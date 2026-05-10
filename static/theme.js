/* =============================================================================
   theme.js — Light/Dark mode toggle
   - Reads saved preference from localStorage on every page load
   - Falls back to no preference (light) if nothing saved
   - Applies the theme as a [data-theme] attribute on <html>
   - This script must run BEFORE the page paints to avoid a "flash of light"
============================================================================= */

(function () {
    var STORAGE_KEY = 'vts-theme';

    function applyTheme(theme) {
        if (theme === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
    }

    // Default is dark. Only switch to light if user explicitly chose it.
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    applyTheme(saved === 'light' ? 'light' : 'dark');

    // Wire up the toggle button(s) once the DOM is ready
    document.addEventListener('DOMContentLoaded', function () {
        var buttons = document.querySelectorAll('.theme-toggle');
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].addEventListener('click', function () {
                var current = document.documentElement.getAttribute('data-theme');
                var next    = current === 'dark' ? 'light' : 'dark';
                applyTheme(next);
                try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
            });
        }
    });
})();


/* ── SESSION TIMEOUT WARNING ──────────────────────────────────────────────
   Shows a warning banner 5 minutes before the 30-minute session expires.
   The banner disappears if the user interacts. On expiry, redirects to login.
   Only runs on pages that have the .topbar element (i.e. logged-in pages).
──────────────────────────────────────────────────────────────────────── */
(function() {
    if (!document.querySelector) return;

    var TIMEOUT_MS = 30 * 60 * 1000;   // 30 minutes (matches Flask config)
    var WARN_MS    = 25 * 60 * 1000;   // warn at 25 minutes
    var _warnTimer, _expireTimer, _banner;

    function resetTimers() {
        clearTimeout(_warnTimer);
        clearTimeout(_expireTimer);
        if (_banner) { _banner.style.display = 'none'; }

        _warnTimer = setTimeout(showWarning, WARN_MS);
        _expireTimer = setTimeout(function() {
            window.location.href = '/logout';
        }, TIMEOUT_MS);
    }

    function showWarning() {
        if (!_banner) {
            _banner = document.createElement('div');
            _banner.style.cssText = (
                'position:fixed;top:0;left:0;right:0;z-index:9999;' +
                'background:#854F0B;color:#fff;text-align:center;' +
                'padding:10px 16px;font-size:13px;font-weight:500;' +
                'display:flex;align-items:center;justify-content:center;gap:12px;'
            );
            _banner.innerHTML = (
                '⚠ Your session will expire in 5 minutes due to inactivity. ' +
                '<button onclick="resetTimers()" style="background:rgba(255,255,255,.2);' +
                'border:1px solid rgba(255,255,255,.4);color:#fff;padding:4px 12px;' +
                'border-radius:6px;cursor:pointer;font-size:12px;">Stay logged in</button>'
            );
            document.body.appendChild(_banner);
        }
        _banner.style.display = 'flex';
    }

    // Only run on logged-in pages (has topbar)
    document.addEventListener('DOMContentLoaded', function() {
        if (!document.getElementById('toast') && !document.querySelector('.topbar')) return;
        // Reset timer on any user interaction
        ['click','keydown','mousemove','touchstart'].forEach(function(ev) {
            document.addEventListener(ev, resetTimers, {passive: true});
        });
        resetTimers();
    });

    // Expose for the "Stay logged in" button
    window.resetTimers = resetTimers;
})();