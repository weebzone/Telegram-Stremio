(() => {
  const root = document.documentElement;
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };
  const REPO = "weebzone/Telegram-Stremio";

  const systemPalette = () => (matchMedia("(prefers-color-scheme: dark)").matches ? "ink" : "paper");
  let palette = store.get("palette") || systemPalette();

  function swapShots() {
    document.querySelectorAll("img[data-shot]").forEach((img) => {
      const next = `shots/${palette}-${img.dataset.shot}.webp`;
      if (img.getAttribute("src") === next) return;
      img.classList.add("out");
      const pre = new Image();
      pre.onload = pre.onerror = () => { img.src = next; requestAnimationFrame(() => img.classList.remove("out")); };
      pre.src = next;
    });
  }

  function applyPalette(p, save) {
    palette = p;
    root.dataset.palette = p;
    document.querySelectorAll(".palette button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.p === p)));
    if (save) store.set("palette", p);
    swapShots();
  }
  document.querySelectorAll(".palette button").forEach((b) => b.addEventListener("click", () => applyPalette(b.dataset.p, true)));
  applyPalette(palette, false);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (!store.get("palette")) applyPalette(systemPalette(), false); });

  const menu = document.querySelector(".menu-btn"), nav = document.querySelector(".nav");
  menu?.addEventListener("click", () => { const open = nav.classList.toggle("open"); menu.setAttribute("aria-expanded", String(open)); });
  nav?.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => nav.classList.remove("open")));

  const screens = {
    home: ["Home", "Server health, library size and the newest titles the moment you sign in.", "home",
      ["Bots, uptime and live streams at a glance", "Recently added posters with ratings", "Who is watching right now, and from where"]],
    media: ["Media library", "Every movie and series your channels hold, matched with posters and ratings.", "media",
      ["Search, filter custom titles and page through thousands of posters", "Quality and size on every card", "Add a personal title by hand"]],
    title: ["Title editor", "The page Stremio shows, plus every file that plays it.", "title",
      ["Logo, backdrop, rating, runtime and genres", "Edit metadata or rescan against TMDB, TVDB and Cinemeta", "Subtitles and catalog membership per title"]],
    episodes: ["Episodes", "Seasons as tabs and every episode with its files.", "title",
      ["Thumbnail, title, air date and overview per episode", "Fallbacks when a thumbnail or title is missing", "Copy a stream link, run a speed test or delete a file"]],
    catalogs: ["Catalogs", "Rows of your own in Stremio and Nuvio, next to the ones the server fills by itself.", "catalogs",
      ["Auto catalogs by language, OTT service and smart lists", "Custom catalogs that are public, per-token or owner-only", "Drag the order Stremio shows them in"]],
    admin: ["Admin", "Health checks, bot load and streaming analytics.", "admin",
      ["Database, TMDB and public-URL checks", "Top titles, top viewers and daily traffic", "Dead links flagged for cleanup"]],
    tokens: ["Tokens", "One install link per person, with data limits and expiry.", "tokens",
      ["Daily and monthly GB limits", "Lifetime keys or keys that expire", "Link a Telegram user to a key"]],
    settings: ["Settings", "Everything after the first deploy lives here, saved to the database.", "settings",
      ["Admin login, channels, TMDB and TVDB keys", "Telegram user session for Global Search", "Backups, extra databases and extra bot tokens"]],
    palette: ["Command palette", "Press Ctrl+K (⌘K on Mac) to jump anywhere or open any title.", "home",
      ["Search titles across movies and series", "Switch theme without the mouse", "G then a letter goes straight to a page"]],
    login: ["Sign in", "A clean admin sign-in in all three themes.", "login",
      ["Rate limited after five wrong passwords", "Works on phones, tablets and desktops", "Paper, Ink and Night themes"]],
  };
  const tabs = document.querySelectorAll(".tab[data-screen]");
  function showScreen(key) {
    const [title, text, phone, points] = screens[key];
    tabs.forEach((t) => t.setAttribute("aria-selected", String(t.dataset.screen === key)));
    const img = document.querySelector("#screen-img");
    if (img) { img.dataset.shot = key; img.alt = `Telegram Stremio ${title}`; }
    const ph = document.querySelector("#phone-img");
    if (ph) { ph.dataset.shot = `phone-${phone}`; ph.alt = `Telegram Stremio ${title} on a phone`; }
    swapShots();
    const note = document.querySelector(".screen-note");
    if (note) {
      note.querySelector("h3").textContent = title;
      note.querySelector("p").textContent = text;
      note.querySelector("ul").replaceChildren(...points.map((p) => Object.assign(document.createElement("li"), { textContent: p })));
    }
  }
  tabs.forEach((t) => t.addEventListener("click", () => showScreen(t.dataset.screen)));
  document.querySelector(".tabs")?.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const list = [...tabs], i = list.findIndex((t) => t.getAttribute("aria-selected") === "true");
    const next = list[(i + (e.key === "ArrowRight" ? 1 : -1) + list.length) % list.length];
    next.focus(); showScreen(next.dataset.screen);
  });
  if (tabs.length) showScreen("home");

  document.querySelectorAll(".os-tabs").forEach((group) => {
    const buttons = group.querySelectorAll(".os-tab");
    buttons.forEach((b) => b.addEventListener("click", () => {
      buttons.forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      document.querySelectorAll(`.os-panel[data-group="${group.dataset.group}"]`).forEach((p) => { p.hidden = p.dataset.os !== b.dataset.os; });
    }));
  });

  document.querySelectorAll(".copy").forEach((btn) => btn.addEventListener("click", async () => {
    const pre = btn.parentElement.querySelector("pre");
    try { await navigator.clipboard.writeText(pre.innerText.trim()); btn.textContent = "Copied"; }
    catch { const r = document.createRange(); r.selectNodeContents(pre); getSelection().removeAllRanges(); getSelection().addRange(r); btn.textContent = "Selected"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 1600);
  }));

  const tocLinks = [...document.querySelectorAll(".toc a")];
  if (tocLinks.length && "IntersectionObserver" in window) {
    const byId = new Map(tocLinks.map((a) => [a.getAttribute("href").slice(1), a]));
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        tocLinks.forEach((a) => a.classList.remove("active"));
        byId.get(e.target.id)?.classList.add("active");
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    document.querySelectorAll(".doc h2[id]").forEach((h) => io.observe(h));
  }

  async function cached(key, url, ttl) {
    try { const c = JSON.parse(sessionStorage.getItem(key)); if (c && Date.now() - c.at < ttl) return c.data; } catch {}
    const r = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
    if (!r.ok) throw new Error(`GitHub ${r.status}`);
    const data = await r.json();
    try { sessionStorage.setItem(key, JSON.stringify({ at: Date.now(), data })); } catch {}
    return data;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = (n) => (n >= 10000 ? `${(n / 1000).toFixed(1)}K` : Number(n).toLocaleString("en"));

  const people = document.getElementById("people");
  cached("contributors", `https://api.github.com/repos/${REPO}/contributors?per_page=100`, 3600000).then((list) => {
    const humans = list.filter((c) => c.type !== "Bot");
    document.querySelectorAll("[data-stat='contributors']").forEach((el) => { el.textContent = humans.length; });
    if (!people) return;
    people.innerHTML = humans.map((c) => `
      <a class="person" href="${esc(c.html_url)}" target="_blank" rel="noopener">
        <img src="${esc(c.avatar_url)}&s=88" alt="" loading="lazy" width="44" height="44">
        <span style="min-width:0"><b>${esc(c.login)}</b><small>${c.contributions} commit${c.contributions === 1 ? "" : "s"}</small></span>
      </a>`).join("");
  }).catch(() => {
    if (people) people.innerHTML = `<p class="lede">GitHub could not be reached. <a href="https://github.com/${REPO}/graphs/contributors">See everyone on GitHub</a>.</p>`;
  });

  cached("repo", `https://api.github.com/repos/${REPO}`, 3600000).then((r) => {
    document.querySelectorAll("[data-stat='stars']").forEach((el) => { el.textContent = short(r.stargazers_count); });
    document.querySelectorAll("[data-stat='forks']").forEach((el) => { el.textContent = short(r.forks_count); });
  }).catch(() => {});

  document.querySelectorAll("[data-year]").forEach((el) => { el.textContent = new Date().getFullYear(); });
})();
