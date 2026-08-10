# Inventory Management System

Inventory and warehouse management for Indian multi-warehouse operations — now becoming
a **multi-tenant web platform**: one website, many client companies, each with fully
isolated data behind their own logins.

See [SPEC.md](SPEC.md) for the feature specification and [SECURITY.md](SECURITY.md) for
the security posture.

## Web application (current direction)

```powershell
.\.venv\Scripts\python.exe run_web.py        # http://127.0.0.1:8000
```

- **Multi-tenant by schema**: each client company lives in its own Postgres schema with
  the full 41-table model — own stock, own invoice numbering, own GST settings. A
  request sets `search_path` to its tenant before any business query, so isolation is
  structural, not per-query discipline. Proven by test: two live tenants, one login
  page, zero data bleed.
- **Provisioning** is operator-only: `manage_platform.py create-tenant acme "Acme
  Traders"` (or the /admin panel when ADMIN_DB_* env vars are set on the server). The
  app role cannot create schemas at all.
- **Sessions** are signed httponly cookies with a sliding idle window; CSRF tokens ride
  inside the signed payload; the same PBKDF2/lockout/forced-change auth as the desktop.
- **Responsive** — usable from phones and tablets; wide tables scroll inside their card.
- On SQLite the web app runs single-tenant for local development.

Web status: **W1 complete** (tenancy, auth, shell, Home, Inventory read view, admin
panel). W2 ports the transactional screens; W3 analytics, documents and deployment.

## Desktop application (being retired once the web is live)

---

## Running it

```powershell
.\.venv\Scripts\python.exe main.py
```

On a database with no users, a first-run wizard creates the Owner account and your first
warehouse. After that you get a normal sign-in screen.

## Database

Live on Supabase (project `rsoevmbycvvmnmqyztbt`, region `ap-southeast-2`,
PostgreSQL 17.6). The schema — 41 tables, 98 foreign keys, 176 indexes — is created and
seeded.

One switch in `.env` picks the backend:

```ini
DB_BACKEND=postgres   # the shared cloud database (current setting)
DB_BACKEND=sqlite     # a local file, for experimenting without touching live data
```

### Two Supabase specifics worth knowing

**Connect through the session pooler, not the direct host.**
`db.<ref>.supabase.co` resolves to an IPv6 address only. Networks without IPv6 — which
is most of them — cannot reach it at all. Use
`aws-0-ap-southeast-2.pooler.supabase.com:5432` with the username
`postgres.<project-ref>`. (Only the `aws-0` cluster serves this project; `aws-1` returns
"tenant not found".)

**Never pass `options` in the connection parameters.** Supabase's Supavisor pooler reads
the `options` startup parameter to work out which tenant you are. Putting anything else
there — a `search_path`, for instance — makes it fall back to a bare `postgres` user and
reject the login with a misleading *"password authentication failed"*. This is why
`SchemaPostgresqlDatabase` in [database/connection.py](database/connection.py) sets the
search path with a `SET` statement *after* connecting instead.

### Performance notes

Round trip to Sydney is roughly 140 ms, so query *count* dominates everything:

- Every list screen is capped at **1–2 queries per refresh** using joins and `prefetch`.
  An earlier N+1 version issued 301 queries to draw 100 inventory rows, which took 46
  seconds over this link. If you add a screen, count its queries before shipping it.
- `create_tables` runs only when the schema is genuinely absent. Re-checking 41 tables
  and 176 indexes on every launch added ~28 s to startup; a warm start is now ~1.8 s and
  5 queries.

## Row Level Security

RLS is enabled on all 41 tables by the project's automatic-RLS trigger. The application
connects as `postgres`, which holds `bypassrls`, so it is unaffected. **If you move the
app to a restricted role, that role needs `BYPASSRLS` or explicit policies** — otherwise
every screen silently reads zero rows.

## Layout

```
main.py              Entry point: logging, DB startup, sign-in loop
config.py            Settings from .env, with PyInstaller-aware paths
SPEC.md              Feature specification and locked decisions

database/
  connection.py      SQLite/PostgreSQL selection behind a peewee proxy
  models.py          The complete schema

services/            Business logic — no Qt imports, so it stays testable
  auth.py            PBKDF2 password hashing, login, role permission matrix
  inventory.py       The stock engine: movements, moving-average cost, FEFO, reservations
  purchasing.py      Purchase orders, totals, reorder suggestions
  numbering.py       Document numbers (PO/25-26/0001)
  gst.py             State codes, GSTIN validation, CGST/SGST vs IGST split
  bootstrap.py       Schema creation and seed data

ui/
  main_window.py     Header, teal nav bar, screen stack
  login.py           Sign-in and first-run setup
  theme.py           Palette and Indian currency/quantity formatting
  styles.qss         Application stylesheet
  widgets/           Card, DataTable, SearchBox, form inputs
  dialogs/           Item, reorder, and the spec-driven record dialog
  screens/           Inventory, Settings, and phase placeholders
```

## Roles

| | Owner | Manager | Clerk |
|---|:--:|:--:|:--:|
| Stock movements, orders, receiving | ✔ | ✔ | ✔ |
| Items, partners, pricing, payments, analytics | ✔ | ✔ | — |
| See cost prices | ✔ | ✔ | — |
| Users, company settings, deletion | ✔ | — | — |

## Build status

**Phase 1 — Foundation: complete.** Schema, authentication and roles, master data
(items, suppliers, customers, warehouses, categories, units, employees, users, price
lists), and the Inventory screen with per-warehouse stock, reordering and export.

**Phase 2 — Transactions: complete.**

- *Receiving (Imports)* — purchase orders with multi-line editing, send to supplier,
  part receipt with batch numbers, expiry dates and serial capture. Every receipt
  reprices the item by moving average. Overdue orders are flagged.
- *Shipping (Exports)* — sales orders with the customer's price list applied
  automatically, stock reservation on confirm, part shipment with FEFO batch picking and
  serial capture, COGS frozen at ship time, GST invoice with the CGST/SGST vs IGST
  split, and part payments.
- *Inventory operations* — stock adjustments with an approval step, warehouse transfers
  with an in-transit state, and cycle counts that post their variances automatically.
- *Returns* — customer returns (restock or scrap) and supplier returns.

**Phase 3 — Intelligence and output: complete.**

- *Home* — reminders (manual plus automatic low-stock, overdue-PO and lot-expiry
  alerts that close themselves once resolved), the employee roster with assigned tasks
  and hours worked, badge clock-in, and highest-profit items.
- *Recent Logs* — the movement trail with warehouse, type and date filters, and a
  running balance after each movement.
- *Analytics* — the five headline figures, inventory and order trends, price trends,
  ranked exports, supplier delays, and an Excel summary export.
- *Documents* — GST tax invoice (IGST or CGST+SGST by state, HSN summary, amount in
  words), delivery challan, purchase order, goods receipt note and barcode labels.
- *Global search* — items, SKUs, barcodes, order numbers, truck numbers, customers,
  suppliers and employee badges, routed to the screen the record lives on.
- *Packaging* — `build_exe.py` produces a distributable folder.

No screen in the app is a placeholder.

## Security

See [SECURITY.md](SECURITY.md) for the full posture, the known limitations and what to do
if you suspect a breach. In short:

- The app connects as **`inventory_app`**, a least-privilege role that cannot change the
  schema, create roles, or alter the audit and stock-movement history. Rebuild or rotate
  it with `.venv\Scripts\python.exe security_setup.py`.
- **The `postgres` superuser password must never be deployed.** It is only for schema
  migrations and for re-running that script.
- TLS is **verify-full** against `certs/supabase-ca.crt`, so the server is authenticated
  and not merely encrypted.
- Roles are enforced in the service layer, not just by disabling buttons.

## Packaging

```powershell
.\.venv\Scripts\python.exe build_exe.py
```

Produces `dist/InventoryManagementSystem/`. Copy that folder to each warehouse PC with a
`.env` beside the executable. A one-folder build is deliberate: a single-file .exe
unpacks to a temp directory on every launch, which is slow on machines with aggressive
antivirus and occasionally trips a false positive.

## Tests

Eight suites in the session scratchpad, roughly 400 assertions: the stock engine and
costing, the Phase 1 UI, Phase 2 services and dialogs, analytics, the workforce and Home
screen, document generation, and global search. Each builds its own throwaway SQLite
database. `smoke1` and `smoke3` also run against PostgreSQL via `verify_pg.py`, which
creates and drops its own scratch schema so live data is never touched.

Two habits worth keeping:

- **Count queries when adding a screen.** Every list screen is held to 1–2 queries per
  refresh; the roster to 4. At ~140 ms per round trip a regression here is felt
  immediately.
- **Render the screen and look at it.** Three real bugs — overlapping cards, colourless
  stat tiles, a black form background — passed every assertion and were only visible in
  a screenshot.

## Notes

- Money and quantities use `DECIMAL`, never float.
- Every stock change goes through `services.inventory.apply_movement`, which is the only
  function that writes to `StockLevel`. That is what keeps the movement log and the
  on-hand numbers from ever disagreeing.
- Cost of goods sold is frozen onto the fulfilment line at ship time, so later price
  changes never rewrite historic profit.
