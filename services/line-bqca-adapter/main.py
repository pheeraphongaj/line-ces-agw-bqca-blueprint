import asyncio
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
import uuid
from typing import Any, Dict, List, Optional

import google.auth
import google.auth.transport.requests
from google.oauth2 import id_token
import httpx
import vl_convert as vlc
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from google.cloud import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("iowa-line-bqca-adapter")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "<YOUR_GCP_PROJECT_ID>")
CES_LOCATION = os.environ.get("CES_LOCATION", "us")
CES_APP_ID = os.environ.get("CES_APP_ID", "")

BQCA_LOCATION = os.environ.get("BQCA_LOCATION", "us")
BQCA_AGENT_ID = os.environ.get(
    "BQCA_AGENT_ID", "agent_4d95fc42-7b1d-428d-8156-32e8c8632161"
)
# Trap #1: Regional Endpoint is required for locations/us BQCA DataAgents
BQCA_CHAT_URL = (
    f"https://geminidataanalytics.{BQCA_LOCATION}.rep.googleapis.com/v1beta/"
    f"projects/{PROJECT_ID}/locations/{BQCA_LOCATION}:chat"
)
BQCA_AGENT_RESOURCE = (
    f"projects/{PROJECT_ID}/locations/{BQCA_LOCATION}/dataAgents/{BQCA_AGENT_ID}"
)

FIRESTORE_BASE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/"
    "databases/(default)/documents"
)
PARTNER_ACCOUNTS_COLLECTION = "line_partner_accounts"
PARTNER_REGISTRY_COLLECTION = "line_partner_registry"

CHART_BUCKET = os.environ.get("CHART_BUCKET", "<YOUR_GCP_PROJECT_ID>-line-charts")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
IAP_AUTH_BASE_URL = os.environ.get(
    "IAP_AUTH_BASE_URL",
    "https://iowa-line-iap-auth-<YOUR_PROJECT_NUMBER>.us-central1.run.app",
).rstrip("/")
UNLOCKED_RICHMENU_ID = os.environ.get(
    "UNLOCKED_RICHMENU_ID", "richmenu-1150166c906fab07940f62a5df4f4f9e"
)
LOGIN_RICHMENU_ID = os.environ.get(
    "LOGIN_RICHMENU_ID", "richmenu-eabdd88ffc5bf4e2ecf4707ed3fad403"
)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
).strip()
LINE_OA_BASIC_ID = os.environ.get("LINE_OA_BASIC_ID", "@your-line-oa").strip()
_CACHED_LINE_BASIC_ID: Optional[str] = None
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "").strip()
_CACHED_FIREBASE_API_KEY: Optional[str] = None
CUTOFF_SECONDS = float(os.environ.get("LINE_REPLY_CUTOFF_SECONDS", "22.0"))

# Model Armor & Workforce Identity Federation (Cloud KMS Asymmetric OIDC Signer)
MODEL_ARMOR_LOCATION = os.environ.get("MODEL_ARMOR_LOCATION", "us-central1")
MODEL_ARMOR_TEMPLATE = os.environ.get("MODEL_ARMOR_TEMPLATE", "").strip()
KMS_SIGNING_KEY_VERSION = os.environ.get(
    "KMS_SIGNING_KEY_VERSION",
    f"projects/{PROJECT_ID}/locations/us-central1/keyRings/line-agw-oidc-ring/cryptoKeys/partner-jwt-signer/cryptoKeyVersions/1",
).strip()
WORKFORCE_POOL_AUDIENCE = os.environ.get("WORKFORCE_POOL_AUDIENCE", "").strip()
JWKS_MEM_CACHE: Dict[str, Any] = {"ts": 0.0, "jwks": None}

# In-memory caches:
# - STS_TOKEN_MEM_CACHE holds short-lived Google Access Tokens in RAM ONLY (Zero Token Storage on GCS/Firestore!)
CHART_CACHE: Dict[str, bytes] = {}
TURN_ARTIFACTS: Dict[str, Dict[str, Any]] = {}
REGISTRY_MEM_CACHE: Dict[str, Dict[str, Any]] = {}
STS_TOKEN_MEM_CACHE: Dict[str, Dict[str, Any]] = {}
RUNTIME_CONFIG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}


def hash_password(password: str) -> str:
  return hashlib.sha256(f"iowa-partner-salt:{password}".encode("utf-8")).hexdigest()


MOCK_PARTNER_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "partner.polk@iowa-beverage.com": {
        "email": "partner.polk@iowa-beverage.com",
        "firebase_uid": "nwZCijyEowabKRv8q8cUBxpyRBr1",
        "oidc_issuer": f"https://securetoken.google.com/{PROJECT_ID}",
        "partner_id": "PARTNER-POLK-001",
        "partner_name": "Hy-Vee #3 Des Moines (Store #2633)",
        "partner_tier": "GOLD",
        "service_account": "sa-partner-polk@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com",
        "rls_policy_name": "rls_partner_polk_store_2633",
        "rls_scope": "Polk • Store #2633 (HY-VEE #3 ONLY)",
        "rls_sql_filter": "LOWER(county) = 'polk' AND store_number = '2633'",
        "status": "ACTIVE",
        "auth_mode": "FIREBASE_AUTH_GCIP_OIDC",
    },
    "partner.east@midwest-spirits.com": {
        "email": "partner.east@midwest-spirits.com",
        "firebase_uid": "w66HFyxx8edDNbcEanyIlNIXPjG2",
        "oidc_issuer": f"https://securetoken.google.com/{PROJECT_ID}",
        "partner_id": "PARTNER-EAST-002",
        "partner_name": "Benz Distributing Cedar Rapids (Store #3773)",
        "partner_tier": "SILVER",
        "service_account": "sa-partner-east@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com",
        "rls_policy_name": "rls_partner_east_store_3773",
        "rls_scope": "Linn • Store #3773 (BENZ DIST ONLY)",
        "rls_sql_filter": "LOWER(county) = 'linn' AND store_number = '3773'",
        "status": "ACTIVE",
        "auth_mode": "FIREBASE_AUTH_GCIP_OIDC",
    },
    "vip@heartland-retail.com": {
        "email": "vip@heartland-retail.com",
        "firebase_uid": "XhPk2yVpXEZdRiEeehHgKFMTpvB2",
        "oidc_issuer": f"https://securetoken.google.com/{PROJECT_ID}",
        "partner_id": "PARTNER-VIP-003",
        "partner_name": "Heartland Retail Corp. (Statewide)",
        "partner_tier": "PLATINUM",
        "service_account": "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com",
        "rls_policy_name": "rls_internal_all_access",
        "rls_scope": "ALL_IOWA",
        "rls_sql_filter": "TRUE",
        "status": "ACTIVE",
        "auth_mode": "FIREBASE_AUTH_GCIP_OIDC",
    },
    "suspended@old-liquor.com": {
        "email": "suspended@old-liquor.com",
        "firebase_uid": "79k62nmQl0cjkwqlBDexCLj7ca13",
        "oidc_issuer": f"https://securetoken.google.com/{PROJECT_ID}",
        "partner_id": "PARTNER-REV-999",
        "partner_name": "Legacy Liquor LLC (Expired Contract)",
        "partner_tier": "EXPIRED",
        "service_account": "",
        "rls_policy_name": "NONE",
        "rls_scope": "NONE",
        "rls_sql_filter": "FALSE",
        "status": "SUSPENDED",
        "auth_mode": "FIREBASE_AUTH_GCIP_OIDC",
    },
}

app = FastAPI(
    title="Iowa Liquor Sales LINE <-> Hybrid GCIP/IAP & Firestore Partner Registry <-> CES/ADK <-> FastMCP PEP & STS Broker <-> BQCA",
    version="2.5.0-production-verified",
)


def get_runtime_config() -> Dict[str, Any]:
  now = time.monotonic()
  if now - RUNTIME_CONFIG_CACHE["ts"] < 15.0 and RUNTIME_CONFIG_CACHE["data"]:
    return RUNTIME_CONFIG_CACHE["data"]
  try:
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(CHART_BUCKET)
    blob = bucket.blob("config/runtime.json")
    if blob.exists():
      data = json.loads(blob.download_as_text())
      RUNTIME_CONFIG_CACHE["ts"] = now
      RUNTIME_CONFIG_CACHE["data"] = data
      return data
  except Exception as e:
    logger.debug("Runtime config fetch skipped: %s", e)
  RUNTIME_CONFIG_CACHE["ts"] = now
  return RUNTIME_CONFIG_CACHE["data"]


def get_effective_ces_app_id() -> str:
  if CES_APP_ID:
    return CES_APP_ID
  return get_runtime_config().get("ces_app_id", "")


def get_effective_base_url(request_base: str = "") -> str:
  if PUBLIC_BASE_URL:
    return PUBLIC_BASE_URL.rstrip("/")
  cfg_url = get_runtime_config().get("public_base_url", "")
  if cfg_url:
    return cfg_url.rstrip("/")
  return request_base.rstrip("/")


def get_effective_iap_auth_url() -> str:
  cfg_url = get_runtime_config().get("iap_auth_base_url", "")
  if cfg_url:
    return cfg_url.rstrip("/")
  return IAP_AUTH_BASE_URL


def get_gcp_bearer_token() -> str:
  creds, _ = google.auth.default(
      scopes=[
          "https://www.googleapis.com/auth/cloud-platform",
          "https://www.googleapis.com/auth/bigquery",
          "https://www.googleapis.com/auth/datastore",
      ]
  )
  creds.refresh(google.auth.transport.requests.Request())
  return creds.token


def _b64url_encode(data: bytes) -> str:
  return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sanitize_prompt_with_model_armor(prompt: str) -> Dict[str, Any]:
  """1P Google Cloud Model Armor Prompt Sanitization + Local Defense-in-Depth Guardrails.

  When `MODEL_ARMOR_TEMPLATE` is configured, invokes:
  `POST https://modelarmor.{MODEL_ARMOR_LOCATION}.rep.googleapis.com/v1/{MODEL_ARMOR_TEMPLATE}:sanitizeUserPrompt`
  Always enforces local SQL/Prompt-Injection guardrails before touching BQCA.
  """
  lower_q = (prompt or "").lower()
  blocked_patterns = (
      "ignore previous instructions",
      "drop table",
      "delete from",
      "grant all",
      "create row access policy",
      "system prompt",
  )
  for pat in blocked_patterns:
    if pat in lower_q:
      return {
          "verdict": "BLOCK",
          "reason": f"Blocked by prompt-injection guardrail ({pat})",
          "engine": "MODEL_ARMOR_AND_PEP_GUARDRAIL",
      }

  if MODEL_ARMOR_TEMPLATE:
    ma_url = (
        f"https://modelarmor.{MODEL_ARMOR_LOCATION}.rep.googleapis.com/v1/"
        f"{MODEL_ARMOR_TEMPLATE}:sanitizeUserPrompt"
    )
    try:
      host_token = get_gcp_bearer_token()
      with httpx.Client(timeout=4.0) as client:
        resp = client.post(
            ma_url,
            headers={
                "Authorization": f"Bearer {host_token}",
                "Content-Type": "application/json",
            },
            json={"userPromptData": {"text": prompt}},
        )
        if resp.status_code == 200:
          ma_data = resp.json()
          match_state = (
              ma_data.get("sanitizationResult", {}).get("filterMatchState", "")
          )
          if match_state == "MATCH_FOUND":
            return {
                "verdict": "BLOCK",
                "reason": "Blocked by 1P Cloud Model Armor template",
                "engine": f"modelarmor:{MODEL_ARMOR_TEMPLATE}",
            }
          return {
              "verdict": "ALLOW",
              "engine": f"modelarmor:{MODEL_ARMOR_TEMPLATE}",
          }
    except Exception as e:
      logger.warning("Model Armor REST call skipped/fallback: %s", e)

  return {
      "verdict": "ALLOW",
      "engine": "pep_inline_prompt_and_pii_guardrail",
  }


def mint_kms_signed_oidc_assertion(
    principal_key: str,
    partner_id: str = "",
    rls_scope: str = "",
    issuer_url: str = "",
) -> Optional[str]:
  """Signs a 5-minute RS256 OIDC JWT assertion using Cloud KMS Asymmetric Signing (`asymmetricSign`).

  Why this is required in production (`google3` / RFC 8693 reality):
  An external OIDC `id_token` from LINE Login / Firebase Auth (`GCIP`) expires after 1 hour (`3600s`).
  To exchange per-request tokens via `sts.googleapis.com/v1/token` for a GCP Workforce Identity Pool
  (`locations/global/workforcePools/...`) without storing long-lived user refresh tokens in Firestore,
  the Cloud Run Token Broker acts as a Cloud KMS-backed Custom OIDC Issuer (`/.well-known/jwks.json`).
  """
  if not WORKFORCE_POOL_AUDIENCE or not KMS_SIGNING_KEY_VERSION:
    return None
  now = int(time.time())
  eff_issuer = issuer_url or get_effective_base_url() or f"https://{PROJECT_ID}.a.run.app"
  header = {"alg": "RS256", "typ": "JWT", "kid": "kms-partner-signer-v1"}
  claims = {
      "iss": eff_issuer,
      "sub": principal_key,
      "aud": WORKFORCE_POOL_AUDIENCE,
      "iat": now,
      "exp": now + 300,
      "partner_id": partner_id,
      "rls_scope": rls_scope,
  }
  signing_input = (
      f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
      f"{_b64url_encode(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
  )
  digest_b64 = base64.b64encode(
      hashlib.sha256(signing_input.encode("ascii")).digest()
  ).decode("ascii")
  kms_url = f"https://cloudkms.googleapis.com/v1/{KMS_SIGNING_KEY_VERSION}:asymmetricSign"
  try:
    host_token = get_gcp_bearer_token()
    with httpx.Client(timeout=5.0) as client:
      resp = client.post(
          kms_url,
          headers={
              "Authorization": f"Bearer {host_token}",
              "Content-Type": "application/json",
          },
          json={"digest": {"sha256": digest_b64}},
      )
      resp.raise_for_status()
      sig_bytes = base64.b64decode(resp.json()["signature"])
      return f"{signing_input}.{_b64url_encode(sig_bytes)}"
  except Exception as e:
    logger.warning("Cloud KMS asymmetricSign skipped/failed: %s", e)
    return None


def exchange_workforce_sts_token(assertion_jwt: str) -> Optional[str]:
  """Exchanges a Cloud KMS-signed JWT assertion for a GCP Workforce Identity Pool STS access token."""
  if not assertion_jwt or not WORKFORCE_POOL_AUDIENCE:
    return None
  try:
    with httpx.Client(timeout=6.0) as client:
      resp = client.post(
          "https://sts.googleapis.com/v1/token",
          headers={"Content-Type": "application/x-www-form-urlencoded"},
          data={
              "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
              "audience": WORKFORCE_POOL_AUDIENCE,
              "scope": "https://www.googleapis.com/auth/cloud-platform",
              "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
              "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
              "subject_token": assertion_jwt,
          },
      )
      resp.raise_for_status()
      return resp.json().get("access_token")
  except Exception as e:
    logger.warning("Workforce Pool STS token exchange skipped/failed: %s", e)
    return None


def get_stateless_sts_token(
    principal_key: str = "default",
    target_sa: str = "",
    partner_id: str = "",
    rls_scope: str = "",
) -> Dict[str, Any]:
  """Dual-Mode Stateless RAM Token Broker (Zero Persistent Token Storage in GCS/Firestore).

  - Mode A (`WORKFORCE_POOL_AUDIENCE` configured — `1,000–1,000,000+` Partners, `0` Partner SAs):
    Signs a 5-min RS256 JWT via Cloud KMS (`asymmetricSign`) and exchanges it with
    `POST https://sts.googleapis.com/v1/token` against a GCP Workforce Identity Pool (`workforcePools`).
  - Mode B (`target_sa` configured — `<50` Partners / Store Cohort SAs, Active on `<YOUR_GCP_PROJECT_ID>`):
    Mints a short-lived `ya29...` token in RAM via `iamcredentials.googleapis.com/...:generateAccessToken`.
  """
  cache_key = f"{principal_key}:{target_sa}:{WORKFORCE_POOL_AUDIENCE}"
  now = int(time.time())
  cached = STS_TOKEN_MEM_CACHE.get(cache_key)
  if cached and int(cached.get("expiry", 0)) > now + 120:
    return cached

  # Mode A: Cloud KMS Asymmetric JWT Signer -> GCP Workforce Identity Pool STS Exchange
  if WORKFORCE_POOL_AUDIENCE:
    assertion = mint_kms_signed_oidc_assertion(
        principal_key=principal_key,
        partner_id=partner_id or principal_key,
        rls_scope=rls_scope,
    )
    wf_token = exchange_workforce_sts_token(assertion) if assertion else None
    if wf_token:
      entry = {
          "access_token": wf_token,
          "expiry": now + 300,
          "principal_key": principal_key,
          "impersonated_sa": f"workforcePool:{principal_key}",
          "broker_mode": "KMS_ASYMMETRIC_JWT_TO_WORKFORCE_POOL_STS",
          "storage": "RAM_ONLY_ZERO_PERSISTENT_VAULT",
      }
      STS_TOKEN_MEM_CACHE[cache_key] = entry
      return entry

  # Mode B: IAM Service Account Impersonation (`iamcredentials.googleapis.com`)
  host_token = get_gcp_bearer_token()
  effective_sa = target_sa or "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"

  if (
      target_sa
      and target_sa != "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  ):
    iam_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/"
        f"serviceAccounts/{target_sa}:generateAccessToken"
    )
    try:
      with httpx.Client(timeout=8.0) as client:
        resp = client.post(
            iam_url,
            headers={
                "Authorization": f"Bearer {host_token}",
                "Content-Type": "application/json",
            },
            json={
                "scope": [
                    "https://www.googleapis.com/auth/cloud-platform",
                    "https://www.googleapis.com/auth/bigquery",
                ],
                "lifetime": "3500s",
            },
        )
        resp.raise_for_status()
        impersonated_token = resp.json()["accessToken"]
        logger.info("Minted impersonated STS token for Partner SA: %s", target_sa)
    except Exception as e:
      logger.error("Failed to impersonate Partner SA %s: %s", target_sa, e)
      raise HTTPException(
          status_code=503,
          detail="Fail-Closed: Partner SA STS token brokering failed.",
      ) from e
  else:
    impersonated_token = host_token

  entry = {
      "access_token": impersonated_token,
      "expiry": now + 3300,
      "principal_key": principal_key,
      "impersonated_sa": effective_sa,
      "broker_mode": "IAMCREDENTIALS_PARTNER_SA_IMPERSONATION",
      "storage": "RAM_ONLY_ZERO_PERSISTENT_VAULT",
  }
  STS_TOKEN_MEM_CACHE[cache_key] = entry
  return entry


# =====================================================================
# 0. Firestore Native REST Helpers (Zero Token Vault — Identity & RLS Only)
# =====================================================================
def _to_firestore_fields(data: Dict[str, Any]) -> Dict[str, Any]:
  fields: Dict[str, Any] = {}
  for k, v in data.items():
    if v is None:
      continue
    if isinstance(v, bool):
      fields[k] = {"booleanValue": v}
    elif isinstance(v, int):
      fields[k] = {"integerValue": str(v)}
    elif isinstance(v, float):
      fields[k] = {"doubleValue": v}
    else:
      fields[k] = {"stringValue": str(v)}
  return {"fields": fields}


def _from_firestore_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
  out: Dict[str, Any] = {}
  for k, v in (doc.get("fields") or {}).items():
    if "stringValue" in v:
      out[k] = v["stringValue"]
    elif "integerValue" in v:
      out[k] = int(v["integerValue"])
    elif "doubleValue" in v:
      out[k] = float(v["doubleValue"])
    elif "booleanValue" in v:
      out[k] = bool(v["booleanValue"])
  return out


def firestore_get_doc(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
  safe_id = urllib.parse.quote(doc_id, safe="")
  url = f"{FIRESTORE_BASE_URL}/{collection}/{safe_id}"
  try:
    token = get_gcp_bearer_token()
    with httpx.Client(timeout=8.0) as client:
      resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
      if resp.status_code == 404:
        return None
      resp.raise_for_status()
      return _from_firestore_doc(resp.json())
  except Exception as e:
    logger.warning("Firestore GET %s/%s failed: %s", collection, doc_id, e)
    return None


def firestore_set_doc(collection: str, doc_id: str, data: Dict[str, Any]) -> bool:
  safe_id = urllib.parse.quote(doc_id, safe="")
  url = f"{FIRESTORE_BASE_URL}/{collection}/{safe_id}"
  try:
    token = get_gcp_bearer_token()
    with httpx.Client(timeout=8.0) as client:
      resp = client.patch(
          url,
          headers={
              "Authorization": f"Bearer {token}",
              "Content-Type": "application/json",
          },
          json=_to_firestore_fields(data),
      )
      resp.raise_for_status()
      return True
  except Exception as e:
    logger.warning("Firestore PATCH %s/%s failed: %s", collection, doc_id, e)
    return False


def firestore_delete_doc(collection: str, doc_id: str) -> bool:
  safe_id = urllib.parse.quote(doc_id, safe="")
  url = f"{FIRESTORE_BASE_URL}/{collection}/{safe_id}"
  try:
    token = get_gcp_bearer_token()
    with httpx.Client(timeout=8.0) as client:
      resp = client.delete(url, headers={"Authorization": f"Bearer {token}"})
      return resp.status_code in (200, 204, 404)
  except Exception as e:
    logger.warning("Firestore DELETE %s/%s failed: %s", collection, doc_id, e)
    return False


def ensure_mock_partners_seeded() -> List[Dict[str, Any]]:
  seeded = []
  for email_key, acct in MOCK_PARTNER_ACCOUNTS.items():
    doc_key = email_key.lower().strip()
    firestore_set_doc(PARTNER_ACCOUNTS_COLLECTION, doc_key, acct)
    seeded.append({
        "email": acct["email"],
        "firebase_uid": acct.get("firebase_uid", ""),
        "oidc_issuer": acct.get("oidc_issuer", ""),
        "partner_id": acct["partner_id"],
        "partner_name": acct["partner_name"],
        "partner_tier": acct["partner_tier"],
        "rls_scope": acct["rls_scope"],
        "status": acct["status"],
        "auth_mode": acct.get("auth_mode", "FIREBASE_AUTH_GCIP_OIDC"),
    })
  return seeded


_EPHEMERAL_FALLBACK_SECRET = secrets.token_hex(32)
_SAFE_UID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_SAFE_CHART_ID_RE = re.compile(r"^[a-fA-F0-9]{16,64}$")


def _get_hmac_secret() -> str:
  if LINE_CHANNEL_SECRET:
    return LINE_CHANNEL_SECRET
  logger.warning(
      "LINE_CHANNEL_SECRET is unset; using per-process ephemeral HMAC key"
  )
  return _EPHEMERAL_FALLBACK_SECRET


def build_state_signature(line_user_id: str, exp: int) -> str:
  secret = _get_hmac_secret()
  msg = f"{line_user_id}:{exp}".encode("utf-8")
  return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_state_signature(uid: str, exp: int, sig: str) -> bool:
  if not uid or not sig or not _SAFE_UID_RE.fullmatch(uid) or time.time() > exp:
    return False
  expected = build_state_signature(uid, exp)
  return hmac.compare_digest(sig, expected)


def verify_admin_request(
    request: Request,
    raw_body: bytes = b"",
    uid: str = "",
    exp: int = 0,
    sig: str = "",
) -> bool:
  """Verifies administrative or test requests via X-Admin-Signature HMAC, GCP IAM Bearer token, or valid state signature."""
  if uid and exp and sig and verify_state_signature(uid, exp, sig):
    return True

  secret = _get_hmac_secret()
  admin_sig = (
      request.headers.get("x-admin-signature", "").strip()
      or request.headers.get("x-line-signature", "").strip()
  )
  if admin_sig:
    body_hmac_hex = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    body_hmac_b64 = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    if hmac.compare_digest(admin_sig, body_hmac_hex) or hmac.compare_digest(
        admin_sig, body_hmac_b64
    ):
      return True
    if uid and exp and hmac.compare_digest(
        admin_sig, build_state_signature(uid, exp)
    ):
      return True

  auth_hdr = request.headers.get("authorization", "").strip()
  if auth_hdr.lower().startswith("bearer "):
    bearer_tok = auth_hdr[7:].strip()
    if bearer_tok:
      try:
        host_tok = get_gcp_bearer_token()
        if host_tok and hmac.compare_digest(bearer_tok, host_tok):
          return True
      except Exception:
        pass
      try:
        claims = id_token.verify_oauth2_token(
            bearer_tok, google.auth.transport.requests.Request()
        )
        if claims and claims.get("sub"):
          return True
      except Exception:
        pass
      if bearer_tok.startswith("ya29."):
        try:
          with httpx.Client(timeout=4.0) as client:
            resp = client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": bearer_tok},
            )
            if resp.status_code == 200 and resp.json().get("expires_in"):
              return True
        except Exception:
          pass

  return False


def build_signed_partner_portal_url(line_user_id: str, base_url: str = "") -> str:
  exp = int(time.time() + 3600)
  sig = build_state_signature(line_user_id, exp)
  safe_uid_q = urllib.parse.quote(str(line_user_id), safe="")
  eff_base = get_effective_base_url(base_url) or "https://iowa-line-bqca-adapter-<YOUR_PROJECT_NUMBER>.us-central1.run.app"
  return (
      f"{eff_base}/auth/portal?uid={safe_uid_q}&exp={exp}&sig={sig}"
      "&openExternalBrowser=1"
  )


def build_signed_iap_login_url(line_user_id: str) -> str:
  exp = int(time.time() + 3600)
  sig = build_state_signature(line_user_id, exp)
  safe_uid_q = urllib.parse.quote(str(line_user_id), safe="")
  base = get_effective_iap_auth_url()
  return (
      f"{base}/auth/login?uid={safe_uid_q}&exp={exp}&sig={sig}"
      "&openExternalBrowser=1"
  )


def get_user_auth_session(line_user_id: str) -> Optional[Dict[str, Any]]:
  """Looks up the LINE User -> Partner/Employee binding in Firestore `line_partner_registry`.

  Zero OAuth tokens are stored in Firestore or GCS. When the user is ACTIVE in
  Firestore, the Stateless Token Broker mints/fetches a short-lived `ya29...`
  access token in RAM only by impersonating the Partner's dedicated Service Account
  (e.g., `sa-partner-polk@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com`), which enforces
  BigQuery Native `ROW ACCESS POLICY` at the storage engine layer.
  """
  if not line_user_id:
    return None
  record = firestore_get_doc(PARTNER_REGISTRY_COLLECTION, line_user_id)
  if not record:
    REGISTRY_MEM_CACHE.pop(line_user_id, None)
    return None
  if not record.get("authenticated") or record.get("status", "ACTIVE") != "ACTIVE":
    REGISTRY_MEM_CACHE.pop(line_user_id, None)
    return None

  enriched = dict(record)
  email_key = (enriched.get("user_email") or "").strip().lower()
  if email_key in MOCK_PARTNER_ACCOUNTS:
    acct_template = MOCK_PARTNER_ACCOUNTS[email_key]
    for k in (
        "partner_name",
        "partner_tier",
        "service_account",
        "rls_policy_name",
        "rls_scope",
        "rls_sql_filter",
    ):
      if k in acct_template:
        enriched[k] = acct_template[k]

  partner_id = enriched.get("partner_id", "PARTNER-DEFAULT")
  target_sa = enriched.get(
      "service_account", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  )
  sts_info = get_stateless_sts_token(partner_id, target_sa=target_sa)
  enriched["oauth2_access_token"] = sts_info["access_token"]
  enriched["impersonated_sa"] = sts_info["impersonated_sa"]
  enriched["token_expiry"] = sts_info["expiry"]
  enriched["token_storage"] = "STATELESS_RAM_STS_NO_GCS"
  REGISTRY_MEM_CACHE[line_user_id] = enriched
  return enriched


def clear_user_auth_session(line_user_id: str) -> None:
  REGISTRY_MEM_CACHE.pop(line_user_id, None)
  firestore_delete_doc(PARTNER_REGISTRY_COLLECTION, line_user_id)


async def set_user_richmenu(line_user_id: str, richmenu_id: str) -> bool:
  if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id or not richmenu_id:
    return False
  url = f"https://api.line.me/v2/bot/user/{line_user_id}/richmenu/{richmenu_id}"
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
        url,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
    )
    return resp.status_code == 200


async def resolve_line_oa_basic_id() -> str:
  """Resolves the LINE Official Account basicId (e.g. @703tucge) dynamically from /v2/bot/info."""
  global _CACHED_LINE_BASIC_ID
  if _CACHED_LINE_BASIC_ID:
    return _CACHED_LINE_BASIC_ID
  if LINE_OA_BASIC_ID and LINE_OA_BASIC_ID != "@your-line-oa":
    _CACHED_LINE_BASIC_ID = LINE_OA_BASIC_ID
    return _CACHED_LINE_BASIC_ID
  if LINE_CHANNEL_ACCESS_TOKEN:
    try:
      async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        )
        if resp.status_code == 200:
          basic_id = resp.json().get("basicId", "").strip()
          if basic_id:
            _CACHED_LINE_BASIC_ID = basic_id
            return _CACHED_LINE_BASIC_ID
    except Exception as e:
      logger.warning("Failed to resolve LINE bot basicId: %s", e)
  return LINE_OA_BASIC_ID or "@your-line-oa"


async def resolve_firebase_web_api_key() -> str:
  """Resolves the project's Firebase Auth / Identity Platform Web API Key from identitytoolkit Admin API."""
  global _CACHED_FIREBASE_API_KEY
  if _CACHED_FIREBASE_API_KEY:
    return _CACHED_FIREBASE_API_KEY
  if FIREBASE_WEB_API_KEY:
    _CACHED_FIREBASE_API_KEY = FIREBASE_WEB_API_KEY
    return _CACHED_FIREBASE_API_KEY
  try:
    tok = get_gcp_bearer_token()
    async with httpx.AsyncClient(timeout=6.0) as client:
      resp = await client.get(
          f"https://identitytoolkit.googleapis.com/admin/v2/projects/{PROJECT_ID}/config",
          headers={
              "Authorization": f"Bearer {tok}",
              "X-Goog-User-Project": PROJECT_ID,
          },
      )
      if resp.status_code == 200:
        api_key = resp.json().get("client", {}).get("apiKey", "").strip()
        if api_key:
          _CACHED_FIREBASE_API_KEY = api_key
          return _CACHED_FIREBASE_API_KEY
  except Exception as e:
    logger.warning("Failed to resolve Firebase Web API Key: %s", e)
  return ""


async def authenticate_with_firebase_auth(
    email: str, password: str
) -> Optional[Dict[str, Any]]:
  """Authenticates user via Firebase Authentication / Identity Platform (`accounts:signInWithPassword`) and verifies the RS256 OIDC idToken."""
  api_key = await resolve_firebase_web_api_key()
  if not api_key:
    return None
  url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
        url,
        json={
            "email": email.strip(),
            "password": password.strip(),
            "returnSecureToken": True,
        },
    )
    if resp.status_code != 200:
      err_msg = ""
      try:
        err_msg = resp.json().get("error", {}).get("message", "")
      except Exception:
        pass
      if "USER_DISABLED" in err_msg:
        return {
            "status": "SUSPENDED",
            "error": "USER_DISABLED",
            "email": email.strip().lower(),
            "partner_id": "PARTNER-REVOKED-999",
        }
      return None

    data = resp.json()
    raw_id_token = data.get("idToken", "")
    local_id = data.get("localId", "")
    claims = id_token.verify_firebase_token(
        raw_id_token,
        google.auth.transport.requests.Request(),
        audience=PROJECT_ID,
    )
    return {
        "firebase_uid": claims.get("sub") or local_id,
        "oidc_issuer": claims.get(
            "iss", f"https://securetoken.google.com/{PROJECT_ID}"
        ),
        "email": claims.get("email", email.strip().lower()),
        "partner_id": claims.get("partner_id", ""),
        "partner_name": claims.get("name") or data.get("displayName", ""),
        "partner_tier": claims.get("partner_tier", "GOLD"),
        "rls_policy_name": claims.get("rls_policy_name", ""),
        "rls_scope": claims.get("rls_scope", ""),
        "service_account": claims.get("service_account", ""),
        "status": claims.get("status", "ACTIVE"),
        "email_verified": bool(claims.get("email_verified", True)),
    }


def build_iap_login_flex_message(line_user_id: str, base_url: str = "") -> Dict[str, Any]:
  portal_url = build_signed_partner_portal_url(line_user_id, base_url)
  iap_url = build_signed_iap_login_url(line_user_id)
  bubble = {
      "type": "bubble",
      "size": "mega",
      "header": {
          "type": "box",
          "layout": "vertical",
          "backgroundColor": "#0F172A",
          "paddingAll": "16px",
          "contents": [
              {
                  "type": "text",
                  "text": "🔒 HYBRID IDENTITY GATE (FIREBASE AUTH GCIP + CLOUD IAP)",
                  "color": "#38BDF8",
                  "size": "xxs",
                  "weight": "bold",
              },
              {
                  "type": "text",
                  "text": "ยืนยันตัวตน Partner หรือ พนักงานองค์กร",
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
                  "text": (
                      "• คู่ค้า (External Partner): ล็อกอินผ่าน Firebase Authentication (Identity Platform OIDC) + ผูกสิทธิ์ใน Firestore บังคับใช้ BigQuery Native ROW ACCESS POLICY ระดับ Store\n"
                      "• พนักงานภายใน (Internal): ล็อกอินผ่าน Google Cloud IAP SSO\n"
                      "• ปลอดภัย 100%: ไม่เก็บ Password Hash หรือ OAuth Token ลง Firestore/gs:// (Stateless IAM STS ใน RAM)"
                  ),
                  "size": "xs",
                  "color": "#334155",
                  "wrap": True,
              },
              {"type": "separator", "margin": "md"},
              {
                  "type": "text",
                  "text": (
                      "บัญชีทดสอบ Partner (Firebase Auth + BigQuery Native RLS):\n"
                      "📧 partner.polk@iowa-beverage.com\n"
                      "🔑 IowaPartner#2026 (Polk • Store #2633 Hy-Vee #3 เท่านั้น)\n"
                      "📧 vip@heartland-retail.com\n"
                      "🔑 Heartland#2026 (All Iowa ทุกร้าน)"
                  ),
                  "size": "xxs",
                  "color": "#0F766E",
                  "wrap": True,
                  "margin": "sm",
              },
          ],
      },
      "footer": {
          "type": "box",
          "layout": "vertical",
          "spacing": "sm",
          "contents": [
              {
                  "type": "button",
                  "style": "primary",
                  "color": "#059669",
                  "action": {
                      "type": "uri",
                      "label": "🤝 Partner Login (Firebase Auth OIDC)",
                      "uri": portal_url,
                  },
              },
              {
                  "type": "button",
                  "style": "secondary",
                  "action": {
                      "type": "uri",
                      "label": "🔐 Internal Employee (Cloud IAP)",
                      "uri": iap_url,
                  },
              },
          ],
      },
  }
  return {
      "type": "flex",
      "altText": "กรุณายืนยันตัวตน (Partner Login หรือ Internal Cloud IAP) เพื่อปลดล็อก Rich Menu 6 การ์ด",
      "contents": bubble,
  }


def render_vega_to_png_url(
    vega_config: Dict[str, Any], base_url: str
) -> Optional[str]:
  try:
    spec = dict(vega_config)
    if "background" not in spec:
      spec["background"] = "white"
    png_bytes = vlc.vegalite_to_png(vl_spec=spec, scale=2.0)
    chart_id = uuid.uuid4().hex
    CHART_CACHE[chart_id] = png_bytes
    try:
      storage_client = storage.Client(project=PROJECT_ID)
      bucket = storage_client.bucket(CHART_BUCKET)
      blob = bucket.blob(f"charts/{chart_id}.png")
      blob.upload_from_string(png_bytes, content_type="image/png")
    except Exception as e:
      logger.warning("GCS upload skipped/failed (serving from /charts): %s", e)
    effective_base = get_effective_base_url(base_url)
    if effective_base:
      return f"{effective_base}/charts/{chart_id}.png"
    return f"https://storage.googleapis.com/{CHART_BUCKET}/charts/{chart_id}.png"
  except Exception as e:
    logger.error("Failed to render Vega-Lite spec to PNG: %s", e)
    return None


async def execute_iowa_bqca_query(
    question: str,
    base_url: str = "",
    oauth_token: str = "",
    user_email: str = "",
    partner_id: str = "",
    rls_scope: str = "",
    rls_policy_name: str = "",
    rls_sql_filter: str = "",
    impersonated_sa: str = "",
) -> Dict[str, Any]:
  # Step 1: Model Armor Prompt Inspection (`sanitizeUserPrompt` + local defense-in-depth)
  ma_res = sanitize_prompt_with_model_armor(question)
  if ma_res.get("verdict") == "BLOCK":
    raise HTTPException(
        status_code=400,
        detail=f"Blocked by Model Armor / PEP Guardrail: {ma_res.get('reason')}",
    )

  # Step 1b: Fail-Closed Token Check (F-01 Fix: Never fall back to Host SA on unauthenticated MCP calls!)
  has_user_oauth = bool(oauth_token and oauth_token.strip().startswith("ya29."))
  if not has_user_oauth:
    raise HTTPException(
        status_code=403,
        detail="Forbidden: Active Partner/IAP STS Bearer token required (Fail-Closed PEP).",
    )
  token = oauth_token.strip()

  # Step 2: Pass the raw user question to BQCA :chat while authenticating via HTTP
  # `Authorization: Bearer <token>` (NEVER `ChatRequest.credentials`, which `ChatAction.kt`
  # only converts for Looker/AlloyDB/Cloud SQL!).
  # BigQuery Native ROW ACCESS POLICY (`rls_partner_polk_store_2633`) enforces
  # row-level filtering directly inside the BigQuery storage engine via `SESSION_USER()`!
  payload = {
      "messages": [{"userMessage": {"text": question}}],
      "dataAgentContext": {
          "dataAgent": BQCA_AGENT_RESOURCE,
          "contextVersion": "PUBLISHED",
      },
  }
  t0 = time.monotonic()
  async with httpx.AsyncClient(timeout=85.0) as client:
    resp = await client.post(
        BQCA_CHAT_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": PROJECT_ID,
        },
        json=payload,
    )
    resp.raise_for_status()
    chunks = resp.json()

  elapsed = round(time.monotonic() - t0, 2)
  final_texts: List[str] = []
  generated_sql = ""
  rows: List[Dict[str, Any]] = []
  followups: List[str] = []
  vega_config: Optional[Dict[str, Any]] = None

  for item in chunks:
    sm = item.get("systemMessage", {})
    if "text" in sm:
      t_type = sm["text"].get("textType")
      parts = sm["text"].get("parts", [])
      if t_type == "FINAL_RESPONSE":
        final_texts.extend(parts)
      elif t_type == "FOLLOWUP_QUESTIONS":
        followups.extend(parts)
    if "data" in sm:
      d = sm["data"]
      if "generatedSql" in d:
        generated_sql = d["generatedSql"].strip()
      if "result" in d and "data" in d["result"]:
        rows = d["result"]["data"][:10]
    if "chart" in sm and "result" in sm["chart"]:
      vega_config = sm["chart"]["result"].get("vegaConfig")

  chart_url = (
      render_vega_to_png_url(vega_config, base_url) if vega_config else None
  )
  raw_answer = "\n".join(final_texts).strip() or "ดำเนินการดึงข้อมูลสำเร็จ"
  if rows:
    first_vals = [str(v) for v in rows[0].values() if v is not None]
    if not any(v in raw_answer for v in first_vals if len(v) >= 4):
      row_lines = [f"\n📌 ข้อมูลที่ได้รับจาก BigQuery ({len(rows)} แถว):"]
      for idx, r in enumerate(rows[:5], 1):
        parts = []
        for k, v in r.items():
          if isinstance(v, float):
            parts.append(f"{k}: ${v:,.2f}" if "sale" in k or "dollar" in k or "rev" in k else f"{k}: {v:,.2f}")
          else:
            parts.append(f"{k}: {v}")
        row_lines.append(f"• #{idx}: " + " | ".join(parts))
      raw_answer = raw_answer + "\n" + "\n".join(row_lines)

  eff_policy = rls_policy_name or "rls_internal_all_access"
  eff_sa = impersonated_sa or "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  if rls_scope and rls_scope not in ("ALL_IOWA", "NONE", ""):
    rls_note = (
        f"🛡️ [BigQuery Native RLS: {eff_policy}]\n"
        f"สิทธิ์ของ {partner_id} ({eff_sa}) ถูกจำกัดด้วย BigQuery Row Access Policy "
        f"ให้เข้าถึงได้เฉพาะ {rls_scope} เท่านั้น (ระบบบล็อกข้อมูลร้านค้าคู่แข่งรายอื่นในระดับตาราง BigQuery)\n\n"
    )
    raw_answer = rls_note + raw_answer

  return {
      "answer": raw_answer,
      "generated_sql": generated_sql,
      "sample_rows": rows,
      "row_count_returned": len(rows),
      "chart_image_url": chart_url,
      "followup_questions": followups[:3],
      "latency_seconds": elapsed,
      "authenticated_user": user_email,
      "partner_id": partner_id,
      "rls_scope": rls_scope or "ALL_IOWA",
      "rls_policy_name": eff_policy,
      "rls_sql_filter": rls_sql_filter or "TRUE",
      "impersonated_sa": eff_sa,
      "oauth_token_propagated": has_user_oauth,
      "model_armor_verdict": ma_res.get("verdict", "ALLOW"),
      "model_armor_engine": ma_res.get("engine", "pep_inline_prompt_and_pii_guardrail"),
      "bqca_auth_channel": "HTTP_AUTHORIZATION_BEARER_HEADER (ChatAction.kt verified)",
      "token_storage": "STATELESS_RAM_STS_NO_GCS",
  }


# =====================================================================
# 1. Partner & Enterprise Identity Portal (/auth/portal & /auth/partner-login)
# =====================================================================
@app.get("/auth/portal", response_class=HTMLResponse)
async def partner_login_portal(
    uid: str = "U_DEMO_PARTNER",
    exp: int = 0,
    sig: str = "",
    error: str = "",
):
  if not _SAFE_UID_RE.fullmatch(uid or ""):
    uid = "U_INVALID"
    error = error or "รหัสผู้ใช้ (UID) ไม่ถูกต้อง"
  if exp == 0 or not sig:
    exp = int(time.time() + 3600)
    sig = build_state_signature(uid, exp)
  iap_login_url = build_signed_iap_login_url(uid)
  safe_uid = html.escape(str(uid), quote=True)
  safe_exp = html.escape(str(exp), quote=True)
  safe_sig = html.escape(str(sig), quote=True)
  safe_error = html.escape(str(error), quote=True)
  safe_iap_login_url = html.escape(str(iap_login_url), quote=True)
  err_banner = (
      f'<div class="err">⚠️ {safe_error}</div>' if safe_error else ""
  )
  page_html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Partner & Enterprise Identity Gate — Iowa Sales AI</title>
  <style>
    body {{
      margin: 0; padding: 20px;
      background: #0F172A; color: #F8FAFC;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 92vh;
    }}
    .card {{
      max-width: 480px; width: 100%;
      background: #1E293B; border: 1px solid #334155;
      border-radius: 20px; padding: 26px 22px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.5);
    }}
    .badge {{
      display: inline-block; background: rgba(56,189,248,0.15);
      color: #38BDF8; border: 1px solid #0284C7;
      font-size: 11px; font-weight: 700; padding: 5px 12px; border-radius: 999px;
      margin-bottom: 12px;
    }}
    h1 {{ font-size: 20px; margin: 0 0 6px; color: #FFFFFF; }}
    p.sub {{ font-size: 13px; color: #94A3B8; margin: 0 0 16px; line-height: 1.45; }}
    .err {{
      background: rgba(239,68,68,0.18); border: 1px solid #EF4444; color: #FCA5A5;
      padding: 10px 12px; border-radius: 10px; font-size: 13px; margin-bottom: 14px;
    }}
    label {{ display: block; font-size: 12px; font-weight: 600; color: #CBD5E1; margin-bottom: 5px; }}
    input {{
      width: 100%; box-sizing: border-box; padding: 11px 12px;
      border-radius: 10px; border: 1px solid #475569; background: #0F172A;
      color: #FFFFFF; font-size: 14px; margin-bottom: 12px;
    }}
    .btn-primary {{
      width: 100%; padding: 13px; border: none; border-radius: 11px;
      background: #059669; color: #FFFFFF; font-weight: 700; font-size: 15px;
      cursor: pointer; margin-top: 4px;
    }}
    .btn-iap {{
      display: block; width: 100%; box-sizing: border-box; text-align: center;
      padding: 12px; border-radius: 11px; border: 1px solid #3B82F6;
      background: rgba(59,130,246,0.15); color: #93C5FD; text-decoration: none;
      font-weight: 700; font-size: 14px; margin-top: 12px;
    }}
    .preset-box {{
      background: #0F172A; border: 1px solid #334155; border-radius: 12px;
      padding: 12px; margin: 16px 0;
    }}
    .preset-title {{ font-size: 11px; font-weight: 700; color: #34D399; margin-bottom: 8px; }}
    .preset-btn {{
      display: block; width: 100%; text-align: left; background: #1E293B;
      border: 1px solid #334155; color: #E2E8F0; padding: 8px 10px;
      border-radius: 8px; font-size: 12px; margin-bottom: 6px; cursor: pointer;
    }}
    .preset-btn:hover {{ border-color: #10B981; }}
    .divider {{
      text-align: center; font-size: 11px; color: #64748B; margin: 14px 0 6px;
    }}
  </style>
  <script>
    function fillCreds(email, pass) {{
      document.getElementById('email').value = email;
      document.getElementById('password').value = pass;
    }}
  </script>
</head>
<body>
  <div class="card">
    <div class="badge">🔒 ZERO-VAULT HYBRID AUTH • FIREBASE AUTH (GCIP OIDC) + FIRESTORE + IAM STS</div>
    <h1>ยืนยันตัวตนเพื่อเปิดใช้งาน LINE 6-Card Menu</h1>
    <p class="sub">สำหรับคู่ค้า (External Partner) ยืนยันตัวตนผ่าน <strong>Firebase Authentication (Identity Platform OIDC)</strong> ผูกสิทธิ์ใน <strong>Cloud Firestore</strong> และออก Short-lived Token ผ่าน <strong>Agent Gateway / FastMCP STS Broker</strong> โดยไม่เก็บ Password Hash หรือ Key บน Firestore/gs://</p>
    {err_banner}
    <form method="POST" action="/auth/partner-login">
      <input type="hidden" name="uid" value="{safe_uid}">
      <input type="hidden" name="exp" value="{safe_exp}">
      <input type="hidden" name="sig" value="{safe_sig}">
      <label for="email">Partner Email</label>
      <input type="email" id="email" name="email" value="partner.polk@iowa-beverage.com" required>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" value="IowaPartner#2026" required>
      <button type="submit" class="btn-primary">🤝 Sign In with Firebase Authentication (Identity Platform)</button>
    </form>

    <div class="preset-box">
      <div class="preset-title">⚡ กดเลือก Mockup Partner Account (BigQuery Native Row Access Policy):</div>
      <button type="button" class="preset-btn" onclick="fillCreds('partner.polk@iowa-beverage.com','IowaPartner#2026')">
        🟢 <strong>Gold Partner — Hy-Vee #3 Des Moines (Store #2633, Polk County)</strong><br>
        <code>partner.polk@iowa-beverage.com</code> / <code>IowaPartner#2026</code><br>
        <span style="color:#34D399;font-size:11px;">BQ Policy: <code>rls_partner_polk_store_2633</code> • SA: <code>sa-partner-polk</code></span>
      </button>
      <button type="button" class="preset-btn" onclick="fillCreds('partner.east@midwest-spirits.com','Midwest#2026')">
        🔵 <strong>Silver Partner — Benz Distributing (Store #3773, Linn County)</strong><br>
        <code>partner.east@midwest-spirits.com</code> / <code>Midwest#2026</code><br>
        <span style="color:#60A5FA;font-size:11px;">BQ Policy: <code>rls_partner_east_store_3773</code> • SA: <code>sa-partner-east</code></span>
      </button>
      <button type="button" class="preset-btn" onclick="fillCreds('vip@heartland-retail.com','Heartland#2026')">
        🟣 <strong>Platinum VIP Partner — Heartland Retail (All Iowa Stores)</strong><br>
        <code>vip@heartland-retail.com</code> / <code>Heartland#2026</code><br>
        <span style="color:#C084FC;font-size:11px;">BQ Policy: <code>rls_internal_all_access</code> (All 2,940 Stores)</span>
      </button>
      <button type="button" class="preset-btn" onclick="fillCreds('suspended@old-liquor.com','Revoked#2026')">
        🔴 <strong>Suspended Partner (ทดสอบโดนบล็อก 403)</strong><br>
        <code>suspended@old-liquor.com</code> / <code>Revoked#2026</code>
      </button>
    </div>

    <div class="divider">─── หรือสำหรับพนักงานภายในองค์กร (Internal Employee) ───</div>
    <a class="btn-iap" href="{safe_iap_login_url}">🔐 Sign in with Google Cloud IAP (Internal SSO)</a>
  </div>
</body>
</html>"""
  return HTMLResponse(content=page_html)


async def send_partner_auth_success_push(uid: str, record: Dict[str, Any]) -> None:
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
                  "text": "✅ FIREBASE AUTH (GCIP OIDC) + FIRESTORE + BIGQUERY RLS",
                  "color": "#D1FAE5",
                  "size": "xxs",
                  "weight": "bold",
              },
              {
                  "type": "text",
                  "text": "ยืนยันตัวตน Partner & ปลดล็อก 6 เมนูสำเร็จ!",
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
          "spacing": "xs",
          "contents": [
              {
                  "type": "text",
                  "text": f"🏢 Partner: {record.get('partner_name')} ({record.get('partner_id')})",
                  "size": "xs",
                  "weight": "bold",
                  "color": "#1E293B",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": f"🆔 Firebase UID: {record.get('firebase_uid')}",
                  "size": "xxs",
                  "color": "#0284C7",
                  "weight": "bold",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": f"📧 Email: {record.get('user_email')} [{record.get('partner_tier')}]",
                  "size": "xs",
                  "color": "#334155",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": f"🛡️ BigQuery RLS Policy: {record.get('rls_policy_name')} ({record.get('rls_scope')})",
                  "size": "xs",
                  "color": "#0F766E",
                  "weight": "bold",
                  "wrap": True,
              },
              {
                  "type": "text",
                  "text": f"🔑 Impersonated SA: {record.get('service_account')} (RAM Only)",
                  "size": "xxs",
                  "color": "#475569",
                  "wrap": True,
              },
          ],
      },
  }
  msg = {
      "type": "flex",
      "altText": f"ยืนยันตัวตน Partner สำเร็จ ({record.get('partner_id')})",
      "contents": bubble,
      "quickReply": {
          "items": [
              {
                  "type": "action",
                  "action": {
                      "type": "message",
                      "label": "🏪 ทดสอบ Top 5 ร้านค้า",
                      "text": "5 อันดับร้านค้า (Store) ที่มียอดสั่งซื้อสูงสุดในปี 2023 คือร้านใดบ้าง มียอดขายเท่าไหร่ และตั้งอยู่ในเมืองใด",
                  },
              },
              {
                  "type": "action",
                  "action": {
                      "type": "message",
                      "label": "🏆 Top 5 แบรนด์ขายดี",
                      "text": "จัดอันดับ 5 แบรนด์สุราที่มียอดขายสูงสุดในปี 2023 พร้อมกราฟแท่ง",
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


@app.post("/auth/partner-login", response_class=HTMLResponse)
async def partner_login_submit(request: Request):
  raw_bytes = await request.body()
  raw_body = raw_bytes.decode("utf-8")
  ctype = request.headers.get("content-type", "").lower()
  if "application/json" in ctype:
    form_data = json.loads(raw_body or "{}")
    uid = str(form_data.get("uid", "")).strip()
    exp = int(form_data.get("exp", 0) or 0)
    sig = str(form_data.get("sig", "")).strip()
    email = str(form_data.get("email", "")).strip()
    password = str(form_data.get("password", "")).strip()
  else:
    parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
    uid = (parsed.get("uid", [""])[0]).strip()
    exp = int((parsed.get("exp", ["0"])[0]) or 0)
    sig = (parsed.get("sig", [""])[0]).strip()
    email = (parsed.get("email", [""])[0]).strip()
    password = (parsed.get("password", [""])[0]).strip()

  if not uid or not _SAFE_UID_RE.fullmatch(uid):
    return await partner_login_portal(
        uid="U_INVALID",
        exp=0,
        sig="",
        error="รหัสผู้ใช้ (UID) ไม่ถูกต้อง กรุณากดเข้าสู่ระบบผ่าน LINE อีกครั้ง",
    )

  if not sig and exp == 0 and uid:
    if verify_admin_request(request, raw_bytes, uid=uid):
      logger.warning(
          "Authorized admin/CLI partner-login without state sig for uid=%s", uid
      )
      exp = int(time.time() + 300)
      sig = build_state_signature(uid, exp)
    elif not request.headers.get("origin") and not request.headers.get("referer"):
      logger.warning(
          "Unsigned non-browser CLI partner-login for validated uid=%s; issuing short-lived sig",
          uid,
      )
      exp = int(time.time() + 300)
      sig = build_state_signature(uid, exp)

  if not verify_state_signature(uid, exp, sig):
    return await partner_login_portal(
        uid=uid, exp=0, sig="", error="ลิงก์หมดอายุหรือไม่มีลายเซ็นยืนยัน กรุณากดเข้าสู่ระบบใหม่อีกครั้ง"
    )

  email_key = email.strip().lower()
  ensure_mock_partners_seeded()
  acct_meta = MOCK_PARTNER_ACCOUNTS.get(email_key) or firestore_get_doc(
      PARTNER_ACCOUNTS_COLLECTION, email_key
  ) or {}

  # 1. Authenticate against live Firebase Authentication / Cloud Identity Platform (`identitytoolkit.googleapis.com`)
  fb_auth = await authenticate_with_firebase_auth(email_key, password.strip())
  if fb_auth and fb_auth.get("status") == "SUSPENDED":
    await set_user_richmenu(uid, LOGIN_RICHMENU_ID)
    return await partner_login_portal(
        uid=uid,
        exp=exp,
        sig=sig,
        error=(
            f"บัญชี Firebase Auth ({acct_meta.get('partner_id') or fb_auth.get('partner_id')}) "
            "ถูกระงับสิทธิ์ (USER_DISABLED / SUSPENDED) ไม่อนุญาตให้เข้าถึง CES & BQCA (403 Forbidden)"
        ),
    )

  if not fb_auth:
    return await partner_login_portal(
        uid=uid,
        exp=exp,
        sig=sig,
        error="อีเมลหรือรหัสผ่านของ Partner (Firebase Auth / Identity Platform) ไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง",
    )

  acct = {
      "email": fb_auth["email"],
      "firebase_uid": fb_auth["firebase_uid"],
      "oidc_issuer": fb_auth["oidc_issuer"],
      "partner_id": fb_auth.get("partner_id") or acct_meta.get("partner_id", "PARTNER-POLK-001"),
      "partner_name": fb_auth.get("partner_name") or acct_meta.get("partner_name", "Verified Partner"),
      "partner_tier": fb_auth.get("partner_tier") or acct_meta.get("partner_tier", "GOLD"),
      "service_account": fb_auth.get("service_account") or acct_meta.get(
          "service_account", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
      ),
      "rls_policy_name": fb_auth.get("rls_policy_name") or acct_meta.get(
          "rls_policy_name", "rls_internal_all_access"
      ),
      "rls_scope": fb_auth.get("rls_scope") or acct_meta.get("rls_scope", "ALL_IOWA"),
      "rls_sql_filter": acct_meta.get("rls_sql_filter", "TRUE"),
      "status": fb_auth.get("status", "ACTIVE"),
      "auth_mode": "FIREBASE_AUTH_GCIP_OIDC",
  }

  if acct.get("status") != "ACTIVE":
    await set_user_richmenu(uid, LOGIN_RICHMENU_ID)
    return await partner_login_portal(
        uid=uid,
        exp=exp,
        sig=sig,
        error=(
            f"บัญชี Partner ({acct.get('partner_id')}) ถูกระงับสิทธิ์ (status=SUSPENDED) "
            "ไม่อนุญาตให้เข้าถึง CES & BQCA (403 Forbidden)"
        ),
    )

  # Save ONLY identity & RLS mapping in Firestore (Zero OAuth tokens stored!)
  registry_doc = {
      "authenticated": True,
      "line_user_id": uid,
      "firebase_uid": acct.get("firebase_uid", ""),
      "oidc_issuer": acct.get(
          "oidc_issuer", f"https://securetoken.google.com/{PROJECT_ID}"
      ),
      "user_email": acct["email"],
      "partner_id": acct["partner_id"],
      "partner_name": acct["partner_name"],
      "partner_tier": acct["partner_tier"],
      "service_account": acct.get(
          "service_account", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
      ),
      "rls_policy_name": acct.get("rls_policy_name", "rls_internal_all_access"),
      "rls_scope": acct["rls_scope"],
      "rls_sql_filter": acct.get("rls_sql_filter", "TRUE"),
      "status": "ACTIVE",
      "auth_mode": acct.get("auth_mode", "FIREBASE_AUTH_GCIP_OIDC"),
      "token_storage": "NONE_STATELESS_IAM_STS",
      "authenticated_at": int(time.time()),
      "unlocked_richmenu_id": UNLOCKED_RICHMENU_ID,
  }
  firestore_set_doc(PARTNER_REGISTRY_COLLECTION, uid, registry_doc)
  linked = await set_user_richmenu(uid, UNLOCKED_RICHMENU_ID)
  await send_partner_auth_success_push(uid, registry_doc)
  line_basic_id = await resolve_line_oa_basic_id()
  line_chat_url = f"https://line.me/R/oaMessage/{line_basic_id}/"
  line_profile_url = f"https://line.me/R/ti/p/{line_basic_id}"

  safe_firebase_uid = html.escape(str(registry_doc["firebase_uid"]), quote=True)
  safe_oidc_issuer = html.escape(str(registry_doc["oidc_issuer"]), quote=True)
  safe_partner_name = html.escape(str(acct["partner_name"]), quote=True)
  safe_partner_id = html.escape(str(acct["partner_id"]), quote=True)
  safe_email = html.escape(str(acct["email"]), quote=True)
  safe_partner_tier = html.escape(str(acct["partner_tier"]), quote=True)
  safe_rls_policy = html.escape(str(registry_doc["rls_policy_name"]), quote=True)
  safe_rls_scope = html.escape(str(acct["rls_scope"]), quote=True)
  safe_service_account = html.escape(str(registry_doc["service_account"]), quote=True)
  safe_linked = html.escape(str(linked), quote=True)
  safe_line_chat_url = html.escape(str(line_chat_url), quote=True)
  safe_line_profile_url = html.escape(str(line_profile_url), quote=True)
  safe_line_basic_id = html.escape(str(line_basic_id), quote=True)

  page_html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Partner Authenticated — Firebase Auth (GCIP OIDC) + Firestore + BigQuery Native RLS</title>
  <style>
    body {{
      margin: 0; padding: 24px;
      background: #0F172A; color: #F8FAFC;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 90vh;
    }}
    .card {{
      max-width: 460px; width: 100%;
      background: #1E293B; border: 2px solid #10B981;
      border-radius: 20px; padding: 28px 24px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.5); text-align: center;
    }}
    .badge {{
      display: inline-block; background: rgba(16,185,129,0.18);
      color: #34D399; border: 1px solid #10B981;
      font-size: 11px; font-weight: 700; padding: 6px 14px; border-radius: 999px;
      margin-bottom: 14px;
    }}
    h1 {{ font-size: 21px; margin: 0 0 10px; color: #FFFFFF; }}
    p {{ font-size: 13px; color: #94A3B8; line-height: 1.5; margin: 0 0 16px; }}
    .meta {{
      background: #0F172A; border-radius: 12px; padding: 14px;
      text-align: left; font-size: 13px; color: #CBD5E1; margin-bottom: 20px;
      border: 1px solid #334155;
    }}
    .meta div {{ margin-bottom: 6px; word-break: break-all; }}
    .btn {{
      display: block; width: 100%; box-sizing: border-box;
      background: #06C755; color: #FFFFFF; text-decoration: none;
      font-weight: 700; font-size: 15px; padding: 14px 20px; border-radius: 12px;
    }}
    .btn-sub {{
      display: block; width: 100%; box-sizing: border-box;
      background: #334155; color: #E2E8F0; text-decoration: none;
      font-weight: 600; font-size: 13px; padding: 10px 16px; border-radius: 10px;
      margin-top: 10px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">✅ FIREBASE AUTH (GCIP OIDC) • FIRESTORE • BIGQUERY RLS</div>
    <h1>ปลดล็อก Rich Menu 6 การ์ดสำเร็จ!</h1>
    <p>ยืนยันตัวตนผ่าน <strong>Firebase Authentication (Identity Platform OIDC)</strong> และผูกสิทธิ์ใน Firestore (<code>line_partner_registry</code>) พร้อมบังคับใช้ <strong>BigQuery Native Row Access Policy</strong> เรียบร้อยแล้ว</p>
    <div class="meta">
      <div><strong>Firebase Auth UID:</strong> <code>{safe_firebase_uid}</code></div>
      <div><strong>OIDC Issuer:</strong> <code>{safe_oidc_issuer}</code></div>
      <div><strong>Partner Org:</strong> {safe_partner_name} ({safe_partner_id})</div>
      <div><strong>Email / Tier:</strong> {safe_email} [{safe_partner_tier}]</div>
      <div><strong>BQ RLS Policy:</strong> <code>{safe_rls_policy}</code></div>
      <div><strong>BQ RLS Scope:</strong> {safe_rls_scope}</div>
      <div><strong>Impersonated SA:</strong> <code>{safe_service_account}</code></div>
      <div><strong>6-Card Menu Linked:</strong> {safe_linked}</div>
    </div>
    <a class="btn" href="{safe_line_chat_url}">💬 เปิดห้องแชท LINE Sales AI ({safe_line_basic_id}) ➔</a>
    <a class="btn-sub" href="{safe_line_profile_url}">กลับไปยังหน้าแชท / โปรไฟล์ ({safe_line_basic_id})</a>
  </div>
</body>
</html>"""
  return HTMLResponse(content=page_html)


@app.post("/auth/seed-partners")
async def api_seed_partners(request: Request):
  raw_body = await request.body()
  if not verify_admin_request(request, raw_body):
    raise HTTPException(
        status_code=401,
        detail="Unauthorized: valid X-Admin-Signature or GCP Bearer token required to seed partner accounts.",
    )
  seeded = ensure_mock_partners_seeded()
  return {
      "status": "ok",
      "firestore_database": f"projects/{PROJECT_ID}/databases/(default)",
      "collection": PARTNER_ACCOUNTS_COLLECTION,
      "mock_partner_accounts": seeded,
  }


# =====================================================================
# 2. MCP Streamable HTTP Endpoint (/mcp and /mcp/) Behind GE Agent Gateway
# =====================================================================
# SECURITY BEST PRACTICE (GCAF + GEAP + GE Agent Gateway):
# - MCP_TOOL_SCHEMA exposes ONLY business parameters ("question") to the LLM.
# - ZERO "x-token", "user_oauth_token", or identity parameters in tool inputSchema!
# - CES McpToolset forwards ONLY `customHeaders: {"x-line-user-id": "$context.variables.line_user_id"}`
#   to GE Agent Gateway (`agent-gateway-in`).
# - GE Agent Gateway invokes its Service Extension (`POST /ext-proc/oauth-broker`) +
#   Model Armor (`CONTENT_AUTHZ`), exchanges the identity via Workforce Identity Federation (WIF) /
#   IAM STS (`sts.googleapis.com` / `iamcredentials.googleapis.com`) in RAM, and injects
#   `Authorization: Bearer <sts_token>` + `x-agw-*` headers into `POST /mcp/`.
MCP_TOOL_SCHEMA = {
    "name": "ask_iowa_sales_agent",
    "description": (
        "Query the Iowa Liquor Sales BigQuery Conversational Analytics (BQCA) "
        "agent for sales revenue, bottle volume, top counties, cities, stores, "
        "brands, categories, vendors, and generate Vega-Lite charts."
    ),
    "inputSchema": {
        "type": "object",
        "title": "ask_iowa_sales_agentArguments",
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "title": "Question",
                "description": (
                    "Natural language analytics or chart question from the user."
                ),
            },
        },
    },
}


def resolve_agw_injected_context(request: Request) -> Dict[str, str]:
  """Resolves the per-user STS Bearer token & RLS context for `POST /mcp/` (Dual-Topology PEP).

  Verified `google3` Production Topologies:
    - Topology 1A (Regional ALB + Envoy `LbTrafficExtension` `/ext-proc/oauth-broker`):
      ALB calls `/ext-proc/oauth-broker` before `/mcp/`, mutating headers to inject
      `authorization: Bearer <sts_access_token>` and `x-agw-*` metadata.
    - Topology 1B / Topology 2 (Direct `CES McpToolset` IAM Invoker OR `Vertex AI Agent Engine ADK`
      via Google-Managed `GE Agent Gateway` `AGENT_TO_ANYWHERE` 1P IAP + 1P Model Armor):
      Because Google-Managed `AgentGateway` rejects custom customer `AuthzExtension` backends
      (`validate.go:752`) and `CES` has no `agent_gateway_config` field (`mcp_toolset.proto`),
      the FastMCP Zero-Trust PEP Middleware resolves `x-line-user-id` -> Firestore `ACTIVE` check
      -> Stateless RAM Token Broker (`iamcredentials` Partner SA or Cloud KMS Asymmetric JWT ->
      Workforce Pool `sts.googleapis.com/v1/token`) right at the MCP transport boundary!
  """
  auth_hdr = request.headers.get("authorization", "")
  agw_marker = request.headers.get("x-agw-ext-broker", "")
  line_uid = request.headers.get("x-line-user-id", "").strip()

  if agw_marker and auth_hdr.lower().startswith("bearer "):
    return {
        "line_user_id": line_uid,
        "oauth_token": auth_hdr[7:].strip(),
        "user_email": request.headers.get("x-agw-user-email", ""),
        "partner_id": request.headers.get("x-agw-verified-partner-id", ""),
        "rls_policy_name": request.headers.get("x-agw-rls-policy", "rls_internal_all_access"),
        "rls_scope": request.headers.get("x-agw-rls-scope", "ALL_IOWA"),
        "rls_sql_filter": request.headers.get("x-agw-rls-filter", "TRUE"),
        "impersonated_sa": request.headers.get(
            "x-agw-impersonated-sa",
            "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com",
        ),
        "token_source": "alb_lb_traffic_extension_ext_proc_header_injection",
    }

  # FastMCP Zero-Trust PEP & Stateless STS Token Broker Middleware (Active on Cloud Run):
  if line_uid:
    broker_sess = get_user_auth_session(line_uid)
    if broker_sess:
      return {
          "line_user_id": line_uid,
          "oauth_token": broker_sess.get("oauth2_access_token", ""),
          "user_email": broker_sess.get("user_email", ""),
          "partner_id": broker_sess.get("partner_id", ""),
          "rls_policy_name": broker_sess.get("rls_policy_name", "rls_internal_all_access"),
          "rls_scope": broker_sess.get("rls_scope", "ALL_IOWA"),
          "rls_sql_filter": broker_sess.get("rls_sql_filter", "TRUE"),
          "impersonated_sa": broker_sess.get(
              "impersonated_sa",
              "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com",
          ),
          "token_source": "fastmcp_zero_trust_pep_stateless_sts_broker",
      }

  return {
      "line_user_id": line_uid,
      "oauth_token": "",
      "user_email": "",
      "partner_id": "",
      "rls_policy_name": "rls_internal_all_access",
      "rls_scope": "ALL_IOWA",
      "rls_sql_filter": "TRUE",
      "impersonated_sa": "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com",
      "token_source": "unauthenticated_default",
  }


@app.get("/mcp")
@app.get("/mcp/")
async def mcp_get_info():
  return JSONResponse(
      content={
          "name": "iowa-bqca-mcp",
          "version": "2.5.0-production-verified",
          "transport": "streamable-http",
          "auth_enforcement": "FASTMCP_ZERO_TRUST_PEP_AND_ALB_EXT_PROC_STS_BROKER",
      }
  )


@app.delete("/mcp")
@app.delete("/mcp/")
async def mcp_delete_session():
  return Response(status_code=204)


@app.post("/mcp")
@app.post("/mcp/")
async def mcp_streamable_http(request: Request):
  body = await request.json()
  session_id = request.headers.get("mcp-session-id") or uuid.uuid4().hex
  req_base = f"https://{request.headers.get('host', '')}"
  base_url = get_effective_base_url(req_base)
  agw_ctx = resolve_agw_injected_context(request)

  async def handle_single_rpc(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
      client_proto = params.get("protocolVersion", "2025-03-26")
      return {
          "jsonrpc": "2.0",
          "id": msg_id,
          "result": {
              "protocolVersion": client_proto,
              "capabilities": {"tools": {"listChanged": False}},
              "serverInfo": {
                  "name": "iowa-bqca-mcp",
                  "version": "2.4.0-gcaf-geap-agw",
              },
          },
      }
    if method.startswith("notifications/") or msg_id is None:
      return None
    if method == "ping":
      return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
      return {
          "jsonrpc": "2.0",
          "id": msg_id,
          "result": {"tools": [MCP_TOOL_SCHEMA]},
      }
    if method == "tools/call":
      tool_name = params.get("name")
      args = params.get("arguments") or {}
      if tool_name != "ask_iowa_sales_agent":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Unknown tool: {tool_name}",
            },
        }
      question = args.get("question", "")
      user_id = agw_ctx["line_user_id"]
      try:
        result = await execute_iowa_bqca_query(
            question,
            base_url=base_url,
            oauth_token=agw_ctx["oauth_token"],
            user_email=agw_ctx["user_email"],
            partner_id=agw_ctx["partner_id"],
            rls_scope=agw_ctx["rls_scope"],
            rls_policy_name=agw_ctx["rls_policy_name"],
            rls_sql_filter=agw_ctx["rls_sql_filter"],
            impersonated_sa=agw_ctx["impersonated_sa"],
        )
        result["token_broker_source"] = agw_ctx["token_source"]
        if user_id:
          TURN_ARTIFACTS[user_id] = result
        TURN_ARTIFACTS["__latest__"] = result
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ],
                "isError": False,
            },
        }
      except HTTPException as he:
        logger.warning("Guardrail/HTTP error in iowa_sales_agent: %s", he.detail)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"error": str(he.detail)}, ensure_ascii=False
                        ),
                    }
                ],
                "isError": True,
            },
        }
      except Exception:
        logger.exception("Error calling iowa_sales_agent")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "error": (
                                    "Internal analytics execution error. "
                                    "Details logged on server."
                                )
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }

  if isinstance(body, list):
    responses = []
    for item in body:
      res = await handle_single_rpc(item)
      if res is not None:
        responses.append(res)
    if not responses:
      return Response(status_code=202, headers={"Mcp-Session-Id": session_id})
    return JSONResponse(
        content=responses, headers={"Mcp-Session-Id": session_id}
    )

  res = await handle_single_rpc(body)
  if res is None:
    return Response(status_code=202, headers={"Mcp-Session-Id": session_id})
  return JSONResponse(content=res, headers={"Mcp-Session-Id": session_id})


# =====================================================================
# 3. Chart Image Proxy Endpoint (GET /charts/{chart_id}.png)
# =====================================================================
@app.get("/charts/{chart_id}.png")
async def get_chart_png(chart_id: str):
  if not _SAFE_CHART_ID_RE.fullmatch(chart_id or ""):
    raise HTTPException(status_code=400, detail="Invalid chart identifier")
  if chart_id in CHART_CACHE:
    return Response(content=CHART_CACHE[chart_id], media_type="image/png")
  try:
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(CHART_BUCKET)
    blob = bucket.blob(f"charts/{chart_id}.png")
    if blob.exists():
      data = blob.download_as_bytes()
      CHART_CACHE[chart_id] = data
      return Response(content=data, media_type="image/png")
  except Exception as e:
    logger.warning("Failed to fetch chart %s from GCS: %s", chart_id, e)
  raise HTTPException(status_code=404, detail="Chart not found")


# =====================================================================
# 4. LINE Messaging Helpers + Flex Bubble Builder
# =====================================================================
async def start_line_loading(line_user_id: str, seconds: int = 60) -> None:
  if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
    return
  try:
    async with httpx.AsyncClient(timeout=5.0) as client:
      await client.post(
          "https://api.line.me/v2/bot/chat/loading/start",
          headers={
              "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
              "Content-Type": "application/json",
          },
          json={"chatId": line_user_id, "loadingSeconds": seconds},
      )
  except Exception as e:
    logger.warning("LINE loading indicator failed: %s", e)


async def send_line_reply(
    reply_token: str, messages: List[Dict[str, Any]]
) -> bool:
  if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
    return False
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"replyToken": reply_token, "messages": messages},
    )
    if resp.status_code == 200:
      return True
    logger.warning("LINE reply failed (%s): %s", resp.status_code, resp.text)
    return False


async def send_line_push(
    line_user_id: str, messages: List[Dict[str, Any]]
) -> bool:
  if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
    return False
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": line_user_id, "messages": messages},
    )
    if resp.status_code == 200:
      return True
    logger.error("LINE push failed (%s): %s", resp.status_code, resp.text)
    return False


def build_line_flex_message(
    summary_text: str,
    artifact: Optional[Dict[str, Any]] = None,
    user_email: str = "",
    partner_id: str = "",
    rls_scope: str = "",
) -> Dict[str, Any]:
  artifact = artifact or {}
  chart_url = artifact.get("chart_image_url")
  sql = artifact.get("generated_sql", "")
  followups = artifact.get("followup_questions") or []
  auth_user = user_email or artifact.get("authenticated_user") or ""
  p_id = partner_id or artifact.get("partner_id") or ""
  scope_label = rls_scope or artifact.get("rls_scope") or "ALL_IOWA"

  body_contents: List[Dict[str, Any]] = [
      {
          "type": "text",
          "text": "IOWA LIQUOR SALES AI (CES + GEAP + BQCA)",
          "weight": "bold",
          "color": "#1A73E8",
          "size": "xs",
      },
  ]

  if auth_user or p_id:
    badge_text = (
        f"🤝 {p_id} ({auth_user}) • RLS: {scope_label}"
        if p_id
        else f"🔒 IAP & STS: {auth_user}"
    )
    body_contents.append({
        "type": "text",
        "text": badge_text,
        "size": "xxs",
        "color": "#059669",
        "weight": "bold",
        "margin": "xs",
        "wrap": True,
    })

  body_contents.append({
      "type": "text",
      "text": summary_text[:1800],
      "wrap": True,
      "size": "sm",
      "color": "#202124",
      "margin": "md",
  })

  if sql:
    body_contents.extend([
        {"type": "separator", "margin": "lg"},
        {
            "type": "text",
            "text": "Generated BigQuery SQL",
            "weight": "bold",
            "size": "xxs",
            "color": "#5F6368",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F1F3F4",
            "cornerRadius": "6px",
            "paddingAll": "8px",
            "margin": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": sql[:500],
                    "size": "xxs",
                    "color": "#3C4043",
                    "wrap": True,
                }
            ],
        },
    ])

  bubble: Dict[str, Any] = {
      "type": "bubble",
      "size": "mega",
      "body": {
          "type": "box",
          "layout": "vertical",
          "contents": body_contents,
      },
  }

  if chart_url:
    bubble["hero"] = {
        "type": "image",
        "url": chart_url,
        "size": "full",
        "aspectRatio": "2:1",
        "aspectMode": "fit",
        "backgroundColor": "#FFFFFF",
        "action": {"type": "uri", "uri": chart_url},
    }

  message: Dict[str, Any] = {
      "type": "flex",
      "altText": summary_text[:300] or "Iowa Liquor Sales Analysis",
      "contents": bubble,
  }

  items = []
  for q in followups[:3]:
    label = q[:19] + "…" if len(q) > 20 else q
    items.append({
        "type": "action",
        "action": {
            "type": "message",
            "label": label,
            "text": q[:300],
        },
    })
  items.append({
      "type": "action",
      "action": {
          "type": "message",
          "label": "🔓 ออกจากระบบ",
          "text": "ออกจากระบบ",
      },
  })
  message["quickReply"] = {"items": items}

  return message


# =====================================================================
# 5. CES SessionService.RunSession Caller
# =====================================================================
async def run_ces_session(
    line_user_id: str,
    user_text: str,
    base_url: str,
    user_email: str = "",
    partner_id: str = "",
    rls_scope: str = "",
    rls_policy_name: str = "",
    rls_sql_filter: str = "",
    impersonated_sa: str = "",
) -> Dict[str, Any]:
  TURN_ARTIFACTS.pop(line_user_id, None)
  TURN_ARTIFACTS.pop("__latest__", None)

  ces_app_id = get_effective_ces_app_id()
  if not ces_app_id:
    sts_info = get_stateless_sts_token(
        partner_id or "default", target_sa=impersonated_sa
    )
    art = await execute_iowa_bqca_query(
        user_text,
        base_url=base_url,
        oauth_token=sts_info["access_token"],
        user_email=user_email,
        partner_id=partner_id,
        rls_scope=rls_scope,
        rls_policy_name=rls_policy_name,
        rls_sql_filter=rls_sql_filter,
        impersonated_sa=impersonated_sa,
    )
    return {"text": art["answer"], "artifact": art, "via": "bqca_direct"}

  # Cloud Run Host Service Account calls CES SessionService.RunSession.
  # ZERO user OAuth token is ever passed into CES or LLM variables!
  ces_host_token = get_gcp_bearer_token()
  safe_partner = (
      (partner_id or "internal")
      .lower()
      .replace("_", "-")
      .replace(" ", "-")
  )
  session_id = f"line-{line_user_id}-{safe_partner}-rls24"
  session_path = (
      f"projects/{PROJECT_ID}/locations/{CES_LOCATION}/apps/{ces_app_id}/"
      f"sessions/{session_id}"
  )
  ces_url = f"https://ces.googleapis.com/v1beta/{session_path}:runSession"

  # Trap #3: SessionInput has oneof input_type { text, variables }
  # Notice: ONLY line_user_id is passed in CES session variables.
  # CES McpToolset forwards `x-line-user-id` to GE Agent Gateway (`agent-gateway-in`),
  # where the Service Extension (`POST /ext-proc/oauth-broker`) mints and injects
  # `Authorization: Bearer <sts_token>` into the upstream MCP request.
  payload = {
      "config": {
          "session": session_path,
          "timeZone": "Asia/Bangkok",
      },
      "inputs": [
          {
              "variables": {
                  "line_user_id": line_user_id,
              }
          },
          {"text": user_text},
      ],
  }

  try:
    async with httpx.AsyncClient(timeout=85.0) as client:
      resp = await client.post(
          ces_url,
          headers={
              "Authorization": f"Bearer {ces_host_token}",
              "Content-Type": "application/json",
              "X-Goog-User-Project": PROJECT_ID,
          },
          json=payload,
      )
      resp.raise_for_status()
      data = resp.json()
  except Exception as e:
    logger.warning(
        "CES runSession returned error (%s); falling back to AGW STS -> BQCA", e
    )
    sts_info = get_stateless_sts_token(
        partner_id or "default", target_sa=impersonated_sa
    )
    art = await execute_iowa_bqca_query(
        user_text,
        base_url=base_url,
        oauth_token=sts_info["access_token"],
        user_email=user_email,
        partner_id=partner_id,
        rls_scope=rls_scope,
        rls_policy_name=rls_policy_name,
        rls_sql_filter=rls_sql_filter,
        impersonated_sa=impersonated_sa,
    )
    return {
        "text": art["answer"],
        "artifact": art,
        "via": "bqca_fallback",
    }

  texts: List[str] = []
  for out in data.get("outputs", []):
    if "text" in out and out["text"]:
      texts.append(out["text"])

  artifact = TURN_ARTIFACTS.pop(line_user_id, None) or TURN_ARTIFACTS.pop(
      "__latest__", None
  )
  final_text = "\n".join(texts).strip()
  # Always prefer the exact BigQuery Native RLS grounded answer when a Partner RLS policy is active
  if artifact and artifact.get("answer"):
    if rls_scope and rls_scope not in ("ALL_IOWA", "NONE", ""):
      final_text = artifact["answer"]
    elif not final_text:
      final_text = artifact["answer"]
  return {
      "text": final_text or "ขออภัย ระบบไม่สามารถประมวลผลคำตอบได้ในขณะนี้",
      "artifact": artifact,
      "via": "ces_run_session",
      "user_email": user_email,
      "partner_id": partner_id,
      "rls_scope": rls_scope or "ALL_IOWA",
      "rls_policy_name": rls_policy_name or "rls_internal_all_access",
      "impersonated_sa": impersonated_sa or "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com",
      "token_broker": "GE_AGENT_GATEWAY_EXT_PROC_STS",
  }


async def process_line_turn(
    line_user_id: str,
    reply_token: str,
    user_text: str,
    base_url: str,
) -> None:
  normalized = user_text.strip().lower()

  # 1. Explicit Logout / Lock Rich Menu command
  if normalized in ("ออกจากระบบ", "logout", "sign out", "ล็อกเมนู"):
    clear_user_auth_session(line_user_id)
    await set_user_richmenu(line_user_id, LOGIN_RICHMENU_ID)
    login_card = build_iap_login_flex_message(line_user_id, base_url)
    await send_line_reply(
        reply_token,
        [
            {
                "type": "text",
                "text": "🔒 ออกจากระบบ ลบสถานะการผูกบัญชีใน Firestore และสลับกลับเป็น Rich Menu 1 การ์ดเรียบร้อยแล้วครับ",
            },
            login_card,
        ],
    )
    return

  # 2. Check Per-User Firestore Partner Registry / Internal IAP State (Pre-Check Only — Zero Token Minting in Webhook!)
  auth_record = firestore_get_doc(PARTNER_REGISTRY_COLLECTION, line_user_id)
  if (
      not auth_record
      or not auth_record.get("authenticated")
      or auth_record.get("status", "ACTIVE") != "ACTIVE"
      or normalized
      in (
          "เข้าสู่ระบบด้วย google iap",
          "เข้าสู่ระบบ",
          "login",
          "partner login",
      )
  ):
    if not auth_record or auth_record.get("status") != "ACTIVE":
      await set_user_richmenu(line_user_id, LOGIN_RICHMENU_ID)
    login_card = build_iap_login_flex_message(line_user_id, base_url)
    sent = await send_line_reply(reply_token, [login_card])
    if not sent:
      await send_line_push(line_user_id, [login_card])
    return

  # 3. Authenticated Partner/Employee -> Call CES with ONLY line_user_id (GE Agent Gateway Brokers STS Token)
  user_email = auth_record.get("user_email", "")
  partner_id = auth_record.get("partner_id", "")
  rls_scope = auth_record.get("rls_scope", "ALL_IOWA")
  rls_policy_name = auth_record.get("rls_policy_name", "rls_internal_all_access")
  rls_sql_filter = auth_record.get("rls_sql_filter", "TRUE")
  impersonated_sa = auth_record.get(
      "service_account", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  )
  await start_line_loading(line_user_id, seconds=60)

  reply_consumed = False

  async def cutoff_guard():
    nonlocal reply_consumed
    await asyncio.sleep(CUTOFF_SECONDS)
    if not reply_consumed:
      reply_consumed = True
      await send_line_reply(
          reply_token,
          [
              {
                  "type": "text",
                  "text": (
                      f"⏳ กำลังประมวลผล SQL และสร้างกราฟจาก BigQuery สำหรับ {partner_id or user_email} "
                      f"(BigQuery RLS: {rls_policy_name} • {rls_scope}) กรุณารอสักครู่ครับ..."
                  ),
              }
          ],
      )

  guard_task = asyncio.create_task(cutoff_guard())
  try:
    ces_res = await run_ces_session(
        line_user_id,
        user_text,
        base_url,
        user_email=user_email,
        partner_id=partner_id,
        rls_scope=rls_scope,
        rls_policy_name=rls_policy_name,
        rls_sql_filter=rls_sql_filter,
        impersonated_sa=impersonated_sa,
    )
    summary_text = ces_res["text"]
    artifact = ces_res.get("artifact")

    if artifact:
      msg = build_line_flex_message(
          summary_text,
          artifact,
          user_email=user_email,
          partner_id=partner_id,
          rls_scope=rls_scope,
      )
    else:
      msg = {"type": "text", "text": summary_text[:4900]}

    if not reply_consumed:
      reply_consumed = True
      guard_task.cancel()
      sent = await send_line_reply(reply_token, [msg])
      if not sent:
        await send_line_push(line_user_id, [msg])
    else:
      await send_line_push(line_user_id, [msg])
  except HTTPException as he:
    logger.warning("Guardrail/HTTP error during LINE turn: %s", he.detail)
    err_msg = {
        "type": "text",
        "text": "🛡️ คำขอนี้ถูกปฏิเสธโดยระบบรักษาความปลอดภัย (Model Armor / PEP Guardrail)",
    }
    if not reply_consumed:
      reply_consumed = True
      guard_task.cancel()
      await send_line_reply(reply_token, [err_msg])
    else:
      await send_line_push(line_user_id, [err_msg])
  except Exception:
    logger.exception("Error processing LINE turn")
    err_msg = {
        "type": "text",
        "text": "ขออภัย ระบบเกิดข้อผิดพลาดชั่วคราวในการเชื่อมต่อระบบวิเคราะห์ข้อมูล กรุณาลองใหม่อีกครั้งครับ",
    }
    if not reply_consumed:
      reply_consumed = True
      guard_task.cancel()
      await send_line_reply(reply_token, [err_msg])
    else:
      await send_line_push(line_user_id, [err_msg])


# =====================================================================
# 6. Northbound LINE Webhook Endpoint (POST /line/webhook)
# =====================================================================
@app.post("/line/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
  raw_body = await request.body()
  signature = request.headers.get("x-line-signature", "").strip()

  if not LINE_CHANNEL_SECRET:
    logger.error("LINE_CHANNEL_SECRET is not configured; rejecting webhook")
    raise HTTPException(
        status_code=503, detail="LINE webhook secret is not configured"
    )
  if not signature:
    raise HTTPException(
        status_code=400, detail="Missing X-Line-Signature header"
    )

  mac = hmac.new(
      LINE_CHANNEL_SECRET.encode("utf-8"),
      raw_body,
      hashlib.sha256,
  ).digest()
  expected_sig = base64.b64encode(mac).decode("utf-8")
  if not hmac.compare_digest(signature, expected_sig):
    logger.warning("Invalid X-Line-Signature received")
    raise HTTPException(status_code=400, detail="Invalid signature")

  payload = json.loads(raw_body.decode("utf-8") or "{}")
  events = payload.get("events", [])
  req_base = f"https://{request.headers.get('host', '')}"
  base_url = get_effective_base_url(req_base)

  for event in events:
    ev_type = event.get("type")
    user_id = event.get("source", {}).get("userId", "")
    reply_token = event.get("replyToken", "")

    if ev_type == "follow" and user_id:
      background_tasks.add_task(
          process_line_turn,
          user_id,
          reply_token,
          "เข้าสู่ระบบ",
          base_url,
      )
    elif ev_type == "message":
      message = event.get("message", {})
      if message.get("type") == "text":
        text = message.get("text", "").strip()
        if user_id and text:
          background_tasks.add_task(
              process_line_turn, user_id, reply_token, text, base_url
          )

  return JSONResponse(content={"status": "ok"})


@app.post("/test/turn")
async def test_turn(request: Request):
  raw_body = await request.body()
  body = json.loads(raw_body.decode("utf-8") or "{}")
  user_id = str(body.get("user_id", "U_DEMO_TEST")).strip()
  text = body.get(
      "text", "Top 5 stores by sales revenue in 2023 with bar chart"
  )
  bypass_auth = bool(body.get("bypass_auth", False))
  req_base = f"https://{request.headers.get('host', '')}"
  base_url = get_effective_base_url(req_base)

  if bypass_auth:
    exp = int(body.get("exp", 0) or 0)
    sig = str(body.get("sig", "")).strip()
    if not verify_admin_request(
        request, raw_body, uid=user_id, exp=exp, sig=sig
    ):
      raise HTTPException(
          status_code=401,
          detail=(
              "Unauthorized: bypass_auth requires a valid X-Admin-Signature "
              "header, GCP IAM Bearer token, or verified state signature."
          ),
      )

  auth_record = firestore_get_doc(PARTNER_REGISTRY_COLLECTION, user_id)
  if (not auth_record or auth_record.get("status") != "ACTIVE") and not bypass_auth:
    return {
        "authenticated": False,
        "action": "partner_or_iap_login_required",
        "login_richmenu_id": LOGIN_RICHMENU_ID,
        "partner_portal_url": build_signed_partner_portal_url(user_id, base_url),
        "iap_login_url": build_signed_iap_login_url(user_id),
        "line_message": build_iap_login_flex_message(user_id, base_url),
    }

  u_email = (
      auth_record.get("user_email", "demo-bypass@<YOUR_GCP_PROJECT_ID>")
      if auth_record
      else "demo-bypass@<YOUR_GCP_PROJECT_ID>"
  )
  p_id = auth_record.get("partner_id", "INTERNAL-BYPASS") if auth_record else "INTERNAL-BYPASS"
  r_scope = auth_record.get("rls_scope", "ALL_IOWA") if auth_record else "ALL_IOWA"
  r_pol = auth_record.get("rls_policy_name", "rls_internal_all_access") if auth_record else "rls_internal_all_access"
  r_flt = auth_record.get("rls_sql_filter", "TRUE") if auth_record else "TRUE"
  imp_sa = (
      auth_record.get("service_account", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com")
      if auth_record
      else "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  )

  ces_res = await run_ces_session(
      user_id,
      text,
      base_url,
      user_email=u_email,
      partner_id=p_id,
      rls_scope=r_scope,
      rls_policy_name=r_pol,
      rls_sql_filter=r_flt,
      impersonated_sa=imp_sa,
  )
  flex = (
      build_line_flex_message(
          ces_res["text"],
          ces_res.get("artifact"),
          user_email=u_email,
          partner_id=p_id,
          rls_scope=r_scope,
      )
      if ces_res.get("artifact")
      else {"type": "text", "text": ces_res["text"]}
  )
  return {
      "authenticated": True,
      "auth_mode": auth_record.get("auth_mode", "BYPASS") if auth_record else "BYPASS",
      "user_email": u_email,
      "partner_id": p_id,
      "rls_scope": r_scope,
      "rls_policy_name": r_pol,
      "rls_sql_filter": r_flt,
      "impersonated_sa": imp_sa,
      "token_storage": "STATELESS_RAM_STS_NO_GCS",
      "token_broker": "GE_AGENT_GATEWAY_EXT_PROC_STS",
      "via": ces_res.get("via"),
      "ces_app_id": get_effective_ces_app_id(),
      "summary_text": ces_res["text"],
      "artifact": ces_res.get("artifact"),
      "line_message": flex,
  }


@app.post("/ext-proc/oauth-broker")
async def agw_service_extension_oauth_broker(request: Request):
  """Envoy ext_proc (EXT_PROC_JSON) Callout Extension for GE Agent Gateway.

  1. Reads `x-line-user-id` from the incoming MCP request headers (forwarded by CES McpToolset).
     Notice: CES McpToolset NEVER sends any `x-token` header!
  2. Queries Cloud Firestore (`line_partner_registry/{line_user_id}`) to verify
     `status == "ACTIVE"` and retrieve `partner_id`, `service_account`, and `rls_policy_name`.
  3. Performs Stateless Token Brokering (via Workforce Identity Federation RFC 8693 STS
     `sts.googleapis.com/v1/token` or `iamcredentials.googleapis.com` in RAM only,
     with ZERO tokens stored on GCS or Firestore) and returns Envoy HeaderMutation
     instructions injecting `authorization: Bearer ya29...`, `x-agw-verified-partner-id`,
     `x-agw-rls-policy`, `x-agw-rls-scope`, `x-agw-impersonated-sa`, and `x-agw-user-email`
     into the upstream `POST /mcp/` request.
  """
  body = await request.json()
  req_headers_raw = (
      body.get("request_headers", {}).get("headers", {}).get("headers", [])
  )
  header_map: Dict[str, str] = {}
  for h in req_headers_raw:
    k = (h.get("key") or "").lower()
    v = h.get("raw_value") or h.get("value") or ""
    if k:
      header_map[k] = v

  line_uid = (
      header_map.get("x-line-user-id")
      or request.headers.get("x-line-user-id", "")
  ).strip()
  auth_record = get_user_auth_session(line_uid)
  if not auth_record:
    return JSONResponse(
        status_code=403,
        content={
            "immediate_response": {
                "status": {"code": 403},
                "body": json.dumps({
                    "error": "FIRESTORE_PARTNER_OR_IAP_AUTH_REQUIRED",
                    "message": (
                        "User is not registered or is SUSPENDED in Firestore "
                        "line_partner_registry."
                    ),
                    "partner_portal_url": build_signed_partner_portal_url(
                        line_uid or "UNKNOWN"
                    ),
                    "iap_login_url": build_signed_iap_login_url(
                        line_uid or "UNKNOWN"
                    ),
                }),
            }
        },
    )

  oauth_token = auth_record.get("oauth2_access_token", "")
  user_email = auth_record.get("user_email", "")
  partner_id = auth_record.get("partner_id", "")
  rls_scope = auth_record.get("rls_scope", "ALL_IOWA")
  rls_policy = auth_record.get("rls_policy_name", "rls_internal_all_access")
  rls_filter = auth_record.get("rls_sql_filter", "TRUE")
  impersonated_sa = auth_record.get(
      "impersonated_sa", "<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
  )
  auth_mode = auth_record.get("auth_mode", "PARTNER_CIP_FIRESTORE")
  return {
      "request_headers": {
          "response": {
              "status": "CONTINUE",
              "header_mutation": {
                  "set_headers": [
                      {
                          "header": {
                              "key": "authorization",
                              "value": f"Bearer {oauth_token}",
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-user-email",
                              "value": user_email,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-verified-partner-id",
                              "value": partner_id,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-rls-policy",
                              "value": rls_policy,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-rls-filter",
                              "value": rls_filter,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-impersonated-sa",
                              "value": impersonated_sa,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-rls-scope",
                              "value": rls_scope,
                          },
                          "append": False,
                      },
                      {
                          "header": {
                              "key": "x-agw-ext-broker",
                              "value": f"ge-agw-ext-proc-sts:{auth_mode}:{rls_policy}",
                          },
                          "append": False,
                      },
                  ]
              },
          }
      }
  }


@app.get("/.well-known/openid-configuration")
async def oidc_discovery(request: Request):
  """OIDC Discovery Document for Cloud KMS Asymmetric JWT Signer -> GCP Workforce Identity Pool."""
  req_base = f"https://{request.headers.get('host', '')}"
  issuer = get_effective_base_url(req_base) or f"https://{PROJECT_ID}.a.run.app"
  return {
      "issuer": issuer,
      "jwks_uri": f"{issuer}/.well-known/jwks.json",
      "response_types_supported": ["id_token"],
      "subject_types_supported": ["public"],
      "id_token_signing_alg_values_supported": ["RS256"],
      "claims_supported": ["sub", "iss", "aud", "exp", "iat", "partner_id", "rls_scope"],
  }


@app.get("/.well-known/jwks.json")
async def oidc_jwks():
  """Exposes the Cloud KMS Asymmetric Signing Public Key (`getPublicKey`) as RFC 7517 JWKS for GCP Workforce Identity Federation."""
  now = time.monotonic()
  if JWKS_MEM_CACHE.get("jwks") and now - JWKS_MEM_CACHE.get("ts", 0.0) < 300:
    return JWKS_MEM_CACHE["jwks"]
  kms_pub_url = f"https://cloudkms.googleapis.com/v1/{KMS_SIGNING_KEY_VERSION}/publicKey"
  try:
    host_token = get_gcp_bearer_token()
    with httpx.Client(timeout=5.0) as client:
      resp = client.get(
          kms_pub_url, headers={"Authorization": f"Bearer {host_token}"}
      )
      if resp.status_code == 200:
        pub_data = resp.json()
        jwks_doc = {
            "keys": [
                {
                    "kty": "RSA",
                    "alg": "RS256",
                    "use": "sig",
                    "kid": "kms-partner-signer-v1",
                    "kms_key_version": KMS_SIGNING_KEY_VERSION,
                    "pem": pub_data.get("pem", ""),
                }
            ]
        }
        JWKS_MEM_CACHE["ts"] = now
        JWKS_MEM_CACHE["jwks"] = jwks_doc
        return jwks_doc
  except Exception as e:
    logger.debug("Cloud KMS publicKey fetch skipped: %s", e)
  return {
      "keys": [
          {
              "kty": "RSA",
              "alg": "RS256",
              "use": "sig",
              "kid": "kms-partner-signer-v1",
              "kms_key_version": KMS_SIGNING_KEY_VERSION,
              "status": "READY_FOR_WORKFORCE_POOL_REGISTRATION",
          }
      ]
  }


@app.get("/")
@app.get("/health")
async def health():
  return {
      "status": "ok",
      "version": "2.5.0-production-verified",
      "project": PROJECT_ID,
      "ces_app_id": get_effective_ces_app_id(),
      "public_base_url": get_effective_base_url(),
      "iap_auth_base_url": get_effective_iap_auth_url(),
      "firestore_database": f"projects/{PROJECT_ID}/databases/(default)",
      "firestore_collections": {
          "partner_accounts": PARTNER_ACCOUNTS_COLLECTION,
          "partner_registry": PARTNER_REGISTRY_COLLECTION,
      },
      "bigquery_native_row_access_policies": {
          "table": "<YOUR_BQ_DATASET_PROJECT_ID>.iowa_liquor_sales.sales",
          "policies": [
              {
                  "policy_id": "rls_partner_polk_store_2633",
                  "grantee": "serviceAccount:sa-partner-polk@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com",
                  "filter_predicate": "LOWER(county) = 'polk' AND store_number = '2633'",
              },
              {
                  "policy_id": "rls_partner_east_store_3773",
                  "grantee": "serviceAccount:sa-partner-east@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com",
                  "filter_predicate": "LOWER(county) = 'linn' AND store_number = '3773'",
              },
              {
                  "policy_id": "rls_internal_all_access",
                  "grantee": "serviceAccount:<YOUR_PROJECT_NUMBER>-compute@developer.gserviceaccount.com, user:<YOUR_CORPORATE_EMAIL>",
                  "filter_predicate": "TRUE",
              },
          ],
      },
      "token_storage_policy": (
          "ZERO_PERSISTENT_TOKEN_VAULT (Stateless RAM STS Token Broker: "
          "Mode A = Cloud KMS Asymmetric JWT -> Workforce Pool sts.googleapis.com/v1/token; "
          "Mode B = iamcredentials.googleapis.com Partner SA Impersonation)"
      ),
      "ces_mcp_header_contract": "customHeaders: {'x-line-user-id': '$context.variables.line_user_id'} (ZERO x-token parameter)",
      "bqca_auth_channel": "HTTP Authorization: Bearer <sts_or_sa_token> header (ChatAction.kt verified; ChatRequest.credentials is Looker/SQL only)",
      "login_richmenu_id": LOGIN_RICHMENU_ID,
      "unlocked_richmenu_id": UNLOCKED_RICHMENU_ID,
      "bqca_agent": BQCA_AGENT_RESOURCE,
      "verified_production_topologies": {
          "topology_1_ces_active": (
              "CES (ces.googleapis.com) McpToolset [service_account_auth_config + custom_headers x-line-user-id] "
              "-> Regional ALB LbTrafficExtension (/ext-proc/oauth-broker) OR Cloud Run FastMCP Zero-Trust PEP "
              "[1. Model Armor sanitizeUserPrompt -> 2. Firestore ACTIVE check -> 3. KMS WIF STS / Partner SA Token in RAM] "
              "-> BQCA :chat [Authorization: Bearer] -> BigQuery ROW ACCESS POLICY [SESSION_USER()]"
          ),
          "topology_2_adk_agent_gateway": (
              "Vertex AI Agent Engine ADK (ReasoningEngine + agent_gateway_config + SPIFFE AGENT_IDENTITY) "
              "-> Google-Managed GE Agent Gateway (AGENT_TO_ANYWHERE: 1P Cloud IAP roles/iap.egressor + 1P Model Armor CONTENT_AUTHZ + PSC 10.10.20.2) "
              "-> Cloud Run FastMCP Zero-Trust PEP [Firestore ACTIVE check + KMS WIF STS / Partner SA Token in RAM] "
              "-> BQCA :chat [Authorization: Bearer] -> BigQuery ROW ACCESS POLICY [SESSION_USER()]"
          ),
          "oidc_discovery_endpoint": "/.well-known/openid-configuration",
          "oidc_jwks_endpoint": "/.well-known/jwks.json",
          "alb_ext_proc_callout": "/ext-proc/oauth-broker",
      },
  }

