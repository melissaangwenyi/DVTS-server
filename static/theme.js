/* 
   theme.js — Light/Dark mode toggle
   - Reads saved preference from localStorage on every page load
   - Falls back to no preference (light) if nothing saved
   - Applies the theme as a [data-theme] attribute on <html>
   - This script must run BEFORE the page paints to avoid a "flash of light" */

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


/*  SESSION TIMEOUT WARNING  
   Shows a warning banner 5 minutes before the 30-minute session expires.
   The banner disappears if the user interacts. On expiry, redirects to login.
   Only runs on pages that have the .topbar element (i.e. logged-in pages).
 */
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

/*  GLOBAL DOUBLE-CLICK / DOUBLE-SUBMIT PROTECTION 
   Prevents any button or form from firing twice due to slow responses.
   Applied to every page automatically via theme.js. */
(function() {
    // Track in-flight AJAX fetch calls by URL
    var _inflight = {};

    // Override fetch globally to prevent duplicate calls to same URL
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        var method = (opts && opts.method) ? opts.method.toUpperCase() : 'GET';
        // Only gate POST/mutating requests
        if (method !== 'GET') {
            var key = method + ':' + url;
            if (_inflight[key]) {
                // Already in flight — return a rejected promise silently
                return Promise.reject(new Error('duplicate_request'));
            }
            _inflight[key] = true;
            return _origFetch.apply(this, arguments).finally(function() {
                delete _inflight[key];
            });
        }
        return _origFetch.apply(this, arguments);
    };

    // Disable form submit buttons immediately on submit (prevents double POST)
    document.addEventListener('submit', function(e) {
        var form = e.target;
        // Skip checkin and checkout — they have their own handlers
        if (form.id === 'checkinForm') return;
        var btn = form.querySelector('button[type="submit"]');
        if (btn && !btn.disabled) {
            btn.disabled = true;
            btn.style.opacity = '0.7';
            // Re-enable after 5s as safety net in case page doesn't navigate
            setTimeout(function() {
                btn.disabled = false;
                btn.style.opacity = '';
            }, 5000);
        }
    }, true);
})();