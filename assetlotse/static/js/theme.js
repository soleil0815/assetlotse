/* Apply the light/dark choice before the first paint (synchronously in <head>, without an inline script). */
try { var t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
