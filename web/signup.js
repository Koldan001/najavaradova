/* The "Prijavi se" form: the signup window on the home page and on every city page (/grad/…/).
   One copy of the form for both, so they can't drift apart. Moved here unchanged from
   web/index.html (2026-10-08); its look is web/signup.css.

   window.NRSignup.open(opts)   open the form, optionally filled in (see open() below)
   window.NRSignup.openFromUrl() open it from /?prijava=1&grad=...&ulica=...&broj=...
   window.NRSignup.me           a promise: is the visitor logged in?
   Events on document: "nrsignup:open", "nrsignup:close" (a page may make room for the form).

   Loaded with a plain <script> at the end of <body> (home) or with defer (city pages), so the
   body exists when it runs. */
(function () {

// Registration temporarily off on the live site (map/notification data stays real and live -
// this only blocks new sign-ups) - flip to false to re-open registration. Single switch, checked
// once at init (see the bottom of this script) to blur the form and show the lock overlay.
var REGISTRATION_DISABLED = false;

// localhost -> same-origin (local_web_server.py serves both page and API on :8000). Anywhere
// else -> the real API subdomain on the VM, since Cloudflare Pages (serving this page) has no
// backend of its own.
var API_BASE = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
  ? "" : "https://app.najavaradova.rs";
var DASHBOARD_URL = "https://app.najavaradova.rs/dashboard";

// Best-effort "is this visitor already logged in?" check, so re-opening the signup modal
// doesn't send someone who already confirmed their email yet another magic-link mail for no
// reason (confirmed real complaint: this origin and app.najavaradova.rs can't otherwise tell
// each other's login state apart - different subdomains). credentials:'include' works
// cross-origin here specifically because the two are same-SITE (share the registrable domain
// najavaradova.rs, differ only by subdomain) - SameSite=Lax on the session cookie already
// permits that, the server just needs Access-Control-Allow-Credentials for JS to read the
// response (see _cors_headers() in scripts/local_web_server.py). Defaults to "logged out" (the
// existing, safe behaviour) on any failure or before this resolves - never blocks the modal
// waiting on it. /api/session, not /api/me: it answers 200 either way, so a logged-out visitor
// (nearly everyone) doesn't leave a 401 error in the browser console.
var loggedIn = false;
var me = fetch(API_BASE + "/api/session", { credentials: "include" })
  .then(function (r) { return r.ok ? r.json() : null; })
  .then(function (d) { loggedIn = !!(d && d.logged_in); return loggedIn; })
  .catch(function () { loggedIn = false; return false; });

document.body.insertAdjacentHTML("beforeend", `<div class="modal-overlay" id="modalOverlay" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <button type="button" class="modal-close" id="modalClose" aria-label="Zatvori">&times;</button>
    <h3 id="modalTitle">Prijavi se za notifikacije</h3>
    <div class="modal-form-wrap" id="modalFormWrap">
    <div class="modal-lock-overlay" id="modalLockOverlay" hidden>
      <div class="icon">🚧</div>
      <h4>Prijave uskoro</h4>
      <p>Radimo na proširenju pokrivenosti gradova. Prijave se privremeno ne primaju — mapa i najave ostaju žive.</p>
    </div>
    <p class="modal-sub">Besplatno. Javljamo kad se najava odnosi na tvoju adresu, ne na ceo grad. Potvrdiš mejl jednom - posle toga se prijavljuješ istim linkom, bez lozinke.</p>
    <p class="field-hint" id="coverageList" style="margin-bottom:16px"></p>
    <form id="regForm">
      <div class="field">
        <label for="fCity">Grad / mesto</label>
        <input type="text" id="fCity" name="city" placeholder="Počni da kucaš…" autocomplete="off" required>
        <div class="suggest-list" id="citySuggest" hidden></div>
        <div class="field-hint" id="cityHint" hidden>Za ovo mesto još nemamo listu ulica &mdash; upiši ulicu i broj ručno. Prijava se čuva; obaveštenja kreću čim pokrijemo tvoj grad.</div>
      </div>
      <div class="field">
        <label for="fUlica">Ulica</label>
        <input type="text" id="fUlica" name="ulica" placeholder="Prvo izaberi grad" autocomplete="off" disabled required>
        <div class="suggest-list" id="streetSuggest" hidden></div>
        <input type="hidden" id="fUlicaId">
      </div>
      <div class="field">
        <label for="fBroj">Broj</label>
        <input type="text" id="fBroj" name="broj" placeholder="Prvo izaberi ulicu" autocomplete="off" maxlength="5" disabled required>
        <div class="suggest-list" id="brojSuggest" hidden></div>
      </div>
      <div class="field">
        <label for="fEmail">Email adresa</label>
        <input type="email" id="fEmail" name="email" placeholder="ti@example.com" required>
        <div class="field-hint">Šaljemo ti link da potvrdiš adresu - bez lozinke. Isti mejl ti otvara tvoju stranicu sa svim adresama, kad god se vratiš.</div>
      </div>
      <div class="field">
        <label>Obaveštenje putem</label>
        <div class="channel-toggle">
          <button type="button" class="chan on" id="chanEmail" data-chan="email">Email</button>
          <button type="button" class="chan" id="chanTelegram" data-chan="telegram">Telegram</button>
        </div>
        <div class="field-hint">Email je podrazumevan. Telegram izaberi ako ga koristiš.</div>
      </div>
      <div class="field" id="ackUncoveredField" hidden style="margin-top:8px">
        <label style="display:flex;gap:8px;align-items:flex-start;font-weight:400;font-size:13.5px;color:var(--dim)">
          <input type="checkbox" id="fAckUncovered" style="margin-top:3px;flex:none">
          <span>Razumem da ovo mesto još nije pokriveno i da možda neću dobijati obaveštenja.</span>
        </label>
      </div>
      <div class="modal-error" id="modalError" hidden role="alert" aria-live="assertive"></div>
      <p class="reg-summary" id="regSummary" hidden></p>
      <button type="submit" class="modal-submit" id="modalSubmit" disabled>Potvrdi prijavu</button>
      <p class="field-hint" style="text-align:center;margin-top:12px">
        Klikom na &bdquo;Potvrdi prijavu&ldquo; prihvataš
        <a href="https://najavaradova.rs/terms" target="_blank" rel="noopener">Uslove korišćenja</a> i
        <a href="https://najavaradova.rs/privacy" target="_blank" rel="noopener">Politiku privatnosti</a>.
        Čuvamo samo tvoju adresu i mejl, isključivo za slanje ovih obaveštenja i prijavu na tvoju
        stranicu. Ne prodajemo i ne delimo podatke. Brisanje u jednom kliku, bilo kad.
      </p>
    </form>
    </div>
    <div class="modal-success" id="modalSuccess" hidden>
      <div class="modal-success-icon">✅</div>
      <div id="modalSuccessText"></div>
      <div class="modal-hint">
        <b>Ne vidiš mejl?</b> Pogledaj u folderu <b>Spam</b> ili <b>Promocije</b> - stiže od NajavaRadova.rs.
        Link u mejlu važi 24 sata.
      </div>
      <div class="modal-mail-actions">
        <a class="modal-mail-btn" id="openGmail" href="https://mail.google.com/mail/u/0/#search/najavaradova" target="_blank" rel="noopener" hidden>Otvori Gmail</a>
        <button type="button" class="modal-mail-btn" id="resendBtn">Pošalji ponovo</button>
      </div>
      <button type="button" class="modal-submit" id="modalDone">U redu</button>
    </div>
  </div>
</div>
`);
// The form's colours come from the home page's theme. A city page has none, so the form brings
// the same colours along (.nr-own-theme in signup.css).
if (!getComputedStyle(document.documentElement).getPropertyValue("--card-bg").trim()) {
  document.getElementById("modalOverlay").classList.add("nr-own-theme");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

var selectedChannel = "email";
var CITY_LIST = [];         // raw naselje_lat strings (real place/town names), loaded once
var CITY_FOLDED = [];       // fold()'d parallel array, so typing is matched diacritic/script-free
var CITY_NOSTREET = [];     // Serbian opštine we have NO RGZ streets for - autocomplete still
var CITY_NOSTREET_FOLDED = []; // offers them, picking one = uncovered mode (free-text street)
var CITY_OPSTINE = [];      // opstina_lat of each CITY_LIST place, same order ("GROCKA" for Kaluđerica)
var CITY_OPSTINE_FOLDED = [];
var cityKnown = false;      // does the current fCity text match a city we have street data for
var lastResolvedCity = "";  // last fCity text applyCityMode actually ran for - guards the blur
// Names people type for a city we list under its Serbian one (fold()ed keys). Mirrors
// CITY_ALIASES in scripts/local_web_server.py.
var CITY_ALIASES = { belgrade: "Beograd", belgrad: "Beograd", nish: "Niš" };
                             // handler below from re-firing (and wiping ulica/broj) on a blur
                             // that follows a pick, where the text hasn't actually changed

// "BEOGRAD (ZEMUN)" -> "Beograd (Zemun)". The letter after "(" must not be swallowed together
// with the space before it: Altina's page used to fill in "Beograd (zemun)".
function titleCase(s) {
  return (s || "").toLowerCase().replace(/(^|[\s(])([^\s(])/g, function (m, before, c) { return before + c.toUpperCase(); });
}

// Normalise a place name / query to a plain-ASCII lowercase form so "indjija" matches "INĐIJA",
// "cacak" matches "ČAČAK", and Cyrillic ("Инђија") matches the Latin naselje_lat the API returns.
var _CYR = {"а":"a","б":"b","в":"v","г":"g","д":"d","ђ":"dj","е":"e","ж":"z","з":"z","и":"i","ј":"j","к":"k","л":"l","љ":"lj","м":"m","н":"n","њ":"nj","о":"o","п":"p","р":"r","с":"s","т":"t","ћ":"c","у":"u","ф":"f","х":"h","ц":"c","ч":"c","џ":"dz","ш":"s"};
var _LAT = {"č":"c","ć":"c","ž":"z","š":"s","đ":"dj"};
function fold(s) {
  s = (s || "").toLowerCase();
  var out = "";
  for (var i = 0; i < s.length; i++) {
    var ch = s[i];
    out += _CYR[ch] || _LAT[ch] || ch;
  }
  return out;
}

// True when `q` is a prefix of `name` or of any word inside it - so "pazova" finds
// "Stara Pazova" / "Nova Pazova", "st paz" finds "Stara Pazova", both after fold(). Also
// matches a word right after "(" so "novi beograd" / "zemun" finds "Beograd (Novi Beograd)".
function nameMatches(foldedName, foldedQ) {
  return foldedName.indexOf(foldedQ) === 0
      || foldedName.indexOf(" " + foldedQ) !== -1
      || foldedName.indexOf("(" + foldedQ) !== -1;
}

// City suggestions for a fold()ed query: places by name; then, for 3+ letters, every place of an
// opština that matches ("Grocka" -> Kaluđerica, Vinča... - someone in a village may only know
// their opština); then the opštine we have no street data for, so the field autocompletes
// everywhere in Serbia (picking one = "upiši ulicu ručno", not a dead end).
function citySuggestions(fq) {
  var hits = [], seen = {};
  for (var i = 0; i < CITY_LIST.length && hits.length < 12; i++) {
    if (nameMatches(CITY_FOLDED[i], fq)) { hits.push({ name: CITY_LIST[i], known: true }); seen[i] = 1; }
  }
  for (var o = 0; fq.length >= 3 && o < CITY_LIST.length && hits.length < 24; o++) {
    if (!seen[o] && CITY_OPSTINE_FOLDED[o] && nameMatches(CITY_OPSTINE_FOLDED[o], fq)) {
      hits.push({ name: CITY_LIST[o], known: true, sub: "opština " + titleCase(CITY_OPSTINE[o].split(" (")[0]) });
    }
  }
  for (var j = 0; j < CITY_NOSTREET.length && hits.length < 30; j++) {
    if (nameMatches(CITY_NOSTREET_FOLDED[j], fq)) hits.push({ name: CITY_NOSTREET[j], known: false, sub: "još bez liste ulica" });
  }
  return hits;
}

// The place we have streets for that a typed city names as whole words, in a form the list
// doesn't know ("Beograd, Grocka", "Kaluđerica Grocka"): a village beats an opština, an opština
// beats the big city. Null when the text also names an opština we have no streets for ("Jabuka,
// Prijepolje" is a real uncovered village). Mirrors covered_places_named() on the server.
function coveredPlaceNamed(typed) {
  var words = " " + fold(typed).replace(/[^a-z0-9]+/g, " ").trim() + " ";
  function named(f) {
    var k = (f || "").replace(/[^a-z0-9]+/g, " ").trim();
    return k.length >= 3 && words.indexOf(" " + k + " ") !== -1;
  }
  for (var n = 0; n < CITY_NOSTREET_FOLDED.length; n++) if (named(CITY_NOSTREET_FOLDED[n])) return null;
  // A place named next to an opština must lie in it ("Jabuka, Grocka" is not Pančevo's Jabuka).
  var opstina = null, place = null;
  for (var o = 0; o < CITY_OPSTINE_FOLDED.length && !opstina; o++) {
    if (named(CITY_OPSTINE_FOLDED[o].split(" (")[0])) opstina = CITY_OPSTINE[o];
  }
  for (var i = 0; i < CITY_FOLDED.length && !place; i++) {
    var f = CITY_FOLDED[i], fo = (CITY_OPSTINE_FOLDED[i] || "").split(" (")[0], paren = f.indexOf(" (");
    var isNamed = paren !== -1 ? named(f.slice(paren + 2, -1)) : (named(f) && f !== fo);
    if (isNamed && (!opstina || CITY_OPSTINE[i] === opstina)) place = CITY_LIST[i];
  }
  if (place || opstina) return titleCase(place || opstina.split(" (")[0]);
  return named("beograd") || named("belgrade") ? "Beograd" : named("nis") ? "Niš" : null;
}

var citiesLoading = null;   // the one /api/cities request, shared by everyone waiting on the list
function loadCities() {
  if (!citiesLoading) {
    citiesLoading = fetch(API_BASE + "/api/cities").then(function (r) { return r.json(); }).then(function (d) {
      CITY_LIST = d.cities || [];
      CITY_FOLDED = CITY_LIST.map(fold);
      CITY_NOSTREET = d.nostreet || [];
      CITY_NOSTREET_FOLDED = CITY_NOSTREET.map(fold);
      CITY_OPSTINE = d.opstine || [];
      CITY_OPSTINE_FOLDED = CITY_OPSTINE.map(fold);
    }).catch(function () { citiesLoading = null; });   // a failed load may be tried again
  }
  return citiesLoading;
}

// Coverage honesty (PLAN.md §3) - shown once in the modal so a visitor sees which cities are
// actually fed by a scraper before typing an address /auth/start might reject.
function loadCoverage() {
  fetch(API_BASE + "/api/coverage").then(function (r) { return r.json(); }).then(function (d) {
    var full = d.full || [], partial = d.partial || [];
    var el = document.getElementById("coverageList");
    var text = "Trenutno pratimo: " + full.join(", ") + ".";
    if (partial.length) text += " Samo struja (EPS): " + partial.join(", ") + ".";
    el.textContent = text;
  }).catch(function () {});
}

// Generic debounced "type -> fetch suggestions -> click to pick" wiring, shared by the city,
// street, and house-number fields below. fetchItems always returns a Promise, even for the
// city field's plain client-side array filter, so one code path covers all three.
function wireSuggest(input, listEl, fetchItems, renderItem, onPick) {
  var timer = null;
  input.addEventListener("input", function () {
    onPick(null);   // any manual edit invalidates a previous pick
    var q = input.value.trim();
    clearTimeout(timer);
    if (q.length < 1) { listEl.hidden = true; return; }
    timer = setTimeout(function () {
      fetchItems(q).then(function (items) {
        listEl.innerHTML = "";
        if (!items.length) { listEl.hidden = true; return; }
        items.forEach(function (item) {
          var row = document.createElement("div");
          row.className = "suggest-item";
          row.innerHTML = renderItem(item);
          row.onclick = function () { onPick(item); listEl.hidden = true; };
          listEl.appendChild(row);
        });
        listEl.hidden = false;
      });
    }, 200);
  });
  input.addEventListener("blur", function () {
    setTimeout(function () { listEl.hidden = true; }, 150);   // let the click land first
  });
}

var fCity = document.getElementById("fCity");
var citySuggest = document.getElementById("citySuggest");
var cityHint = document.getElementById("cityHint");
var CITY_HINT_UNCOVERED = cityHint.textContent;   // the hint also asks "which one?" - see the city blur
var fUlica = document.getElementById("fUlica");
var fUlicaId = document.getElementById("fUlicaId");
var fBroj = document.getElementById("fBroj");
var streetSuggest = document.getElementById("streetSuggest");
var brojSuggest = document.getElementById("brojSuggest");

// A street and number brought from a city page (prefill below), waiting for
// the city to be set - by the page itself, or by the visitor picking "Beograd (Zemun)" from the
// suggestions when the page could only say "Beograd".
var pendingAddress = null;
var pendingBroj = "";

function applyCityMode(known) {
  cityKnown = known;
  lastResolvedCity = fCity.value.trim().toUpperCase();
  fUlica.value = "";
  fUlicaId.value = "";
  fBroj.value = "";
  fUlica.disabled = false;
  fBroj.disabled = false;
  cityHint.textContent = CITY_HINT_UNCOVERED;
  cityHint.hidden = known;
  // Changing the city invalidates any earlier "this city isn't covered" acknowledgement - don't
  // let a stale checked box silently carry over to a different (possibly covered) address.
  document.getElementById("ackUncoveredField").hidden = true;
  document.getElementById("fAckUncovered").checked = false;
  if (known) {
    fUlica.placeholder = "npr. Futoška";
    fBroj.placeholder = "Prvo izaberi ulicu";
    fBroj.disabled = true;
  } else {
    fUlica.placeholder = "Upiši naziv ulice";
    fBroj.placeholder = "npr. 12, 12a ili bb";
    // Uncovered place: show the acknowledgement now, not after a failed submit round-trip.
    document.getElementById("ackUncoveredField").hidden = false;
  }
  updateSubmitEnabled();
  if (pendingAddress) {
    var addr = pendingAddress;
    pendingAddress = null;
    fillStreet(addr.ulica, addr.broj);
  }
}

wireSuggest(
  fCity, citySuggest,
  function (q) {
    return Promise.resolve(citySuggestions(fold(q)));
  },
  function (item) {
    return titleCase(item.name) + (item.sub ? "<span class='sub'>" + item.sub + "</span>" : "");
  },
  function (picked) {
    if (picked) { fCity.value = titleCase(picked.name); applyCityMode(picked.known); }
    else {
      fUlica.disabled = true;
      fBroj.disabled = true;
      fUlica.placeholder = "Prvo izaberi grad";
      fBroj.placeholder = "Prvo izaberi ulicu";
      cityHint.hidden = true;
      cityKnown = false;
      updateSubmitEnabled();
    }
  }
);

// Typing a city and clicking elsewhere without picking from the dropdown: resolve it fold()'d
// (so "indjija" / "Инђија" still land on "INĐIJA"), snap the field to the canonical name so the
// /api/streets lookup gets the exact naselje_lat, and enable street mode. Ambiguous or no match
// falls back to plain free-text ulica/broj instead of leaving the form stuck disabled.
fCity.addEventListener("blur", function () {
  setTimeout(function () {
    var typed = fCity.value.trim();
    if (!typed || typed.toUpperCase() === lastResolvedCity) return;   // already resolved, e.g. by a click-pick
    var fq = fold(typed);
    if (CITY_ALIASES[fq]) { typed = CITY_ALIASES[fq]; fCity.value = typed; fq = fold(typed); }
    var exact = -1, hits = [];
    for (var i = 0; i < CITY_FOLDED.length; i++) {
      if (CITY_FOLDED[i] === fq) { exact = i; break; }
      if (nameMatches(CITY_FOLDED[i], fq)) hits.push(i);
    }
    var idx = exact !== -1 ? exact : (hits.length === 1 ? hits[0] : -1);
    if (idx !== -1) {
      fCity.value = titleCase(CITY_LIST[idx]);
      applyCityMode(true);
      return;
    }
    // Several covered places match ("Beograd" -> every "Beograd (Čukarica)"..., "Niš", "Novi"):
    // ask for one of them. Falling through to the uncovered mode below told a Belgrade visitor
    // that Belgrade isn't covered and saved an address that never reached the map (19.10.2026).
    if (hits.length > 1) {
      fUlica.disabled = true;
      fBroj.disabled = true;
      fUlica.placeholder = "Prvo izaberi grad";
      cityKnown = false;
      cityHint.textContent = "Izaberi tačno mesto sa liste, npr. " + titleCase(CITY_LIST[hits[0]]) + ".";
      cityHint.hidden = false;
      document.getElementById("ackUncoveredField").hidden = true;
      updateSubmitEnabled();
      return;
    }
    // "Beograd, Grocka", "Kaluđerica Grocka": names a place we have streets for, in a form the
    // list doesn't know. Offer that place's suggestions - never the uncovered mode below, which
    // saved a Kaluđerica address no alert could reach (27.10.2026).
    var named = coveredPlaceNamed(typed);
    if (named) {
      fCity.value = named;
      fCity.dispatchEvent(new Event("input"));
      fCity.focus();
      cityHint.textContent = "Izaberi tačno mesto sa liste (selo ili deo grada).";
      cityHint.hidden = false;
      document.getElementById("ackUncoveredField").hidden = true;
      return;
    }
    // No street-data city matched. Snap to a known opština name for a tidy label if one
    // matches, then continue in uncovered (free-text street) mode either way - never a dead end.
    var nExact = -1, nHits = [];
    for (var k = 0; k < CITY_NOSTREET_FOLDED.length; k++) {
      if (CITY_NOSTREET_FOLDED[k] === fq) { nExact = k; break; }
      if (nameMatches(CITY_NOSTREET_FOLDED[k], fq)) nHits.push(k);
    }
    var nIdx = nExact !== -1 ? nExact : (nHits.length === 1 ? nHits[0] : -1);
    if (nIdx !== -1) fCity.value = titleCase(CITY_NOSTREET[nIdx]);
    applyCityMode(false);
  }, 160);
});

wireSuggest(
  fUlica, streetSuggest,
  function (q) {
    if (!cityKnown) return Promise.resolve([]);
    return fetch(API_BASE + "/api/streets?city=" + encodeURIComponent(fCity.value) + "&q=" + encodeURIComponent(q))
      .then(function (r) { return r.json(); }).then(function (d) { return d.streets || []; });
  },
  function (item) {
    return titleCase(item.name) + "<span class='sub'>" + titleCase(item.opstina) + "</span>";
  },
  function (picked) {
    if (picked) {
      fUlica.value = titleCase(picked.name);
      fUlicaId.value = picked.id;
      fBroj.disabled = false;
      fBroj.placeholder = "npr. 12, 12a ili bb";
      if (pendingBroj) { fBroj.value = pendingBroj; pendingBroj = ""; }
    } else if (cityKnown) {
      fUlicaId.value = "";
      fBroj.value = "";
      fBroj.disabled = true;
      fBroj.placeholder = "Prvo izaberi ulicu";
    }
    updateSubmitEnabled();
  }
);

wireSuggest(
  fBroj, brojSuggest,
  function (q) {
    if (!cityKnown || !fUlicaId.value) return Promise.resolve([]);
    return fetch(API_BASE + "/api/housenumbers?street_id=" + encodeURIComponent(fUlicaId.value) + "&q=" + encodeURIComponent(q))
      .then(function (r) { return r.json(); }).then(function (d) { return d.numbers || []; });
  },
  function (item) { return item; },
  function (picked) { if (picked) fBroj.value = picked; updateSubmitEnabled(); }
);

// Digits, plus an optional trailing letter suffix in any script ("12a"/"12а"), or "bb" (bez
// broja - no assigned number, a real address form, not a placeholder) - matches valid_broj() in
// scripts/local_web_server.py. Anything else (spaces, punctuation) is stripped as you type, not
// just rejected after submit; letters are upper-cased live so what's on screen already matches
// what gets stored ("bb" -> "BB").
fBroj.addEventListener("input", function () {
  var cleaned = fBroj.value.replace(/[^0-9\p{L}]/gu, "").toUpperCase();
  if (cleaned !== fBroj.value) fBroj.value = cleaned;
  updateSubmitEnabled();
});
function validBrojClient(v) {
  var b = (v || "").trim().toUpperCase();
  if (b === "BB") return true;
  return /^\d{1,4}\p{L}?$/u.test(b);
}
fUlica.addEventListener("input", updateSubmitEnabled);
fCity.addEventListener("input", updateSubmitEnabled);
document.getElementById("fEmail").addEventListener("input", updateSubmitEnabled);
document.getElementById("fAckUncovered").addEventListener("change", updateSubmitEnabled);

function updateSubmitEnabled() {
  var cityOk = fCity.value.trim().length >= 2;
  var ulicaOk = cityKnown ? !!fUlicaId.value : fUlica.value.trim().length >= 2;
  var brojOk = validBrojClient(fBroj.value);
  var email = document.getElementById("fEmail").value.trim();
  var emailOk = EMAIL_CLIENT_RE.test(email);
  // An uncovered place needs the acknowledgement ticked - it's revealed up front now, not
  // after a rejected submit, so this just gates the button until they confirm.
  var ackOk = cityKnown || document.getElementById("fAckUncovered").checked;
  var allOk = cityOk && ulicaOk && brojOk && emailOk && ackOk;
  document.getElementById("modalSubmit").disabled = !allOk;

  // Plain-language recap of exactly what this signup does, shown only once every field is
  // valid. A 30-50, low-tech user fills four fields and otherwise submits on faith - this
  // spells out the address and where the messages land before they commit.
  var sum = document.getElementById("regSummary");
  if (sum) {
    if (allOk) {
      var where = "<b>" + esc(titleCase(fUlica.value.trim())) + " " + esc(fBroj.value.trim()) +
        ", " + esc(titleCase(fCity.value.trim())) + "</b>";
      var how = selectedChannel === "telegram"
        ? "Obaveštenja stižu na Telegram; potvrda i prijava idu na <b>" + esc(email) + "</b>."
        : "Obaveštenja i prijava idu na <b>" + esc(email) + "</b>.";
      sum.innerHTML = "Javićemo ti čim najava obuhvati adresu " + where + ". " + how +
        (cityKnown ? "" :
          "<br>Ovo mesto još ne pokrivamo &mdash; čuvamo tvoju prijavu i javljamo čim krene.");
      sum.hidden = false;
    } else {
      sum.hidden = true;
    }
  }
}
var EMAIL_CLIENT_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function resetFormFields() {
  document.getElementById("regForm").reset();
  document.getElementById("regForm").hidden = false;
  fUlicaId.value = "";
  fUlica.disabled = true;
  fUlica.placeholder = "Prvo izaberi grad";
  fBroj.disabled = true;
  fBroj.placeholder = "Prvo izaberi ulicu";
  cityHint.hidden = true;
  cityKnown = false;
  lastResolvedCity = "";
  streetSuggest.hidden = true;
  brojSuggest.hidden = true;
  citySuggest.hidden = true;
  pendingAddress = null;
  pendingBroj = "";
  setChannel("email");
  updateSubmitEnabled();
}

// Accessibility: Escape closes the modal, Tab/Shift+Tab cycles focus without ever leaving it
// (a sighted mouse user never notices this, but without it a keyboard-only user tabbing past
// the last field lands back on the blurred/inert page behind the modal), and focus moves into
// the modal on open and back to whatever opened it on close.
var modalOpenerEl = null;
function modalFocusables() {
  var modal = document.querySelector("#modalOverlay .modal");
  return Array.prototype.slice.call(
    modal.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
  ).filter(function (el) { return el.offsetParent !== null; });   // skip hidden fields
}
function modalKeydown(e) {
  if (e.key === "Escape") { closeModal(); return; }
  if (e.key !== "Tab") return;
  var items = modalFocusables();
  if (!items.length) return;
  var first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
function openModal() {
  // Already logged in (see the /api/me check above) - straight to the dashboard instead of the
  // signup form. Every "Prijavi se" entry point on the page goes through this one function, so
  // this one check covers all of them.
  if (loggedIn) { location.href = DASHBOARD_URL; return; }
  modalOpenerEl = document.activeElement;
  // The page may need to make room: the home page's cookie banner steps aside on this event.
  document.dispatchEvent(new CustomEvent("nrsignup:open"));
  if (!REGISTRATION_DISABLED) { loadCities(); loadCoverage(); }   // locked: no point loading autocomplete data
  resetFormFields();
  document.getElementById("coverageList").hidden = false;
  count("open");
  document.getElementById("modalOverlay").hidden = false;
  document.getElementById("modalSuccess").hidden = true;
  document.getElementById("modalError").hidden = true;
  document.addEventListener("keydown", modalKeydown);
  var items = modalFocusables();
  (items[0] || document.getElementById("modalClose")).focus();
}
function closeModal() {
  document.getElementById("modalOverlay").hidden = true;
  document.removeEventListener("keydown", modalKeydown);
  document.dispatchEvent(new CustomEvent("nrsignup:close"));
  if (modalOpenerEl && modalOpenerEl.focus) modalOpenerEl.focus();
}
function setChannel(chan) {
  // Email is always collected now (it's the account/login identity, not just one of two
  // delivery options) - this toggle only picks which channel actually gets pinged about
  // outages, so it no longer shows/hides or (un)requires the email field.
  selectedChannel = chan;
  document.getElementById("chanTelegram").classList.toggle("on", chan === "telegram");
  document.getElementById("chanEmail").classList.toggle("on", chan === "email");
  updateSubmitEnabled();
}

document.getElementById("modalClose").onclick = closeModal;
document.getElementById("modalDone").onclick = closeModal;

// "Pošalji ponovo" on the check-your-email screen: the same form sent again, which mails a fresh
// link. The server sends at most one per address every 90 s and stays silent past that, so the
// button waits it out and says so instead of looking like it did something.
var sentPayload = null, resendTimer = null;
function holdResend() {
  var btn = document.getElementById("resendBtn"), left = 95;
  clearInterval(resendTimer);
  btn.disabled = true;
  function tick() {
    if (left <= 0) { clearInterval(resendTimer); btn.disabled = false; btn.textContent = "Pošalji ponovo"; return; }
    btn.textContent = "Pošalji ponovo (" + left + " s)";
    left -= 1;
  }
  tick();
  resendTimer = setInterval(tick, 1000);
}
document.getElementById("resendBtn").onclick = function () {
  if (!sentPayload) return;
  var btn = this;
  btn.disabled = true;
  btn.textContent = "Šaljem…";
  fetch(API_BASE + "/auth/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sentPayload)
  })
    .then(function (r) {
      if (!r.ok) throw new Error();
      holdResend();
      document.getElementById("modalSuccessText").textContent =
        "Poslali smo novi mejl na " + sentPayload.email + ". Klikni link iz bilo kog od njih.";
    })
    .catch(function () { btn.disabled = false; btn.textContent = "Pošalji ponovo"; });
};

// Registration locked: the modal still opens (so the message itself is discoverable) but the
// form underneath is blurred/inert and never loads city data or accepts input.
if (REGISTRATION_DISABLED) {
  var formWrap = document.getElementById("modalFormWrap");
  formWrap.classList.add("locked");
  formWrap.inert = true;   // pointer-events:none (in the CSS) blocks mouse but not Tab -
                            // without this a keyboard user could still tab into the blurred,
                            // functionally-dead fields underneath the lock message.
  document.getElementById("modalLockOverlay").hidden = false;
}
document.getElementById("modalOverlay").addEventListener("click", function (e) {
  if (e.target === this) closeModal();
});
document.getElementById("chanTelegram").onclick = function () { setChannel("telegram"); };
document.getElementById("chanEmail").onclick = function () { setChannel("email"); };

// A city or neighbourhood page (/grad/…/) opens this form in place with the names it goes by;
// without JavaScript its link reaches the home page as
// /?prijava=1&grad=Beograd (Zemun)|Zemun|Beograd&ulica=Cvijićeva&broj=55. Either way the form opens with the
// place and, when the street is one we know, the street and number already filled in. `grad`
// lists the names that page may go by, most specific first (signup_places() in
// scripts/build_city_pages.py); the first one on our list wins. With none of them on it, the
// most general one is typed in and its suggestions opened, so "Beograd" offers its opštine.
function fillStreet(ulica, broj) {
  broj = (broj || "").replace(/[^0-9\p{L}]/gu, "").toUpperCase().slice(0, 5);
  if (!ulica) { fUlica.focus(); return; }
  if (!cityKnown) {
    fUlica.value = ulica;
    fBroj.value = broj;
    updateSubmitEnabled();
    (broj ? document.getElementById("fEmail") : fBroj).focus();
    return;
  }
  fetch(API_BASE + "/api/streets?city=" + encodeURIComponent(fCity.value) + "&q=" + encodeURIComponent(ulica))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var list = d.streets || [], want = fold(ulica);
      var same = list.filter(function (st) { return fold(st.name) === want; });
      var pick = same.length === 1 ? same[0] : (list.length === 1 ? list[0] : null);
      if (pick) {
        fUlica.value = titleCase(pick.name);
        fUlicaId.value = pick.id;
        fBroj.disabled = false;
        fBroj.placeholder = "npr. 12, 12a ili bb";
        fBroj.value = broj;
        updateSubmitEnabled();
        (broj ? document.getElementById("fEmail") : fBroj).focus();
        return;
      }
      // Not one clear match: show what was typed with its suggestions, and keep the number for
      // when a street is picked from them.
      pendingBroj = broj;
      fUlica.value = ulica;
      fUlica.focus();
      fUlica.dispatchEvent(new Event("input"));
    })
    .catch(function () { fUlica.value = ulica; fUlica.focus(); });
}

function prefill(names, address) {
  if (loggedIn || REGISTRATION_DISABLED) return;
  names = (names || []).map(function (n) { return String(n || "").trim(); })
    .filter(function (n) { return n; }).slice(0, 4);
  if (!names.length) return;
  address = { ulica: String(address.ulica || "").trim().slice(0, 80), broj: address.broj || "" };
  (loadCities() || Promise.resolve()).then(function () {
    var i, j, f;
    for (i = 0; i < names.length; i++) {
      f = fold(names[i]);
      j = CITY_FOLDED.indexOf(f);
      if (j !== -1) { fCity.value = titleCase(CITY_LIST[j]); pendingAddress = address; applyCityMode(true); return; }
    }
    for (i = 0; i < names.length; i++) {
      j = CITY_NOSTREET_FOLDED.indexOf(fold(names[i]));
      if (j !== -1) { fCity.value = titleCase(CITY_NOSTREET[j]); pendingAddress = address; applyCityMode(false); return; }
    }
    fCity.value = names[names.length - 1];
    fCity.focus();
    fCity.dispatchEvent(new Event("input"));   // opens the suggestions; resets nothing else
    // Not resolved on blur either: "Beograd" alone would fall back to the free-text mode meant
    // for places with no street list. The visitor picks the opština from the suggestions.
    lastResolvedCity = fCity.value.trim().toUpperCase();
    pendingAddress = address;                    // applied when a place is picked
  });
}

// Open the form. opts.grad: the place names a city page goes by, most specific first;
// opts.ulica / opts.broj: an address to fill in; opts.source: {src, from} when the page knows
// better than this page's URL where the visitor came from (a city page: its referrer and name).
function open(opts) {
  opts = opts || {};
  sourceOverride = opts.source || null;
  openModal();
  var placed = (opts.grad || []).some(function (g) { return String(g || "").trim(); });
  // The list of every city we cover is for someone choosing one. With the place already set by
  // the page it only pushes the fields down.
  document.getElementById("coverageList").hidden = placed;
  if (placed) prefill(opts.grad, { ulica: opts.ulica, broj: opts.broj });
}

// How many open the form and how many send it, per page - a count per day and nothing else: no
// cookie, nothing on the device, no IP kept (scripts/local_web_server.py /api/ev). Without it
// a visit and a signup were all we saw, and not where people stop in between.
function count(ev) {
  try {
    var from = (sourceOverride && sourceOverride.from) || new URLSearchParams(location.search).get("from")
      || location.pathname.replace(/^\/+|\/+$/g, "").replace(/^grad\//, "").replace(/\//g, "-") || "home";
    navigator.sendBeacon(API_BASE + "/api/ev", JSON.stringify({ e: ev, p: from }));
  } catch (err) {}
}

// /?prijava=1&grad=...&ulica=...&broj=... opens the form filled in.
function openFromUrl() {
  var q = new URLSearchParams(location.search);
  if (q.get("prijava") !== "1") return;
  open({ grad: (q.get("grad") || "").split("|"), ulica: q.get("ulica") || "", broj: q.get("broj") || "" });
}

// Where this visitor found us, for the operator's count of which channels bring people: the
// referring site's name (the browser's referrer, e.g. "www.google.com"), or a tag our own links
// carry (?src=facebook, ?utm_source=...), plus the city page they came through (?from=kraljevo,
// added by that page's link). Sent once with the signup and never stored on the device, same as
// ref. The server keeps only the site's name, for a new account (core/auth.clean_source).
var sourceOverride = null;   // set by open({source}) from a city page
function signupSource() {
  if (sourceOverride) return sourceOverride;
  var q = new URLSearchParams(location.search), host = "";
  try { host = document.referrer ? new URL(document.referrer).hostname : ""; } catch (err) {}
  return { src: q.get("src") || q.get("utm_source") || host, from: q.get("from") || "" };
}

document.getElementById("regForm").addEventListener("submit", function (e) {
  e.preventDefault();
  var errBox = document.getElementById("modalError");
  errBox.hidden = true;

  var submitBtn = document.getElementById("modalSubmit");
  submitBtn.disabled = true;
  submitBtn.textContent = "Šaljem…";

  var payload = {
    city: document.getElementById("fCity").value.trim(),
    ulica: fUlica.value.trim(),
    broj: fBroj.value.trim(),
    channel: selectedChannel,
    email: document.getElementById("fEmail").value.trim(),
    ack_uncovered: document.getElementById("fAckUncovered").checked,
    // Partner referral link (najavaradova.rs/?ref=<slug>). Read straight from this page's URL,
    // never stored on the device - the consent notice promises no tracking, and a remembered
    // ref would be exactly that. The server ignores unknown refs (core/partners.py).
    ref: new URLSearchParams(location.search).get("ref") || undefined,
    source: signupSource()
  };

  // /auth/start always responds the same way (no telegram_link, no distinction by channel) -
  // login is mandatory now (see PLAN.md's subscriber design), so every signup's next step is
  // "check your email", regardless of which channel ends up delivering the outage pings. The
  // old Telegram-specific "open t.me in a new tab immediately" flow is gone with it - that link
  // only gets handed out once the email is confirmed (see the dashboard).
  fetch(API_BASE + "/auth/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
    .then(function (res) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Potvrdi prijavu";
      if (!res.ok) {
        errBox.textContent = (res.data && res.data.error) || "Greška, pokušaj ponovo.";
        errBox.hidden = false;
        // Uncovered city: reveal the acknowledgement checkbox instead of just failing - the
        // visitor can tick it and resubmit rather than being silently accepted (the old bug)
        // or hitting a dead end.
        if (res.data && res.data.code === "UNCOVERED") {
          document.getElementById("ackUncoveredField").hidden = false;
        }
        if (res.data && res.data.code === "PICK_PLACE") {
          document.getElementById("ackUncoveredField").hidden = true;
          document.getElementById("fAckUncovered").checked = false;
        }
        return;
      }
      count("submit");
      document.getElementById("regForm").hidden = true;
      // The address as the server saved it - shown in full, so "Beograd, Grocka" turning into
      // Kaluđerica (check_address on the server) is seen before the link is clicked.
      var saved = (res.data && res.data.saved) || {};
      var label = saved.label || (payload.ulica + " " + payload.broj + ", " + titleCase(payload.city));
      var successText = document.getElementById("modalSuccessText");
      successText.textContent = "Poslali smo ti mejl na " + payload.email +
        " - klikni link tamo da potvrdiš adresu i uđeš na svoju stranicu.";
      var addr = document.createElement("div");
      addr.className = "modal-saved-addr";
      addr.appendChild(document.createTextNode("Adresa: "));
      addr.appendChild(document.createElement("b")).textContent = label;
      successText.appendChild(addr);
      if (saved.changed) {
        var note = document.createElement("div");
        note.className = "modal-saved-note";
        note.textContent = "Upisano je „" + payload.ulica + " " + payload.broj + ", " + payload.city +
          "“ - ovako tu adresu vodi registar adresa. Nije tačno? Promeni je na svojoj stranici posle potvrde.";
        successText.appendChild(note);
      }
      document.getElementById("openGmail").hidden = !/@(gmail|googlemail)\.com$/i.test(payload.email);
      sentPayload = payload;
      holdResend();
      document.getElementById("modalSuccess").hidden = false;
    })
    .catch(function () {
      submitBtn.disabled = false;
      submitBtn.textContent = "Potvrdi prijavu";
      errBox.textContent = "Server nije dostupan na /auth/start. Da li je scripts/local_web_server.py pokrenut?";
      errBox.hidden = false;
    });
});

window.NRSignup = { open: open, openFromUrl: openFromUrl, me: me };
})();
