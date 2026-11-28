(() => {
  function mount() {
    const bar = document.querySelector(".topbar");
    if (!bar || bar.querySelector(".preview-window-buttons")) return;

    const drag = document.createElement("div");
    drag.className = "preview-window-drag pywebview-drag-region";
    const controls = document.createElement("div");
    controls.className = "preview-window-buttons";
    let maximized = false;

    function button(symbol, label, run) {
      const el = document.createElement("button");
      el.type = "button";
      el.textContent = symbol;
      el.title = label;
      el.setAttribute("aria-label", label);
      el.addEventListener("click", run);
      controls.append(el);
      return el;
    }

    button("−", "最小化", () => window.pywebview.api.minimize());
    const maximize = button("□", "最大化", toggleMaximize);
    button("×", "关闭窗口", () => window.pywebview.api.destroy());

    async function toggleMaximize() {
      await window.pywebview.api[maximized ? "restore" : "maximize"]();
      maximized = !maximized;
      maximize.textContent = maximized ? "❐" : "□";
      maximize.title = maximized ? "还原窗口" : "最大化";
      maximize.setAttribute("aria-label", maximize.title);
    }

    drag.addEventListener("dblclick", toggleMaximize);
    bar.querySelector(".topbar-monitor")?.before(drag);
    bar.append(controls);
  }

  const css = document.createElement("style");
  css.textContent = `
    html, body { background-color: var(--surface-2, #f5f5f6); }
    .preview-window-drag { align-self: stretch; flex: 1; min-width: 36px; }
    .preview-window-buttons { display: flex; align-self: stretch; margin-left: 12px; }
    .preview-window-buttons button {
      width: 42px; border: 0; background: transparent; color: inherit;
      font: 18px system-ui; cursor: default;
    }
    .preview-window-buttons button:hover { background: var(--surface-3, #e9e9eb); }
    .preview-window-buttons button:last-child:hover { background: #c42b1c; color: white; }
  `;
  document.head.append(css);
  mount();
  new MutationObserver(mount).observe(document.body, { childList: true, subtree: true });
})();
