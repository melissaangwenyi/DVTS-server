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