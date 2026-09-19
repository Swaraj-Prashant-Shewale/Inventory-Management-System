# Inventory Management System — Complete Technical Reference

*A single, in-depth explanation of everything in this project: what each part is, how it
works, and why it was built that way. Written for your own use as the person who owns and
runs it.*

---

## Table of contents

1. [What this is, in one page](#1-what-this-is-in-one-page)
2. [The big picture: architecture and why it's shaped this way](#2-the-big-picture)
3. [Configuration and the two backends](#3-configuration-and-the-two-backends)
4. [The database connection layer](#4-the-database-connection-layer)
5. [The data model — all 41 tables](#5-the-data-model)
6. [The inventory engine — the core of the whole system](#6-the-inventory-engine)
7. [Costing: moving average and frozen COGS](#7-costing)
8. [Lots, FEFO and serials](#8-lots-fefo-and-serials)
9. [Purchasing (buying in)](#9-purchasing)
10. [Sales (selling out)](#10-sales)
11. [Warehouse operations: adjustments, transfers, cycle counts](#11-warehouse-operations)
12. [GST and document numbering](#12-gst-and-document-numbering)
13. [Pricing](#13-pricing)
14. [Authentication and permissions](#14-authentication-and-permissions)
15. [Multi-tenancy: how many companies share one system safely](#15-multi-tenancy)
16. [The database security model (least-privilege role)](#16-the-database-security-model)
17. [The web application](#17-the-web-application)
18. [Intelligence: alerts, analytics, search](#18-intelligence)
19. [Documents (PDF generation)](#19-documents)
20. [Workforce: clocking, tasks, roster](#20-workforce)
21. [The desktop application (being retired)](#21-the-desktop-application)
22. [Correctness and concurrency guarantees](#22-correctness-and-concurrency-guarantees)
23. [Testing](#23-testing)
24. [Operations: setup, provisioning, deployment](#24-operations)
25. [Known limitations and roadmap](#25-known-limitations-and-roadmap)
26. [Glossary](#26-glossary)

---

## 1. What this is, in one page

This is inventory and warehouse management software for Indian SME businesses that run
one or more warehouses. It handles the full cycle: buying goods in (purchase orders and
goods receipts), selling goods out (sales orders, shipments, GST invoices, payments),
moving stock around (adjustments, warehouse transfers, cycle counts), and the intelligence
on top (automatic reminders, analytics, PDF documents, barcode search, staff clocking).

It exists in **two front ends over one shared brain**:

- A **desktop app** (PySide6/Qt, packaged as a Windows `.exe`) — the original product,
  now being retired.
- A **web app** (FastAPI + server-rendered HTML) — the current direction, built as a
  **multi-tenant platform**: one website, many separate client companies, each with fully
  isolated data behind their own logins.

Both front ends call the exact same `services/` (business logic) and `database/` (data
model) layer. That shared core is where all the real logic lives, and it never imports Qt
or FastAPI, which is what lets the same code drive a desktop screen, a web page, or a test.

The production database is **PostgreSQL on Supabase**; local development uses **SQLite**.
A single setting (`DB_BACKEND`) switches between them.

---

## 2. The big picture

**Layered design.** From the bottom up:

```
config.py            → reads .env, resolves settings and which backend to use
database/            → connection.py (engine + connection handling)
                       models.py    (the 41-table schema)
services/            → all business logic (no UI imports — stays testable)
  auth, tenancy, inventory, purchasing, sales, warehouse_ops,
  numbering, gst, pricing, alerts, analytics, documents, search,
  workforce, bootstrap
web/                 → FastAPI app: routes, request context, sessions, templates
ui/                  → PySide6 desktop app: main window, screens, dialogs, widgets
security_setup.py    → one-time creation of the restricted DB role
manage_platform.py   → operator CLI: create clients, admins, list, drop
run_web.py / main.py → launchers for the web and desktop apps
```

**Why this separation matters.** The `services/` and `database/` layers have no knowledge
of *how* they're being displayed. That is why the desktop app can be retired and replaced
by the web app without rewriting a single business rule — the rules were never in the UI.
It is also why the test suites can exercise the whole system headlessly.

**Two design themes run through everything:**

1. *The database is the source of truth, and history is immutable.* Every quantity change
   is an append-only ledger row; on-hand numbers are a cache that is always reconciled to
   the ledger; cost of goods sold is frozen at the moment of shipment. This is what makes
   the accounting trustworthy over time.
2. *Correctness under concurrency and least privilege.* Multiple warehouse PCs (or web
   requests) hit the same rows at once, so every stock write takes an explicit row lock;
   and the app runs as a restricted database role that physically cannot rewrite history
   or escape its own schema.

---

## 3. Configuration and the two backends

`config.py` loads settings from a `.env` file that sits next to the app. It uses
`app_root()` to find that file — under a PyInstaller build (`sys.frozen`) it returns the
`.exe`'s own directory, so the writable `.env`, certificate and generated files live
*outside* the read-only bundle.

The master switch is **`DB_BACKEND`**: `sqlite` (a local file, for experimenting without
touching live data) or `postgres` (the shared cloud database). Everything else keys off
that.

Key settings:

- **Postgres connection** — `DB_NAME/USER/PASSWORD/HOST/PORT`. The app connects through
  Supabase's *session pooler* (`aws-0-<region>.pooler.supabase.com:5432`), with the
  username in dotted form (`inventory_app.<project-ref>`).
- **TLS** — `DB_SSLMODE` and `DB_SSLROOTCERT`. If the CA file exists and the mode is
  `require`/`prefer`/`allow`, config **auto-upgrades** it to `verify-full` (which actually
  authenticates the server, not just encrypts). Without the CA it stays encrypted but
  unverified, and logs a warning.
- **`PG_CONFIGURED`** is `True` only when `DB_HOST` is set *and* doesn't still contain the
  placeholder `YOUR_PROJECT_REF` — so an untouched sample `.env` is treated as "not set up
  yet" rather than trying to connect to a fake host.
- **`PBKDF2_ITERATIONS`** (default 600,000, the OWASP floor) and **`SESSION_IDLE_MINUTES`**
  (default 20) tune auth. Because each stored password hash records its own iteration
  count, raising this number transparently upgrades old hashes the next time each user
  logs in.
- **Business defaults** — currency symbol (₹), financial-year start month (4 = April),
  default GST rate (18%).

---

## 4. The database connection layer

`database/connection.py` is small but does three important jobs.

**The proxy pattern.** Models bind at import time to a peewee `DatabaseProxy` called `db`,
an indirection that lets the model classes load without yet knowing which engine they'll
use. At startup `configure_database()` calls `db.initialize(real_db)` to resolve it. This
is what makes the single-line backend switch possible.

**`SchemaPostgresqlDatabase`** is the Postgres engine, combining two behaviours:

- **Reconnect handling** (via peewee's `ReconnectMixin`). Warehouse PCs reach Sydney over
  the public internet, so the link *will* drop — a dozing laptop, a flapping router, a
  pooler restart. The mixin transparently retries once after re-establishing the
  connection. Its `reconnect_errors` list holds the exact psycopg2 wordings that count as
  a dead link (e.g. "server closed the connection", the DNS "could not translate host
  name"). Critically, it only retries **outside a transaction** — replaying half a
  transaction would be worse than an error.
- **search_path management** (the multi-tenancy mechanism). The schema is set by running
  `SET search_path TO "<schema>"` *after* connecting — deliberately **not** through the
  libpq `options` startup parameter, because Supabase's Supavisor pooler reads `options`
  to work out which tenant you are, and anything else there makes it reject the login with
  a misleading "password authentication failed". The active schema is stored in
  `threading.local()` so each web-server thread serves its own tenant, and
  `set_active_schema()` records the choice per thread so a **reconnect restores this
  thread's tenant**, not a global default. (This last point was a real bug that was found
  and fixed — see §22.)

On SQLite, `build_database()` instead returns a `SqliteDatabase` with pragmas: WAL mode so
readers don't block writers, foreign keys enforced, and tuned cache/synchronous settings.

---

## 5. The data model

`database/models.py` defines exactly **41 tables** (listed in `ALL_MODELS` in
creation-dependency order). Every table inherits from `BaseModel` (integer primary key,
bound to the `db` proxy, snake-cased table names); tables that track lifecycle inherit from
`TimestampedModel`, whose `save()` refreshes `updated_at` on every write.

### Custom field types — and why precision matters

Money and quantities are **always `DECIMAL`, never float** — the module says plainly that
"rounding errors in a ledger are bugs." Four semantic wrappers exist:

| Field | Type | Used for | Why |
|---|---|---|---|
| `MoneyField` | `DECIMAL(16,2)` | prices, invoice totals | currency = 2 decimals |
| `CostField` | `DECIMAL(18,6)` | moving-average cost, frozen COGS | **6 decimals** so a weighted-average recompute doesn't drift; rounding to the cent between receipts would compound valuation errors |
| `QtyField` | `DECIMAL(16,3)` | quantities | 3 decimals so kg/litre items work |
| `RateField` | `DECIMAL(6,2)` | GST %, discount % | percentages |

A subtle detail: nullable decimal columns get **no** zero default, so that NULL ("no
override set") stays distinct from 0 ("the override is explicitly zero") — important for
per-warehouse min/max levels.

### The enumerations

Stored as strings, each usually with a `LABELS` map for the UI:

- **`Role`** — OWNER / MANAGER / CLERK.
- **`Direction`** — IN / OUT (the sign carrier for every stock movement and time entry).
- **`DocType`** — the source document of a stock movement: GRN, FULFILLMENT, ADJUSTMENT,
  TRANSFER_OUT, TRANSFER_IN, CYCLE_COUNT, CUSTOMER_RETURN, SUPPLIER_RETURN, OPENING.
- **`PurchaseStatus`** / **`SalesStatus`** — Draft → Ordered/Confirmed → Partial →
  Received/Shipped → Cancelled; each exposes an `OPEN` tuple used by the overdue checks.
- **`PaymentStatus`** — Unpaid → Partial → Paid.
- **`TransferStatus`** — Draft → In Transit → Received → Cancelled.
- **`CountStatus`** — Open → Counted → Approved → Cancelled.
- **`TaskStatus`** — Pending / In Progress / Done / Cancelled.
- **`ReminderSource`** — Manual, Low Stock, PO Overdue, Lot Expiry.
- **`SerialStatus`** — In Stock / Shipped / Returned / Scrapped.
- **`AdjustmentReason`** — Damage / Loss / Theft / Found / Expiry / Count Variance / Other.

### The tables, by cluster

**Company & configuration.** `CompanySettings` (single row — the GST invoice header: legal
name, GSTIN, PAN, address, state code, bank details, FY start). `NumberSequence` (per-doc
running counters, with `last_fy` for the financial-year reset).

**People.** `Warehouse` (code, name, and the `state_code` that drives the GST split),
`Employee` (badge code, home warehouse), `User` (login credentials, separate from
Employee: `password_hash`, `role`, and the lockout fields `failed_attempts` /
`locked_until` / `must_change_password`).

**Master data.** `Category` (self-referential tree), `Uom` (`allow_decimal` blocks half a
carton on discrete units), `Supplier` (`lead_time_days`, `payment_terms_days`), `Customer`
(price list, credit limit), `PriceList` / `PriceListItem` (customer pricing tiers with
quantity breaks), and `Item` — the catalogue core (SKU, barcode, category, base vs purchase
UoM with pack size, HSN/GST rate, min/max levels, and the valuation fields `avg_cost` /
`last_purchase_cost` / `selling_price`, plus lot/serial/shelf-life flags).

**Inventory.** `StockLevel` (one row per item×warehouse — on hand, reserved, per-warehouse
min/max), `Lot` (batch + expiry), `LotStock` (a lot's quantity in a warehouse), `Serial`
(one physical unit).

**Purchasing.** `PurchaseOrder`/`PurchaseOrderLine`, `GoodsReceipt`/`GoodsReceiptLine`,
`SupplierReturn`/`SupplierReturnLine`.

**Sales.** `SalesOrder`/`SalesOrderLine`, `Fulfillment`/`FulfillmentLine` (the latter holds
the **frozen COGS**), `Invoice` (with the CGST/SGST/IGST split), `Payment`,
`CustomerReturn`/`CustomerReturnLine`.

**Warehouse ops.** `StockAdjustment`/`Line`, `StockTransfer`/`Line`, `CycleCount`/`Line`.

**Activity & audit.** `StockMovement` (the immutable ledger of every quantity change),
`TimeEntry` (clock in/out), `Task`, `Reminder`, and `AuditLog` (who did what, keeping the
username as text even if the user is later deleted).

**Registry (Postgres only, in the `public` schema).** `Tenant`, `UserDirectory`,
`PlatformAdmin` — the multi-tenant control tables (see §15).

---

## 6. The inventory engine

`services/inventory.py` is the beating heart of the system. Everything about stock funnels
through it, and it is deliberately built around one idea: **there is one and only one place
that writes stock.**

### The ledger and the cache

Stock exists in two complementary forms:

- **`StockMovement`** is an **append-only ledger** — one immutable row per quantity change,
  recording the timestamp, item, warehouse, direction, a always-positive quantity, the
  `balance_after`, the cost in force at that moment, and full provenance (which document,
  which user, which lot). Nothing ever updates or deletes these rows. This is the audit
  trail and it powers the "Recent Logs" screen.
- **`StockLevel`** is the **running cache** — one row per item×warehouse holding `on_hand`
  and `reserved`. Reading a current balance is an instant lookup instead of summing the
  whole ledger.

They are kept honest by the `balance_after` field: every time on-hand changes, that new
figure is stamped onto the ledger row. So replaying the ledger must reproduce the cache,
and any divergence is detectable. The consistency is **structural**: `apply_movement()` is
the *only* function allowed to touch `StockLevel`, and it writes the level and the ledger
row inside a single transaction — they commit together or not at all.

### `apply_movement()` — the single write path

Quantity is always positive; `direction` (IN/OUT) carries the sign. Inside a transaction it
locks the level row, reads the current on-hand, computes the new balance, refuses to go
negative on an OUT (unless explicitly allowed), saves, optionally syncs the lot, and writes
the movement. Because it is the sole writer, the movement log and the on-hand numbers can
never disagree.

---

## 7. Costing

### Moving-average cost

On every goods receipt, the item's cost basis is re-blended:

```
new_avg = (existing_qty × old_avg + received_qty × receipt_cost) / (existing_qty + received_qty)
```

`existing_qty` is the on-hand across **all** warehouses, because average cost is a property
of the *item*, not a location. This runs inside a transaction with the `Item` row locked
`FOR UPDATE`, so two simultaneous receipts of the same item can't each blend only their own
line and clobber each other. The result is quantized to 4 guard digits (the field stores 6)
so that the basis, which feeds the *next* receipt's blend, doesn't accumulate rounding
drift.

Timing is load-bearing: on receiving, the moving average is updated **before** the stock
movement is posted, so the incoming cost blends against the on-hand *before* this delivery
lands. Doing it after would blend against an already-inflated quantity and corrupt the
average.

### Frozen COGS — why profit never changes retroactively

When goods ship, the cost of goods sold is captured onto the `FulfillmentLine` at that
moment (`unit_cost = item.avg_cost` *now*) and **never recalculated**. This is the single
most important accounting decision in the system: because COGS is frozen at ship time, a
purchase next month that moves the average cost **cannot rewrite last quarter's reported
profit**. Gross profit is simply Σ(line revenue) − Σ(line frozen cost) over shipped orders.

---

## 8. Lots, FEFO and serials

For lot-tracked items, a `Lot` is a batch (with expiry, supplier, cost) and `LotStock`
holds a batch's quantity in one warehouse. Whenever a movement carries a lot,
`_apply_lot_movement` keeps per-lot stock in lockstep with the aggregate stock level (also
under a row lock, and refusing to drive a lot negative).

**FEFO = First-Expiry-First-Out.** `available_lots` orders batches by expiry date ascending
(undated lots last); `allocate_fefo` walks that list taking what it needs from each,
returning a list of `(lot, quantity)` pairs, or raising `InsufficientStock` reporting how
much the lots could actually cover. This is used everywhere goods leave without a specific
lot named — sales fulfilment, supplier returns, transfers.

A recurring subtlety the code guards against: if you posted a single "no lot" OUT movement
for a lot-tracked item, on-hand would drop but `LotStock` wouldn't, leaving lot totals
permanently above on-hand. So every one of those flows splits across lots and posts one
movement per lot. (This was one of the bugs found and fixed — see §22.)

`Serial` tracks individual physical units (in stock / shipped / returned / scrapped) for
items flagged as serialised.

---

## 9. Purchasing

`services/purchasing.py` runs the buying-in cycle: **Draft → Ordered → Partially Received →
Received** (or Cancelled).

- **`create_purchase_order`** drops zero-quantity lines, derives the ETA from the
  supplier's lead time if not given, and defaults each line's cost and GST/HSN from the
  item master, all inside one transaction. `recalculate_totals` computes line totals and
  the GST split, deciding interstate-vs-intrastate once for the whole order.
- **`confirm_order`** is the "send to supplier" step (Draft → Ordered).
- **`receive_goods`** produces a **Goods Receipt Note** and is the heart of the module. It
  re-validates against a freshly **locked** order line (`SELECT … FOR UPDATE`) so two PCs
  can't both receive against a stale copy, and refuses over-receipt (almost always a typo).
  For each line, in a deliberate order: materialise the lot → **update moving average** →
  post the IN movement → increment received quantity, carrying any real delivered cost back
  onto the line. Status is then *derived* from physical reality: all lines fulfilled →
  Received; some → Partial. Short deliveries are fine and just leave the order open.
- **Supplier returns** push stock out; for lot-tracked items with no lot named, they split
  the return across lots FEFO (one return line per lot) to keep `LotStock` honest.
- Supporting queries (`overdue_orders`, `on_order_quantity`) are written as single joined
  queries to avoid N+1 round trips over the remote link.

---

## 10. Sales

`services/sales.py` runs the selling-out cycle: **Draft → Confirmed → Partially Shipped →
Shipped**, with a parallel payment status (Unpaid → Partial → Paid).

- **`create_sales_order`** resolves each price through the customer's price list (§13) and
  applies per-line discounts before the GST split. `shortfalls` reports lines you can't
  currently cover but never blocks the order — taking an order for not-yet-stocked goods is
  normal.
- **`confirm_order`** **reserves** stock (making it unavailable to other orders without
  physically moving it). Reservation is allowed to exceed current stock, because you may
  purchase to cover a confirmed order.
- **`fulfill`** produces a **shipment** and is where accounting hinges. It re-validates
  against a locked line to prevent double-shipping, freezes COGS onto each fulfilment line
  (§7), ships lot-tracked items FEFO (one movement per lot), releases the matching
  reservation, and increments shipped quantity. Status is derived from what's outstanding.
- **`create_invoice`** raises the GST invoice from the already-frozen line totals (not by
  re-pricing), totals the CGST/SGST/IGST, and snaps the grand total to the nearest rupee
  storing the round-off adjustment.
- **`record_payment`** appends payments (partials allowed, overpayment blocked by default)
  and refreshes payment status. **`create_customer_return`** optionally restocks or scraps.

Every state change writes an audit entry.

---

## 11. Warehouse operations

`services/warehouse_ops.py` — all three flows are deliberately **two-step** (create, then
approve/receive) so a mistake is caught before stock moves and a name is attached to every
variance.

- **Adjustments** — `create_adjustment` records signed deltas but moves nothing;
  `approve_adjustment` (permission-gated, idempotent) posts one movement per line.
- **Transfers** — three states (Draft → In Transit → Received). `dispatch_transfer` sends
  goods out (FEFO, one TRANSFER_OUT movement per lot on a multi-lot split). `receive_transfer`
  then **replays** those TRANSFER_OUT movements to credit the destination with exactly the
  lots that travelled — without this replay, a partial FEFO-split transfer would drift the
  destination's lot totals. Stock in transit is invisible to both warehouses.
- **Cycle counts** — `open_cycle_count` snapshots expected quantities; `record_counts`
  (permission-gated, never accepted from an unknown user, since these numbers become a
  stock adjustment); `approve_cycle_count` routes the variances straight through the
  audited adjustment machinery.

---

## 12. GST and document numbering

**GST** (`services/gst.py`). India charges tax in two shapes depending on whether a sale
crosses a state line, and this module encodes exactly that. `is_interstate(supply_state,
party_state)` compares the two two-digit state codes; different codes → interstate. When a
code is unknown it **defaults to intrastate** (the safer choice — it keeps the CGST/SGST
split visible rather than silently charging IGST). `split_tax(taxable, rate, interstate)`
then either halves the tax into CGST + SGST (intrastate) or charges the whole rate as IGST
(interstate), giving any rounding remainder to CGST so the halves always re-sum exactly.
`round_off` snaps a total to the nearest rupee. GSTIN validation is structural (pattern +
state-code existence), explicitly not a government lookup.

**Numbering** (`services/numbering.py`). Document numbers look like `SO/26-27/0001`:
prefix / financial-year / zero-padded counter. `next_number` reserves one inside a
transaction with the sequence row locked, so two simultaneous "Create" clicks can't
collide. The Indian financial year runs April–March, so a date in, say, February 2027
belongs to FY `26-27`. The key feature is the **per-FY reset**: when the year rolls over,
the counter restarts at 0001 and the new year is recorded (`last_fy`), so each financial
year forms its own clean series — exactly what accountants expect. `peek_number` shows what
*would* be issued without consuming it.

---

## 13. Pricing

`services/pricing.py`. `resolve_price(item, customer, quantity)` determines what a specific
customer pays. It starts from the item's selling price; if the customer has a price list it
honours **quantity breaks** (picking the tier whose minimum-quantity threshold is the
highest one the order reaches), an explicit tier price winning outright, otherwise the
list's blanket discount, otherwise base price. `price_explanation` returns a human-readable
reason ("Acme price from 50 units", "10% off list") for the UI tooltip.

---

## 14. Authentication and permissions

`services/auth.py`.

**Password hashing.** PBKDF2-HMAC-SHA256 from the Python standard library — chosen over
bcrypt/argon2 specifically because it needs no compiled dependency, keeping the packaged
build simple. Each password gets a fresh 16-byte random salt and 600,000 iterations. The
stored value is self-describing (`pbkdf2_sha256$iterations$salt$hash`), which makes the
work factor **upgradable**: on a successful login, if the hash was made with fewer
iterations than currently required, it's transparently re-hashed and saved. Verification
uses a constant-time comparison so timing can't leak how much of the digest matched.

**Anti-enumeration.** When a username doesn't exist, login does **not** return instantly —
that fast path would take microseconds while a real check spends 600k PBKDF2 rounds, and an
attacker timing the response could map which usernames exist. Instead it runs
`dummy_verify()` against a throwaway hash so both paths cost the same, and returns the
identical "Incorrect username or password" message either way.

**Lockout and audit.** Five wrong passwords lock the account for 10 minutes (a locked
account is refused even with the correct password). Every failure writes an audit row (with
the username but never the attempted password) — a burst of these is the signature of a
brute-force attempt.

**Permissions.** Authorisation is a capability matrix, not scattered role checks. Twenty
`PERM_*` constants each name one gated action. Three sets compose by union: Clerk (floor
operations) ⊂ Manager (+ analytics, cost visibility, master-data and pricing management,
payments, approvals) ⊂ Owner (+ user management, settings, deletion). `require(user, perm,
action)` is called at the top of every business write and **refuses a missing user
outright** — an unattributed write is exactly what must never happen. Authorisation never
depends on a button being greyed out.

---

## 15. Multi-tenancy

This is the core of the web platform, and the part most worth understanding deeply.

**The design: one schema per client.** Each client company lives in its own PostgreSQL
**schema** named `t_<slug>`, containing the full 41-table model — their own items, orders,
invoice numbering, GST settings. The shared `public` schema holds only the **registry**:

- `Tenant` — which companies exist (slug, schema name, active flag).
- `UserDirectory` — a platform-wide `username → tenant` map with a UNIQUE constraint, which
  is what lets one login page serve every client unambiguously.
- `PlatformAdmin` — operator (your) accounts for the `/admin` panel, entirely separate from
  any client's users.

These three are pinned to the `public` schema so they resolve no matter which tenant is
currently active.

**Why isolation is structural, not per-query.** The naive approach — one big shared table
with a `tenant_id` column and `WHERE tenant_id = ?` on every query — is one forgotten
clause away from a cross-company data leak. This system instead sets the connection's
`search_path` to the client's schema at the **start of every request**, and writes business
queries with **no tenant qualifier at all**. Because a client's tables physically live in
their own namespace, a query running in company A's search_path simply *cannot resolve*
company B's rows. The isolation is enforced by PostgreSQL's own name resolution — there is
no `tenant_id` to forget. That is why schema-per-tenant was chosen: cross-tenant leakage is
**structurally impossible** rather than dependent on never making a mistake.

**Provisioning** (`provision_tenant`) runs entirely on administrator credentials — the app
role can't create schemas, which is the whole point. It first clears any orphan schema from
a prior failed run, then: creates the schema, builds all tables + seed data + first
warehouse + first Owner (with `must_change_password=True`) in one transaction, grants the
app role its runtime rights, and inserts the registry rows **last**. If anything fails, a
compensation block deletes the tenant row and drops the schema, so you never end up with a
half-built client. `drop_tenant` is the deliberate inverse and makes you repeat the slug to
confirm.

**This isolation is proven, not assumed.** A live adversarial test (see §23) provisions two
companies that deliberately share the same SKU and the same internal user-id, then verifies:
neither can see the other's data in either direction; a tampered cookie is rejected; a
correctly-signed cookie pairing one company's user-id with the other company resolves
strictly inside the other company; and the operator panel exposes no client stock.

---

## 16. The database security model

`security_setup.py` creates the runtime role, **`inventory_app`**, and is run once as the
Supabase `postgres` superuser.

- The role is created `NOSUPERUSER NOCREATEDB NOCREATEROLE` and granted `CONNECT`, schema
  `USAGE`, and full `SELECT/INSERT/UPDATE/DELETE` on tables — but `CREATE ON SCHEMA` is
  **revoked**, so it can add no objects (no DDL). Since it owns nothing, ALTER/DROP are
  already impossible.
- **The append-only ledgers are frozen at the grant level.** `UPDATE` *and* `DELETE` are
  revoked on both `audit_log` and `stock_movement`. DELETE is revoked too, not just UPDATE
  — otherwise delete-and-reinsert would rewrite history and the guarantee would be a lie.
  Narrow column-level `UPDATE` is granted back only on nullable foreign keys (so
  `ON DELETE SET NULL` still works when a referenced record is removed); everything else —
  quantity, balance, cost, timestamp, action — stays frozen. Default privileges on *future*
  tables are limited to `SELECT, INSERT` so a migration that recreates a ledger can't
  silently restore full write access.
- **`verify()` reconnects as `inventory_app` itself** and probes each restriction — reads
  must succeed; creating tables, deleting/rewriting audit or movement rows, and creating
  roles must all fail — printing the credentials only if every restriction holds.
- The same shape is applied to each tenant schema by `grant_app_access`.

**The credential model.** Two credentials with strictly separate lives: the **`postgres`
superuser** (setup, provisioning, migrations, backups only — typed at a prompt, never in a
deployed `.env`, never on a client machine) and **`inventory_app`** (the runtime role,
whose password goes in the app's `.env`). This is blast-radius containment: a stolen `.env`
yields only what `inventory_app` permits — read/write of business data, but no DDL, no role
creation, no schema escape, and no ability to rewrite the frozen ledgers. That turns an
otherwise-invisible tampering into a visible, auditable one.

`inventory_app` holds `BYPASSRLS` deliberately: row-level security is enabled on all 41
tables, but the *grants* are the real boundary; RLS is a backstop that stops the PostgREST
`anon`/`authenticated` roles reading anything if Supabase's Data API is ever switched on.
(So: don't remove BYPASSRLS without adding explicit policies, or every screen would read
zero rows.)

---

## 17. The web application

`web/` is a thin, server-rendered FastAPI layer over the shared core. It holds no client
state beyond a signed cookie.

**App construction** (`web/app.py`). `create_app()` builds the FastAPI app with the
interactive docs and OpenAPI schema **switched off** (nothing advertises the route surface
to a visitor), a catch-all exception handler that logs the full traceback server-side but
returns a **generic 500 page** (the browser never sees SQL, schema names or stack traces),
and a middleware that sets hardening headers on every response: `nosniff`, `X-Frame-Options:
DENY` plus CSP `frame-ancestors 'none'` (double clickjacking defence), and a tight
`Content-Security-Policy` confining scripts/styles/images/forms to same-origin.

**The per-request lifecycle** (`web/context.py`) — the most important sequence in the web
layer. Every route calls `resolve_user(request)`, which never raises and does this in a
strict order:

1. Ensure the (reused, kept-warm) database connection is open.
2. Decode the signed session cookie into bare identifiers.
3. Look up which tenant this session belongs to — **schema-qualified**, so it's correct no
   matter what search_path the pooled connection currently carries.
4. **Only now** set the search_path to that tenant's schema, then load the user from within
   it.

The ordering is the whole game: pooled connections remember their search_path from a
*previous* request that may have served a *different* client, so tenant selection (immune to
the current path) must strictly precede any business query (governed by the path). Getting
this wrong would be a cross-tenant leak — which is exactly why the search_path is
re-asserted on every request. `resolve_admin` is the parallel path for the operator panel,
reading a *different* cookie and loading a `PlatformAdmin`, never a tenant user.

**Sessions and CSRF** (`web/security.py`). Sessions are `itsdangerous` signed, timestamped
cookies carrying **only identifiers** (kind, ids, a per-session CSRF token) — nothing
sensitive is client-side and tampering breaks the signature. The `kind` field ("user" vs
"admin") is enforced on read, so a client cookie can never be replayed as an operator
session or vice-versa. The idle timeout is the cookie's max-age, refreshed on every
response — a sliding window. CSRF is layered: authenticated forms carry the per-session
token (compared constant-time); **login itself** uses a pre-session double-submit handshake
(a signed `SameSite=Lax` cookie whose value must match a hidden field), because a forced
cross-site login is a real threat and a cross-site POST can't carry the matching cookie.
Cookies are always `HttpOnly` + `SameSite=Lax`, and `Secure` by default on the Postgres
(production) backend so a forgotten env var can't ship auth cookies in cleartext.

**Login hardening.** Both the tenant sign-in and the operator `/admin` sign-in are rate
limited per client IP (an in-process sliding window, `web/ratelimit.py`): once an IP is
over its limit the server answers `429` *before* doing any PBKDF2 work, which stops both
password spraying and the CPU-exhaustion an unmetered ~0.5s-per-attempt login invites; a
successful login clears the counter so a shared office IP is never penalised. The operator
panel additionally supports **two-factor authentication** — a platform admin can enable
TOTP (RFC 6238, implemented in `services/totp.py` with only the standard library) via
`manage_platform.py admin-2fa`, after which `/admin` requires the 6-digit code as well as
the password, with wrong codes counting toward the lockout so the code space can't be
brute-forced.

**The routes today.** Login (with the anti-enumeration dummy verify and the forced
password-change redirect), logout, account password change; the operator `/admin` panel
(which imports **no business models at all**, so it structurally cannot query client
inventory); and the signed-in operational screens: Home, Check In / Out, Inventory with
item create/edit, filterable Recent Logs, Settings for warehouses, employees, users,
roles, access status, and password resets, plus purchase-order/receipt and sales-order/
shipment lifecycles with supplier and customer creation.

**Mobile-adaptive by design.** The whole thing is server-rendered HTML with responsive CSS
(no client JavaScript framework), so it works on a phone or tablet and stays consistent with
the strict CSP.

---

## 18. Intelligence

**Alerts** (`services/alerts.py`). Generates the reminders nobody types by hand — low
stock, overdue POs, expiring lots — into the same `Reminder` table as manual notes,
distinguished by `source`. The clever part is `_upsert`: it keys each generated reminder to
the record that produced it, keeps one open reminder per problem and marks duplicates done
(a **self-healing dedupe**), and `_clear_resolved` ticks off reminders whose underlying
problem has gone away. So re-running the generator *converges* the reminder set to current
reality instead of piling up. Each generator is wrapped so that an alert failure can never
stop the app from opening. On the web, this refresh is throttled to once per company per
120 seconds because it scans stock, POs and lots.

**Analytics** (`services/analytics.py`). Every sales figure reads from the **fulfilment
lines**, never order headers, because revenue books when goods ship. `headline` computes net
sales, COGS (from the *frozen* line cost, so history never shifts), gross profit, margin and
counts — all in a **single aggregate query**, scoped by date and warehouse. Query *count* is
the binding cost against the remote database, so single-query discipline recurs throughout.

**Search** (`services/search.py`). `scanned_item` treats input as a barcode/SKU scan for an
exact jump; `search` runs LIKE queries across items, orders, customers, suppliers and
employees, sorting exact matches first, then by a fixed type precedence — so a scanned
barcode beats a document number beats a partial name.

---

## 19. Documents

`services/documents.py` builds PDFs with ReportLab from the record plus `CompanySettings`,
so a document can never disagree with the database (and prints a "NOT A VALID TAX INVOICE"
banner if the company's legal details are incomplete). The **GST tax invoice** carries both
parties' GSTIN/state, HSN per line, the CGST/SGST-vs-IGST split decided by
`invoice.is_interstate`, totals, an **amount-in-words** line using Indian crore/lakh
grouping, and an HSN summary. The same helpers produce the delivery challan (quantities and
batches, no prices — it travels with the goods), purchase order, goods receipt note (ordered
vs received vs rejected), and Code128 **barcode labels**.

---

## 20. Workforce

`services/workforce.py`. Clocking is a **badge-scan toggle**: `clock_scan` finds the
employee by badge, reads whether they're currently in, and writes the opposite entry. It is
deliberately **not** permission-gated (it's a physical badge at a terminal), whereas
`correct_entry` (a manual timestamp that moves payroll hours) *is* gated. Hours are
**derived, not stored** — `hours_worked` sums paired IN/OUT intervals, and an unclosed shift
accrues only up to *now* (so an open shift doesn't read as 15 hours at 11 a.m.). `roster`
builds the whole Home roster from a **fixed number of queries regardless of headcount** —
the per-employee version would cost one round trip per person, ruinous over the remote link.

---

## 21. The desktop application

`main.py` + `ui/`. The desktop app boots (logging, a Qt exception hook so errors show a
dialog rather than vanishing, database startup) and runs a **sign-in loop**: handle the
first-run owner wizard / login / any forced password change, then open the `MainWindow`; on
sign-out, return to the sign-in screen. `MainWindow` is a header + teal nav bar over a
stack of seven screens (Home, Shipping, Receiving, Inventory, Logs, Analytics, Settings),
with nav buttons hidden by permission and an **idle lock** for shared warehouse terminals.

The key point: the desktop screens are thin views over the **same `services/` and
`database/` layer** the web app uses. That shared core is why the desktop can be retired in
favour of the web app without touching a single business rule — and it's why the web app was
able to reuse all of it unchanged.

---

## 22. Correctness and concurrency guarantees

The system was hardened through an adversarial multi-agent audit that found and fixed **26
bugs** (1 critical, 6 high, 11 medium, 6 low). The load-bearing guarantees now are:

- **No lost updates on stock.** Every read-modify-write of an on-hand or reserved quantity
  takes a `SELECT … FOR UPDATE` row lock (on the stock level, the lot, and the item row for
  cost) inside a transaction. Without this, two documents touching the same item hold locks
  on *different* order lines, never serialise, and can silently overwrite each other's
  balance change. **Proven by a live test** that fires 240 concurrent movements from 12
  threads at one stock level and confirms the final quantity is exactly right with the
  running balances forming a perfect 1..240 sequence — no gaps, no duplicates.
- **The single write path.** `apply_movement` is the only function that writes `StockLevel`,
  and it writes the ledger row in the same transaction — the log and the on-hand numbers can
  never disagree.
- **Append-only history at the database level.** Even the app's own role cannot UPDATE or
  DELETE `stock_movement` or `audit_log` — enforced by REVOKE, not just by code.
- **Cost frozen at ship time**, so historic profit is immutable.
- **Search_path re-asserted per request and restored after a reconnect** (stored
  thread-locally), so a dropped connection mid-request can't silently switch a user to the
  wrong tenant's schema.
- **Financial-year numbering resets per year**; **moving average carries 6-decimal
  precision**; FEFO flows decrement per-lot stock so lot totals never drift from on-hand.

---

## 23. Testing

The test suites (in the session scratchpad, ~400 assertions across the stock engine,
costing, UI, transactions, analytics, workforce, documents and search) each build their own
throwaway SQLite database. Several also run against **live PostgreSQL** in a disposable
scratch schema that is created and dropped so live data is never touched:

- **Restricted-role suite** — runs a full business cycle (PO → receipt → sale → invoice →
  payment → adjustment → analytics → PDF) through the `inventory_app` role and confirms the
  frozen-ledger restrictions actually bite on the live database.
- **Multi-tenant isolation suite** — provisions two companies, signs into both through the
  real web app, and proves each sees only its own data, the app-role limits hold inside
  tenant schemas, and the admin panel works.
- **Adversarial isolation suite** — the attacks in §15: same-SKU/same-user-id companies,
  cookie tampering, cross-wired cookies, operator-vs-client cookie boundaries.
- **Concurrency proof** — the 240-parallel-movements test in §22.

Two habits the project keeps: *count the queries* when adding a screen (every list screen is
held to 1–2 queries; at ~140 ms per round trip to Sydney a regression is felt immediately),
and *render the screen and actually look at it* (some visual bugs pass every assertion).

---

## 24. Operations

- **One-time DB setup** — run `security_setup.py` (as superuser) to create the
  `inventory_app` role and print its password for the `.env`.
- **Create your operator login** — `manage_platform.py create-admin` (once; also builds the
  registry tables).
- **Onboard a client** — `manage_platform.py create-tenant <slug> "<Name>" --owner <u>
  --owner-name "<n>" --state <code>`. It prints a temporary password you hand over; the
  client is forced to set their own on first login.
- **Run the web app** — `run_web.py` in dev; in production, `uvicorn web.app:app --workers 1`
  behind an HTTPS reverse proxy, hosted near the database (Sydney).
- **Manage clients** — `manage_platform.py` subcommands: `list`, `reset-owner <slug>`
  (issue a fresh temporary password), `suspend-tenant`/`reactivate-tenant <slug>` (block
  logins without deleting data — the non-payment lever, enforced by the `is_active` flag
  that `find_tenant_for_username`/`get_tenant` already check), and `drop-tenant <slug>`
  (permanent). Owner-account management (adding a client's own staff on the web) is W2.

(A full step-by-step, including the exact `.env` and the Supabase-side security tasks, is in
your private `HOW_TO_UPLOAD_TO_GITHUB.txt` runbook.)

---

## 25. Known limitations and roadmap

**Operator responsibilities not enforceable in code** (Supabase dashboard/account): turn on
2FA for the Supabase account; add Network Restrictions (IP allowlist); configure backups
(none are set up — the highest-priority gap before real clients) and rehearse one restore;
keep the superuser password in a password manager and rotate on suspicion.

**Web build roadmap.** W1 (done): tenancy, auth, the shell, Home, the Inventory read view,
and the admin panel. **W2 (done):** Check In / Out, item editing, Recent Logs,
warehouses, employees, user access, purchasing and receiving, sales and shipping, stock
adjustments, warehouse transfers, and cycle counts. **W3 (done):** Analytics, PDF document
downloads, barcode-label printing, and global search are live. **Post-W3 parity:**
partial/full sales-order payments and payment history are live; web returns remain. Hosting/HTTPS is already
running on Vercel.

**Security hardening — done since the review:** per-IP rate-limiting on both login
endpoints, and TOTP two-factor on the `/admin` panel. **Still recommended:** session
revocation on password change (so a stolen cookie dies when the password is changed) and an
absolute — not just idle — session lifetime.

---

## 26. Glossary

- **Tenant** — one client company; physically, one PostgreSQL schema (`t_<slug>`).
- **search_path** — the PostgreSQL setting that decides which schema unqualified table names
  resolve to; the mechanism that isolates tenants.
- **Moving average cost** — an item's cost basis, re-blended on every receipt.
- **COGS** — cost of goods sold; frozen onto the fulfilment line at ship time.
- **FEFO** — First-Expiry-First-Out lot picking.
- **GRN** — Goods Receipt Note, the record of what physically arrived against a PO.
- **CGST/SGST vs IGST** — India's intra-state (split) vs inter-state (single) GST.
- **Reserved / Available** — reserved = committed to a confirmed unshipped order; available
  = on hand − reserved.
- **`inventory_app`** — the restricted database role the running app uses.
- **Ledger** — the append-only `StockMovement` (and `audit_log`) history that cannot be
  rewritten.

---

*This document reflects the codebase as reviewed and hardened. If you change a business
rule, update the relevant section — the value of this file is that it stays true to the
code.*
