"use strict";

// Витрина каталога, корзина и оформление заказа.
// Данные берутся из /api, вёрстка — в index.html.

const state = {
  categories: [],
  categoryId: null,   // null — показываем все товары
  product: null,      // открытая карточка
  variant: null,      // выбранная фасовка
  qty: 1,
  cart: [],           // {variantId, productId, name, weight, price, qty}
  cartNotice: "",     // что изменилось в корзине, пока её не открывали
  clientKey: null,    // ключ попытки оформления, чтобы повтор не создал второй заказ
  scrollY: 0,         // позиция каталога, чтобы вернуть её после карточки
  deliveryPrice: 0,   // приходит с сервера, чтобы итог совпал с заказом
  slot: "Как можно скорее",
  payment: "cash",
  sending: false,     // защита от повторной отправки заказа
};

// приложение сворачивают и открывают заново — корзина не должна пропадать
const CART_KEY = "rozmart.cart.v1";
const MAX_QTY = 99;   // столько же принимает сервер

const SLOTS = [
  ["Как можно скорее", "60–90 минут"],
  ["Сегодня вечером", "16:00 – 18:00"],
  ["Завтра утром", "09:00 – 12:00"],
];
const PAYMENTS = [
  ["cash", "Наличными курьеру", "ti-cash"],
  ["online", "Онлайн картой", "ti-credit-card"],
];

// каждый асинхронный запрос получает номер: ответ старого запроса не должен
// перерисовать экран поверх нового (быстрые тапы по категориям)
let requestId = 0;

const el = (id) => document.getElementById(id);

// данные приходят из админки, куда их вводит человек: экранируем всё,
// что подставляем в разметку, иначе кавычка или тег ломают страницу
const esc = (value) =>
  String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function money(value) {
  return value.toLocaleString("ru-RU").replace(/\s/g, " ") + " UZS";
}

/** Подписанные данные Telegram. Сервер проверяет подпись сам — здесь мы их
 *  только передаём, доверять клиентской копии нельзя. */
function tgHeaders() {
  const tg = window.Telegram && window.Telegram.WebApp;
  return tg && tg.initData ? { "X-Telegram-Init-Data": tg.initData } : {};
}

async function api(path) {
  const response = await fetch(path, { headers: tgHeaders() });
  if (!response.ok) {
    const error = new Error(`${path} -> ${response.status}`);
    error.status = response.status;   // 404 «товара больше нет» отличаем от обрыва связи
    throw error;
  }
  return response.json();
}

const PHOTO_STUB = `<i class="ti ti-photo" style="font-size:22px"></i>`;

function photo(url, height) {
  const inner = url ? `<img src="${esc(url)}" alt="">` : PHOTO_STUB;
  return `<div class="ph" style="height:${height}px">${inner}</div>`;
}

function fail(message, error) {
  console.error(error);
  el("products").innerHTML = `<div class="msg" style="grid-column:1/-1">${esc(message)}</div>`;
  el("list-count").textContent = "";
}

// битая картинка заменяется той же заглушкой, что и отсутствующая.
// Слушатель в фазе перехвата: событие error по картинкам не всплывает.
document.addEventListener(
  "error",
  (event) => {
    const img = event.target;
    if (img.tagName !== "IMG") return;
    // .ph — служебное место под фото, его заменяем целиком; на плитке категории
    // картинка лежит рядом с названием, там убираем только её
    if (img.parentElement && img.parentElement.classList.contains("ph")) {
      img.parentElement.innerHTML = PHOTO_STUB;
    } else {
      img.remove();
    }
  },
  true,
);

// ---------- каталог ----------

async function loadCatalog() {
  try {
    const [categories, products] = await Promise.all([
      api("/api/categories"),
      api("/api/products"),
    ]);
    state.categories = categories;

    el("categories").innerHTML = categories
      .map((c) => `
        <div class="cov" data-category="${c.id}">
          ${c.photo ? `<img src="${esc(c.photo)}" alt="">` : ""}
          <span class="n">${c.product_count}</span>
          <span class="t">${esc(c.name)}</span>
          <div class="dash"></div>
        </div>`)
      .join("");

    renderProducts(products);
  } catch (error) {
    fail("Не удалось загрузить каталог. Проверьте подключение к интернету.", error);
  }
}

function renderProducts(products) {
  el("list-count").textContent = `${products.length} ${plural(products.length)}`;
  if (!products.length) {
    el("products").innerHTML = `<div class="msg" style="grid-column:1/-1">Товаров нет</div>`;
    return;
  }
  el("products").innerHTML = products
    .map((p) => {
      const sub = p.weights.length > 1
        ? `${p.weights.length} ${plural(p.weights.length, "фасовка", "фасовки", "фасовок")}: ${p.weights.join(", ")}`
        : p.weights[0];
      const prefix = p.weights.length > 1 ? "от " : "";
      return `
        <div class="pcard" data-product="${p.id}">
          ${photo(p.photo, 100)}
          <div class="nm" style="margin-top:8px">${esc(p.name)}</div>
          <div class="sub">${esc(sub)}</div>
          <div class="row">
            <span class="prc" style="font-size:13.5px">${prefix}${money(p.min_price)}</span>
            <span class="qb" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:15px"></i></span>
          </div>
        </div>`;
    })
    .join("");
}

function plural(n, one = "товар", few = "товара", many = "товаров") {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

async function selectCategory(categoryId) {
  const next = state.categoryId === categoryId ? null : categoryId;
  const my = ++requestId;
  try {
    const products = await api(`/api/products${next ? `?category_id=${next}` : ""}`);
    if (my !== requestId) return;   // пока ждали, пользователь выбрал другое
    state.categoryId = next;
    const category = state.categories.find((c) => c.id === next);
    el("list-title").textContent = category ? category.name : "Все товары";
    renderProducts(products);
    el("list-title").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    if (my === requestId) fail("Не удалось загрузить товары категории.", error);
  }
}

// ---------- карточка товара ----------

async function openProduct(productId) {
  const my = ++requestId;
  try {
    const product = await api(`/api/products/${productId}`);
    if (my !== requestId) return;
    state.scrollY = window.scrollY;
    state.product = product;
    state.variant = product.variants[0];
    state.qty = 1;
    renderProduct();
    show("product");
  } catch (error) {
    if (my === requestId) fail("Не удалось открыть товар.", error);
  }
}

function renderProduct() {
  const p = state.product;
  const options = p.variants.length > 1
    ? `<div class="mut" style="margin:16px 0 8px">Фасовка</div>
       <div style="display:flex;gap:8px;flex-wrap:wrap">
         ${p.variants.map((v) => `
           <span class="wopt ${v.id === state.variant.id ? "on" : ""}" data-variant="${v.id}">${esc(v.weight)}</span>`).join("")}
       </div>`
    : "";

  const extra = [
    ["Категория", p.category],
    ["Состав", p.composition],
    ["Срок годности", p.shelf_life],
  ].filter(([, value]) => value);

  el("product-body").innerHTML = `
    ${photo(p.photo, 230)}
    <div class="card" style="margin-top:16px">
      <div style="font-size:19px;font-weight:600;line-height:1.3;margin-bottom:5px">${esc(p.name)}</div>
      <div class="row">
        <span class="mut">${esc(state.variant.weight)}</span>
        <span class="prc" style="font-size:21px" id="variant-price">${money(state.variant.price)}</span>
      </div>
      ${options}
      <div class="row" style="margin-top:16px">
        <span class="mut">Количество</span>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="qb" id="minus" style="background:#F1F1F1"><i class="ti ti-minus" style="font-size:15px"></i></span>
          <span style="font-size:16px;min-width:22px;text-align:center" id="qty-value">${state.qty}</span>
          <span class="qb" id="plus" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:15px"></i></span>
        </div>
      </div>
    </div>
    ${p.description ? `<div class="card"><div class="mut" style="margin-bottom:6px">Описание</div><div style="line-height:1.6">${esc(p.description)}</div></div>` : ""}
    ${extra.length ? `<div class="card" style="background:#F7F7F7">${extra.map(([k, v], i) => `
      <div class="row" ${i < extra.length - 1 ? 'style="margin-bottom:9px"' : ""}>
        <span class="mut">${esc(k)}</span><span style="font-size:13px;text-align:right">${esc(v)}</span></div>`).join("")}</div>` : ""}
  `;
  updateTotal();
}

/** Меняем только цифры: полный ре-рендер моргал бы фотографией на каждый «+». */
function updateTotal() {
  el("qty-value").textContent = state.qty;
  el("add").textContent = `В корзину · ${money(state.variant.price * state.qty)}`;
}

// ---------- корзина ----------

function persistCart() {
  try {
    localStorage.setItem(CART_KEY, JSON.stringify({ cart: state.cart, clientKey: state.clientKey }));
  } catch (error) {
    console.error(error);   // приватный режим: корзина просто не переживёт перезапуск
  }
}

/** Состав изменился — значит это уже другой заказ, и ключ прошлой попытки
 *  оформления больше не годится. */
function saveCart() {
  state.clientKey = null;
  persistCart();
}

function loadSavedCart() {
  try {
    const saved = JSON.parse(localStorage.getItem(CART_KEY) || "null");
    if (!saved || !Array.isArray(saved.cart)) return;
    state.cart = saved.cart.filter((i) => i && i.variantId && i.productId && i.qty > 0);
    state.clientKey = saved.clientKey || null;
  } catch (error) {
    console.error(error);
  }
}

function addToCart() {
  const line = state.cart.find((item) => item.variantId === state.variant.id);
  if (line) {
    line.qty = Math.min(MAX_QTY, line.qty + state.qty);
  } else {
    state.cart.push({
      variantId: state.variant.id,
      productId: state.product.id,
      name: state.product.name,
      weight: state.variant.weight,
      price: state.variant.price,
      qty: state.qty,
    });
  }
  saveCart();
  show("catalog");
}

/** Корзина лежит на телефоне и могла пролежать до изменения каталога: перед
 *  показом сверяем цены и наличие, чтобы покупатель видел то же, что и сервер. */
async function refreshCart() {
  if (!state.cart.length) return;
  const ids = [...new Set(state.cart.map((i) => i.productId))];
  let loaded;
  try {
    loaded = await Promise.all(
      ids.map((id) =>
        api(`/api/products/${id}`).catch((e) => (e.status === 404 ? null : Promise.reject(e)))),
    );
  } catch (error) {
    console.error(error);   // связи нет — показываем как есть, сервер всё равно пересчитает
    return;
  }

  const actual = new Map();
  for (const product of loaded.filter(Boolean)) {
    for (const v of product.variants) {
      actual.set(v.id, { name: product.name, weight: v.weight, price: v.price });
    }
  }

  const kept = state.cart.filter((i) => actual.has(i.variantId));
  const gone = state.cart.length - kept.length;
  const changed = kept.filter((i) => i.price !== actual.get(i.variantId).price).length;
  for (const line of kept) Object.assign(line, actual.get(line.variantId));
  state.cart = kept;

  state.cartNotice = gone
    ? "Часть товаров больше не продаётся — мы убрали их из корзины."
    : changed
      ? "Цены обновились."
      : "";
  if (gone || changed) saveCart();
}

const goodsTotal = () => state.cart.reduce((sum, i) => sum + i.price * i.qty, 0);
const cartCount = () => state.cart.reduce((sum, i) => sum + i.qty, 0);

function renderCartBar() {
  const count = cartCount();
  el("cart-bar").classList.toggle("hidden", count === 0);
  el("cart-count").textContent = `${count} ${plural(count)}`;
  el("cart-sum").textContent = money(goodsTotal());
}

function renderCart() {
  const notice = state.cartNotice
    ? `<div class="err" style="margin:0 0 10px">${esc(state.cartNotice)}</div>`
    : "";
  el("to-checkout").disabled = !state.cart.length;

  if (!state.cart.length) {
    el("cart-body").innerHTML = notice + `<div class="msg">Корзина пуста</div>`;
    return;
  }

  const lines = state.cart
    .map((item, index) => `
      <div class="card">
        <div class="line">
          <div style="flex:1">
            <div style="font-size:13px;line-height:1.35">${esc(item.name)} · ${esc(item.weight)}</div>
            <div class="prc" style="font-size:13px;margin-top:3px">${money(item.price)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="qb" data-cart-minus="${index}" style="background:#F1F1F1"><i class="ti ti-minus" style="font-size:14px"></i></span>
            <span style="font-size:15px;min-width:20px;text-align:center">${item.qty}</span>
            <span class="qb" data-cart-plus="${index}" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:14px"></i></span>
          </div>
          <i class="ti ti-trash del" data-cart-del="${index}"></i>
        </div>
      </div>`)
    .join("");

  el("cart-body").innerHTML = notice + lines + `<div class="card">${totalsRows()}</div>` + `
    <div class="mut" style="font-size:12px;text-align:center;line-height:1.6">
      <i class="ti ti-info-circle"></i> Состав можно менять до оформления заказа
    </div>`;
}

/** Строки «товары / доставка / итого». Обёртку-карточку добавляет вызывающий. */
function totalsRows() {
  const goods = goodsTotal();
  return `
    <div class="row" style="margin-bottom:9px"><span class="mut">Товары (${cartCount()})</span>
      <span style="font-size:13px">${money(goods)}</span></div>
    <div class="row" style="margin-bottom:11px"><span class="mut">Доставка</span>
      <span style="font-size:13px">${money(state.deliveryPrice)}</span></div>
    <div class="row" style="border-top:1px solid #EDEDED;padding-top:11px">
      <span style="font-size:15px;font-weight:600">Итого</span>
      <span class="prc" style="font-size:17px">${money(goods + state.deliveryPrice)}</span></div>`;
}

// ---------- оформление ----------

function renderCheckout() {
  el("slots").innerHTML = SLOTS.map(([name, hint]) => `
    <div class="opt ${state.slot === name ? "on" : ""}" data-slot="${esc(name)}">
      <div><div style="font-size:14px">${esc(name)}</div>
        <div class="mut" style="font-size:11px">${esc(hint)}</div></div>
      <i class="ti ${state.slot === name ? "ti-circle-check" : "ti-circle"}"></i>
    </div>`).join("");

  el("payments").innerHTML = PAYMENTS.map(([code, name, icon]) => `
    <div class="opt ${state.payment === code ? "on" : ""}" data-payment="${code}">
      <span style="font-size:14px"><i class="ti ${icon}" style="color:#E30613"></i> ${esc(name)}</span>
      <i class="ti ${state.payment === code ? "ti-circle-check" : "ti-circle"}"></i>
    </div>`).join("");

  el("checkout-total").innerHTML = totalsRows();
  el("submit-order").textContent = `Подтвердить заказ · ${money(goodsTotal() + state.deliveryPrice)}`;
}

function showError(message) {
  const box = el("checkout-error");
  box.textContent = message;
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitOrder() {
  if (state.sending) return;
  el("checkout-error").classList.add("hidden");

  const fields = {
    "f-name": el("f-name").value.trim(),
    "f-phone": el("f-phone").value.trim(),
    "f-address": el("f-address").value.trim(),
  };
  const digits = fields["f-phone"].replace(/\D/g, "");
  const problems = [];
  if (fields["f-name"].length < 2) problems.push("f-name");
  if (!digits.startsWith("998") || digits.length !== 12) problems.push("f-phone");
  if (fields["f-address"].length < 5) problems.push("f-address");

  for (const id of Object.keys(fields)) el(id).classList.toggle("bad", problems.includes(id));
  if (problems.length) {
    return showError("Заполните имя, номер телефона в формате +998 XX XXX XX XX и адрес доставки.");
  }

  state.sending = true;
  el("submit-order").textContent = "Отправляем…";
  try {
    const response = await fetch("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tgHeaders() },
      body: JSON.stringify({
        customer_name: fields["f-name"],
        phone: fields["f-phone"],
        address: fields["f-address"],
        delivery_slot: state.slot,
        payment_method: state.payment,
        comment: el("f-comment").value.trim() || null,
        items: state.cart.map((i) => ({ variant_id: i.variantId, quantity: i.qty })),
        client_key: state.clientKey,
        expected_total: goodsTotal() + state.deliveryPrice,
      }),
    });

    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      console.error(error);   // прокси вернул HTML вместо ответа сервера
    }

    if (response.status === 409) {
      return openCart("Цены изменились. Проверьте корзину и подтвердите заказ заново.");
    }
    if (response.status === 401) {
      // подпись Telegram живёт сутки: приложение провисело открытым дольше
      throw Object.assign(new Error(body && body.detail
        ? body.detail
        : "Закройте и откройте приложение заново, вход в Telegram устарел."),
        { forUser: true });
    }
    if (!response.ok) {
      // текст сервера показываем только там, где он написан для покупателя
      const detail = response.status < 500 && body && typeof body.detail === "string"
        ? body.detail
        : "Не удалось оформить заказ. Проверьте данные и попробуйте ещё раз.";
      throw Object.assign(new Error(detail), { forUser: true });
    }

    state.cart = [];
    state.scrollY = 0;   // «Вернуться в каталог» — сверху, а не там, где смотрели товар
    saveCart();
    renderCartBar();
    if (!body) {
      // заказ создан, но ответ пришёл не в том виде — второй раз отправлять нельзя
      throw Object.assign(new Error("Заказ отправлен. Найдите его в «Моих заказах»."),
        { forUser: true });
    }
    renderDone(body);
    show("done");
  } catch (error) {
    console.error(error);
    // обрыв связи даёт техническое «Failed to fetch» — покупателю такое не показываем
    showError(error.forUser
      ? error.message
      : "Не удалось отправить заказ. Проверьте связь и попробуйте ещё раз.");
    renderCheckout();   // возвращаем кнопке сумму вместо «Отправляем…»
  } finally {
    state.sending = false;
  }
}

function renderDone(order) {
  const paid = order.payment_method === "online";
  el("done-body").innerHTML = `
    <div style="text-align:center">
      <div style="width:82px;height:82px;border-radius:50%;background:#E30613;color:#fff;display:flex;align-items:center;justify-content:center;font-size:40px;margin:0 auto 20px">
        <i class="ti ti-check"></i></div>
      <div style="font-size:23px;font-weight:600;margin-bottom:8px">Заказ принят</div>
      <div class="mut" style="margin-bottom:22px;line-height:1.65">
        ${paid ? "Ожидаем оплату, после неё магазин начнёт сборку." : "Мы уже собираем ваш заказ.<br>Курьер свяжется перед доставкой."}
      </div>
    </div>
    <div class="card" style="background:#F7F7F7;text-align:center">
      <div class="mut" style="font-size:12px;margin-bottom:4px">Номер заказа</div>
      <div style="font-size:21px;font-weight:600;letter-spacing:.6px">${esc(order.number)}</div>
    </div>
    <div class="card" style="background:#F7F7F7">
      <div class="row" style="margin-bottom:9px"><span class="mut">Состав</span>
        <span style="font-size:13px">${order.items.length} ${plural(order.items.length, "позиция", "позиции", "позиций")}</span></div>
      <div class="row" style="margin-bottom:9px"><span class="mut">${paid ? "К оплате" : "Оплата курьеру"}</span>
        <span class="prc" style="font-size:13px">${money(order.total)}</span></div>
      <div class="row"><span class="mut">Доставка</span>
        <span style="font-size:13px">${esc(order.delivery_slot)}</span></div>
    </div>`;
}

// ---------- мои заказы ----------

async function openOrders() {
  show("orders");
  el("orders-body").innerHTML = `<div class="msg">Загружаем…</div>`;
  try {
    const orders = await api("/api/my-orders");
    el("orders-body").innerHTML = orders.length
      ? orders.map(orderCard).join("")
      : `<div class="msg">Вы ещё ничего не заказывали</div>`;
  } catch (error) {
    console.error(error);
    el("orders-body").innerHTML = `<div class="msg">${error.status === 401
      ? "Закройте и откройте приложение заново, вход в Telegram устарел."
      : "Не удалось загрузить заказы."}</div>`;
  }
}

function orderCard(order) {
  const when = new Date(order.created_at).toLocaleString("ru-RU", {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });
  return `
    <div class="card">
      <div class="row" style="margin-bottom:7px">
        <span style="font-weight:600">${esc(order.number)}</span>
        <span class="prc">${money(order.total)}</span>
      </div>
      <div class="row">
        <span class="mut" style="font-size:12px">${esc(when)}</span>
        <span class="mut" style="font-size:12px">${esc(order.status_text)}</span>
      </div>
      <div class="mut" style="font-size:12px;margin-top:7px">
        ${order.items.length} ${plural(order.items.length, "позиция", "позиции", "позиций")}
        · ${esc(order.delivery_slot)}
      </div>
    </div>`;
}

// ---------- переключение экранов ----------

const SCREENS = ["catalog", "product", "cart", "checkout", "done", "orders"];

function show(name) {
  for (const s of SCREENS) el(`screen-${s}`).classList.toggle("hidden", s !== name);
  // панель корзины фиксированная и перекрыла бы кнопки на других экранах
  el("cart-bar").classList.add("hidden");
  if (name === "catalog") renderCartBar();
  window.scrollTo(0, name === "catalog" ? state.scrollY : 0);
}

/** Открывает корзину, предварительно сверив её с каталогом. */
async function openCart(notice = "") {
  state.cartNotice = notice;
  renderCart();
  show("cart");
  await refreshCart();
  if (!state.cartNotice) state.cartNotice = notice;
  renderCart();
}

// ---------- события ----------

document.addEventListener("click", (event) => {
  const category = event.target.closest("[data-category]");
  if (category) return selectCategory(Number(category.dataset.category));

  const product = event.target.closest("[data-product]");
  if (product) return openProduct(Number(product.dataset.product));

  const variant = event.target.closest("[data-variant]");
  if (variant) {
    // количество сохраняем: покупатель уже выбрал, сколько ему нужно
    state.variant = state.product.variants.find((v) => v.id === Number(variant.dataset.variant));
    return renderProduct();
  }

  if (event.target.closest("#plus")) {
    state.qty = Math.min(MAX_QTY, state.qty + 1);
    return updateTotal();
  }
  if (event.target.closest("#minus")) {
    state.qty = Math.max(1, state.qty - 1);
    return updateTotal();
  }
  if (event.target.closest("#back")) return show("catalog");
  if (event.target.closest("#add")) return addToCart();
  if (event.target.closest("#my-orders")) return openOrders();

  // корзина
  const minus = event.target.closest("[data-cart-minus]");
  if (minus) return changeCartQty(Number(minus.dataset.cartMinus), -1);
  const plus = event.target.closest("[data-cart-plus]");
  if (plus) return changeCartQty(Number(plus.dataset.cartPlus), 1);
  const del = event.target.closest("[data-cart-del]");
  if (del) {
    state.cart.splice(Number(del.dataset.cartDel), 1);
    saveCart();
    return renderCart();
  }

  // оформление
  const slot = event.target.closest("[data-slot]");
  if (slot) {
    state.slot = slot.dataset.slot;
    return renderCheckout();
  }
  const payment = event.target.closest("[data-payment]");
  if (payment) {
    state.payment = payment.dataset.payment;
    return renderCheckout();
  }
  if (event.target.closest("#to-checkout")) {
    // ключ живёт до конца попытки, в том числе если приложение свернули
    // и открыли заново: повторная отправка не заведёт второй заказ
    if (!state.clientKey) {
      state.clientKey = crypto.randomUUID();
      persistCart();
    }
    state.cartNotice = "";
    el("checkout-error").classList.add("hidden");   // ошибка прошлой попытки
    renderCheckout();
    return show("checkout");
  }
  if (event.target.closest("#submit-order")) return submitOrder();

  // переходы между экранами
  const go = event.target.closest("[data-go]");
  if (go) return go.dataset.go === "cart" ? openCart() : show(go.dataset.go);
});

function changeCartQty(index, delta) {
  const item = state.cart[index];
  if (!item) return;
  item.qty = Math.min(MAX_QTY, item.qty + delta);
  if (item.qty < 1) state.cart.splice(index, 1);
  saveCart();
  renderCart();
}

/** Внутри Telegram приложение по умолчанию открывается в половину высоты,
 *  а свайп вниз закрывает его прямо посреди прокрутки каталога. */
function setupTelegram() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) return;   // открыли в обычном браузере
  try {
    tg.ready();
    tg.expand();
    tg.setHeaderColor("#E30613");
    tg.setBackgroundColor("#F5F5F5");
    if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
  } catch (error) {
    console.error(error);   // старый клиент не знает часть команд — не повод падать
  }

  if (!tg.initData) return;   // открыто в браузере: заказов у нас на него нет
  el("my-orders").classList.remove("hidden");
  // подставляем имя, чтобы не набирать его руками. Телефона Telegram не даёт
  const user = tg.initDataUnsafe && tg.initDataUnsafe.user;
  if (user) {
    el("f-name").value = [user.first_name, user.last_name].filter(Boolean).join(" ");
  }
}

async function start() {
  setupTelegram();
  loadSavedCart();
  // стоимость доставки грузим вместе с каталогом: без неё итог в корзине
  // разошёлся бы с расчётом сервера
  try {
    const { delivery_price: price } = await api("/api/delivery-price");
    state.deliveryPrice = price;
    await loadCatalog();
  } catch (error) {
    fail("Не удалось загрузить каталог. Проверьте подключение к интернету.", error);
    return;
  }
  renderCartBar();
}

start();
