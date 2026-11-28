(() => {
  function mount() {
    const bar = document.querySelector(".topbar");
    if (!bar || bar.querySelector(".preview-window-buttons")) return;

    const drag = document.createElement("div");
    drag.className = "preview-window-drag";
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

    let origin;
    const canDrag = (event) => !event.target.closest("button, a, input, select, textarea");
    const brand = document.querySelector(".sidebar .brand");
    for (const area of [bar, brand]) {
      if (!area) continue;
      area.addEventListener("pointerdown", (event) => {
        if (event.button === 0 && canDrag(event)) origin = [event.clientX, event.clientY];
      });
      area.addEventListener("pointermove", (event) => {
        if (!origin || !(event.buttons & 1)) {
          origin = undefined;
          return;
        }
        if (Math.hypot(event.clientX - origin[0], event.clientY - origin[1]) < 4) return;
        origin = undefined;
        void window.pywebview.api.drag_window().catch(console.error);
      });
      area.addEventListener("pointerup", () => { origin = undefined; });
      area.addEventListener("pointerleave", () => { origin = undefined; });
      area.addEventListener("dblclick", (event) => {
        if (canDrag(event)) void toggleMaximize();
      });
    }
    bar.querySelector(".topbar-monitor")?.before(drag);
    bar.append(controls);
  }

  const css = document.createElement("style");
  css.textContent = `
    html, body { background-color: var(--surface-2, #f5f5f6); }
    .topbar, .sidebar .brand { user-select: none; }
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
