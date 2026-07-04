// Copyright (C) 2026 D. Brandmeyer - AGPL-3.0
// Applies saved theme before first paint to prevent flash.
(function () {
  var t = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', t);
}());
