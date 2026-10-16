/* Landing page: nav behaviour and the demo buttons. A file, not inline, so the page's
   Content-Security-Policy can allow scripts from this server only. */
  // nav: border on scroll, mobile menu
  const nav = document.getElementById('nav');
  addEventListener('scroll', () => nav.classList.toggle('scrolled', scrollY > 8), { passive: true });
  const menuBtn = document.getElementById('menu-btn'), links = document.getElementById('nav-links');
  menuBtn.addEventListener('click', () => {
    const open = links.classList.toggle('open');
    menuBtn.setAttribute('aria-expanded', open);
  });
  links.querySelectorAll('a').forEach(a => a.addEventListener('click', () => links.classList.remove('open')));

  // demo accounts, when this server offers them
  async function loadDemo() {
    let info;
    try { info = await (await fetch('/demo/info')).json(); } catch { return; }
    if (!info.enabled) return;
    const box = document.getElementById('demo-accounts');
    info.accounts.slice().reverse().forEach(a => {
      const card = document.createElement('div');
      card.className = 'demo-account';
      const title = document.createElement('b'); title.textContent = a.label;
      const about = document.createElement('p'); about.textContent = a.about;
      const btn = document.createElement('button');
      btn.className = a.id === 'day30' ? 'btn btn-primary' : 'btn btn-ghost';
      btn.textContent = a.id === 'day30' ? 'Open the day-30 shop' : 'Open the day-1 shop';
      btn.onclick = () => demoLogin(a.id, btn);
      card.append(title, about, btn);
      box.prepend(card);
    });
  }
  async function demoLogin(account, btn) {
    btn.disabled = true;
    const err = document.getElementById('demo-error');
    err.hidden = true;
    try {
      const res = await fetch('/demo/login', { method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account }) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Could not open the demo.');
      location.href = '/app';
    } catch (e) {
      err.textContent = e.message; err.hidden = false; btn.disabled = false;
    }
  }
  loadDemo();
