# Identity and provenance model

The public database separates what a source said from the identity OLRESULTS2
currently assigns it to.  This is intentionally evidence-based: an ANNE user
id is a strong source identifier, but only Naturfreunde Wien's book of record
is treated as independently verified club membership.

## Source evidence

`source_document` identifies the concrete ANNE response or legacy result file
behind a result. Committed ANNE responses carry their repository snapshot and
SHA-256; legacy attachments carry the source URL/file name plus the committed
normalized file and its SHA-256. Gitignored local attachment copies are not
published as snapshots because CI cannot reproduce them. The parser hash is
recorded in both cases. `result.source_document_id` therefore makes every
published result traceable to a reproducible input.

The result row retains the observed name, club and source-supplied user id as
well as the matching basis and confidence.  These fields are evidence; they
must not be silently rewritten into a claim about a person's current name or
membership.

## Canonical identity

`person` remains the compatibility-facing canonical identity used by the
static site.  Supporting tables make its derivation auditable:

- `person_identifier`: external identifiers and their verification source;
- `person_alias`: source spellings and verified book-of-record aliases;
- `person_redirect`: compatibility redirects for previously published ids.

Identifier semantics:

- `anne_user_id`: supplied by ANNE, strong person evidence, not independent
  proof of club membership;
- `oefol_id`: verified only when present in the club's private book of record;
- `iof_id`: supplied by ANNE;
- `club_internal`: verified internal Naturfreunde Wien identity without an
  ÖFOL id.

Legacy-only identities receive a deterministic negative id derived from their
normalized name and birth year. The cumulative
`data/person_id_redirects.json` ledger keeps previously published negative URLs
working across migrations. It is updated by
`build/generate_person_redirects.py` against the previously published and
newly built databases; older redirects are retained and chained to a current
target. Historical ids that represented only misparsed split-time rows are
deliberately not redirected to a real person.

Verified book-of-record IDs are canonicalized before duplicate-account
matching and are never merged into a different ID. If ANNE crosses a verified
ID with another person's exact name, only the affected result row is reassigned
when that name uniquely identifies an independently established identity. The
source-supplied ID remains in `result.observed_user_id` as conflict evidence.

## Future matching decisions

New automatic matchers should add evidence and an explicit confidence/basis;
they must not claim that another club's roster is verified.  Ambiguous or
conflicting evidence belongs in a review queue, not in an automatic merge.
