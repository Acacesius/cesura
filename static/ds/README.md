# cesura/rubato design system

The single source of truth for what cesura and rubato share: type, base styles,
components and client helpers. It lives here, in cesura's `static/ds/`. Rubato
vendors a copy pinned to a cesura release tag. There's no package registry, no
submodule and no build step.

| File | What it is |
|---|---|
| `base.css` | Fonts (`@font-face`), reset, utilities, components: wordmark, element tile, pill, dot, card, avatar stack, tag, buttons, track rows, skeletons, empty states, sheets, switch, people rows, touch reorder states, the `pulse` motion. |
| `core.js` | `el()`, icons, avatars, `art()`, `toast()`, `setMediaSession()`, `liveSocket()` (reconnect + banner), `skeletonRows()`, `emptyState()`, sheets, `touchQueue()`. |
| `fonts/` | Bricolage Grotesque (variable weight) and IBM Plex Mono 400/500/600, with their OFL licences. |
| `VERSION` | The design-system version (follows cesura releases). |

## Contract: tokens an app defines on `:root`

`base.css` styles everything through these names. An app sets them in its own
theme file, loaded **after** `base.css`, then adds app-specific CSS.

| Role | Tokens |
|---|---|
| Neutrals | `--ground` `--surface` `--raised` `--line` `--line-2` `--row` |
| Text | `--text` `--muted` `--faint` |
| Accent (one per app) | `--accent` `--accent-hi` `--on-accent` `--accent-line` |
| Signal (status, sync, "OK") | `--signal` `--signal-line` `--signal-bg` |
| Warning | `--warn` |

cesura: accent = amber `#F4B544`, signal = cesium blue `#8CB8F0`.
rubato: accent = rubidium red `#F2506A`. Its signal colour is its call.

```html
<link rel="stylesheet" href="/static/ds/base.css">
<link rel="stylesheet" href="/static/APP.css">   <!-- :root palette + app CSS -->
<script src="/static/ds/core.js"></script>
<script src="/static/APP.js"></script>
```

`.btn.accent` / `.tag.accent` are the accent variants (cesura's markup also
uses the older `.amber` names, which `base.css` keeps as aliases).

## How each repo gets it

- **cesura** owns it: edit here, and bump `VERSION` with the release.
- **rubato** vendors it to `static/ds/` from a cesura tag:

  ```sh
  # rubato/scripts/sync-ds.sh v0.2.0
  curl -fsSL "https://codeload.github.com/acacesius/cesura/tar.gz/refs/tags/$1" \
    | tar -xz --strip-components=3 -C static/ds "cesura-${1#v}/static/ds"
  ```

  The copy is committed in rubato, so its Docker build and CI don't change.
  Re-running the script on a newer tag is the upgrade. Don't edit the copy;
  change cesura and release instead.

## Rules for new components

Use the tokens above (never a raw palette colour), the two typefaces, 44px
minimum tap targets, and the existing motion (`pulse`, 0.15 s ease-out
transforms). Check text contrast against `--ground` and `--surface` (WCAG AA).
