/* The test code editor: CodeMirror 5 (vendored, fully offline) with Python
   highlighting, bracket handling, Ctrl-F search, and autocomplete that knows
   this hub's API -- type `page.` or `ctx.` (or press Ctrl-Space) to browse
   what's available, with signatures and one-line docs. */
(function () {
  "use strict";
  const textarea = document.getElementById("id_code");
  if (!textarea || typeof CodeMirror === "undefined") return;

  /* ---- the API dictionary (shown as `display` -> inserts `text`) ---------- */
  const PAGE_API = [
    ["goto(ctx.base_url)", "goto(", "navigate to a URL (auto-timed)"],
    ["click(selector)", "click(\"", "click an element"],
    ["dblclick(selector)", "dblclick(\"", "double-click an element"],
    ["fill(selector, value)", "fill(\"", "clear + type into an input"],
    ["type(selector, text)", "type(\"", "type character by character"],
    ["press(selector, key)", "press(\"", "press a key, e.g. Enter"],
    ["check(selector)", "check(\"", "tick a checkbox"],
    ["uncheck(selector)", "uncheck(\"", "untick a checkbox"],
    ["set_checked(selector, bool)", "set_checked(\"", "set checkbox state"],
    ["select_option(selector, value)", "select_option(\"", "choose a <select> option"],
    ["hover(selector)", "hover(\"", "hover an element"],
    ["focus(selector)", "focus(\"", "focus an element"],
    ["drag_and_drop(source, target)", "drag_and_drop(\"", "drag one element onto another"],
    ["set_input_files(selector, path)", "set_input_files(\"", "attach a file to <input type=file>"],
    ["wait_for_selector(selector)", "wait_for_selector(\"", "wait until an element exists/visible"],
    ["wait_for_url(url_glob)", "wait_for_url(\"", "wait until the URL matches"],
    ["wait_for_load_state()", "wait_for_load_state()", "wait for load/networkidle"],
    ["wait_for_timeout(ms)", "wait_for_timeout(", "hard wait (avoid when possible)"],
    ["wait_for_function(js)", "wait_for_function(\"", "wait until JS returns truthy"],
    ["inner_text(selector)", "inner_text(\"", "visible text of an element"],
    ["text_content(selector)", "text_content(\"", "raw text content"],
    ["inner_html(selector)", "inner_html(\"", "innerHTML of an element"],
    ["get_attribute(selector, name)", "get_attribute(\"", "read an attribute"],
    ["is_visible(selector)", "is_visible(\"", "True if visible right now"],
    ["is_checked(selector)", "is_checked(\"", "checkbox state"],
    ["locator(selector)", "locator(\"", "build a Locator for chained actions"],
    ["title()", "title()", "the page title"],
    ["url", "url", "current URL (property)"],
    ["content()", "content()", "full HTML of the page"],
    ["evaluate(js)", "evaluate(\"", "run JavaScript in the page"],
    ["screenshot(path=...)", "screenshot(path=", "raw screenshot (prefer ctx.screenshot)"],
    ["reload()", "reload()", "reload the page"],
    ["go_back()", "go_back()", "history back"],
    ["keyboard.type(text)", "keyboard.type(\"", "type at the current focus"],
    ["keyboard.press(key)", "keyboard.press(\"", "press a key globally"],
    ["mouse.wheel(dx, dy)", "mouse.wheel(0, ", "scroll the page"],
    ["set_default_timeout(ms)", "set_default_timeout(", "per-action timeout for this page"],
  ];
  const CTX_API = [
    ["base_url", "base_url", "the target URL from config.json"],
    ["log(message)", "log(\"", "timestamped line in the run log"],
    ["screenshot(name)", "screenshot(\"", "named screenshot into the artifacts"],
    ["timed(label):  (with-block)", "timed(\"", "time a block: with ctx.timed(\"login\"):"],
    ["test_id", "test_id", "this test's id"],
    ["artifacts_dir", "artifacts_dir", "folder for this run's files"],
  ];
  const SNIPPETS = [
    ["assert text present", 'assert "expected" in page.inner_text("#result"), "missing result"'],
    ["wait then assert", 'page.wait_for_selector("#result", state="visible", timeout=10000)\nassert "expected" in page.inner_text("#result")'],
    ["timed block", 'with ctx.timed("phase name"):\n    page.wait_for_selector("#done", timeout=60000)'],
    ["new test skeleton", 'def run(page, ctx):\n    page.goto(ctx.base_url)\n    ctx.screenshot("loaded")\n    assert page.title(), "page has no title"'],
  ];
  const PY_KEYWORDS = ("def return if elif else for while with as in not and or True False None "
    + "import from try except finally raise pass break continue assert lambda class").split(" ");

  function hintList(cm) {
    const cur = cm.getCursor();
    const line = cm.getLine(cur.line).slice(0, cur.ch);
    const dotMatch = /(page|ctx)\.([A-Za-z_]*)$/.exec(line);
    const wordMatch = /([A-Za-z_][A-Za-z0-9_]*)$/.exec(line);
    let list = [], from = cur, to = cur;

    function mk(items, prefix) {
      return items
        .filter(([disp, text]) => !prefix || text.toLowerCase().startsWith(prefix.toLowerCase())
                                  || disp.toLowerCase().startsWith(prefix.toLowerCase()))
        .map(([disp, text, doc]) => ({
          text, displayText: disp + (doc ? "  — " + doc : ""),
        }));
    }

    if (dotMatch) {
      const api = dotMatch[1] === "page" ? PAGE_API : CTX_API;
      list = mk(api, dotMatch[2]);
      from = CodeMirror.Pos(cur.line, cur.ch - dotMatch[2].length);
    } else if (wordMatch) {
      const prefix = wordMatch[1];
      list = PY_KEYWORDS.filter(k => k.startsWith(prefix)).map(k => ({ text: k }));
      list = list.concat(
        SNIPPETS.filter(([n]) => n.toLowerCase().includes(prefix.toLowerCase()))
                .map(([n, code]) => ({ text: code, displayText: "▶ " + n })));
      if (("page").startsWith(prefix)) list.unshift({ text: "page." });
      if (("ctx").startsWith(prefix)) list.unshift({ text: "ctx." });
      from = CodeMirror.Pos(cur.line, cur.ch - prefix.length);
    } else {
      list = SNIPPETS.map(([n, code]) => ({ text: code, displayText: "▶ " + n }));
    }
    return { list, from, to };
  }

  const editor = CodeMirror.fromTextArea(textarea, {
    // "python" or "javascript" -- the server stamps the test's language
    mode: textarea.dataset.mode || "python",
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    indentWithTabs: false,
    matchBrackets: true,
    autoCloseBrackets: true,
    styleActiveLine: true,
    viewportMargin: Infinity,
    extraKeys: {
      "Ctrl-Space": (cm) => {
        if (cm.getOption("mode") === "python")
          cm.showHint({ hint: hintList, completeSingle: false });
      },
      "Cmd-/": "toggleComment",
      "Ctrl-/": "toggleComment",
      Tab: (cm) => {
        if (cm.somethingSelected()) cm.indentSelection("add");
        else cm.replaceSelection("    ", "end");
      },
    },
  });
  editor.setSize(null, 420);

  // auto-open the API list right after typing `page.` or `ctx.` --
  // python mode only: the hint list is the PYTHON harness API, and
  // offering page.fill(...) python signatures inside a .spec.ts misleads
  editor.on("inputRead", (cm, change) => {
    if (cm.getOption("mode") !== "python") return;
    if (change.text.length === 1 && change.text[0] === ".") {
      const line = cm.getLine(change.to.line).slice(0, change.to.ch + 1);
      if (/(page|ctx)\.$/.test(line)) {
        cm.showHint({ hint: hintList, completeSingle: false });
      }
    }
  });

  // keep the underlying <textarea name="code"> in sync for the form POST
  const form = textarea.form;
  if (form) form.addEventListener("submit", () => editor.save());

  // recorders and other scripts set code through this
  window.hubEditor = editor;
  window.hubSetCode = (code) => {
    editor.setValue(code);
    editor.save();
  };
  window.hubEditor = editor;
})();
