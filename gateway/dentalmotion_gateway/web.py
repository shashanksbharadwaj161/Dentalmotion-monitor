from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Callable

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from . import __version__
from .config_store import GatewayConfigStore, validate_gateway_id
from .state import GatewayState
from .update_manager import GatewayUpdateManager
from . import wifi_provision


ConfigCallback = Callable[[dict[str, Any]], None]

_DEVICE_CONTROL_PORT = 13250


PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DentalMotion Gateway</title>
  <style>
    :root { color-scheme:light; --bg:#f7f7f3; --panel:#fffffb; --ink:#151816; --muted:#697068; --line:#d9ddd4; --green:#3f7b61; --danger:#b6554c; --warn:#b5842a; --blue:#3f648f; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); }

    /* ── Topbar ── */
    .topbar { position:sticky; top:0; z-index:20; background:var(--panel); border-bottom:1px solid var(--line); padding:0 22px; display:flex; align-items:center; gap:14px; height:52px; }
    .topbar-brand { font-size:16px; font-weight:700; flex:1; letter-spacing:-.01em; }
    .topbar-gw { font-family:ui-monospace,monospace; font-size:12px; color:var(--muted); }

    /* ── Layout ── */
    main { max-width:1120px; margin:0 auto; padding:24px 20px 52px; }
    h2 { margin:0 0 14px; font-size:17px; }
    .grid { display:grid; grid-template-columns:repeat(12,1fr); gap:14px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; box-shadow:0 1px 2px rgba(0,0,0,.03); }
    .span-4 { grid-column:span 4; } .span-6 { grid-column:span 6; } .span-8 { grid-column:span 8; } .span-12 { grid-column:span 12; }

    /* ── Stat cards ── */
    .stat { color:var(--muted); display:block; font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }
    strong { font-size:18px; }

    /* ── Badges ── */
    .badge { display:inline-flex; align-items:center; gap:5px; min-height:28px; padding:3px 10px; border:1px solid var(--line); border-radius:999px; font-size:13px; color:var(--muted); background:#f1f2ee; }
    .badge::before { content:''; width:6px; height:6px; border-radius:50%; background:currentColor; opacity:.65; flex-shrink:0; }
    .badge.ok { border-color:#aac9ba; color:var(--green); background:#f4fbf7; }
    .badge.err { border-color:#d9aaa4; color:var(--danger); background:#fff7f5; }
    .badge.warn { border-color:#e0c58b; color:var(--warn); background:#fffaf0; }

    /* ── Status pills (device table) ── */
    .pill { display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }
    .pill-serving { background:#ebf7f2; color:var(--green); }
    .pill-nearby  { background:#eef2fa; color:var(--blue); }
    .pill-denied  { background:#fdf2f1; color:var(--danger); }
    .pill-pending { background:#fef8ee; color:var(--warn); }

    /* ── Forms ── */
    label { color:var(--muted); font-size:13px; }
    input, select { width:100%; min-height:40px; padding:8px 10px; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--ink); font:inherit; }
    input:focus, select:focus { outline:2px solid var(--blue); outline-offset:1px; }

    /* ── Buttons ── */
    button { min-height:40px; padding:8px 16px; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--ink); font:inherit; cursor:pointer; }
    button:hover:not(:disabled) { background:var(--bg); }
    button.primary { background:var(--green); color:#fff; border-color:var(--green); }
    button.primary:hover:not(:disabled) { background:#336553; }
    button.secondary { color:var(--blue); border-color:#b8c7d9; background:#f7faff; }
    button.danger { color:var(--danger); border-color:#d9aaa4; }
    button.danger:hover:not(:disabled) { background:#fff7f5; }
    button.sm { min-height:34px; padding:5px 12px; font-size:13px; }
    button:disabled { opacity:.5; cursor:not-allowed; }

    /* ── Field + Toolbar ── */
    .field { display:grid; gap:5px; }
    .field.narrow { flex:0 0 160px; min-width:140px; }
    .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:end; }
    .toolbar .field { flex:1; min-width:160px; }
    .button-row { display:flex; gap:8px; flex-wrap:wrap; }
    .header-row, .section-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
    .stack { display:grid; gap:14px; }

    /* ── Toggle switch ── */
    .sw-row { display:flex; gap:10px; align-items:center; min-height:40px; }
    .sw { position:relative; display:inline-block; width:46px; height:26px; flex-shrink:0; }
    .sw input { opacity:0; width:0; height:0; }
    .sw-track { position:absolute; inset:0; background:var(--line); border-radius:26px; cursor:pointer; transition:.15s; }
    .sw-track::after { content:''; position:absolute; width:20px; height:20px; left:3px; top:3px; background:#fff; border-radius:50%; transition:.15s; }
    .sw input:checked + .sw-track { background:var(--green); }
    .sw input:checked + .sw-track::after { transform:translateX(20px); }

    /* ── Wizard ── */
    .wizard-wrap { max-width:500px; margin:40px auto 0; }
    .wiz-steps { display:flex; gap:6px; margin-bottom:28px; }
    .wiz-pip { height:4px; flex:1; border-radius:2px; background:var(--line); }
    .wiz-pip.active { background:var(--green); }
    .wiz-pip.done { background:#aac9ba; }
    .wiz-step { display:none; }
    .wiz-step.active { display:block; }
    .wiz-title { font-size:22px; font-weight:700; margin:0 0 6px; }
    .wiz-sub { color:var(--muted); margin:0 0 22px; font-size:15px; }
    .wiz-actions { display:flex; justify-content:flex-end; gap:10px; margin-top:20px; }
    .wiz-confirm-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:16px; }
    .wiz-card { border:1px solid var(--line); border-radius:6px; padding:12px; background:#fcfcf8; }

    /* ── Summary grid ── */
    .summary-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
    .summary-card { border:1px solid var(--line); border-radius:7px; padding:12px; background:#fcfcf8; }
    .summary-card strong { font-size:15px; }

    /* ── Tables ── */
    table { width:100%; border-collapse:collapse; }
    th, td { border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:middle; }
    tr:last-child td { border-bottom:none; }
    th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; font-weight:600; }

    /* ── Misc ── */
    .muted { color:var(--muted); }
    .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
    .small { font-size:13px; }
    .notice { margin:10px 0 0; font-size:13px; }
    .notice.error { color:var(--danger); }
    .notice.success { color:var(--green); }
    .notice.info { color:var(--blue); }
    .sub-heading { font-size:13px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin:0 0 8px; }

    @media (max-width:860px) {
      .span-4,.span-6,.span-8 { grid-column:span 12; }
      main { padding:16px 14px; }
      .summary-grid { grid-template-columns:1fr; }
      .topbar-gw { display:none; }
      .header-row,.section-head { display:grid; }
    }
  </style>
</head>
<body>

  <!-- ── Sticky topbar ── -->
  <header class="topbar">
    <span class="topbar-brand" data-i18n="appTitle">DentalMotion Gateway</span>
    <span class="topbar-gw mono" id="topbar-gw-id"></span>
    <span id="topbar-badge" class="badge">-</span>
    <select id="language-select" style="width:auto;min-height:34px;padding:4px 8px">
      <option value="en">English</option>
      <option value="ja">日本語</option>
      <option value="zh">繁體中文</option>
    </select>
  </header>

  <main>

    <!-- ── Setup wizard (3-step, shown when no Gateway ID is configured) ── -->
    <div class="wizard-wrap panel" id="setup-wizard" hidden>
      <div class="wiz-steps">
        <div class="wiz-pip" id="wiz-pip0"></div>
        <div class="wiz-pip" id="wiz-pip1"></div>
      </div>

      <!-- Step 1: Gateway ID -->
      <div class="wiz-step active" id="wiz-step-0">
        <p class="wiz-title" data-i18n="w0title">Set up your Gateway</p>
        <p class="wiz-sub" data-i18n="w0sub">Assign a unique ID so devices can identify this gateway on your local network.</p>
        <div class="field" style="margin-bottom:12px">
          <label data-i18n="gatewayId">Gateway ID</label>
          <input id="setup-gateway-id-input" maxlength="64" placeholder="dentalmotion-gateway-xxxxxx">
        </div>
        <div class="wiz-actions">
          <button id="setup-auto-gateway-id" data-i18n="autoGenerate">Auto-generate</button>
          <button class="primary" id="wiz-next-0" data-i18n="next">Next →</button>
        </div>
        <p class="notice" id="setup-message"></p>
      </div>

      <!-- Step 2: Confirm and enable -->
      <div class="wiz-step" id="wiz-step-1">
        <p class="wiz-title" data-i18n="w2title">Enable Gateway</p>
        <p class="wiz-sub" data-i18n="w2sub">Confirm your settings and start the gateway service.</p>
        <div class="wiz-confirm-grid">
          <div class="wiz-card"><span class="muted small" data-i18n="gatewayId">Gateway ID</span><br><strong id="wiz-confirm-id">-</strong></div>
        </div>
        <div class="wiz-actions">
          <button id="wiz-back-1" data-i18n="back">← Back</button>
          <button class="primary" id="setup-save-id" data-i18n="enableAndStart">Enable &amp; Start</button>
        </div>
        <p class="notice" id="wiz-msg-2"></p>
      </div>
    </div>

    <!-- ── Main dashboard ── -->
    <div class="grid" id="dashboard-grid" hidden>

      <!-- Status row -->
      <section class="panel span-4">
        <span class="stat" data-i18n="gateway">Gateway</span>
        <strong id="gateway-name">-</strong>
        <p class="muted mono small" id="gateway-id">-</p>
      </section>
      <section class="panel span-4">
        <span class="stat" data-i18n="version">Version</span>
        <strong id="gateway-version">-</strong>
        <p class="muted mono small" id="update-mini">-</p>
      </section>
      <section class="panel span-4">
        <span class="stat" data-i18n="upstream">Status</span>
        <span id="upstream-badge" class="badge">-</span>
        <p class="muted mono small" id="upstream-url">-</p>
      </section>

      <!-- ── Gateway Settings ── -->
      <section class="panel span-12" id="nearby-section">
        <h2 data-i18n="gatewaySettings">Gateway Settings</h2>

        <!-- Identity + enable toggle -->
        <div class="toolbar" style="margin-bottom:14px">
          <div class="field">
            <label data-i18n="gatewayId">Gateway ID</label>
            <input id="gateway-id-input" maxlength="64" placeholder="dentalmotion-gateway-xxxxxx">
          </div>
          <button id="auto-gateway-id" data-i18n="autoGenerate">Auto-generate</button>
          <div class="sw-row" style="flex:0;white-space:nowrap">
            <label class="sw"><input type="checkbox" id="gateway-enabled"><span class="sw-track"></span></label>
            <span data-i18n="enabledCopy">Start UDP and FindMe</span>
          </div>
          <button class="primary" id="save-settings" data-i18n="save">Save</button>
        </div>
        <p class="notice" id="settings-message"></p>
      </section>

      <!-- ── Operations: device management + discovery ── -->
      <section class="panel span-12">
        <div class="section-head">
          <div>
            <h2 data-i18n="operations">Operations</h2>
            <p class="muted small" data-i18n="operationsCopy">Device discovery and management. Nearby devices were seen via FindMe — click Transfer to move them to this gateway.</p>
          </div>
          <div class="button-row">
            <button class="sm secondary" id="refresh-now" data-i18n="refreshNow">Refresh now</button>
            <button class="sm primary" id="discover-nearby" data-i18n="discoverDevices">Discover devices</button>
          </div>
        </div>

        <!-- Summary counts -->
        <div class="summary-grid" style="margin-bottom:18px">
          <div class="summary-card"><span class="stat" data-i18n="servingDevices">Serving Devices</span><strong id="serving-count">0</strong></div>
          <div class="summary-card"><span class="stat" data-i18n="nearbyDevices">Nearby Devices / FindMe Discovery</span><strong id="nearby-count">0</strong></div>
          <div class="summary-card"><span class="stat" data-i18n="claims">Claims</span><strong id="claim-count">0</strong></div>
        </div>

        <!-- Serving devices -->
        <p class="sub-heading" data-i18n="servingDevices">Serving Devices</p>
        <table style="margin-bottom:20px">
          <thead><tr><th data-i18n="device">Device</th><th data-i18n="mode">Mode</th><th>FindMe</th><th>UDP</th><th data-i18n="action">Action</th></tr></thead>
          <tbody id="device-body"></tbody>
        </table>

        <!-- Nearby devices (visible by default) -->
        <div class="section-head" style="margin-bottom:8px">
          <div>
            <p class="sub-heading" data-i18n="nearbyDevices">Nearby Devices / FindMe Discovery</p>
            <p class="muted small" style="margin:0" data-i18n="nearbyCopy">Recent FindMe broadcasts seen by this gateway. Use Transfer only for devices not currently served here.</p>
          </div>
          <button id="nearby-toggle" class="sm" data-i18n="hideNearby">Hide nearby</button>
        </div>
        <div id="nearby-panel">
          <table>
            <thead><tr><th data-i18n="device">Device</th><th data-i18n="mode">Mode</th><th data-i18n="address">Address</th><th data-i18n="state">State</th><th data-i18n="lastSeen">Last seen</th><th data-i18n="action">Action</th></tr></thead>
            <tbody id="nearby-body"></tbody>
          </table>
        </div>

        <!-- Local service stats -->
        <div style="margin-top:16px;display:flex;gap:16px;flex-wrap:wrap;align-items:center">
          <span class="muted small" data-i18n="localServices">Local services</span>
          <strong class="small" id="ports">-</strong>
          <span class="muted small" id="traffic-stats">-</span>
        </div>
      </section>

      <!-- ── Claims ── -->
      <section class="panel span-12">
        <h2 data-i18n="claims">Claims</h2>
        <table>
          <thead><tr><th data-i18n="time">Time</th><th data-i18n="device">Device</th><th>Claim</th><th data-i18n="status">Status</th><th data-i18n="error">Error</th></tr></thead>
          <tbody id="claim-body"></tbody>
        </table>
      </section>

      <!-- ── Update ── -->
      <section class="panel span-12">
        <div class="section-head">
          <div>
            <h2 data-i18n="update">Update</h2>
            <p class="muted small" id="update-status">-</p>
          </div>
          <div class="button-row">
            <button class="sm" id="check-update" data-i18n="checkUpdate">Check for update</button>
            <button class="sm" id="download-update" data-i18n="downloadUpdate">Download update</button>
            <button class="sm" id="apply-update" data-i18n="applyUpdate">Apply update</button>
            <button class="sm danger" id="restart-gateway" data-i18n="restartGateway">Restart Gateway</button>
          </div>
        </div>
        <p class="notice" id="update-message"></p>
      </section>

    </div>
  </main>

  <script>
    const I18N = {
      en: {
        action:"Action", address:"Address", allow:"Allow", applyUpdate:"Apply update", autoGenerate:"Auto-generate",
        appTitle:"DentalMotion Gateway", back:"← Back", checkUpdate:"Check for update",
        claims:"Claims", connectionMode:"Connection mode", denied:"Denied", device:"Device",
        discoverDevices:"Discover devices", downloadUpdate:"Download update",
        effectiveServer:"Effective server", enableAndStart:"Enable & Start",
        enabledCopy:"Start UDP and FindMe", error:"Error",
        gateway:"Gateway", gatewayId:"Gateway ID", gatewaySettings:"Gateway Settings",
        hideNearby:"Hide nearby", showNearby:"Show nearby",
        language:"Language", lastSeen:"Last seen", localServices:"Local services",
        localServicesCopy:"FindMe / UDP control and data", manualUrl:"Manual WS/WSS URL", mode:"Mode", next:"Next →",
        nearbyCopy:"Recent FindMe broadcasts seen by this gateway. Use Transfer only for devices not currently served here.",
        nearbyDevices:"Nearby Devices / FindMe Discovery",
        noActiveClaims:"No active claims.", noDevices:"No devices served yet.", noNearby:"No nearby FindMe requests yet.",
        offline:"offline", online:"online", operations:"Operations",
        operationsCopy:"Device discovery and management. Nearby devices were seen via FindMe — click Transfer to move them to this gateway.",
        packets:"packets", production:"Production", reject:"Reject",
        refresh:"Refresh", refreshNow:"Refresh now", restartGateway:"Restart Gateway",
        dropped:"dropped", queueDropped:"queue dropped",
        save:"Save", saved:"Saved", serveThisDevice:"Transfer to this gateway",
        serving:"Serving", servingDevices:"Serving Devices",
        setupCopy:"Create a unique Gateway ID before enabling upstream, UDP and FindMe services.",
        setupTitle:"Set up Gateway ID", firstRun:"First setup",
        state:"State", status:"Status", targetServer:"Target Server",
        time:"Time", transfer:"Transfer to this gateway",
        upstream:"Upstream", upstreamSent:"Upstream sent", upstreamStatus:"Upstream status",
        udpIn:"UDP in", update:"Update", version:"Version",
        w0title:"Set up your Gateway",
        w0sub:"Assign a unique ID so devices can identify this gateway on your local network.",
        w1title:"Upstream server",
        w1sub:"Choose which DentalMotion server this gateway relays device data to.",
        w2title:"Enable Gateway",
        w2sub:"Confirm your settings and start the gateway service.",
      },
      ja: {
        action:"操作", address:"アドレス", allow:"許可", applyUpdate:"更新を適用", autoGenerate:"自動生成",
        appTitle:"DentalMotion Gateway", back:"← 戻る", checkUpdate:"更新を確認",
        claims:"要求", connectionMode:"接続モード", denied:"拒否済み", device:"デバイス",
        discoverDevices:"デバイスを検出", downloadUpdate:"更新をダウンロード",
        effectiveServer:"実際の接続先", enableAndStart:"有効化して開始",
        enabledCopy:"UDP、FindMeを開始", error:"エラー",
        gateway:"ゲートウェイ", gatewayId:"Gateway ID", gatewaySettings:"ゲートウェイ設定",
        hideNearby:"非表示", showNearby:"表示",
        language:"言語", lastSeen:"最終確認", localServices:"ローカルサービス",
        localServicesCopy:"FindMe / UDP制御とデータ", manualUrl:"手動 WS/WSS URL", mode:"モード", next:"次へ →",
        nearbyCopy:"このゲートウェイが受信した最近のFindMeブロードキャストです。ここで未提供のデバイスだけ転送できます。",
        nearbyDevices:"近くのデバイス / FindMe 検出",
        noActiveClaims:"有効な要求はありません。", noDevices:"提供中のデバイスはありません。", noNearby:"近くのFindMe要求はありません。",
        offline:"オフライン", online:"オンライン", operations:"操作",
        operationsCopy:"デバイスの検出と管理。「近くにある」デバイスはFindMe経由で確認済み——転送ボタンでこのゲートウェイに移動できます。",
        packets:"パケット", production:"本番", reject:"拒否",
        refresh:"更新", refreshNow:"今すぐ更新", restartGateway:"Gatewayを再起動",
        dropped:"破棄", queueDropped:"キュー破棄",
        save:"保存", saved:"保存しました", serveThisDevice:"このゲートウェイに転送",
        serving:"提供中", servingDevices:"提供中のデバイス",
        setupCopy:"上流接続、UDP、FindMeサービスを有効にする前に、一意なGateway IDを設定してください。",
        setupTitle:"Gateway IDを設定", firstRun:"初回設定",
        state:"状態", status:"状態", targetServer:"接続先サーバー",
        time:"時刻", transfer:"このゲートウェイに転送",
        upstream:"上流接続", upstreamSent:"上流送信", upstreamStatus:"上流状態",
        udpIn:"UDP入力", update:"更新", version:"バージョン",
        w0title:"ゲートウェイのセットアップ",
        w0sub:"ネットワーク上でこのゲートウェイを識別する固有のIDを割り当てます。",
        w1title:"上流サーバー",
        w1sub:"このゲートウェイが接続するサーバーを選択します。",
        w2title:"ゲートウェイを有効化",
        w2sub:"設定を確認してサービスを開始します。",
      },
      zh: {
        action:"操作", address:"位址", allow:"允許", applyUpdate:"套用更新", autoGenerate:"自動生成",
        appTitle:"DentalMotion 閘道器", back:"← 返回", checkUpdate:"檢查更新",
        claims:"認領記錄", connectionMode:"連線模式", denied:"已拒絕", device:"設備",
        discoverDevices:"搜尋設備", downloadUpdate:"下載更新",
        effectiveServer:"實際伺服器", enableAndStart:"啟用並開始",
        enabledCopy:"啟用 UDP 及 FindMe 服務", error:"錯誤",
        gateway:"閘道器", gatewayId:"閘道器 ID", gatewaySettings:"閘道器設定",
        hideNearby:"隱藏附近", showNearby:"顯示附近",
        language:"語言", lastSeen:"最後發現", localServices:"本地服務",
        localServicesCopy:"FindMe / UDP 控制與資料", manualUrl:"手動 WS/WSS 網址", mode:"模式", next:"下一步 →",
        nearbyCopy:"本閘道器收到的近期 FindMe 廣播。僅對尚未由本閘道器服務的設備使用「轉移」功能。",
        nearbyDevices:"附近設備 / FindMe 偵測",
        noActiveClaims:"目前沒有認領記錄。", noDevices:"目前沒有正在服務的設備。", noNearby:"尚未收到附近的 FindMe 請求。",
        offline:"離線", online:"上線", operations:"操作",
        operationsCopy:"設備探索與管理。「附近」設備透過 FindMe 廣播發現——點擊「轉移」可將其移至本閘道器。",
        packets:"封包", production:"生產環境", reject:"拒絕",
        refresh:"重新整理", refreshNow:"立即重新整理", restartGateway:"重新啟動閘道器",
        dropped:"丟棄", queueDropped:"佇列丟棄",
        save:"儲存", saved:"已儲存", serveThisDevice:"轉移至本閘道器",
        serving:"服務中", servingDevices:"服務中的設備",
        setupCopy:"在啟用上游連線、UDP 及 FindMe 服務前，請先設定唯一的閘道器 ID。",
        setupTitle:"設定閘道器 ID", firstRun:"首次設定",
        state:"狀態", status:"狀態", targetServer:"目標伺服器",
        time:"時間", transfer:"轉移至本閘道器",
        upstream:"上游連線", upstreamSent:"上游已傳送", upstreamStatus:"上游狀態",
        udpIn:"UDP 輸入", update:"更新", version:"版本",
        w0title:"設定閘道器",
        w0sub:"為本閘道器指定一個唯一 ID，以便在區域網路上識別。",
        w1title:"上游伺服器",
        w1sub:"選擇本閘道器要轉發設備資料的伺服器。",
        w2title:"啟用閘道器",
        w2sub:"確認設定並啟動閘道器服務。",
      },
    };

    let language = localStorage.getItem("dentalmotion-gateway-language") || "en";
    let showNearby = true;
    let targetSettingsDirty = false;
    let setupGatewayIdSuggested = false;
    let wizardStep = 0;

    function tr(key) { return (I18N[language] && I18N[language][key]) || I18N.en[key] || key; }

    function updateNearbyToggleLabel() {
      const node = document.getElementById("nearby-toggle");
      if (node) node.textContent = showNearby ? tr("hideNearby") : tr("showNearby");
    }

    function applyI18n() {
      document.documentElement.lang = language;
      document.getElementById("language-select").value = language;
      document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = tr(node.getAttribute("data-i18n")); });
      updateNearbyToggleLabel();
    }

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
    }

    async function api(path, options) {
      const res = await fetch(path, options);
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || payload.message || res.statusText);
      return payload;
    }

    function text(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value || "-";
    }

    function notice(id, message, cls = "") {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = message || "";
      node.className = `notice ${cls}`.trim();
    }

    function syncTargetSettings(config) {
      if (targetSettingsDirty) return;
      document.getElementById("gateway-id-input").value = config.gateway_id || "";
      document.getElementById("gateway-enabled").checked = !!config.enabled;
    }

    // ── Wizard ──────────────────────────────────────────────────────────
    function wizSetStep(step) {
      wizardStep = step;
      [0, 1].forEach((i) => {
        const s = document.getElementById(`wiz-step-${i}`);
        if (s) s.className = `wiz-step${i === step ? " active" : ""}`;
        const p = document.getElementById(`wiz-pip${i}`);
        if (p) p.className = `wiz-pip${i === step ? " active" : i < step ? " done" : ""}`;
      });
      if (step === 1) {
        text("wiz-confirm-id", document.getElementById("setup-gateway-id-input").value || "-");
      }
    }

    document.getElementById("setup-auto-gateway-id").addEventListener("click", async () => {
      try {
        const payload = await api("/api/gateway-id/suggest", { method: "POST" });
        document.getElementById("setup-gateway-id-input").value = payload.gateway_id || "";
        setupGatewayIdSuggested = true;
      } catch (error) {
        notice("setup-message", error.message || String(error), "error");
      }
    });

    document.getElementById("wiz-next-0").addEventListener("click", () => {
      const id = String(document.getElementById("setup-gateway-id-input").value || "").trim();
      if (!id) { notice("setup-message", tr("gatewayId") + " required", "error"); return; }
      notice("setup-message", "");
      wizSetStep(1);
    });

    document.getElementById("wiz-back-1").addEventListener("click", () => wizSetStep(0));

    document.getElementById("setup-save-id").addEventListener("click", async () => {
      notice("wiz-msg-2", "");
      const btn = document.getElementById("setup-save-id");
      btn.disabled = true;
      try {
        await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            gateway_id: String(document.getElementById("setup-gateway-id-input").value || "").trim(),
            enabled: true,
          }),
        });
        targetSettingsDirty = false;
        refresh();
      } catch (error) {
        notice("wiz-msg-2", error.message || String(error), "error");
      } finally {
        btn.disabled = false;
      }
    });

    // ── Settings ────────────────────────────────────────────────────────
    document.getElementById("save-settings").addEventListener("click", async () => {
      notice("settings-message", "");
      try {
        await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            gateway_id: document.getElementById("gateway-id-input").value,
            enabled: document.getElementById("gateway-enabled").checked,
          }),
        });
        targetSettingsDirty = false;
        notice("settings-message", tr("saved"), "success");
        refresh();
      } catch (error) {
        notice("settings-message", error.message || String(error), "error");
      }
    });

    document.getElementById("auto-gateway-id").addEventListener("click", async () => {
      try {
        const payload = await api("/api/gateway-id/suggest", { method: "POST" });
        document.getElementById("gateway-id-input").value = payload.gateway_id || "";
        targetSettingsDirty = true;
      } catch (error) {
        notice("settings-message", error.message || String(error), "error");
      }
    });

    document.getElementById("gateway-id-input").addEventListener("input", () => { targetSettingsDirty = true; });
    document.getElementById("gateway-enabled").addEventListener("change", () => { targetSettingsDirty = true; });

    // ── Update ───────────────────────────────────────────────────────────
    function renderUpdate(state) {
      const current = state.current_version || "-";
      const latest = state.latest_version || "-";
      text("gateway-version", current);
      text("update-mini", `${state.phase || "idle"} / latest ${latest}`);
      text("update-status", `current ${current} / latest ${latest} / ${state.update_available ? "available" : "no update"}`);
      document.getElementById("download-update").disabled = !state.update_available && !state.zip_url;
      document.getElementById("apply-update").disabled = !state.downloaded;
      document.getElementById("restart-gateway").disabled = !state.restart_required;
    }

    async function updateAction(path) {
      notice("update-message", "");
      try {
        const payload = await api(path, { method: "POST" });
        renderUpdate(payload.update_state || payload);
        notice("update-message", (payload.update_state || payload).phase || "ok", "success");
      } catch (error) {
        notice("update-message", error.message || String(error), "error");
      }
    }

    document.getElementById("check-update").addEventListener("click", () => updateAction("/api/update/check"));
    document.getElementById("download-update").addEventListener("click", () => updateAction("/api/update/download"));
    document.getElementById("apply-update").addEventListener("click", () => updateAction("/api/update/apply"));
    document.getElementById("restart-gateway").addEventListener("click", () => updateAction("/api/update/restart"));

    // ── Device action clicks ─────────────────────────────────────────────
    document.addEventListener("click", async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const reject = target.getAttribute("data-reject");
      const allow = target.getAttribute("data-allow");
      const serve = target.getAttribute("data-serve");
      if (reject) {
        target.disabled = true;
        await api(`/api/devices/${encodeURIComponent(reject)}/reject`, { method: "POST" }).catch(() => { target.disabled = false; });
      }
      if (allow) {
        target.disabled = true;
        await api(`/api/devices/${encodeURIComponent(allow)}/allow`, { method: "POST" }).catch(() => { target.disabled = false; });
      }
      if (serve) {
        target.disabled = true;
        const orig = target.textContent;
        target.textContent = "…";
        await api(`/api/devices/${encodeURIComponent(serve)}/serve`, { method: "POST" }).catch(() => {
          target.textContent = orig;
          target.disabled = false;
        });
      }
      if (reject || allow || serve) refresh();
    });

    document.getElementById("refresh-now").addEventListener("click", () => refresh());

    document.getElementById("discover-nearby").addEventListener("click", async () => {
      showNearby = true;
      await api("/api/discover", { method: "POST" }).catch(() => {});
      await refresh();
      const panel = document.getElementById("nearby-section");
      if (panel) panel.scrollIntoView({ block: "start", behavior: "smooth" });
    });

    document.getElementById("nearby-toggle").addEventListener("click", () => {
      showNearby = !showNearby;
      refresh();
    });

    document.getElementById("language-select").addEventListener("change", (event) => {
      language = event.target.value;
      if (!I18N[language]) language = "en";
      localStorage.setItem("dentalmotion-gateway-language", language);
      refresh();
    });

    // ── Main refresh ──────────────────────────────────────────────────────
    async function refresh() {
      applyI18n();
      const data = await api("/api/status");
      const hasGatewayId = !!String(data.config.gateway_id || "").trim();
      document.getElementById("setup-wizard").hidden = hasGatewayId;
      document.getElementById("dashboard-grid").hidden = !hasGatewayId;

      if (hasGatewayId) { setupGatewayIdSuggested = false; wizardStep = 0; }

      if (!hasGatewayId && !setupGatewayIdSuggested) {
        setupGatewayIdSuggested = true;
        try {
          const payload = await api("/api/gateway-id/suggest", { method: "POST" });
          if (!String(document.getElementById("setup-gateway-id-input").value || "").trim()) {
            document.getElementById("setup-gateway-id-input").value = payload.gateway_id || "";
          }
        } catch (error) {
          notice("setup-message", error.message || String(error), "error");
        }
      }

      if (!hasGatewayId) { wizSetStep(wizardStep); return; }

      // Topbar
      text("topbar-gw-id", data.config.gateway_id);
      const isEnabled = !!data.config.enabled;
      const isConnected = isEnabled && !!data.upstream.connected;
      const topbarBadge = document.getElementById("topbar-badge");
      topbarBadge.textContent = !isEnabled ? "disabled" : (isConnected ? tr("online") : tr("offline"));
      topbarBadge.className = `badge${!isEnabled ? " warn" : isConnected ? " ok" : " err"}`;

      // Stat panels
      text("gateway-name", data.config.gateway_name);
      text("gateway-id", data.config.gateway_id);
      text("upstream-url", data.upstream.server_url);
      const upBadge = document.getElementById("upstream-badge");
      upBadge.textContent = !isEnabled ? "disabled" : (isConnected ? tr("online") : tr("offline"));
      upBadge.className = `badge${!isEnabled ? " warn" : isConnected ? " ok" : " err"}`;

      text("upstream-summary", !isEnabled ? "disabled" : (isConnected ? tr("online") : tr("offline")));
      text("ports", `FindMe ${data.config.listen_discovery_port} / UDP ${data.config.listen_udp_port}`);
      text("traffic-stats", `${tr("udpIn")} ${Number(data.upstream.udp_in_fps || 0)}/s · ${tr("upstreamSent")} ${Number(data.upstream.upstream_sent_fps || 0)}/s · ${tr("queueDropped")} ${Number(data.upstream.data_queue_dropped || 0)}`);

      syncTargetSettings(data.config || {});
      renderUpdate(data.update_state || {});

      // Counts
      const servingDevices = (data.state.devices || []).filter((item) => item.connected);
      text("serving-count", String(servingDevices.length));
      text("nearby-count", String((data.state.nearby_devices || []).length));
      text("claim-count", String((data.state.claims || []).length));

      // Serving devices table
      const body = document.getElementById("device-body");
      body.innerHTML = "";
      for (const item of servingDevices) {
        const row = document.createElement("tr");
        const action = item.denied
          ? `<button class="sm" data-allow="${esc(item.device_uid)}">${tr("allow")}</button>`
          : `<button class="sm danger" data-reject="${esc(item.device_uid)}">${tr("reject")}</button>`;
        row.innerHTML = `<td><strong>${esc(item.device_name || item.device_uid)}</strong><br><span class="muted mono small">${esc(item.device_uid)}</span></td><td class="small">${esc(item.mode || "-")}</td><td class="small">${esc(item.findme_state || "attached")}<br><span class="muted">${esc(item.wifi_rssi ?? "")}</span></td><td class="small">${Number(item.udp_packets || 0)} ${tr("packets")}<br><span class="muted">${Number(item.udp_dropped || 0)} ${tr("dropped")}</span></td><td>${action}</td>`;
        body.appendChild(row);
      }
      if (!servingDevices.length) body.innerHTML = `<tr><td colspan="5" class="muted">${tr("noDevices")}</td></tr>`;

      // Nearby devices table
      document.getElementById("nearby-panel").hidden = !showNearby;
      updateNearbyToggleLabel();
      const nbody = document.getElementById("nearby-body");
      nbody.innerHTML = "";
      for (const item of data.state.nearby_devices || []) {
        const row = document.createElement("tr");
        const stateLabel = item.serving ? tr("serving") : (item.denied ? tr("denied") : esc(item.findme_state || item.findme_reason || "-"));
        let action = "";
        if (item.serving) {
          action = `<span class="badge ok">${tr("serving")}</span>`;
        } else if (item.denied) {
          action = `<button class="sm" data-allow="${esc(item.device_uid)}">${tr("allow")}</button>`;
        } else if (data.upstream.connected) {
          action = `<button class="sm secondary" data-serve="${esc(item.device_uid)}">${tr("serveThisDevice")}</button>`;
        } else {
          action = `<button class="sm" disabled title="${tr("upstreamStatus")}: ${tr("offline")}">${tr("serveThisDevice")}</button>`;
        }
        row.innerHTML = `<td><strong>${esc(item.device_name || item.device_uid)}</strong><br><span class="mono muted small">${esc(item.device_uid || "-")}</span></td><td class="small">${esc(item.mode || "-")}</td><td class="small mono">${esc(item.addr || "-")}</td><td class="small">${stateLabel}</td><td class="small muted">${esc(item.last_findme_at || "-")}</td><td>${action}</td>`;
        nbody.appendChild(row);
      }
      if (!(data.state.nearby_devices || []).length) nbody.innerHTML = `<tr><td colspan="6" class="muted">${tr("noNearby")}</td></tr>`;

      // Claims table
      const cbody = document.getElementById("claim-body");
      cbody.innerHTML = "";
      for (const item of data.state.claims || []) {
        const row = document.createElement("tr");
        row.innerHTML = `<td class="small">${esc(item.updated_at || item.requested_at || "-")}</td><td><span class="mono small">${esc(item.device_uid || "-")}</span></td><td><span class="mono small">${esc(item.claim_id || "-")}</span></td><td class="small">${esc(item.state || "-")}</td><td class="small muted">${esc(item.last_error || "")}</td>`;
        cbody.appendChild(row);
      }
      if (!(data.state.claims || []).length) cbody.innerHTML = `<tr><td colspan="5" class="muted">${tr("noActiveClaims")}</td></tr>`;
    }

    applyI18n();
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>"""


class GatewayWebServer:
    def __init__(
        self,
        host: str,
        port: int,
        config_store: GatewayConfigStore,
        state: GatewayState,
        upstream: Any,
        udp_control: Any | None = None,
        *,
        discovery: Any | None = None,
        on_config_saved: ConfigCallback | None = None,
        update_manager: GatewayUpdateManager | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.config_store = config_store
        self.state = state
        self.upstream = upstream
        self.udp_control = udp_control
        self.discovery = discovery
        self.on_config_saved = on_config_saved
        self.update_manager = update_manager or GatewayUpdateManager()
        self.app = self._make_app()
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def _make_app(self) -> Flask:
        app = Flask(__name__)

        @app.get("/")
        def index() -> str:
            return PAGE

        @app.get("/api/status")
        def status() -> Any:
            config = self.config_store.snapshot()
            return jsonify({
                "config": self._public_config(config),
                "version": __version__,
                "upstream": self.upstream.status(),
                "udp_control": self.udp_control.snapshot() if self.udp_control is not None else {},
                "state": self.state.snapshot(config.get("denied_devices", [])),
                "update_state": self.update_manager.state(),
            })

        @app.post("/api/settings")
        def settings() -> Any:
            body = request.get_json(silent=True) or {}
            try:
                patch = {
                    "gateway_id": body.get("gateway_id", ""),
                    "enabled": bool(body.get("enabled", False)),
                }
                if patch["enabled"]:
                    validate_gateway_id(patch["gateway_id"])
                config = self.config_store.save(patch)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if self.on_config_saved is not None:
                self.on_config_saved(config)
            else:
                self.upstream.gateway_id = str(config.get("gateway_id") or "")
            return jsonify({"ok": True, "config": self._public_config(config)})

        @app.post("/api/gateway-id/suggest")
        def suggest_gateway_id() -> Any:
            try:
                return jsonify({"gateway_id": self._request_gateway_id_suggestion()})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 503

        @app.post("/api/update/check")
        def update_check() -> Any:
            return jsonify({"ok": True, "update_state": self.update_manager.check()})

        @app.post("/api/update/download")
        def update_download() -> Any:
            return jsonify({"ok": True, "update_state": self.update_manager.download()})

        @app.post("/api/update/apply")
        def update_apply() -> Any:
            return jsonify({"ok": True, "update_state": self.update_manager.apply()})

        @app.post("/api/update/restart")
        def update_restart() -> Any:
            return jsonify({"ok": True, "update_state": self.update_manager.restart()})

        @app.post("/api/discover")
        def discover() -> Any:
            if self.discovery is not None:
                self.discovery.send_probe()
            return jsonify({"ok": True})

        @app.get("/api/provision/scan")
        def provision_scan() -> Any:
            if not wifi_provision.IS_WINDOWS:
                return jsonify({"ok": False, "error": "automatic_wifi_switch_only_supported_on_windows", "boards": []})
            current = wifi_provision.get_current_wifi()
            interface = current.get("interface") or ""
            boards = wifi_provision.find_setup_networks(interface) if interface else []
            return jsonify({"ok": True, "boards": boards, "current_ssid": current.get("ssid", "")})

        @app.post("/api/provision")
        def provision() -> Any:
            body = request.get_json(silent=True) or {}
            ssid = str(body.get("ssid") or "").strip()
            password = str(body.get("password") or "")
            board_ssid = str(body.get("board_ssid") or "").strip() or None
            if not ssid:
                return jsonify({"ok": False, "error": "ssid_required"}), 400
            try:
                result = wifi_provision.provision_board(ssid, password, board_ap_ssid=board_ssid)
            except wifi_provision.ProvisioningError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 502
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify(result)

        @app.post("/api/devices/<device_uid>/reject")
        def reject(device_uid: str) -> Any:
            config = self.config_store.deny(device_uid)
            return jsonify({"ok": True, "denied_devices": config.get("denied_devices", [])})

        @app.post("/api/devices/<device_uid>/allow")
        def allow(device_uid: str) -> Any:
            config = self.config_store.allow(device_uid)
            return jsonify({"ok": True, "denied_devices": config.get("denied_devices", [])})

        @app.post("/api/devices/<device_uid>/serve")
        def serve(device_uid: str) -> Any:
            if self.config_store.is_denied(device_uid):
                return jsonify({"ok": False, "error": "device_denied"}), 409
            if not self.upstream.is_connected():
                return jsonify({"ok": False, "error": "upstream_offline"}), 503
            claim = self.state.create_claim(device_uid)
            self.state.update_claim(claim["claim_id"], state="sent")
            self.upstream.send_claim_request(claim["device_uid"], claim["claim_id"], int(claim["ttl_ms"]))
            updated = self.state.update_claim(claim["claim_id"], state="requested")
            device_ip = self.state.last_findme_addr(device_uid)
            if device_ip and self.udp_control is not None:
                gw_id = str(self.config_store.snapshot().get("gateway_id") or "")
                self.udp_control.send_command_to(device_uid, (device_ip, _DEVICE_CONTROL_PORT), {
                    "command": "findme_switch_gateway",
                    "request_id": "findme-claim-{}".format(claim["claim_id"]),
                    "preferred_gateway_id": gw_id,
                    "claim_id": claim["claim_id"],
                    "ttl_ms": int(claim["ttl_ms"]),
                    "expires_at_ms": int(claim["expires_at_ms"]),
                })
            return jsonify({"ok": True, "claim": updated or claim})

        @app.post("/api/devices/<device_uid>/cmd")
        def device_cmd(device_uid: str) -> Any:
            body = request.get_json(silent=True) or {}
            # Board's UDP commandName() reads the "command" key from payload
            cmd = str(body.get("command") or body.get("cmd") or "").strip()
            if not cmd:
                return jsonify({"ok": False, "error": "missing_cmd"}), 400
            device_ip = self.state.last_findme_addr(device_uid)
            if not device_ip or self.udp_control is None:
                return jsonify({"ok": False, "error": "device_unreachable"}), 404
            import uuid
            payload = {k: v for k, v in body.items() if k not in ("cmd",)}
            payload["command"] = cmd
            payload.setdefault("request_id", str(uuid.uuid4())[:8])
            sent = self.udp_control.send_command_to(
                device_uid, (device_ip, _DEVICE_CONTROL_PORT), payload
            )
            return jsonify({"ok": sent, "cmd": cmd, "target": device_ip})

        return app

    @staticmethod
    def _public_config(config: dict[str, Any]) -> dict[str, Any]:
        return dict(config)

    def _request_gateway_id_suggestion(self) -> str:
        return validate_gateway_id(f"dentalmotion-gateway-{secrets.token_hex(3)}")

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="dentalmotion-gateway-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
        if thread is not None:
            thread.join(timeout=1.0)
