"""Write the guide pages (web/vodic/<slug>/index.html): plain-language answers to the questions
people search before an outage page helps them ("kako da proverim isključenje struje", "šta znači
Dan 0", "nestalo je vode, šta da radim"). Search engines and their AI answers quote pages that
answer such a question directly, so each guide opens with the short answer.

Each guide is data here, rendered in the look of web/faq.html, with its FAQPage data built from
the same sections, so what the page shows and what it tells search engines can't drift apart.
Run it after editing a guide; tests/test_guides.py fails while a published page is out of date.

"Prijavi adresu" opens the signup form right on the guide (web/signup.js, as on the city pages);
without the script it goes to the same form on the home page.

    python scripts/build_guides.py
"""
from __future__ import annotations

import html
import json
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_city_pages import CITY_SOURCES, SITE, WEB_DIR, carry_script  # noqa: E402

GUIDE_DIR = os.path.join(WEB_DIR, "vodic")


def _water_sources() -> str:
    items = [f'<li><strong>{html.escape(city)}</strong>: <a href="{html.escape(url)}" rel="nofollow noopener">'
             f'{html.escape(name)}</a></li>'
             for city, sources in CITY_SOURCES.items()
             for kind, name, url in sources if kind == "VODA"]
    return "<ul>" + "".join(items) + "</ul>"


# (slug, title, description, short answer, [(question, answer html)])
GUIDES = [
    ("iskljucenja-struje",
     "Kako da proverim da li je najavljeno isključenje struje",
     "Gde EPS objavljuje planirana isključenja struje, šta znače Dan 0, Dan 1, Dan 2 i Dan 3, kako "
     "da nađeš svoju ulicu i kako da razlikuješ planirano isključenje od kvara.",
     "Planirana isključenja struje objavljuje Elektrodistribucija Srbije (EPS Distribucija) na sajtu "
     "elektrodistribucija.rs, za danas i naredna tri dana, po opštinama, sa vremenom i spiskom ulica. "
     "Isti spisak za svoj grad vidiš i na stranici grada na NajavaRadova.rs, a možeš da se prijaviš "
     "da ti poruka stigne kad se najava odnosi na tvoju adresu.",
     [("Gde EPS objavljuje planirana isključenja struje?",
       "<p>Na sajtu <a href=\"https://elektrodistribucija.rs/\" rel=\"nofollow noopener\">elektrodistribucija.rs</a>, "
       "u delu „Planirana isključenja”. Srbija je podeljena na pet područja: Beograd, Novi Sad, "
       "Kragujevac, Kraljevo i Niš. Svako područje pokriva više opština, pa prvo izaberi ono kojem "
       "pripada tvoje mesto.</p>"),
      ("Šta znače Dan 0, Dan 1, Dan 2 i Dan 3?",
       "<p>To su tabele za četiri dana: <strong>Dan 0 je danas</strong>, Dan 1 sutra, Dan 2 prekosutra, "
       "a Dan 3 za tri dana. Tačan datum piše iznad svake tabele. U tabeli su opština, vreme "
       "isključenja i ulice, često sa kućnim brojevima.</p>"),
      ("Zašto je teško naći svoju ulicu u spisku?",
       "<p>Spiskovi su pisani ćirilicom, ulice su često skraćene („Б.Бухе” umesto Boška Buhe), a "
       "kućni brojevi su dati kao opsezi, parni ili neparni („парни бројеви 56–58”). Jedna "
       "najava ponekad navodi i samo deo naselja. NajavaRadova.rs svaku ulicu iz objave povezuje "
       "sa zvaničnim adresnim registrom (RGZ) i proverava kućni broj, pa poruku dobijaju samo "
       "adrese na koje se najava zaista odnosi.</p>"),
      ("Kako da razlikujem planirano isključenje od kvara?",
       "<p>Planirano isključenje je unapred u spisku, sa vremenom početka i kraja. Ako struje nema, "
       "a tvoja ulica nije u spisku za danas, verovatno je u pitanju kvar. Proveri da li struje "
       "imaju i komšije: ako nemaju, kvar prijavi kontakt centru Elektrodistribucije Srbije (broj je "
       "na sajtu elektrodistribucija.rs). Ako struje nema samo kod tebe, prvo proveri osigurače u "
       "svom stanu.</p>"),
      ("Koliko unapred se najavljuje isključenje struje?",
       "<p>Obično dan do tri dana unapred, koliko obuhvataju tabele Dan 1 do Dan 3. Kvarovi se ne "
       "najavljuju, jer nastanu iznenada.</p>"),
      ("Kako da ne propustim najavu za svoju adresu?",
       "<p><a href=\"/?prijava=1\" data-signup>Prijavi adresu</a> na NajavaRadova.rs. Pratimo EPS za celu Srbiju i šaljemo "
       "poruku na Telegram ili e-mail čim se najava odnosi na tvoju ulicu i broj. Besplatno je.</p>"),
      ]),
    ("iskljucenja-vode",
     "Kako da proverim da li je najavljeno isključenje vode",
     "Gde vodovodi objavljuju planirane radove i kvarove na mreži, koliko unapred, i kako da dobiješ "
     "poruku kad voda bude isključena u tvojoj ulici.",
     "Isključenja vode objavljuje vodovod tvog grada (JKP Vodovod i kanalizacija) na svom sajtu, "
     "obično u delu „Kvarovi na mreži”, „Najava radova” ili „Servisne informacije”. Planirani "
     "radovi se najavljuju dan ili više unapred, a kvarovi istog dana. NajavaRadova.rs prati te "
     "objave za veće gradove i šalje poruku kad se odnose na tvoju adresu.",
     [("Gde vodovod objavljuje isključenja vode?",
       "<p>Svaki grad ima svoje komunalno preduzeće za vodu i svako objavljuje na svom sajtu. Ovo su "
       "objave koje pratimo:</p>" + _water_sources()
       + "<p>Spisak gradova se širi. Za svaki grad su izvori navedeni i na njegovoj stranici, u "
       "delu „Koje izvore pratimo”.</p>"),
      ("Koliko unapred se najavljuje isključenje vode?",
       "<p>Planirani radovi, kao zamena cevi ili priključenje novih objekata, obično se najavljuju "
       "dan ili više unapred, sa ulicama i vremenom. Kvar na mreži (pukla cev) objavljuje se istog "
       "dana, kad ekipa izađe na teren, često bez tačnog vremena završetka.</p>"),
      ("Zašto u najavi piše samo naselje, bez ulica?",
       "<p>Kad je kvar na većoj cevi, bez vode ostaje ceo deo grada, pa vodovod navede naselje ili "
       "granične ulice. Neki vodovodi objavljuju i jednu dnevnu objavu sa više radova odjednom. "
       "Kad se iz objave ne vidi pouzdano da li si u zoni, NajavaRadova.rs ne šalje poruku nasumično, "
       "već pokaže najavu na mapi.</p>"),
      ("Nema vode, a ništa nije najavljeno. Šta da radim?",
       "<p>Proveri da li vode imaju i komšije. Ako nemaju, verovatno je kvar: pozovi dežurnu službu "
       "vodovoda (broj je na sajtu vodovoda). Ako vode nema samo kod tebe, problem je verovatno na "
       "instalaciji u zgradi ili kući.</p>"),
      ("Kako da dobijem poruku kad nestane vode u mojoj ulici?",
       "<p><a href=\"/?prijava=1\" data-signup>Prijavi adresu</a> na NajavaRadova.rs i izaberi Telegram ili e-mail. Kad "
       "vodovod objavi radove ili kvar u tvojoj ulici, poruka stiže sama. Besplatno je.</p>"),
      ]),
    ("nestalo-struje-ili-vode",
     "Nestalo je struje ili vode: šta da radim",
     "Šta da proveriš kad nestane struje, vode ili grejanja: da li je isključenje najavljeno, da li "
     "je kvar, kome se prijavljuje i koliko traje.",
     "Prvo proveri da li je isključenje najavljeno: na stranici svog grada na NajavaRadova.rs ili na "
     "sajtu EPS-a, vodovoda ili toplane. Ako je najavljeno, tamo piše i do kad traje. Ako nije, a "
     "nemaju ni komšije, verovatno je kvar i prijavljuje se preduzeću: struja EPS Distribuciji, voda "
     "vodovodu, grejanje toplani.",
     [("Kako da proverim da li je isključenje najavljeno?",
       "<p>Otvori <a href=\"/grad/\">stranicu svog grada</a>: na njoj su najave za danas, sutra i "
       "prekosutra, sa ulicama, vremenom i linkom na zvaničnu objavu. Isto vidiš i na "
       "<a href=\"/\">mapi</a>. Detaljno: <a href=\"/vodic/iskljucenja-struje/\">kako se proverava "
       "struja</a> i <a href=\"/vodic/iskljucenja-vode/\">kako se proverava voda</a>.</p>"),
      ("Kako da znam da li je kvar samo kod mene?",
       "<p>Pitaj komšije ili pogledaj da li u zgradi radi osvetljenje i lift. Ako struje nema samo "
       "u tvom stanu, proveri osigurače. Ako vode nema samo kod tebe, proveri ventil i bojler. Ako "
       "nema ni kod drugih, a ništa nije najavljeno, verovatno je kvar na mreži.</p>"),
      ("Kome da prijavim kvar?",
       "<ul><li><strong>Struja</strong>: kontakt centru Elektrodistribucije Srbije (broj je na sajtu "
       "elektrodistribucija.rs).</li>"
       "<li><strong>Voda</strong>: dežurnoj službi vodovoda tvog grada (broj je na sajtu vodovoda).</li>"
       "<li><strong>Grejanje</strong>: toplani tvog grada.</li></ul>"
       "<p>NajavaRadova.rs nije komunalno preduzeće i ne prima prijave kvarova.</p>"),
      ("Koliko traje isključenje?",
       "<p>Kod planiranog isključenja vreme je u najavi, najčešće nekoliko sati preko dana. Radovi "
       "se ponekad produže, pa računaj na malo duže. Kod kvara vreme se unapred ne zna: ekipa ga "
       "procenjuje na terenu, a preduzeća ponekad objave novo stanje.</p>"),
      ("Kako da ubuduće znam unapred?",
       "<p><a href=\"/?prijava=1\" data-signup>Prijavi adresu</a> na NajavaRadova.rs. Kad EPS, vodovod ili toplana objave "
       "isključenje za tvoju ulicu i broj, poruka ti stiže na Telegram ili e-mail. Besplatno je.</p>"),
      ]),
]


def _faq_parts() -> tuple[str, str, str, str]:
    """(head up to <title>, the <style> block, the page header, the footer with its script), taken
    from web/faq.html so the guides keep its look and theme toggle."""
    page = open(os.path.join(WEB_DIR, "faq.html"), encoding="utf-8").read()
    head = page[:page.index("<title>")]
    style = re.search(r"<style>.*?</style>", page, re.S).group(0)
    header = re.search(r"<header>.*?</header>", page, re.S).group(0)
    footer = page[page.index("<footer>"):]
    return head, style, header, footer


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def guide_html(slug: str, title: str, description: str, answer: str, sections) -> str:
    head, style, header, footer = _faq_parts()
    url = f"{SITE}/vodic/{slug}/"
    data = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": _text(a)}}
        for q, a in sections]}
    others = " · ".join(f'<a href="/vodic/{s}/">{html.escape(t)}</a>'
                        for s, t, *_ in GUIDES if s != slug)
    body = "\n".join(f"  <h2>{html.escape(q)}</h2>\n  {a}" for q, a in sections)
    return (
        f"{head}<title>{html.escape(title)} — NajavaRadova.rs</title>\n"
        f'<meta name="description" content="{html.escape(description)}">\n'
        f'<link rel="canonical" href="{url}">\n'
        f"{style}\n"
        f'<link rel="stylesheet" href="/signup.css">\n'
        f"<style>.answer {{ font-size: 16px; color: var(--text); border-left: 3px solid var(--voda); "
        f"padding: 4px 0 4px 14px; margin: 8px 0 28px; }}</style>\n"
        f'<script type="application/ld+json">\n{json.dumps(data, ensure_ascii=False)}\n</script>\n'
        f"</head>\n<body>\n\n{header}\n\n<main>\n"
        f"  <h1>{html.escape(title)}</h1>\n"
        f'  <p class="answer">{html.escape(answer)}</p>\n'
        f"{body}\n"
        f'  <h2>Još</h2>\n  <p>{others} · <a href="/faq">Česta pitanja</a> · '
        f'<a href="/grad/">Svi gradovi</a></p>\n'
        f"</main>\n\n"
        + footer.replace("</body>", '<script src="/signup.js" defer></script>\n'
                         f"{carry_script('vodic-' + slug)}\n</body>")
    )


def guide_paths() -> dict[str, str]:
    return {os.path.join(GUIDE_DIR, slug, "index.html"): guide_html(slug, t, d, a, s)
            for slug, t, d, a, s in GUIDES}


def main() -> int:
    for path, page in guide_paths().items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(page)
        print(f"wrote {os.path.relpath(path, WEB_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
