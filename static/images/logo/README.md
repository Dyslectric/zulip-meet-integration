Consort's own marks are the `consort-*` files. The `zulip-*` files are
upstream's and are left in place unreferenced, so that merges from
`zulip/zulip` do not conflict here.

| File | Used by |
|---|---|
| `consort-logo.svg` | the navbar logo, top left — the default realm logo, from `zerver/lib/realm_logo.py` |
| `consort-icon-square.svg` | the PWA manifest's maskable icon |
| `consort-icon-circle.svg` | avatars and anywhere a disc is wanted |
| `consort-icon-128x128.png` | `og:image` |
| `consort-icon-512x512.png` | the PWA manifest icon and Web Push notifications |
| `apple-touch-icon-precomposed.png` | iOS home screen |

The mark also appears inlined, rather than as a file, in
`static/images/favicon.svg`, `web/templates/favicon.svg.hbs` (the unread-count
favicon), `templates/zerver/app/index.html` (the loading splash and the
message-feed watermark) and `templates/zerver/portico-header.html` (the login
page). Those copies and these files all come out of one generator — see
`brand/` in the Consort repository — so regenerate rather than hand-editing.

Generally, prefer the SVG. To get a PNG at some other size:

```
rsvg-convert -h 512 static/images/logo/consort-icon-square.svg -o /tmp/consort-512.png
```
