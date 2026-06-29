/* ── i18n strings ─────────────────────────────────────────────────────────── */
const I18N = {
  zh: {
    site_title: '新史T',
    label_currency: '货币',
    label_sort: '排序',
    sort_priority: '优先级',
    sort_cut: '折扣',
    sort_score: '好评率',
    sort_price: '当前价格',
    search_placeholder: '搜索游戏…',
    badge_atl: '史低',
    badge_aaa: '大作',
    badge_known: '知名',
    reviews_label: '好评',
    low_label: '史低',
    open_steam: '在 Steam 打开',
    no_deals: '当前没有检测到 Steam 促销，请稍后再来。',
    footer_data: '数据来源：',
    updated_at: '更新于',
    stale_notice: '⚠ 数据为旧版本，抓取时发生错误，展示上次成功数据。',
    loading: '正在加载数据…',
    error: '加载失败，请刷新重试。',
    screenshots: '截图',
    trailer: '预告片',
    atl_badge_card: '当前区域史低',
    load_more: '加载更多',
  },
  en: {
    site_title: 'New S Deals',
    label_currency: 'Currency',
    label_sort: 'Sort',
    sort_priority: 'Priority',
    sort_cut: 'Discount',
    sort_score: 'Rating',
    sort_price: 'Price',
    search_placeholder: 'Search games…',
    badge_atl: 'ALL-TIME LOW',
    badge_aaa: 'AAA',
    badge_known: 'POPULAR',
    reviews_label: 'positive',
    low_label: 'ATL',
    open_steam: 'Open in Steam',
    no_deals: 'No active Steam sale detected. Check back later.',
    footer_data: 'Data from ',
    updated_at: 'Updated',
    stale_notice: '⚠ Showing cached data from a previous run (fetch failed).',
    loading: 'Loading deals…',
    error: 'Failed to load data. Please refresh.',
    screenshots: 'Screenshots',
    trailer: 'Trailer',
    atl_badge_card: 'Regional ATL',
    load_more: 'Load more',
  },
};

/* ── state ────────────────────────────────────────────────────────────────── */
const PAGE_SIZE = 50;

const state = {
  lang: localStorage.getItem('lang') || 'zh',
  currency: localStorage.getItem('currency') || 'USD',
  sort: 'priority',
  search: '',
  data: null,
  visibleCount: PAGE_SIZE,
};

/* ── helpers ──────────────────────────────────────────────────────────────── */
const t = (key) => (I18N[state.lang] || I18N.zh)[key] || key;

function fmt(amount, currency) {
  if (amount == null) return '—';
  const symbols = { USD: '$', CNY: '¥', MYR: 'RM ' };
  const sym = symbols[currency] || '';
  return `${sym}${Number(amount).toFixed(2)}`;
}

function scoreClass(score) {
  if (score >= 80) return 'score-high';
  if (score >= 60) return 'score-mid';
  return 'score-low';
}

function el(tag, cls, inner) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (inner !== undefined) e.innerHTML = inner;
  return e;
}

function setI18n() {
  document.documentElement.lang = state.lang === 'zh' ? 'zh' : 'en';
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelector('#sort-select').querySelectorAll('option').forEach((opt) => {
    if (opt.dataset.i18n) opt.textContent = t(opt.dataset.i18n);
  });
  document.getElementById('lang-toggle').textContent = state.lang === 'zh' ? 'EN' : '中文';
}

/* ── rendering ────────────────────────────────────────────────────────────── */
function getDisplayGames() {
  if (!state.data) return [];
  let list = [...state.data.games];

  const q = state.search.trim().toLowerCase();
  if (q) list = list.filter((g) => g.title.toLowerCase().includes(q));

  const cur = state.currency;

  switch (state.sort) {
    case 'cut':
      list.sort((a, b) => {
        const ca = (a.prices[cur] || {}).cut ?? a.prices.USD.cut;
        const cb = (b.prices[cur] || {}).cut ?? b.prices.USD.cut;
        return cb - ca;
      });
      break;
    case 'score':
      list.sort((a, b) => (b.reviews.score || 0) - (a.reviews.score || 0));
      break;
    case 'price':
      list.sort((a, b) => {
        const pa = (a.prices[cur] || {}).current ?? Infinity;
        const pb = (b.prices[cur] || {}).current ?? Infinity;
        return pa - pb;
      });
      break;
    default:
      // priority order from server is already set
      break;
  }

  return list;
}

function renderCard(game) {
  const cur = state.currency;
  // Use selected currency's price; fall back to USD only for the discount badge (cut %)
  const priceData = game.prices[cur] || {};
  const usdData = game.prices.USD || {};

  const isAtl = priceData.is_atl;
  const cut = priceData.cut ?? usdData.cut ?? 0;
  const current = priceData.current ?? null;   // null → fmt shows "—"
  const regular = priceData.regular ?? null;
  const low = priceData.low ?? null;

  const card = el('div', 'game-card');
  card.setAttribute('tabindex', '0');
  card.setAttribute('role', 'button');
  card.setAttribute('aria-label', game.title);

  // Cover
  const coverDiv = el('div', 'card-cover');
  if (game.images.capsule) {
    const img = document.createElement('img');
    img.src = game.images.capsule;
    img.alt = game.title;
    img.loading = 'lazy';
    img.onerror = () => { coverDiv.style.background = '#0a1520'; };
    coverDiv.appendChild(img);
  }

  const badgeDiv = el('div', 'card-badges');
  if (game.is_atl) badgeDiv.appendChild(el('span', 'badge badge-atl', t('badge_atl')));
  else if (isAtl) badgeDiv.appendChild(el('span', 'badge badge-atl', t('atl_badge_card')));
  if (game.tier === 'aaa') badgeDiv.appendChild(el('span', 'badge badge-aaa', t('badge_aaa')));
  else if (game.tier === 'known') badgeDiv.appendChild(el('span', 'badge badge-known', t('badge_known')));
  coverDiv.appendChild(badgeDiv);

  if (cut > 0) coverDiv.appendChild(el('span', 'discount-badge', `-${cut}%`));
  card.appendChild(coverDiv);

  // Body
  const body = el('div', 'card-body');
  body.appendChild(el('div', 'card-title', game.title));

  if (game.tags && game.tags.length) {
    const tagsDiv = el('div', 'card-tags');
    game.tags.slice(0, 4).forEach((tag) => tagsDiv.appendChild(el('span', 'tag', tag)));
    body.appendChild(tagsDiv);
  }

  if (game.reviews.count) {
    const revDiv = el('div', 'card-reviews');
    const scoreEl = el('span', `review-score ${scoreClass(game.reviews.score)}`, `${game.reviews.score}%`);
    revDiv.appendChild(scoreEl);
    const cnt = game.reviews.count >= 1000
      ? `${(game.reviews.count / 1000).toFixed(0)}k ${t('reviews_label')}`
      : `${game.reviews.count} ${t('reviews_label')}`;
    revDiv.appendChild(el('span', 'review-count', cnt));
    body.appendChild(revDiv);
  }

  const pricingDiv = el('div', 'card-pricing');
  pricingDiv.appendChild(el('div', 'price-current', fmt(current, cur)));

  const priceBlock = el('div', 'price-block');
  if (regular) priceBlock.appendChild(el('div', 'price-regular', fmt(regular, cur)));
  if (low) priceBlock.appendChild(el('div', 'price-low-label', `${t('low_label')}: ${fmt(low, cur)}`));
  pricingDiv.appendChild(priceBlock);
  body.appendChild(pricingDiv);

  card.appendChild(body);

  card.addEventListener('click', () => openModal(game));
  card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') openModal(game); });

  return card;
}

function renderGrid() {
  const grid = document.getElementById('game-grid');
  const loadMoreWrap = document.getElementById('load-more-wrap');
  const empty = document.getElementById('empty-state');
  const games = getDisplayGames();

  grid.innerHTML = '';
  loadMoreWrap.innerHTML = '';

  if (!games.length) {
    empty.classList.remove('hidden');
    empty.querySelector('[data-i18n]').textContent = t('no_deals');
    return;
  }
  empty.classList.add('hidden');

  const visible = games.slice(0, state.visibleCount);
  const frag = document.createDocumentFragment();
  visible.forEach((g) => frag.appendChild(renderCard(g)));
  grid.appendChild(frag);

  if (games.length > state.visibleCount) {
    const remaining = games.length - state.visibleCount;
    const btn = document.createElement('button');
    btn.className = 'btn-load-more';
    btn.textContent = `${t('load_more')} (${remaining})`;
    btn.addEventListener('click', () => {
      state.visibleCount += PAGE_SIZE;
      renderGrid();
    });
    loadMoreWrap.appendChild(btn);
  }
}

/* ── modal ────────────────────────────────────────────────────────────────── */
function openModal(game) {
  const modal = document.getElementById('modal');
  const content = document.getElementById('modal-content');
  content.innerHTML = '';

  const currencies = ['USD', 'CNY', 'MYR'];

  // Header
  const header = el('div', 'modal-header');

  if (game.images.capsule) {
    const coverDiv = el('div', 'modal-cover');
    const img = document.createElement('img');
    img.src = game.images.capsule;
    img.alt = game.title;
    coverDiv.appendChild(img);
    header.appendChild(coverDiv);
  }

  const meta = el('div', 'modal-meta');
  meta.appendChild(el('div', 'modal-title', game.title));

  const badgesDiv = el('div', 'modal-badges');
  if (game.is_atl) badgesDiv.appendChild(el('span', 'badge badge-atl', t('badge_atl')));
  if (game.tier === 'aaa') badgesDiv.appendChild(el('span', 'badge badge-aaa', t('badge_aaa')));
  else if (game.tier === 'known') badgesDiv.appendChild(el('span', 'badge badge-known', t('badge_known')));
  meta.appendChild(badgesDiv);

  if (game.reviews.count) {
    const rev = el('div', 'modal-reviews',
      `${game.reviews.score}% ${t('reviews_label')} (${game.reviews.count.toLocaleString()})`);
    if (game.reviews.text) rev.title = game.reviews.text;
    meta.appendChild(rev);
  }

  if (game.tags && game.tags.length) {
    const tagsDiv = el('div', 'modal-tags');
    game.tags.forEach((tag) => tagsDiv.appendChild(el('span', 'tag', tag)));
    meta.appendChild(tagsDiv);
  }

  header.appendChild(meta);
  content.appendChild(header);

  // Prices grid
  const pricesGrid = el('div', 'modal-prices');
  currencies.forEach((cur) => {
    const p = game.prices[cur];
    if (!p || p.current == null) return;

    const card = el('div', `price-card${p.is_atl ? ' is-atl' : ''}`);
    card.appendChild(el('div', 'price-card-label', cur));
    card.appendChild(el('div', 'price-card-current', fmt(p.current, cur)));
    if (p.regular) card.appendChild(el('div', 'price-card-original', fmt(p.regular, cur)));
    if (p.low) card.appendChild(el('div', 'price-card-low', `${t('low_label')}: ${fmt(p.low, cur)}`));
    if (p.is_atl) card.appendChild(el('div', 'price-card-atl-badge', t('badge_atl')));
    pricesGrid.appendChild(card);
  });
  content.appendChild(pricesGrid);

  // Screenshots
  if (game.images.screenshots && game.images.screenshots.length) {
    const section = el('div', 'modal-screenshots');
    section.appendChild(el('div', 'modal-section-title', t('screenshots')));
    const grid = el('div', 'screenshots-grid');
    game.images.screenshots.forEach((src) => {
      const img = document.createElement('img');
      img.src = src;
      img.className = 'screenshot-img';
      img.loading = 'lazy';
      img.addEventListener('click', (e) => {
        e.stopPropagation();
        window.open(src, '_blank');
      });
      grid.appendChild(img);
    });
    section.appendChild(grid);
    content.appendChild(section);
  }

  // Trailer
  if (game.trailer) {
    const section = el('div', 'modal-trailer');
    section.appendChild(el('div', 'modal-section-title', t('trailer')));
    const video = document.createElement('video');
    video.className = 'trailer-video';
    video.src = game.trailer;
    video.controls = true;
    video.preload = 'none';
    video.poster = game.images.capsule || '';
    section.appendChild(video);
    content.appendChild(section);
  }

  // Actions
  if (game.steam_url) {
    const actions = el('div', 'modal-actions');
    const link = document.createElement('a');
    link.href = game.steam_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.className = 'btn-steam';
    link.textContent = t('open_steam');
    actions.appendChild(link);
    content.appendChild(actions);
  }

  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
  document.body.style.overflow = '';
  // Stop any playing video
  const video = document.querySelector('.trailer-video');
  if (video) video.pause();
}

/* ── data loading ─────────────────────────────────────────────────────────── */
async function loadData() {
  const statusBar = document.getElementById('status-bar');

  try {
    const res = await fetch('./data/deals.json');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();

    if (state.data.generated_at) {
      const d = new Date(state.data.generated_at);
      const hoursSince = (Date.now() - d.getTime()) / 36e5;
      const updated = document.getElementById('updated-at');
      updated.textContent = `${t('updated_at')} ${d.toLocaleString()}`;

      // Show stale warning if data is more than 24h old
      if (hoursSince > 24) {
        statusBar.textContent = t('stale_notice');
        statusBar.classList.add('stale');
        statusBar.classList.remove('hidden');
      }
    }
  } catch (err) {
    statusBar.textContent = t('error');
    statusBar.classList.remove('hidden');
    console.error(err);
  }

  renderGrid();
}

/* ── event wiring ─────────────────────────────────────────────────────────── */
function initControls() {
  // Language toggle
  document.getElementById('lang-toggle').addEventListener('click', () => {
    state.lang = state.lang === 'zh' ? 'en' : 'zh';
    localStorage.setItem('lang', state.lang);
    setI18n();
    state.visibleCount = PAGE_SIZE;
    renderGrid();
  });

  // Currency buttons
  document.getElementById('currency-group').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-currency]');
    if (!btn) return;
    document.querySelectorAll('#currency-group .btn-toggle').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.currency = btn.dataset.currency;
    localStorage.setItem('currency', state.currency);
    state.visibleCount = PAGE_SIZE;
    renderGrid();
  });

  // Restore saved currency button state
  document.querySelectorAll('#currency-group .btn-toggle').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.currency === state.currency);
  });

  // Sort
  document.getElementById('sort-select').addEventListener('change', (e) => {
    state.sort = e.target.value;
    state.visibleCount = PAGE_SIZE;
    renderGrid();
  });

  // Search
  let searchTimer;
  document.getElementById('search-input').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = e.target.value;
      state.visibleCount = PAGE_SIZE;
      renderGrid();
    }, 200);
  });

  // Modal close
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-backdrop').addEventListener('click', closeModal);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
}

/* ── init ─────────────────────────────────────────────────────────────────── */
(function init() {
  setI18n();
  initControls();
  loadData();
})();
