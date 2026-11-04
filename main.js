"use strict";

/*
 * PDF Outline Export — an Obsidian plugin.
 *
 * How Obsidian prints a PDF: the renderer builds an options object and sends
 * it over IPC ("print-to-pdf"); the main process hands that same object to
 * Electron's webContents.printToPDF() without filtering it.
 *
 * Chromium can build a bookmark tree (document outline) from <h1>…<h6> by
 * itself — the `generateDocumentOutline` option. Obsidian's built-in export
 * never turns it on, which is why exported PDFs have no outline.
 *
 * This plugin does not replace the renderer. It creates the same internal
 * export dialog that "Export to PDF" opens (without showing it) and calls its
 * printToPdf() with an options object that has the outline turned on. Layout,
 * fonts, math and theme are therefore exactly those of the built-in export.
 *
 * Chromium puts the outline into the file but does not ask viewers to show
 * it. After printing, the plugin appends `/PageMode /UseOutlines` to the PDF
 * as an incremental update (the existing bytes are left untouched), so the
 * bookmarks panel opens together with the document.
 */

const obsidian = require("obsidian");
const {
    MarkdownRenderer,
    MarkdownView,
    Modal,
    Notice,
    Plugin,
    PluginSettingTab,
    Setting,
    TFile,
    normalizePath,
} = obsidian;
const fs = require("fs");
const fsp = fs.promises;
const path = require("path");

/** Page sizes understood by Obsidian's export dialog. */
const PAGE_SIZES = ["A3", "A4", "A5", "Legal", "Letter", "Tabloid"];

/** Margins as Obsidian's export dialog stores them: 0 default, 1 none, 2 minimal. */
const MARGINS = { default: 0, none: 1, minimal: 2, "0": 0, "1": 1, "2": 2 };

/** Export settings Obsidian uses before the user has saved any of their own. */
const DEFAULT_EXPORT_SETTINGS = {
    includeName: true,
    pageSize: "A4",
    landscape: false,
    margin: "0",
    downscalePercent: 100,
};

const DEFAULT_SETTINGS = {
    /** Mark exported PDFs so viewers open them with the bookmarks panel visible. */
    showOutlinePanel: true,
    /** Open the PDF in the system viewer when an export from the app finishes. */
    openAfterExport: true,
};

// ----------------------------------------------------------------------
//  Small helpers
// ----------------------------------------------------------------------

function wait(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback;
    if (typeof value === "boolean") return value;
    const text = String(value).trim().toLowerCase();
    if (["1", "true", "yes", "y", "on"].includes(text)) return true;
    if (["0", "false", "no", "n", "off"].includes(text)) return false;
    return fallback;
}

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function parsePageSize(value) {
    if (!value) return "A4";
    const wanted = String(value).trim().toLowerCase();
    const found = PAGE_SIZES.find((size) => size.toLowerCase() === wanted);
    if (!found) throw new Error(`unknown page size: ${value} (expected ${PAGE_SIZES.join(", ")})`);
    return found;
}

function parseMargin(value) {
    if (value === undefined || value === null || value === "") return 0;
    const key = String(value).trim().toLowerCase();
    if (!(key in MARGINS)) throw new Error(`unknown margin: ${value} (expected default, minimal or none)`);
    return MARGINS[key];
}

/**
 * Waits until the document has requested and loaded its fonts — but no longer
 * than `limitMs`.
 *
 * `fonts.ready` alone is not enough: it resolves immediately if layout has not
 * asked for any font yet, and layout asks lazily. Printing too early measures
 * text with a fallback font, so formulas overlap and page counts drift.
 * Reading `scrollHeight` forces a layout pass first.
 */
function fontsSettled(targetWindow, limitMs) {
    const doc = targetWindow.document;
    const fonts = doc.fonts;
    if (!fonts) return Promise.resolve();
    if (doc.body) void doc.body.scrollHeight;
    return Promise.race([
        Promise.resolve(fonts.ready).catch(() => {}),
        wait(limitMs),
    ]);
}

/**
 * Forces the math fonts (about a dozen faces) to load.
 *
 * Waiting is not enough for them: MathJax positions glyphs from its own metric
 * tables, so a late font does not trigger a re-layout — glyphs simply land in
 * the wrong places. Once the faces are in memory the print window uses them
 * right away. Other faces are left alone: every load re-lays out the document,
 * which takes minutes on a long note.
 */
function forceLoadMathFonts(targetWindow, limitMs) {
    const fonts = targetWindow.document.fonts;
    if (!fonts) return Promise.resolve();
    const pending = [];
    try {
        fonts.forEach((face) => {
            if (/math|mjx|tex|katex/i.test(face.family) && face.status === "unloaded") {
                pending.push(face.load().catch(() => {}));
            }
        });
    } catch (error) {
        // FontFaceSet is not iterable here — plain waiting will have to do.
    }
    return Promise.race([Promise.all(pending), wait(limitMs)]);
}

// ----------------------------------------------------------------------
//  PDF post-processing: open with the outline panel
// ----------------------------------------------------------------------

/**
 * Returns the text of the `<< … >>` dictionary starting at `start` (brackets
 * included), or null. Nested dictionaries and literal strings are skipped so
 * that a `>>` inside them does not end the dictionary early.
 */
function readPdfDict(text, start) {
    if (text.substr(start, 2) !== "<<") return null;
    let depth = 0;
    let i = start;
    while (i < text.length) {
        const ch = text[i];
        if (ch === "(") {
            // Literal string: parentheses may be nested or escaped.
            let level = 1;
            i++;
            while (i < text.length && level > 0) {
                if (text[i] === "\\") i++;
                else if (text[i] === "(") level++;
                else if (text[i] === ")") level--;
                i++;
            }
            continue;
        }
        if (ch === "<" && text[i + 1] === "<") { depth++; i += 2; continue; }
        if (ch === ">" && text[i + 1] === ">") {
            depth--;
            i += 2;
            if (depth === 0) return text.slice(start, i);
            continue;
        }
        i++;
    }
    return null;
}

/**
 * Adds `/PageMode /UseOutlines` to a PDF so viewers open the bookmarks panel
 * right away.
 *
 * The file is not rewritten: a standard incremental update is appended (a new
 * revision of the catalog, a one-object xref section and a trailer with
 * /Prev), the same way Acrobat saves edits. Every byte before it stays as is.
 *
 * Returns a new Buffer, or null when there is nothing to do (no outline, flag
 * already set) or the file is not laid out the way Chromium writes PDFs
 * (e.g. cross-reference streams).
 */
function addOutlinePageMode(buffer) {
    // latin1 maps bytes to characters one to one — nothing is lost.
    const text = buffer.toString("latin1");

    const startxrefAt = text.lastIndexOf("startxref");
    if (startxrefAt < 0) throw new Error("the PDF has no startxref");
    const prevMatch = /^startxref\s+(\d+)/.exec(text.slice(startxrefAt));
    if (!prevMatch) throw new Error("could not read startxref");
    const prevXref = Number(prevMatch[1]);

    const trailerAt = text.lastIndexOf("trailer", startxrefAt);
    if (trailerAt < 0) return null; // cross-reference stream — Chromium does not write these
    const trailerDictAt = text.indexOf("<<", trailerAt);
    const trailer = trailerDictAt >= 0 && trailerDictAt < startxrefAt
        ? readPdfDict(text, trailerDictAt)
        : null;
    if (!trailer) throw new Error("could not read the PDF trailer");

    const rootMatch = /\/Root\s+(\d+)\s+(\d+)\s+R/.exec(trailer);
    if (!rootMatch) throw new Error("the PDF trailer has no /Root");
    const [, rootNum, rootGen] = rootMatch;

    // The last revision of the object in the file is the one in effect.
    const objPattern = new RegExp(`(?:^|[\\s>])${rootNum}\\s+${rootGen}\\s+obj\\b`, "g");
    let objAt = -1;
    for (let m; (m = objPattern.exec(text)) !== null;) objAt = m.index;
    if (objAt < 0) throw new Error("the PDF catalog was not found");
    const catalogAt = text.indexOf("<<", objAt);
    const catalog = catalogAt >= 0 ? readPdfDict(text, catalogAt) : null;
    if (!catalog) throw new Error("could not read the PDF catalog");

    if (!/\/Outlines\s/.test(catalog)) return null;                // no bookmarks to show
    if (/\/PageMode\s*\/UseOutlines\b/.test(catalog)) return null; // already set

    const patched = /\/PageMode\s*\/\w+/.test(catalog)
        ? catalog.replace(/\/PageMode\s*\/\w+/, "/PageMode /UseOutlines")
        : `<</PageMode /UseOutlines\n${catalog.slice(2)}`;

    const head = text.endsWith("\n") || text.endsWith("\r") ? "" : "\n";
    const objOffset = buffer.length + head.length;
    const objText = `${rootNum} ${rootGen} obj\n${patched}\nendobj\n`;
    const xrefOffset = objOffset + objText.length;
    const trailerBody = trailer.slice(2, -2).replace(/\/Prev\s+\d+/, "").trim();
    const update = head + objText
        + "xref\n"
        // The entry for object 0 is optional, but Acrobat writes it and some
        // readers treat a section without it as misnumbered.
        + "0 1\n"
        + "0000000000 65535 f \n"
        + `${rootNum} 1\n`
        + `${String(objOffset).padStart(10, "0")} ${String(rootGen).padStart(5, "0")} n \n`
        + "trailer\n"
        + `<<${trailerBody}\n/Prev ${prevXref}>>\n`
        + "startxref\n"
        + `${xrefOffset}\n`
        + "%%EOF\n";
    return Buffer.concat([buffer, Buffer.from(update, "latin1")]);
}

// ----------------------------------------------------------------------
//  The plugin
// ----------------------------------------------------------------------

module.exports = class PdfOutlineExportPlugin extends Plugin {
    async onload() {
        await this.loadSettings();
        /** Obsidian's internal export dialog class, captured once and cached. */
        this.exportModalClass = null;
        /** Exports patch globals for their duration, so they run one at a time. */
        this.busy = false;

        this.addSettingTab(new PdfOutlineExportSettingTab(this.app, this));

        this.addCommand({
            id: "export-active-note",
            name: "Export current note to PDF with outline",
            checkCallback: (checking) => {
                const file = this.app.workspace.getActiveFile();
                if (!file || file.extension !== "md") return false;
                if (!checking) void this.exportWithDialog(file);
                return true;
            },
        });

        this.registerEvent(
            this.app.workspace.on("file-menu", (menu, file) => {
                if (!(file instanceof TFile) || file.extension !== "md") return;
                menu.addItem((item) => item
                    .setTitle("Export to PDF with outline")
                    .setIcon("file-output")
                    .onClick(() => void this.exportWithDialog(file)));
            }),
        );

        // Obsidian 1.12.2+ can run plugin commands from a terminal:
        //   obsidian pdf-outline-export file=Notes/Lecture.md out=D:\PDF\Lecture.pdf
        if (typeof this.registerCliHandler === "function") {
            this.registerCliHandler(
                "pdf-outline-export",
                "Export a note to PDF with a document outline",
                {
                    file: { value: "<path>", description: "Note to export (path in the vault)", required: true },
                    out: { value: "<path>", description: "Output PDF (default: next to the note)" },
                    "page-size": { value: PAGE_SIZES.join("|"), description: "Page size (default: A4)" },
                    "include-name": { value: "yes|no", description: "Use the file name as the title (default: yes)" },
                    downscale: { value: "<10..100>", description: "Downscale percent (default: 100)" },
                    landscape: { description: "Landscape orientation" },
                    margin: { value: "default|minimal|none", description: "Page margins (default: default)" },
                    "outline-panel": { value: "yes|no", description: "Open the PDF with the bookmarks panel shown (default: plugin setting)" },
                },
                (args) => this.exportFromCli(args),
            );
        }
    }

    async loadSettings() {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    }

    async saveSettings() {
        await this.saveData(this.settings);
    }

    // ------------------------------------------------------------------
    //  Entry points
    // ------------------------------------------------------------------

    /** Command and file menu: ask where to save, then export. */
    async exportWithDialog(file) {
        let progress = null;
        try {
            const result = await this.electron().dialog.showSaveDialog({
                defaultPath: `${file.basename}.pdf`,
                filters: [{ name: "PDF Files", extensions: ["pdf"] }],
                properties: ["showOverwriteConfirmation"],
            });
            if (result.canceled || !result.filePath) return;

            const saved = this.obsidianExportSettings();
            const options = this.buildPrintOptions({
                pageSize: saved.pageSize,
                includeName: saved.includeName,
                downscale: saved.downscalePercent,
                landscape: saved.landscape,
                margin: saved.margin,
            }, result.filePath, this.settings.openAfterExport);

            progress = new Notice(`Exporting ${file.basename} to PDF…`, 0);
            await this.exportFile(file, options, this.settings.showOutlinePanel);
            new Notice(`PDF with outline saved: ${path.basename(result.filePath)}`);
        } catch (error) {
            console.error("PDF Outline Export:", error);
            new Notice(`PDF export failed: ${error && error.message ? error.message : error}`);
        } finally {
            if (progress) progress.hide();
        }
    }

    /** `obsidian pdf-outline-export file=… out=…` */
    async exportFromCli(args) {
        const file = this.resolveNote(args.file);
        const base = this.vaultPath();
        let out;
        if (args.out) {
            out = path.isAbsolute(args.out) || !base ? path.resolve(args.out) : path.resolve(base, args.out);
        } else {
            if (!base) throw new Error("the vault is not on disk; pass out=<path>");
            out = path.join(base, ...file.path.replace(/\.md$/i, ".pdf").split("/"));
        }
        await fsp.mkdir(path.dirname(out), { recursive: true });

        const options = this.buildPrintOptions({
            pageSize: args["page-size"],
            includeName: args["include-name"],
            downscale: args.downscale,
            landscape: args.landscape,
            margin: args.margin,
        }, out, false);
        const showPanel = parseBoolean(args["outline-panel"], this.settings.showOutlinePanel);

        await this.exportFile(file, options, showPanel);
        if (!fs.existsSync(out)) throw new Error("Obsidian did not create the file");
        return `PDF saved to ${out}`;
    }

    /** Finds a note by vault path or by an absolute path inside the vault. */
    resolveNote(value) {
        if (!value || value === "true") throw new Error("pass the note to export: file=<path>");
        let vaultPath = String(value);
        const base = this.vaultPath();
        if (base && path.isAbsolute(vaultPath)) {
            const relative = path.relative(base, vaultPath);
            if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
                throw new Error(`the note is outside this vault: ${value}`);
            }
            vaultPath = relative;
        }
        vaultPath = normalizePath(vaultPath.split(path.sep).join("/"));
        let file = this.app.vault.getAbstractFileByPath(vaultPath);
        if (!file && !/\.md$/i.test(vaultPath)) {
            file = this.app.vault.getAbstractFileByPath(`${vaultPath}.md`);
        }
        if (!(file instanceof TFile) || file.extension !== "md") {
            throw new Error(`no such note in the vault: ${value}`);
        }
        return file;
    }

    // ------------------------------------------------------------------
    //  Export
    // ------------------------------------------------------------------

    /**
     * The same options object Obsidian's export dialog builds, plus the two
     * Chromium options this plugin exists for.
     */
    buildPrintOptions(params, out, open) {
        const pageSize = parsePageSize(params.pageSize);
        const marginsType = parseMargin(params.margin);
        const downscale = clamp(parseInt(params.downscale, 10) || 100, 10, 100);

        const options = {
            includeName: parseBoolean(params.includeName, true),
            pageSize,
            landscape: parseBoolean(params.landscape, false),
            marginsType,
            scaleFactor: downscale,
            scale: clamp(downscale / 100, 0.1, 2),
            open: Boolean(open),
            filepath: out,
            // Chromium builds the bookmark tree from the page's headings.
            // A tagged PDF is the precondition for that outline to be generated.
            generateTaggedPDF: true,
            generateDocumentOutline: true,
        };
        if (marginsType === 1 || marginsType === 2) {
            options.margins = { top: 0, left: 0, bottom: 0, right: 0 };
        }
        return options;
    }

    /** Settings from Obsidian's own "Export to PDF" dialog (internal API, guarded). */
    obsidianExportSettings() {
        const vault = this.app.vault;
        const saved = typeof vault.getConfig === "function" ? vault.getConfig("pdfExportSettings") : null;
        return Object.assign({}, DEFAULT_EXPORT_SETTINGS, saved || {});
    }

    /**
     * Prints one vault file.
     *
     * Obsidian's export dialog (an internal class) is created but never shown:
     * only its printToPdf(options) is needed, and that does all the work —
     * hidden window, light theme, rendering the note, IPC to the main process.
     */
    async exportFile(file, options, showPanel) {
        if (this.busy) throw new Error("another PDF export is still running");
        this.busy = true;
        try {
            const vault = this.app.vault;
            if (typeof vault.getConfig === "function" && typeof vault.setConfig === "function"
                && !vault.getConfig("pdfExportSettings")) {
                vault.setConfig("pdfExportSettings", { ...DEFAULT_EXPORT_SETTINGS });
            }

            // The panel flag is written into the finished file, so Obsidian must
            // not open it itself — the viewer could get there first. The plugin
            // opens the file after patching it.
            const printOptions = { ...options };
            const openAfter = showPanel && printOptions.open;
            if (openAfter) printOptions.open = false;

            await this.warmUpFonts();
            const modal = await this.createExportModal(file);
            const release = this.holdPrintUntilFontsReady();
            try {
                await modal.printToPdf(printOptions);
            } finally {
                release();
            }

            if (showPanel) await this.showOutlineOnOpen(printOptions.filepath);
            if (openAfter) await this.openExternally(printOptions.filepath);
        } finally {
            this.busy = false;
        }
    }

    /** Appends the "open with the bookmarks panel" flag to a finished PDF. */
    async showOutlineOnOpen(file) {
        try {
            // printToPdf resolves once the main process has answered; the file
            // is written by then, but wait a little just in case.
            const deadline = Date.now() + 10000;
            while (!fs.existsSync(file) && Date.now() < deadline) await wait(100);
            const patched = addOutlinePageMode(await fsp.readFile(file));
            if (patched) await fsp.writeFile(file, patched);
        } catch (error) {
            console.warn("PDF Outline Export: could not set the outline panel flag:", error);
        }
    }

    async openExternally(file) {
        try {
            const error = await this.electron().shell.openPath(file);
            if (error) console.warn("PDF Outline Export: could not open the PDF:", error);
        } catch (error) {
            console.warn("PDF Outline Export: could not open the PDF:", error);
        }
    }

    /**
     * Returns an export dialog instance for this file.
     *
     * The dialog class is not part of the public API, but
     * `MarkdownView.prototype.printToPdf()` is a one-liner,
     * `new ExportDialog(this.app, this.file)`, so calling it with a stand-in
     * `this` is enough — no open tab is needed. `Modal.prototype.open` is
     * replaced for the duration of the call so the dialog never appears.
     */
    async createExportModal(file) {
        if (this.exportModalClass) {
            return new this.exportModalClass(this.app, file);
        }
        if (!MarkdownView || typeof MarkdownView.prototype.printToPdf !== "function") {
            throw new Error("this version of Obsidian does not expose PDF export");
        }
        let captured = null;
        const originalOpen = Modal.prototype.open;
        Modal.prototype.open = function () { captured = this; };
        try {
            MarkdownView.prototype.printToPdf.call({ app: this.app, file });
        } finally {
            Modal.prototype.open = originalOpen;
        }
        if (!captured || typeof captured.printToPdf !== "function") {
            throw new Error("could not access Obsidian's PDF export dialog");
        }
        this.exportModalClass = captured.constructor;
        return captured;
    }

    // ------------------------------------------------------------------
    //  Waiting for the page to settle before printing
    // ------------------------------------------------------------------

    /**
     * Delays printing until the print window has its math fonts and a stable
     * layout.
     *
     * The print window starts empty, and until the math font arrives Chromium
     * measures with a fallback — formulas print on top of each other. Obsidian
     * does not wait for anything between rendering and printing, so the
     * "print-to-pdf" IPC message is intercepted and held back. Its reply
     * handler is already subscribed at that point, so delaying it is safe.
     */
    holdPrintUntilFontsReady() {
        const originalOpen = window.open;
        window.open = (...args) => {
            const printWindow = originalOpen.apply(window, args);
            try {
                this.delayPrintIpc(printWindow);
            } catch (error) {
                console.warn("PDF Outline Export: font wait:", error);
            }
            return printWindow;
        };
        return () => { window.open = originalOpen; };
    }

    delayPrintIpc(printWindow, attempt = 0) {
        const ipc = printWindow && printWindow.electron && printWindow.electron.ipcRenderer;
        if (!ipc) {
            // `electron` appears in the new window a moment later; printing
            // starts at least 400 ms after the window opens, so there is time.
            if (attempt < 40) window.setTimeout(() => this.delayPrintIpc(printWindow, attempt + 1), 10);
            else console.warn("PDF Outline Export: no ipcRenderer in the print window");
            return;
        }
        const send = ipc.send.bind(ipc);
        ipc.send = (channel, payload) => {
            if (channel !== "print-to-pdf") return send(channel, payload);
            fontsSettled(printWindow, 10000)
                .then(() => this.waitForStableLayout(printWindow, 10000))
                .catch((error) => console.warn("PDF Outline Export: layout wait:", error))
                .then(() => send(channel, payload));
        };
    }

    /**
     * Waits until the page in the print window stops changing.
     *
     * Math is typeset separately and images load late; printing too early
     * misplaces formulas and adds pages. The page counts as settled once its
     * height, the number of rendered formulas and the number of loaded images
     * stay the same for several checks in a row.
     */
    async waitForStableLayout(printWindow, limitMs) {
        const deadline = Date.now() + limitMs;
        let previous = "";
        let stable = 0;
        while (Date.now() < deadline) {
            const signature = this.layoutSignature(printWindow);
            if (signature && signature === previous) {
                if (++stable >= 4) return;
            } else {
                stable = 0;
                previous = signature;
            }
            await wait(120);
        }
        console.warn("PDF Outline Export: the print window never settled");
    }

    layoutSignature(printWindow) {
        const doc = printWindow.document;
        const body = doc && doc.body;
        if (!body) return "";
        const images = Array.from(doc.images || []);
        const loaded = images.filter((image) => image.complete).length;
        const math = doc.querySelectorAll("mjx-container, .katex, .math").length;
        return `${body.scrollHeight}|${math}|${loaded}/${images.length}`;
    }

    /**
     * Renders one formula once per session so the math fonts are cached before
     * the print window opens. MathJax itself loads lazily on first use; if that
     * coincides with an export, part of the math is printed half-typeset (the
     * built-in export behaves the same way).
     */
    async warmUpFonts() {
        if (this.fontsWarm) return;
        this.fontsWarm = true;
        const host = document.body.createDiv({ cls: "pdf-outline-export-warmup" });
        try {
            if (typeof obsidian.loadMathJax === "function") await obsidian.loadMathJax();
            const markdown = "$$z = \\max\\{3x_1\\} \\le \\alpha \\quad \\sum_{i=1}^{n}\\frac{a}{b}\\sqrt{x}$$";
            await MarkdownRenderer.render(this.app, markdown, host, "", this);
            if (typeof obsidian.finishRenderMath === "function") await obsidian.finishRenderMath();
            await forceLoadMathFonts(window, 10000);
        } catch (error) {
            console.warn("PDF Outline Export: math warm-up:", error);
        } finally {
            host.detach();
        }
    }

    // ------------------------------------------------------------------
    //  Environment
    // ------------------------------------------------------------------

    vaultPath() {
        const adapter = this.app.vault.adapter;
        if (typeof adapter.getBasePath === "function") return adapter.getBasePath();
        return null;
    }

    electron() {
        const electron = window.electron || require("electron");
        return electron.remote || electron;
    }
};

class PdfOutlineExportSettingTab extends PluginSettingTab {
    constructor(app, plugin) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display() {
        const { containerEl } = this;
        containerEl.empty();

        containerEl.createEl("p", {
            cls: "setting-item-description",
            text: "Page size, margins, scale and the title option come from Obsidian's own "
                + "Export to PDF dialog. Change them there; this plugin reuses them.",
        });

        new Setting(containerEl)
            .setName("Show the outline panel when the PDF opens")
            .setDesc("Marks the file so PDF viewers open it with the bookmarks panel visible. "
                + "A few hundred bytes are appended to the file; the pages are not touched.")
            .addToggle((toggle) => toggle
                .setValue(this.plugin.settings.showOutlinePanel)
                .onChange(async (value) => {
                    this.plugin.settings.showOutlinePanel = value;
                    await this.plugin.saveSettings();
                }));

        new Setting(containerEl)
            .setName("Open the PDF after export")
            .setDesc("Opens the exported file in your default PDF viewer.")
            .addToggle((toggle) => toggle
                .setValue(this.plugin.settings.openAfterExport)
                .onChange(async (value) => {
                    this.plugin.settings.openAfterExport = value;
                    await this.plugin.saveSettings();
                }));
    }
}
