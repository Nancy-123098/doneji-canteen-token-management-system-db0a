# Canteen Token Management System

Canteen Token Management System keeps all the data the student listed: student ID, name and contact number (sign-up fields), food item name and price (menu), order details with token number, payment amount and status, and order date/time with order status pending or completed (the orders the shop builds). Students order food and pay to get a token; staff see every order, mark it paid and move it from pending to completed, and the shop's sales page counts orders and rupees so queues stay short.

Built by its owner on [DoneJi](https://doneji.app). The owner read the code and passed the X-ray: explained it, found the lines that matter, and changed it by hand before it could ship.

## Run it locally

1. pip install -r requirements.txt
2. python app.py: it creates the tables and the sample rows on the first run.
3. Sign up first through "I run this as the canteen staff": that account runs the app.

## Deployment

- **App**: Vercel, auto-deploys on every push to `main`
- **Database**: Supabase; the schema lives in `supabase/migrations/` and applies automatically on push
