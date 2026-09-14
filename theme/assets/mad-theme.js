(() => {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const initCarousels = (scope = document) => {
    if (typeof window.Swiper !== 'function') return;
    scope.querySelectorAll('[data-mad-swiper]').forEach((root) => {
      if (root.madSwiper) return;
      const container = root.querySelector('.swiper');
      if (!container) return;
      let config = {};
      try {
        config = JSON.parse(root.dataset.madSwiper || '{}');
      } catch (error) {
        config = {};
      }
      const prev = root.querySelector('[data-mad-prev]');
      const next = root.querySelector('[data-mad-next]');
      const dots = root.querySelector('[data-mad-dots]');
      if (prev && next) config.navigation = { prevEl: prev, nextEl: next, addIcons: false };
      if (dots) config.pagination = { el: dots, clickable: true };
      if (config.autoplay && reduceMotion.matches) delete config.autoplay;
      config.watchOverflow = true;
      root.madSwiper = new window.Swiper(container, config);
    });
  };

  const destroyCarousels = (scope) => {
    scope.querySelectorAll('[data-mad-swiper]').forEach((root) => {
      if (!root.madSwiper) return;
      root.madSwiper.destroy(true, true);
      root.madSwiper = null;
    });
  };

  document.addEventListener('change', (event) => {
    const select = event.target.closest('[data-mad-sort]');
    if (!select) return;
    const url = new URL(window.location.href);
    url.searchParams.set('sort_by', select.value);
    url.searchParams.delete('page');
    window.location.href = url.toString();
  });

  document.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-mad-filter-toggle]');
    if (!toggle) return;
    const panel = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!panel) return;
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
  });

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-mad-qty]');
    if (!button) return;
    const input = button.closest('.mad-qty')?.querySelector('input');
    if (!input) return;
    const min = parseInt(input.min, 10) || 1;
    const value = (parseInt(input.value, 10) || min) + parseInt(button.dataset.madQty, 10);
    input.value = Math.max(min, value);
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });

  const initMenuHover = () => {
    const desktop = window.matchMedia('(hover: hover) and (min-width: 990px)');
    document.querySelectorAll('.header__inline-menu header-menu').forEach((menu) => {
      const details = menu.querySelector('details');
      const summary = details?.querySelector('summary');
      if (!details || !summary) return;
      menu.addEventListener('mouseenter', () => {
        if (desktop.matches) details.open = true;
      });
      menu.addEventListener('mouseleave', () => {
        if (desktop.matches) details.open = false;
      });
      summary.addEventListener('click', (event) => {
        if (!desktop.matches || !summary.dataset.href) return;
        event.preventDefault();
        window.location.href = summary.dataset.href;
      });
    });
  };

  const initGallery = (scope = document) => {
    scope.querySelectorAll('[data-mad-gallery]').forEach((gallery) => {
      const main = gallery.querySelector('[data-mad-main]');
      gallery.querySelectorAll('[data-mad-thumb]').forEach((thumb) => {
        thumb.addEventListener('click', () => {
          if (!main) return;
          main.src = thumb.dataset.src;
          main.srcset = thumb.dataset.srcset || '';
          main.alt = thumb.dataset.alt || main.alt;
          gallery.querySelectorAll('[data-mad-thumb]').forEach((item) => item.setAttribute('aria-current', 'false'));
          thumb.setAttribute('aria-current', 'true');
        });
      });
    });
  };

  const initVariantSelects = (scope = document) => {
    scope.querySelectorAll('[data-mad-variant-select]').forEach((select) => {
      const root = select.closest('[data-mad-product]');
      const data = root?.querySelector('[data-mad-variants]');
      if (!root || !data) return;
      const variants = JSON.parse(data.textContent);
      select.addEventListener('change', () => {
        const variant = variants.find((item) => String(item.id) === select.value);
        if (!variant) return;
        root.querySelectorAll('input[name="id"]').forEach((input) => { input.value = variant.id; });
        const price = root.querySelector('[data-mad-price]');
        const compare = root.querySelector('[data-mad-compare]');
        const pix = root.querySelector('[data-mad-pix]');
        if (price) price.textContent = variant.price;
        if (compare) {
          compare.textContent = variant.compare || '';
          compare.hidden = !variant.compare;
        }
        if (pix) pix.textContent = variant.pix;
        const submit = root.querySelector('[data-mad-submit]');
        if (submit) {
          submit.disabled = !variant.available;
          const label = submit.querySelector('span');
          if (label) label.textContent = variant.available ? submit.dataset.labelAvailable : submit.dataset.labelSoldOut;
        }
        const url = new URL(window.location.href);
        url.searchParams.set('variant', variant.id);
        window.history.replaceState({}, '', url.toString());
      });
    });
  };

  const renderCart = (response) => {
    const cart = document.querySelector('cart-notification') || document.querySelector('cart-drawer');
    if (!cart) {
      window.location.href = window.routes?.cart_url || '/cart';
      return;
    }
    cart.renderContents(response);
    if (cart.classList.contains('is-empty')) cart.classList.remove('is-empty');
  };

  const initBuyTogether = (scope = document) => {
    scope.querySelectorAll('[data-mad-bt]').forEach((root) => {
      const button = root.querySelector('[data-mad-bt-submit]');
      if (!button || button.dataset.bound) return;
      button.dataset.bound = 'true';
      button.addEventListener('click', async () => {
        const ids = [root.dataset.mainVariant]
          .concat(Array.from(root.querySelectorAll('[data-mad-bt-check]:checked')).map((input) => input.value))
          .filter(Boolean);
        if (!ids.length) return;
        const cart = document.querySelector('cart-notification') || document.querySelector('cart-drawer');
        const body = { items: ids.map((id) => ({ id: Number(id), quantity: 1 })) };
        if (cart) {
          body.sections = cart.getSectionsToRender().map((section) => section.id);
          body.sections_url = window.location.pathname;
        }
        button.classList.add('loading');
        button.setAttribute('aria-disabled', 'true');
        try {
          const response = await fetch(`${window.routes?.cart_add_url || '/cart/add'}.js`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify(body),
          });
          const data = await response.json();
          if (data.status) throw new Error(data.description || data.message);
          data.key = data.items?.[0]?.key;
          if (typeof publish === 'function' && typeof PUB_SUB_EVENTS !== 'undefined') {
            publish(PUB_SUB_EVENTS.cartUpdate, { source: 'mad-buy-together', cartData: data });
          }
          renderCart(data);
        } catch (error) {
          const message = root.querySelector('[data-mad-bt-error]');
          if (message) {
            message.textContent = error.message;
            message.hidden = false;
          }
        } finally {
          button.classList.remove('loading');
          button.removeAttribute('aria-disabled');
        }
      });
    });
  };

  const initRecommendations = () => {
    document.querySelectorAll('[data-mad-recs]').forEach(async (root) => {
      if (root.dataset.loaded) return;
      root.dataset.loaded = 'true';
      try {
        const response = await fetch(root.dataset.url);
        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const fresh = doc.querySelector(`[data-mad-recs][data-section="${root.dataset.section}"]`);
        if (fresh && fresh.querySelector('[data-mad-bt-check]')) {
          root.innerHTML = fresh.innerHTML;
          const wrapper = root.closest('[data-mad-bt]');
          if (wrapper) wrapper.hidden = false;
          initBuyTogether();
        }
      } catch (error) {
        root.hidden = true;
      }
    });
  };

  const init = () => {
    initCarousels();
    initMenuHover();
    initGallery();
    initVariantSelects();
    initBuyTogether();
    initRecommendations();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  document.addEventListener('shopify:section:unload', (event) => destroyCarousels(event.target));

  document.addEventListener('shopify:section:load', (event) => {
    initCarousels(event.target);
    initGallery(event.target);
    initVariantSelects(event.target);
    initBuyTogether(event.target);
    initRecommendations();
  });
})();
