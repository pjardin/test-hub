/* Read-only syntax highlighting for code shown outside the editor
   (test pages, recorder output). Uses CodeMirror's runMode addon with the
   same token colors as the editor, so code looks identical everywhere. */
(function () {
  "use strict";
  function highlight(el) {
    if (typeof CodeMirror === "undefined" || !CodeMirror.runMode) return;
    const text = el.textContent;
    el.textContent = "";
    el.classList.add("cm-s-default");
    // data-mode="javascript" on the element switches highlighting for
    // TypeScript tests; python stays the default everywhere else.
    CodeMirror.runMode(text, el.dataset.mode || "python", el);
  }
  window.hubHighlight = highlight;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("pre.code-view").forEach(highlight);
  });
})();
