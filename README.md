# PDF Outline Export

An [Obsidian](https://obsidian.md) plugin that exports a note to PDF **with a
document outline** — the clickable bookmarks panel in PDF viewers, built from
the note's headings.

Pages are printed by Obsidian's own **Export to PDF**, so the result looks
exactly like the built-in export: same theme, fonts, math, callouts and
tables. The only difference is that the PDF has bookmarks and opens with the
bookmarks panel already shown.

## Usage

Run any of these on a Markdown note:

- **Command palette** → *PDF Outline Export: Export current note to PDF with outline* (Ctrl + P)
- **File menu** (right-click a note in the file explorer, or the note's `⋯`
  menu) → *Export to PDF with outline*

Choose where to save the file. When the export finishes, the PDF opens in your
default viewer (this can be turned off in the settings).

Page size, margins, scale and the *Include file name as title* option are taken
from Obsidian's own **Export to PDF** dialog. To change them, open that dialog
once, adjust the values and close it — the plugin reuses what Obsidian saved.

### Settings

| Setting | Default | What it does |
|---|---|---|
| Show the outline panel when the PDF opens | on | Marks the file so viewers open it with the bookmarks panel visible. |
| Open the PDF after export | on | Opens the exported file in the system PDF viewer. |

### Command line

With Obsidian 1.12.2 or later and the Obsidian CLI enabled, a note can be
exported from a terminal while Obsidian is running:

```
obsidian pdf-outline-export file="Notes/Lecture 5.md" out="D:\PDF\Lecture 5.pdf"
```

| Flag | Meaning |
|---|---|
| `file=<path>` | Note to export: a path inside the vault (required). |
| `out=<path>` | Output file. Relative paths are resolved against the vault folder. Default: next to the note. |
| `page-size=A3\|A4\|A5\|Legal\|Letter\|Tabloid` | Page size (default `A4`). |
| `include-name=yes\|no` | Use the file name as the title (default `yes`). |
| `downscale=<10..100>` | Scale in percent (default `100`). |
| `landscape` | Landscape orientation. |
| `margin=default\|minimal\|none` | Page margins (default `default`). |
| `outline-panel=yes\|no` | Open the PDF with the bookmarks panel shown (default: the plugin setting). |

## Installation

**From Obsidian:** Settings → Community plugins → Browse → search for
*PDF Outline Export* → Install → Enable.

**Manually:** download `main.js`, `manifest.json` and `styles.css` from the
[latest release](https://github.com/leafyff/pdf_outliner/releases/latest),
put them into `<your vault>/.obsidian/plugins/pdf-outline-export/`, then
reload Obsidian and enable the plugin under Settings → Community plugins.

The plugin works on desktop only (Windows, macOS, Linux).

## How it works

When Obsidian prints a PDF, it passes an options object to Electron's
`webContents.printToPDF()` unchanged. Chromium can build a PDF outline from
the page's `<h1>`–`<h6>` elements (`generateDocumentOutline`), but the
built-in export never turns that option on.

The plugin creates the same export dialog that *Export to PDF* opens (without
showing it) and calls its print method with the outline enabled. Nothing is
re-rendered by the plugin, which is why the layout matches the built-in export
exactly.

Two more things happen around printing:

- **Waiting for the page to settle.** Obsidian prints as soon as the note is
  rendered. With math-heavy notes this can happen before the math fonts have
  loaded, and formulas end up misplaced. The plugin holds the print request
  until fonts are loaded and the layout stops changing (at most about 20
  seconds).
- **Showing the outline on open.** Chromium writes the outline but does not ask
  viewers to display it. After printing, the plugin appends
  `/PageMode /UseOutlines` to the PDF as a standard incremental update: a few
  hundred bytes are added at the end of the file, and the existing content is
  left byte for byte as it was.

## Things to know

- **Large notes with a lot of math export slowly.** Chromium only generates an
  outline for a *tagged* (accessible) PDF, and building the accessibility tree
  for hundreds of formulas takes time. On a long note with many formulas an
  export can take minutes instead of seconds, and the file can be several
  times larger than a plain export.
- **No bookmarks in the PDF?** The note may have no headings, or all of them
  may be inside code blocks. Very old Obsidian installers ship an Electron
  version without outline support; reinstalling Obsidian from
  [obsidian.md/download](https://obsidian.md/download) updates it.
- **Internal APIs.** The plugin relies on how Obsidian's PDF export works
  internally (the export dialog and its print call). A future Obsidian update
  may change that; if exports stop working, please
  [open an issue](https://github.com/leafyff/pdf_outliner/issues).
- Only one export runs at a time.

## Disclosures

- **Files outside the vault:** the plugin writes the PDF to the location you
  pick in the save dialog (or pass as `out=` on the command line), which may be
  outside the vault. It reads and modifies only that PDF.
- **Network:** none. The plugin makes no network requests and collects no data.

## Releasing (for maintainers)

1. Update `version` in `manifest.json` and add the version to `versions.json`.
2. Commit, then push a tag with exactly the same version, e.g.
   `git tag 1.1.0 && git push origin 1.1.0`.
3. The *Release* workflow attaches `main.js`, `manifest.json` and `styles.css`
   to a new GitHub release.

## License

[MIT](LICENSE)
