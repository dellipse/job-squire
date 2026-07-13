// Copyright (C) 2026 D. Brandmeyer
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.
//
// Unobtrusive JS. Loaded from /static so it satisfies the CSP `script-src 'self'`
// (inline handlers and inline <script> are blocked by the policy on purpose).
document.addEventListener('DOMContentLoaded', function () {
  // Clickable table rows: <tr data-href="/jobs/1">
  // Skip navigation when the click originates from an interactive element
  // (checkboxes, selects, buttons, links, labels) so those elements work normally.
  document.querySelectorAll('[data-href]').forEach(function (el) {
    el.style.cursor = 'pointer';
    el.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input, select, textarea, label')) return;
      window.location = el.getAttribute('data-href');
    });
  });

  // Theme toggle
  var themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var current = document.documentElement.getAttribute('data-theme') || 'dark';
      var next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    });
  }

  // Select that navigates on change: <select data-navigate="/kit?job_id=">
  document.querySelectorAll('select[data-navigate]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      if (sel.value) window.location = sel.getAttribute('data-navigate') + encodeURIComponent(sel.value);
    });
  });

  // Select that submits its form on change: <select data-autosubmit>
  document.querySelectorAll('select[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () { if (sel.form) sel.form.submit(); });
  });

  // Click to select all text: <input class="select-on-click">
  document.querySelectorAll('.select-on-click').forEach(function (el) {
    el.addEventListener('click', function () { el.select(); });
  });

  // Confirm before submitting: <form data-confirm="Are you sure?">
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      if (!window.confirm(f.getAttribute('data-confirm'))) e.preventDefault();
    });
  });

  // Show a "working…" state on slow, synchronous single-shot AI requests that
  // aren't backed by a live status page: <button data-loading-text="...">
  // Disables the button and swaps its label right after the click, so there's
  // visible feedback while the browser waits for the full-page response
  // (which can take up to a minute or more with some AI providers).
  document.querySelectorAll('button[data-loading-text]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      window.setTimeout(function () {
        btn.disabled = true;
        btn.textContent = btn.getAttribute('data-loading-text');
      }, 0);
    });
  });

  // Settings page tabs. Tabs use data-tab on buttons and id on panels.
  // Active tab is persisted in localStorage so it survives form-submit redirects,
  // and reflected in the URL hash so links like #tab-claude land on the right tab.
  var tabBtns = document.querySelectorAll('.page-tabs button[data-tab]');
  if (tabBtns.length) {
    var STORAGE_KEY = 'settings-tab';
    function activateTab(id, opts) {
      tabBtns.forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-tab') === id);
      });
      document.querySelectorAll('.tab-panel').forEach(function (p) {
        p.classList.toggle('active', p.id === id);
      });
      try { localStorage.setItem(STORAGE_KEY, id); } catch (e) {}
      if (!opts || opts.updateHash !== false) {
        try { history.replaceState(null, '', '#' + id); } catch (e) {}
      }
    }
    tabBtns.forEach(function (b) {
      b.addEventListener('click', function () { activateTab(b.getAttribute('data-tab')); });
    });
    var valid = Array.from(tabBtns).map(function (b) { return b.getAttribute('data-tab'); });
    var first = tabBtns[0].getAttribute('data-tab');
    // A URL hash (e.g. #tab-claude) takes priority over the remembered tab,
    // so shared/bookmarked links always land where they say they will.
    var hashTab = window.location.hash ? window.location.hash.slice(1) : null;
    var saved;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    var initial = (hashTab && valid.indexOf(hashTab) !== -1) ? hashTab
      : (saved && valid.indexOf(saved) !== -1) ? saved
      : first;
    activateTab(initial, { updateHash: !!hashTab || initial !== first });
    // If the hash changes while the page is open (e.g. back/forward), follow it.
    window.addEventListener('hashchange', function () {
      var h = window.location.hash ? window.location.hash.slice(1) : null;
      if (h && valid.indexOf(h) !== -1) activateTab(h, { updateHash: false });
    });
  }

  // Getting Started: First Search step. While a search is running in the
  // background, poll by reloading every 5s so the result appears without a
  // manual refresh. The server decides whether to keep polling (data-poll)
  // based on the SearchRun status, so this just stops naturally once done.
  var searchStatus = document.getElementById('first-search-status');
  if (searchStatus && searchStatus.getAttribute('data-poll') === '1') {
    window.setTimeout(function () { window.location.reload(); }, 5000);
  }

  // AI settings sub-tabs (secondary row within the AI tab).
  var aiSubBtns = document.querySelectorAll('.ai-sub-tabs button[data-ai-tab]');
  if (aiSubBtns.length) {
    var AI_STORAGE_KEY = 'settings-ai-subtab';
    function activateAiTab(id) {
      aiSubBtns.forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-ai-tab') === id);
      });
      document.querySelectorAll('.ai-sub-panel').forEach(function (p) {
        p.classList.toggle('active', p.id === id);
      });
      try { localStorage.setItem(AI_STORAGE_KEY, id); } catch (e) {}
    }
    aiSubBtns.forEach(function (b) {
      b.addEventListener('click', function () { activateAiTab(b.getAttribute('data-ai-tab')); });
    });
    var savedAi;
    try { savedAi = localStorage.getItem(AI_STORAGE_KEY); } catch (e) {}
    var firstAi = aiSubBtns[0].getAttribute('data-ai-tab');
    var validAi = Array.from(aiSubBtns).map(function (b) { return b.getAttribute('data-ai-tab'); });
    activateAiTab((savedAi && validAi.indexOf(savedAi) !== -1) ? savedAi : firstAi);
  }

  // -----------------------------------------------------------------------
  // Mobile nav toggle (hamburger)
  // -----------------------------------------------------------------------
  var navToggle = document.getElementById('nav-toggle');
  var mainNav   = document.getElementById('main-nav');
  if (navToggle && mainNav) {
    navToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = mainNav.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', String(open));
    });
    // Close when a nav link is tapped
    mainNav.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', function () { mainNav.classList.remove('open'); });
    });
    // Close when tapping outside
    document.addEventListener('click', function (e) {
      if (!mainNav.contains(e.target) && e.target !== navToggle) {
        mainNav.classList.remove('open');
      }
    });
  }

  // -----------------------------------------------------------------------
  // Jobs list: multi-column sort and per-page selector
  // -----------------------------------------------------------------------
  var sortTable = document.querySelector('table.sortable[data-sort]');
  if (sortTable) {
    // Parse "company:asc,title:desc" → [{col, dir}, ...]
    function parseSortStr(s) {
      if (!s) return [];
      return s.split(',').map(function (part) {
        var pieces = part.trim().split(':');
        return { col: pieces[0].trim(), dir: (pieces[1] || 'asc').trim() };
      }).filter(function (x) { return x.col; });
    }

    function buildSortStr(cols) {
      return cols.map(function (x) { return x.col + ':' + x.dir; }).join(',');
    }

    var currentSort = parseSortStr(sortTable.getAttribute('data-sort'));

    document.querySelectorAll('th[data-sortcol]').forEach(function (th) {
      th.addEventListener('click', function () {
        var col = th.getAttribute('data-sortcol');
        var existing = currentSort.find(function (x) { return x.col === col; });
        var newSort;

        if (!existing) {
          // Not sorted → add as ASC
          newSort = currentSort.concat([{ col: col, dir: 'asc' }]);
        } else if (existing.dir === 'asc') {
          // ASC → DESC
          newSort = currentSort.map(function (x) {
            return x.col === col ? { col: col, dir: 'desc' } : x;
          });
        } else {
          // DESC → remove
          newSort = currentSort.filter(function (x) { return x.col !== col; });
        }

        var url = new URL(window.location.href);
        url.searchParams.set('sort', buildSortStr(newSort));
        url.searchParams.set('page', '1');
        window.location = url.toString();
      });
    });

    // Per-page selector
    var perPageSel = document.getElementById('per-page-select');
    if (perPageSel) {
      perPageSel.addEventListener('change', function () {
        var url = new URL(window.location.href);
        url.searchParams.set('per_page', perPageSel.value);
        url.searchParams.set('page', '1');
        window.location = url.toString();
      });
    }
  }

  // -----------------------------------------------------------------------
  // Job detail: pre-fill follow-up date input with 3 business days from today
  // -----------------------------------------------------------------------
  var followupInput = document.getElementById('followup-date-input');
  if (followupInput && !followupInput.value) {
    function addBusinessDays(d, n) {
      var date = new Date(d.getTime());
      var added = 0;
      while (added < n) {
        date.setDate(date.getDate() + 1);
        var dow = date.getDay();
        if (dow !== 0 && dow !== 6) added++;
      }
      return date;
    }
    var def = addBusinessDays(new Date(), 3);
    // Format as YYYY-MM-DD (local date, not UTC)
    var y = def.getFullYear();
    var m = String(def.getMonth() + 1).padStart(2, '0');
    var day = String(def.getDate()).padStart(2, '0');
    followupInput.value = y + '-' + m + '-' + day;
  }

  // -----------------------------------------------------------------------
  // "Open in Claude" buttons: open claude.ai/new pre-filled with the prompt
  // AND copy the full prompt to clipboard (URL truncation fallback).
  // Usage: <button class="open-in-claude-btn" data-prompt="...full text...">
  //   Optional data-prompt-id="textarea-id" to read from a textarea instead.
  //   Optional data-status-id or a sibling .routine-open-status for feedback.
  // -----------------------------------------------------------------------
  document.querySelectorAll('.open-in-claude-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var prompt = btn.getAttribute('data-prompt') || '';
      // If a prompt-id is set, prefer reading the live textarea value (handles
      // any runtime changes and is always the full untruncated text).
      var promptId = btn.getAttribute('data-prompt-id');
      if (promptId) {
        var el = document.getElementById(promptId);
        if (el && el.value) prompt = el.value;
      }
      if (!prompt) return;

      // 1. Open Claude in a new tab. Use ?q= pre-fill only when the encoded URL
      //    stays within a safe length — servers reject URLs over ~4 KB (HTTP 414).
      //    For longer prompts, open the bare URL; the clipboard copy below lets
      //    the user paste the prompt manually.
      var encoded = 'https://claude.ai/new?q=' + encodeURIComponent(prompt);
      var url = encoded.length <= 4096 ? encoded : 'https://claude.ai/new';
      window.open(url, '_blank', 'noopener,noreferrer');

      // 2. Also copy the full prompt to clipboard so the user can paste it
      //    manually if the pre-fill was truncated.
      var statusEl = null;
      // Look for a sibling status span on the same card.
      var card = btn.closest('.routine-card');
      if (card) statusEl = card.querySelector('.routine-open-status');

      function showStatus(msg) {
        if (statusEl) {
          statusEl.textContent = msg;
          setTimeout(function () { statusEl.textContent = ''; }, 4000);
        }
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(prompt).then(function () {
          showStatus('Claude opened. Prompt also copied to clipboard.');
        }).catch(function () {
          showStatus('Claude opened. Copy the prompt above if needed.');
        });
      } else {
        // Fallback: select the textarea so the user can copy manually.
        if (promptId) {
          var ta = document.getElementById(promptId);
          if (ta) ta.select();
        }
        showStatus('Claude opened. Select the prompt above and copy if needed.');
      }
    });
  });

  // -----------------------------------------------------------------------
  // Copy-to-clipboard buttons: <button class="copy-btn" data-target="element-id">Copy</button>
  // -----------------------------------------------------------------------
  document.querySelectorAll('.copy-btn[data-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = document.getElementById(btn.getAttribute('data-target'));
      if (!target) return;
      var text = target.tagName === 'TEXTAREA' || target.tagName === 'INPUT'
        ? target.value
        : (target.textContent || target.innerText || '');
      if (!text) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          var orig = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(function () { btn.textContent = orig; }, 1800);
        }).catch(function () {
          _fallbackCopy(target, btn);
        });
      } else {
        _fallbackCopy(target, btn);
      }
    });
  });

  function _fallbackCopy(el, btn) {
    if (el.select) { el.select(); }
    try {
      document.execCommand('copy');
      var orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(function () { btn.textContent = orig; }, 1800);
    } catch (e) {
      btn.textContent = 'Select and copy manually';
    }
  }

  // -----------------------------------------------------------------------
  // Jobs list: bulk-select checkboxes + floating action bar
  // -----------------------------------------------------------------------
  var selectAllCb = document.getElementById('select-all-cb');
  var bulkBar     = document.getElementById('bulk-bar');
  var bulkForm    = document.getElementById('bulk-form');

  if (selectAllCb && bulkBar && bulkForm) {
    var bulkCountNum  = document.getElementById('bulk-count-num');
    var bulkIdsDiv    = document.getElementById('bulk-job-ids');
    var bulkActionInp = document.getElementById('bulk-action');
    var bulkStatusInp = document.getElementById('bulk-status');

    function getJobCbs() {
      return Array.from(document.querySelectorAll('.job-cb'));
    }

    function getChecked() {
      return getJobCbs().filter(function (cb) { return cb.checked; });
    }

    function updateBulkBar() {
      var checked = getChecked();
      var n = checked.length;
      bulkCountNum.textContent = n;
      bulkBar.classList.toggle('visible', n > 0);
      // Highlight selected rows.
      getJobCbs().forEach(function (cb) {
        var row = cb.closest('tr');
        if (row) row.classList.toggle('row-selected', cb.checked);
      });
    }

    // Individual checkbox change.
    document.querySelectorAll('.job-cb').forEach(function (cb) {
      cb.addEventListener('change', function () {
        var all = getJobCbs();
        selectAllCb.checked = all.length > 0 && all.every(function (c) { return c.checked; });
        selectAllCb.indeterminate = getChecked().length > 0 && !selectAllCb.checked;
        updateBulkBar();
      });
    });

    // Select All toggle.
    selectAllCb.addEventListener('change', function () {
      getJobCbs().forEach(function (cb) { cb.checked = selectAllCb.checked; });
      selectAllCb.indeterminate = false;
      updateBulkBar();
    });

    // Clear selection.
    document.getElementById('bulk-clear').addEventListener('click', function () {
      getJobCbs().forEach(function (cb) { cb.checked = false; });
      selectAllCb.checked = false;
      selectAllCb.indeterminate = false;
      updateBulkBar();
    });

    function submitBulk(action, status) {
      var checked = getChecked();
      if (!checked.length) return;
      // Populate the hidden form with selected IDs.
      bulkIdsDiv.innerHTML = '';
      checked.forEach(function (cb) {
        var inp = document.createElement('input');
        inp.type = 'hidden';
        inp.name = 'job_ids';
        inp.value = cb.value;
        bulkIdsDiv.appendChild(inp);
      });
      bulkActionInp.value = action;
      bulkStatusInp.value = status || '';
      bulkForm.style.display = 'block';
      bulkForm.submit();
    }

    document.getElementById('bulk-set-status').addEventListener('click', function () {
      var sel = document.getElementById('bulk-status-sel');
      if (!sel.value) { window.alert('Choose a status first.'); return; }
      if (window.confirm('Set ' + getChecked().length + ' job(s) to "' + sel.value + '"?')) {
        submitBulk('set_status', sel.value);
      }
    });

    document.getElementById('bulk-withdrawn').addEventListener('click', function () {
      if (window.confirm('Mark ' + getChecked().length + ' job(s) as Withdrawn?')) {
        submitBulk('withdrawn');
      }
    });

    document.getElementById('bulk-ghosted').addEventListener('click', function () {
      if (window.confirm('Mark ' + getChecked().length + ' job(s) as Ghosted?')) {
        submitBulk('ghosted');
      }
    });

    document.getElementById('bulk-pass').addEventListener('click', function () {
      if (window.confirm('Mark ' + getChecked().length + ' job(s) as Pass?')) {
        submitBulk('pass');
      }
    });
  }

  // "Build kit in Claude" button that reads the current kit form fields.
  var kitBtn = document.getElementById('kit-claude-btn');
  if (kitBtn) {
    kitBtn.addEventListener('click', function (e) {
      e.preventDefault();
      var val = function (name) {
        var el = document.querySelector('[name="' + name + '"]');
        return el ? el.value.trim() : '';
      };
      var title = val('job_title'), company = val('company'),
          loc = val('location'), desc = val('job_description');
      if (!title || !company) {
        window.alert('Enter at least a job title and company first.');
        return;
      }
      var trackedId = (function () {
        var h = document.querySelector('[name="tracked_job_id"]');
        return h ? h.value : '';
      }());
      var jobRef = trackedId
        ? 'call get_job(' + trackedId + ') to load the tracked job'
        : 'the role is: ' + title + ' at ' + company + (loc ? ' (' + loc + ')' : '');
      var p = 'Using my job-squire connector: call get_kit_instructions() to load the full ' +
        'workflow, then call get_candidate_profile() and ' + jobRef + ', and follow those ' +
        'instructions exactly.' +
        (!trackedId && desc ? ('\n\nJob posting details:\n' + desc) : '');
      window.open('https://claude.ai/new?q=' + encodeURIComponent(p), '_blank', 'noopener');
    });
  }

  // -----------------------------------------------------------------------
  // Job picker combobox
  // Navigation mode: container has data-navigate="/kit?job_id=" — clicking
  //   an item navigates to that URL.
  // Form mode: no data-navigate — clicking sets the hidden input value.
  // -----------------------------------------------------------------------
  document.querySelectorAll('.job-picker').forEach(function (picker) {
    var searchEl = picker.querySelector('.job-picker-search');
    var listEl   = picker.querySelector('.job-picker-list');
    var items    = Array.from(listEl.querySelectorAll('li[role="option"]'));
    var navBase  = picker.getAttribute('data-navigate');
    var hiddenEl = picker.querySelector('input[type="hidden"]');
    var selDiv   = picker.querySelector('.job-picker-selected');
    var selText  = selDiv ? selDiv.querySelector('.jpc-selected-text')    : null;
    var clearBtn = selDiv ? selDiv.querySelector('.job-picker-clear-btn') : null;

    function labelFor(li) {
      var co = li.querySelector('.jpc-company');
      var me = li.querySelector('.jpc-meta');
      return (co ? co.textContent.trim() : '') +
             (me && me.textContent.trim() ? ' — ' + me.textContent.trim() : '');
    }

    // Initialise form mode: if hidden input already has a value (edit page),
    // show the matching label in the selected display.
    if (!navBase && hiddenEl && hiddenEl.value) {
      var presel = items.find(function (li) {
        return li.getAttribute('data-value') === hiddenEl.value;
      });
      if (presel && selDiv) {
        selText.textContent = labelFor(presel);
        selDiv.removeAttribute('hidden');
      }
    }

    function filterItems(q) {
      q = (q || '').toLowerCase().trim();
      items.forEach(function (li) {
        li.hidden = q ? li.textContent.toLowerCase().indexOf(q) === -1 : false;
      });
    }

    function openList() {
      filterItems(searchEl.value);
      listEl.removeAttribute('hidden');
    }

    function closeList() { listEl.setAttribute('hidden', ''); }

    function selectItem(li) {
      if (navBase) {
        window.location = navBase + encodeURIComponent(li.getAttribute('data-value'));
      } else {
        if (hiddenEl) hiddenEl.value = li.getAttribute('data-value');
        if (selText)  selText.textContent = labelFor(li);
        if (selDiv)   selDiv.removeAttribute('hidden');
        searchEl.value = '';
        closeList();
      }
    }

    searchEl.addEventListener('focus', openList);
    searchEl.addEventListener('input', function () { filterItems(searchEl.value); });
    searchEl.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeList(); searchEl.blur(); }
    });

    // mousedown before blur so click still fires on the list item
    listEl.addEventListener('mousedown', function (e) { e.preventDefault(); });
    items.forEach(function (li) {
      li.addEventListener('click', function () { selectItem(li); });
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        if (hiddenEl) hiddenEl.value = '';
        if (selDiv)   selDiv.setAttribute('hidden', '');
        searchEl.value = '';
        searchEl.focus();
      });
    }

    document.addEventListener('click', function (e) {
      if (!picker.contains(e.target)) closeList();
    });
  });

  // -----------------------------------------------------------------------
  // Provider inline edit rows: Edit button expands a row beneath the entry
  // -----------------------------------------------------------------------
  var providerEditBtns = document.querySelectorAll('.provider-edit-btn');
  if (providerEditBtns.length) {
    providerEditBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pid = btn.getAttribute('data-pid');
        var editRow = document.getElementById('edit-row-' + pid);
        var isOpen = editRow.style.display !== 'none';
        // Close all open edit rows
        document.querySelectorAll('.provider-edit-row').forEach(function (r) {
          r.style.display = 'none';
        });
        // Toggle: open if it was closed
        if (!isOpen) { editRow.style.display = ''; }
      });
    });
    document.querySelectorAll('.provider-close-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pid = btn.getAttribute('data-pid');
        document.getElementById('edit-row-' + pid).style.display = 'none';
      });
    });
    document.querySelectorAll('.provider-delete-btn').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        if (!confirm('Remove ' + btn.getAttribute('data-name') + '?')) {
          e.preventDefault();
        }
      });
    });
  }

  // -----------------------------------------------------------------------
  // Search settings: the "City, ST" pattern only applies to a US location.
  // Loosen it client-side to match the server-side rule in settings_search()
  // so international operators aren't blocked by browser validation before
  // the request even reaches the server.
  // -----------------------------------------------------------------------
  var searchCountry = document.getElementById('search-country');
  var searchLocation = document.getElementById('search-location');
  var searchLocationHint = document.getElementById('search-location-hint');
  if (searchCountry && searchLocation) {
    var US_PATTERN = searchLocation.getAttribute('pattern');
    var US_PLACEHOLDER = searchLocation.getAttribute('placeholder');
    var US_HINT = searchLocationHint ? searchLocationHint.textContent : '';
    var INTL_HINT = 'Any location text works outside the US, e.g. "Manchester, UK".';
    function syncLocationConstraint() {
      var isUS = (searchCountry.value || '').trim().toUpperCase() === 'US';
      if (isUS) {
        searchLocation.setAttribute('pattern', US_PATTERN);
        searchLocation.setAttribute('placeholder', US_PLACEHOLDER);
      } else {
        searchLocation.removeAttribute('pattern');
        searchLocation.setAttribute('placeholder', 'City or region');
      }
      if (searchLocationHint) searchLocationHint.textContent = isUS ? US_HINT : INTL_HINT;
    }
    searchCountry.addEventListener('input', syncLocationConstraint);
    syncLocationConstraint();
  }

  // -----------------------------------------------------------------------
  // Add Provider form: show thinking_mode field only for Anthropic
  // -----------------------------------------------------------------------
  var newProviderType = document.getElementById('new-provider-type');
  var thinkingField   = document.getElementById('new-provider-thinking-field');
  if (newProviderType && thinkingField) {
    function syncThinkingField() {
      thinkingField.style.display = newProviderType.value === 'anthropic' ? '' : 'none';
    }
    newProviderType.addEventListener('change', syncThinkingField);
    syncThinkingField();
  }

  // -----------------------------------------------------------------------
  // Task status page — live log polling
  // Triggered by <div id="task-status-root" data-poll-url="..."> present
  // on the task_status.html page.
  // -----------------------------------------------------------------------
  var tsRoot = document.getElementById('task-status-root');
  if (tsRoot) {
    var pollUrl   = tsRoot.getAttribute('data-poll-url');
    var tsTask    = tsRoot.getAttribute('data-task') || '';
    var logBox    = document.getElementById('ts-log-box');
    var summary   = document.getElementById('ts-summary');
    var spinner   = document.getElementById('ts-spinner');
    var heading   = document.getElementById('ts-heading');
    var actions   = document.getElementById('ts-done-actions');
    var seenCount = 0;
    var tsTimer, tsTickTimer;
    var tsStarted = Date.now();
    var tsRunning = true;

    function tsElapsedText() {
      var secs = Math.floor((Date.now() - tsStarted) / 1000);
      if (secs < 60) return secs + 's';
      var m = Math.floor(secs / 60), s = secs % 60;
      return m + 'm ' + s + 's';
    }

    function tsAppendLogs(lines) {
      for (var i = seenCount; i < lines.length; i++) {
        var span = document.createElement('span');
        span.className = 'log-line';
        var lvl = lines[i].split(' ')[0].toLowerCase().replace(':', '');
        if (lvl === 'error' || lvl === 'warning') {
          span.className += ' level-' + lvl;
        }
        span.textContent = lines[i];
        logBox.appendChild(span);
        logBox.appendChild(document.createTextNode('\n'));
      }
      seenCount = lines.length;
      logBox.scrollTop = logBox.scrollHeight;
    }

    var closeBtn = document.getElementById('ts-close-btn');
    if (closeBtn) {
      closeBtn.addEventListener('click', function() { window.close(); });
    }

    function tsFinish(status, data) {
      tsRunning = false;
      clearInterval(tsTimer);
      clearInterval(tsTickTimer);
      if (spinner) spinner.classList.add('hidden');
      if (actions) actions.style.display = '';
      // Only show the close button if the browser will honour window.close()
      // (requires the tab to have been opened by a parent window).
      if (closeBtn && window.opener) {
        closeBtn.style.display = '';
      }

      if (status === 'done') {
        heading.style.color = 'var(--color-success, #3dbf6c)';
        var r = data.result || {};
        var parts = [];
        if (r.scored  !== undefined) parts.push('Scored: ' + r.scored);
        if (r.failed  !== undefined && r.failed > 0) parts.push('Failed: ' + r.failed);
        if (r.drafted !== undefined) parts.push('Drafted: ' + r.drafted);
        if (r.overall_summary) parts.push(r.overall_summary.slice(0, 120));
        if (tsTask === 'score_fit' && r.score !== undefined) {
          parts.push('Fit score: ' + r.score + '/10');
        }
        if (tsTask === 'draft_followup' && r.email_text) {
          parts.push('Draft saved');
        }
        if (tsTask === 'ats_gap' && r.overall_match_estimate) {
          parts.push('Match estimate: ' + r.overall_match_estimate);
        }
        if (tsTask === 'build_kit' && r.title !== undefined && r.company !== undefined && !parts.length) {
          parts.push('Kit built for ' + r.title + ' at ' + r.company);
        }
        summary.textContent = parts.length ? parts.join(' · ') : 'Complete.';
        document.title = 'Done — ' + heading.textContent.trim();

        // Kit-build result: either a link to the tracked job, or (untracked
        // posting) the kit text itself plus a .docx download button.
        var kitExtra = document.getElementById('ts-kit-extra');
        if (kitExtra && tsTask === 'build_kit' && (r.job_id || r.kit_markdown)) {
          kitExtra.style.display = '';
          if (r.job_id) {
            var jobLink = document.getElementById('ts-kit-job-link');
            jobLink.href = '/jobs/' + r.job_id;
            jobLink.textContent = 'View application kit →';
            jobLink.style.display = '';
          }
          if (r.kit_markdown) {
            document.getElementById('ts-kit-output-text').textContent = r.kit_markdown;
            document.getElementById('ts-kit-docx-markdown').value = r.kit_markdown;
            document.getElementById('ts-kit-docx-company').value = r.company || '';
            document.getElementById('ts-kit-docx-title').value = r.title || '';
            document.getElementById('ts-kit-output-wrap').style.display = '';
          }
        }

        // Single-job result: ats-gap, score-fit, draft-followup. A link back to
        // the job, plus an inline preview of whatever the task produced.
        var jobExtra = document.getElementById('ts-job-extra');
        var singleJobTasks = {ats_gap: 1, score_fit: 1, draft_followup: 1};
        if (jobExtra && singleJobTasks[tsTask] && r.job_id) {
          jobExtra.style.display = '';
          var jLink = document.getElementById('ts-job-link');
          jLink.href = '/jobs/' + r.job_id;
          jLink.textContent = 'View job →';
          jLink.style.display = '';

          var resultTitle = document.getElementById('ts-job-result-title');
          var resultText  = document.getElementById('ts-job-result-text');
          var body = '';
          if (tsTask === 'score_fit') {
            resultTitle.textContent = 'Fit score: ' + r.score + '/10';
            body = r.reason || '';
          } else if (tsTask === 'draft_followup') {
            resultTitle.textContent = 'Follow-up draft';
            body = r.email_text || '';
          } else if (tsTask === 'ats_gap') {
            resultTitle.textContent = 'ATS match estimate: ' + (r.overall_match_estimate || 'N/A');
            body = (r.missing_count || 0) + ' missing keyword(s) identified — see the job page for the full breakdown.';
          }
          if (body) {
            resultText.textContent = body;
            document.getElementById('ts-job-result-wrap').style.display = '';
          }
        }
      } else if (status === 'error') {
        heading.style.color = 'var(--color-danger, #e05555)';
        summary.textContent = 'Error: ' + (data.error || 'unknown');
        document.title = 'Error — ' + heading.textContent.trim();
      } else {
        summary.textContent = 'Status not found — task may have already completed.';
        document.title = 'Unknown';
      }
    }

    function tsPoll() {
      fetch(pollUrl, {credentials: 'same-origin'})
        .then(function(r) {
          if (r.status === 404) { tsFinish('not_found', {}); return null; }
          return r.json();
        })
        .then(function(data) {
          if (!data) return;
          if (data.logs) tsAppendLogs(data.logs);
          if (data.status === 'running') {
            summary.textContent = 'Running… ' + tsElapsedText() + ' elapsed · ' +
              (data.logs ? data.logs.length : 0) + ' log line(s)';
          } else {
            tsFinish(data.status, data);
          }
        })
        .catch(function() {
          // Network hiccup — still show elapsed time so the page doesn't look frozen.
          summary.textContent = 'Running… ' + tsElapsedText() + ' elapsed (retrying connection…)';
        });
    }

    tsTimer = setInterval(tsPoll, 2000);
    // Separate 1s ticker so the elapsed time visibly moves between polls —
    // proof the page itself is alive even when the backend has nothing new to report.
    tsTickTimer = setInterval(function() {
      if (tsRunning) summary.textContent = summary.textContent.replace(
        /^Running… .*? elapsed/, 'Running… ' + tsElapsedText() + ' elapsed');
    }, 1000);
    tsPoll();
  }

  // -----------------------------------------------------------------------
  // Triage batch live run (triage_batch.html — run_id present)
  // Polls the same /ai/task/<run_id>/poll endpoint as the task-status page,
  // but renders a results table inline and shows the next-batch form.
  // -----------------------------------------------------------------------
  var trRoot = document.getElementById('triage-run-root');
  if (trRoot) {
    var trPollUrl  = trRoot.getAttribute('data-poll-url');
    var trJobBase  = trRoot.getAttribute('data-job-url-base') || '/jobs/';
    var trLogBox   = document.getElementById('tr-log-box');
    var trSummary  = document.getElementById('tr-run-summary');
    var trSpinner  = document.getElementById('tr-spinner');
    var trHeading  = document.getElementById('tr-heading-text');
    var trResults  = document.getElementById('tr-results-card');
    var trBody     = document.getElementById('tr-result-body');
    var trMeta     = document.getElementById('tr-result-meta');
    var trNextCard = document.getElementById('tr-next-card');
    var trNextOff  = document.getElementById('tr-next-offset');
    var trRemLabel = document.getElementById('tr-remaining-label');
    var trDoneCard = document.getElementById('tr-all-done-card');
    var trSeen     = 0;
    var trTimer;

    function trAppendLogs(lines) {
      for (var i = trSeen; i < lines.length; i++) {
        var span = document.createElement('span');
        span.className = 'log-line';
        var lvl = lines[i].split(' ')[0].toLowerCase().replace(':', '');
        if (lvl === 'error' || lvl === 'warning') {
          span.className += ' level-' + lvl;
        }
        span.textContent = lines[i];
        trLogBox.appendChild(span);
        trLogBox.appendChild(document.createTextNode('\n'));
      }
      trSeen = lines.length;
      trLogBox.scrollTop = trLogBox.scrollHeight;
    }

    function trScoreBadge(r) {
      if (!r.ok) {
        return '<span class="badge badge-danger" title="' + trEsc(r.reason) + '">fail</span>';
      }
      var cls = r.score >= 7 ? 'success' : (r.score >= 4 ? 'info' : 'secondary');
      return '<span class="badge badge-' + cls + '" title="' + trEsc(r.reason) + '">' + r.score + '/10</span>';
    }

    function trEsc(s) {
      return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function trFinish(data) {
      clearInterval(trTimer);
      if (trSpinner) trSpinner.classList.add('hidden');

      var r = (data && data.result) || {};

      if (data && data.status === 'done') {
        trHeading.textContent = 'Batch complete';
        var parts = [];
        if (r.scored  !== undefined) parts.push(r.scored + ' scored');
        if (r.failed  !== undefined && r.failed > 0) parts.push(r.failed + ' failed');
        if (r.total_remaining !== undefined) parts.push(r.total_remaining + ' remaining');
        trSummary.textContent = parts.join(' · ');

        // Build results table
        var rows = r.results || [];
        if (rows.length) {
          var html = '';
          for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var reasonCell = trEsc(row.reason || '');
            html += '<tr>'
              + '<td>' + trScoreBadge(row) + '</td>'
              + '<td><a href="' + trJobBase + row.id + '">' + trEsc(row.title) + '</a></td>'
              + '<td>' + trEsc(row.company) + '</td>'
              + '<td class="muted small" style="max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + reasonCell + '">' + reasonCell + '</td>'
              + '</tr>';
          }
          trBody.innerHTML = html;
          var metaParts = [];
          if (r.scored !== undefined) metaParts.push(r.scored + ' scored');
          if (r.failed !== undefined && r.failed > 0) metaParts.push(r.failed + ' failed');
          if (r.total_remaining !== undefined) metaParts.push(r.total_remaining + ' remaining');
          trMeta.textContent = metaParts.join(' · ');
          trResults.style.display = '';
        }

        // Show next-batch form or all-done card
        var remaining = (r.total_remaining !== undefined) ? r.total_remaining : -1;
        if (remaining > 0) {
          if (trNextOff) trNextOff.value = r.next_offset || 0;
          if (trRemLabel) trRemLabel.textContent = remaining + ' unscored job' + (remaining !== 1 ? 's' : '') + ' remaining';
          trNextCard.style.display = '';
        } else if (remaining === 0) {
          trDoneCard.style.display = '';
        }

      } else {
        // error or not_found
        trHeading.textContent = 'Batch failed';
        trHeading.style.color = 'var(--color-danger, #e05555)';
        trSummary.textContent = (data && data.error) ? 'Error: ' + data.error : 'Unknown error.';
      }
    }

    function trPoll() {
      fetch(trPollUrl, {credentials: 'same-origin'})
        .then(function(resp) {
          if (resp.status === 404) { trFinish({status: 'not_found'}); return null; }
          return resp.json();
        })
        .then(function(data) {
          if (!data) return;
          if (data.logs) trAppendLogs(data.logs);
          if (data.status === 'running') {
            trSummary.textContent = 'Running… (' + (data.logs ? data.logs.length : 0) + ' log lines)';
          } else {
            trFinish(data);
          }
        })
        .catch(function() { /* network hiccup, retry */ });
    }

    trTimer = setInterval(trPoll, 2000);
    trPoll();
  }

  // -----------------------------------------------------------------------
  // Kit batch live run (kit_batch.html — run_id present)
  // Polls /ai/task/<run_id>/poll and renders a per-job results table.
  // -----------------------------------------------------------------------
  var kbRoot = document.getElementById('kit-run-root');
  if (kbRoot) {
    var kbPollUrl   = kbRoot.getAttribute('data-poll-url');
    var kbJobBase   = kbRoot.getAttribute('data-job-url-base') || '/jobs/';
    var kbLogBox    = document.getElementById('kb-log-box');
    var kbSummary   = document.getElementById('kb-run-summary');
    var kbSpinner   = document.getElementById('kb-spinner');
    var kbHeading   = document.getElementById('kb-heading-text');
    var kbResults   = document.getElementById('kb-results-card');
    var kbBody      = document.getElementById('kb-result-body');
    var kbMeta      = document.getElementById('kb-result-meta');
    var kbDoneCard  = document.getElementById('kb-all-done-card');
    var kbPartCard  = document.getElementById('kb-partial-done-card');
    var kbPartMsg   = document.getElementById('kb-partial-msg');
    var kbSeen      = 0;
    var kbTimer;

    function kbEsc(s) {
      return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function kbAppendLogs(lines) {
      for (var i = kbSeen; i < lines.length; i++) {
        var span = document.createElement('span');
        span.className = 'log-line';
        var lvl = lines[i].split(' ')[0].toLowerCase();
        if (lvl === 'error' || lvl === 'warning') span.className += ' level-' + lvl;
        span.textContent = lines[i];
        kbLogBox.appendChild(span);
        kbLogBox.appendChild(document.createTextNode('\n'));
      }
      kbSeen = lines.length;
      kbLogBox.scrollTop = kbLogBox.scrollHeight;
    }

    function kbFinish(data) {
      clearInterval(kbTimer);
      if (kbSpinner) kbSpinner.classList.add('hidden');

      var r = (data && data.result) || {};

      if (data && data.status === 'done') {
        kbHeading.textContent = 'Batch complete';
        var parts = [];
        if (r.built  !== undefined) parts.push(r.built + ' built');
        if (r.failed !== undefined && r.failed > 0) parts.push(r.failed + ' failed');
        kbSummary.textContent = parts.join(' · ');

        var rows = r.results || [];
        if (rows.length) {
          var html = '';
          for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var badge = row.ok
              ? '<span class="badge badge-success">built</span>'
              : '<span class="badge badge-danger" title="' + kbEsc(row.error) + '">failed</span>';
            var viewLink = row.ok
              ? '<a href="' + kbJobBase + row.id + '">View kit &rarr;</a>'
              : '';
            html += '<tr>'
              + '<td>' + badge + '</td>'
              + '<td><a href="' + kbJobBase + row.id + '">' + kbEsc(row.title) + '</a></td>'
              + '<td>' + kbEsc(row.company) + '</td>'
              + '<td>' + viewLink + '</td>'
              + '</tr>';
          }
          kbBody.innerHTML = html;
          kbMeta.textContent = parts.join(' · ');
          kbResults.style.display = '';
        }

        if ((r.failed || 0) === 0) {
          kbDoneCard.style.display = '';
        } else {
          var msg = (r.built || 0) + ' kit' + (r.built !== 1 ? 's' : '') + ' built';
          if (r.failed) msg += ', ' + r.failed + ' failed — check the log for details.';
          kbPartMsg.textContent = msg;
          kbPartCard.style.display = '';
        }

      } else {
        kbHeading.textContent = 'Batch failed';
        kbHeading.style.color = 'var(--color-danger, #e05555)';
        kbSummary.textContent = (data && data.error) ? 'Error: ' + data.error : 'Unknown error.';
      }
    }

    function kbPoll() {
      fetch(kbPollUrl, {credentials: 'same-origin'})
        .then(function(resp) {
          if (resp.status === 404) { kbFinish({status: 'not_found'}); return null; }
          return resp.json();
        })
        .then(function(data) {
          if (!data) return;
          if (data.logs) kbAppendLogs(data.logs);
          if (data.status === 'running') {
            kbSummary.textContent = 'Running… (' + (data.logs ? data.logs.length : 0) + ' log lines)';
          } else {
            kbFinish(data);
          }
        })
        .catch(function() { /* network hiccup, retry */ });
    }

    kbTimer = setInterval(kbPoll, 2000);
    kbPoll();
  }

  // SerpApi / Google Jobs monthly query calculator.
  // Looks for a div[data-serpapi-calc] inside the googlejobs provider card.
  // data-pages = ceil(results_per_query / 10) passed from the template.
  var serpCalcEl = document.querySelector('[data-provider="googlejobs"] [data-serpapi-calc]');
  if (serpCalcEl) {
    var serpPages = parseInt(serpCalcEl.getAttribute('data-pages'), 10) || 3;
    var serpForm = serpCalcEl.closest('form');
    var serpRunsInput  = serpForm ? serpForm.querySelector('[name="max_runs_per_day"]')  : null;
    var serpTitlesInput = serpForm ? serpForm.querySelector('[name="max_titles_per_run"]') : null;
    var serpDisplay = document.getElementById('serpapi-monthly-estimate');

    function updateSerpCalc() {
      if (!serpDisplay) return;
      var runs   = Math.max(1, parseInt(serpRunsInput  && serpRunsInput.value,  10) || 1);
      var titles = Math.max(1, parseInt(serpTitlesInput && serpTitlesInput.value, 10) || 1);
      var monthly = runs * titles * serpPages * 22;
      serpDisplay.textContent = '≈ ' + monthly + ' queries/month (22 weekdays × '
        + runs + ' run' + (runs !== 1 ? 's' : '') + '/day × '
        + titles + ' title' + (titles !== 1 ? 's' : '') + ' × '
        + serpPages + ' page' + (serpPages !== 1 ? 's' : '') + ')';
      var warn = monthly > 250 ? 'var(--danger,#dc2626)'
               : monthly > 200 ? 'var(--warning-text,#92400e)'
               : 'var(--success-text,#166534)';
      serpDisplay.style.color = warn;
    }

    if (serpRunsInput)   serpRunsInput.addEventListener('input',  updateSerpCalc);
    if (serpTitlesInput) serpTitlesInput.addEventListener('input', updateSerpCalc);
    updateSerpCalc();
  }
});
