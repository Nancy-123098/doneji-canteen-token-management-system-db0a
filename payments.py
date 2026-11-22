"""UPI collection: the link, the QR, the reference check, and the three
money routes, installed once into any app.

Everything money-related that is the SAME in every app lives here: the
payments table's rows, the claim / verify / reject routes, and the context a
page needs to show the panel. What each app keeps for itself is WHICH row is
being paid for, and it says so in one small function: bill(target_id, user).

The one rule that matters: the UPI ID is read from the environment, never
written down here. An invented UPI ID is not a harmless placeholder: it may
be somebody's real account, and a customer's real money would arrive there.
"""
import os
from urllib.parse import quote

from flask import flash, redirect, request

import db


def enabled(vpa=None):
    """True only when the owner has set their own UPI ID.

    The whole panel is off until then, so an app that ships without this
    variable is a working app that takes cash, not a broken one. A market of
    many sellers passes each seller's own UPI ID (vpa): that seller is paid
    directly, and the app's own ID is only the fallback.
    """
    return bool((vpa or os.environ.get("UPI_VPA", "")).strip())


def payee(name=None):
    """The name shown inside the customer's UPI app before they confirm."""
    return (name or "").strip() or os.environ.get("UPI_PAYEE_NAME", "").strip() or "This shop"


def pay_link(amount, note, vpa=None, name=None):
    """The upi:// deep link. On a phone, tapping it opens the UPI app.

    quote() escapes anything the note contains, so a shop name with a space
    or an ampersand cannot break the link apart into extra parameters.
    """
    vpa = (vpa or os.environ.get("UPI_VPA", "")).strip()
    return (
        "upi://pay?pa=" + quote(vpa)
        + "&pn=" + quote(payee(name))
        + "&am=" + quote("{:.2f}".format(float(amount)))
        + "&tn=" + quote(str(note))
        + "&cu=INR"
    )


def qr_svg(amount, note, vpa=None, name=None):
    """The same link as a QR, drawn as inline SVG, or None if it cannot be.

    A laptop cannot open a upi:// link (there is no UPI app on it) so on the
    device this course is taught on, the QR IS the payment method: the page is
    on the screen and the customer scans it with their own phone.

    Nothing the customer typed ever reaches this string; only the amount and
    an order number do. That is what makes it safe to render with |safe in the
    page, and it is the reason the note is a number and not a message.
    """
    try:
        import segno
    except ImportError:
        # No QR library on this host: fall back to the link alone rather than
        # taking the whole page down over a picture.
        return None
    qr = segno.make(pay_link(amount, note, vpa, name), micro=False)
    return qr.svg_inline(scale=4, border=2)


def clean_ref(raw):
    """The 12-digit UPI reference, or None if it is not one.

    UPI calls it an RRN; your bank app may label it UTR or 'UPI Ref No'. It is
    the same number, and it is always exactly 12 digits, which is all this
    can check. It proves the customer read something off a screen; it does not
    prove the money arrived. Only the owner's own bank app proves that, which
    is exactly why there is a separate verify step.
    """
    ref = "".join(str(raw).split())
    # isascii too: Hindi digits pass isdigit, and the database's check refuses them.
    if len(ref) == 12 and ref.isascii() and ref.isdigit():
        return ref
    return None


# ── The payments table, one row per thing being paid for ──────────────────
#
# kind names the parent table in words ('order', 'booking', 'customer') and
# target_id is that row's id. There is no FOREIGN KEY because one table serves
# every app; the UNIQUE (target_kind, target_id) is what keeps it to one row.

def get(kind, target_id):
    """The one payment row for a thing, or None if nobody has paid yet."""
    return db.query_one(
        """select id, target_kind, target_id, amount, status, claimed_ref,
                  claimed_at, verified_at, note
             from payments where target_kind = %s and target_id = %s""",
        (kind, target_id),
    )


def by_kind(kind, target_ids=None):
    """{target_id: payment row} for a whole list page, in one query.

    Pass the ids the page is about (a customer's own orders, the queue's
    orders) and only their rows come back: a list page must never load
    every payment in the system to decorate ten rows.
    """
    paid = {}
    if target_ids is not None:
        ids = [int(i) for i in target_ids]
        if not ids:
            return paid
        rows = db.query(
            """select id, target_id, amount, status, claimed_ref
                 from payments where target_kind = %s and target_id = any(%s)""",
            (kind, ids),
        )
    else:
        rows = db.query(
            """select id, target_id, amount, status, claimed_ref
                 from payments where target_kind = %s""",
            (kind,),
        )
    for row in rows:
        paid[row["target_id"]] = row
    return paid


def start(kind, target_id, amount):
    """Creates the row once. ON CONFLICT DO NOTHING plus the UNIQUE constraint
    is what makes this safe to run twice: opening a pay page twice cannot
    make two payments. Nothing to pay (a zero bill) makes no row: the
    table's own rule says amount > 0, and the honest answer is no payment."""
    if amount is None or amount <= 0:
        return
    db.execute(
        """insert into payments (target_kind, target_id, amount)
           values (%s, %s, %s)
           on conflict (target_kind, target_id) do nothing""",
        (kind, target_id, amount),
    )


def reference_used(ref, kind, target_id):
    """True when this reference already stands against a different payment."""
    return bool(db.query_one(
        """select id from payments
            where claimed_ref = %s and status in ('claimed', 'verified')
              and not (target_kind = %s and target_id = %s)""",
        (ref, kind, target_id),
    ))


def claim(kind, target_id, amount, raw_ref, user_id=None, new_round=False):
    """The customer says they have paid, and types the 12-digit reference.

    Note what this does NOT do: check that the money arrived. Nothing here can
    without a payment gateway there is no API to ask. All this records is a
    CLAIM, which is why the row goes to 'claimed' and not to 'verified'.
    Returns the cleaned reference, or None when what was typed is not one, or
    when nothing changed (the payment is already verified, or the reference
    already stands against another payment).

    new_round=True lets an already-verified row be claimed again: a khata
    collects many rounds against one running balance. Everything else keeps
    a verified payment exactly as it is.
    """
    ref = clean_ref(raw_ref)
    if not ref:
        return None
    start(kind, target_id, amount)
    # A verified payment is left alone unless the app asked for a new round.
    keep = "" if new_round else " and status <> 'verified'"
    # One bank payment pays one thing, checked in the same statement that writes the claim.
    changed = db.execute(
        """update payments
              set amount = %s, status = 'claimed', claimed_ref = %s,
                  claimed_by = %s, claimed_at = now(),
                  verified_by = null, verified_at = null, note = ''
            where target_kind = %s and target_id = %s""" + keep + """
              and not exists (select 1 from payments other
                               where other.claimed_ref = %s and other.status in ('claimed', 'verified')
                                 and not (other.target_kind = %s and other.target_id = %s))""",
        (amount, ref, user_id, kind, target_id, ref, kind, target_id),
    )
    return ref if changed > 0 else None


def verify(payment_id, user_id, ref=None):
    """The owner looked in their own UPI app and the money was there.

    status = 'claimed' in the WHERE makes the button idempotent: a second
    press updates zero rows, and the function says so by returning False.
    ref is the reference the owner was SHOWN: if the payer sent a different
    one since the page opened, nothing is verified, so the owner never
    confirms a number they did not look up.
    """
    same = " and claimed_ref = %s" if ref else ""
    changed = db.execute(
        """update payments
              set status = 'verified', verified_by = %s, verified_at = now()
            where id = %s and status = 'claimed'""" + same,
        (user_id, payment_id, ref) if ref else (user_id, payment_id),
    )
    return changed > 0


def reject(payment_id, reason=""):
    """The money was not there. The customer may send the reference again.

    The reason, when the owner gives one, is kept on the row for the payer's
    panel. Returns False when nothing changed: the payment was already
    checked (verified, or rejected before), so the owner is told the truth.
    """
    changed = db.execute(
        "update payments set status = 'rejected', note = %s where id = %s and status = 'claimed'",
        (str(reason or "").strip()[:200], payment_id),
    )
    return changed > 0


def release(kind, target_id, reason=""):
    """A claim nobody judged yet no longer counts: the row was sent back, or
    moved on without being paid (rejected, cancelled). The reference is
    freed, so the payer may type it again if it was a typing slip, and the
    reason is kept for their panel. Money already VERIFIED is never undone
    here: it arrived, and taking it back is the owner's job outside the app."""
    changed = db.execute(
        """update payments set status = 'rejected', note = %s
            where target_kind = %s and target_id = %s and status = 'claimed'""",
        (str(reason or "").strip()[:200], kind, target_id),
    )
    return changed > 0


def reopen(kind, target_id, reason="sent back"):
    """The row was sent back to be paid again: an unchecked claim no longer
    counts (release). Money already confirmed stays confirmed: the payer is
    never asked to pay twice."""
    return release(kind, target_id, reason)


def cash(kind, target_id, amount, user_id, note="cash at counter"):
    """Cash handed over in person. The owner is the witness, so the row goes
    straight to 'verified': no claim step to wait for.

    Returns True when the cash was recorded. Nothing changes, and it returns
    False, when the money is already verified (a second press, a stale tab)
    or when a UPI claim is waiting to be checked: that reference is the only
    record of the payer's UPI money, so the owner checks it first (Not
    received, then cash)."""
    start(kind, target_id, amount)
    changed = db.execute(
        """update payments
              set status = 'verified', verified_by = %s, verified_at = now(), note = %s,
                  amount = %s, claimed_ref = null, claimed_by = null, claimed_at = null
            where target_kind = %s and target_id = %s and status in ('awaiting', 'rejected')""",
        (user_id, note, amount, kind, target_id),
    )
    return changed > 0


# ── What a page needs to show the panel ───────────────────────────────────

def context(amount, note, action, payment=None, mode="customer", error=None, show=True, vpa=None, name=None):
    """Every pay_* variable pages/_pay.html reads, for the CUSTOMER's side.

    Spread it into the render: render_template(page, **payments.context(...)).
    amount is what they owe, note is the order/booking NUMBER (never a name:
    nothing typed into a form belongs inside a upi:// link), action is the URL
    the "I have paid" form posts to. The QR and link appear only while there
    is something left to pay.
    """
    live = enabled(vpa) and show and not (payment and payment["status"] == "verified")
    return {
        "pay_enabled": enabled(vpa),
        "pay_payee": payee(name),
        "pay_amount": amount,
        "pay_link": pay_link(amount, note, vpa, name) if live else "",
        "pay_qr": qr_svg(amount, note, vpa, name) if live else None,
        "pay_action": action,
        "pay_error": error,
        "payment": payment,
        "mode": mode,
    }


def owner_context(payment, verify_action, reject_action):
    """The OWNER's side of the panel: what was claimed, and the two buttons."""
    return {
        "pay_enabled": enabled(),
        "mode": "owner",
        "payment": payment,
        "pay_verify_action": verify_action,
        "pay_reject_action": reject_action,
    }


# ── The three money routes, installed once ────────────────────────────────

def install(app, kind, who, bill, owner, manage, on_verified=None, on_rejected=None):
    """Attaches the claim / verify / reject routes to the app.

    The app hands in what only it knows:
      who()                 -> the logged-in user row, or None
      bill(target_id, user) -> {"amount": ..., "back": "/url"} when THIS user
                               may pay for THIS thing, else None (not theirs,
                               free, or unknown: every one of those is a
                               redirect with a reason, never a crash)
      owner(user, payment)  -> True when this user may judge this payment
      manage                -> the URL to return to after verify/reject, or a
                               function of the payment row that builds one
      on_verified(payment)  -> optional: what else to record when money is
                               confirmed (a khata writes the jama line)
      on_rejected(payment, reason) -> optional: tell the payer their money
                               was not found, and why (a notice)

    The URLs are fixed so every page can rely on them:
      POST /pay/<target_id>/claim
      POST /manage/payments/<payment_id>/verify
      POST /manage/payments/<payment_id>/reject
    """

    def _row(payment_id):
        return db.query_one(
            """select id, target_kind, target_id, amount, status, claimed_ref, note
                 from payments where id = %s and target_kind = %s""",
            (payment_id, kind),
        )

    def _back(payment):
        return manage(payment) if callable(manage) else manage

    @app.route("/pay/<int:target_id>/claim", methods=["POST"])
    def claim_payment(target_id):
        user = who()
        if not user:
            flash("Please log in first.", "warning")
            return redirect("/")
        due = bill(target_id, user) if bill else None
        if not enabled((due or {}).get("vpa")):
            flash("UPI is not switched on here yet. Please pay in cash.", "warning")
            return redirect("/")
        if not due:
            flash("There is nothing of yours to pay there.", "warning")
            return redirect("/")
        back = due.get("back") or "/"
        typed = clean_ref(request.form.get("ref", ""))
        if not typed:
            flash("That did not look like a 12-digit UPI reference. Check it in your UPI app and try again.", "warning")
            return redirect(back)
        now = get(kind, target_id)
        if now and now["status"] == "verified" and not due.get("new_round"):
            flash("This is already paid. There is nothing more to send.", "warning")
            return redirect(back)
        # One bank payment pays one thing: the same number on a second bill is a mistake or a trick.
        if reference_used(typed, kind, target_id):
            flash("That UPI reference was already used for another payment. Check it in your UPI app and try again.", "warning")
            return redirect(back)
        if not claim(kind, target_id, due["amount"], typed, user["id"], new_round=bool(due.get("new_round"))):
            flash("That reference was not saved. Check this page and try again.", "warning")
            return redirect(back)
        flash("Thank you. Your payment will show as paid once it is checked.", "success")
        return redirect(back)

    @app.route("/manage/payments/<int:payment_id>/verify", methods=["POST"])
    def verify_payment(payment_id):
        user = who()
        payment = _row(payment_id)
        if not user or not payment or not owner(user, payment):
            flash("Only the person who checks payments can do that.", "warning")
            return redirect("/")
        # The reference the owner looked up (sent by the page): never one the payer swapped in since.
        shown = clean_ref(request.form.get("ref", ""))
        if verify(payment_id, user["id"], ref=shown):
            if on_verified:
                on_verified(payment)
            flash("Payment marked as received.", "success")
        elif shown and payment["status"] == "claimed" and payment["claimed_ref"] != shown:
            flash("The payer sent a new reference since this page opened. Check the new one in your bank app.", "warning")
        else:
            flash("That payment was already checked.", "warning")
        return redirect(_back(payment))

    @app.route("/manage/payments/<int:payment_id>/reject", methods=["POST"])
    def reject_payment(payment_id):
        user = who()
        payment = _row(payment_id)
        if not user or not payment or not owner(user, payment):
            flash("Only the person who checks payments can do that.", "warning")
            return redirect("/")
        reason = request.form.get("reason", "").strip()[:200]
        if reject(payment_id, reason):
            if on_rejected:
                on_rejected(payment, reason)
            flash("Payment marked as not received. The payer " + ("is told and " if on_rejected else "") + "can send the reference again.", "success")
        else:
            flash("That payment was already checked.", "warning")
        return redirect(_back(payment))
