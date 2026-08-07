(() => {
  const input = document.getElementById('marketInput');
  const loadButton = document.getElementById('loadButton');
  const resultsEl = document.getElementById('searchResults');
  if (!input || !loadButton || !resultsEl) return;

  const DEBOUNCE_MS = 230;
  let timer = null;
  let controller = null;
  let results = [];
  let activeIndex = -1;
  let requestSerial = 0;

  const looksLikeLocator = (value) =>
    /(^https?:\/\/)|polymarket\.com\/|^event\//i.test(value);

  function money(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return null;
    const abs = Math.abs(n);
    if (abs >= 1e9) return `$${(n / 1e9).toFixed(abs >= 10e9 ? 0 : 1)}b`;
    if (abs >= 1e6) return `$${(n / 1e6).toFixed(abs >= 10e6 ? 0 : 1)}m`;
    if (abs >= 1e3) return `$${(n / 1e3).toFixed(abs >= 10e3 ? 0 : 1)}k`;
    return `$${n.toFixed(0)}`;
  }

  function endLabel(value) {
    if (!value) return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return null;
    return `ends ${date.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: date.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined})}`;
  }

  function closeResults() {
    results = [];
    resultsEl.classList.add('hidden');
    resultsEl.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    activeIndex = -1;
  }

  function setActive(index) {
    const buttons = [...resultsEl.querySelectorAll('.search-result')];
    if (!buttons.length) return;
    activeIndex = Math.max(0, Math.min(index, buttons.length - 1));
    buttons.forEach((button, i) => button.classList.toggle('active', i === activeIndex));
    buttons[activeIndex].scrollIntoView({block: 'nearest'});
  }

  function selectResult(index) {
    const item = results[index];
    if (!item) return;
    closeResults();

    // app.js already owns the resolver + submarket chooser. Feed it the event
    // slug, then restore the human title once it has synchronously built the URL.
    const title = item.title || item.slug;
    input.value = item.slug;
    input.dataset.resolvedSlug = item.slug;
    loadButton.click();
    queueMicrotask(() => { input.value = title; });
  }

  function iconNode(item) {
    const wrap = document.createElement('span');
    wrap.className = 'search-result-icon';
    const fallback = document.createElement('span');
    fallback.className = 'search-result-icon-fallback';
    fallback.textContent = (item.title || '?').trim().slice(0, 1).toUpperCase();
    wrap.append(fallback);
    if (item.icon) {
      const img = document.createElement('img');
      img.src = item.icon;
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('load', () => fallback.classList.add('hidden'));
      img.addEventListener('error', () => img.remove());
      wrap.prepend(img);
    }
    return wrap;
  }

  function render(items, query) {
    results = items;
    resultsEl.replaceChildren();
    activeIndex = -1;

    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'search-empty';
      empty.textContent = `No live binary events found for “${query}”.`;
      resultsEl.append(empty);
    } else {
      for (const [index, item] of items.entries()) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'search-result';
        button.setAttribute('role', 'option');
        button.dataset.index = String(index);
        button.append(iconNode(item));

        const body = document.createElement('span');
        body.className = 'search-result-body';

        const title = document.createElement('span');
        title.className = 'search-result-title';
        title.textContent = item.title || item.slug;
        body.append(title);

        if (item.subtitle) {
          const subtitle = document.createElement('span');
          subtitle.className = 'search-result-subtitle';
          subtitle.textContent = item.subtitle;
          body.append(subtitle);
        }

        const meta = document.createElement('span');
        meta.className = 'search-result-meta';
        const marketCount = Number(item.binary_market_count) || 0;
        const chips = [
          marketCount ? `${marketCount} binary ${marketCount === 1 ? 'market' : 'markets'}` : null,
          money(item.volume_24h) ? `${money(item.volume_24h)} 24h` : null,
          money(item.liquidity) ? `${money(item.liquidity)} liq` : null,
          endLabel(item.end_date),
        ].filter(Boolean);
        for (const text of chips) {
          const chip = document.createElement('span');
          chip.textContent = text;
          meta.append(chip);
        }
        body.append(meta);
        button.append(body);

        const live = document.createElement('span');
        live.className = 'search-live';
        live.textContent = 'LIVE';
        button.append(live);

        button.addEventListener('pointerenter', () => setActive(index));
        button.addEventListener('click', () => selectResult(index));
        resultsEl.append(button);
      }
    }

    resultsEl.classList.remove('hidden');
    input.setAttribute('aria-expanded', 'true');
  }

  function renderLoading() {
    results = [];
    activeIndex = -1;
    resultsEl.replaceChildren();
    const loading = document.createElement('div');
    loading.className = 'search-empty search-loading';
    loading.textContent = 'Searching Polymarket…';
    resultsEl.append(loading);
    resultsEl.classList.remove('hidden');
    input.setAttribute('aria-expanded', 'true');
  }

  async function search(query) {
    const serial = ++requestSerial;
    if (controller) controller.abort();
    controller = new AbortController();
    renderLoading();
    try {
      const response = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=8`, {signal: controller.signal});
      const payload = await response.json();
      if (serial !== requestSerial) return;
      if (!response.ok) throw new Error(payload.detail || 'Search failed');
      render(payload.events || [], query);
    } catch (error) {
      if (error.name === 'AbortError' || serial !== requestSerial) return;
      results = [];
      resultsEl.replaceChildren();
      const failure = document.createElement('div');
      failure.className = 'search-empty search-error';
      failure.textContent = error.message || 'Search failed.';
      resultsEl.append(failure);
      resultsEl.classList.remove('hidden');
      input.setAttribute('aria-expanded', 'true');
    }
  }

  input.addEventListener('input', () => {
    input.dataset.resolvedSlug = '';
    const query = input.value.trim();
    if (timer) clearTimeout(timer);
    if (controller) controller.abort();
    requestSerial += 1;

    if (query.length < 2 || looksLikeLocator(query)) {
      closeResults();
      return;
    }
    timer = setTimeout(() => search(query), DEBOUNCE_MS);
  });

  // Capture Enter before app.js's existing "resolve this as a slug" handler.
  input.addEventListener('keydown', (event) => {
    const query = input.value.trim();
    const open = !resultsEl.classList.contains('hidden');
    if (event.key === 'ArrowDown' && open && results.length) {
      event.preventDefault();
      setActive(activeIndex < 0 ? 0 : activeIndex + 1);
    } else if (event.key === 'ArrowUp' && open && results.length) {
      event.preventDefault();
      setActive(activeIndex < 0 ? results.length - 1 : activeIndex - 1);
    } else if (event.key === 'Enter' && !looksLikeLocator(query) && query.length >= 2) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (open && results.length) {
        selectResult(activeIndex >= 0 ? activeIndex : 0);
      } else {
        if (timer) clearTimeout(timer);
        search(query);
      }
    } else if (event.key === 'Escape' && open) {
      closeResults();
    }
  }, true);

  // If a selected result is displayed by title, still let the fallback button
  // resolve the stored event slug without replacing the friendly text.
  loadButton.addEventListener('click', () => {
    const slug = input.dataset.resolvedSlug;
    if (!slug || looksLikeLocator(input.value.trim())) return;
    const friendly = input.value;
    input.value = slug;
    queueMicrotask(() => { input.value = friendly; });
  }, true);

  input.addEventListener('focus', () => {
    if (results.length && input.value.trim().length >= 2 && !looksLikeLocator(input.value.trim())) {
      resultsEl.classList.remove('hidden');
      input.setAttribute('aria-expanded', 'true');
    }
  });

  document.addEventListener('pointerdown', (event) => {
    if (event.target !== input && !resultsEl.contains(event.target)) closeResults();
  });
})();
