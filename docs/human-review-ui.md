# Human review UI checkpoint

Scope: source-only follow-up to `ed2ff849209419426e4447307a1c2c8fa329babe`.
No schema migration, real document acceptance, live deployment, credentials, or external dispatch.

## What changes

- Batch review shows every extracted item, quantity/unit, literal price/currency/VAT,
  original filename/type and sheet/page/row/cell plus escaped extracted text. Nothing
  is preselected. The user selects reviewed draft rows explicitly (up to 500/request).
- Confirmation validates the entire selection before a single transaction. Repeated IDs
  and parallel/repeated confirmations do not change the original actor/timestamp or
  add duplicate review audit events. Foreign/rejected IDs abort the entire selection.
  The existing `confirmed` imported-price status means reviewed extraction, **not paid**.
- Batch list returns exact supplier-draft/entry/draft-entry/confirmed-entry counts.
  Archive filters show draft, confirmed or all imported prices, including reviewer and
  timestamp. The list is limited to 500 latest rows and says so; opening an individual
  batch shows all its entries. Counts are explicitly counts of entries in batches.
  Load failures are shown as errors, not as evidence that the archive is empty.
- Outbox preview includes the full escaped subject/body/channel/recipient. A checkbox
  gates human approval. Approval does not send or simulate. A second explicit action
  invokes the existing local-only sandbox adapter; its persisted receipt survives UI
  reload and says `external_send=false`. A saved receipt is not delivery evidence;
  approval/content consistency is revalidated by the backend when simulating again.
- Comparison exposes source currency, VAT basis, item subtotal, delivery cost, total,
  lead time and literal payment terms. It labels the leader as a scoring-model leader,
  not a financially normalized winner. Existing score weights and backend money
  policy are unchanged: no currency conversion, net/gross VAT normalization or
  financing adjustment is introduced. Human business-policy approval remains necessary.

SSO actors still come from the trusted DAS session, never from UI labels. Legacy mode
retains explicit actor entry. The shared supplier registry is not converted into
per-user ownership. Existing SSO/RBAC/CSRF and sandbox content guards remain in force.

## Verification commands (synthetic, isolated)

```text
node --test tests/ui_review_helpers.cjs
python3 -B -m pytest -q -p no:cacheprovider --junitxml=<new-evidence-path>
```

The Node suite executes the shipped helper functions and batch-selection handler in
a synthetic DOM, and parses the entire inline script. It is **not browser acceptance**.
The Python tests use temporary SQLite databases, synthetic XLSX, and TestClient.
No tests authenticate as a real user or send Email/MAX/Telegram externally.

## Demo for a reviewer (only after separate canary deployment approval)

1. Sign in personally through DAS; do not share the password. Use an authorized canary
   with isolated test data, never production. Mark fixtures `SYNTHETIC-UI-REVIEW`.
2. Upload a two-row synthetic XLSX invoice (RUB without VAT, one cable and one bolt).
   Open its batch. Check source row/price/VAT, select only the cable, confirm.
   Verify counters `2 / 1 / 1`; switch to confirmed and then all.
3. With a same-cluster synthetic project/lot/supplier, prepare an RFQ. Open full body,
   verify recipient and approve. Confirm it is still not sent. Simulate explicitly,
   inspect `external_send=false`, repeat and verify the same receipt/no new dispatch.
4. Enter a synthetic quote with explicit currency/VAT, delivery and payment terms.
   Compare and verify the literal columns; do not interpret score as net-price or
   financing-adjusted selection. Confirmed import prices are not paid purchases.

Remaining real acceptance blockers: Dima's personal login/module grant and browser
session; representative invoices/quotes with ground truth (including PDF scans and
format variants); approved policy for VAT/payment/financing and a real tender-table
format. OCR/DOCX extraction and actual external messaging are not added by this patch.

## Rollback and data compatibility

### Fast-click refresh guard

Creating a project disables modal openers until the write and list refresh finish.
Dependent forms (lot, document, quote, purchase history) cannot snapshot an incomplete
list while connecting or after a failed refresh. A successful POST is never retried
by the refresh handler: failure says the record was saved and asks for a GET-only
refresh. Existing state is retained on a failed list request; older overlapping
responses cannot overwrite a newer list. The guard also prevents repeated modal
submissions while the save/refresh is in flight. This is UI-only, no schema/API change.

`node --test tests/ui_refresh_guard.cjs` deterministically delays GETs and archive
loading, exercises the shipped modal handlers, repeated clicks, refresh failure and
out-of-order GET completion. These synthetic checks are not native browser acceptance.

No schema or storage migration and no mass update is required. New API response fields
are additive; existing columns and tables are reused. `entries_confirmed` is a new
action value in the existing audit table. Confirming rows is an intentional user action,
not background backfill. This source-only change needs no production backup or restart.
Before a separately authorized deployment take the established Online Backup; rolling
the canary image back to the previous immutable `ed2ff849` image keeps data readable.
Do not undo a legitimate human confirmation blindly; data rollback, if required, must
be separately reviewed against the backup and audit journal. Never rewrite the old image.
