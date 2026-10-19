# Blogger to Beulah one-time migration

## Existing architecture

Beulah stores blog posts as ordinary rows in the existing `resources` table with
`resource_type = 'blog'`. Public post URLs use `/blog/<resource_slug>/`, and the blog list orders
published posts by `resource_published_date DESC`, then `resource_updated_date DESC`.

The importer does not add a model, column, table, or migration. It writes only these existing
`Resource` fields:

- `resource_title`, `resource_body`, `resource_type`, and `resource_slug`
- `resource_featured_image`, `resource_status`, and `resource_published_date`
- `resource_category` and `resource_content_json`
- `resource_date`, `resource_updated_date`, and `resource_is_deleted`

The editor is Tiptap 2. The application renders sanitized `resource_body` HTML and stores a valid
Tiptap document in `resource_content_json` for editing.

## Export inspection

The inspected Takeout `feed.atom` is 7,441,075 bytes and contains:

| Item | Count |
|---|---:|
| Total entries | 601 |
| Posts | 559 |
| Live/published posts | 555 |
| Draft/scheduled posts | 0 |
| Soft-trashed posts | 4 |
| Pages | 1 |
| Comments | 41 |
| Inline images | 3 |
| Posts containing Word-generated markup | 309 |
| Exported labels/categories | 0 |

The single page is `Audio room` and contains a YouTube iframe. It is reported and skipped because
Beulah has no compatible page content type. Comments and soft-trashed posts are also reported and
skipped.

## Field mapping

| Blogger source | Existing Resource field | Rule |
|---|---|---|
| Title | `resource_title` | Preserve exactly. |
| Permalink filename | `resource_slug` | Remove `.html`; preserve where globally available; add `-2`, `-3`, etc. for collisions. |
| Published date | `resource_published_date` | Preserve the original UTC publication date; never substitute import time. |
| Created date | `resource_date` | Preserve when present. |
| Updated date | `resource_updated_date` | Preserve when present. |
| HTML content | `resource_body` | Normalize semantic formatting and sanitize with Beulah's existing rules. |
| HTML content | `resource_content_json` | Convert to validated Tiptap JSON. |
| Labels | `resource_category` | Comma-join into the existing 100-character field. This export has none. |
| Inline images | Body HTML and Tiptap image nodes | Preserve source URLs; do not download or rehost. |
| Featured image | `resource_featured_image` | Leave null because the existing field expects a validated local upload filename. |
| `LIVE` | `resource_status = 'published'` | Preserve original publication date. |
| `DRAFT` / `SCHEDULED` | `resource_status = 'draft'` | Supported, though none exist in this export. |
| `SOFT_TRASHED` | none | Skip. |
| `PAGE` / `COMMENT` | none | Distinguish, count, and skip. |

Blogger author, entry ID, original URL, meta-description, comment metadata, settings, and other
Blogger-only fields are not written to the database.

## Formatting and image handling

The converter preserves supported headings, paragraphs, bold, italic, underline, strike,
superscript, links, ordered and unordered lists, blockquotes, code, horizontal rules, images, and
hard line breaks. Word conditional markup, Word XML, scripts, stylesheets, and unsupported CSS are
removed while readable content is retained.

The three exported images are old HTTP references to
`www.beufoundation.org/wp-includes/js/tinymce/plugins/wordpress/img/trans.gif`. They remain inline
and are not rehosted. Rehosting should be a separate reviewed operation that validates response
status, content type, dimensions, size, and checksum before using the existing image upload path.

## Duplicate detection and idempotency

The connected dry run loads only existing rows whose `resource_type` is exactly `blog`, including
soft-deleted blog rows. Text, audio, slide, and any other resource types are never duplicate
candidates. Duplicate checks use the strongest available combinations in this order:

1. normalized title + Blogger slug + publication date;
2. normalized title + publication date;
3. Blogger slug + publication date;
4. normalized title + Blogger slug.

A slug alone is not treated as a duplicate because Blogger includes year/month in its permalink and
this export has distinct posts sharing the same basename. After an import, rerunning the importer
matches the created Resource rows using the same combinations and does not create duplicates.
Because `resource_slug` has a global database uniqueness constraint, a non-blog row with a non-null
slug can cause a Blogger post to receive a suffix, but it cannot cause that post to be skipped.

The current schema requires globally unique slugs. Nine of the 555 live posts collide by Blogger
basename, so the earliest keeps the basename and later posts receive deterministic suffixes.

## Production orchestration and local JSON reports

`scripts/migrate_blogger_production.py` is the production entry point. It reuses the importer and
normalizer modules to perform duplicate detection, Resource-only import, full semantic HTML/Tiptap
normalization, and the 36 reviewed hard-break corrections in one finalized plan. The hard-break
rules use Blogger entry IDs guarded by title, requested slug, and publication timestamp; they do not
contain development Resource IDs.

The dry-run report includes feed and plan hashes, counts, duplicate matches, skips, warnings,
planned production slugs, normalization results, and the database target. Commit requires that
reviewed report and refuses to proceed if the feed, plan, or database target changed. The commit
report records every newly assigned production Resource ID plus a Resource fingerprint. No audit
metadata is stored in the database.

## Commands

Run from the project root in PowerShell after starting the configured MySQL database.

Production-connected dry run—read-only and required before import:

```powershell
$feed = 'C:\Users\Grace\Downloads\takeout-20261017T152109Z-1-001\Takeout\Blogger\Blogs\Beulah Foundation for Christ\feed.atom'
.\vbeulah\Scripts\python.exe .\scripts\migrate_blogger_production.py --dry-run --feed $feed --report-json .\logs\blogger-production-dry-run.json
```

Review the JSON report and take a database backup. No `flask db upgrade` command is needed.

Commit the reviewed finalized plan and record created production Resource IDs locally:

```powershell
.\vbeulah\Scripts\python.exe .\scripts\migrate_blogger_production.py --commit --feed $feed --verified-report .\logs\blogger-production-dry-run.json --report-json .\logs\blogger-production-commit.json
```

## Rollback

The primary rollback safeguard is a database backup taken after the connected dry run and before
the import. The local import report also supports targeted rollback:

```powershell
.\vbeulah\Scripts\python.exe .\scripts\migrate_blogger_production.py --commit --rollback --verified-report .\logs\blogger-production-commit.json --report-json .\logs\blogger-production-rollback.json
```

Rollback deletes only the Resource IDs listed in the import report. It first verifies the database
target and each Resource fingerprint, and refuses the entire rollback if a Resource is missing,
has been edited, is no longer a blog, or has received comments. If the local report is unavailable
or a protected Resource has changed, restore the pre-import database backup or review removals
manually.

## Verification checklist

- Confirm the connected dry-run counts: 601 entries, 559 posts, 555 published posts, 0 drafts,
  4 soft-trashed posts, 1 page, 41 comments, and 0 labels.
- Review every reported duplicate against the manually-created Resource row.
- Review the nine planned slug suffixes.
- Confirm the import report contains the expected number of created Resource IDs and zero write
  errors.
- Confirm publication dates span 2011-08-16 through 2026-10-05 rather than using the import date.
- Confirm public ordering is newest-to-oldest.
- Compare representative posts containing headings, bold, italic, links, lists, line breaks, and
  images with Blogger.
- Open representative imported posts in the admin editor and confirm Tiptap loads and saves them.
- Confirm the page, comments, and soft-trashed posts were not inserted as Resources.
- Keep the backup and both JSON reports until visual review is complete.
