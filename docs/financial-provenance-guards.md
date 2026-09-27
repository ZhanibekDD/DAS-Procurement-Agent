# Financial provenance guards (source-only canary change)

No schema migration, existing-row rewrite or automatic reclassification is part
of this change. Production/canary runtime rollout requires a separate decision.

- Legacy `.xls` is rejected before batch creation, even if its bytes look like
  XLSX. Convert it explicitly and review the resulting `.xlsx`.
- New quotes and manually confirmed purchases require explicit `currency` and
  strict boolean `vat_included`; their UI selectors start empty. Existing API
  clients must send both fields. No exchange rates or financing costs are guessed.
  The lot UI also requires an explicit currency, allowing matching KZT/USD/EUR
  quotes instead of silently creating every lot in RUB.
- Historical benchmarks require an approved purchase, the exact normalized
  region, the same mapped/confirmed supplier cluster, currency, unit and a
  compatible VAT basis. A missing supplier, region, ambiguous VAT or cluster
  mismatch is excluded. Region aliases are deliberately not guessed; absence of
  a benchmark is not an estimate of zero. Each quote uses its own VAT basis.
- The established ranking weights are unchanged. Payment terms remain descriptive;
  they are not a financing-adjusted total cost of ownership. Historical records
  created under previous defaults still need a separate evidence-based review;
  a stored boolean cannot prove that an old user explicitly confirmed VAT.
  In particular the legacy ranking can still score mixed net/gross quotations
  with a 15-point VAT bonus: this is a business-policy limitation, **not VAT
  normalization or a financially normalized purchasing recommendation**. Human
  review is required; changing this ranking policy is outside this patch.
- Supplier approval links `price_history_entries.supplier_id` only. It leaves
  prices `draft`; separate entry review changes them to `confirmed`, which means
  imported price review, **not proof of payment**. Neither action inserts into
  the independent `purchase_history` table.
- New batch imports persist original bytes using existing document storage.
  `source_documents.document_type` is the actual extracted type (`invoice`,
  `price_list`, `commercial_offer`, `unknown`), never inferred `paid_invoice`.
  `storage_path` points to the preserved bytes and `extraction_status` is
  `extracted_needs_review`. Existing SHA-matched source rows are left unchanged;
  legacy incorrect metadata must be reported and reviewed separately, not silently
  repaired or labelled as a verified file.

## Backup and rollback

No backup is needed merely to check out this source commit. Before any authorized
runtime rollout take a SQLite Online Backup plus a consistent copy of the existing
`uploads/` directory and record SHA256 hashes. New batch uploads now retain source
bytes under that existing directory, so database-only backups are incomplete.
Rollback the image/source to `b659068eaa2686313098417eaa43f691c36c1766`; there is no
schema rollback. Preserve newly uploaded files and reviewed records. Restoring a
database/file snapshot is a separate authorized operation, not an automatic
cleanup or a prerequisite to source rollback.
