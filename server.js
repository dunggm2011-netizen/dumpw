const express = require('express');
const { TelegramClient } = require('telegram');
const { StringSession } = require('telegram/sessions');
const { Api } = require('telegram');

const app = express();
app.use(express.json());

const apiId = 123456; // <-- THAY BẰNG API_ID CỦA MÀY
const apiHash = 'your_api_hash'; // <-- THAY BẰNG API_HASH

let client = null;
let phoneCodeHash = null;
let currentPhone = null;

// ================= HTML UI =================
const HTML_PAGE = `
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Telegram Checker</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px; min-height: 100vh;
      background: radial-gradient(circle at 50% 10%, #0a1f2e 0%, #000 80%);
      color: #fff; font-family: 'Segoe UI', sans-serif;
      display: flex; align-items: center; justify-content: center;
    }
    .container {
      background: rgba(255,255,255,0.03); border: 1px solid rgba(0,150,255,0.3);
      border-radius: 16px; padding: 30px; width: 100%; max-width: 600px;
      box-shadow: 0 0 40px rgba(0,150,255,0.1);
    }
    h1 { text-align: center; color: #0af; text-shadow: 0 0 10px #0af; margin-top: 0; }
    label { display: block; margin: 10px 0 5px; color: #0af; font-size: 13px; }
    input, select {
      width: 100%; padding: 12px; margin-bottom: 15px;
      background: #000; border: 1px solid #0af; border-radius: 8px;
      color: #0af; font-size: 14px; outline: none;
    }
    button {
      width: 100%; padding: 12px; border: none; border-radius: 8px;
      background: linear-gradient(90deg, #0af, #06f);
      color: #fff; font-weight: bold; cursor: pointer; margin-bottom: 10px;
    }
    button:hover { opacity: 0.9; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    #result {
      margin-top: 15px; padding: 15px; background: rgba(0,0,0,0.5);
      border-radius: 8px; font-size: 13px; white-space: pre-wrap; display: none;
      border: 1px solid rgba(0,150,255,0.2); max-height: 400px; overflow-y: auto;
    }
    .ok { color: #0f0; } .fail { color: #f44; }
    .step { display: none; }
    .step.active { display: block; }
    .info-line { margin-bottom: 5px; }
    .info-key { color: #0af; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Telegram Checker</h1>

    <!-- Bước 1: Đăng nhập -->
    <div id="step1" class="step active">
      <label>Số điện thoại (có mã quốc gia)</label>
      <input type="text" id="phone" placeholder="+84912345678">
      <button onclick="sendCode()">Gửi mã OTP</button>
    </div>

    <!-- Bước 2: Nhập OTP -->
    <div id="step2" class="step">
      <label>Mã OTP</label>
      <input type="text" id="otp" placeholder="12345">
      <button onclick="login()">Đăng nhập</button>
    </div>

    <!-- Bước 3: Check info -->
    <div id="step3" class="step">
      <label>Loại target</label>
      <select id="targetType">
        <option value="username">Username</option>
        <option value="phone">Số điện thoại</option>
      </select>
      <label>Target</label>
      <input type="text" id="target" placeholder="@username hoặc +84912345678">
      <button onclick="checkInfo()">Check Info</button>
    </div>

    <div id="result"></div>
  </div>

  <script>
    const result = document.getElementById('result');
    function showStep(n) {
      document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
      document.getElementById('step' + n).classList.add('active');
    }
    function showResult(text, isError = false) {
      result.style.display = 'block';
      result.innerHTML = isError ? '<span class="fail">' + text + '</span>' : text;
    }

    async function sendCode() {
      const phone = document.getElementById('phone').value.trim();
      if (!phone) return showResult('Nhập số điện thoại đi.', true);
      showResult('Đang gửi OTP...');
      try {
        const res = await fetch('/api/init', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone })
        });
        const data = await res.json();
        if (data.error) return showResult('Lỗi: ' + data.error, true);
        showResult('Đã gửi OTP. Nhập mã để đăng nhập.');
        showStep(2);
      } catch (e) { showResult('Lỗi kết nối: ' + e.message, true); }
    }

    async function login() {
      const code = document.getElementById('otp').value.trim();
      if (!code) return showResult('Nhập OTP đi.', true);
      showResult('Đang đăng nhập...');
      try {
        const res = await fetch('/api/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code })
        });
        const data = await res.json();
        if (data.error) return showResult('Lỗi: ' + data.error, true);
        showResult('Đăng nhập thành công!');
        showStep(3);
      } catch (e) { showResult('Lỗi kết nối: ' + e.message, true); }
    }

    async function checkInfo() {
      const target = document.getElementById('target').value.trim();
      const type = document.getElementById('targetType').value;
      if (!target) return showResult('Nhập target đi.', true);
      showResult('Đang lấy thông tin...');
      try {
        const res = await fetch('/api/check', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target, type })
        });
        const data = await res.json();
        if (data.error) return showResult('Lỗi: ' + data.error, true);
        const info = data.info;
        let html = '<div class="ok">✅ Thông tin tài khoản:</div><br>';
        for (const [k, v] of Object.entries(info)) {
          html += '<div class="info-line"><span class="info-key">' + k + ':</span> ' + (v === null ? 'N/A' : v) + '</div>';
        }
        showResult(html);
      } catch (e) { showResult('Lỗi kết nối: ' + e.message, true); }
    }
  </script>
</body>
</html>
`;

// ================= API ENDPOINTS =================
app.get('/', (req, res) => {
  res.send(HTML_PAGE);
});

app.post('/api/init', async (req, res) => {
  const { phone } = req.body;
  if (!phone) return res.status(400).json({ error: 'Thiếu số điện thoại' });
  try {
    if (client) await client.disconnect();
    client = new TelegramClient(new StringSession(''), apiId, apiHash, { connectionRetries: 5 });
    await client.connect();
    currentPhone = phone;
    const result = await client.sendCode({ apiId, apiHash }, phone);
    phoneCodeHash = result.phoneCodeHash;
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/login', async (req, res) => {
  const { code } = req.body;
  if (!client || !phoneCodeHash) return res.status(400).json({ error: 'Chưa gửi OTP' });
  try {
    await client.invoke(new Api.auth.SignIn({
      phoneNumber: currentPhone,
      phoneCodeHash,
      phoneCode: code
    }));
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/check', async (req, res) => {
  const { target, type } = req.body;
  if (!client) return res.status(400).json({ error: 'Chưa đăng nhập' });
  try {
    const entity = await client.getEntity(target);
    const full = await client.invoke(new Api.users.GetFullUser({ id: entity.id }));
    res.json({
      ok: true,
      info: {
        id: entity.id,
        username: entity.username || null,
        firstName: entity.firstName || '',
        lastName: entity.lastName || '',
        phone: entity.phone || null,
        premium: entity.premium || false,
        verified: entity.verified || false,
        bot: entity.bot || false,
        bio: full.fullUser.about || null,
        commonChats: full.fullUser.commonChatsCount || 0,
        status: entity.status ? entity.status.className : 'Unknown'
      }
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ================= START =================
const PORT = process.env.PORT || 3000;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`Telegram Checker running on port ${PORT}`);
});
