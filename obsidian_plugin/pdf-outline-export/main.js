"use strict";

/*
 * PDF outline export — плагін для Obsidian.
 *
 * Obsidian друкує PDF так: рендерер збирає об'єкт налаштувань і шле його
 * через IPC ("print-to-pdf"), а головний процес передає цей самий об'єкт
 * просто в Electron webContents.printToPDF(). Нічого дорогою не фільтрується.
 *
 * Chromium уміє сам складати дерево закладок із <h1>…<h6> — опція
 * generateDocumentOutline. Штатний експорт Obsidian її не вмикає, і саме
 * тому в експортованих PDF немає змісту.
 *
 * Плагін не підміняє рендеринг: він бере те саме внутрішнє вікно експорту,
 * що відкривається по «Export to PDF», і викликає його метод printToPdf()
 * зі своїм об'єктом налаштувань, куди додано generateDocumentOutline.
 * Тому верстка, шрифти, KaTeX і тема — байт у байт як у звичайного експорту.
 */

const obsidian = require("obsidian");
const { MarkdownRenderer, MarkdownView, Modal, Notice, Plugin, TFile } = obsidian;
const fs = require("fs");
const fsp = fs.promises;

const path = require("path");

/** Дія для посилань obsidian://pdf-outline-export?... */
const ACTION = "pdf-outline-export";

/**
 * Тека всередині сховища, куди тимчасово копіюються зовнішні .md.
 * Без крапки на початку: теки з крапкою Obsidian не індексує, а нам потрібен
 * саме проіндексований файл (TFile), щоб його можна було відкрити й надрукувати.
 * Живе вона секунди — прибирається одразу після друку й при завантаженні плагіна.
 */
const TEMP_FOLDER = "_pdf-outline-export-temp";

/** Формати аркуша, які розуміє Obsidian (ті самі, що у вікні експорту). */
const PAGE_SIZES = ["A3", "A4", "A5", "Legal", "Letter", "Tabloid"];

/** Поля у вікні експорту: 0 — звичайні поля, 1 — без полів, 2 — мінімальні. */
const MARGINS = { default: 0, none: 1, minimal: 2, "0": 0, "1": 1, "2": 2 };

/** Налаштування експорту, якими Obsidian користується, коли свої ще не збережені. */
const DEFAULT_EXPORT_SETTINGS = {
    includeName: true,
    pageSize: "A4",
    landscape: false,
    margin: "0",
    downscalePercent: 100,
};

/** Що з md-розмітки вважаємо локальним вкладенням, яке треба скопіювати поруч. */
const EMBED_PATTERNS = [
    /!\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]/g,   // ![[малюнок.png]]
    /!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, // ![alt](img/x.png)
    /<img[^>]+src\s*=\s*["']([^"']+)["']/gi,    // <img src="x.png">
];

function truthy(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback;
    if (typeof value === "boolean") return value;
    const text = String(value).trim().toLowerCase();
    if (["1", "true", "yes", "y", "on", "так"].includes(text)) return true;
    if (["0", "false", "no", "n", "off", "ні"].includes(text)) return false;
    return fallback;
}

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function wait(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/**
 * Змушує документ завантажити всі свої шрифти — але не довше за `limitMs`.
 *
 * Самого `fonts.ready` не досить: він відповідає миттєво, якщо верстка ще
 * не попросила жодного шрифту. А просить вона їх ліниво, вже під час
 * розкладки — і якщо надрукувати раніше, ширини рахуються запасним шрифтом:
 * формули налазять одна на одну, а сторінок виходить більше, ніж має бути.
 * Тому кожне накреслення вантажиться примусово.
 */
function fontsSettled(targetWindow, limitMs) {
    const document_ = targetWindow.document;
    const fonts = document_.fonts;
    if (!fonts) return Promise.resolve();
    // Шрифти замовляє саме розкладка, тож спершу змушуємо її порахуватися —
    // інакше `fonts.ready` відповість миттєво, бо чекати ще нема на що.
    // Вантажити примусово геть усі накреслення не можна: кожне завантаження
    // переверстує документ, і на великій нотатці це хвилини роботи.
    if (document_.body) void document_.body.scrollHeight;
    return Promise.race([
        Promise.resolve(fonts.ready).catch(() => {}),
        wait(limitMs),
    ]);
}

/**
 * Примусово вантажить шрифти математики (їх небагато — десяток накреслень).
 *
 * Чекати на них не досить: формули набрані так, що ширини беруться з
 * власних таблиць MathJax, тож коли шрифт спізнюється, сторінка не
 * переверстується — гліфи просто лягають не на свої місця. А якщо шрифт уже
 * в пам'яті, вікно друку бере його одразу.
 *
 * Решту накреслень чіпати не можна: кожне завантаження переверстує документ,
 * і на нотатці в 25 сторінок це хвилини роботи.
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
        // FontFaceSet не ітерується — лишається звичайне очікування.
    }
    return Promise.race([Promise.all(pending), wait(limitMs)]);
}

module.exports = class PdfOutlineExportPlugin extends Plugin {
    async onload() {
        /** Внутрішній клас вікна експорту: перехоплюється один раз і кешується. */
        this.exportModalClass = null;
        /** Дрібні негаразди поточного запиту — їдуть у відповідь разом зі статусом. */
        this.warnings = [];

        this.registerObsidianProtocolHandler(ACTION, (params) => {
            void this.handleRequest(params);
        });

        // Якщо минулого разу Obsidian закрили посеред експорту — приберемо хвіст.
        this.app.workspace.onLayoutReady(() => {
            void this.removeFolder(TEMP_FOLDER);
        });

        this.addCommand({
            id: "export-active-note",
            name: "Export to PDF with outline",
            checkCallback: (checking) => {
                const file = this.app.workspace.getActiveFile();
                if (!file || file.extension !== "md") return false;
                if (!checking) void this.exportActiveNote(file);
                return true;
            },
        });

        // Obsidian 1.13+ уміє приймати команди з терміналу; якщо CLI увімкнено,
        // те саме можна зробити через `obsidian export-pdf --file ... --out ...`.
        if (typeof this.registerCliHandler === "function") {
            this.registerCliHandler(
                "export-pdf",
                "Export a note to PDF with a document outline",
                {
                    file: { value: "<path>", description: "Note to export", required: true },
                    out: { value: "<path>", description: "Output PDF path" },
                    "page-size": { value: PAGE_SIZES.join("|"), description: "Page size (default: A4)" },
                    "include-name": { value: "yes|no", description: "Use the file name as a title (default: yes)" },
                    downscale: { value: "<10..100>", description: "Downscale percent (default: 100)" },
                    landscape: { description: "Landscape orientation" },
                    margin: { value: "default|minimal|none", description: "Page margins (default: default)" },
                },
                async (args) => {
                    const out = await this.exportToPdf({
                        src: args.file,
                        out: args.out,
                        pageSize: args["page-size"],
                        includeName: args["include-name"],
                        downscale: args.downscale,
                        landscape: args.landscape ? "1" : undefined,
                        margin: args.margin,
                    });
                    return `PDF saved to ${out}`;
                },
            );
        }
    }

    // ------------------------------------------------------------------
    //  Зовнішні виклики
    // ------------------------------------------------------------------

    /** Обробка посилання obsidian://pdf-outline-export?src=…&out=… */
    async handleRequest(params) {
        const status = params.status || (params.out ? `${params.out}.status` : null);
        const startedAt = Date.now();
        this.warnings = [];
        try {
            const out = await this.exportToPdf(params);
            new Notice(`PDF зі змістом: ${path.basename(out)}`);
            await this.writeStatus(status, {
                ok: true,
                out,
                ms: Date.now() - startedAt,
                printMs: this.lastPrintMs,
                stages: this.stages,
                warnings: this.warnings,
            });
        } catch (error) {
            const message = error && error.message ? error.message : String(error);
            console.error("pdf-outline-export:", error);
            new Notice(`Не вдалося експортувати PDF: ${message}`);
            await this.writeStatus(status, {
                ok: false,
                error: message,
                ms: Date.now() - startedAt,
                warnings: this.warnings,
            });
        }
    }

    warn(message) {
        console.warn("pdf-outline-export:", message);
        this.warnings.push(message);
    }

    /** Команда для активної нотатки: питає шлях і зберігає PDF зі змістом. */
    async exportActiveNote(file) {
        try {
            const dialog = this.electronRemote().dialog;
            const result = await dialog.showSaveDialog({
                defaultPath: `${file.basename}.pdf`,
                filters: [{ name: "PDF Files", extensions: ["pdf"] }],
                properties: ["showOverwriteConfirmation"],
            });
            if (result.canceled || !result.filePath) return;
            const saved = this.app.vault.getConfig("pdfExportSettings") || DEFAULT_EXPORT_SETTINGS;
            await this.printFile(file, this.buildOptions({
                pageSize: saved.pageSize,
                includeName: saved.includeName,
                downscale: saved.downscalePercent,
                landscape: saved.landscape,
                margin: saved.margin,
                open: "1",
            }, result.filePath));
            new Notice(`PDF зі змістом: ${path.basename(result.filePath)}`);
        } catch (error) {
            console.error("pdf-outline-export:", error);
            new Notice("Не вдалося експортувати PDF — подробиці в консолі.");
        }
    }

    // ------------------------------------------------------------------
    //  Власне експорт
    // ------------------------------------------------------------------

    /** Головний шлях: з параметрів до готового PDF. Повертає шлях до файлу. */
    async exportToPdf(params) {
        if (!params || !params.src) throw new Error("не вказано вхідний .md");
        const src = path.resolve(String(params.src));
        if (!fs.existsSync(src)) throw new Error(`немає файлу: ${src}`);

        const out = path.resolve(
            params.out ? String(params.out) : src.replace(/\.md$/i, "") + ".pdf",
        );
        await fsp.mkdir(path.dirname(out), { recursive: true });

        const options = this.buildOptions(params, out);
        const marks = this.stages = { locate: 0, print: 0, cleanup: 0 };
        let mark = Date.now();
        const { file, cleanup } = await this.locateInVault(src);
        marks.locate = Date.now() - mark;
        try {
            mark = Date.now();
            await this.printFile(file, options);
            marks.print = Date.now() - mark;
        } finally {
            mark = Date.now();
            await cleanup();
            marks.cleanup = Date.now() - mark;
        }
        if (!fs.existsSync(out)) throw new Error("Obsidian не створив файл");
        return out;
    }

    /**
     * Той самий об'єкт налаштувань, який будує вікно експорту Obsidian,
     * плюс дві опції Chromium, заради яких усе й затівалося.
     */
    buildOptions(params, out) {
        const pageSize = this.pickPageSize(params.pageSize || params["page-size"]);
        const marginsType = this.pickMargin(params.margin);
        const downscale = clamp(parseInt(params.downscale, 10) || 100, 10, 100);

        const options = {
            includeName: truthy(params.includeName ?? params["include-name"], true),
            pageSize,
            landscape: truthy(params.landscape, false),
            marginsType,
            scaleFactor: downscale,
            scale: clamp(downscale / 100, 0.1, 2),
            open: truthy(params.open, false),
            filepath: out,
            // Заради цих двох рядків усе й робиться: Chromium складає дерево
            // закладок із заголовків сторінки. Tagged PDF — умова, за якої
            // outline взагалі генерується. Вимикаються лише для порівняння
            // з «голим» експортом Obsidian (?outline=0&tagged=0).
            generateTaggedPDF: truthy(params.tagged, true),
            generateDocumentOutline: truthy(params.outline, true),
            // ?settle=0 друкує так само поспішно, як це робить сам Obsidian —
            // лишень для порівняння, коли треба перевірити, чи наші очікування
            // щось міняють.
            settle: truthy(params.settle, true),
        };
        if (marginsType === 1 || marginsType === 2) {
            options.margins = { top: 0, left: 0, bottom: 0, right: 0 };
        }
        return options;
    }

    pickPageSize(value) {
        if (!value) return "A4";
        const wanted = String(value).trim().toLowerCase();
        const found = PAGE_SIZES.find((size) => size.toLowerCase() === wanted);
        if (!found) throw new Error(`невідомий формат аркуша: ${value}`);
        return found;
    }

    pickMargin(value) {
        if (value === undefined || value === null || value === "") return 0;
        const key = String(value).trim().toLowerCase();
        if (!(key in MARGINS)) throw new Error(`невідомі поля сторінки: ${value}`);
        return MARGINS[key];
    }

    /**
     * Друк конкретного файлу сховища.
     *
     * Вікно експорту (внутрішній клас Obsidian) створюється, але не
     * показується: нам потрібен лише його метод printToPdf(options), який
     * і робить усю роботу — приховане вікно, світла тема, рендер нотатки,
     * IPC до головного процесу.
     */
    async printFile(file, options) {
        if (!this.app.vault.getConfig("pdfExportSettings")) {
            this.app.vault.setConfig("pdfExportSettings", { ...DEFAULT_EXPORT_SETTINGS });
        }
        let mark = Date.now();
        if (options.settle !== false) await this.warmUpFonts();
        if (this.stages) this.stages.warm = Date.now() - mark;
        mark = Date.now();
        const modal = await this.createExportModal(file);
        if (this.stages) this.stages.modal = Date.now() - mark;
        const startedAt = Date.now();
        const release = options.settle === false
            ? () => {}
            : this.holdPrintUntilFontsReady();
        try {
            await modal.printToPdf(options);
        } finally {
            release();
        }
        this.lastPrintMs = Date.now() - startedAt;
    }

    /**
     * Затримує друк, доки у вікні друку не завантажаться шрифти математики.
     *
     * Вікно створюється порожнім, і поки шрифт формул не приїхав, Chromium
     * рахує ширини запасним — формули друкуються одна на одній. Obsidian між
     * рендером і друком нічого не чекає, тож перехоплюємо саме повідомлення
     * «print-to-pdf»: обробник відповіді на той момент уже підписаний, тож
     * відкласти сам виклик безпечно.
     */
    holdPrintUntilFontsReady() {
        const originalOpen = window.open;
        window.open = (...args) => {
            const printWindow = originalOpen.apply(window, args);
            try {
                this.delayPrintIpc(printWindow);
            } catch (error) {
                this.warn(`очікування шрифтів: ${error.message}`);
            }
            return printWindow;
        };
        return () => { window.open = originalOpen; };
    }

    /**
     * Чекає, доки сторінка у вікні друку перестане змінюватися.
     *
     * Obsidian не чекає ні на що: він рендерить нотатку й одразу шле на друк.
     * Формули ж верстаються окремо (MathJax малює їх векторами), зображення
     * довантажуються — і якщо надрукувати зарано, половина формул лягає не на
     * свої місця, а сторінок виходить більше, ніж має бути.
     *
     * Ознака готовності складена з трьох чисел: висота тіла, кількість
     * намальованих формул і кількість завантажених зображень. Поки всі три
     * не постоять незмінними кілька замірів поспіль, друкувати рано.
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
        this.warn("сторінка у вікні друку так і не вгамувалася");
    }

    layoutSignature(printWindow) {
        const document_ = printWindow.document;
        const body = document_ && document_.body;
        if (!body) return "";
        const images = Array.from(document_.images || []);
        const loaded = images.filter((image) => image.complete).length;
        const math = document_.querySelectorAll("mjx-container, .katex, .math").length;
        return `${body.scrollHeight}|${math}|${loaded}/${images.length}`;
    }

    delayPrintIpc(printWindow, attempt = 0) {
        const ipc = printWindow.electron && printWindow.electron.ipcRenderer;
        if (!ipc) {
            // `electron` з'являється у вікні не миттєво; друк почнеться
            // щонайменше через 400 мс, тож часу вистачає.
            if (attempt < 40) window.setTimeout(() => this.delayPrintIpc(printWindow, attempt + 1), 10);
            else this.warn("у вікні друку не знайшлося ipcRenderer");
            return;
        }
        const send = ipc.send.bind(ipc);
        ipc.send = (channel, payload) => {
            if (channel !== "print-to-pdf") return send(channel, payload);
            const startedAt = Date.now();
            fontsSettled(printWindow, 10000)
                .then(() => this.waitForStableLayout(printWindow, 10000))
                .catch((error) => this.warn(`очікування верстки: ${error.message}`))
                .then(() => {
                    if (this.stages) this.stages.settle = Date.now() - startedAt;
                    send(channel, payload);
                });
        };
    }

    /**
     * Один раз за сеанс малює формулу, щоб шрифти математики потрапили в кеш
     * ще до того, як відкриється вікно друку. Чекати на них без обмеження не
     * можна: `fonts.ready` у головному вікні Obsidian висить близько хвилини.
     */
    async warmUpFonts() {
        if (this.fontsWarm) return;
        this.fontsWarm = true;
        const host = document.body.createDiv();
        host.style.cssText = "position:absolute;left:-9999px;top:-9999px;width:400px;opacity:0";
        try {
            // Формули малює MathJax, і вантажиться він лише тоді, коли вперше
            // знадобився. Якщо це збігається з експортом, частина формул іде
            // на друк недоверстаною — так само поводиться й штатний експорт.
            if (typeof obsidian.loadMathJax === "function") await obsidian.loadMathJax();
            const markdown = "$$z = \\max\\{3x_1\\} \\le \\alpha \\quad \\sum_{i=1}^{n}\\frac{a}{b}\\sqrt{x}$$";
            if (MarkdownRenderer && typeof MarkdownRenderer.render === "function") {
                await MarkdownRenderer.render(this.app, markdown, host, "", this);
            } else if (MarkdownRenderer && typeof MarkdownRenderer.renderMarkdown === "function") {
                await MarkdownRenderer.renderMarkdown(markdown, host, "", this);
            }
            if (typeof obsidian.finishRenderMath === "function") await obsidian.finishRenderMath();
            await forceLoadMathFonts(window, 10000);
        } catch (error) {
            this.warn(`прогрів математики: ${error.message}`);
        } finally {
            host.detach();
        }
    }

    /**
     * Повертає готовий екземпляр вікна експорту для цього файлу.
     *
     * Класу вікна в публічному API немає, але `MarkdownView.printToPdf()` з
     * нього складається з одного рядка `new КлассВікна(this.app, this.file)` —
     * тож достатньо викликати цей метод з підставленим `this`: ні вкладки,
     * ні відкритого файлу для цього не потрібно. Щоб вікно не з'явилося на
     * екрані, на час виклику підміняється `Modal.prototype.open`.
     */
    async createExportModal(file) {
        if (this.exportModalClass) {
            return new this.exportModalClass(this.app, file);
        }
        if (!MarkdownView || typeof MarkdownView.prototype.printToPdf !== "function") {
            throw new Error("ця версія Obsidian не дає доступу до експорту PDF");
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
            throw new Error("не вдалося перехопити вікно експорту PDF");
        }
        this.exportModalClass = captured.constructor;
        return captured;
    }

    // ------------------------------------------------------------------
    //  Файл поза сховищем
    // ------------------------------------------------------------------

    /**
     * Obsidian рендерить лише те, що лежить у сховищі. Файл зі сховища
     * беремо як є (працюють вкладення, посилання, теми), чужий — копіюємо
     * у тимчасову теку разом із малюнками, на які він посилається.
     */
    async locateInVault(src) {
        const base = this.vaultPath();
        if (base) {
            const relative = path.relative(base, src);
            if (relative && !relative.startsWith("..") && !path.isAbsolute(relative)) {
                const vaultPath = relative.split(path.sep).join("/");
                const file = this.app.vault.getAbstractFileByPath(vaultPath);
                if (file instanceof TFile) {
                    return { file, cleanup: async () => {} };
                }
            }
        }
        return this.importToVault(src);
    }

    /**
     * Кладе копію нотатки у сховище звичайними файловими операціями й чекає,
     * доки Obsidian її помітить. Через власне API сховища (`vault.create`)
     * це робити не варто: у щойно запущеному застосунку його черга файлових
     * операцій зависає на хвилину.
     */
    async importToVault(src) {
        const base = this.vaultPath();
        if (!base) throw new Error("сховище не на диску — зовнішній файл нікуди копіювати");
        const relative = `${TEMP_FOLDER}/${Date.now().toString(36)}`;
        const absolute = path.join(base, relative);
        await fsp.mkdir(absolute, { recursive: true });
        const text = await fsp.readFile(src, "utf8");
        await fsp.writeFile(path.join(absolute, path.basename(src)), text, "utf8");
        await this.copyAttachments(src, text, absolute);

        const file = await this.waitForVaultFile(`${relative}/${path.basename(src)}`);
        // Друк щойно створеного файлу чекає, доки сховище розбере посилання:
        // без цього Obsidian стоїть близько хвилини.
        await this.waitForMetadata(file);

        const cleanup = async () => {
            await this.removeFolder(relative);
            await this.removeFolderIfEmpty(TEMP_FOLDER);
        };
        return { file, cleanup };
    }

    /** Чекає, доки сховище помітить щойно створений на диску файл. */
    async waitForVaultFile(relativePath, timeoutMs = 15000) {
        const deadline = Date.now() + timeoutMs;
        for (;;) {
            const found = this.app.vault.getAbstractFileByPath(relativePath);
            if (found instanceof TFile) return found;
            if (Date.now() > deadline) {
                throw new Error("сховище не помітило тимчасову копію нотатки");
            }
            await wait(100);
        }
    }

    /** Чекає, доки metadataCache розбере щойно створений файл. */
    async waitForMetadata(file, timeoutMs = 15000) {
        const cache = this.app.metadataCache;
        if (cache.getFileCache(file)) return;
        await new Promise((resolve) => {
            let finished = false;
            const finish = () => {
                if (finished) return;
                finished = true;
                cache.offref(reference);
                window.clearTimeout(timer);
                resolve();
            };
            const reference = cache.on("resolve", (resolved) => {
                if (resolved && resolved.path === file.path) finish();
            });
            const timer = window.setTimeout(() => {
                this.warn("сховище не встигло проіндексувати тимчасову копію");
                finish();
            }, timeoutMs);
        });
    }

    /**
     * Прибирає тимчасову теку.
     *
     * Способи перебираються по черзі й кожен обмежений у часі: у щойно
     * запущеному Obsidian `vault.delete` висне на цілу хвилину й падає з
     * «File system operation timed out», тому першим іде найпростіше —
     * файлові операції. Лишати по собі сміття в чужому сховищі не можна,
     * тож здаємося лише тоді, коли не спрацював жоден спосіб.
     */
    async removeFolder(folderPath) {
        const base = this.vaultPath();
        const absolute = base ? path.join(base, folderPath) : null;
        const gone = () => (absolute ? !fs.existsSync(absolute) : false);
        if (gone()) return;

        const attempts = [
            // Спершу звичайне видалення: черга файлових операцій Obsidian у
            // щойно запущеному застосунку може висіти хвилину.
            ["fs.rm", () => (absolute ? fsp.rm(absolute, { recursive: true, force: true }) : null)],
            ["adapter.rmdir", () => this.app.vault.adapter.rmdir(folderPath, true)],
            ["vault.delete", () => {
                const folder = this.app.vault.getAbstractFileByPath(folderPath);
                return folder ? this.app.vault.delete(folder, true) : null;
            }],
        ];
        for (const [name, attempt] of attempts) {
            try {
                const running = Promise.resolve(attempt()).catch((error) => {
                    this.warn(`прибирання (${name}): ${error.message}`);
                });
                await Promise.race([running, wait(5000)]);
            } catch (error) {
                this.warn(`прибирання (${name}): ${error.message}`);
            }
            if (gone()) return;
        }
        this.warn(`тимчасова тека лишилася у сховищі: ${folderPath}`);
    }

    /** Прибирає корінь тимчасової теки, коли в ньому вже нічого немає. */
    async removeFolderIfEmpty(folderPath) {
        const base = this.vaultPath();
        if (!base) return;
        const absolute = path.join(base, folderPath);
        try {
            if (!fs.existsSync(absolute) || fs.readdirSync(absolute).length) return;
            await this.removeFolder(folderPath);
        } catch (error) {
            this.warn(`перевірка ${folderPath}: ${error.message}`);
        }
    }

    /** Копіює поруч малюнки, на які посилається нотатка відносними шляхами. */
    async copyAttachments(src, text, folderAbsolute) {
        const sourceDir = path.dirname(src);
        const seen = new Set();
        for (const pattern of EMBED_PATTERNS) {
            pattern.lastIndex = 0;
            let match;
            while ((match = pattern.exec(text)) !== null) {
                const raw = decodeURI(match[1].trim());
                if (!raw || /^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith("#")) continue;
                if (path.isAbsolute(raw)) continue;
                if (seen.has(raw)) continue;
                seen.add(raw);
                const from = path.resolve(sourceDir, raw);
                if (!from.startsWith(sourceDir) || !fs.existsSync(from)) continue;
                const target = path.join(folderAbsolute, raw);
                await fsp.mkdir(path.dirname(target), { recursive: true });
                await fsp.copyFile(from, target);
            }
        }
    }

    // ------------------------------------------------------------------
    //  Дрібниці
    // ------------------------------------------------------------------

    vaultPath() {
        const adapter = this.app.vault.adapter;
        if (typeof adapter.getBasePath === "function") return adapter.getBasePath();
        return adapter.basePath || null;
    }

    electronRemote() {
        const electron = window.electron || require("electron");
        return electron.remote || electron;
    }

    /** Маленький файл поруч із PDF: за ним .bat розуміє, чим скінчилося. */
    async writeStatus(statusPath, payload) {
        if (!statusPath) return;
        try {
            await fsp.mkdir(path.dirname(statusPath), { recursive: true });
            await fsp.writeFile(statusPath, JSON.stringify(payload), "utf8");
        } catch (error) {
            console.error("pdf-outline-export: не вдалося записати статус", error);
        }
    }
};
