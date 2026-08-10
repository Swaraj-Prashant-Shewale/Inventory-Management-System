# Inventory Management System — Specification

Desktop inventory & warehouse management for an Indian multi-warehouse operation.
UI follows `INVENTORY MANAGEMENT SYSTEM.pdf`; feature depth follows Oracle NetSuite.

---

## 1. Locked decisions

| Area | Decision |
|---|---|
| Platform | PySide6 desktop app, packaged as a Windows `.exe` with PyInstaller |
| Database | PostgreSQL (Supabase) in production; SQLite for local dev — selected by `DB_BACKEND` |
| Deployment | Several warehouse PCs, always online, all pointing at the same cloud database |
| Stock model | Tracked **per warehouse** — every item has an independent on-hand quantity per location |
| Orders | **Multi-line**: one order header (ID, status, warehouse, truck) with many item lines |
| Auth | Login screen + **role-based permissions** (Owner / Manager / Clerk) |
| Item master | SKU + barcode, unit of measure + pack size, categories, lot/batch + expiry |
| Serials | Per-unit serial numbers on **items flagged as serialised only** |
| Costing | **Moving average** — unit cost recalculates on every goods receipt |
| Stock moves | Explicit Receive / Fulfil action with **partial quantities**; orders stay open if short |
| Partners | Full supplier **and** customer master records |
| Pricing | **Customer-specific price lists** (tiers), overridable per order line |
| Tax | **Full GST** — HSN/SAC per item, GSTIN on partners, automatic CGST+SGST vs IGST |
| Revenue basis | Net Sales and Gross Profit book **when goods ship** (accrual) |
| Payments | Payments recorded against orders with **partial amounts**; Unpaid → Partial → Paid |
| Recent Logs | A **stock movement trail** — In/Out means goods in vs goods out |
| Time clock | Employees clock in/out by **scanning an ID badge barcode** |
| Tasks | Managers **assign real work items** to employees; Home shows each person's current one |
| Reorder | `Order` = prompt for quantity; `Order Max` = top up to maximum level. Both create draft POs grouped by supplier |
| Operations | Stock adjustments, warehouse transfers, cycle counts, customer + supplier returns |
| Search | **Global search** across items, orders, partners, employees; scanning a barcode jumps to the item |
| Reminders | Manual entries **plus automatic alerts** (below minimum, PO past ETA, lot nearing expiry) |
| Documents | Tax invoice, delivery challan, purchase order PDF, GRN, barcode labels; CSV/Excel export on every table |
| Existing data | None to preserve — schema is created clean |

## 2. Build sequence

**Phase 1 — Foundation. ✅ Complete.** Schema, login and roles, master data (items,
suppliers, customers, warehouses, categories, UOMs, users), working Inventory screen with
per-warehouse stock.

**Phase 2 — Transactions. ✅ Complete.** Purchase orders → goods receipts; sales orders →
fulfilments → invoices and payments; stock adjustments, transfers, cycle counts, returns.

**Phase 3 — Intelligence & output. ✅ Complete.** Home screen, Recent Logs, Analytics
dashboard, global search across all record types, badge scanning, automatic alerts, PDF
documents, PyInstaller packaging.

Each phase runs and is testable on its own.

### Deviations from the PDF, and why

- **Inventory gained tabs.** The PDF draws only the stock table; adjustments, transfers
  and cycle counts are the machinery that keeps that table true, so they sit beside it as
  sibling tabs rather than taking their own place in the navigation bar.
- **Shipping and Receiving gained workflow buttons.** The PDF shows Create / Delete /
  Print. Those are there, alongside the buttons the chosen feature set requires —
  Receive, Confirm, Fulfil, Invoice, Payment, Cancel.
- **Both screens gained a filter row** (warehouse, partner, status) and Receiving gained a
  *Received %* column, because per-warehouse stock and part receipts make a single flat
  list unusable once there is real volume.
- **The donut charts became horizontal bars.** The PDF draws a donut for "Highest Profit
  Items" and "Most Exported Items". Comparing four close values on a donut is unreliable,
  and the PDF's own chart palette failed validation — its light pink measured chroma 0.07
  (reads as grey) and sat ΔE 14.2 from its orange, below the threshold at which full
  colour vision can separate them. Bars use a single validated hue, so the problem
  disappears. Reverting to a donut is a small change if the look matters more.
- **Recent Logs' "Supplier" column is labelled "Supplier / Customer"**, because outbound
  movements carry a customer, and a column that lies about half its rows is worse than a
  longer heading.

## 3. Roles and permissions

| Capability | Owner | Manager | Clerk |
|---|:--:|:--:|:--:|
| View inventory, orders, logs | ✔ | ✔ | ✔ |
| Create/receive purchase orders | ✔ | ✔ | ✔ |
| Create/fulfil sales orders | ✔ | ✔ | ✔ |
| Clock in/out, complete assigned tasks | ✔ | ✔ | ✔ |
| Create and edit items, suppliers, customers | ✔ | ✔ | — |
| Edit selling prices and price lists | ✔ | ✔ | — |
| Approve stock adjustments and cycle counts | ✔ | ✔ | — |
| Assign tasks, manage employees | ✔ | ✔ | — |
| Record payments | ✔ | ✔ | — |
| Delete any record | ✔ | — | — |
| View Analytics (sales, profit, costs) | ✔ | ✔ | — |
| Manage users, roles, company & GST settings | ✔ | — | — |

## 4. Data model overview

**Masters** — `User`, `Employee`, `Warehouse`, `Category`, `Uom`, `Item`, `Supplier`,
`Customer`, `PriceList`, `PriceListItem`, `CompanySettings`, `NumberSequence`

**Stock** — `StockLevel` (item × warehouse: on hand, reserved, min, max), `Lot` (batch +
expiry), `LotStock` (lot × warehouse), `Serial`

**Purchasing** — `PurchaseOrder` / `PurchaseOrderLine`, `GoodsReceipt` / `GoodsReceiptLine`,
`SupplierReturn` / `SupplierReturnLine`

**Sales** — `SalesOrder` / `SalesOrderLine`, `Fulfillment` / `FulfillmentLine`, `Invoice`,
`Payment`, `CustomerReturn` / `CustomerReturnLine`

**Warehouse ops** — `StockAdjustment` / `StockAdjustmentLine`, `StockTransfer` /
`StockTransferLine`, `CycleCount` / `CycleCountLine`

**Activity** — `StockMovement` (the Recent Logs trail), `TimeEntry`, `Task`, `Reminder`,
`AuditLog`

## 5. Rules that govern the numbers

- **Moving average cost.** On each goods receipt:
  `new_avg = (qty_on_hand × old_avg + qty_received × receipt_cost) / (qty_on_hand + qty_received)`.
  Held globally per item, not per warehouse.
- **COGS** is captured on the fulfilment line at ship time, so later cost changes never
  rewrite history.
- **Gross Profit** = Σ(fulfilment line revenue) − Σ(fulfilment line COGS), over shipped orders.
- **GST split.** Supplier/customer state code equal to the warehouse's state code → CGST + SGST
  at half the rate each; otherwise IGST at the full rate.
- **FEFO.** Lot-tracked items are picked earliest-expiry-first by default.
- **Reserved stock.** A confirmed, unshipped sales order line reserves stock; available to
  promise = on hand − reserved.

## 6. Deployment status

**Database: live.** Supabase project `rsoevmbycvvmnmqyztbt` (`ap-southeast-2`,
PostgreSQL 17.6). Schema created and seeded; verified by re-running the service suites
against it in a throwaway schema. Connection goes through the session pooler — see the
README for the two Supabase gotchas that cost real time to diagnose.

## 7. Open items

1. **Restricted database role.** Every warehouse PC currently needs the `postgres`
   password in a plaintext `.env`. A role with only DML rights should replace it — and
   because RLS is enabled on all 41 tables, that role needs `BYPASSRLS` or policies.
2. **Network restrictions.** Allowlisting the warehouses' public IPs would make a leaked
   password unusable from outside your sites. Needs those IPs.
3. Company legal name, GSTIN, registered address and state — required on GST invoices.
4. Warehouse list with each site's state (drives the CGST/SGST vs IGST split).
5. Financial year start (assumed 1 April) and invoice number format (assumed `INV/26-27/0001`).
6. Whether Clerks should see item cost prices (assumed **no** — cost is hidden from Clerks).
