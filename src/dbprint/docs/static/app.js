// Client-side behaviour for the docs site: tables, tabs, sidebar, chart panels.

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table[data-enhance]").forEach(enhanceTable);
  initFilter();
  initTabs();
  initSidebar();
  initCharts();
  document.querySelector(".sidebar .nav-current")?.scrollIntoView({ block: "center" });
});

// Column anchors (#col-x) live in the "columns" panel. Switching tabs waits for `load`:
// mermaid renders around DOMContentLoaded and its text measurement fails inside a hidden panel.
window.addEventListener("load", () => {
  if (location.hash.startsWith("#col-")) jumpToColumn(location.hash);
});

function jumpToColumn(hash) {
  activateTab("columns");
  document.getElementById(hash.slice(1))?.scrollIntoView({ block: "center" });
}

function activateTab(name) {
  const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  if (!btn) return;

  for (const b of document.querySelectorAll(".tab-btn")) b.classList.toggle("active", b === btn);
  for (const panel of document.querySelectorAll("[data-tab-panel]")) {
    panel.hidden = panel.dataset.tabPanel !== name;
  }

  if (!btn.dataset.fitted) {
    btn.dataset.fitted = "1";
    document.querySelectorAll(`[data-tab-panel="${name}"] .boxplot-labels`).forEach(fitBoxplotLabels);
  }
}

function initTabs() {
  const tabs = document.querySelectorAll(".tab-btn");
  if (!tabs.length) return;

  for (const btn of tabs) {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  }

  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#col-"]');
    if (!a) return;
    e.preventDefault();
    jumpToColumn(a.getAttribute("href"));
    history.replaceState(null, "", a.getAttribute("href"));
  });

  activateTab("overview");
}

// Priority tiers, most- to least-inclusive.
function fitBoxplotLabels(container) {
  const [lo, p25, median, p75, hi] = container.children;

  function overlaps(visible) {
    for (let i = 1; i < visible.length; i++) {
      if (visible[i - 1].getBoundingClientRect().right + 4 > visible[i].getBoundingClientRect().left) return true;
    }
    return false;
  }

  function show(...visible) {
    for (const span of container.children) span.style.display = visible.includes(span) ? "" : "none";
  }

  for (const tier of [[lo, p25, median, p75, hi], [p25, median, p75], [median]]) {
    show(...tier);
    if (!overlaps(tier)) return;
  }
}

function enhanceTable(table) {
  const tbody = table.tBodies[0];
  const groupSelect = document.getElementById("group-by");
  let rows = [...tbody.rows];
  let groupKey = groupSelect ? groupSelect.value : "";

  function render() {
    tbody.innerHTML = "";
    if (!groupKey) {
      rows.forEach((r) => tbody.appendChild(r));
      return;
    }
    const groups = new Map();
    for (const r of rows) {
      const key = r.dataset[groupKey] || "(none)";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    }
    const span = table.tHead.rows[0].cells.length;
    for (const key of [...groups.keys()].sort()) {
      const header = document.createElement("tr");
      header.className = "group-header";
      const label = key.replace(/_/g, "\u00A0");
      header.innerHTML = `<td colspan="${span}"><strong>${label}</strong> <span class="muted">(${groups.get(key).length})</span></td>`;
      tbody.appendChild(header);
      groups.get(key).forEach((r) => tbody.appendChild(r));
    }
  }

  table.querySelectorAll("th[data-sort]").forEach((th) => {
    let asc = true;
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const numeric = th.dataset.sortType === "num";
      rows = [...rows].sort((a, b) => {
        let av = a.dataset[key] ?? "";
        let bv = b.dataset[key] ?? "";
        if (numeric) { av = parseFloat(av) || 0; bv = parseFloat(bv) || 0; }
        return (av > bv ? 1 : av < bv ? -1 : 0) * (asc ? 1 : -1);
      });
      asc = !asc;
      render();
    });
  });

  if (groupSelect) {
    groupSelect.addEventListener("change", () => {
      groupKey = groupSelect.value;
      render();
    });
  }

  render();
}

function initFilter() {
  const input = document.getElementById("q");
  if (!input) return;
  input.oninput = (e) => {
    const q = e.target.value.toLowerCase();
    for (const row of document.querySelectorAll("tbody tr")) {
      row.hidden = !row.textContent.toLowerCase().includes(q);
    }
  };
}

const SIDEBAR_COLLAPSE_KEY = "dbprint-docs-sidebar-collapsed";
const SIDEBAR_BREAKPOINT = 960;

// The breakpoint decides by default; a manual toggle overrides it, persisted in localStorage.
function initSidebar() {
  const layout = document.querySelector(".layout");
  const toggle = document.getElementById("sidebar-toggle");
  if (!layout || !toggle) return;

  let manual = localStorage.getItem(SIDEBAR_COLLAPSE_KEY);
  const apply = (collapsed) => layout.classList.toggle("sidebar-collapsed", collapsed);

  apply(manual !== null ? manual === "1" : window.innerWidth < SIDEBAR_BREAKPOINT);

  toggle.addEventListener("click", () => {
    const collapsed = !layout.classList.contains("sidebar-collapsed");
    apply(collapsed);
    manual = collapsed ? "1" : "0";
    localStorage.setItem(SIDEBAR_COLLAPSE_KEY, manual);
  });

  window.addEventListener("resize", () => {
    if (manual !== null) return;
    apply(window.innerWidth < SIDEBAR_BREAKPOINT);
  });
}

const CHART_ZOOM_MIN = 0.3;
const CHART_ZOOM_MAX = Infinity;
const CHART_ZOOM_STEP = 1.2;
const CHART_PAN_STEP = 60;
const FS_ENTER_ICON = '<span class="fs-icon" aria-hidden="true"></span>';
const FS_EXIT_ICON = "&times;";
const CHART_PAN_DELTAS = { up: [0, 1], down: [0, -1], left: [1, 0], right: [-1, 0] };

let activeChartPanel = null; // { panel, state, applyTransform } while any panel is fullscreen

// Fullscreen via a CSS overlay; zoom and pan apply only to a `data-zoomable` panel.
function initCharts() {
  document.querySelectorAll(".chart-panel[data-chart]").forEach(setUpChartPanel);
  document.addEventListener("keydown", handleChartKeydown);
}

function setUpChartPanel(panel) {
  const fsBtn = panel.querySelector("[data-fs-toggle]");
  const zoomArea = panel.querySelector(".chart-zoom-area");
  const state = { scale: 1, x: 0, y: 0 };

  function applyTransform() {
    if (zoomArea) zoomArea.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.scale})`;
  }

  function setFullscreen(on) {
    panel.classList.toggle("fs-active", on);
    document.body.classList.toggle("chart-fs-open", on);
    fsBtn.innerHTML = on ? FS_EXIT_ICON : FS_ENTER_ICON;
    fsBtn.title = fsBtn.ariaLabel = on ? "Close full screen" : "Full screen";
    activeChartPanel = on ? { panel, state, applyTransform } : null;

    if (!on) {
      state.scale = 1;
      state.x = 0;
      state.y = 0;
      applyTransform();
    }
  }

  fsBtn.addEventListener("click", () => setFullscreen(!panel.classList.contains("fs-active")));

  panel.querySelectorAll("[data-zoom]").forEach((btn) => {
    btn.addEventListener("click", () => {
      zoomChart(state, btn.dataset.zoom === "in" ? CHART_ZOOM_STEP : 1 / CHART_ZOOM_STEP);
      applyTransform();
    });
  });

  panel.querySelectorAll("[data-pan]").forEach((btn) => {
    btn.addEventListener("click", () => {
      panChart(state, btn.dataset.pan);
      applyTransform();
    });
  });

  panel._dbprintChart = { setFullscreen };
}

function zoomChart(state, factor) {
  state.scale = Math.min(CHART_ZOOM_MAX, Math.max(CHART_ZOOM_MIN, state.scale * factor));
}

function panChart(state, direction) {
  const [dx, dy] = CHART_PAN_DELTAS[direction] || [0, 0];
  state.x += dx * CHART_PAN_STEP;
  state.y += dy * CHART_PAN_STEP;
}

function handleChartKeydown(e) {
  if (!activeChartPanel) return;
  const { panel, state, applyTransform } = activeChartPanel;

  if (e.key === "Escape") {
    panel._dbprintChart.setFullscreen(false);
    return;
  }

  if (!panel.hasAttribute("data-zoomable")) return;

  const panKey = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[e.key];

  if (e.key === "+" || e.key === "=") {
    zoomChart(state, CHART_ZOOM_STEP);
  } else if (e.key === "-") {
    zoomChart(state, 1 / CHART_ZOOM_STEP);
  } else if (panKey) {
    panChart(state, panKey);
  } else {
    return;
  }

  e.preventDefault();
  applyTransform();
}
