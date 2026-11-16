"""Static list of Serbia's local self-government units - opštine plus the city municipalities
of Beograd, Niš, Novi Sad, Kragujevac - in Latin, with diacritics.

Used for one thing only: filling in the registration form's "Grad / mesto" autocomplete for
places `data/rgz.db` has no street data for yet (Inđija, Stara Pazova, Ruma, Šid, most of
Srem, and every smaller opština outside the ~38 loaded so far). Picking one of these puts the
signup in "uncovered" mode - free-text street, an explicit acknowledgement checkbox - so the
address is on file and starts getting notified the moment a scraper reaches it, instead of the
city field being a dead end.

This is NOT authoritative geography. A cosmetic misspelling here only changes an autocomplete
label; `coverage_tier()` / `_resolve_naselje()` in local_web_server.py still decide real
coverage from rgz.db, and the field is free-text and fold-matched on submit either way.

Kosovo opštine are intentionally excluded - the service scrapes no utilities there.

Kept in Python (not data/) on purpose: docker-compose bind-mounts ./data over /app/data on the
VM, which would hide a file baked into the image at data/. A module ships in the image and is
never shadowed.
"""

# Alphabetical (Serbian Latin collation is not enforced here - the frontend sorts/filters).
OPSTINE = [
    "Ada", "Aleksandrovac", "Aleksinac", "Alibunar", "Apatin", "Aranđelovac", "Arilje",
    "Babušnica", "Bajina Bašta", "Barajevo", "Batočina", "Bač", "Bačka Palanka",
    "Bačka Topola", "Bački Petrovac", "Bela Crkva", "Bela Palanka", "Beograd", "Beočin",
    "Bečej", "Blace", "Bogatić", "Bojnik", "Boljevac", "Bor", "Bosilegrad", "Brus",
    "Bujanovac", "Valjevo", "Varvarin", "Velika Plana", "Veliko Gradište", "Vladimirci",
    "Vladičin Han", "Vlasotince", "Voždovac", "Vranje", "Vrbas", "Vrnjačka Banja", "Vršac",
    "Vračar", "Gadžin Han", "Golubac", "Gornji Milanovac", "Grocka", "Despotovac",
    "Dimitrovgrad", "Doljevac", "Žabalj", "Žabari", "Žagubica", "Žitište", "Žitorađa",
    "Zaječar", "Zemun", "Zrenjanin", "Zvezdara", "Ivanjica", "Inđija", "Irig", "Jagodina",
    "Kanjiža", "Kikinda", "Kladovo", "Knić", "Knjaževac", "Kovačica", "Kovin", "Koceljeva",
    "Kosjerić", "Kragujevac", "Kraljevo", "Krupanj", "Kruševac", "Kula", "Kučevo",
    "Kuršumlija", "Lazarevac", "Lapovo", "Lebane", "Leskovac", "Loznica", "Lučani", "Ljig",
    "Ljubovija", "Majdanpek", "Mali Zvornik", "Mali Iđoš", "Malo Crniće", "Medveđa",
    "Merošina", "Mionica", "Mladenovac", "Negotin", "Niš", "Nova Varoš", "Nova Crnja",
    "Novi Beograd", "Novi Bečej", "Novi Kneževac", "Novi Pazar", "Novi Sad", "Odžaci",
    "Opovo", "Osečina", "Obrenovac", "Palilula", "Pančevo", "Paraćin", "Petrovac na Mlavi",
    "Pirot", "Plandište", "Požarevac", "Požega", "Preševo", "Priboj", "Prijepolje",
    "Prokuplje", "Pećinci", "Rača", "Raška", "Ražanj", "Rakovica", "Rekovac", "Ruma", "Savski venac",
    "Svilajnac", "Svrljig", "Senta", "Sečanj", "Sjenica", "Smederevo",
    "Smederevska Palanka", "Sokobanja", "Sombor", "Sopot", "Srbobran", "Sremska Mitrovica",
    "Sremski Karlovci", "Stara Pazova", "Stari grad", "Subotica", "Surdulica", "Surčin",
    "Temerin", "Titel", "Topola", "Trgovište", "Trstenik", "Tutin", "Ub", "Užice",
    "Crna Trava", "Ćićevac", "Ćuprija", "Čajetina", "Čačak", "Čoka", "Čukarica", "Šabac",
    "Šid",
]
