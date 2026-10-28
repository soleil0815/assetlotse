/* AssetLotse – Frontend-Logik (ohne Build-Schritt, ohne Abhängigkeiten). */
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const CSRF = ($('meta[name="csrf-token"]') || {}).content || "";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignorieren */ } },
  };
  /* Übersetzung: T(<deutscher Text>, {name: wert}) – die Texte kommen vom Server (#i18n-strings). */
  const I18N = (() => {
    const el = document.getElementById("i18n-strings");
    try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
  })();
  const T = (msg, vars) => String(I18N[msg] || msg).replace(/\{(\w+)\}/g,
    (m, k) => (vars && Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ------------------------------------------------------------ Grundfunktionen */
  $$("[data-theme-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    store.set("theme", root.dataset.theme);
  }));

  const navButtons = $$("[data-nav-toggle]");
  const setNav = (open) => {
    document.body.classList.toggle("nav-open", open);
    navButtons.forEach((b) => b.setAttribute("aria-expanded", open ? "true" : "false"));
  };
  navButtons.forEach((btn) => btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !document.body.classList.contains("nav-open");
    setNav(open);
    if (open) { const first = document.querySelector("#sidebar a"); if (first) first.focus(); }
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) {
      setNav(false);
      if (navButtons[0]) navButtons[0].focus();
    }
  });
  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar")) {
      setNav(false);
    }
    // Benutzermenü schließen, wenn daneben getippt wird
    $$(".user-menu[open]").forEach((m) => { if (!m.contains(e.target)) m.removeAttribute("open"); });
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Kleine Helfer statt eingebetteter on…-Attribute (erlaubt eine strenge Content-Security-Policy)
  document.addEventListener("submit", (e) => {
    if (e.target.hasAttribute && e.target.hasAttribute("data-no-submit")) e.preventDefault();
  }, true);
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-confirm-click],[data-dismiss],[data-print],[data-fill-previous]");
    if (!t) return;
    if (t.hasAttribute("data-confirm-click") && !window.confirm(t.dataset.confirmClick)) { e.preventDefault(); e.stopPropagation(); }
    if (t.hasAttribute("data-dismiss")) t.parentElement.remove();
    if (t.hasAttribute("data-print")) window.print();
    if (t.hasAttribute("data-fill-previous") && t.previousElementSibling) t.previousElementSibling.value = t.dataset.fillPrevious;
  }, true);
  $$("[data-enter-clicks-next]").forEach((el) => el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (el.nextElementSibling) el.nextElementSibling.click(); }
  }));
  $$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form && el.form.submit()));
  // Bilder, die nicht laden, ausblenden
  document.addEventListener("error", (e) => {
    if (e.target.tagName === "IMG" && e.target.hasAttribute("data-hide-broken")) e.target.remove();
  }, true);
  $$("img[data-hide-broken]").forEach((img) => { if (img.complete && img.naturalWidth === 0) img.remove(); });
  const scrollTarget = $("[data-scroll-to]");
  if (scrollTarget) {
    const el = document.getElementById(scrollTarget.dataset.scrollTo);
    if (el) el.scrollIntoView({ block: scrollTarget.dataset.scrollBlock || "start" });
  }

  // Hinweise blenden sich nach 20 s aus – aber nicht, solange Maus oder Fokus darauf liegen.
  // Fehler und Warnungen bleiben stehen, bis sie geschlossen werden.
  $$(".flash").forEach((el) => {
    if (el.classList.contains("error") || el.classList.contains("warning")) return;
    let hold = false;
    const fade = () => {
      if (hold) { setTimeout(fade, 2000); return; }
      el.style.transition = "opacity .4s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 400);
    };
    ["mouseenter", "focusin"].forEach((ev) => el.addEventListener(ev, () => { hold = true; }));
    ["mouseleave", "focusout"].forEach((ev) => el.addEventListener(ev, () => { hold = false; }));
    setTimeout(fade, 20000);
  });

  // Kachel-/Listenansicht im Katalog
  const viewTarget = $("[data-view-target]");
  if (viewTarget) {
    const apply = (v) => {
      $(".view-grid", viewTarget).classList.toggle("hide", v === "list");
      $(".view-list", viewTarget).classList.toggle("hide", v !== "list");
      $$("[data-view]").forEach((b) => { b.classList.toggle("on", b.dataset.view === v); b.setAttribute("aria-pressed", String(b.dataset.view === v)); });
    };
    apply(store.get("inventoryView") || "list");
    $$("[data-view]").forEach((b) => b.addEventListener("click", () => { store.set("inventoryView", b.dataset.view); apply(b.dataset.view); }));
  } else {
    $$("[data-view]").forEach((b) => b.closest(".seg") && b.closest(".seg").classList.add("hide"));
  }

  // Listen filtern (z. B. Gruppenmitglieder)
  $$("[data-filter-list]").forEach((input) => {
    const list = $(input.dataset.filterList);
    input.addEventListener("input", () => {
      const q = input.value.toLowerCase();
      $$("[data-filter-item]", list).forEach((el) => { el.style.display = el.textContent.toLowerCase().includes(q) ? "" : "none"; });
    });
  });

  // Vorlagen für den Verzeichnisdienst
  const PRESETS = {
    ad: { ldap_user_attr: "sAMAccountName", ldap_user_filter: "(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))", nested: true },
    samba: { ldap_user_attr: "sAMAccountName", ldap_user_filter: "(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))", nested: true },
    openldap: { ldap_user_attr: "uid", ldap_user_filter: "(objectClass=posixAccount)", nested: false },
  };
  $$("[data-ldap-preset]").forEach((btn) => btn.addEventListener("click", () => {
    const p = PRESETS[btn.dataset.ldapPreset];
    const form = btn.closest("form");
    form.elements.ldap_user_attr.value = p.ldap_user_attr;
    form.elements.ldap_user_filter.value = p.ldap_user_filter;
    form.elements.ldap_nested_groups.checked = p.nested;
    $$("[data-ldap-preset]").forEach((b) => b.classList.toggle("on", b === btn));
  }));

  // AD-Gruppe per Klick in die passende Rollenliste übernehmen
  $$("[data-add-rule]").forEach((btn) => btn.addEventListener("click", () => {
    const box = $(`[data-rule-box="${btn.dataset.addRule}"]`);
    if (!box) return;
    const name = btn.dataset.name;
    const lines = box.value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.some((l) => l.toLowerCase() === name.toLowerCase())) lines.push(name);
    box.value = lines.join("\n");
    btn.classList.add("on");
    box.style.transition = "box-shadow .3s";
    box.style.boxShadow = "0 0 0 3px color-mix(in srgb, var(--accent) 35%, transparent)";
    setTimeout(() => { box.style.boxShadow = ""; }, 700);
  }));

  // SMTP: Standard-Port passend zur Verschlüsselung setzen
  const secSel = $("[data-smtp-security]"), portIn = $("[data-smtp-port]");
  if (secSel && portIn) {
    const defaults = { starttls: "587", ssl: "465", none: "25" };
    secSel.addEventListener("change", () => {
      if (!portIn.value || Object.values(defaults).includes(portIn.value)) portIn.value = defaults[secSel.value];
    });
  }

  /* ------------------------------------------------------------ Erscheinungsbild: Live-Vorschau */
  const brandForm = $("[data-branding-form]");
  if (brandForm) {
    const root = document.documentElement.style;
    const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const hex = (c) => "#" + c.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
    const mix = (a, b, t) => { const x = rgb(a), y = rgb(b); return hex(x.map((v, i) => v + (y[i] - v) * t)); };
    const lum = (h) => { const c = rgb(h).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
    const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
    const ink = (bg) => (contrast(bg, "#ffffff") >= contrast(bg, "#14191a") ? "#ffffff" : "#14191a");
    const apply = {
      accent(c) { root.setProperty("--accent", c); root.setProperty("--accent-ink", ink(c)); root.setProperty("--accent-soft", mix(c, "#fffdf8", 0.84)); },
      brass(c) { root.setProperty("--brass", c); root.setProperty("--brass-ink", ink(c)); root.setProperty("--brass-soft", mix(c, "#fffdf8", 0.82));
        root.setProperty("--auth-eyebrow", mix(c, "#ffffff", 0.3)); },
      side(c) { const dark = lum(c) < 0.3; const sink = dark ? "#ece7dc" : "#1d2421";
        root.setProperty("--side", c); root.setProperty("--side-2", dark ? mix(c, "#ffffff", 0.08) : mix(c, "#000000", 0.07));
        root.setProperty("--side-ink", sink); root.setProperty("--side-muted", mix(sink, c, 0.38));
        root.setProperty("--auth-a", mix(c, "#ffffff", 0.06)); root.setProperty("--auth-b", mix(c, "#000000", 0.35)); },
    };
    $$("[data-color-picker]", brandForm).forEach((picker) => {
      const key = picker.dataset.colorPicker;
      picker.addEventListener("input", () => {
        $(`[data-color-value="${key}"]`).value = picker.value;
        $(`[data-color-label="${key}"]`).textContent = picker.value;
        apply[key](picker.value);
      });
    });
    $$("[data-color-reset]", brandForm).forEach((btn) => btn.addEventListener("click", () => {
      const key = btn.dataset.colorReset;
      $(`[data-color-value="${key}"]`).value = "";
      $(`[data-color-label="${key}"]`).textContent = T("Standard");
      $(`[data-color-picker="${key}"]`).value = btn.dataset.default;
      apply[key](btn.dataset.default);
    }));
    const preview = $("[data-login-preview]");
    $$("[data-file-preview]", brandForm).forEach((input) => input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const url = URL.createObjectURL(file);
      if (input.dataset.filePreview === "logo") {
        $$("[data-preview-logo]").forEach((img) => { img.src = url; img.hidden = false; });
        $$("[data-preview-nologo]").forEach((el) => { el.hidden = true; });
        const ll = $("[data-preview-loginlogo]"); if (ll) ll.hidden = false;
      } else {
        preview.style.setProperty("--auth-bg", `url("${url}")`);
        preview.classList.add("has-bg");
      }
    }));
    const dim = $("[data-dim]", brandForm);
    if (dim) dim.addEventListener("input", () => {
      preview.style.setProperty("--auth-dim", dim.value / 100);
      $("[data-dim-label]").textContent = dim.value;
    });
    const shelf = $("[data-shelf-toggle]", brandForm);
    if (shelf) shelf.addEventListener("change", () => { $("[data-preview-shelf]").hidden = !shelf.checked; });
    const plate = $("[data-plate-toggle]", brandForm);
    if (plate) plate.addEventListener("change", () => { const ll = $("[data-preview-loginlogo]"); if (ll) ll.classList.toggle("plate", plate.checked); });
    $$("[data-bind]", brandForm).forEach((el) => el.addEventListener("input", () => {
      $(`[data-preview-${el.dataset.bind}]`).textContent = el.value;
    }));
  }

  /* ------------------------------------------------------------ Sicherheitsschlüssel (WebAuthn) */
  const b64u = {
    dec(str) {
      const s = str.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((str.length + 3) % 4);
      return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    },
    enc(buf) {
      const bytes = new Uint8Array(buf);
      let bin = "";
      bytes.forEach((b) => { bin += String.fromCharCode(b); });
      return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    },
  };
  const webauthnOk = () => window.isSecureContext && !!window.PublicKeyCredential;

  function creationOptions(o) {
    const pk = { ...o.publicKey };
    pk.challenge = b64u.dec(pk.challenge);
    pk.user = { ...pk.user, id: b64u.dec(pk.user.id) };
    pk.excludeCredentials = (pk.excludeCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
    return { publicKey: pk };
  }
  function requestOptions(o) {
    const pk = { ...o.publicKey };
    pk.challenge = b64u.dec(pk.challenge);
    pk.allowCredentials = (pk.allowCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
    return { publicKey: pk };
  }
  function credentialJSON(cred) {
    const r = cred.response;
    const out = { id: cred.id, rawId: b64u.enc(cred.rawId), type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}, response: {} };
    out.response.clientDataJSON = b64u.enc(r.clientDataJSON);
    if (r.attestationObject) out.response.attestationObject = b64u.enc(r.attestationObject);
    if (r.authenticatorData) out.response.authenticatorData = b64u.enc(r.authenticatorData);
    if (r.signature) out.response.signature = b64u.enc(r.signature);
    if (r.userHandle) out.response.userHandle = b64u.enc(r.userHandle);
    return out;
  }
  const webauthnError = (err) => (err && err.name === "NotAllowedError")
    ? T("Abgebrochen oder Zeit abgelaufen. Bitte erneut versuchen.")
    : (err && err.name === "InvalidStateError") ? T("Dieser Schlüssel ist bereits registriert.")
    : T("Fehler: {error}", { error: (err && (err.message || err.name)) || err });

  $$("[data-webauthn-register]").forEach((btn) => {
    const box = btn.closest("[data-webauthn-register-box]");
    const status = $("[data-webauthn-status]", box);
    if (!webauthnOk()) {
      btn.disabled = true;
      status.textContent = T("Sicherheitsschlüssel gehen nur über HTTPS (z. B. über deinen Reverse-Proxy), nicht per http://IP.");
      return;
    }
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      status.textContent = T("Schlüssel einstecken und berühren …");
      try {
        const begin = await api("/profil/sicherheit/schluessel/start", { method: "POST", body: "{}" });
        if (!begin.ok) throw new Error(begin.error);
        const cred = await navigator.credentials.create(creationOptions(begin.options));
        const done = await api("/profil/sicherheit/schluessel/speichern", { method: "POST",
          body: JSON.stringify({ credential: credentialJSON(cred), name: $("[data-webauthn-name]", box).value }) });
        if (!done.ok) throw new Error(done.error);
        window.location.href = done.redirect;
      } catch (err) {
        status.textContent = webauthnError(err);
        btn.disabled = false;
      }
    });
  });

  $$("[data-webauthn-login]").forEach((btn) => {
    const status = $("[data-webauthn-status]");
    if (!webauthnOk()) {
      btn.disabled = true;
      if (status) status.textContent = T("Sicherheitsschlüssel gehen nur über HTTPS.");
      return;
    }
    const run = async () => {
      btn.disabled = true;
      if (status) status.textContent = T("Schlüssel einstecken und berühren …");
      try {
        const begin = await api("/login/2fa/schluessel/start", { method: "POST", body: "{}" });
        if (!begin.ok) throw new Error(begin.error);
        const cred = await navigator.credentials.get(requestOptions(begin.options));
        const done = await api("/login/2fa/schluessel/pruefen", { method: "POST", body: JSON.stringify(credentialJSON(cred)) });
        if (!done.ok) throw new Error(done.error);
        window.location.href = done.redirect;
      } catch (err) {
        if (status) status.textContent = webauthnError(err);
        btn.disabled = false;
      }
    };
    btn.addEventListener("click", run);
  });

  $$("[data-copy-codes]").forEach((btn) => btn.addEventListener("click", async () => {
    const text = $$("[data-codes] > div").map((d) => d.textContent.trim()).join("\n");
    try { await navigator.clipboard.writeText(text); btn.textContent = T("✓ Kopiert"); } catch (e) { window.prompt(T("Codes:"), text); }
  }));

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      headers: { "X-CSRFToken": CSRF, "Accept": "application/json", ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      ...opts,
    });
    let data = {};
    try { data = await res.json(); } catch (e) {
      // kein JSON – meist ist die Sitzung abgelaufen und es kam die Anmeldeseite zurück
      if (res.redirected || res.ok) data.error = T("Sitzung abgelaufen – bitte die Seite neu laden.");
    }
    if (!res.ok && !data.error) data.error = T("Fehler {status}", { status: res.status });
    return data;
  }

  /* ------------------------------------------------------------ Ausgabe: unbefristet */
  $$("[data-no-due]").forEach((box) => {
    const date = box.form && box.form.elements.due_at;
    const sync = () => { if (date) { date.disabled = box.checked; if (box.checked) date.value = ""; } };
    box.addEventListener("change", sync);
    sync();
  });

  /* ------------------------------------------------------------ Vertrauliche Angaben kopieren */
  $$("[data-copy-secret]").forEach((btn) => btn.addEventListener("click", async () => {
    const box = $("[data-secret]");
    if (!box) return;
    const label = btn.innerHTML;
    try { await navigator.clipboard.writeText(box.textContent.trim()); btn.textContent = T("✓ Kopiert"); }
    catch (e) { window.getSelection().selectAllChildren(box); btn.textContent = T("Markiert – mit Strg+C kopieren"); }
    setTimeout(() => { btn.innerHTML = label; }, 2500);
  }));

  /* ------------------------------------------------------------ Hilfe */
  const helpBox = $("[data-help-topics]");
  if (helpBox) {
    const topics = $$("[data-help-topic]", helpBox);
    const links = $$("[data-help-link]");
    const focus = helpBox.dataset.helpFocus;
    if (focus && document.getElementById(focus)) {
      const el = document.getElementById(focus);
      el.classList.add("focus");
      el.scrollIntoView({ block: "start" });
    }
    const search = $("[data-help-search]");
    if (search) search.addEventListener("input", () => {
      const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      topics.forEach((t) => {
        const text = t.textContent.toLowerCase();
        const hit = words.every((w) => text.includes(w));
        t.hidden = !hit;
        if (hit) shown += 1;
        const link = links.find((a) => a.dataset.helpLink === t.id);
        if (link) link.hidden = !hit;
      });
      const empty = $("[data-help-empty]");
      if (empty) empty.style.display = shown ? "none" : "block";
    });
  }

  /* ------------------------------------------------------------ Erfassen: aus dem Bestand ergänzen
     Bezeichnung oder Modell aus der Liste gewählt (oder genau so getippt) → leere Felder wie Hersteller,
     Modell und Kategorie mit dem ausfüllen, was im Bestand meist dazugehört. Ausgefüllte Felder bleiben. */
  const knownEl = $("#asset-known");
  if (knownEl) {
    let known = {};
    try { known = JSON.parse(knownEl.textContent || "{}"); } catch (e) { known = {}; }
    const fill = (name, value) => {
      const field = knownEl.form ? knownEl.form.elements[name] : document.querySelector(`[name="${name}"]`);
      if (field && value && !field.value.trim()) {
        field.value = value;
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
    };
    $$("[data-known-fill]").forEach((input) => {
      const table = input.dataset.knownFill === "name" ? known.names : known.models;
      const apply = () => {
        const hit = table && Object.prototype.hasOwnProperty.call(table, input.value.trim()) ? table[input.value.trim()] : null;
        if (hit) Object.entries(hit).forEach(([k, v]) => fill(k, v));
      };
      // „input“: Auswahl aus der Liste füllt sofort aus (Chrome meldet „change“ erst beim Verlassen des Felds);
      // nur bei genau passendem Eintrag – beim Tippen eines Anfangs passiert nichts
      input.addEventListener("input", apply);
      input.addEventListener("change", apply);
    });
  }

  /* ------------------------------------------------------------ Vorschläge beim Tippen
     <input data-suggest="URL" data-suggest-for="suche|theke" data-suggest-action="open|issue"
            [data-suggest-user="benutzername"] [data-suggest-more="URL mit ?q= am Ende"]>
     Ein Barcodescanner tippt schnell und drückt Enter: Enter ohne markierten Vorschlag sendet das Formular wie
     bisher ab – die Vorschläge stören den Scanner also nicht. */
  let suggestSeq = 0;
  $$("input[data-suggest]").forEach((input) => {
    const host = input.parentElement;
    const boxId = `suggest-${++suggestSeq}`;
    host.classList.add("suggest-host");
    const box = document.createElement("div");
    box.className = "suggest-box";
    box.hidden = true;
    box.setAttribute("role", "listbox");
    box.id = boxId;
    host.appendChild(box);
    input.setAttribute("autocomplete", "off");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", boxId);
    input.setAttribute("aria-expanded", "false");
    // shownQ: Text, zu dem die angezeigte Liste gehört. Nur dann darf Enter einen Vorschlag wählen – sonst
    // (z. B. Scanner tippt nach einer Pfeil-Auswahl eine neue Nummer) wird das Formular normal abgeschickt.
    let timer = null, ctrl = null, items = [], active = -1, lastQ = null, shownQ = null, submitted = false;

    const mark = (i) => {
      active = i;
      $$(".suggest-item", box).forEach((el, n) => el.classList.toggle("on", n === i));
      const el = $$(".suggest-item", box)[i];
      if (el) { el.scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", el.id); }
      else input.removeAttribute("aria-activedescendant");
    };
    const close = () => { box.hidden = true; mark(-1); shownQ = null; input.setAttribute("aria-expanded", "false"); };
    const choose = (item) => {
      if (!item) return;
      if (input.dataset.suggestAction === "person" && item.post) {
        // AD-Person ohne Konto: Konto übernehmen (POST mit CSRF-Token, der Server prüft live im AD)
        const form = document.createElement("form");
        form.method = "post";
        form.action = item.post.url;
        form.hidden = true;
        [["csrf_token", CSRF], ["username", item.post.username]].forEach(([n, v]) => {
          const f = document.createElement("input");
          f.type = "hidden"; f.name = n; f.value = v;
          form.appendChild(f);
        });
        document.body.appendChild(form);
        submitted = true; close();
        form.submit();
        return;
      }
      if (input.dataset.suggestAction === "issue") {
        if (!item.can_issue) return;
        const form = input.form;
        let hidden = form.querySelector('input[name="asset_id"]');
        if (!hidden) {
          hidden = document.createElement("input");
          hidden.type = "hidden"; hidden.name = "asset_id";
          form.appendChild(hidden);
        }
        hidden.value = String(item.id);
        submitted = true; close();
        form.submit();
      } else {
        submitted = true; close();
        window.location.href = item.url;
      }
    };
    const render = (data, q) => {
      items = data.items || [];
      if (!items.length) {
        box.innerHTML = `<div class="suggest-note">${esc(T("Keine Treffer."))}</div>`;
      } else {
        box.innerHTML = items.map((it, n) => {
          let sub = [it.numbers ? `<span class="mono">${esc(it.numbers)}</span>` : "", esc(it.category)]
            .filter(Boolean).join(" · ");
          let badge;
          if (input.dataset.suggestAction === "person") {
            sub = esc(it.sub);
            badge = it.badge ? `<span class="badge accent">${esc(it.badge)}</span>` : "";
          } else if (input.dataset.suggestAction === "issue" && it.available > 0 && it.can_issue === false) {
            badge = `<span class="badge warn">${esc(T("nicht für diese Person"))}</span>`;
          } else if (it.available > 0) {
            badge = `<span class="badge ok">${esc(T("{n} von {total} frei", { n: it.available, total: it.quantity }))}</span>`;
          } else {
            badge = `<span class="badge warn">${esc(T("ausgegeben"))}</span>`;
          }
          const disabled = input.dataset.suggestAction === "issue" && !it.can_issue;
          return `<button type="button" tabindex="-1" class="suggest-item" role="option" id="${boxId}-${n}" data-n="${n}"${disabled ? ' aria-disabled="true"' : ""}>
            <span class="grow"><b>${esc(it.name)}</b><small>${sub}</small></span>${badge}</button>`;
        }).join("");
      }
      const more = input.dataset.suggestMore;
      if (more && (data.more || items.length)) {
        box.insertAdjacentHTML("beforeend",
          `<a class="suggest-more" tabindex="-1" href="${esc(more + encodeURIComponent(q))}">${esc(T("Alle Treffer anzeigen"))}</a>`);
      }
      box.hidden = false;
      input.setAttribute("aria-expanded", "true");
      shownQ = q;
      mark(-1);
    };
    const load = () => {
      const q = input.value.trim();
      if (submitted) return;
      if (!q) { lastQ = null; close(); return; }
      if (q === lastQ && !box.hidden) return;
      lastQ = q;
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      const url = new URL(input.dataset.suggest, window.location.href);
      url.searchParams.set("q", q);
      url.searchParams.set("fuer", input.dataset.suggestFor || "suche");
      if (input.dataset.suggestUser) url.searchParams.set("benutzer", input.dataset.suggestUser);
      fetch(url, { credentials: "same-origin", signal: ctrl.signal, headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((data) => { if (!submitted && input.value.trim() === q && document.activeElement === input) render(data, q); })
        .catch(() => { /* abgebrochen oder offline – ohne Vorschläge weiter */ });
    };
    input.addEventListener("input", () => {
      mark(-1);                       // neue Eingabe: keine alte Auswahl mehr übernehmen
      clearTimeout(timer); timer = setTimeout(load, 180);
    });
    input.addEventListener("focus", () => { if (input.value.trim()) { lastQ = null; load(); } });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(timer);
        if (!box.hidden && active >= 0 && input.value.trim() === shownQ) { e.preventDefault(); choose(items[active]); }
        else { submitted = true; close(); if (ctrl) ctrl.abort(); }
        return;
      }
      if (box.hidden || !items.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); mark(Math.min(items.length - 1, active + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); mark(Math.max(-1, active - 1)); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
    // Zurück-Taste (Seite aus dem Browser-Cache): alten Zustand verwerfen
    window.addEventListener("pageshow", (e) => {
      if (!e.persisted) return;
      submitted = false; lastQ = null; close();
      const hidden = input.form && input.form.querySelector('input[name="asset_id"]');
      if (hidden) hidden.remove();
    });
    box.addEventListener("mousedown", (e) => e.preventDefault());   // Fokus im Feld lassen
    box.addEventListener("click", (e) => {
      const el = e.target.closest(".suggest-item");
      if (el) choose(items[Number(el.dataset.n)]);
    });
  });

  /* ------------------------------------------------------------ Inventar-/Seriennummer prüfen (Formular)
     <input data-number-check="URL" data-field="inventory_no|serial_number" data-except="ID"> + [data-number-hint] */
  $$("input[data-number-check]").forEach((input) => {
    const hint = input.closest(".field").querySelector("[data-number-hint]");
    if (!hint) return;
    let timer = null, ctrl = null, seq = 0;
    const field = input.dataset.field;
    const show = (data, value) => {
      const parts = [];
      input.classList.toggle("is-taken", !!data.taken);
      input.setAttribute("aria-invalid", data.taken ? "true" : "false");
      if (value && data.taken) {
        parts.push(data.by
          ? `<span class="taken">${esc(T("Schon vergeben"))}</span> – <a href="${esc(data.by.url)}" target="_blank" rel="noopener">${esc(data.by.name)}</a>`
          : `<span class="taken">${esc(T("Schon vergeben"))}</span>`);
      } else if (value && !(data.numbers && data.numbers.length)) {
        parts.push(`<span class="free">${esc(T("Frei"))}</span>`);
      }
      if (data.numbers && data.numbers.length && !(data.numbers.length === 1 && data.numbers[0].toLowerCase() === value.toLowerCase())) {
        parts.push(`${esc(T("Vorhanden:"))} <span class="mono">${data.numbers.map(esc).join(", ")}</span>`);
      }
      if (data.next && data.next.toLowerCase() !== value.toLowerCase()) {
        parts.push(`${esc(T("Nächste freie:"))} <button type="button" class="linklike mono" data-take="${esc(data.next)}">${esc(data.next)}</button>`);
      }
      hint.innerHTML = parts.join(" · ");
    };
    const check = () => {
      const value = input.value.trim();
      if (ctrl) ctrl.abort();
      const mine = ++seq;              // ältere Antworten verwerfen
      if (field === "serial_number" && !value) {
        hint.innerHTML = ""; input.classList.remove("is-taken"); input.setAttribute("aria-invalid", "false");
        return;
      }
      ctrl = new AbortController();
      const url = new URL(input.dataset.numberCheck, window.location.href);
      url.searchParams.set("feld", field);
      url.searchParams.set("wert", value);
      if (input.dataset.except) url.searchParams.set("ausser", input.dataset.except);
      fetch(url, { credentials: "same-origin", signal: ctrl.signal, headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((data) => { if (mine === seq) show(data, value); })
        .catch(() => { /* ohne Hinweis weiter – der Server prüft beim Speichern ohnehin */ });
    };
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(check, 250); });
    input.addEventListener("focus", check);
    if (input.value.trim()) check();
    hint.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-take]");
      if (!btn) return;
      input.value = btn.dataset.take;
      input.focus();
      check();
    });
  });
})();
