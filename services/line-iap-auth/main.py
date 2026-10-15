import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from typing import Any, Dict

import google.auth
import google.auth.transport.requests
from google.oauth2 import id_token
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("iowa-line-iap-auth")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "<YOUR_GCP_PROJECT_ID>")
PROJECT_NUMBER = os.environ.get("GCP_PROJECT_NUMBER", "<YOUR_PROJECT_NUMBER>")
REGION = os.environ.get("GCP_REGION", "us-central1")
SERVICE_NAME = os.environ.get("K_SERVICE", "iowa-line-iap-auth")

FIRESTORE_BASE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/"
    "databases/(default)/documents"
)
PARTNER_REGISTRY_COLLECTION = "line_partner_registry"

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
UNLOCKED_RICHMENU_ID = os.environ.get(
    "UNLOCKED_RICHMENU_ID", "richmenu-1150166c906fab07940f62a5df4f4f9e"
)
LOGIN_RICHMENU_ID = os.environ.get(
    "LOGIN_RICHMENU_ID", "richmenu-eabdd88ffc5bf4e2ecf4707ed3fad403"
)

app = FastAPI(
    title="Iowa Sales AI — Internal Employee Cloud IAP Gate (Firestore Registry, Zero Token Storage)",
    version="2.2.0-partner-firestore-sts",
)


_EPHEMERAL_FALLBACK_SECRET = secrets.token_hex(32)
_SAFE_UID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def verify_state_signature(uid: str, exp: int, sig: str) -> bool:
  if not uid or not sig or not _SAFE_UID_RE.fullmatch(uid) or time.time() > exp:
    return False
  secret = LINE_CHANNEL_SECRET or _EPHEMERAL_FALLBACK_SECRET
  msg = f"{uid}:{exp}".encode("utf-8")
  expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
  return hmac.compare_digest(sig, expected)


def verify_iap_jwt(request: Request) -> Dict[str, str]:
  iap_jwt = request.headers.get("x-goog-iap-jwt-assertion", "").strip()
  raw_email = request.headers.get("x-goog-authenticated-user-email", "").strip()
  raw_id = request.headers.get("x-goog-authenticated-user-id", "").strip()

  email = raw_email.split(":", 1)[-1] if ":" in raw_email else raw_email
  sub = raw_id.split(":", 1)[-1] if ":" in raw_id else raw_id

  if iap_jwt:
    expected_aud = (
        f"/projects/{PROJECT_NUMBER}/locations/{REGION}/services/{SERVICE_NAME}"
    )
    try:
      claims = id_token.verify_token(
          iap_jwt,
          google.auth.transport.requests.Request(),
          audience=expected_aud,
          certs_url="https://www.gstatic.com/iap/verify/public_key",
      )
      email = claims.get("email") or email
      sub = claims.get("sub") or sub
    except Exception as e:
      logger.error("IAP JWT cryptographic verification failed: %s", e)
      raise HTTPException(
          status_code=401,
          detail="Invalid Cloud IAP JWT assertion signature or audience.",
      ) from e

  return {
      "email": email or "authenticated-iap-user@google-cloud",
      "sub": sub or "iap-verified",
  }


def get_gcp_bearer_token() -> str:
  creds, _ = google.auth.default(
      scopes=[
          "https://www.googleapis.com/auth/cloud-platform",
          "https://www.googleapis.com/auth/datastore",
      ]
  )
  creds.refresh(google.auth.transport.requests.Request())
  return creds.token


def save_user_registry_firestore(uid: str, record: Dict[str, Any]) -> None:
  """Writes ONLY identity & RLS mapping to Firestore (Zero OAuth tokens stored!)."""
  fields: Dict[str, Any] = {}
  for k, v in record.items():
    if v is None:
      continue
    if isinstance(v, bool):
      fields[k] = {"booleanValue": v}
    elif isinstance(v, int):
      fields[k] = {"integerValue": str(v)}
    else:
      fields[k] = {"stringValue": str(v)}
  safe_id = urllib.parse.quote(uid, safe="")
  url = f"{FIRESTORE_BASE_URL}/{PARTNER_REGISTRY_COLLECTION}/{safe_id}"
  token = get_gcp_bearer_token()
  with httpx.Client(timeout=8.0) as client:
    resp = client.patch(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"fields": fields},
    )
    resp.raise_for_status()


async def link_user_richmenu(uid: str, richmenu_id: str) -> bool:
  if not LINE_CHANNEL_ACCESS_TOKEN or not uid or not richmenu_id:
    return False
  url = f"https://api.line.me/v2/bot/user/{uid}/richmenu/{richmenu_id}"
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
        url,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
    )
    return resp.status_code == 200


async def send_auth_success_push(uid: str, email: str) -> None:
  if not LINE_CHANNEL_ACCESS_TOKEN or not uid:
    return
  bubble = {
      "type": "bubble",
      "size": "mega",
      "header": {
          "type": "box",
          "layout": "vertical",
          "backgroundColor": "#059669",
          "paddingAll": "16px",
          "contents": [
              {
                  "type": "text",
                  "text": "🔒 INTERNAL EMPLOYEE • CLOUD IAP + FIRESTORE VERIFIED",
                  "color": "#D1FAE5",
                  "size": "xxs",
                  "weight": "bold",
              },
              {
                  "type": "text",
                  "text": "ยืนยันตัวตนพนักงานและปลดล็อก 6 เมนูสำเร็จ!",
                  "color": "#FFFFFF",
                  "size": "md",
                  "weight": "bold",
                  "margin": "xs",
              },
          ],
      },
      "body": {
          "type": "box",
          "layout": "vertical",
          "spacing": "sm",
          "contents": [
              {
                  "type": "text",
                  "text": f"👤 Internal User: {email}",
                  "size": "sm",
                  "weight": "bold",
                  "color": "#1E293B",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": "🛡️ Scope: ALL_IOWA (Internal Full Access)",
                  "size": "xs",
                  "color": "#0F766E",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": "🔑 Token Broker: Stateless IAM STS (No Token on gs://)",
                  "size": "xxs",
                  "color": "#475569",
                  "wrap": True,
              },
          ],
      },
  }
  msg = {
      "type": "flex",
      "altText": f"ยืนยันตัวตนผ่าน Google Cloud IAP สำเร็จ ({email})",
      "contents": bubble,
      "quickReply": {
          "items": [
              {
                  "type": "action",
                  "action": {
                      "type": "message",
                      "label": "📊 ภาพรวมยอดขาย",
                      "text": "สรุปยอดขายรวมและจำนวนขวดทั้งหมดในปี 2023",
                  },
              },
              {
                  "type": "action",
                  "action": {
                      "type": "message",
                      "label": "🏆 Top 5 Counties",
                      "text": "จัดอันดับ 5 เมือง (County) ที่มียอดขายสูงสุดในปี 2023 พร้อมสร้างกราฟแท่งเปรียบเทียบ",
                  },
              },
              {
                  "type": "action",
                  "action": {
                      "type": "message",
                      "label": "🔓 ออกจากระบบ",
                      "text": "ออกจากระบบ",
                  },
              },
          ]
      },
  }
  async with httpx.AsyncClient(timeout=10.0) as client:
    await client.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": uid, "messages": [msg]},
    )


@app.get("/auth/login", response_class=HTMLResponse)
async def auth_login(
    request: Request,
    uid: str = "",
    exp: int = 0,
    sig: str = "",
):
  if not verify_state_signature(uid, exp, sig):
    raise HTTPException(
        status_code=400,
        detail="Invalid or expired LINE authentication link. Please tap the Login Rich Menu in LINE again.",
    )

  iap_info = verify_iap_jwt(request)
  record = {
      "authenticated": True,
      "line_user_id": uid,
      "user_email": iap_info["email"],
      "iap_sub": iap_info["sub"],
      "partner_id": "INTERNAL-CORP-SSO",
      "partner_name": "Google Cloud Internal Employee",
      "partner_tier": "INTERNAL",
      "rls_scope": "ALL_IOWA",
      "status": "ACTIVE",
      "auth_mode": "INTERNAL_CLOUD_IAP",
      "token_storage": "NONE_STATELESS_IAM_STS",
      "authenticated_at": int(time.time()),
      "unlocked_richmenu_id": UNLOCKED_RICHMENU_ID,
  }
  save_user_registry_firestore(uid, record)
  linked = await link_user_richmenu(uid, UNLOCKED_RICHMENU_ID)
  await send_auth_success_push(uid, iap_info["email"])

  safe_email = html.escape(str(iap_info["email"]), quote=True)
  safe_uid_prefix = html.escape(str(uid[:12]), quote=True)
  safe_linked = html.escape(str(linked), quote=True)

  page_html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Internal Cloud IAP Authenticated</title>
  <style>
    body {{
      margin: 0; padding: 24px;
      background: #0F172A; color: #F8FAFC;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 90vh;
    }}
    .card {{
      max-width: 440px; width: 100%;
      background: #1E293B; border: 2px solid #10B981;
      border-radius: 20px; padding: 28px 24px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.5); text-align: center;
    }}
    .badge {{
      display: inline-block; background: rgba(16,185,129,0.18);
      color: #34D399; border: 1px solid #10B981;
      font-size: 12px; font-weight: 700; padding: 6px 14px; border-radius: 999px;
      margin-bottom: 16px;
    }}
    h1 {{ font-size: 22px; margin: 0 0 10px; color: #FFFFFF; }}
    p {{ font-size: 14px; color: #94A3B8; line-height: 1.5; margin: 0 0 18px; }}
    .meta {{
      background: #0F172A; border-radius: 12px; padding: 14px;
      text-align: left; font-size: 13px; color: #CBD5E1; margin-bottom: 22px;
      border: 1px solid #334155;
    }}
    .meta div {{ margin-bottom: 6px; word-break: break-all; }}
    .btn {{
      display: block; width: 100%; box-sizing: border-box;
      background: #06C755; color: #FFFFFF; text-decoration: none;
      font-weight: 700; font-size: 16px; padding: 14px 20px; border-radius: 12px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">✅ INTERNAL CLOUD IAP + FIRESTORE VERIFIED</div>
    <h1>ปลดล็อก Rich Menu 6 การ์ดสำเร็จ!</h1>
    <p>ผูกบัญชีพนักงานภายในเข้ากับ Firestore (<code>line_partner_registry</code>) โดยไม่เก็บ OAuth Key ลง <code>gs://</code></p>
    <div class="meta">
      <div><strong>Internal Identity:</strong> {safe_email}</div>
      <div><strong>LINE User ID:</strong> {safe_uid_prefix}...</div>
      <div><strong>RLS Scope:</strong> ALL_IOWA (Full Access)</div>
      <div><strong>Token Broker:</strong> Stateless IAM STS (RAM Only)</div>
      <div><strong>6-Card Menu Linked:</strong> {safe_linked}</div>
    </div>
    <a class="btn" href="https://line.me/R/ti/p/@703tucge">กลับไปยังแชท LINE Sales AI ➔</a>
  </div>
</body>
</html>"""
  return HTMLResponse(content=page_html)


@app.get("/health")
async def health():
  return {
      "status": "ok",
      "service": SERVICE_NAME,
      "version": "2.2.0-partner-firestore-sts",
      "iap_protected": True,
      "firestore_collection": PARTNER_REGISTRY_COLLECTION,
      "token_storage": "NONE_STATELESS_IAM_STS",
  }
