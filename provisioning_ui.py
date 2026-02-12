#!/usr/bin/env python3
"""
Lightweight Device Management UI for Raspberry Pi Zero
Access via browser: http://<pi-ip>:5000

Features:
- Device Provisioning
- Start/Stop Services
- Camera Preview
- WiFi Configuration

RAM Usage: ~20-30MB
"""

import os
import sys
import json
import socket
import subprocess
import threading
import time

# Minimal Flask import
from flask import Flask, render_template_string, jsonify, request, Response

# Import provisioning functions
from device_provisioning import (
    provision_device,
    is_device_provisioned,
    get_device_credentials,
    delete_device_credentials,
    Config
)
from get_configure import get_configure, set_configure, DEFAULT_CONFIG

# Import WiFi AP Manager (try local first, then pi_control)
AP_MANAGER_AVAILABLE = False
WiFiAPManager = None
try:
    from wifi_ap_manager import WiFiAPManager, check_and_start_ap_if_needed
    AP_MANAGER_AVAILABLE = True
except ImportError:
    try:
        sys.path.insert(0, '/home/pi/pi_control')
        from wifi_ap_manager import WiFiAPManager, check_and_start_ap_if_needed
        AP_MANAGER_AVAILABLE = True
    except ImportError:
        pass

# Global AP manager instance
ap_manager = None
if AP_MANAGER_AVAILABLE:
    ap_manager = WiFiAPManager()

app = Flask(__name__)

# All services to manage (shown in UI with individual start/stop/restart)
SERVICES = [
    {'name': 'facial1', 'display': 'Facial Tracking', 'description': 'Driver drowsiness detection'},
    {'name': 'get_gps_data1', 'display': 'GPS Tracking', 'description': 'GPS + MQTT data'},
    {'name': 'send-data-api', 'display': 'Data Sync', 'description': 'Telemetry to cloud API'},
    {'name': 'upload_images', 'display': 'Image Upload', 'description': 'Upload images to cloud'},
    {'name': 'ota-auto-update', 'display': 'OTA Updates', 'description': 'Auto-update daemon'},
    {'name': 'pi-control', 'display': 'Pi Control', 'description': 'Remote device control'},
]

# Non-essential services that can be toggled off for max performance
# Stopping these frees CPU/RAM/I/O for facial detection
MONITORING_SERVICES = ['send-data-api', 'upload_images', 'ota-auto-update']

# Camera streaming globals
camera = None
camera_lock = threading.Lock()

# Minimal HTML template (embedded to avoid file I/O)
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Device Manager</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: -apple-system, sans-serif; 
            background: #1a1a2e; 
            color: #eee;
            min-height: 100vh;
            padding: 15px;
            padding-bottom: 80px;
        }
        .container { max-width: 500px; margin: 0 auto; overflow-y: auto; }
        h1 { font-size: 1.3em; margin-bottom: 15px; text-align: center; }
        h2 { font-size: 1em; margin-bottom: 10px; color: #888; }
        .card {
            background: #16213e;
            border-radius: 12px;
            padding: 15px;
            margin-bottom: 12px;
        }
        .status { 
            display: flex; 
            align-items: center; 
            gap: 10px;
            margin-bottom: 10px;
        }
        .dot {
            width: 10px; height: 10px;
            border-radius: 50%;
            background: #ff6b6b;
            flex-shrink: 0;
        }
        .dot.ok { background: #51cf66; }
        .dot.warn { background: #fcc419; }
        .label { color: #888; font-size: 0.8em; margin-bottom: 4px; }
        .value { 
            font-family: monospace; 
            font-size: 0.85em;
            word-break: break-all;
            background: #0f0f23;
            padding: 6px;
            border-radius: 6px;
        }
        .btn {
            width: 100%;
            padding: 12px;
            border: none;
            border-radius: 8px;
            font-size: 0.9em;
            font-weight: 600;
            cursor: pointer;
            margin-top: 8px;
        }
        .btn-sm {
            width: auto;
            padding: 6px 10px;
            font-size: 0.8em;
            margin: 2px;
        }
        .btn-primary { background: #4c6ef5; color: white; }
        .btn-success { background: #2f9e44; color: white; }
        .btn-danger { background: #c92a2a; color: white; }
        .btn-secondary { background: #2d3748; color: #eee; }
        .btn:disabled { background: #555; cursor: not-allowed; opacity: 0.7; }
        .msg { 
            padding: 8px; 
            border-radius: 6px; 
            margin-top: 8px;
            font-size: 0.85em;
        }
        .msg.error { background: #c92a2a; }
        .msg.success { background: #2f9e44; }
        .tabs {
            display: flex;
            gap: 5px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }
        .tab {
            flex: 1;
            min-width: 80px;
            padding: 10px;
            background: #2d3748;
            border: none;
            border-radius: 8px;
            color: #888;
            cursor: pointer;
            font-size: 0.85em;
        }
        .tab.active { background: #4c6ef5; color: white; }
        .panel { display: none; }
        .panel.active { display: block; }
        .service-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #2d3748;
        }
        .service-row:last-child { border-bottom: none; }
        .service-info { flex: 1; }
        .service-name { font-weight: 600; font-size: 0.9em; }
        .service-desc { font-size: 0.75em; color: #888; }
        .service-actions { display: flex; gap: 5px; }
        .camera-container {
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            text-align: center;
        }
        .camera-container img {
            width: 100%;
            height: auto;
            display: block;
        }
        .wifi-network {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px;
            background: #0f0f23;
            border-radius: 6px;
            margin-bottom: 8px;
            cursor: pointer;
        }
        .wifi-network:hover { background: #1a1a3e; }
        .wifi-signal { font-size: 0.8em; color: #888; }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #2d3748;
            border-radius: 6px;
            background: #0f0f23;
            color: #eee;
            font-size: 0.9em;
            margin-bottom: 8px;
        }
        .info { font-size: 0.75em; color: #666; margin-top: 10px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📱 Device Manager</h1>
        
        <div class="tabs">
            <button class="tab active" onclick="showTab('provision')">Provision</button>
            <button class="tab" onclick="showTab('services')">Services</button>
            <button class="tab" onclick="showTab('camera')">Camera</button>
            <button class="tab" onclick="showTab('wifi')">WiFi</button>
            <button class="tab" onclick="showTab('settings')">Settings</button>
        </div>
        
        <!-- Provisioning Panel -->
        <div id="provision" class="panel active">
            <div class="card">
                <div class="status">
                    <div class="dot" id="statusDot"></div>
                    <span id="statusText">Checking...</span>
                </div>
                <div id="deviceInfo" style="display:none;">
                    <div class="label">Device ID</div>
                    <div class="value" id="deviceId">-</div>
                    <div class="label" style="margin-top:8px;">Auth Key</div>
                    <div class="value" id="authKey">-</div>
                </div>
            </div>
            <button class="btn btn-primary" id="provisionBtn" onclick="provision()">
                Provision Device
            </button>
            <button class="btn btn-secondary" onclick="checkStatus()">
                Refresh Status
            </button>
        </div>
        
        <!-- Services Panel -->
        <div id="services" class="panel">
            <!-- Remote Monitoring Toggle -->
            <div class="card" style="border: 1px solid #4c6ef5;">
                <div style="display:flex; align-items:center; justify-content:space-between;">
                    <div>
                        <div style="font-weight:600; font-size:0.95em;">Remote Monitoring</div>
                        <div style="font-size:0.75em; color:#888;">Cloud upload, data sync, OTA updates</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="monitoringToggle" onchange="toggleMonitoring(this.checked)" style="opacity:0; width:0; height:0;">
                        <span id="toggleSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="toggleKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>
                <div id="monitoringStatus" style="font-size:0.75em; color:#ff6b6b; margin-top:8px;">OFF - Max performance for detection</div>
            </div>

            <div class="card">
                <div id="servicesList">Loading...</div>
            </div>
            <button class="btn btn-success" onclick="controlAllServices('start')">
                Start All
            </button>
            <button class="btn btn-danger" onclick="controlAllServices('stop')">
                Stop All
            </button>
            <button class="btn btn-secondary" onclick="loadServices()">
                Refresh
            </button>
        </div>
        
        <!-- Camera Panel -->
        <div id="camera" class="panel">
            <div class="card">
                <div class="camera-container" id="cameraContainer">
                    <p style="padding:40px;color:#666;">Camera Off</p>
                </div>
            </div>
            <button class="btn btn-success" id="cameraStartBtn" onclick="startCamera()">
                📷 Start Camera
            </button>
            <button class="btn btn-danger" id="cameraStopBtn" onclick="stopCamera()" style="display:none;">
                ■ Stop Camera
            </button>
        </div>
        
        <!-- WiFi Panel -->
        <div id="wifi" class="panel">
            <!-- AP Mode Status Card -->
            <div class="card" id="apModeCard" style="display:none; background:#2d1f4e;">
                <h2>📡 Access Point Mode</h2>
                <div class="status">
                    <div class="dot warn"></div>
                    <span>AP Mode Active</span>
                </div>
                <div class="label">Network Name (SSID)</div>
                <div class="value" id="apSsid">-</div>
                <div class="label" style="margin-top:8px;">Password</div>
                <div class="value" id="apPassword">-</div>
                <div class="label" style="margin-top:8px;">Connect to this IP</div>
                <div class="value" id="apIp">-</div>
                <p style="font-size:0.75em; color:#fcc419; margin-top:10px;">⚠️ Connect to the WiFi network above, then configure a new network below.</p>
            </div>
            
            <div class="card">
                <h2>Current Connection</h2>
                <div class="status">
                    <div class="dot" id="wifiDot"></div>
                    <span id="wifiStatus">Checking...</span>
                </div>
            </div>
            <div class="card">
                <h2>Available Networks</h2>
                <div id="wifiList">
                    <p style="color:#666;">Click Scan to find networks</p>
                </div>
            </div>
            <button class="btn btn-primary" onclick="scanWifi()">
                🔍 Scan Networks
            </button>
            <div class="card" style="margin-top:12px;">
                <h2>Connect to Network</h2>
                <input type="text" id="wifiSsid" placeholder="Network Name (SSID)">
                <input type="password" id="wifiPass" placeholder="Password">
                <button class="btn btn-success" onclick="connectWifi()">
                    Connect
                </button>
            </div>
            
            <!-- AP Mode Controls -->
            <div class="card" style="margin-top:12px;">
                <h2>Access Point Mode</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Start AP mode to configure WiFi when no networks are available.</p>
                <button class="btn btn-secondary" id="apStartBtn" onclick="startApMode()">
                    📡 Start AP Mode
                </button>
                <button class="btn btn-danger" id="apStopBtn" onclick="stopApMode()" style="display:none;">
                    ■ Stop AP Mode
                </button>
            </div>
            
            <!-- Reset WiFi -->
            <div class="card" style="margin-top:12px; border: 1px solid #c92a2a;">
                <h2>⚠️ Reset WiFi</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Forget all saved networks and restart AP mode for re-provisioning.</p>
                <button class="btn btn-danger" onclick="resetWifi()">
                    🔄 Reset WiFi & Start AP
                </button>
            </div>
        </div>
        
        <!-- Settings Panel -->
        <div id="settings" class="panel">
            <!-- Detection Settings -->
            <div class="card">
                <h2>Detection Settings</h2>
                <div style="display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <label style="font-size:0.85em; width:160px;">Activation Speed (km/h)</label>
                    <input type="number" id="activationSpeed" min="0" max="200" style="flex:1;" placeholder="0">
                </div>

                <!-- LED Blink Toggle -->
                <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div>
                        <div style="font-weight:600; font-size:0.9em;">LED Blink on Alert</div>
                        <div style="font-size:0.75em; color:#888;">Blink LED (GPIO 4) during sleep/yawn detection</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="ledBlinkToggle" onchange="updateToggleUI(this, 'ledSlider', 'ledKnob')" style="opacity:0; width:0; height:0;">
                        <span id="ledSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="ledKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>

                <!-- NoFace Alert Toggle -->
                <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div>
                        <div style="font-weight:600; font-size:0.9em;">NoFace Alert</div>
                        <div style="font-size:0.75em; color:#888;">Beep when no face detected for set duration</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="nofaceToggle" onchange="toggleNofaceUI()" style="opacity:0; width:0; height:0;">
                        <span id="nofaceSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="nofaceKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>
                <div id="nofaceSettings" style="display:none; padding:10px 0;">
                    <div style="display:flex; gap:10px; align-items:center;">
                        <label style="font-size:0.85em; width:160px;">Alert after (seconds)</label>
                        <input type="number" id="nofaceThreshold" min="1" max="30" step="1" style="flex:1;" placeholder="2">
                    </div>
                </div>

                <button class="btn btn-primary" style="margin-top:10px;" onclick="saveSettings()">Save Detection Settings</button>
                <div id="settingsMessage" class="msg" style="display:none;"></div>
            </div>

            <!-- Data Retention Settings -->
            <div class="card" style="margin-top:12px; border: 1px solid #fab005;">
                <h2>Data Retention</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Auto-delete old GPS data and images. Requires password.</p>
                <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                    <label style="font-size:0.85em; width:110px;">GPS Data (days)</label>
                    <input type="number" id="gpsRetention" min="1" max="365" style="flex:1;" placeholder="30">
                </div>
                <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                    <label style="font-size:0.85em; width:110px;">Images (days)</label>
                    <input type="number" id="imageRetention" min="1" max="365" style="flex:1;" placeholder="15">
                </div>
                <input type="password" id="retentionPassword" placeholder="Password to save" style="margin-bottom:8px;">
                <button class="btn btn-primary btn-sm" onclick="saveRetention()">Save Retention Settings</button>
                <div id="retentionMessage" class="msg" style="display:none;"></div>
            </div>

            <!-- Delete Device ID Card -->
            <div class="card" style="margin-top:12px; border: 1px solid #c92a2a;">
                <h2>Delete Device</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Remove device credentials, GPS data, car data, and images. Requires password.</p>
                <input type="password" id="deletePassword" placeholder="Enter password to confirm">
                <button class="btn btn-danger" onclick="deleteDeviceId()">Delete Device</button>
                <div id="deleteMessage" class="msg" style="display:none;"></div>
            </div>
        </div>

        <div id="message"></div>
        <div class="info">IP: {{ ip_address }}</div>
    </div>
    
    <script>
        let cameraActive = false;
        
        function showTab(name) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            event.target.classList.add('active');
            
            if (name === 'services') loadServices();
            if (name === 'wifi') checkWifi();
            if (name === 'settings') { loadSettings(); loadRetention(); }
        }
        
        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.className = 'msg ' + type;
            msg.textContent = text;
            setTimeout(() => { msg.textContent = ''; msg.className = ''; }, 4000);
        }
        
        // === Provisioning ===
        function checkStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    const dot = document.getElementById('statusDot');
                    const text = document.getElementById('statusText');
                    const info = document.getElementById('deviceInfo');
                    const btn = document.getElementById('provisionBtn');
                    
                    if (data.provisioned) {
                        dot.classList.add('ok');
                        text.textContent = 'Provisioned ✓';
                        info.style.display = 'block';
                        document.getElementById('deviceId').textContent = data.device_id;
                        document.getElementById('authKey').textContent = data.auth_key.substring(0, 16) + '...';
                        btn.disabled = true;
                        btn.textContent = 'Already Provisioned';
                    } else {
                        dot.classList.remove('ok');
                        text.textContent = 'Not Provisioned';
                        info.style.display = 'none';
                        btn.disabled = false;
                        btn.textContent = 'Provision Device';
                    }
                });
        }
        
        function provision() {
            const btn = document.getElementById('provisionBtn');
            btn.disabled = true;
            btn.textContent = 'Provisioning...';
            
            fetch('/api/provision', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showMessage('Device provisioned!', 'success');
                        checkStatus();
                    } else {
                        showMessage(data.error || 'Failed', 'error');
                        btn.disabled = false;
                        btn.textContent = 'Retry';
                    }
                });
        }
        
        function deleteDeviceId() {
            const password = document.getElementById('deletePassword').value;
            const msgDiv = document.getElementById('deleteMessage');
            
            if (!password) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Password is required';
                return;
            }
            
            if (!confirm('Are you sure you want to delete the device ID? This action cannot be undone.')) {
                return;
            }
            
            fetch('/api/device/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: password })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Device ID deleted successfully';
                    document.getElementById('deletePassword').value = '';
                    checkStatus();
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed to delete';
                }
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }
        
        // === Services ===
        function loadServices() {
            fetch('/api/services')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('servicesList');
                    list.innerHTML = data.services.map(s => `
                        <div class="service-row">
                            <div class="service-info">
                                <div class="service-name">
                                    <span class="dot ${s.active ? 'ok' : ''}" style="display:inline-block;width:8px;height:8px;margin-right:6px;"></span>
                                    ${s.display}
                                </div>
                                <div class="service-desc">${s.description} - ${s.status}</div>
                            </div>
                            <div class="service-actions">
                                <button class="btn btn-sm btn-success" onclick="controlService('${s.name}', 'start')" ${s.active ? 'disabled' : ''} title="Start">▶</button>
                                <button class="btn btn-sm btn-danger" onclick="controlService('${s.name}', 'stop')" ${!s.active ? 'disabled' : ''} title="Stop">■</button>
                                <button class="btn btn-sm btn-primary" onclick="controlService('${s.name}', 'restart')" title="Restart">🔄</button>
                            </div>
                        </div>
                    `).join('');
                });
        }
        
        function controlService(name, action) {
            fetch('/api/services/' + name + '/' + action, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    showMessage(data.message, data.success ? 'success' : 'error');
                    setTimeout(loadServices, 1000);
                });
        }
        
        function controlAllServices(action) {
            fetch('/api/services/all/' + action, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    showMessage(data.message, 'success');
                    setTimeout(loadServices, 1500);
                });
        }
        
        // === Camera ===
        function startCamera() {
            document.getElementById('cameraContainer').innerHTML = '<img src="/api/camera/stream" alt="Camera">';
            document.getElementById('cameraStartBtn').style.display = 'none';
            document.getElementById('cameraStopBtn').style.display = 'block';
            cameraActive = true;
        }
        
        function stopCamera() {
            fetch('/api/camera/stop', { method: 'POST' });
            document.getElementById('cameraContainer').innerHTML = '<p style="padding:40px;color:#666;">Camera Off</p>';
            document.getElementById('cameraStartBtn').style.display = 'block';
            document.getElementById('cameraStopBtn').style.display = 'none';
            cameraActive = false;
        }
        
        // === WiFi ===
        function checkWifi() {
            fetch('/api/wifi/status')
                .then(r => r.json())
                .then(data => {
                    const dot = document.getElementById('wifiDot');
                    const text = document.getElementById('wifiStatus');
                    const apCard = document.getElementById('apModeCard');
                    const apStartBtn = document.getElementById('apStartBtn');
                    const apStopBtn = document.getElementById('apStopBtn');
                    
                    // Prioritize WiFi connection status over AP mode
                    // WiFi can be connected even if hostapd is still running
                    if (data.connected) {
                        // WiFi is connected - show connected status
                        dot.classList.add('ok');
                        dot.classList.remove('warn');
                        text.textContent = data.ssid + ' (' + data.ip + ')';
                        apCard.style.display = 'none';
                        apStartBtn.style.display = 'block';
                        apStopBtn.style.display = 'none';
                    } else if (data.ap_active) {
                        // Not connected to WiFi, AP mode is active
                        apCard.style.display = 'block';
                        document.getElementById('apSsid').textContent = data.ap_ssid || '-';
                        document.getElementById('apPassword').textContent = data.ap_password || '-';
                        document.getElementById('apIp').textContent = data.ap_ip || '-';
                        apStartBtn.style.display = 'none';
                        apStopBtn.style.display = 'block';
                        dot.classList.remove('ok');
                        dot.classList.add('warn');
                        text.textContent = 'AP Mode (No WiFi)';
                    } else {
                        // Not connected and AP mode not active
                        apCard.style.display = 'none';
                        apStartBtn.style.display = 'block';
                        apStopBtn.style.display = 'none';
                        dot.classList.remove('ok');
                        dot.classList.remove('warn');
                        text.textContent = 'Not Connected';
                    }
                });
        }
        
        // === AP Mode ===
        function startApMode() {
            const btn = document.getElementById('apStartBtn');
            btn.disabled = true;
            btn.textContent = 'Starting AP...';
            showMessage('Starting Access Point...', 'success');
            
            fetch('/api/wifi/ap/start', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    btn.textContent = '📡 Start AP Mode';
                    if (data.success) {
                        showMessage('AP Mode started! SSID: ' + data.ssid, 'success');
                        setTimeout(checkWifi, 1000);
                    } else {
                        showMessage(data.error || 'Failed to start AP', 'error');
                    }
                })
                .catch(e => {
                    btn.disabled = false;
                    btn.textContent = '📡 Start AP Mode';
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function stopApMode() {
            const btn = document.getElementById('apStopBtn');
            btn.disabled = true;
            btn.textContent = 'Stopping AP...';
            showMessage('Stopping Access Point...', 'success');
            
            fetch('/api/wifi/ap/stop', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    btn.textContent = '■ Stop AP Mode';
                    if (data.success) {
                        showMessage('AP Mode stopped. Reconnecting to WiFi...', 'success');
                        setTimeout(checkWifi, 3000);
                    } else {
                        showMessage(data.error || 'Failed to stop AP', 'error');
                    }
                })
                .catch(e => {
                    btn.disabled = false;
                    btn.textContent = '■ Stop AP Mode';
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function resetWifi() {
            if (!confirm("This will forget ALL saved WiFi networks and start AP mode. You will need to reconnect to the device hotspot to configure a new network. Continue?")) {
                return;
            }
            showMessage('Resetting WiFi...', 'success');
            
            fetch('/api/wifi/reset', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showMessage('WiFi reset! Connect to AP: ' + (data.ap_ssid || 'SapienceDevice'), 'success');
                        setTimeout(checkWifi, 2000);
                    } else {
                        showMessage(data.error || 'Reset failed', 'error');
                    }
                })
                .catch(e => {
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function scanWifi() {
            document.getElementById('wifiList').innerHTML = '<p style="color:#666;">Scanning...</p>';
            fetch('/api/wifi/scan')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('wifiList');
                    if (data.networks.length === 0) {
                        list.innerHTML = '<p style="color:#666;">No networks found</p>';
                        return;
                    }
                    list.innerHTML = data.networks.map(n => `
                        <div class="wifi-network" onclick="selectWifi('${n.ssid}')">
                            <span>${n.ssid}</span>
                            <span class="wifi-signal">${n.signal}%</span>
                        </div>
                    `).join('');
                });
        }
        
        function selectWifi(ssid) {
            document.getElementById('wifiSsid').value = ssid;
            document.getElementById('wifiPass').focus();
        }
        
        function connectWifi() {
            const ssid = document.getElementById('wifiSsid').value;
            const password = document.getElementById('wifiPass').value;
            if (!ssid) {
                showMessage('Enter network name', 'error');
                return;
            }
            showMessage('Connecting...', 'success');
            fetch('/api/wifi/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ssid, password })
            })
            .then(r => r.json())
            .then(data => {
                showMessage(data.message, data.success ? 'success' : 'error');
                if (data.success) setTimeout(checkWifi, 3000);
            });
        }
        
        // === Settings ===
        function updateToggleUI(checkbox, sliderId, knobId) {
            const slider = document.getElementById(sliderId);
            const knob = document.getElementById(knobId);
            if (checkbox.checked) {
                slider.style.background = '#2f9e44';
                knob.style.left = '27px';
            } else {
                slider.style.background = '#c92a2a';
                knob.style.left = '3px';
            }
        }

        function toggleNofaceUI() {
            const toggle = document.getElementById('nofaceToggle');
            updateToggleUI(toggle, 'nofaceSlider', 'nofaceKnob');
            document.getElementById('nofaceSettings').style.display = toggle.checked ? 'block' : 'none';
        }

        function loadSettings() {
            fetch('/api/config/settings')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('activationSpeed').value = data.speed || 0;
                    const ledToggle = document.getElementById('ledBlinkToggle');
                    ledToggle.checked = data.led_blink_enabled;
                    updateToggleUI(ledToggle, 'ledSlider', 'ledKnob');
                    const nofaceToggle = document.getElementById('nofaceToggle');
                    nofaceToggle.checked = data.noface_enabled;
                    updateToggleUI(nofaceToggle, 'nofaceSlider', 'nofaceKnob');
                    document.getElementById('nofaceThreshold').value = data.noface_threshold || 2;
                    document.getElementById('nofaceSettings').style.display = data.noface_enabled ? 'block' : 'none';
                });
        }

        function saveSettings() {
            const speed = document.getElementById('activationSpeed').value;
            const ledEnabled = document.getElementById('ledBlinkToggle').checked;
            const nofaceEnabled = document.getElementById('nofaceToggle').checked;
            const nofaceThreshold = document.getElementById('nofaceThreshold').value;
            const msgDiv = document.getElementById('settingsMessage');

            fetch('/api/config/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    speed: parseInt(speed) || 0,
                    led_blink_enabled: ledEnabled,
                    noface_enabled: nofaceEnabled,
                    noface_threshold: parseInt(nofaceThreshold) || 2
                })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Settings saved! Restarting Facial Tracking...';
                    fetch('/api/services/facial1/restart', { method: 'POST' })
                        .then(r => r.json())
                        .then(r => {
                            msgDiv.textContent = r.success ? 'Settings saved! Facial Tracking restarted.' : 'Settings saved! Restart failed: ' + (r.message || '');
                            setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
                        });
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed';
                    setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
                }
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }

        // === Data Retention ===
        function loadRetention() {
            fetch('/api/config/retention')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('gpsRetention').value = data.gps_retention_days || 30;
                    document.getElementById('imageRetention').value = data.image_retention_days || 15;
                });
        }

        function saveRetention() {
            const gpsDays = document.getElementById('gpsRetention').value;
            const imageDays = document.getElementById('imageRetention').value;
            const password = document.getElementById('retentionPassword').value;
            const msgDiv = document.getElementById('retentionMessage');

            if (!password) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Password is required';
                setTimeout(() => { msgDiv.style.display = 'none'; }, 3000);
                return;
            }
            if (!gpsDays || gpsDays < 1 || gpsDays > 365 || !imageDays || imageDays < 1 || imageDays > 365) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Days must be between 1 and 365';
                setTimeout(() => { msgDiv.style.display = 'none'; }, 3000);
                return;
            }

            fetch('/api/config/retention', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    gps_retention_days: parseInt(gpsDays),
                    image_retention_days: parseInt(imageDays),
                    password: password
                })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Retention settings saved!';
                    document.getElementById('retentionPassword').value = '';
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed to save';
                }
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }

        // === Monitoring Toggle ===
        function checkMonitoring() {
            fetch('/api/monitoring/status')
                .then(r => r.json())
                .then(data => {
                    const toggle = document.getElementById('monitoringToggle');
                    const slider = document.getElementById('toggleSlider');
                    const knob = document.getElementById('toggleKnob');
                    const status = document.getElementById('monitoringStatus');
                    toggle.checked = data.enabled;
                    if (data.enabled) {
                        slider.style.background = '#2f9e44';
                        knob.style.left = '27px';
                        status.style.color = '#51cf66';
                        status.textContent = 'ON - Cloud sync active';
                    } else {
                        slider.style.background = '#c92a2a';
                        knob.style.left = '3px';
                        status.style.color = '#ff6b6b';
                        status.textContent = 'OFF - Max performance for detection';
                    }
                });
        }

        function toggleMonitoring(enable) {
            const slider = document.getElementById('toggleSlider');
            const knob = document.getElementById('toggleKnob');
            const status = document.getElementById('monitoringStatus');
            status.textContent = enable ? 'Starting...' : 'Stopping...';
            status.style.color = '#fcc419';

            fetch('/api/monitoring/toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enable: enable })
            })
            .then(r => r.json())
            .then(data => {
                if (data.enabled) {
                    slider.style.background = '#2f9e44';
                    knob.style.left = '27px';
                    status.style.color = '#51cf66';
                    status.textContent = 'ON - Cloud sync active';
                } else {
                    slider.style.background = '#c92a2a';
                    knob.style.left = '3px';
                    status.style.color = '#ff6b6b';
                    status.textContent = 'OFF - Max performance for detection';
                }
                showMessage(data.enabled ? 'Monitoring enabled' : 'Monitoring disabled', 'success');
                setTimeout(loadServices, 1000);
            });
        }

        // Init
        checkStatus();
        checkMonitoring();
    </script>
</body>
</html>
'''

def get_ip_address():
    """Get the Pi's IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"


# ============================================
# Service Management APIs
# ============================================

def get_service_status(service_name):
    """Get status of a systemd service"""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', f'{service_name}.service'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except:
        return 'unknown'


def run_service_command(service_name, action):
    """Run systemctl command on a service"""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', action, f'{service_name}.service'],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0, result.stderr or result.stdout
    except Exception as e:
        return False, str(e)


# ============================================
# WiFi Management APIs
# ============================================

def get_wifi_status():
    """Get current WiFi connection status including AP mode"""
    status = {
        'connected': False,
        'ssid': '',
        'ip': get_ip_address(),
        'ap_active': False,
        'ap_ssid': None,
        'ap_password': None,
        'ap_ip': None,
    }
    
    # Check AP mode status if manager is available
    if ap_manager:
        try:
            ap_status = ap_manager.get_status()
            status['ap_active'] = ap_status.get('ap_active', False)
            if status['ap_active']:
                status['ap_ssid'] = ap_status.get('ap_ssid')
                status['ap_password'] = ap_status.get('ap_password')
                status['ap_ip'] = ap_status.get('ap_ip')
                status['ip'] = ap_status.get('ap_ip', status['ip'])
        except Exception as e:
            print(f"Error getting AP status: {e}")
    
    try:
        # Get current SSID
        result = subprocess.run(
            ['iwgetid', '-r'],
            capture_output=True, text=True, timeout=5
        )
        ssid = result.stdout.strip()
        
        if ssid:
            status['connected'] = True
            status['ssid'] = ssid
    except:
        pass
    
    return status


def scan_wifi_networks():
    """Scan for available WiFi networks"""
    try:
        result = subprocess.run(
            ['sudo', 'iwlist', 'wlan0', 'scan'],
            capture_output=True, text=True, timeout=30
        )
        
        networks = []
        current_ssid = None
        current_signal = 0
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if 'ESSID:' in line:
                ssid = line.split('ESSID:')[1].strip('"')
                if ssid and ssid not in [n['ssid'] for n in networks]:
                    networks.append({'ssid': ssid, 'signal': current_signal})
            elif 'Signal level=' in line:
                try:
                    # Parse signal level (format varies)
                    if 'dBm' in line:
                        dbm = int(line.split('Signal level=')[1].split(' ')[0])
                        current_signal = min(100, max(0, 2 * (dbm + 100)))
                    else:
                        current_signal = int(line.split('Signal level=')[1].split('/')[0])
                except:
                    current_signal = 50
        
        # Sort by signal strength
        networks.sort(key=lambda x: x['signal'], reverse=True)
        return networks[:10]  # Return top 10
    except Exception as e:
        return []


def connect_to_wifi(ssid, password):
    """Connect to a WiFi network using nmcli or wpa_supplicant"""
    debug_info = []
    
    try:
        # Check if NetworkManager is available
        nm_check = subprocess.run(
            ['systemctl', 'is-active', 'NetworkManager'],
            capture_output=True, text=True, timeout=5
        )
        nm_active = nm_check.stdout.strip() == 'active'
        debug_info.append(f"NetworkManager active: {nm_active}")
        print(f"[DEBUG] connect_to_wifi: NetworkManager active: {nm_active}")
        
        if nm_active:
            # Try nmcli first (NetworkManager)
            print(f"[DEBUG] connect_to_wifi: Trying nmcli connect to '{ssid}'")
            result = subprocess.run(
                ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid, 'password', password],
                capture_output=True, text=True, timeout=30
            )
            debug_info.append(f"nmcli returncode: {result.returncode}")
            debug_info.append(f"nmcli stdout: {result.stdout.strip()}")
            debug_info.append(f"nmcli stderr: {result.stderr.strip()}")
            print(f"[DEBUG] connect_to_wifi: nmcli result - code={result.returncode}, stdout={result.stdout.strip()}, stderr={result.stderr.strip()}")
            
            if result.returncode == 0:
                return True, 'Connected successfully'
            
            # nmcli failed - log the error
            error_msg = result.stderr.strip() or result.stdout.strip() or 'Unknown nmcli error'
            print(f"[DEBUG] connect_to_wifi: nmcli failed: {error_msg}")
        
        # Fallback: Update wpa_supplicant.conf
        print(f"[DEBUG] connect_to_wifi: Falling back to wpa_supplicant for '{ssid}'")
        debug_info.append("Falling back to wpa_supplicant")
        
        wpa_conf = f'''
network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
}}
'''
        # Append to wpa_supplicant.conf
        wpa_result = subprocess.run(
            ['sudo', 'bash', '-c', f'echo \'{wpa_conf}\' >> /etc/wpa_supplicant/wpa_supplicant.conf'],
            capture_output=True, text=True, timeout=10
        )
        debug_info.append(f"wpa_supplicant.conf update: {wpa_result.returncode}")
        print(f"[DEBUG] connect_to_wifi: wpa_supplicant.conf update result: {wpa_result.returncode}")
        
        # Reconfigure
        reconf_result = subprocess.run(
            ['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'],
            capture_output=True, text=True, timeout=10
        )
        debug_info.append(f"wpa_cli reconfigure: {reconf_result.returncode}, {reconf_result.stdout.strip()}")
        print(f"[DEBUG] connect_to_wifi: wpa_cli reconfigure result: {reconf_result.returncode}, {reconf_result.stdout.strip()}")
        
        # Wait for connection
        print(f"[DEBUG] connect_to_wifi: Waiting 5s for connection...")
        time.sleep(5)
        
        # Check if connected
        status = get_wifi_status()
        debug_info.append(f"Final status: connected={status['connected']}, ssid={status.get('ssid', 'N/A')}")
        print(f"[DEBUG] connect_to_wifi: Final status - connected={status['connected']}, ssid={status.get('ssid', 'N/A')}")
        
        if status['connected'] and status['ssid'] == ssid:
            return True, 'Connected successfully'
        
        # Connection failed - return debug info
        debug_str = '; '.join(debug_info)
        return False, f'Connection failed. Debug: {debug_str}'
        
    except subprocess.TimeoutExpired as e:
        error_msg = f'Timeout: {str(e)}'
        print(f"[DEBUG] connect_to_wifi: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f'Error: {str(e)}'
        print(f"[DEBUG] connect_to_wifi: {error_msg}")
        return False, error_msg


# ============================================
# Camera Streaming
# ============================================

def generate_camera_frames():
    """Generator for camera frames (MJPEG stream)"""
    global camera
    
    try:
        import cv2
        
        with camera_lock:
            if camera is None:
                camera = cv2.VideoCapture(0)
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                camera.set(cv2.CAP_PROP_FPS, 10)
        
        while True:
            with camera_lock:
                if camera is None:
                    break
                success, frame = camera.read()
            
            if not success:
                break
            
            # Encode as JPEG
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            frame_bytes = buffer.tobytes()
            
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            
            time.sleep(0.1)  # ~10 FPS to save CPU
            
    except ImportError:
        yield b'--frame\r\nContent-Type: text/plain\r\n\r\nOpenCV not available\r\n'
    except Exception as e:
        yield f'--frame\r\nContent-Type: text/plain\r\n\r\nError: {e}\r\n'.encode()


def stop_camera():
    """Stop camera and release resources"""
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None


# ============================================
# Flask Routes
# ============================================

@app.route('/')
def index():
    return render_template_string(
        HTML_TEMPLATE,
        ip_address=get_ip_address()
    )


# --- Data Retention APIs ---

@app.route('/api/config/retention', methods=['GET'])
def api_get_retention():
    """Get data retention settings"""
    gps_days = get_configure('gps_retention_days')
    image_days = get_configure('image_retention_days')
    return jsonify({
        'gps_retention_days': int(gps_days) if gps_days else 30,
        'image_retention_days': int(image_days) if image_days else 15,
    })


@app.route('/api/config/retention', methods=['POST'])
def api_set_retention():
    """Set data retention settings (requires password)"""
    try:
        data = request.get_json()
        password = data.get('password', '')
        gps_days = data.get('gps_retention_days')
        image_days = data.get('image_retention_days')

        delete_password = get_delete_password()
        if not password or password != delete_password:
            return jsonify({'success': False, 'error': 'Invalid password'})

        if gps_days is not None:
            gps_days = int(gps_days)
            if gps_days < 1 or gps_days > 365:
                return jsonify({'success': False, 'error': 'GPS days must be 1-365'})
            set_configure('gps_retention_days', gps_days)

        if image_days is not None:
            image_days = int(image_days)
            if image_days < 1 or image_days > 365:
                return jsonify({'success': False, 'error': 'Image days must be 1-365'})
            set_configure('image_retention_days', image_days)

        return jsonify({'success': True})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# --- Provisioning APIs ---

@app.route('/api/status')
def api_status():
    provisioned, device_id, auth_key = is_device_provisioned()
    return jsonify({
        'provisioned': provisioned,
        'device_id': device_id or '',
        'auth_key': auth_key or ''
    })


@app.route('/api/provision', methods=['POST'])
def api_provision():
    try:
        success = provision_device()
        if success:
            _, device_id, auth_key = is_device_provisioned()
            return jsonify({
                'success': True,
                'device_id': device_id,
                'auth_key': auth_key
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Provisioning failed. Check logs.'
            })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/credentials')
def api_credentials():
    """Get credentials as JSON (for other scripts)"""
    creds = get_device_credentials()
    if creds:
        return jsonify(creds)
    return jsonify({'error': 'Not provisioned'}), 404


# --- Configuration APIs ---

@app.route('/api/config/speed', methods=['GET'])
def api_get_speed():
    """Get activation speed configuration"""
    value = get_configure('speed')
    return jsonify({
        'value': int(value) if value else 20,
        'default': int(DEFAULT_CONFIG.get('speed', 20))
    })


@app.route('/api/config/speed', methods=['POST'])
def api_set_speed():
    """Set activation speed configuration"""
    try:
        data = request.get_json()
        value = data.get('value')

        if value is None:
            return jsonify({'success': False, 'error': 'Value is required'})

        value = int(value)
        if value < 0 or value > 200:
            return jsonify({'success': False, 'error': 'Speed must be between 0 and 200 km/h'})

        success = set_configure('speed', value)
        if success:
            return jsonify({'success': True, 'value': value})
        else:
            return jsonify({'success': False, 'error': 'Failed to save configuration'})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid speed value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# --- Settings APIs (unified) ---

@app.route('/api/config/settings', methods=['GET'])
def api_get_settings():
    """Get all detection settings"""
    speed = get_configure('speed')
    led = get_configure('led_blink_enabled')
    nf = get_configure('noface_enabled')
    nf_thresh = get_configure('noface_threshold')
    return jsonify({
        'speed': int(speed) if speed else 0,
        'led_blink_enabled': led != '0' if led is not None else True,
        'noface_enabled': nf == '1' if nf is not None else False,
        'noface_threshold': int(nf_thresh) if nf_thresh else 2,
    })


@app.route('/api/config/settings', methods=['POST'])
def api_set_settings():
    """Save all detection settings"""
    try:
        data = request.get_json()

        speed = data.get('speed')
        if speed is not None:
            speed = int(speed)
            if speed < 0 or speed > 200:
                return jsonify({'success': False, 'error': 'Speed must be 0-200'})
            set_configure('speed', speed)

        led = data.get('led_blink_enabled')
        if led is not None:
            set_configure('led_blink_enabled', '1' if led else '0')

        nf = data.get('noface_enabled')
        if nf is not None:
            set_configure('noface_enabled', '1' if nf else '0')

        nf_thresh = data.get('noface_threshold')
        if nf_thresh is not None:
            nf_thresh = int(nf_thresh)
            if nf_thresh < 1 or nf_thresh > 30:
                return jsonify({'success': False, 'error': 'Threshold must be 1-30 seconds'})
            set_configure('noface_threshold', nf_thresh)

        return jsonify({'success': True})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def get_delete_password():
    """Get delete password from .env file or environment"""
    # First try to read directly from .env file
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'),
        '/home/pi/facial-tracker-firmware/.env'
    ]
    for env_file in env_paths:
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and line.startswith('DELETE_DEVICE_PASSWORD='):
                            value = line.split('=', 1)[1].strip()
                            print(f"[DEBUG] get_delete_password: Found in {env_file}: {value}")
                            return value
            except Exception as e:
                print(f"[DEBUG] get_delete_password: Error reading {env_file}: {e}")
    
    # Fallback to environment variable
    env_val = os.getenv("DELETE_DEVICE_PASSWORD", "Sapience@2128")
    print(f"[DEBUG] get_delete_password: Using os.getenv: {env_val}")
    return env_val


@app.route('/api/device/delete', methods=['POST'])
def api_delete_device():
    """Delete device credentials (requires password)"""
    try:
        data = request.get_json()
        password = data.get('password', '')
        
        # Read password dynamically from .env file
        delete_password = get_delete_password()
        
        if not password:
            return jsonify({
                'success': False,
                'error': 'Password is required'
            })
        
        if password != delete_password:
            return jsonify({
                'success': False,
                'error': 'Invalid password'
            })
        
        # Check if device is provisioned
        provisioned, device_id, _ = is_device_provisioned()
        if not provisioned:
            return jsonify({
                'success': False,
                'error': 'No device credentials to delete'
            })
        
        # Delete credentials
        success = delete_device_credentials()
        if success:
            # Restart all services to pick up the change
            services_to_restart = [
                'facial1',
                'get_gps_data1', 
                'upload_images',
                'send-data-api',
                'pi-control'
            ]
            restart_results = []
            for svc in services_to_restart:
                try:
                    result = subprocess.run(
                        ['sudo', 'systemctl', 'restart', f'{svc}.service'],
                        capture_output=True, text=True, timeout=10
                    )
                    restart_results.append(f"{svc}: {'ok' if result.returncode == 0 else 'failed'}")
                except Exception as e:
                    restart_results.append(f"{svc}: error - {e}")
            
            return jsonify({
                'success': True,
                'message': f'Device ID {device_id} deleted successfully. Services restarted: {", ".join(restart_results)}'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to delete device credentials'
            })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


# --- Monitoring Toggle APIs ---

@app.route('/api/monitoring/status')
def api_monitoring_status():
    """Check if remote monitoring services are running"""
    active_count = 0
    for svc in MONITORING_SERVICES:
        if get_service_status(svc) == 'active':
            active_count += 1
    # Consider enabled if majority of services are running
    return jsonify({'enabled': active_count > len(MONITORING_SERVICES) // 2})


@app.route('/api/monitoring/toggle', methods=['POST'])
def api_monitoring_toggle():
    """Enable or disable remote monitoring services (cloud upload, OTA, data sync)"""
    data = request.get_json()
    enable = data.get('enable', True)
    action = 'start' if enable else 'stop'

    results = []
    for svc in MONITORING_SERVICES:
        success, msg = run_service_command(svc, action)
        results.append(f"{svc}: {'ok' if success else 'failed'}")

    # Also handle OTA timer (controls periodic scheduling)
    try:
        subprocess.run(
            ['sudo', 'systemctl', action, 'ota-auto-update.timer'],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass

    return jsonify({
        'success': True,
        'enabled': enable,
        'details': results
    })


# --- Service APIs ---

@app.route('/api/services')
def api_services():
    """Get status of all services"""
    services = []
    for svc in SERVICES:
        status = get_service_status(svc['name'])
        services.append({
            'name': svc['name'],
            'display': svc['display'],
            'description': svc['description'],
            'status': status,
            'active': status == 'active'
        })
    return jsonify({'services': services})


@app.route('/api/services/<name>/<action>', methods=['POST'])
def api_service_control(name, action):
    """Start or stop a service"""
    if action not in ['start', 'stop', 'restart']:
        return jsonify({'success': False, 'message': 'Invalid action'})
    
    # Verify service is in our list
    if name not in [s['name'] for s in SERVICES]:
        return jsonify({'success': False, 'message': 'Unknown service'})
    
    success, message = run_service_command(name, action)
    return jsonify({
        'success': success,
        'message': f'{name} {action}ed' if success else message
    })


@app.route('/api/services/all/<action>', methods=['POST'])
def api_services_all(action):
    """Start or stop all services"""
    if action not in ['start', 'stop']:
        return jsonify({'success': False, 'message': 'Invalid action'})
    
    for svc in SERVICES:
        run_service_command(svc['name'], action)
    
    return jsonify({
        'success': True,
        'message': f'All services {action}ed'
    })


# --- Camera APIs ---

@app.route('/api/camera/stream')
def api_camera_stream():
    """MJPEG camera stream"""
    return Response(
        generate_camera_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/camera/stop', methods=['POST'])
def api_camera_stop():
    """Stop camera stream"""
    stop_camera()
    return jsonify({'success': True})


# --- WiFi APIs ---

@app.route('/api/wifi/status')
def api_wifi_status():
    """Get WiFi connection status"""
    return jsonify(get_wifi_status())


@app.route('/api/wifi/scan')
def api_wifi_scan():
    """Scan for WiFi networks"""
    networks = scan_wifi_networks()
    return jsonify({'networks': networks})


@app.route('/api/wifi/connect', methods=['POST'])
def api_wifi_connect():
    """Connect to a WiFi network"""
    data = request.get_json()
    ssid = data.get('ssid', '')
    password = data.get('password', '')
    
    if not ssid:
        return jsonify({'success': False, 'message': 'SSID required'})
    
    # If AP manager is available, use it for better handling
    if ap_manager:
        result = ap_manager.connect_to_wifi(ssid, password)
        return jsonify(result)
    
    success, message = connect_to_wifi(ssid, password)
    return jsonify({'success': success, 'message': message})


# --- AP Mode APIs ---

@app.route('/api/wifi/ap/start', methods=['POST'])
def api_ap_start():
    """Start WiFi Access Point mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available. Install wifi_ap_manager.py'
        })
    
    try:
        result = ap_manager.start_ap_mode()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/ap/stop', methods=['POST'])
def api_ap_stop():
    """Stop WiFi Access Point mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available'
        })
    
    try:
        result = ap_manager.stop_ap_mode()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/ap/status')
def api_ap_status():
    """Get AP mode status"""
    if not ap_manager:
        return jsonify({
            'ap_available': False,
            'ap_active': False,
        })
    
    try:
        status = ap_manager.get_status()
        status['ap_available'] = True
        return jsonify(status)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/reset', methods=['POST'])
def api_wifi_reset():
    """Reset WiFi - forget all networks and start AP mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available. Install wifi_ap_manager.py'
        })
    
    try:
        result = ap_manager.reset_wifi(start_ap=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================
# Data Retention Cleanup
# ============================================

def _run_retention_cleanup():
    """Delete old GPS data and images based on retention settings.
    GPS retention uses 'active days' — only days with data count.
    Image retention uses calendar days from folder name (YYYYMMDD format).
    """
    import shutil
    import db_helper
    from datetime import datetime, timedelta

    try:
        # Get retention settings
        gps_days_str = get_configure('gps_retention_days')
        image_days_str = get_configure('image_retention_days')
        gps_days = int(gps_days_str) if gps_days_str else 30
        image_days = int(image_days_str) if image_days_str else 15

        # --- GPS data cleanup (active days) ---
        # Get list of distinct active days, ordered newest first
        rows = db_helper.fetchall(
            "SELECT DISTINCT date(timestamp) as day FROM gps_data ORDER BY day DESC")
        if rows:
            active_days = [r['day'] for r in rows if r['day']]
            if len(active_days) > gps_days:
                # Keep only the most recent N active days
                cutoff_day = active_days[gps_days - 1]  # last day to keep
                deleted = db_helper.execute_commit(
                    "DELETE FROM gps_data WHERE date(timestamp) < ?", (cutoff_day,))
                print(f"[Retention] GPS: kept {gps_days} active days, cutoff={cutoff_day}")

                # Also clean car_data with same cutoff
                db_helper.execute_commit(
                    "DELETE FROM car_data WHERE date(timestamp) < ?", (cutoff_day,))

        # --- Image cleanup (calendar days from folder names) ---
        images_base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
        if os.path.exists(images_base):
            cutoff_date = datetime.now() - timedelta(days=image_days)
            for item in os.listdir(images_base):
                item_path = os.path.join(images_base, item)
                if not os.path.isdir(item_path):
                    continue
                # Skip 'events' and 'pending' folders — check date-named folders
                if item in ('events', 'pending'):
                    # For events folder, clean subfolders by date prefix (YYYYMMDD_*)
                    if item == 'events':
                        for event_dir in os.listdir(item_path):
                            try:
                                date_str = event_dir[:8]  # YYYYMMDD
                                folder_date = datetime.strptime(date_str, '%Y%m%d')
                                if folder_date < cutoff_date:
                                    shutil.rmtree(os.path.join(item_path, event_dir))
                                    print(f"[Retention] Deleted event folder: {event_dir}")
                            except (ValueError, OSError):
                                continue
                    continue
                # Date-named daily folders (e.g., 2026-02-10 or 20260210)
                try:
                    # Try YYYY-MM-DD format first, then YYYYMMDD
                    try:
                        folder_date = datetime.strptime(item, '%Y-%m-%d')
                    except ValueError:
                        folder_date = datetime.strptime(item, '%Y%m%d')
                    if folder_date < cutoff_date:
                        shutil.rmtree(item_path)
                        print(f"[Retention] Deleted image folder: {item}")
                except (ValueError, OSError):
                    continue

    except Exception as e:
        print(f"[Retention] Cleanup error: {e}")


def _retention_cleanup_loop():
    """Run cleanup every hour."""
    time.sleep(60)  # Wait 1 min after startup
    while True:
        try:
            _run_retention_cleanup()
        except Exception as e:
            print(f"[Retention] Loop error: {e}")
        time.sleep(3600)  # Every hour


# ============================================
# Main
# ============================================

if __name__ == '__main__':
    print("=" * 50)
    print("Device Manager UI")
    print("=" * 50)
    ip = get_ip_address()
    print(f"Open in browser: http://{ip}:5000")
    print("=" * 50)

    # Start data retention cleanup thread
    cleanup_thread = threading.Thread(target=_retention_cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Run with minimal resources
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        threaded=True,  # Need threads for camera streaming
        use_reloader=False
    )
