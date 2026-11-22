-- Initial schema for Canteen Token Management System.
-- DoneJi ran this file against your database when it made it, and
-- recorded it in supabase_migrations.schema_migrations, the same
-- record Supabase's own GitHub integration keeps, so linking the repo
-- later applies only NEWER files. To change the schema, add a NEW file
-- with a newer timestamp. Never edit this one: applied migrations are
-- tracked by timestamp and an edited file is silently skipped.

-- Canteen Token Management System: the tables. Safe to run on every start.

-- Everyone who can log in. The role decides what each person may see and do;
-- an "approved" of false means the account waits for the Canteen Staff to let it in.
create table if not exists users (
  id             serial primary key,
  name           text not null,
  email          text not null constraint users_email_key unique,
  password_hash  text not null,
  role           text not null default 'student' constraint users_role_check check (role in ('canteen_staff', 'student')),
  approved       boolean not null default true,
  created_at     timestamp not null default now(),
  student_id     text,
  contact_number text
);

-- Two accounts never share a student id.
create unique index if not exists users_student_id_key on users (lower(student_id::text)) where student_id is not null;

-- Menu items.
create table if not exists food_items (
  id             serial primary key,
  staff          integer references users(id) on delete cascade,
  name           text not null,
  price          numeric(12, 2) not null constraint food_items_price_range check (price >= 1),
  category       text not null constraint food_items_category_check check (category in ('snack', 'meal', 'beverage')),
  on_menu        boolean not null default false,
  created_at     timestamp not null default now()
);

-- Sample menu items, added only while the table is empty.
insert into food_items (name, price, category, on_menu)
select * from (values
  ('Samosa'::text, 10::numeric(12, 2), 'snack'::text, true),
  ('Dosa'::text, 30::numeric(12, 2), 'snack'::text, true),
  ('Veg Thali'::text, 50::numeric(12, 2), 'meal'::text, true),
  ('Chole Bhature'::text, 40::numeric(12, 2), 'meal'::text, true),
  ('Masala Chai'::text, 10::numeric(12, 2), 'beverage'::text, true)
) as sample (name, price, category, on_menu)
where not exists (select 1 from food_items);

-- The owner's photo link for each item they sell: an https address, or empty for our photo.
alter table food_items add column if not exists photo_link text constraint food_items_photo_link_check check (photo_link is null or (photo_link ~ '^https://' and length(photo_link) <= 600));

-- The shop's orders: one per checkout. What was asked for at checkout (how
-- it goes out, where, when) and the money chosen, with the total as it was.
create table if not exists shop_orders (
  id           serial primary key,
  buyer        integer not null references users(id) on delete cascade,
  mode         text not null constraint shop_orders_mode_check check (mode in ('pickup')),
  address      text,
  pickup_time  text,
  table_no     integer constraint shop_orders_table_no_check check (table_no is null or table_no between 1 and 999),
  note         text,
  total        numeric(12, 2) not null constraint shop_orders_total_check check (total >= 0),
  pay_method   text not null default 'cash' constraint shop_orders_pay_method_check check (pay_method in ('upi', 'cash')),
  status       text not null default 'placed' constraint shop_orders_status_check check (status in ('placed', 'pending', 'completed', 'cancelled')),
  phone        text,
  ask_contact_number text,
  created_at   timestamp not null default now()
);
-- An app built before these columns keeps its table: they are added, never assumed (a saved preview
-- or a live database would crash on a column it does not have).
alter table shop_orders add column if not exists ask_contact_number text;

-- Each line copies the name and price as they were at checkout, so a menu edit
-- tomorrow never rewrites yesterday's bill.
create table if not exists shop_order_lines (
  id        serial primary key,
  order_id  integer not null references shop_orders(id) on delete cascade,
  item_id   integer references food_items(id) on delete set null,
  name      text not null,
  price     numeric(12, 2) not null,
  qty       integer not null constraint shop_order_lines_qty_check check (qty between 1 and 99)
);

-- One payment per thing being paid for. The columns split deliberately down
-- the middle: what the CUSTOMER says happened, and what the OWNER confirmed
-- after looking at their own bank app. Never collapse them into one "paid"
-- flag: a claim is not a confirmation, and the whole point of this table is
-- that the app can tell the two apart.
create table if not exists payments (
  id          serial primary key,
  -- Which row is being paid for. 'order' here, 'booking' or 'entry' in the
  -- other starters. There is deliberately no FOREIGN KEY: one table serves
  -- three different parent shapes, and that is the price of the reuse.
  target_kind text not null,
  target_id   integer not null,
  amount      numeric(10,2) not null constraint payments_amount_check check (amount > 0),
  status      text not null default 'awaiting'
              constraint payments_status_check check (status in ('awaiting', 'claimed', 'verified', 'rejected')),

  -- What the customer typed. Never trusted: a UPI reference is 12 digits and
  -- anyone can type 12 digits. The CHECK is shape, not proof.
  claimed_ref text constraint payments_claimed_ref_check check (claimed_ref ~ '^[0-9]{12}$'),
  claimed_by  integer references users(id) on delete set null,
  claimed_at  timestamp,

  -- What the owner confirmed, by eye, against their own UPI app.
  verified_by integer references users(id) on delete set null,
  verified_at timestamp,

  note        text not null default '',
  -- One payment row per thing. This is what makes the insert idempotent, so
  -- opening the pay panel twice cannot create two rows.
  unique (target_kind, target_id)
);

-- Notices: what an action told someone, shown under "Notices" until they open it.
create table if not exists dj_notices (
  id          serial primary key,
  user_id     integer not null references users(id) on delete cascade,
  text        text not null,
  seen        boolean not null default false,
  created_at  timestamp not null default now()
);


-- Security: lock every table against Supabase's public REST API.
-- Supabase publishes every table in the public schema on its anon
-- REST endpoint. Without this, anyone with your project's public key
-- could read and change your data (passwords, orders, everything).
-- Your app connects as the owner and is unaffected; only the public
-- API is shut out. DoneJi runs this for you; it is kept here so the
-- lock stays in place if you ever link this repo in Supabase yourself.
do $$
declare
  t record;
  has_anon boolean := exists (select 1 from pg_roles where rolname = 'anon');
  has_auth boolean := exists (select 1 from pg_roles where rolname = 'authenticated');
begin
  for t in
    select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table public.%I enable row level security', t.tablename);
    if has_anon then
      execute format('revoke all on public.%I from anon', t.tablename);
    end if;
    if has_auth then
      execute format('revoke all on public.%I from authenticated', t.tablename);
    end if;
  end loop;
end
$$;
