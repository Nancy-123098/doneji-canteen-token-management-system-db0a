"""Canteen Token Management System: every page of the app, in plain Python.

Flask is the only non-syllabus piece: @app.route("/x") means "when someone
opens /x, run this function". Every page checks who is looking before it
shows anything, and every SQL statement takes its values as %s parameters.

The functions marked "# slot:" are the app's own rules (who may apply, what a
report counts). Everything else follows from the app's plan.
"""
import os
import re
import secrets
import time as clock
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
import doneji
import payments
import photos

# "Today" and "two hours before" are India's, wherever the host's clock is set.
os.environ["TZ"] = "Asia/Kolkata"
if hasattr(clock, "tzset"):
    clock.tzset()

app = Flask(__name__, template_folder="pages")
# No fallback on purpose: a default here would quietly become the real
# signing key the day someone forgets to set the variable.
app.secret_key = os.environ["SECRET_KEY"]

# DoneJi's supervision layer: the health ping, /privacy, /dashboard and the footer.
doneji.install(app)

# The menu's photos: the owner's own photo, else a checked photo of a dish everyone knows, else
# the dish's first letter on a tile. Never a random photo (photos.py).
app.jinja_env.globals["stock_photo"] = photos.stock_photo


@app.before_request
def no_nul_bytes():
    """A NUL byte is never real input, and the database refuses it: a plain answer, never a crash."""
    if any("\x00" in value for value in [*request.args.values(), *request.form.values()]):
        return "Something you typed has a hidden character that cannot be saved. Please go back and type it again.", 400


@app.route("/.well-known/doneji.txt")
def doneji_proof():
    """Proves this deployment is yours: DoneJi checks your site serves this token."""
    return os.environ.get("DONEJI_VERIFY_TOKEN", ""), 200, {"Content-Type": "text/plain"}


def current_user():
    """The logged-in person's row, or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.query_one("select id, name, email, role, approved, student_id, contact_number from users where id = %s", (user_id,))


# Where each kind of account lands after logging in.
HOME_OF = {
    "canteen_staff": "/menu",
    "student": "/",
}


def home_of(user):
    return HOME_OF.get(user["role"], "/")


def safe_back(path):
    """A form's "come back here" address, only if it is a page of this site.

    No tab, newline or backslash: browsers drop or bend them, and "/	/x"
    becomes "//x", another site."""
    if not path or not path.startswith("/") or path.startswith("//") or chr(92) in path:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "/"
    return path


def next_page(default):
    """Where a log in or sign up goes on: the page that sent the person here
    (?next=, such as the shop's checkout), only if it is a page of this site."""
    wanted = request.values.get("next") or ""
    return wanted if wanted and safe_back(wanted) == wanted else default


def canteen_staff_seat_open():
    """True until the first Canteen Staff account exists: only that first account may take the role."""
    return db.query_one("select id from users where role = 'canteen_staff' limit 1") is None


@app.context_processor
def page_context():
    """What every page knows: who is logged in and whether the Canteen Staff seat is still open."""
    return {"user": current_user(), "canteen_staff_seat_open": canteen_staff_seat_open()}


def picked(name, kind="text"):
    """One filter box from the address, or None when it is empty or not valid."""
    value = (request.args.get(name) or "").strip()
    if not value:
        return None
    try:
        if kind == "date":
            return date.fromisoformat(value[:10])
        if kind == "numeric":
            return Decimal(value)
        if kind == "time":
            return datetime.strptime(value[:5], "%H:%M").time()
    except (ValueError, ArithmeticError):
        return None
    return value[:100]


PAGE_SIZE = 50


def page_link(number):
    """The same list at page number, its filters kept."""
    from urllib.parse import urlencode

    args = request.args.to_dict()
    args["page"] = str(number)
    return request.path + "?" + urlencode(args)


@app.context_processor
def notice_count():
    """How many notices the person looking has not opened yet, for the menu."""
    person = current_user()
    if not person:
        return {"unseen_notices": 0}
    return {"unseen_notices": db.query_one("select count(*) as n from dj_notices where user_id = %s and not seen", (person["id"],))["n"]}


@app.route("/my/notices")
def dj_notices():
    """The person's notices, newest first; opening the page marks them read."""
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    rows = db.query("select * from dj_notices where user_id = %s order by created_at desc limit 100", (user["id"],))
    db.execute("update dj_notices set seen = true where user_id = %s and not seen", (user["id"],))
    return render_template("dj_notices.html", rows=rows)


def home_counts():
    """The live numbers on the front page: counts only, never anyone's rows."""
    return [
        db.query_one("select count(*) as n from food_items")["n"],
    ]


@app.route("/")
def home():
    """The menu is the front page: anyone may look and fill a cart."""
    return shop_menu(front=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.query_one("select id, role, approved, password_hash from users where email = %s", (email,))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            if not user["approved"]:
                return render_template("login.html", error="Your account is waiting for approval.")
            session["user_id"] = user["id"]
            return redirect(next_page(home_of(user)))
        return render_template("login.html", error="Wrong email or password.")
    return render_template("login.html", error=None)


@app.route("/register", methods=["GET", "POST"])
def register():
    # The kind of account comes from the sign-up link (?role=...) or the form,
    # and is kept only if it is one of the ordinary roles anyone may pick.
    role = request.values.get("role", "student")
    if role not in ("student",):
        role = "student"
    # The Canteen Staff role goes to the FIRST account only, while the seat is open.
    role = "canteen_staff" if request.values.get("role") == "canteen_staff" and canteen_staff_seat_open() else role
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email:
            return render_template("register.html", role=role, error="Name and email are required.")
        if len(password) < 8:
            return render_template("register.html", role=role, error="Password must be at least 8 characters.")
        if db.query_one("select id from users where email = %s", (email,)):
            return render_template("register.html", role=role, error="That email is already registered.")
        # The questions this kind of account is asked besides name, email and password.
        asked = {"student": ("student_id", "contact_number")}.get(role, ())
        if any("\x00" in value for value in request.form.values()):
            return render_template("register.html", role=role, error="Something you typed has a hidden character that cannot be saved. Please type it again.")
        for column, label in {"student": (("student_id", "Student ID"), ("contact_number", "Contact number"))}.get(role, ()):
            if not request.form.get(column, "").strip():
                return render_template("register.html", role=role, error=label + " is required.")
        answers, problems = db.row_from_form(request.form, "users", only=asked) if asked else ({}, [])
        if problems:
            return render_template("register.html", role=role, error=problems[0])
        if answers.get("student_id") not in (None, "") and db.query_one("select id from users where lower(student_id::text) = lower(%s)", (str(answers.get("student_id")),)):
            return render_template("register.html", role=role, error="That student id is already registered.")
        approved = role not in ("__none__",)
        new = db.insert_from_form("users", {"name": name, "email": email, "password_hash": generate_password_hash(password), "role": role, "approved": approved, **answers})
        if role == "canteen_staff":
            db.execute("update food_items set staff = %s where staff is null", (new["id"],))
        if not approved:
            flash("Your account is made. It opens once it is approved.", "info")
            return redirect(url_for("login"))
        session["user_id"] = new["id"]
        return redirect(next_page(HOME_OF.get(role, "/")))
    return render_template("register.html", role=role, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


def dj_rupees(value):
    """Money as people write it: the rupee sign and 1,480, with paise only when there are some."""
    value = Decimal(value or 0)
    return "\u20b9" + ("{:,.0f}".format(value) if value == value.to_integral_value() else "{:,.2f}".format(value))


def dashboard_stats():
    """The canteen staff's dashboard: today's orders and money, the week, what sells, and what needs doing now."""
    user = current_user()
    if not user or user["role"] != "canteen_staff":
        return None
    where, args = shop_orders_where(user)
    today = db.query_one("select current_date as d")["d"]
    orders_today = db.query_one("select count(*) as n from shop_orders o where o.status <> 'cancelled' and o.created_at::date = current_date and " + where, args)["n"]
    # Money counts on the day it was confirmed, the same rule as the Sales page.
    money_today = db.query_one("select coalesce(sum(p.amount), 0) as m from payments p join shop_orders o on o.id = p.target_id where p.target_kind = 'order' and p.status = 'verified' and o.status <> 'cancelled' and p.verified_at::date = current_date and " + where, args)["m"]
    waiting = db.query_one("select count(*) as n from shop_orders o where o.status = any(%s) and " + where, (SHOP_OPEN, *args))["n"]
    to_check = db.query_one("select count(*) as n from payments p join shop_orders o on o.id = p.target_id where p.target_kind = 'order' and p.status = 'claimed' and o.status <> 'cancelled' and " + where, args)["n"]
    not_paid = db.query_one(
        "select count(*) as n from shop_orders o where o.status = %s and o.total > 0 "
        "and not exists (select 1 from payments p where p.target_kind = 'order' and p.target_id = o.id and p.status in ('verified', 'claimed')) "
        "and o.created_at >= current_date - 29 and " + where, ("completed", *args))["n"]
    claims = db.query(
        "select o.id, o.total, p.claimed_ref, (select name from users where id = o.buyer) as buyer_name from payments p join shop_orders o on o.id = p.target_id "
        "where p.target_kind = 'order' and p.status = 'claimed' and o.status <> 'cancelled' and " + where + " order by p.id limit 5", args)
    # Handed over and not paid: the money is still to collect (a customer may pay any time before collection).
    unpaid = db.query(
        "select o.id, o.total, (select name from users where id = o.buyer) as buyer_name from shop_orders o where o.status = %s and o.total > 0 "
        "and not exists (select 1 from payments p where p.target_kind = 'order' and p.target_id = o.id and p.status in ('verified', 'claimed')) "
        "and o.created_at >= current_date - 29 and " + where + " order by o.created_at limit 5", ("completed", *args))
    open_orders = db.query(
        "select o.id, o.status, o.total, o.mode, o.pickup_time, (select name from users where id = o.buyer) as buyer_name, "
        "(select string_agg(l.qty::text || ' ' || l.name, ', ' order by l.id) from shop_order_lines l where l.order_id = o.id) as what "
        "from shop_orders o where o.status = any(%s) and " + where + " order by o.created_at limit 6", (SHOP_OPEN, *args))

    # The money of the last 7 days, one bar a day; today's bar is the darker one.
    by_day = {r["day"]: r["m"] for r in db.query(
        "select p.verified_at::date as day, coalesce(sum(p.amount), 0) as m from payments p join shop_orders o on o.id = p.target_id "
        "where p.target_kind = 'order' and p.status = 'verified' and o.status <> 'cancelled' and p.verified_at >= current_date - 6 and " + where + " group by p.verified_at::date", args)}
    days = [today - timedelta(days=back) for back in range(6, -1, -1)]
    top_day = max(days, key=lambda d: by_day.get(d, 0))
    most = by_day.get(top_day, 0)
    week = {"title": "Money received, last 7 days"}
    if most > 0:
        week["days"] = [
            {"label": "Today" if d == today else d.strftime("%a"), "n": dj_rupees(by_day.get(d, 0)), "pct": int(round(by_day.get(d, 0) * 100 / most)), "now": d == today}
            for d in days
        ]
        week["note"] = "Busiest day: %s, %s." % ("today" if top_day == today else top_day.strftime("%A"), dj_rupees(most))
    else:
        week["empty"] = "No money received yet. The bars start with your first confirmed payment."

    # What sells most this week, by how many were sold (free extras are not what sells).
    top = db.query(
        "select (array_agg(l.name order by l.id desc))[1] as name, sum(l.qty) as qty, sum(l.qty * l.price) as amount from shop_order_lines l join shop_orders o on o.id = l.order_id "
        "where o.status <> 'cancelled' and l.price > 0 and o.created_at >= current_date - 6 and " + where + " group by coalesce(l.item_id::text, l.name) order by qty desc, amount desc limit 5", args)
    best = max([t["qty"] for t in top] or [0])
    bars = {"title": "What sells most, this week"}
    if best > 0:
        bars["rows"] = [{"label": t["name"], "text": "%s sold \u00b7 %s" % (t["qty"], dj_rupees(t["amount"])), "pct": int(round(t["qty"] * 100 / best))} for t in top]
    else:
        bars["empty"] = "Nothing sold this week yet. Your best sellers show here from the first order."

    rows = []
    for o in claims:
        rows.append({"badge": "UPI to check", "text": "#%s \u00b7 %s \u00b7 ref %s" % (o["id"], o["buyer_name"] or "Customer", o["claimed_ref"] or "-"), "meta": dj_rupees(o["total"]), "href": "/shop/manage", "action": "Check payment"})
    for o in unpaid:
        row = {"badge": "not paid", "text": "#%s \u00b7 %s \u00b7 completed \u00b7 %s not paid yet" % (o["id"], o["buyer_name"] or "Customer", dj_rupees(o["total"]))}
        row.update({"post": "/shop/manage/%s/cash" % o["id"], "action": "Cash received"})
        rows.append(row)
    for o in open_orders:
        what = o["what"] or ""
        when = o["mode"].replace("_", " ") + (" " + o["pickup_time"] if o["pickup_time"] else "")
        rows.append({"badge": o["status"].replace("_", " "), "text": "#%s \u00b7 %s \u00b7 %s" % (o["id"], o["buyer_name"] or "Customer", what if len(what) <= 80 else what[:77] + "..."), "meta": "%s \u00b7 %s" % (dj_rupees(o["total"]), when), "href": "/shop/manage", "action": "Open"})

    return {
        "eyebrow": "Canteen Staff",
        "title": "Dashboard",
        "lead": "Today at %s, %s" % ("Canteen Token Management System", today.strftime("%A %d %B").replace(" 0", " ")),
        "actions": [
            {"label": "Orders waiting (%s)" % waiting if waiting else "Orders", "href": "/shop/manage"},
            {"label": "Menu", "href": "/menu"},
            {"label": "Sales", "href": "/shop/sales"},
        ],
        "stats": [
            {"label": "Orders today", "value": orders_today, "foot": "since midnight"},
            {"label": "Money received today", "value": dj_rupees(money_today), "foot": "UPI and cash you confirmed"},
            {"label": "Waiting", "value": waiting, "foot": "not finished yet"},
            {"label": "UPI to check", "value": to_check, "foot": "a customer says they paid"},
        ],
        "week": week,
        "bars": bars,
        "panels": [
            {"title": "Needs you now", "empty": "Nothing waiting. New orders show here first.", "rows": rows},
        ],
    }


@app.route("/menu")
def page_menu():
    """Menu: the list page for student, canteen_staff."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    if user["role"] not in ("student", "canteen_staff"):
        flash("That page is only for the student or canteen staff.", "warning")
        return redirect(url_for("home"))
    if (current_user() or {}).get("role") == "student":
        return redirect("/shop")
    # The filter boxes above the list: a box left empty shows every row.
    shown = {
        "q": picked("q"),
        "category": picked("category"),
    }
    page_no = max(1, min(request.args.get("page", 1, type=int) or 1, 100000))
    rows = db.query("select t.*, (select name from users where id = t.staff) as staff_label, t.name as dj_label from food_items t where (%s::text is null or t.name::text ilike %s::text) and (%s::text is null or t.category::text = %s::text) order by t.name asc nulls last, t.created_at asc limit %s offset %s", (shown["q"], ("%" + shown["q"] + "%") if shown["q"] else None, shown["category"], shown["category"], PAGE_SIZE + 1, (page_no - 1) * PAGE_SIZE))
    next_url = page_link(page_no + 1) if len(rows) > PAGE_SIZE else None
    prev_url = page_link(page_no - 1) if page_no > 1 else None
    rows = rows[:PAGE_SIZE]
    return render_template("menu.html", choices={}, parent=None, shown=shown, next_url=next_url, prev_url=prev_url, rows=rows)

@app.route("/menu/<int:id>")
def page_food_item_detail(id):
    """Menu item: the detail page for canteen_staff."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    if user["role"] not in ("canteen_staff",):
        flash("That page is only for the canteen staff.", "warning")
        return redirect(url_for("home"))
    row = db.query_one("select t.*, (select name from users where id = t.staff) as staff_label, t.name as dj_label from food_items t where t.id = %s", (id,))
    if row is None:
        flash("That menu item no longer exists.", "warning")
        return redirect(url_for("home"))
    return render_template("food_item_detail.html", choices={}, row=row)

@app.route("/do/add_item", methods=["POST"])
@app.route("/do/add_item/<int:row_id>", methods=["POST"])
def act_add_item(row_id=None):
    """Add item: create food item, for canteen_staff."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    if user["role"] not in ("canteen_staff",):
        flash("Only the canteen staff can do that.", "warning")
        return redirect(url_for("home"))
    back = safe_back(request.form.get("back"))
    if any("\x00" in value for value in request.form.values()):
        flash("Something you typed has a hidden character that cannot be saved. Please type it again.", "warning")
        return redirect(back)
    row, errors = db.row_from_form(request.form, "food_items", required=("name", "price", "category"), only=("name", "price", "category"))
    if errors:
        flash(errors[0], "warning")
        return redirect(back)
    if row.get("name") is not None and len(str(row.get("name"))) > 200:
        flash("Food item name is too long (200 letters at most).", "warning")
        return redirect(back)
    if row.get("price") is not None and not row.get("price").is_finite():
        flash("Price must be a number.", "warning")
        return redirect(back)
    if row.get("price") is not None and abs(row.get("price")) >= Decimal("10000000000"):
        flash("The number for price is too big.", "warning")
        return redirect(back)
    if row.get("price") is not None and row.get("price") < Decimal("1"):
        flash("Price must be at least 1.", "warning")
        return redirect(back)
    if row.get("price") is not None and abs(row.get("price")) > Decimal("9999999999.99"):
        flash("The number for price is too big.", "warning")
        return redirect(back)
    if row.get("category") is not None and row.get("category") not in ("snack", "meal", "beverage"):
        flash("Please pick a category from the list.", "warning")
        return redirect(back)
    row["staff"] = user["id"]
    row["on_menu"] = True
    try:
        new = db.insert_from_form("food_items", row)
    except Exception as exc:
        if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
        flash("That menu item already exists.", "warning")
        return redirect(back)
    flash("Add item: done.", "success")
    return redirect(back)

@app.route("/do/take_off", methods=["POST"])
@app.route("/do/take_off/<int:row_id>", methods=["POST"])
def act_take_off(row_id=None):
    """Take off: update food item, for canteen_staff."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    if user["role"] not in ("canteen_staff",):
        flash("Only the canteen staff can do that.", "warning")
        return redirect(url_for("home"))
    back = safe_back(request.form.get("back"))
    if any("\x00" in value for value in request.form.values()):
        flash("Something you typed has a hidden character that cannot be saved. Please type it again.", "warning")
        return redirect(back)
    if row_id is None:
        flash("Nothing to change.", "warning")
        return redirect(back)
    row = db.query_one("select * from food_items where id = %s", (row_id,)) if row_id and row_id <= 2147483647 else None
    if row is None:
        flash("That menu item no longer exists.", "warning")
        return redirect(back)
    if row["staff"] != user["id"]:
        flash("That menu item is not yours.", "warning")
        return redirect(back)
    changes, errors = db.row_from_form(request.form, "food_items", required=(), only=("on_menu",))
    if errors:
        flash(errors[0], "warning")
        return redirect(back)
    db.execute("update food_items set on_menu = %s where id = %s", (False, row["id"]))
    flash("Take off: done.", "success")
    return redirect(back)

@app.route("/do/put_back", methods=["POST"])
@app.route("/do/put_back/<int:row_id>", methods=["POST"])
def act_put_back(row_id=None):
    """Put back: update food item, for canteen_staff."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    if user["role"] not in ("canteen_staff",):
        flash("Only the canteen staff can do that.", "warning")
        return redirect(url_for("home"))
    back = safe_back(request.form.get("back"))
    if any("\x00" in value for value in request.form.values()):
        flash("Something you typed has a hidden character that cannot be saved. Please type it again.", "warning")
        return redirect(back)
    if row_id is None:
        flash("Nothing to change.", "warning")
        return redirect(back)
    row = db.query_one("select * from food_items where id = %s", (row_id,)) if row_id and row_id <= 2147483647 else None
    if row is None:
        flash("That menu item no longer exists.", "warning")
        return redirect(back)
    if row["staff"] != user["id"]:
        flash("That menu item is not yours.", "warning")
        return redirect(back)
    changes, errors = db.row_from_form(request.form, "food_items", required=(), only=("on_menu",))
    if errors:
        flash(errors[0], "warning")
        return redirect(back)
    db.execute("update food_items set on_menu = %s where id = %s", (True, row["id"]))
    flash("Put back: done.", "success")
    return redirect(back)

# ---- The shop (the plan's "shop"): menu, cart, checkout and orders. ----------

SHOP_MODES = "pickup".split()
SHOP_NEXT = {"placed": "pending", "pending": "completed"}
SHOP_OPEN = "placed pending".split()
# The paid step is the money: the order is completed only once it is paid.
SHOP_PAID_BEFORE = "completed"

# What checkout asks once per order (the plan's "ask"): each answer is saved on the order as ask_<name>.
SHOP_ASK = [
    {"name": "contact_number", "label": "Contact number", "type": "text", "required": True, "values": (), "min": None, "max": None, "range": ""},
]


def shop_asked(form, user):
    """The answers to the shop's own questions, each checked, and the first thing wrong, said once.
    """
    answers = {}
    for q in SHOP_ASK:
        raw = (form.get("ask_" + q["name"]) or "").strip()
        label = q["label"]
        if not raw:
            if q["required"]:
                return answers, ("Please add " if q["type"] == "file" else "Please choose " if q["type"] == "enum" else "Please fill in ") + label.lower() + "."
            answers[q["name"]] = None
            continue
        if q["type"] == "int":
            if not re.fullmatch(r"-?[0-9]{1,9}", raw):
                return answers, label + " must be a whole number."
            value = int(raw)
        elif q["type"] == "decimal":
            try:
                value = Decimal(raw)
            except ArithmeticError:
                value = None
            if value is None or not value.is_finite() or abs(value) >= Decimal("10000000000"):
                return answers, label + " must be a number."
        elif q["type"] == "date":
            try:
                value = date.fromisoformat(raw[:10])
            except ValueError:
                return answers, "Please enter " + label.lower() + " as a date."
        elif q["type"] == "enum":
            if raw not in q["values"]:
                return answers, "Please choose " + label.lower() + " from the list."
            value = raw
        else:
            if len(raw) > 300:
                return answers, label + " is too long (300 letters at most)."
            value = raw
        if q["type"] in ("int", "decimal") and ((q["min"] is not None and value < q["min"]) or (q["max"] is not None and value > q["max"])):
            return answers, label + " must be " + q["range"] + "."
        answers[q["name"]] = value
    return answers, None


def shop_cart():
    """The cart, kept in the session as {item_id: quantity}: its lines and total.

    Prices come from the menu now, never from the cart: an item that went off
    the menu leaves the cart here, not on the bill.
    """
    cart = session.get("cart", {})
    lines, total = [], Decimal("0")
    ids = [int(k) for k in cart if str(k).isdigit() and int(k) <= 2147483647]
    if not ids:
        return lines, total
    rows = db.query("select id, name as name, price as price from food_items where id = any(%s) and on_menu", (ids,))
    by_id = {row["id"]: row for row in rows}
    for key, qty in sorted(cart.items()):
        row = by_id.get(int(key)) if str(key).isdigit() else None
        if row is None or row["price"] is None:
            continue
        qty = max(1, min(int(qty), 99))
        line_total = qty * row["price"]
        total += line_total
        lines.append({"id": row["id"], "name": row["name"], "qty": qty, "price": row["price"], "line_total": line_total})
    return lines, total


@app.route("/shop")
def shop_menu(front=False):
    """The menu: everything for sale, with an Add button. Anyone may look.
    front: the app's front page "/" shows this same menu (home.html is this page)."""
    category = (request.args.get("category") or "").strip()[:60]
    items = db.query("select * from food_items t where true and t.on_menu and (%s = '' or t.category = %s) order by t.category, t.name", (category, category))
    categories = [r["c"] for r in db.query("select distinct category as c from food_items where category is not null and on_menu order by 1")]
    lines, total = shop_cart()
    if front:
        return render_template("home.html", items=items, cart=lines, cart_total=total, categories=categories, active=category)
    return render_template("shop_menu.html", items=items, cart=lines, cart_total=total, categories=categories, active=category)


def shop_buyer():
    """The logged-in student, or None with the reason already said."""
    user = current_user()
    if user is None:
        flash("Log in to order.", "warning")
        return None
    if user["role"] != "student":
        flash("Only a student can order.", "warning")
        return None
    return user


@app.route("/shop/cart/<int:item_id>/add", methods=["POST"])
def shop_cart_add(item_id):
    """One more of an item in the cart. A visitor fills a cart too, and signs up at checkout."""
    back = safe_back(request.form.get("back"))
    user = current_user()
    if user is not None and user["role"] != "student":
        flash("Only a student can order.", "warning")
        return redirect(back)
    item = db.query_one("select id from food_items where id = %s and on_menu", (item_id,)) if item_id <= 2147483647 else None
    if item is None:
        flash("That item is not for sale now.", "warning")
        return redirect(back)
    cart = session.get("cart", {})
    cart[str(item_id)] = min(int(cart.get(str(item_id), 0)) + 1, 99)
    session["cart"] = cart
    return redirect(back)

@app.route("/shop/cart/<int:item_id>/less", methods=["POST"])
def shop_cart_less(item_id):
    """One fewer of an item; none left takes it out of the cart."""
    cart = session.get("cart", {})
    left = int(cart.get(str(item_id), 0)) - 1
    if left > 0:
        cart[str(item_id)] = left
    else:
        cart.pop(str(item_id), None)
    session["cart"] = cart
    return redirect(safe_back(request.form.get("back")))



@app.route("/shop/cart/clear", methods=["POST"])
def shop_cart_clear():
    """Empties the cart."""
    session["cart"] = {}
    return redirect(url_for("shop_menu"))


def shop_clock_time(text):
    """True for a time of day as the time box sends it, such as 18:30."""
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        return False
    return True


def shop_saved_details(user):
    """The phone and address this person already saved, to fill the checkout boxes."""
    return {}


@app.route("/shop/checkout", methods=["GET", "POST"])
def shop_checkout():
    """The cart, how it goes out, and how it is paid; then one button places the order."""
    if current_user() is None:
        # The cart lives in the session, so it is still here after signing up or logging in.
        flash("Sign up or log in to place your order. Your cart is kept.", "info")
        return redirect("/register?role=student&next=/shop/checkout")
    user = shop_buyer()
    if user is None:
        return redirect(url_for("shop_menu"))
    lines, total = shop_cart()
    # An item that went off the menu leaves the cart with a word, never silently.
    gone = [key for key in session.get("cart", {}) if str(key) not in {str(line["id"]) for line in lines}]
    if gone:
        session["cart"] = {key: qty for key, qty in session.get("cart", {}).items() if key not in gone}
        flash("Something in your cart went off the menu, so it was taken out.", "warning")
    if not lines:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("shop_menu"))
    saved = shop_saved_details(user)
    if request.method == "GET":
        return render_template("shop_checkout.html", cart=lines, cart_total=total, modes=SHOP_MODES, upi=payments.enabled(), error=None, entered={"mode": SHOP_MODES[0], "pay_method": "upi", **saved})
    mode = request.form.get("mode", SHOP_MODES[0])
    if mode not in SHOP_MODES:
        mode = SHOP_MODES[0]
    address = (request.form.get("address") or "").strip() or None
    pickup_time = (request.form.get("pickup_time") or "").strip() or None
    table_no = (request.form.get("table_no") or "").strip()
    note = (request.form.get("note") or "").strip() or None
    phone = "".join(ch for ch in (request.form.get("phone") or "") if ch.isdigit() or ch == "+") or None
    pay_method = "upi" if request.form.get("pay_method") == "upi" and payments.enabled() else "cash"
    error = None
    # Too long is said, never cut off quietly (a 2000-letter address was cut, Sample Kitchen V6).
    if address and len(address) > 300:
        error = "The delivery address is too long (300 letters at most)."
    elif note and len(note) > 300:
        error = "The note is too long (300 letters at most)."
    elif pickup_time and not shop_clock_time(pickup_time):
        error = "Please enter a pickup time like 18:30."
    elif mode == "pickup" and pickup_time and db.query_one("select %s::time < date_trunc('minute', localtimestamp)::time as gone", (pickup_time,))["gone"]:
        # An order is for today, and a time already gone today is refused (2026-11-15, Sample Plant
        # Nursery took a 09:00 pickup at 20:15). The clock is the database's, India time, as Missed's.
        error = "That pickup time has already gone today. Please pick a later time."
    elif mode == "delivery" and not address:
        error = "Please enter a delivery address."
    elif mode == "delivery" and not (phone and len(phone) <= 16 and 10 <= len(phone.lstrip("+")) <= 13):
        error = "Please enter a phone number for the delivery, 10 to 13 digits."
    elif mode == "pickup" and not pickup_time:
        error = "Please enter a pickup time."
    elif mode == "table" and not (table_no.isdigit() and 0 < int(table_no) < 1000):
        error = "Table number must be a whole number from 1 to 999."
    elif total <= 0:
        error = "This order comes to nothing. Add something with a price first."
    elif total >= Decimal("100000000"):
        error = "This order is too large to take here."
    elif request.form.get("seen_total") and request.form.get("seen_total") != str(total):
        error = "The total changed while you were here. Please check it and place the order again."
    asked, problem = shop_asked(request.form, user)
    error = error or problem
    if error:
        # Everything typed stays in the boxes, so a refused order is one correction away.
        return render_template("shop_checkout.html", cart=lines, cart_total=total, modes=SHOP_MODES, upi=payments.enabled(), error=error, entered={**saved, **request.form.to_dict()})
    order = db.insert_from_form("shop_orders", {
        "buyer": user["id"], "mode": mode, "address": address if mode == "delivery" else None,
        "pickup_time": pickup_time if mode == "pickup" else None,
        "table_no": int(table_no) if mode == "table" else None,
        "note": note, "total": total, "pay_method": pay_method, "phone": phone if mode == "delivery" else None,
        **{"ask_" + name: value for name, value in asked.items()},
    })
    for line in lines:
        db.execute(
            "insert into shop_order_lines (order_id, item_id, name, price, qty) values (%s, %s, %s, %s, %s)",
            (order["id"], line["id"], line["name"], line["price"], line["qty"]),
        )
    session["cart"] = {}
    flash("Order placed.", "success")
    return redirect(url_for("shop_pay", order_id=order["id"]))


def shop_order_of(order_id, user):
    """The order, when it is this student's own; else None."""
    if user is None or order_id > 2147483647:
        return None
    return db.query_one("select * from shop_orders where id = %s and buyer = %s", (order_id, user["id"]))


@app.route("/shop/orders/<int:order_id>/pay", methods=["GET", "POST"])
def shop_pay(order_id):
    """The order's bill and how it is paid: UPI now, or cash. Changeable until money is in flight."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    order = shop_order_of(order_id, user)
    if order is None:
        flash("That order is not one of yours.", "warning")
        return redirect(url_for("shop_menu"))
    payment = payments.get("order", order_id)
    if request.method == "POST":
        if payment and payment["status"] in ("claimed", "verified"):
            flash("This order's payment is already on its way.", "warning")
        else:
            method = "upi" if request.form.get("pay_method") == "upi" and payments.enabled() else "cash"
            db.execute("update shop_orders set pay_method = %s where id = %s", (method, order_id))
            flash("You will pay by UPI." if method == "upi" else "You will pay in cash.", "success")
        return redirect(url_for("shop_pay", order_id=order_id))
    lines = db.query("select * from shop_order_lines where order_id = %s order by id", (order_id,))
    show = order["pay_method"] == "upi" and order["status"] not in ("cancelled", "missed")
    return render_template(
        "shop_pay.html", order=order, lines=lines, upi=payments.enabled(),
        **payments.context(order["total"], order["id"], "/pay/%s/claim" % order["id"], payment=payment, mode="customer", show=show),
    )


@app.route("/shop/my-orders")
def shop_my_orders():
    """The student's orders, newest first, each with what is paid."""
    user = current_user()
    if user is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    orders = db.query("select o.* from shop_orders o where o.buyer = %s order by o.created_at desc limit 100", (user["id"],))
    paid = payments.by_kind("order", [o["id"] for o in orders]) if orders else {}
    lines = {}
    for line in db.query("select * from shop_order_lines where order_id = any(%s) order by id", ([o["id"] for o in orders] or [0],)):
        lines.setdefault(line["order_id"], []).append(line)
    return render_template("shop_my_orders.html", orders=orders, paid=paid, lines=lines)


def shop_bill(target_id, user):
    """What payments.py may collect for an order: only its own student's, while it is open."""
    order = shop_order_of(target_id, user)
    if order is None or order["status"] in ("cancelled", "missed") or order["total"] <= 0 or order["pay_method"] != "upi":
        return None
    return {"amount": order["total"], "back": "/shop/orders/%s/pay" % order["id"]}


def shop_checks_money(user, payment):
    """Only the canteen staff judges a payment."""
    if not user or user["role"] != "canteen_staff":
        return False
    return True


def shop_tell(order, text):
    """The buyer hears each step of their order under Notices (2026-11-05: the shop wrote none)."""
    user = current_user()
    if order and order["buyer"] != (user["id"] if user else None):
        db.execute("insert into dj_notices (user_id, text) values (%s, %s)", (order["buyer"], text))


def shop_paid(payment):
    """Confirmed UPI money: the buyer is told their order is paid."""
    shop_tell(db.query_one("select * from shop_orders where id = %s", (payment["target_id"],)), "Order %s is paid." % payment["target_id"])


def shop_unpaid(payment, reason):
    """UPI money the canteen staff could not find: the buyer is told, and why."""
    shop_tell(db.query_one("select * from shop_orders where id = %s", (payment["target_id"],)), "Order %s: your UPI payment was not found%s. Check the reference in your UPI app and send it again." % (payment["target_id"], (": " + reason) if reason else ""))


payments.install(app, "order", who=current_user, bill=shop_bill, owner=shop_checks_money, manage="/shop/manage", on_verified=shop_paid, on_rejected=shop_unpaid)


def shop_seller():
    """The logged-in canteen staff, or None: the money is theirs alone."""
    user = current_user()
    return user if user and user["role"] == "canteen_staff" else None


def shop_staff():
    """Whoever moves orders on (canteen_staff), or None."""
    user = current_user()
    return user if user and user["role"] in ("canteen_staff",) else None


def shop_reader():
    """Whoever may read the orders (canteen_staff), or None."""
    user = current_user()
    return user if user and user["role"] in ("canteen_staff",) else None

def shop_orders_where(user):
    """Everyone who works the orders reads every order: there is one shop."""
    return "true", ()


def shop_may_move(order, user):
    """There is one shop: whoever works the orders may move any of them."""
    return True


def shop_refuse(why):
    """Sends someone who may not be here away, saying why."""
    if current_user() is None:
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))
    flash(why, "warning")
    return redirect(url_for("home"))


@app.route("/shop/manage")
def shop_manage():
    """Every order this person may read, newest first: its lines, its money, and the next step."""
    user = shop_reader()
    if user is None:
        return shop_refuse("Only the canteen staff can move orders on.")
    where, args = shop_orders_where(user)
    # What needs doing comes first, oldest first; finished orders follow, newest first.
    orders = db.query("select o.*, (select name from users where id = o.buyer) as buyer_name from shop_orders o where " + where + " order by (o.status = any(%s)) desc, case when o.status = any(%s) then o.created_at end asc, o.created_at desc limit 200", (*args, SHOP_OPEN, SHOP_OPEN))
    paid = payments.by_kind("order", [o["id"] for o in orders]) if orders else {}
    lines = {}
    for line in db.query("select * from shop_order_lines where order_id = any(%s) order by id", ([o["id"] for o in orders] or [0],)):
        lines.setdefault(line["order_id"], []).append(line)
    return render_template("shop_manage.html", orders=orders, paid=paid, lines=lines, steps=SHOP_NEXT, open=SHOP_OPEN, money=shop_seller() is not None, moves=shop_staff() is not None)


@app.route("/shop/sales")
def shop_sales():
    """Today's orders and takings, and what sells most over the last 30 days."""
    user = current_user()
    if user is None or user["role"] not in ("canteen_staff",):
        return shop_refuse("Only the canteen staff can see the sales.")
    where, args = shop_orders_where(user)
    # Ordered is not received: "total" is what was ordered, "received" the money the canteen staff has confirmed.
    # A missed order was usually paid first (the paid step comes before ready), so its money stays received.
    # Money received in a window is money CONFIRMED in it, whatever day it was ordered (batch 5 and 6,
    # 2026-11-07: an order placed at night and paid in the morning counted on the wrong day).
    def received_since(days, upi=False):
        since = "" if days is None else " and p.verified_at >= current_date - " + str(int(days))
        # The part paid by UPI, at the counter or by reference: the order keeps how it was paid.
        if upi:
            since += " and o.pay_method = 'upi'"
        row = db.query_one(
            "select coalesce(sum(p.amount), 0) as received from payments p join shop_orders o on o.id = p.target_id "
            "where p.target_kind = 'order' and p.status = 'verified' and o.status <> 'cancelled'" + since + " and " + where, args)
        return row["received"] if row else 0
    def placed(days):
        since = "" if days is None else " and o.created_at >= current_date - " + str(int(days))
        row = dict(db.query_one("select count(*) as orders, coalesce(sum(o.total), 0) as total from shop_orders o where o.status <> 'cancelled'" + since + " and " + where, args) or {"orders": 0, "total": 0})
        row["received"] = received_since(days)
        row["upi"] = received_since(days, upi=True)
        return row
    ever = placed(None)
    today = placed(0)
    week = placed(6)
    # One row per item whatever it was called then, and free extras are not what sells.
    top = db.query(
        "select (array_agg(l.name order by l.id desc))[1] as name, sum(l.qty) as qty, sum(l.qty * l.price) as amount from shop_order_lines l join shop_orders o on o.id = l.order_id "
        "where o.status <> 'cancelled' and l.price > 0 and o.created_at >= current_date - 29 and " + where + " group by coalesce(l.item_id::text, l.name) order by qty desc, amount desc limit 10", args
    )
    # Per item for today and for the last 7 days (2026-11-06): Sample Bakery's baker asked for
    # "how many of each item were sold today" and Sample Tiffin for "money taken per tiffin this
    # week"; the page had only totals and a 30-day list. "Received" is money confirmed.
    paid_line = "exists (select 1 from payments p where p.target_kind = 'order' and p.target_id = o.id and p.status = 'verified')"
    def per_item(days):
        return db.query(
            "select (array_agg(l.name order by l.id desc))[1] as name, sum(l.qty) as qty, sum(l.qty * l.price) as amount, "
            "coalesce(sum(l.qty * l.price) filter (where " + paid_line + "), 0) as received "
            "from shop_order_lines l join shop_orders o on o.id = l.order_id "
            "where o.status <> 'cancelled' and l.price > 0 and o.created_at >= current_date - " + str(int(days)) + " and " + where +
            " group by coalesce(l.item_id::text, l.name) order by qty desc, amount desc limit 30", args
        )
    items_today = per_item(0)
    items_week = per_item(6)
    # Each day of the last 30, for "the money taken each day" (Sample Canteen Tokens).
    # Orders by the day they were placed, money by the day it was confirmed (a missed order paid first keeps its money).
    by_day = {}
    for r in db.query(
        "select o.created_at::date as day, count(*) as orders, coalesce(sum(o.total), 0) as total "
        "from shop_orders o where o.status <> 'cancelled' and o.created_at >= current_date - 29 and " + where +
        " group by o.created_at::date", args
    ):
        by_day[r["day"]] = {"day": r["day"], "orders": r["orders"], "total": r["total"], "received": 0}
    for r in db.query(
        "select p.verified_at::date as day, coalesce(sum(p.amount), 0) as received from payments p join shop_orders o on o.id = p.target_id "
        "where p.target_kind = 'order' and p.status = 'verified' and o.status <> 'cancelled' and p.verified_at >= current_date - 29 and " + where +
        " group by p.verified_at::date", args
    ):
        by_day.setdefault(r["day"], {"day": r["day"], "orders": 0, "total": 0, "received": 0})["received"] = r["received"]
    days = [by_day[k] for k in sorted(by_day, reverse=True)]
    stores = []
    return render_template("shop_sales.html", today=today, week=week, ever=ever, top=top, stores=stores, items_today=items_today, items_week=items_week, days=days)


@app.route("/shop/manage/<int:order_id>/next", methods=["POST"])
def shop_next(order_id):
    """Moves an order one step on: placed, pending, completed."""
    if shop_staff() is None:
        return shop_refuse("Only the canteen staff can move orders on.")
    order = db.query_one("select * from shop_orders where id = %s", (order_id,)) if order_id <= 2147483647 else None
    if order is None:
        flash("That order no longer exists.", "warning")
    elif not shop_may_move(order, current_user()):
        flash("That order is not one you can change.", "warning")
    elif order["status"] not in SHOP_NEXT or request.form.get("from", order["status"]) != order["status"]:
        # A second press, or a page left open: the button's words no longer match the order.
        flash("Order %s is already %s." % (order_id, order["status"].replace("_", " ")), "warning")
    elif SHOP_NEXT[order["status"]] == SHOP_PAID_BEFORE and (payments.get("order", order_id) or {}).get("status") != "verified":
        flash("Order %s is not paid yet. Record the cash, or check the UPI payment, first." % order_id, "warning")
    else:
        db.execute("update shop_orders set status = %s where id = %s", (SHOP_NEXT[order["status"]], order_id))
        shop_tell(order, "Order %s is now %s." % (order_id, SHOP_NEXT[order["status"]].replace("_", " ")))
        flash("Order %s is now %s." % (order_id, SHOP_NEXT[order["status"]].replace("_", " ")), "success")
    return redirect(url_for("shop_manage"))


@app.route("/shop/manage/<int:order_id>/cancel", methods=["POST"])
def shop_cancel(order_id):
    """Cancels an order that is not finished yet."""
    if shop_staff() is None:
        return shop_refuse("Only the canteen staff can move orders on.")
    order = db.query_one("select * from shop_orders where id = %s", (order_id,)) if order_id <= 2147483647 else None
    if order is None:
        flash("That order no longer exists.", "warning")
    elif not shop_may_move(order, current_user()):
        flash("That order is not one you can change.", "warning")
    elif order["status"] not in SHOP_OPEN:
        flash("Order %s is already %s, so it cannot be cancelled." % (order_id, order["status"].replace("_", " ")), "warning")
    elif (payments.get("order", order_id) or {}).get("status") == "claimed":
        flash("Order %s has a UPI payment waiting to be checked. Check it first (Verified or Not received), then cancel." % order_id, "warning")
    elif (payments.get("order", order_id) or {}).get("status") == "verified":
        # A paid order is never cancelled (2026-11-15, Sample Staff Lunch): Sales leaves cancelled
        # orders out, so the cash the canteen had taken was counted nowhere.
        flash("Order %s is already paid, so it cannot be cancelled. Please finish it instead." % order_id, "warning")
    else:
        db.execute("update shop_orders set status = 'cancelled' where id = %s", (order_id,))
        shop_tell(order, "Order %s is cancelled." % order_id)
        flash("Order %s is cancelled." % order_id, "success")
    return redirect(url_for("shop_manage"))


@app.route("/shop/manage/<int:order_id>/cash", methods=["POST"])
def shop_cash(order_id):
    """The canteen staff saw the money at the counter: recorded as paid, by cash or by UPI."""
    # UPI paid at the counter is UPI (2026-11-15, Sample Plant Nursery: it was saved as cash).
    how = "upi" if request.form.get("pay_method") == "upi" else "cash"
    user = shop_seller()
    if user is None:
        return shop_refuse("Only the canteen staff can see the sales and record money.")
    order = db.query_one("select * from shop_orders where id = %s", (order_id,)) if order_id <= 2147483647 else None
    if order is None:
        flash("That order no longer exists.", "warning")
    elif not shop_may_move(order, user):
        flash("That order is not one you can change.", "warning")
    elif order["status"] in ("cancelled", "missed") or order["total"] <= 0:
        flash("Order %s is %s, so there is no money to record." % (order_id, order["status"]), "warning")
    elif payments.cash("order", order_id, order["total"], user["id"], note="UPI at the counter" if how == "upi" else "cash at counter"):
        db.execute("update shop_orders set pay_method = %s where id = %s", (how, order_id))
        shop_tell(order, "Order %s is paid (%s)." % (order_id, "UPI" if how == "upi" else "cash"))
        flash(("UPI" if how == "upi" else "Cash") + " for order %s is recorded." % order_id, "success")
    elif (payments.get("order", order_id) or {}).get("status") == "claimed":
        flash("Order %s has a UPI payment waiting to be checked. Check it first: if it did not arrive, press Not received, then record the cash." % order_id, "warning")
    else:
        flash("Order %s is already paid." % order_id, "warning")
    # The dashboard's "Cash received" comes back to the dashboard.
    return redirect(safe_back(request.form.get("back")) if request.form.get("back") else url_for("shop_manage"))

if __name__ == "__main__":
    db.init_db()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
