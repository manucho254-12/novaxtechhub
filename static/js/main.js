(function () {
  /* theme */
  var root = document.documentElement;
  function syncTheme() {
    var dark = root.getAttribute('data-theme') === 'dark';
    document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
      b.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
      b.setAttribute('aria-pressed', dark);
    });
  }
  syncTheme();
  document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
    b.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
      syncTheme();
    });
  });

  /* back to top */
  var top = document.querySelector('.to-top');
  if (top) {
    var onScroll = function () { top.hidden = window.scrollY < 700; };
    window.addEventListener('scroll', onScroll, { passive: true }); onScroll();
  }

  /* scroll-spy: highlight the menu link for the section in view */
  var links = Array.prototype.slice.call(document.querySelectorAll('#menu a[href*="#"]:not(.btn)'));
  if (links.length && document.querySelector('.hero') && 'IntersectionObserver' in window) {
    var map = {};
    links.forEach(function (a) { map[a.getAttribute('href').split('#')[1]] = a; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          links.forEach(function (a) { a.classList.remove('on'); a.removeAttribute('aria-current'); });
          var a = map[en.target.id]; if (a) { a.classList.add('on'); a.setAttribute('aria-current', 'true'); }
        }
      });
    }, { rootMargin: '-45% 0px -50% 0px' });
    Object.keys(map).forEach(function (id) { var s = document.getElementById(id); if (s) io.observe(s); });
  }

  /* open unlock dialogs */
  document.querySelectorAll('[data-open]').forEach(function (b) {
    b.addEventListener('click', function () { var d = document.getElementById(b.dataset.open); if (d && d.showModal) { d.showModal(); var f = d.querySelector('input[type=password]'); if (f) f.focus(); } });
  });
  var openId = new URLSearchParams(location.search).get('open');
  if (openId) {
    var dlg = document.getElementById('unlock-' + openId);
    if (dlg && dlg.showModal) { dlg.showModal(); }
    history.replaceState(null, '', location.pathname + location.hash);
  }
  document.querySelectorAll('dialog.modal').forEach(function (d) {
    d.addEventListener('click', function (e) { var r = d.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close(); });
  });

  var toggle = document.querySelector('.nav-toggle');
  var menu = document.getElementById('menu');
  if (toggle && menu) {
    toggle.addEventListener('click', function () {
      var open = menu.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open);
    });
    menu.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') { menu.classList.remove('open'); toggle.setAttribute('aria-expanded', 'false'); }
    });
  }

  document.querySelectorAll('[data-toggle-pw]').forEach(function (b) {
    b.addEventListener('click', function () {
      var f = document.querySelector(b.dataset.togglePw);
      var show = f.type === 'password';
      f.type = show ? 'text' : 'password';
      b.textContent = show ? 'Hide' : 'Show';
    });
  });

  document.querySelectorAll('[data-generate]').forEach(function (b) {
    b.addEventListener('click', function () {
      var chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789', out = '';
      var len = parseInt(b.dataset.length || '10', 10); var arr = new Uint32Array(len); crypto.getRandomValues(arr);
      arr.forEach(function (n) { out += chars[n % chars.length]; });
      var f = document.querySelector(b.dataset.generate);
      f.type = 'text'; f.value = out; f.focus(); f.select();
    });
  });

  document.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      var done = function () { var t = b.textContent; b.textContent = 'Copied'; setTimeout(function () { b.textContent = t; }, 1600); };
      if (navigator.clipboard) { navigator.clipboard.writeText(b.dataset.copy).then(done); }
      else { var ta = document.createElement('textarea'); ta.value = b.dataset.copy; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done(); }
    });
  });

  document.querySelectorAll('[data-edit]').forEach(function (b) {
    b.addEventListener('click', function () { var d = document.getElementById('edit-' + b.dataset.edit); if (d && d.showModal) d.showModal(); });
  });
  document.querySelectorAll('[data-close]').forEach(function (b) {
    b.addEventListener('click', function () { b.closest('dialog').close(); });
  });

  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  var file = document.getElementById('file');
  if (file) {
    file.addEventListener('change', function () {
      var label = document.querySelector('[data-file-name]');
      label.textContent = file.files.length ? file.files[0].name : 'Choose a file';
      label.parentNode.classList.toggle('has', file.files.length > 0);
    });
  }

  /* project form: hosted / not hosted yet */
  document.querySelectorAll('fieldset.hosting').forEach(function (fs) {
    var form = fs.closest('form');
    var wrap = form.querySelector('.domain-field');
    var input = wrap && wrap.querySelector('input');
    var status = form.querySelector('input[name=status]');
    function sync() {
      var pending = form.querySelector('input[name=hosting]:checked').value === 'pending';
      if (!input) return;
      input.disabled = pending;
      wrap.classList.toggle('off', pending);
      if (pending) {
        input.value = '';
        input.placeholder = 'Not hosted yet';
        if (status && (!status.value || status.value.toLowerCase() === 'live')) status.value = 'Developed, awaiting hosting';
      } else {
        input.placeholder = 'example.com';
        if (status && status.value === 'Developed, awaiting hosting') status.value = 'Live';
      }
    }
    fs.querySelectorAll('input[name=hosting]').forEach(function (r) { r.addEventListener('change', sync); });
    sync();
  });

  /* legal forms: contracts always need a password; terms and policies may be open */
  document.querySelectorAll('[data-legal-form],[data-legal-edit]').forEach(function (form) {
    var required = (form.dataset.requiredKinds || '').split(',').filter(Boolean);
    var kind = form.querySelector('[data-legal-kind]');
    var protect = form.querySelector('[data-legal-protect]');
    var openOpt = form.querySelector('[data-open-option]');
    var block = form.querySelector('[data-pw-block]');
    var pw = form.querySelector('input[name=password]');
    var removeWrap = form.querySelector('[data-remove-pw]');
    var removeBox = removeWrap && removeWrap.querySelector('input');
    var note = form.querySelector('[data-contract-note]');
    var protectedNow = form.dataset.protected === '1';
    function sync() {
      var must = !!kind && required.indexOf(kind.value) !== -1;
      if (note) note.hidden = !must;
      if (protect) {                                   /* upload form */
        if (openOpt) openOpt.disabled = must;
        if (must) protect.value = '1';
        var open = protect.value === '0';
        if (block) block.hidden = open;
        if (pw) { pw.required = !open; pw.disabled = open; }
      } else if (pw) {                                 /* edit form */
        pw.required = must && !protectedNow;
        if (removeBox) {
          removeBox.disabled = must || !!pw.value;
          if (removeBox.disabled) removeBox.checked = false;
          removeWrap.classList.toggle('off', removeBox.disabled);
        }
      }
    }
    if (kind) kind.addEventListener('change', sync);
    if (protect) protect.addEventListener('change', sync);
    if (pw) pw.addEventListener('input', sync);
    sync();
  });

  /* Password Manager: filter the list as you type */
  document.querySelectorAll('[data-filter]').forEach(function (input) {
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      document.querySelectorAll(input.dataset.filter + ' > li').forEach(function (li) {
        li.hidden = q && (li.dataset.filterText || '').indexOf(q) === -1;
      });
    });
  });
})();
