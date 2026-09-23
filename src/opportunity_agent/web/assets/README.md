# Static assets

Served by `api.py`'s `StaticFiles` mount at `/assets` - see the comment next
to `app.mount(...)` there for why it's a real static mount rather than data
URIs embedded in the HTML.

| File | What it is | Used by |
|---|---|---|
| `meshcloud-logo.jpeg` | The full Meshcloud Consultants wordmark, as supplied | `#brand-footer` in `web/index.html` |
| `icon-512.png`, `icon-192.png` | A **tight crop of just the geometric mark** from the same logo, no wordmark | `manifest.json` (PWA install icon), `og:image`/Twitter card in the SEO head block |
| `favicon.png` | The same mark at 48x48 | `<link rel="icon">` |
| `manifest.json` | PWA manifest | `<link rel="manifest">` |
| `sw.js` | Service worker - see its own comments for why it's network-first, not cache-first | Registered from the bootstrap script at the bottom of `index.html` |
| `login-bag.jpg` | Real photo (not a render or illustration) of an open canvas tote with books spilling out onto linen, by photographer fzaytt on Pexels ([photo 30382353](https://www.pexels.com/photo/30382353/)), free under the Pexels License (commercial use permitted, no attribution required). Resized to 800px wide / re-encoded at JPEG q72 (181KB) from the original - the source frame is 1200x2133. Replaces an earlier hand-drawn SVG bag illustration on explicit request for a photographic, not illustrated, image | `.auth-illustration` background in `web/index.html` (login screen and onboarding wizard panels) |

## Where the icons came from

`meshcloud-logo.jpeg` is 640x640. The icon crop is `img.crop((48, 253, 176,
381))` - found by trial against the real image (three attempts, checked
visually each time) rather than guessed, because the wordmark bleeds into a
naive centre-crop and an app icon showing half a letter looks broken. If the
source logo is ever replaced, that box needs re-finding the same way, not
reused blindly.

**A near-miss worth recording**: several other files named `MESHCLOUD*.png`
exist in the owner's local folders and are *not* this company - they are
marketing banners for "Meshcloud Hardware & Building Solutions", an
unrelated hardware wholesaler that happens to share the name. Confirmed by
actually opening the image before use, not by filename. The correct source
is the 13KB JPEG with "MESHCLOUD CONSULTANCY PRIVATE LTD" on a white
background.

## Regenerating the icon set

```python
from PIL import Image
img = Image.open("meshcloud-logo.jpeg").convert("RGB")
mark = img.crop((48, 253, 176, 381))
for size, name in [(512, "icon-512.png"), (192, "icon-192.png"), (48, "favicon.png")]:
    mark.resize((size, size), Image.LANCZOS).save(name)
```

Pillow is not a runtime dependency of this project - it is only ever needed
to regenerate these files, once, on whoever's machine is doing it.
