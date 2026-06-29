import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pymongo import ASCENDING, DESCENDING, MongoClient
import csv
import io
import json

from seo_checker import load_config as load_seo_config
from seo_checker import load_domains, rebuild_rankings, run_checks_incremental, save_results


load_dotenv(override=True)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017").strip()
MONGO_DB = os.getenv("MONGO_DB", "expired_domains_db").strip()
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "expired_domains").strip()
API_KEY = os.getenv("API_KEY", "").strip()
SEO_COLLECTION = os.getenv("SEO_COLLECTION", "domain_seo_checks").strip()
SEO_RANKING_COLLECTION = os.getenv("SEO_RANKING_COLLECTION", "domain_seo_rankings").strip()

app = FastAPI(title="Expired Domains API", version="1.0.0")
seo_jobs: dict[str, dict[str, Any]] = {}
seo_jobs_lock = threading.Lock()
scraper_process: subprocess.Popen | None = None
scraper_log_handle: Any | None = None
scraper_lock = threading.RLock()
PROJECT_DIR = Path(__file__).resolve().parent
MAIN_SCRIPT = PROJECT_DIR / "main.py"
MAIN_LOG = PROJECT_DIR / "main_process.log"
MAIN_STATUS_FILE = PROJECT_DIR / os.getenv("MAIN_STATUS_FILE", "main_status.json")
EXPORT_DOMAINS_FILE = PROJECT_DIR / os.getenv("EXPORT_DOMAINS_FILE", "expired_domains_export.csv")

GUI_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Expired Domains Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --panel-soft: #eef3f7;
      --ink: #17202a;
      --muted: #5d6b7a;
      --line: #d8e0e7;
      --blue: #1d6fb8;
      --green: #16845f;
      --red: #c83c3c;
      --amber: #ad7412;
      --shadow: 0 12px 28px rgba(22, 32, 42, 0.08);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    button, input, select {
      font: inherit;
    }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
    }

    .sidebar {
      background: #17202a;
      color: #f8fafc;
      padding: 24px 18px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .brand {
      display: grid;
      gap: 4px;
      padding: 0 6px 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.14);
    }

    .brand strong {
      font-size: 19px;
      line-height: 1.25;
      letter-spacing: 0;
    }

    .brand span {
      color: #aebdca;
      font-size: 13px;
    }

    .nav {
      display: grid;
      gap: 8px;
    }

    .nav button {
      width: 100%;
      min-height: 40px;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #d9e2ec;
      text-align: left;
      padding: 9px 11px;
      cursor: pointer;
    }

    .nav button.active,
    .nav button:hover {
      background: rgba(255, 255, 255, 0.11);
      color: #ffffff;
    }

    .main {
      min-width: 0;
      display: flex;
      flex-direction: column;
    }

    .topbar {
      min-height: 76px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 28px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }

    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0;
    }

    .subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }

    .content {
      min-width: 0;
      width: 100%;
      max-width: 1440px;
      padding: 24px 28px 36px;
      display: grid;
      gap: 18px;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }

    .btn {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      padding: 8px 12px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      white-space: nowrap;
    }

    .btn.primary {
      background: var(--blue);
      border-color: var(--blue);
      color: #ffffff;
    }

    .btn.danger {
      background: var(--red);
      border-color: var(--red);
      color: #ffffff;
    }

    .btn.success {
      background: var(--green);
      border-color: var(--green);
      color: #ffffff;
    }

    .btn:disabled {
      opacity: 0.58;
      cursor: wait;
    }

    .grid {
      display: grid;
      gap: 14px;
    }

    .stats {
      grid-template-columns: repeat(4, minmax(150px, 1fr));
    }

    .stat {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
      min-height: 112px;
      display: grid;
      align-content: space-between;
      gap: 12px;
    }

    .stat span {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .stat strong {
      font-size: 28px;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }

    .status-pill {
      display: inline-flex;
      min-height: 30px;
      align-items: center;
      gap: 8px;
      padding: 5px 10px;
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      font-weight: 650;
      font-size: 13px;
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--muted);
      flex: 0 0 auto;
    }

    .dot.on { background: var(--green); }
    .dot.off { background: var(--red); }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel-head {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }

    .panel-head h2 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }

    .filters {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
    }

    input, select {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #ffffff;
      color: var(--ink);
      min-width: 0;
    }

    .table-wrap {
      width: 100%;
      overflow: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }

    th, td {
      padding: 11px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }

    th {
      color: var(--muted);
      background: #f8fafc;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    td {
      overflow-wrap: anywhere;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
    }

    pre {
      margin: 0;
      min-height: 260px;
      max-height: 420px;
      overflow: auto;
      white-space: pre-wrap;
      background: #101820;
      color: #dbe7f0;
      padding: 16px;
      line-height: 1.5;
      font-size: 12px;
    }

    .hidden { display: none !important; }

    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: min(420px, calc(100vw - 36px));
      background: #17202a;
      color: #ffffff;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: var(--shadow);
      font-size: 13px;
      z-index: 20;
    }

    .empty {
      padding: 28px 16px;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 900px) {
      .app {
        grid-template-columns: 1fr;
      }

      .sidebar {
        position: sticky;
        top: 0;
        z-index: 10;
        padding: 14px;
      }

      .brand {
        padding-bottom: 12px;
      }

      .nav {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }

      .nav button {
        text-align: center;
        padding-inline: 8px;
      }

      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 18px;
      }

      .toolbar {
        justify-content: flex-start;
      }

      .content {
        padding: 18px;
      }

      .stats {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 560px) {
      .stats {
        grid-template-columns: 1fr;
      }

      .nav {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .panel-head {
        align-items: stretch;
        flex-direction: column;
      }

      .filters {
        justify-content: stretch;
      }

      .filters > * {
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <strong>Expired Domains</strong>
        <span>Scraper control center</span>
      </div>
      <nav class="nav" aria-label="Dashboard sections">
        <button class="active" data-tab="overview">Overview</button>
        <button data-tab="domains">Domains</button>
        <button data-tab="seo">SEO</button>
        <button data-tab="logs">Logs</button>
      </nav>
    </aside>

    <main class="main">
      <header class="topbar">
        <div>
          <h1 id="pageTitle">Overview</h1>
          <div class="subtitle" id="pageSubtitle">Monitor the scraper and exported domains.</div>
        </div>
        <div class="toolbar">
          <span class="status-pill"><span id="runDot" class="dot"></span><span id="runText">Checking</span></span>
          <button class="btn success" id="startBtn" type="button">Start</button>
          <button class="btn primary" id="restartBtn" type="button">Run Again</button>
          <button class="btn danger" id="stopBtn" type="button">Stop</button>
          <button class="btn" id="refreshBtn" type="button">Refresh</button>
        </div>
      </header>

      <section class="content" id="overviewTab">
        <div class="grid stats">
          <div class="stat"><span>Database Domains</span><strong id="dbDomains">-</strong></div>
          <div class="stat"><span>Export CSV Rows</span><strong id="csvDomains">-</strong></div>
          <div class="stat"><span>Last Batch</span><strong id="lastBatch">-</strong></div>
          <div class="stat"><span>Exported This Run</span><strong id="exportedRows">-</strong></div>
        </div>
        <section class="panel">
          <div class="panel-head">
            <h2>Live Status</h2>
            <span class="status-pill" id="nextRun">Next run: -</span>
          </div>
          <div class="table-wrap">
            <table>
              <tbody id="statusTable"></tbody>
            </table>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Recent Batches</h2>
            <button class="btn" id="loadBatchesBtn" type="button">Load Batches</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Batch</th><th>Count</th><th>Exported At</th></tr></thead>
              <tbody id="batchesBody"><tr><td colspan="3" class="empty">No batches loaded yet.</td></tr></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="content hidden" id="domainsTab">
        <section class="panel">
          <div class="panel-head">
            <h2>Domains</h2>
            <div class="filters">
              <input id="batchInput" type="number" min="1" placeholder="Batch">
              <select id="domainLimit">
                <option value="50">50 rows</option>
                <option value="100" selected>100 rows</option>
                <option value="250">250 rows</option>
                <option value="500">500 rows</option>
              </select>
              <button class="btn primary" id="loadDomainsBtn" type="button">Load</button>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Domain</th><th>Batch</th><th>Exported At</th><th>Data</th></tr></thead>
              <tbody id="domainsBody"><tr><td colspan="4" class="empty">Load domains to view results.</td></tr></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="content hidden" id="seoTab">
        <section class="panel">
          <div class="panel-head">
            <h2>SEO Rankings</h2>
            <div class="filters">
              <input id="seoSearch" type="search" placeholder="Domain contains">
              <input id="minScore" type="number" min="0" max="100" placeholder="Min score">
              <select id="reachableFilter">
                <option value="">Any reachability</option>
                <option value="true">Reachable</option>
                <option value="false">Unreachable</option>
              </select>
              <button class="btn success" id="runSeoBtn" type="button">Run SEO Check</button>
              <button class="btn primary" id="loadSeoBtn" type="button">Load</button>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Domain</th><th>Score</th><th>Reachable</th><th>Title</th><th>Checked</th></tr></thead>
              <tbody id="seoBody"><tr><td colspan="5" class="empty">Load SEO checks to view results.</td></tr></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="content hidden" id="logsTab">
        <section class="panel">
          <div class="panel-head">
            <h2>Process Logs</h2>
            <div class="filters">
              <select id="logLines">
                <option value="100" selected>100 lines</option>
                <option value="250">250 lines</option>
                <option value="500">500 lines</option>
                <option value="1000">1000 lines</option>
              </select>
              <button class="btn primary" id="loadLogsBtn" type="button">Load Logs</button>
            </div>
          </div>
          <pre id="logsOutput">Logs will appear here.</pre>
        </section>
      </section>
    </main>
  </div>
  <div id="toast" class="toast hidden"></div>

  <script>
    const API_KEY = __API_KEY__;
    const state = { currentTab: "overview", busy: false, lastSeoJob: null };

    const $ = (id) => document.getElementById(id);
    const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);
    const fmtDate = (value) => {
      if (!value) return "-";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
    };
    const esc = (value) => text(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function toast(message) {
      const node = $("toast");
      node.textContent = message;
      node.classList.remove("hidden");
      clearTimeout(toast.timer);
      toast.timer = setTimeout(() => node.classList.add("hidden"), 3500);
    }

    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (API_KEY) headers["200m-API-Key"] = API_KEY;
      const response = await fetch(path, { ...options, headers });
      const type = response.headers.get("content-type") || "";
      const body = type.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) {
        const detail = body && body.detail ? body.detail : response.statusText;
        throw new Error(detail);
      }
      return body;
    }

    function setBusy(busy) {
      state.busy = busy;
      ["startBtn", "restartBtn", "stopBtn", "refreshBtn", "loadDomainsBtn", "loadSeoBtn", "runSeoBtn", "loadLogsBtn", "loadBatchesBtn"].forEach((id) => {
        const node = $(id);
        if (node) node.disabled = busy;
      });
    }

    function renderStatus(data) {
      const running = Boolean(data.running);
      $("runDot").className = "dot " + (running ? "on" : "off");
      $("runText").textContent = running ? `Running${data.pid ? " PID " + data.pid : ""}` : "Stopped";
      $("dbDomains").textContent = text(data.db_domains);
      $("csvDomains").textContent = text(data.export_csv_domains);
      const live = data.live_status || {};
      $("lastBatch").textContent = text(live.batch || live.last_batch);
      $("exportedRows").textContent = text(live.exported_rows || live.last_exported_rows);
      $("nextRun").textContent = live.next_run_in_seconds === undefined ? "Next run: -" : `Next run: ${Math.max(0, live.next_run_in_seconds)}s`;

      const rows = [
        ["Script", data.script],
        ["Log file", data.log],
        ["Export file", data.export_file],
        ["Last run started", fmtDate(live.last_run_started)],
        ["Updated", fmtDate(live.updated_at)],
        ["Return code", data.returncode],
      ];
      $("statusTable").innerHTML = rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td class="mono">${esc(v)}</td></tr>`).join("");
    }

    async function refreshStatus() {
      const data = await api("/main/logs?lines=80");
      renderStatus(data);
      if (state.currentTab === "logs") renderLogs(data.logs || []);
    }

    function renderLogs(lines) {
      $("logsOutput").textContent = lines.length ? lines.join("\n") : "No log lines found.";
    }

    async function startScraper() {
      await api("/main/start", { method: "POST" });
      toast("Scraper started.");
      await refreshStatus();
    }

    async function restartScraper() {
      await api("/main/restart", { method: "POST" });
      toast("Scraper started again.");
      await refreshStatus();
    }

    async function stopScraper() {
      await api("/main/stop", { method: "POST" });
      toast("Scraper stopped.");
      await refreshStatus();
    }

    async function loadBatches() {
      const data = await api("/batches");
      const rows = data.items || [];
      $("batchesBody").innerHTML = rows.length
        ? rows.map((row) => `<tr><td>${esc(row.batch)}</td><td>${esc(row.count)}</td><td>${esc(fmtDate(row.exported_at))}</td></tr>`).join("")
        : `<tr><td colspan="3" class="empty">No batches found.</td></tr>`;
    }

    async function loadDomains() {
      const params = new URLSearchParams({ limit: $("domainLimit").value });
      if ($("batchInput").value) params.set("batch", $("batchInput").value);
      const data = await api(`/domains?${params}`);
      const rows = data.items || [];
      $("domainsBody").innerHTML = rows.length
        ? rows.map((row) => {
            const data = row.data ? Object.entries(row.data).slice(0, 8).map(([k, v]) => `${k}: ${v}`).join(", ") : "";
            return `<tr><td class="mono">${esc(row.domain)}</td><td>${esc(row.batch)}</td><td>${esc(fmtDate(row.exported_at))}</td><td>${esc(data)}</td></tr>`;
          }).join("")
        : `<tr><td colspan="4" class="empty">No domains found.</td></tr>`;
    }

    async function loadSeo() {
      const params = new URLSearchParams({ limit: "100" });
      if ($("seoSearch").value) params.set("domain", $("seoSearch").value);
      if ($("minScore").value) params.set("min_score", $("minScore").value);
      if ($("reachableFilter").value) params.set("reachable", $("reachableFilter").value);
      const data = await api(`/seo/checks?${params}`);
      const rows = data.items || [];
      $("seoBody").innerHTML = rows.length
        ? rows.map((row) => `<tr><td class="mono">${esc(row.domain)}</td><td>${esc(row.seo_score)}</td><td>${esc(row.reachable)}</td><td>${esc(row.title)}</td><td>${esc(fmtDate(row.checked_at))}</td></tr>`).join("")
        : `<tr><td colspan="5" class="empty">No SEO checks found.</td></tr>`;
    }

    async function runSeo() {
      const job = await api("/seo/check?limit=2000&force=true&recheck_days=14", { method: "POST" });
      state.lastSeoJob = job.job_id;
      toast(`SEO job queued: ${job.job_id}`);
      pollSeoJob(job.job_id);
    }

    async function pollSeoJob(jobId) {
      try {
        const job = await api(`/seo/jobs/${jobId}`);
        toast(job.message || `SEO job ${job.status}`);
        if (job.status === "queued" || job.status === "running") {
          setTimeout(() => pollSeoJob(jobId), 3000);
        } else {
          await loadSeo();
        }
      } catch (error) {
        toast(error.message);
      }
    }

    async function loadLogs() {
      const data = await api(`/main/logs?lines=${$("logLines").value}`);
      renderStatus(data);
      renderLogs(data.logs || []);
    }

    async function guarded(fn) {
      if (state.busy) return;
      setBusy(true);
      try {
        await fn();
      } catch (error) {
        toast(error.message || "Something went wrong.");
      } finally {
        setBusy(false);
      }
    }

    function switchTab(tab) {
      state.currentTab = tab;
      document.querySelectorAll(".nav button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
      ["overview", "domains", "seo", "logs"].forEach((name) => $(`${name}Tab`).classList.toggle("hidden", name !== tab));
      const titles = {
        overview: ["Overview", "Monitor the scraper and exported domains."],
        domains: ["Domains", "Browse saved expired domains by batch."],
        seo: ["SEO", "Run checks and inspect SEO scoring."],
        logs: ["Logs", "Read the latest scraper process output."],
      };
      $("pageTitle").textContent = titles[tab][0];
      $("pageSubtitle").textContent = titles[tab][1];
      if (tab === "logs") guarded(loadLogs);
    }

    document.querySelectorAll(".nav button").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
    $("startBtn").addEventListener("click", () => guarded(startScraper));
    $("restartBtn").addEventListener("click", () => guarded(restartScraper));
    $("stopBtn").addEventListener("click", () => guarded(stopScraper));
    $("refreshBtn").addEventListener("click", () => guarded(refreshStatus));
    $("loadBatchesBtn").addEventListener("click", () => guarded(loadBatches));
    $("loadDomainsBtn").addEventListener("click", () => guarded(loadDomains));
    $("loadSeoBtn").addEventListener("click", () => guarded(loadSeo));
    $("runSeoBtn").addEventListener("click", () => guarded(runSeo));
    $("loadLogsBtn").addEventListener("click", () => guarded(loadLogs));

    guarded(async () => {
      await refreshStatus();
      await loadBatches();
    });
    setInterval(() => refreshStatus().catch(() => {}), 10000);
  </script>
</body>
</html>
"""


def require_api_key(request: Request) -> None:
    api_key = request.headers.get("200m-API-Key")
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def scraper_status() -> dict[str, Any]:
    with scraper_lock:
        running = scraper_process is not None and scraper_process.poll() is None
        return {
            "running": running,
            "pid": scraper_process.pid if scraper_process is not None else None,
            "returncode": scraper_process.poll() if scraper_process is not None else None,
            "script": str(MAIN_SCRIPT),
            "log": str(MAIN_LOG),
        }


def close_scraper_log() -> None:
    global scraper_log_handle
    if scraper_log_handle is not None:
        try:
            scraper_log_handle.close()
        finally:
            scraper_log_handle = None


def read_main_log(lines: int = 80) -> list[str]:
    if not MAIN_LOG.exists():
        return []
    text = MAIN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[-max(1, lines):]


def read_live_status() -> dict[str, Any]:
    if not MAIN_STATUS_FILE.exists():
        return {}
    try:
        loaded = json.loads(MAIN_STATUS_FILE.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


def export_count() -> int:
    if not EXPORT_DOMAINS_FILE.exists():
        return 0
    with EXPORT_DOMAINS_FILE.open("r", encoding="utf-8-sig", errors="replace") as fh:
        count = sum(1 for line in fh if line.strip())
    return max(0, count - 1)


def domain_counts() -> dict[str, Any]:
    client, col = collection()
    try:
        return {
            "db_domains": col.count_documents({}),
            "export_csv_domains": export_count(),
            "export_file": str(EXPORT_DOMAINS_FILE),
        }
    finally:
        client.close()


def main_response(message: str) -> dict[str, Any]:
    live_status = read_live_status()
    return {
        "ok": True,
        "message": message,
        **scraper_status(),
        **domain_counts(),
        "live_status": live_status,
    }


def compact_main_status() -> dict[str, Any]:
    live = read_live_status()
    return {
        "exported_rows": live.get("exported_rows", live.get("last_exported_rows")),
        "batch": live.get("batch", live.get("last_batch")),
        "last_run_started": live.get("last_run_started"),
        "last_exported_rows": live.get("last_exported_rows"),
        "last_batch": live.get("last_batch"),
        "next_run_in_seconds": live.get("next_run_in_seconds"),
    }


def start_main_process() -> dict[str, Any]:
    global scraper_process, scraper_log_handle
    with scraper_lock:
        if scraper_process is not None and scraper_process.poll() is None:
            return {"message": "start successful"}

        close_scraper_log()
        scraper_log_handle = MAIN_LOG.open("a", encoding="utf-8")
        scraper_log_handle.write(f"\n[{datetime.utcnow().isoformat()}] starting main.py\n")
        scraper_log_handle.flush()
        scraper_process = subprocess.Popen(
            [sys.executable, str(MAIN_SCRIPT)],
            cwd=str(PROJECT_DIR),
            stdout=scraper_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return {"message": "start successful"}


def stop_main_process() -> dict[str, Any]:
    global scraper_process
    with scraper_lock:
        if scraper_process is None or scraper_process.poll() is not None:
            close_scraper_log()
            return {"message": "stop successful"}

        scraper_process.terminate()
        try:
            scraper_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            scraper_process.kill()
            scraper_process.wait(timeout=10)
        close_scraper_log()
        return {"message": "stop successful"}


def restart_main_process() -> dict[str, Any]:
    stop_main_process()
    result = start_main_process()
    result["message"] = "restart successful"
    return result


def collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][MONGO_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("batch", ASCENDING)])
    col.create_index([("exported_at", DESCENDING)])
    return client, col


def seo_ranking_collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][SEO_RANKING_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("rank", ASCENDING)])
    col.create_index([("seo_score", DESCENDING)])
    return client, col


def seo_check_collection():
    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][SEO_COLLECTION]
    col.create_index([("domain", ASCENDING)], unique=True)
    col.create_index([("checked_at", DESCENDING)])
    col.create_index([("seo_score", DESCENDING)])
    col.create_index([("reachable", ASCENDING)])
    return client, col


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items() if key not in {"_id", "source_url"}}
    return value


def as_percent(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    if isinstance(value, bool):
        return "100%" if value else "0%"
    if isinstance(value, (int, float)):
        return f"{int(value)}%"
    return str(value)


def sheet_row(seo: dict[str, Any], domain_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    data = (domain_doc or {}).get("data", {}) if domain_doc else {}
    title = seo.get("title") or "n/a"
    score = int(seo.get("seo_score") or 0)

    return {
        "URL": seo.get("domain") or (domain_doc or {}).get("domain", ""),
        "Title": title,
        "DA": data.get("DA", "n/a"),
        "PA": data.get("PA", "n/a"),
        "TBL": data.get("BL", data.get("TBL", seo.get("link_count", "n/a"))),
        "QBL": data.get("DP", data.get("QBL", "n/a")),
        "Q/T": f"{seo.get('title_length', 0)}/70" if title != "n/a" else "n/a",
        "OS": as_percent(score),
        "MT": "10/10" if seo.get("meta_description_ok") else "0/10",
        "SS": "100%" if seo.get("sitemap_xml") else "0%",
        "DH": "",
    }


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    headers = ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def set_seo_job(job_id: str, **values: Any) -> None:
    with seo_jobs_lock:
        job = seo_jobs.setdefault(job_id, {})
        job.update(values)


def run_seo_job(job_id: str, limit: int, force: bool, recheck_days: int) -> None:
    import asyncio

    set_seo_job(job_id, status="running", started_at=datetime.utcnow(), message="Loading domains")
    try:
        config = load_seo_config()
        domains = load_domains(config, limit, recheck_days, force_all=force)
        set_seo_job(job_id, total=len(domains), message=f"Checking {len(domains)} domains")
        if not domains:
            ranked = rebuild_rankings(config)
            set_seo_job(
                job_id,
                status="complete",
                completed_at=datetime.utcnow(),
                checked=0,
                reachable=0,
                unreachable=0,
                rankings=ranked,
                message="No domains needed SEO checks",
            )
            return

        results = asyncio.run(run_checks_incremental(config, domains))
        save_results(config, results)
        ranked = rebuild_rankings(config)
        reachable = sum(1 for result in results if result.get("reachable"))
        set_seo_job(
            job_id,
            status="complete",
            completed_at=datetime.utcnow(),
            checked=len(results),
            reachable=reachable,
            unreachable=len(results) - reachable,
            rankings=ranked,
            message="SEO check complete",
        )
    except Exception as exc:
        set_seo_job(job_id, status="error", completed_at=datetime.utcnow(), error=str(exc), message="SEO check failed")


@app.get("/", response_class=HTMLResponse)
def root_gui() -> HTMLResponse:
    return dashboard_gui()


@app.get("/ui", response_class=HTMLResponse)
def dashboard_gui() -> HTMLResponse:
    return HTMLResponse(GUI_HTML.replace("__API_KEY__", json.dumps(API_KEY)))


@app.get("/health")
def health() -> dict[str, Any]:
    client, col = collection()
    try:
        return {
            "ok": True,
            "database": MONGO_DB,
            "collection": MONGO_COLLECTION,
            "documents": col.count_documents({}),
        }
    finally:
        client.close()


@app.post("/main/start")
def start_main(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return start_main_process()


@app.post("/main/stop")
def stop_main(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return stop_main_process()


@app.post("/main/restart")
def restart_main(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return restart_main_process()


@app.get("/main/status")
def get_main_status(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return compact_main_status()


@app.post("/main/{action}")
def control_main(action: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    normalized = action.strip().lower()
    if normalized == "start":
        return start_main_process()
    if normalized == "stop":
        return stop_main_process()
    if normalized == "restart":
        return restart_main_process()
    if normalized == "status":
        return compact_main_status()
    raise HTTPException(status_code=400, detail="Use action: start, stop, restart, or status")


@app.get("/main/logs")
def get_main_logs(
    _: None = Depends(require_api_key),
    lines: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    response = main_response("main.py logs")
    response["lines"] = lines
    response["logs"] = read_main_log(lines)
    return response


@app.get("/domains")
def get_domains(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    batch: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    client, col = collection()
    try:
        query: dict[str, Any] = {}
        if batch is not None:
            query["batch"] = batch
        total = col.count_documents(query)
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("batch", ASCENDING), ("exported_at", ASCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


@app.get("/domains/{domain}")
def get_domain(domain: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    client, col = collection()
    try:
        row = col.find_one({"domain": domain.lower()}, {"_id": 0})
        if not row:
            raise HTTPException(status_code=404, detail="Domain not found")
        return serialize(row)
    finally:
        client.close()


@app.get("/batches")
def get_batches(_: None = Depends(require_api_key)) -> dict[str, Any]:
    client, col = collection()
    try:
        pipeline = [
            {"$group": {"_id": "$batch", "count": {"$sum": 1}, "exported_at": {"$max": "$exported_at"}}},
            {"$sort": {"_id": 1}},
        ]
        rows = [
            {"batch": row["_id"], "count": row["count"], "exported_at": row.get("exported_at")}
            for row in col.aggregate(pipeline)
        ]
        return {"items": serialize(rows)}
    finally:
        client.close()


@app.post("/seo/check")
def start_seo_check(
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
    limit: int = Query(2000, ge=1, le=10000),
    force: bool = Query(True),
    recheck_days: int = Query(14, ge=0),
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    set_seo_job(
        job_id,
        id=job_id,
        status="queued",
        created_at=datetime.utcnow(),
        limit=limit,
        force=force,
        recheck_days=recheck_days,
    )
    background_tasks.add_task(run_seo_job, job_id, limit, force, recheck_days)
    return {"job_id": job_id, "status": "queued", "limit": limit, "force": force}


@app.get("/seo/jobs/{job_id}")
def get_seo_job(job_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    with seo_jobs_lock:
        job = seo_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="SEO job not found")
        return serialize(job)


@app.get("/seo/rankings")
def get_seo_rankings(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
) -> dict[str, Any]:
    client, col = seo_ranking_collection()
    try:
        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        total = col.count_documents(query)
        rows = list(col.find(query, {"_id": 0}).sort([("rank", ASCENDING)]).skip(skip).limit(limit))
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


@app.get("/seo/checks")
def get_seo_checks(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
    domain: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
) -> dict[str, Any]:
    client, col = seo_check_collection()
    try:
        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        score_query: dict[str, int] = {}
        if min_score is not None:
            score_query["$gte"] = min_score
        if max_score is not None:
            score_query["$lte"] = max_score
        if score_query:
            query["seo_score"] = score_query

        total = col.count_documents(query)
        rows = list(
            col.find(query, {"_id": 0})
            .sort([("checked_at", DESCENDING), ("seo_score", DESCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        return {"total": total, "limit": limit, "skip": skip, "items": serialize(rows)}
    finally:
        client.close()


@app.get("/seo/sheet", response_model=None)
def get_seo_sheet(
    _: None = Depends(require_api_key),
    limit: int = Query(100, ge=1, le=5000),
    skip: int = Query(0, ge=0),
    reachable: bool | None = Query(None),
    domain: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB]
        seo_col = db[SEO_COLLECTION]
        domain_col = db[MONGO_COLLECTION]

        query: dict[str, Any] = {}
        if reachable is not None:
            query["reachable"] = reachable
        if domain:
            query["domain"] = {"$regex": domain.strip().lower(), "$options": "i"}
        if min_score is not None:
            query["seo_score"] = {"$gte": min_score}

        total = seo_col.count_documents(query)
        seo_rows = list(
            seo_col.find(query, {"_id": 0})
            .sort([("seo_score", DESCENDING), ("reachable", DESCENDING), ("domain", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        domains = [row.get("domain") for row in seo_rows if row.get("domain")]
        domain_docs = {
            row["domain"]: row
            for row in domain_col.find({"domain": {"$in": domains}}, {"_id": 0})
            if row.get("domain")
        }
        rows = [sheet_row(row, domain_docs.get(row.get("domain"))) for row in seo_rows]

        if format == "csv":
            return Response(
                content=rows_to_csv(rows),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="seo_sheet.csv"'},
            )

        columns = ["URL", "Title", "DA", "PA", "TBL", "QBL", "Q/T", "OS", "MT", "SS", "DH"]
        return {"total": total, "limit": limit, "skip": skip, "columns": columns, "items": serialize(rows)}
    finally:
        client.close()
