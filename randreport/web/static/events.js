/* «Мероприятия» — интерактив утверждённого постера (прототип events.html).
   Источник данных — серверный публичный JSON (#events-data): демо-генератор
   makeEvents и объект months прототипа удалены. Скрипт перерисовывает только
   публичные события; закрытых дат, названий и точек здесь нет и быть не может.
   Разметка рендеров дословно совпадает с серверной (randreport/web/events_owner.py). */
'use strict';
(function () {
  var dataEl = document.getElementById('events-data');
  if (!dataEl) return;
  var DATA = JSON.parse(dataEl.textContent);
  var $ = function (q) { return document.querySelector(q); };

  var LABELS = {};
  DATA.types.forEach(function (t) { LABELS[t.key] = t.label; });
  var typeLabel = function (key) { return LABELS[key] || String(key).toUpperCase(); };

  var month = DATA.period;
  var filter = 'all';
  var selectedDay = DATA.selectedDay;
  var shown = 8;
  var STEP = 10;
  var hasDayGrid = DATA.granularity === 'day';

  var eventsFor = function () {
    return DATA.events.filter(function (ev) {
      return filter === 'all' || ev.type === filter;
    });
  };

  function plural(n) {
    if (n % 100 >= 11 && n % 100 <= 14) return n + ' публичных событий';
    switch (n % 10) {
      case 1: return n + ' публичное событие';
      case 2: case 3: case 4: return n + ' публичных события';
      default: return n + ' публичных событий';
    }
  }

  function chooseDay(day) {
    selectedDay = day;
    renderCalendar();
    renderSheet();
  }

  function renderCalendar() {
    var grid = $('#dayGrid');
    if (!grid || !hasDayGrid) return;
    grid.innerHTML = '';
    var first = DATA.first_offset;
    var maxDay = DATA.max_day;
    var limit = DATA.limit;
    var list = eventsFor();
    var perDay = {};
    list.forEach(function (ev) { perDay[ev.d] = (perDay[ev.d] || 0) + 1; });
    var typesPerDay = {};
    list.forEach(function (ev) {
      (typesPerDay[ev.d] = typesPerDay[ev.d] || []).push(ev.type);
    });
    var total = Math.ceil((first + maxDay) / 7) * 7;
    for (var i = 0; i < total; i++) {
      var day = i - first + 1;
      if (day < 1 || day > maxDay) {
        var blank = document.createElement('span');
        blank.className = 'day empty';
        blank.setAttribute('aria-hidden', 'true');
        grid.append(blank);
        continue;
      }
      var count = perDay[day] || 0;
      var isFuture = limit > 0 && day > limit;
      var b = document.createElement('button');
      b.type = 'button';
      var cls = 'day' + (selectedDay === day ? ' active' : '') + (isFuture ? ' outside' : '') + (limit > 0 && day === limit ? ' today' : '');
      b.className = cls;
      if (isFuture) b.disabled = true;
      if (selectedDay === day) b.setAttribute('aria-pressed', 'true');
      var label = day + ' ' + DATA.gen + ': ' + plural(count) + (isFuture ? ' (вне отчётного периода)' : '');
      b.setAttribute('aria-label', label);
      var html = '<span class="day-num">' + day + '</span>' + (count ? '<span class="day-count">' + count + '</span>' : '');
      var dots = (typesPerDay[day] || []).slice(0, 5).map(function (t) {
        return '<i class="dot ' + t + '" aria-hidden="true"></i>';
      }).join('');
      html += '<span class="dots">' + dots + '</span>';
      b.innerHTML = html;
      b.addEventListener('click', function (d) { return function () { chooseDay(d); }; }(day));
      grid.append(b);
    }
  }

  function renderSheet() {
    var box = $('#sheetEvents');
    var sheetDate = $('#sheetDate');
    var sheetMonth = $('#sheetMonth');
    if (!box || !hasDayGrid) return;
    sheetDate.textContent = selectedDay;
    sheetMonth.textContent = DATA.gen.toUpperCase();
    var found = eventsFor().filter(function (ev) { return ev.d === selectedDay; });
    box.innerHTML = '';
    if (!found.length) {
      var empty = document.createElement('div');
      empty.className = 'day-no-events';
      empty.textContent = filter === 'all' ? 'Публичных событий в этот день нет.' : 'Публичных событий этого типа за выбранный день нет.';
      box.append(empty);
      return;
    }
    var label = document.createElement('span');
    label.className = 'day-label';
    label.textContent = plural(found.length).toUpperCase();
    box.append(label);
    found.forEach(function (ev) {
      var b = document.createElement('button');
      b.className = 'day-event';
      b.type = 'button';
      var span = document.createElement('b');
      span.textContent = ev.title;
      var kind = document.createElement('span');
      kind.className = 'kind';
      kind.textContent = typeLabel(ev.type);
      b.append(span, kind);
      b.addEventListener('click', function () { openEvent(ev); });
      box.append(b);
    });
  }

  function renderBreakdown() {
    var box = $('#breakdown');
    if (!box) return;
    box.innerHTML = '';
    var total = DATA.types.reduce(function (sum, t) { return sum + t.count; }, 0);
    DATA.types.forEach(function (t) {
      var pct = total ? Math.round(10000 * t.count / total) / 100 : 0;
      var row = document.createElement('div');
      row.className = 'bar-row';
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = t.label;
      var track = document.createElement('div');
      track.className = 'track';
      var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'fill');
      svg.setAttribute('width', pct + '%');
      svg.setAttribute('height', '19');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', '0');
      rect.setAttribute('y', '0');
      rect.setAttribute('width', '100%');
      rect.setAttribute('height', '15');
      rect.setAttribute('class', 'fillrect fill-' + t.key);
      svg.append(rect);
      track.append(svg);
      var value = document.createElement('b');
      value.className = 'value';
      value.textContent = t.count;
      row.append(name, track, value);
      box.append(row);
    });
  }

  function renderLog() {
    var list = eventsFor().slice().sort(function (a, b) { return a.d - b.d || (a.title < b.title ? -1 : 1); });
    var count = $('#logCount');
    if (count) count.textContent = list.length ? (plural(list.length).toUpperCase() + (filter === 'all' ? ' МЕСЯЦА' : ' В ВЫБРАННОМ ФИЛЬТРЕ')) : 'ПУБЛИЧНЫЕ СОБЫТИЯ МЕСЯЦА';
    var box = $('#eventLog');
    if (!box) return;
    box.innerHTML = '';
    list.slice(0, shown).forEach(function (ev) {
      var b = document.createElement('button');
      b.className = 'log-row';
      b.type = 'button';
      var dt = document.createElement('span');
      dt.className = 'log-date';
      dt.textContent = String(ev.d).padStart(2, '0');
      var sec = document.createElement('span');
      sec.className = 'log-desc';
      var t = document.createElement('b');
      t.className = 'log-title';
      t.textContent = ev.title;
      var typ = document.createElement('span');
      typ.className = 'log-kind';
      typ.textContent = typeLabel(ev.type);
      sec.append(t, typ);
      b.append(dt, sec);
      b.addEventListener('click', function () { openEvent(ev); });
      box.append(b);
    });
    var more = $('#more');
    if (more) {
      more.hidden = list.length <= shown;
      more.textContent = 'ПОКАЗАТЬ ЕЩЁ (' + (list.length - shown) + ') ↓';
    }
  }

  function openEvent(ev) {
    var when = $('#dialogWhen');
    var title = $('#dialogTitle');
    var chip = $('#dialogType');
    var text = $('#dialogText');
    var link = $('#dialogLink');
    if (when) when.textContent = ev.d + ' ' + DATA.gen.toUpperCase() + ' ' + DATA.year + ' · ПУБЛИЧНОЕ СОБЫТИЕ';
    if (title) title.textContent = ev.title;
    if (chip) {
      chip.textContent = typeLabel(ev.type);
      chip.className = 'dialog-type paper news type-' + ev.type;
    }
    if (text) {
      if (ev.description) { text.hidden = false; text.textContent = ev.description; }
      else { text.hidden = true; text.textContent = ''; }
    }
    if (link) {
      link.innerHTML = '';
      if (ev.url) {
        var a = document.createElement('a');
        a.href = ev.url;
        a.rel = 'noopener';
        a.target = '_blank';
        a.textContent = 'опубликованный анонс ↗';
        link.hidden = false;
        link.append(a);
      } else {
        link.hidden = true;
      }
    }
    var dialog = $('#eventDialog');
    if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
  }

  function renderAll() {
    renderCalendar();
    renderSheet();
    renderBreakdown();
    renderLog();
  }

  var form = $('#periodForm');
  var select = $('#month');
  if (form && select) {
    select.addEventListener('change', function () { form.submit(); });
  }

  document.querySelectorAll('.filter').forEach(function (b) {
    b.addEventListener('click', function () {
      filter = b.dataset.filter;
      shown = 8;
      document.querySelectorAll('.filter').forEach(function (x) {
        x.setAttribute('aria-pressed', String(x === b));
      });
      renderAll();
    });
  });

  var more = $('#more');
  if (more) more.addEventListener('click', function () { shown += STEP; renderLog(); });

  var dialog = $('#eventDialog');
  if (dialog) {
    var close = $('#dialogClose');
    if (close) close.addEventListener('click', function () { dialog.close(); });
    dialog.addEventListener('click', function (e) {
      if (e.target === dialog) dialog.close();
    });
  }

  var goList = $('#goList');
  if (goList) goList.addEventListener('click', function () {
    var journal = $('#journal');
    if (journal && journal.scrollIntoView) journal.scrollIntoView({ behavior: 'smooth' });
  });

  renderAll();
})();
