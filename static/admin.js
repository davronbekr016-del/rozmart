/* Административная панель ROZMART: товары (SA-01) и заказы (SA-04).
 *
 * Открывается из Telegram: подпись оттуда уходит заголовком X-Telegram-Init-Data,
 * тем же, что и в витрине. Сервер сверяет её и проверяет, что Telegram-id есть
 * в списке администраторов. Посторонний получает 404, а не 403 — чтобы по ответу
 * нельзя было понять, что панель вообще существует.
 */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  var state = { view: 'products', status: 'new', q: '', category: '',
                orderStatus: '', statuses: {} };
  var timer = null;

  // ------------------------------------------------------------------ запросы

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (tg && tg.initData) opts.headers['X-Telegram-Init-Data'] = tg.initData;
    return fetch('/api/admin' + path, opts).then(function (r) {
      if (r.status === 404 && !opts.quiet) {
        throw new Error('Нет доступа. Ваш Telegram не в списке администраторов.');
      }
      if (r.status === 401) {
        // из браузера показываем форму входа, из Telegram — объясняем словами:
        // там формы быть не должно, подпись уже есть, дело в списке админов
        if (!(tg && tg.initData)) { location.href = '/admin/login'; }
        throw new Error('Нет доступа к панели.');
      }
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body.detail || ('Ошибка ' + r.status));
        return body;
      });
    });
  }

  function say(text, kind) {
    var box = document.getElementById('msg');
    if (!text) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="msg ' + (kind || 'err') + '"></div>';
    box.firstChild.textContent = text;
    if (kind === 'ok') setTimeout(function () { box.innerHTML = ''; }, 2500);
  }

  function money(v) {
    return v == null ? '—' : String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' сум';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // textContent, не innerHTML: в названиях из REGOS
    return e;                                  // встречается что угодно, включая угловые скобки
  }

  // ------------------------------------------------------------------ товары

  function loadProducts() {
    var url = '/products?status=' + state.status + '&limit=200';
    if (state.q) url += '&q=' + encodeURIComponent(state.q);
    if (state.category) url += '&category_id=' + state.category;
    api(url).then(renderProducts).catch(function (e) { say(e.message); });
    api('/products/count').then(function (c) {
      document.getElementById('cntProducts').textContent = c.hidden + ' / ' + c.total;
    }).catch(function () {});
  }

  function renderProducts(rows) {
    var box = document.getElementById('products');
    box.innerHTML = '';

    var at = document.getElementById('productsAt');
    if (at) {
      at.textContent = 'обновлено в ' + new Date().toLocaleTimeString('ru-RU',
        { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' · товаров ' + rows.length;
    }

    if (!rows.length) {
      box.appendChild(el('div', 'empty', 'Ничего не найдено'));
      return;
    }
    rows.forEach(function (p) {
      var card = el('div', 'card');
      if (p.photo) {
        var img = document.createElement('img');
        img.src = p.photo; img.alt = '';
        card.appendChild(img);
      } else {
        card.appendChild(el('div', 'ph', '—'));
      }
      var info = el('div');
      info.appendChild(el('div', 'nm', p.name));

      var meta = el('div', 'meta');
      meta.appendChild(el('span', 'pill ' + (p.is_active ? 'ok' : 'no'),
        p.is_active ? 'на витрине' : 'скрыт'));
      if (!p.sellable) meta.appendChild(el('span', 'pill warn', 'нет цены'));
      info.appendChild(meta);

      info.appendChild(el('div', 'meta',
        p.category + ' · фасовок ' + p.variant_count +
        (p.min_price != null ? ' · от ' + money(p.min_price) : '')));
      card.appendChild(info);
      card.onclick = function () { openProduct(p.id); };
      box.appendChild(card);
    });
  }

  // Отбор по категории. Список тянем один раз: категорий единицы, и дёргать
  // сервер на каждое открытие вкладки незачем.
  function loadCategoryFilter() {
    api('/categories').then(function (cats) {
      var sel = document.getElementById('catFilter');
      if (!sel) return;
      var current = state.category;
      sel.innerHTML = '';
      var all = document.createElement('option');
      all.value = ''; all.textContent = 'Все категории';
      sel.appendChild(all);
      cats.forEach(function (c) {
        var o = document.createElement('option');
        o.value = c.id;
        o.textContent = c.name + ' (' + c.product_count + ')';
        if (String(c.id) === String(current)) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function () {});   // без фильтра список товаров всё равно работает
  }

  function openProduct(id) {
    Promise.all([api('/products/' + id), api('/categories')])
      .then(function (r) { renderProduct(r[0], r[1]); })
      .catch(function (e) { say(e.message); });
  }

  // Фасовка с ценой. Своя цена действует вместо цены REGOS, и синхронизация
  // её не трогает — поэтому рядом всегда видно, сколько стоит в REGOS:
  // иначе забытая своя цена разойдётся с кассой, и никто не заметит
  function variantRow(productId, v) {
    var row = el('div');
    row.style.cssText = 'padding:9px 0;border-bottom:1px solid #F0F0F0';

    var top = el('div');
    top.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap';
    top.appendChild(el('span', null, v.weight));
    var input = document.createElement('input');
    input.type = 'number'; input.min = '1'; input.step = '1';
    input.style.cssText = 'max-width:130px';
    input.value = v.price != null ? v.price : '';
    input.placeholder = v.regos_price != null ? String(v.regos_price) : 'цена';
    top.appendChild(input);
    top.appendChild(el('span', 'meta', 'сум'));

    var save = el('button', 'act ghost', 'Сохранить цену');
    save.onclick = function () {
      var value = input.value.trim();
      if (!value) return say('Укажите цену или верните цену REGOS');
      setPrice(v.id, Number(value));
    };
    top.appendChild(save);
    if (v.manual_price != null) {
      var reset = el('button', 'act ghost', 'Вернуть цену REGOS');
      reset.onclick = function () { setPrice(v.id, null); };
      top.appendChild(reset);
    }
    row.appendChild(top);

    var source = v.manual_price != null
      ? 'своя цена · в REGOS ' + (v.regos_price != null ? money(v.regos_price) : 'цены нет')
      : (v.regos_price != null ? 'цена REGOS' : 'в REGOS цены нет — фасовка не продаётся');
    row.appendChild(el('div', 'meta', source + ' · код ' + v.external_code +
      (v.barcode ? ' · ш/к ' + v.barcode : '') + (v.is_active ? '' : ' · скрыта')));
    return row;
  }

  function setPrice(variantId, price) {
    api('/variants/' + variantId + '/price', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price: price })
    }).then(function (p) {
      say(price == null ? 'Вернули цену REGOS' : 'Цена сохранена', 'ok');
      loadProducts();
      return api('/categories').then(function (cats) { renderProduct(p, cats); });
    }).catch(function (e) { say(e.message); });
  }

  function renderProduct(p, categories) {
    document.getElementById('paneTtl').textContent = 'Карточка товара';
    var body = document.getElementById('paneBody');
    body.innerHTML = '';

    function field(label, node) {
      var f = el('div', 'fld');
      f.appendChild(el('label', null, label));
      f.appendChild(node);
      body.appendChild(f);
      return node;
    }

    var name = document.createElement('input');
    name.value = p.name;
    field('Название для покупателя', name);

    var cat = document.createElement('select');
    categories.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.id; o.textContent = c.name;
      if (c.id === p.category_id) o.selected = true;
      cat.appendChild(o);
    });
    field('Категория', cat);

    var desc = document.createElement('textarea'); desc.value = p.description || '';
    field('Описание', desc);
    var comp = document.createElement('textarea'); comp.value = p.composition || '';
    field('Состав', comp);
    var life = document.createElement('input'); life.value = p.shelf_life || '';
    field('Срок годности', life);

    // Фото: видно текущее, иначе администратор не знает, что заменяет.
    // Адрес приходит с отметкой версии, поэтому после замены показывается
    // новая картинка, а не та, что осела в кеше браузера.
    var photoBox = el('div');
    photoBox.style.cssText = 'display:flex;gap:12px;align-items:flex-start';

    var preview = document.createElement('img');
    preview.style.cssText = 'width:96px;height:96px;object-fit:cover;border-radius:10px;' +
      'background:#F2F2F2;flex:none;border:1px solid #ECECEC';
    if (p.photo) {
      preview.src = p.photo;
    } else {
      preview.replaceWith();
      preview = el('div');
      preview.style.cssText = 'width:96px;height:96px;border-radius:10px;background:#F2F2F2;' +
        'flex:none;display:flex;align-items:center;justify-content:center;color:#BBB;font-size:12px';
      preview.textContent = 'нет фото';
    }
    photoBox.appendChild(preview);

    var side = el('div');
    side.style.cssText = 'flex:1;min-width:0';
    var photo = document.createElement('input');
    photo.type = 'file'; photo.accept = 'image/jpeg,image/png,image/webp';
    side.appendChild(photo);
    side.appendChild(el('div', 'meta', 'JPEG, PNG или WebP, до 4 МБ'));

    if (p.photo) {
      var drop = el('button', 'act ghost', 'Удалить фото');
      drop.style.marginTop = '8px';
      drop.onclick = function () {
        if (!window.confirm('Убрать фотографию с карточки?')) return;
        api('/products/' + p.id + '/photo', { method: 'DELETE' })
          .then(function () { say('Фото удалено', 'ok'); openProduct(p.id); loadProducts(); })
          .catch(function (e) { say(e.message); });
      };
      side.appendChild(drop);
    }
    photoBox.appendChild(side);
    field('Фотография', photoBox);

    photo.onchange = function () {
      if (!photo.files.length) return;
      var fd = new FormData();
      fd.append('file', photo.files[0]);
      say('Загружаем…', 'ok');
      api('/products/' + p.id + '/photo', { method: 'POST', body: fd })
        .then(function () { say('Фото обновлено', 'ok'); openProduct(p.id); loadProducts(); })
        .catch(function (e) { say(e.message); });
    };

    // Фасовки. Показываем код REGOS — администратору он нужен, чтобы сверяться
    // с учётной системой. Покупателю этот код не показывается нигде (BR-36).
    var vbox = el('div');
    p.variants.forEach(function (v) { vbox.appendChild(variantRow(p.id, v)); });
    field('Фасовки из REGOS (' + p.variants.length + ')', vbox);

    var moveTo = document.createElement('input');
    moveTo.placeholder = 'id карточки-получателя';
    if (p.variants.length) {
      field('Перенести первую фасовку в другую карточку', moveTo);
      var moveBtn = el('button', 'act ghost', 'Перенести ' + p.variants[0].weight);
      moveBtn.onclick = function () {
        if (!moveTo.value) return say('Укажите id карточки');
        api('/variants/' + p.variants[0].id + '/move?target_product_id=' + moveTo.value,
            { method: 'POST' })
          .then(function () { say('Перенесено', 'ok'); closePane(); loadProducts(); })
          .catch(function (e) { say(e.message); });
      };
      body.appendChild(moveBtn);
    }

    body.appendChild(el('div', 'meta', 'id карточки: ' + p.id));

    var foot = document.getElementById('paneFoot');
    foot.innerHTML = '';
    var save = el('button', 'act', 'Сохранить');
    save.onclick = function () {
      api('/products/' + p.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.value.trim(), category_id: Number(cat.value),
          description: desc.value.trim() || null, composition: comp.value.trim() || null,
          shelf_life: life.value.trim() || null
        })
      }).then(function () { say('Сохранено', 'ok'); closePane(); loadProducts(); })
        .catch(function (e) { say(e.message); });
    };
    foot.appendChild(save);

    var pub = el('button', 'act ' + (p.is_active ? 'ghost' : ''),
      p.is_active ? 'Снять с витрины' : 'Опубликовать');
    pub.disabled = !p.is_active && !p.sellable;
    pub.title = pub.disabled ? 'Нет цены ни у одной фасовки' : '';
    pub.onclick = function () {
      api('/products/' + p.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !p.is_active })
      }).then(function () {
        say(p.is_active ? 'Снято с витрины' : 'Опубликовано', 'ok');
        closePane(); loadProducts();
      }).catch(function (e) { say(e.message); });
    };
    foot.appendChild(pub);

    openPane();
  }

  // ------------------------------------------------------------------ заказы

  function loadOrders() {
    var url = '/orders?limit=100';
    if (state.orderStatus) url += '&status=' + state.orderStatus;
    if (state.q) url += '&q=' + encodeURIComponent(state.q);
    api(url).then(renderOrders).catch(function (e) { say(e.message); });
  }

  function renderOrders(rows) {
    var body = document.getElementById('orders');
    body.innerHTML = '';

    // Отметка времени: по ней видно, насколько список свежий. Без неё после
    // нажатия «Обновить» непонятно, отработало оно или нет — особенно когда
    // ничего не изменилось.
    var at = document.getElementById('ordersAt');
    if (at) {
      at.textContent = 'обновлено в ' + new Date().toLocaleTimeString('ru-RU',
        { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' · заказов ' + rows.length;
    }

    if (!rows.length) {
      var tr = el('tr'); var td = el('td', 'empty', 'Заказов нет');
      td.colSpan = 7; tr.appendChild(td); body.appendChild(tr);
      return;
    }
    rows.forEach(function (o) {
      var tr = el('tr', 'row');
      [o.number,
       new Date(o.created_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }),
       o.customer_name + ' · ' + o.phone,
       String(o.item_count),
       money(o.total),
       o.status_text,
       o.regos_document_id ? '№ ' + o.regos_document_id : '—'
      ].forEach(function (v) { tr.appendChild(el('td', null, v)); });
      tr.onclick = function () { openOrder(o.id); };
      body.appendChild(tr);
    });
  }

  function renderOrderTabs() {
    var box = document.getElementById('orderTabs');
    box.innerHTML = '';
    var all = el('button', 'tab' + (state.orderStatus ? '' : ' on'), 'Все');
    all.onclick = function () { state.orderStatus = ''; renderOrderTabs(); loadOrders(); };
    box.appendChild(all);
    Object.keys(state.statuses).forEach(function (code) {
      var b = el('button', 'tab' + (state.orderStatus === code ? ' on' : ''),
                 state.statuses[code]);
      b.onclick = function () { state.orderStatus = code; renderOrderTabs(); loadOrders(); };
      box.appendChild(b);
    });
  }

  // Про деньги — без полутонов: «онлайн» без слова «оплачено» кассир
  // прочитал бы как «оплатит онлайн когда-нибудь»
  function paymentText(o) {
    if (o.payment_method === 'cash') return 'наличные курьеру';
    if (!o.paid_at) {
      return o.status === 'CANCELED' ? 'картой — не оплачен, заказ отменён'
                                     : 'картой — ждёт оплаты';
    }
    var when = new Date(o.paid_at).toLocaleString('ru-RU');
    return 'картой — ОПЛАЧЕНО ' + when + ', ' + money(o.paid_amount)
      + ' · платёж ' + (o.provider_charge_id || o.payment_charge_id);
  }

  function openOrder(id) {
    api('/orders/' + id).then(function (o) {
      document.getElementById('paneTtl').textContent = 'Заказ ' + o.number;
      var body = document.getElementById('paneBody');
      body.innerHTML = '';

      [['Состояние', o.status_text],
       ['В REGOS', o.regos_document_id
          ? 'документ № ' + o.regos_document_id
          : (o.regos_error ? 'не выгружен: ' + o.regos_error
                           : 'ещё не выгружен (попыток ' + o.regos_attempts + ')')],
       ['Покупатель', o.customer_name],
       ['Телефон', o.phone],
       // @имя ссылкой: оператору по нему писать, а не читать его
       ['Telegram', o.telegram_username ? '@' + o.telegram_username : '—',
        o.telegram_username ? 'https://t.me/' + o.telegram_username : null],
       ['Адрес', o.address],
       // точка с карты: по ней курьер находит дом, по адресу — подъезд
       ['На карте', o.lat != null ? o.lat.toFixed(6) + ', ' + o.lon.toFixed(6) : 'не указана',
        o.lat != null ? 'https://maps.google.com/?q=' + o.lat + ',' + o.lon : null],
       ['Доставка', o.delivery_slot],
       ['Оплата', paymentText(o)],
       ['Комментарий', o.comment || '—'],
       ['Оформлен', new Date(o.created_at).toLocaleString('ru-RU')]
      ].forEach(function (pair) {
        var f = el('div', 'fld');
        f.appendChild(el('label', null, pair[0]));
        if (pair[2]) {
          var a = el('a', null, pair[1]);
          a.href = pair[2];
          a.target = '_blank';
          a.rel = 'noopener';
          f.appendChild(a);
        } else {
          f.appendChild(el('div', null, pair[1]));
        }
        body.appendChild(f);
      });

      var t = document.createElement('table');
      t.innerHTML = '<thead><tr><th>Товар</th><th>Фасовка</th><th>Кол-во</th><th>Сумма</th></tr></thead>';
      var tb = document.createElement('tbody');
      o.items.forEach(function (i) {
        var tr = el('tr');
        [i.product_name, i.weight, String(i.quantity), money(i.sum)]
          .forEach(function (v) { tr.appendChild(el('td', null, v)); });
        tb.appendChild(tr);
      });
      var tr = el('tr');
      tr.appendChild(el('td', null, 'Доставка')); tr.appendChild(el('td'));
      tr.appendChild(el('td')); tr.appendChild(el('td', null, money(o.delivery_price)));
      tb.appendChild(tr);
      var total = el('tr');
      total.appendChild(el('td', null, 'Итого')); total.appendChild(el('td'));
      total.appendChild(el('td')); total.appendChild(el('td', null, money(o.total)));
      tb.appendChild(total);
      t.appendChild(tb);
      body.appendChild(t);

      // Кнопки только для разрешённых переходов: сервер всё равно проверит,
      // но показывать заведомо невозможное действие — врать оператору
      var foot = document.getElementById('paneFoot');
      foot.innerHTML = '';

      // Ручная отправка — на случай, когда автоматическая исчерпала попытки
      // или оператор устранил причину отказа
      // неоплаченный заказ картой уйдёт в REGOS сам, когда придёт оплата;
      // кнопка здесь была лазейкой отдать его кассиру без денег
      if (!o.regos_document_id && !o.awaiting_payment && o.status !== 'CANCELED') {
        var push = el('button', 'act ghost', 'Отправить в REGOS');
        push.onclick = function () {
          push.disabled = true;
          push.textContent = 'Отправляем…';
          api('/orders/' + o.id + '/push', { method: 'POST' })
            .then(function (r) {
              say('Выгружен, документ № ' + r.document_id, 'ok');
              closePane(); loadOrders();
            })
            .catch(function (e) {
              say(e.message);
              push.disabled = false;
              push.textContent = 'Отправить в REGOS';
            });
        };
        foot.appendChild(push);
      }

      (state.transitions[o.status] || []).forEach(function (code) {
        // сервер всё равно откажет, но показывать заведомо невозможное — врать
        if (code === 'CONFIRMED' && o.awaiting_payment) return;
        var b = el('button', 'act ' + (code === 'CANCELED' ? 'ghost' : ''),
                   state.statuses[code]);
        b.onclick = function () {
          if (code === 'CANCELED' && o.checkout_open && !confirm(
              'Покупатель прямо сейчас в окне оплаты. Если деньги успеют списаться, '
              + 'заказ останется отменённым и понадобится возврат. Всё равно отменить?')) {
            return;
          }
          api('/orders/' + o.id + '/status', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: code })
          }).then(function () { say('Состояние изменено', 'ok'); closePane(); loadOrders(); })
            .catch(function (e) { say(e.message); });
        };
        foot.appendChild(b);
      });

      // Удаление — необратимое действие, поэтому стоит последним, выглядит
      // неприметно и спрашивает подтверждение с номером заказа: нажать его
      // вместо соседней кнопки не должно быть легко
      var del = el('button', 'act ghost', 'Удалить');
      del.style.marginLeft = 'auto';
      del.style.color = '#B3050F';
      del.onclick = function () {
        if (!confirm('Удалить заказ ' + o.number + '?\n\n'
                     + 'Заказ исчезнет из витрины вместе с составом и суммой, '
                     + 'а в REGOS документ будет отменён. Отменить удаление нельзя.')) {
          return;
        }
        del.disabled = true;
        del.textContent = 'Удаляем…';
        api('/orders/' + o.id, { method: 'DELETE' })
          .then(function (r) {
            say('Заказ ' + r.deleted + ' удалён — ' + r.regos, 'ok');
            closePane(); loadOrders();
          })
          .catch(function (e) {
            say(e.message);
            del.disabled = false;
            del.textContent = 'Удалить';
          });
      };
      foot.appendChild(del);
      openPane();
    }).catch(function (e) { say(e.message); });
  }


  // ------------------------------------------------------------- состав каталога

  function loadSettings() {
    api('/categories').then(renderCats).catch(function (e) { say(e.message); });
    api('/regos/groups').then(renderGroups).catch(function (e) { say(e.message); });
    api('/regos/auto').then(renderAuto).catch(function (e) { say(e.message); });
    loadChats();
    loadPriceTypes();
  }

  function renderAuto(data) {
    var box = document.getElementById('autoPush');
    var note = document.getElementById('autoNote');
    box.checked = data.enabled;
    // Включать нечего, пока выгрузка не настроена: молча включённая
    // автоотправка, которая ничего не делает, уже один раз стоила заказов
    box.disabled = !data.ready && !data.enabled;
    note.textContent = data.ready
      ? (data.enabled
          ? 'Заказ уходит в REGOS сам и сразу встаёт на кассу.'
          : 'Сейчас заказы передаёт оператор кнопкой в карточке заказа.')
      : 'Включить нельзя: ' + data.reason + '.';
  }

  document.getElementById('autoPush').onchange = function (e) {
    var on = e.target.checked;
    api('/regos/auto', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on })
    }).then(function () {
      say(on ? 'Автоотправка включена' : 'Автоотправка выключена', 'ok');
      loadSettings();
    }).catch(function (err) {
      e.target.checked = !on;        // возвращаем флажок: сервер отказал
      say(err.message);
      loadSettings();
    });
  };

  function when(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit',
                                       minute: '2-digit' });
  }

  // --------------------------------------------------- кому приходят заказы

  // Сюда уходит каждый заказ целиком — с телефоном и адресом покупателя.
  // Поэтому доступ включается только здесь, руками: сам себе его в боте
  // никто выдать не может.
  function renderChats(data) {
    var box = document.getElementById('chats');
    var name = document.getElementById('botName');
    if (name && data.bot) name.textContent = '@' + data.bot;
    box.innerHTML = '';

    if (!data.enabled) {
      box.appendChild(el('div', 'meta', 'Бот не настроен: не задан токен.'));
      return;
    }
    if (!data.chats.length) {
      box.appendChild(el('div', 'meta',
        'Пока боту никто не писал. Напишите ему «Старт» — и чат появится здесь.'));
      return;
    }

    data.chats.forEach(function (c) {
      var row = el('div');
      row.style.cssText = 'display:flex;gap:10px;align-items:center;margin-bottom:8px;'
        + 'max-width:640px;padding:9px 11px;border:1px solid #ECECEC;border-radius:10px';

      var who = el('div');
      who.style.cssText = 'flex:1;min-width:0';
      var title = el('div', null, (c.kind === 'group' ? '👥 ' : '') + c.title);
      title.style.fontWeight = '500';
      who.appendChild(title);
      var sub = [];
      if (c.username) sub.push('@' + c.username);
      sub.push(c.kind === 'group' ? 'группа' : 'личный чат');
      if (!c.reachable) sub.push('бот заблокирован');
      who.appendChild(el('div', 'meta', sub.join(' · ')));
      row.appendChild(who);

      var mark = el('span', 'pill ' + (c.access ? 'ok' : 'no'),
                    c.access ? 'получает заказы' : 'нет доступа');
      row.appendChild(mark);

      var btn = el('button', 'act' + (c.access ? ' ghost' : ''),
                   c.access ? 'Отключить' : 'Дать доступ');
      btn.onclick = function () {
        btn.disabled = true;
        api('/notify/chats/' + c.chat_id, { method: c.access ? 'DELETE' : 'POST' })
          .then(function () {
            say(c.access ? 'Отключён' : 'Доступ выдан', 'ok');
            loadChats();
          })
          .catch(function (e) { say(e.message); btn.disabled = false; });
      };
      row.appendChild(btn);
      box.appendChild(row);
    });
  }

  // --------------------------------------------- фотографии из REGOS

  function pullPhotos(apply) {
    var note = document.getElementById('photoNote');
    var buttons = [document.getElementById('photoCheck'), document.getElementById('photoPull')];
    buttons.forEach(function (b) { b.disabled = true; });
    note.textContent = apply ? 'Загружаем… это может занять минуту' : 'Считаем…';

    api('/regos/photos?apply=' + (apply ? 'true' : 'false'), { method: 'POST' })
      .then(function (r) {
        if (!r.applied) {
          note.textContent = r.candidates
            ? 'Карточек без фото, для которых есть картинка: ' + r.candidates
              + ' (всего картинок в REGOS ' + r.in_regos + ')'
            : 'Все карточки уже с фотографиями';
          return;
        }
        note.textContent = 'Загружено: ' + r.loaded
          + (r.failed ? ', не получилось: ' + r.failed : '');
        if (r.loaded) { say('Фотографии загружены', 'ok'); loadProducts(); }
      })
      .catch(function (e) { note.textContent = ''; say(e.message); })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  }

  // --------------------------------------------------- вид цены REGOS

  var priceState = { current: null, checked: null };

  function loadPriceTypes() {
    api('/regos/price-types').then(function (r) {
      priceState.current = r.current;
      var sel = document.getElementById('priceType');
      sel.innerHTML = '';
      r.types.forEach(function (t) {
        var o = document.createElement('option');
        o.value = t.id;
        o.textContent = t.name + (t.id === r.current ? ' — сейчас на витрине' : '');
        if (t.id === r.current) o.selected = true;
        sel.appendChild(o);
      });
      document.getElementById('priceNote').textContent = '';
      document.getElementById('priceApply').disabled = true;
    }).catch(function (e) {
      document.getElementById('priceNote').textContent = 'Виды цен не загрузились: ' + e.message;
    });
  }

  // Применять можно только то, что сначала посмотрели: в другом виде цены
  // части товаров цены может не быть, и они молча пропали бы с витрины
  function priceChange(apply) {
    var id = Number(document.getElementById('priceType').value);
    var note = document.getElementById('priceNote');
    var applyBtn = document.getElementById('priceApply');
    if (apply && priceState.checked !== id) return say('Сначала посмотрите, что изменится');
    note.textContent = apply ? 'Применяем…' : 'Считаем…';
    applyBtn.disabled = true;
    api('/regos/price-type?apply=' + (apply ? 'true' : 'false'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price_type_id: id })
    }).then(function (r) {
      var st = r.stats;
      var text = 'Товаров на витрине: ' + st.total + '. Дороже: ' + st.up +
        ', дешевле: ' + st.down + ', без изменений: ' + st.same +
        (st.manual ? ', со своей ценой (не меняются): ' + st.manual : '') +
        (st.lost ? '. ПРОПАДУТ с витрины — нет цены в этом виде: ' + st.lost : '') + '.';
      var lost = r.rows.filter(function (x) { return x.change === 'пропадёт с витрины'; })
        .slice(0, 5).map(function (x) { return x.product + ' ' + x.weight; });
      if (lost.length) text += ' Например: ' + lost.join(', ') + '.';
      if (r.applied) {
        note.textContent = 'Применено. ' + text;
        say('Вид цены переключён', 'ok');
        priceState.checked = null;
        loadPriceTypes(); loadProducts();
      } else {
        note.textContent = text;
        priceState.checked = id;
        applyBtn.disabled = id === priceState.current;
      }
    }).catch(function (e) { note.textContent = ''; say(e.message); });
  }

  function loadChats() {
    api('/notify/chats').then(renderChats).catch(function (e) { say(e.message); });
  }

  function renderCats(cats) {
    var box = document.getElementById('cats');
    box.innerHTML = '';
    cats.forEach(function (c) {
      var row = el('div');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;margin-bottom:8px;max-width:640px';

      var name = document.createElement('input');
      name.value = c.name;
      var ord = document.createElement('input');
      ord.type = 'number'; ord.value = c.sort_order; ord.style.maxWidth = '90px';

      var save = el('button', 'act ghost', 'Сохранить');
      save.onclick = function () {
        api('/categories/' + c.id, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.value.trim(), sort_order: Number(ord.value) })
        }).then(function () { say('Сохранено', 'ok'); loadSettings(); })
          .catch(function (e) { say(e.message); });
      };

      var del = el('button', 'act ghost', 'Удалить');
      del.disabled = c.product_count > 0;
      del.title = c.product_count ? 'В категории есть товары' : '';
      del.onclick = function () {
        api('/categories/' + c.id, { method: 'DELETE' })
          .then(function () { say('Удалена', 'ok'); loadSettings(); })
          .catch(function (e) { say(e.message); });
      };

      row.appendChild(name);
      row.appendChild(ord);
      row.appendChild(el('span', 'meta', c.product_count + ' тов.'));
      row.appendChild(save);
      row.appendChild(del);
      box.appendChild(row);
    });
  }

  function renderGroups(data) {
    var box = document.getElementById('groups');
    box.innerHTML = '';
    if (data.error) {
      box.appendChild(el('div', 'meta', 'REGOS недоступен: ' + data.error));
      return;
    }
    if (!data.groups.length) {
      box.appendChild(el('div', 'meta', 'Групп нет'));
      return;
    }
    data.groups.forEach(function (g) {
      var row = el('label');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;padding:5px 2px;cursor:pointer';
      var cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = g.id; cb.checked = g.selected;
      cb.style.width = 'auto';
      cb.className = 'grp';
      row.appendChild(cb);
      row.appendChild(el('span', null, g.name));
      var cnt = el('span', 'meta', g.in_catalog ? g.in_catalog + ' в витрине' : '');
      cnt.style.marginLeft = 'auto';
      row.appendChild(cnt);
      box.appendChild(row);
    });
  }

  function chosenGroups() {
    return [].slice.call(document.querySelectorAll('.grp'))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return Number(c.value); });
  }

  document.getElementById('addCat').onclick = function () {
    var input = document.getElementById('newCat');
    if (!input.value.trim()) return say('Введите название');
    api('/categories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: input.value.trim(), sort_order: 100 })
    }).then(function () { input.value = ''; say('Добавлена', 'ok'); loadSettings(); })
      .catch(function (e) { say(e.message); });
  };

  document.getElementById('saveGroups').onclick = function () {
    api('/regos/groups', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_ids: chosenGroups() })
    }).then(function (r) {
      say('Состав сохранён: групп ' + r.selected.length +
          '. Применится при следующей синхронизации.', 'ok');
    }).catch(function (e) { say(e.message); });
  };

  // Удаление необратимо, поэтому сначала показываем, сколько именно уедет,
  // и только вторым нажатием выполняем.
  document.getElementById('priceCheck').onclick = function () { priceChange(false); };
  document.getElementById('priceApply').onclick = function () { priceChange(true); };
  document.getElementById('priceType').onchange = function () {
    // выбрали другой вид — прежний предпросмотр к нему не относится
    priceState.checked = null;
    document.getElementById('priceApply').disabled = true;
    document.getElementById('priceNote').textContent = '';
  };
  document.getElementById('photoCheck').onclick = function () { pullPhotos(false); };
  document.getElementById('photoPull').onclick = function () { pullPhotos(true); };

  document.getElementById('cleanup').onclick = function () {
    api('/cleanup', { method: 'POST' }).then(function (r) {
      if (!r.count) return say('Лишнего нет', 'ok');
      var text = 'Удалить ' + r.count + ' неопубликованных карточек вне выбранных групп?'
        + '\n\nНапример: ' + r.sample.slice(0, 3).join(', ')
        + '\n\nОтменить это будет нельзя.';
      if (!window.confirm(text)) return;
      api('/cleanup?apply=true', { method: 'POST' }).then(function (d) {
        say('Удалено ' + d.removed + (d.blocked ? ', пропущено (есть заказы) ' + d.blocked : ''), 'ok');
        loadSettings();
      }).catch(function (e) { say(e.message); });
    }).catch(function (e) { say(e.message); });
  };

  // ------------------------------------------------------------------ каркас

  function openPane() {
    document.getElementById('pane').classList.add('on');
    document.getElementById('ovl').classList.add('on');
  }
  function closePane() {
    document.getElementById('pane').classList.remove('on');
    document.getElementById('ovl').classList.remove('on');
  }

  function show(view) {
    state.view = view;
    document.getElementById('viewProducts').hidden = view !== 'products';
    document.getElementById('viewOrders').hidden = view !== 'orders';
    document.getElementById('viewSettings').hidden = view !== 'settings';
    document.getElementById('q').hidden = view === 'settings';
    document.getElementById('ttl').textContent =
      { products: 'Категории и товары', orders: 'Заказы',
        settings: 'Состав каталога' }[view];
    document.querySelectorAll('.nav').forEach(function (n) {
      n.classList.toggle('on', n.dataset.go === view);
    });
    say('');
    if (view === 'products') loadProducts();
    else if (view === 'orders') loadOrders();
    else loadSettings();
  }

  document.querySelectorAll('.nav').forEach(function (n) {
    n.onclick = function () { show(n.dataset.go); };
  });
  document.querySelectorAll('.tab[data-status]').forEach(function (t) {
    t.onclick = function () {
      state.status = t.dataset.status;
      document.querySelectorAll('.tab[data-status]').forEach(function (x) {
        x.classList.toggle('on', x === t);
      });
      loadProducts();
    };
  });
  document.getElementById('catFilter').onchange = function (e) {
    state.category = e.target.value;
    loadProducts();
  };

  function refreshButton(button, action) {
    button.disabled = true;
    var was = button.textContent;
    button.textContent = 'Обновляем…';
    action();
    // кнопку возвращаем сразу: список отрисуется сам, а держать её
    // заблокированной до ответа незачем — запрос короткий
    setTimeout(function () {
      button.disabled = false;
      button.textContent = was;
    }, 400);
  }

  document.getElementById('reloadProducts').onclick = function (e) {
    refreshButton(e.target, function () {
      loadCategoryFilter();       // счётчики в категориях тоже освежаем
      loadProducts();
    });
  };

  document.getElementById('reloadOrders').onclick = function (e) {
    refreshButton(e.target, loadOrders);
  };

  document.getElementById('close').onclick = closePane;
  document.getElementById('ovl').onclick = closePane;
  document.getElementById('q').oninput = function (e) {
    state.q = e.target.value.trim();
    clearTimeout(timer);            // не дёргаем сервер на каждой букве
    timer = setTimeout(function () {
      if (state.view === 'products') loadProducts();
      else if (state.view === 'orders') loadOrders();
    }, 350);
  };

  // Если не пустили — показываем собственный id, иначе человеку неоткуда его
  // взять, а без id его не добавить в список администраторов.
  function denied(message) {
    document.querySelector('.body').innerHTML = '';
    fetch('/api/admin/whoami', {
      headers: tg && tg.initData ? { 'X-Telegram-Init-Data': tg.initData } : {}
    }).then(function (r) { return r.json(); }).then(function (me) {
      say(message + (me.id ? ' Ваш Telegram id: ' + me.id : ' ' + (me.hint || '')));
    }).catch(function () { say(message); });
  }

  // Кто вошёл и чем выйти. В Telegram выхода нет: там сессии не заводится,
  // вход подтверждается подписью при каждом запросе.
  api('/whoami', { quiet: true }).then(function (me) {
    document.getElementById('who').textContent = me.name || '';
    var out = document.getElementById('logout');
    if (me.via !== 'password') { out.hidden = true; return; }
    out.onclick = function () {
      api('/logout', { method: 'POST' })
        .then(function () { location.href = '/admin/login'; })
        .catch(function () { location.href = '/admin/login'; });
    };
  }).catch(function () {});

  api('/statuses').then(function (s) {
    state.statuses = s.statuses;
    state.transitions = s.transitions;
    renderOrderTabs();
    loadCategoryFilter();
    show('products');
  }).catch(function (e) { denied(e.message); });
})();
