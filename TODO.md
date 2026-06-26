
## City official contact enrichment (Cowork task — future)
- 510 city officials (half the dataset) have no phone or email on file.
- Root cause: small WA city sites mostly don't resolve / don't expose structured contact data (confirmed during enrichment work).
- Not worth a one-shot scraper. Good candidate for a patient Cowork reconciliation job.
- Sources to try: county clerk directories, Association of Washington Cities (AWC) rosters, MRSC, individual city council pages.
- Goal: fill phone/email/website for city officials; feed results back into the officials table.
- Blocked on nothing. Pick up when ready.

## CommuneUSA — Outstanding Backlog

### Action platform
- [x] Take Action center on official profiles (contact templates, vote, testify, recall info)
- [ ] Local sentiment polling — BLOCKED on attorney consult. Build behind NEXT_PUBLIC_ENABLE_POLLING flag or on `polling` branch; keep dark until reviewed. Attorney brief is in legal/communeusa_attorney_brief.docx.
- [ ] A/B test contact templates (template vs blank) once live — data-action-variant hook already in place.

### Data completeness
- [ ] Law enforcement — still 0 from source; needs scrape + seed fallback (same fix pattern as hospitals).
- [ ] Fire & EMS — still 0 from source; same approach.
- [ ] City official contact enrichment (phone/email) — 510 missing; Cowork reconciliation job. See note above.
- [ ] School board members — partial coverage only.
- [ ] Hospitals — have ~20 largest via seed; long tail (~100 total) needs CMS national dataset or manual seed later.

### Legal / ops
- [ ] Book attorney consult using legal/communeusa_attorney_brief.docx (longest-lead item — do soon).
- [ ] Set up hello@communeusa.com email (Google Workspace ~$6/mo).
- [ ] Name-change decision (still pending).

### Scale
- [ ] Expand beyond Washington — per-state data sources + per-state recall statutes for Take Action center. Biggest lift.

### Automation (optional)
- [ ] Consider adding redistricting-sync, finance-sync, elections-sync to vercel.json cron (currently manual).

## CommuneUSA — Outstanding Backlog (updated)

### Action platform
- [x] Take Action center on official profiles (contact templates, vote, testify, recall info)
- [x] Officials name search (/officials)
- [ ] Local sentiment polling — BLOCKED on attorney consult. Build behind NEXT_PUBLIC_ENABLE_POLLING flag or on `polling` branch; keep dark until reviewed. Brief: legal/communeusa_attorney_brief.docx.
- [ ] A/B test contact templates (template vs blank) once live — data-action-variant hook already in place.

### Directory / data
- [x] Directory entity detail pages for all 8 categories + school board rosters
- [ ] Law enforcement — still 0 from source; needs scrape + seed fallback (same pattern as hospitals fix).
- [ ] Fire & EMS — still 0 from source; same approach.
- [ ] City official contact enrichment (phone/email) — 510 missing; Cowork reconciliation job. Sources: AWC rosters, county clerk directories, MRSC, city council pages.
- [ ] School board members — partial coverage only.
- [ ] Hospitals — ~20 largest via seed; long tail (~100 total) needs CMS national dataset or manual seed.

### Data cleanup (low priority)
- [ ] Normalize party in DB: `UPDATE officials SET party = 'Democratic' WHERE party = 'Democrat';` (render-time handles display already; this makes source canonical).

### Legal / ops
- [ ] Book attorney consult using legal/communeusa_attorney_brief.docx — longest-lead item, do soon.
- [ ] Set up hello@communeusa.com email (Google Workspace ~$6/mo).
- [ ] Name-change decision (pending).

### Scale
- [ ] Expand beyond Washington — per-state data sources + per-state recall statutes for Take Action center. Biggest lift.

### Automation (optional)
- [ ] Consider adding redistricting-sync, finance-sync, elections-sync to vercel.json cron (currently manual).
