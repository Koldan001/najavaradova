"""Referral partners, managed from the SSH CLI - no web page, same reasoning as /stats (operator
data stays off the public surface).

Usage:
    python -m scripts.partners add zgradahub "ZgradaHub d.o.o." [--pct 25]
    python -m scripts.partners list
    python -m scripts.partners report [zgradahub]

The link to hand a partner is https://najavaradova.rs/?ref=<slug>. A manager onboarded by hand
is credited with: python -m scripts.bulk_register --email ... addresses.csv --ref <slug>

Billing isn't live yet, so the report's amounts are what each account WOULD pay at list price
(core/partners.py TIERS) and the partner's share of that - the numbers a partner conversation
needs, not an invoice.
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import partners
from core.database import get_connection, init_db


def add(slug, name, pct):
    clean = partners.normalize_slug(slug)
    if not clean or clean != slug:
        print(f"❌ Neispravan slug '{slug}' - mala slova, cifre i crtica, 2-32 znaka (npr. zgradahub).")
        return 1
    if not 0 < pct <= 100:
        print("❌ --pct mora biti između 1 i 100.")
        return 1
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM partners WHERE slug = ?", (slug,)).fetchone():
            print(f"❌ Partner '{slug}' već postoji.")
            return 1
        conn.execute(
            "INSERT INTO partners (slug, name, commission_pct) VALUES (?, ?, ?)", (slug, name, pct)
        )
        conn.commit()
    finally:
        conn.close()
    print(f"✅ Dodat partner '{slug}' ({name}), {pct}%.")
    print(f"   Link: https://najavaradova.rs/?ref={slug}")
    return 0


def list_partners():
    conn = get_connection()
    rows = conn.execute('''
        SELECT p.slug, p.name, p.commission_pct, p.created_at,
               (SELECT COUNT(*) FROM subscribers s WHERE s.referred_by = p.slug) AS referred
        FROM partners p ORDER BY p.created_at
    ''').fetchall()
    conn.close()
    if not rows:
        print("Nema partnera. Dodaj: python -m scripts.partners add <slug> \"Naziv\"")
        return 0
    for r in rows:
        print(f"{r['slug']:<20} {r['commission_pct']:>3}%  {r['referred']:>4} naloga  "
              f"{r['name']}  (od {r['created_at'][:10]})")
    return 0


def report_rows(conn, slug=None):
    """Per referred account: (partner slug, pct, email, created_at, active addresses, tier,
    monthly list price EUR or None, partner share EUR or None). Split out from report() for the
    tests."""
    query = '''
        SELECT p.slug, p.commission_pct, s.email, s.created_at,
               (SELECT COUNT(*) FROM targets t
                WHERE t.subscriber_id = s.id AND t.is_active = 1) AS addresses
        FROM partners p JOIN subscribers s ON s.referred_by = p.slug
    '''
    args = ()
    if slug:
        query += " WHERE p.slug = ?"
        args = (slug,)
    query += " ORDER BY p.slug, s.created_at"
    out = []
    for r in conn.execute(query, args).fetchall():
        tier, price = partners.tier_for(r["addresses"])
        share = round(price * r["commission_pct"] / 100, 2) if price is not None else None
        out.append((r["slug"], r["commission_pct"], r["email"], r["created_at"],
                    r["addresses"], tier, price, share))
    return out


def report(slug=None):
    conn = get_connection()
    rows = report_rows(conn, slug)
    conn.close()
    if not rows:
        print("Nema pripisanih naloga" + (f" za '{slug}'." if slug else "."))
        return 0
    print("Naplata još nije aktivna - iznosi su po mesečnom cenovniku (EUR).\n")
    current = None
    total_price = total_share = 0
    for p_slug, pct, email, created, addresses, tier, price, share in rows:
        if p_slug != current:
            if current is not None:
                print(f"   UKUPNO: €{total_price:g}, partneru €{total_share:g}\n")
            current, total_price, total_share = p_slug, 0, 0
            print(f"== {p_slug} ({pct}%)")
        price_txt = f"€{price}" if price is not None else "po dogovoru"
        share_txt = f"€{share:g}" if share is not None else "-"
        print(f"   {email:<32} {created[:10]}  {addresses:>3} adr.  {tier:<6} {price_txt:>12}  "
              f"partneru {share_txt}")
        total_price += price or 0
        total_share += share or 0
    print(f"   UKUPNO: €{total_price:g}, partneru €{total_share:g}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="Register a partner.")
    p_add.add_argument("slug")
    p_add.add_argument("name")
    p_add.add_argument("--pct", type=int, default=25, help="Commission percent (default 25).")
    sub.add_parser("list", help="All partners and how many accounts each brought.")
    p_rep = sub.add_parser("report", help="Referred accounts, tiers and would-be commission.")
    p_rep.add_argument("slug", nargs="?")
    args = parser.parse_args()

    init_db()  # creates the partners table on a DB that predates it
    if args.cmd == "add":
        return add(args.slug, args.name, args.pct)
    if args.cmd == "list":
        return list_partners()
    return report(args.slug)


if __name__ == "__main__":
    sys.exit(main())
