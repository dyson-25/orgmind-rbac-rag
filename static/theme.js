'use strict';
(() => {
  let theme;
  try { theme = localStorage.getItem('orgmind-theme'); } catch { /* Storage can be unavailable in private contexts. */ }
  if (theme !== 'day' && theme !== 'night') {
    theme = window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'night' : 'day';
  }
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'night' ? '#080b0f' : '#e4f2f7');
})();
