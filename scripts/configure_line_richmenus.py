#!/usr/bin/env python3
"""Automated LINE Rich Menu Provisioning Script.

Creates and uploads:
1. Default 1-Card Login Gate Rich Menu (`2500x843`) — presented to all unauthenticated users.
2. Unlocked 6-Card Analytics Rich Menu (`2500x1686`) — linked per-user via `POST /v2/bot/user/{uid}/richmenu/{id}` after Partner CIP or Cloud IAP authentication.

Note on LINE Rich Menu Limits (Trap #5 & #6):
- `chatBarText` MUST be <= 14 characters.
- Rich Menus created via Messaging API are separate from `manager.line.biz` UI menus (disable any active Manager UI menu so the API menu is displayed).
"""

import os
import pathlib
import sys
import httpx

LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"


def create_richmenu(
    client: httpx.Client, token: str, spec: dict, png_path: pathlib.Path
) -> str:
  headers = {
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
  }
  r = client.post(f"{LINE_API}/richmenu", headers=headers, json=spec)
  r.raise_for_status()
  richmenu_id = r.json()["richMenuId"]

  img_headers = {
      "Authorization": f"Bearer {token}",
      "Content-Type": "image/png",
  }
  with open(png_path, "rb") as f:
    r_img = client.post(
        f"{LINE_DATA_API}/richmenu/{richmenu_id}/content",
        headers=img_headers,
        content=f.read(),
    )
    r_img.raise_for_status()
  return richmenu_id


def main() -> None:
  token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
  if not token:
    print("ERROR: Set LINE_CHANNEL_ACCESS_TOKEN in environment.", file=sys.stderr)
    sys.exit(1)

  repo_root = pathlib.Path(__file__).resolve().parent.parent
  assets_dir = repo_root / "assets" / "richmenus"

  # 1-Card Login Gate Rich Menu (2500x843)
  login_menu_spec = {
      "size": {"width": 2500, "height": 843},
      "selected": True,
      "name": "Sales AI - 1-Card Hybrid Auth Gate",
      "chatBarText": "เข้าสู่ระบบ AI",  # <= 14 chars mandatory
      "areas": [
          {
              "bounds": {"x": 0, "y": 0, "width": 2500, "height": 843},
              "action": {
                  "type": "message",
                  "label": "เข้าสู่ระบบ",
                  "text": "เข้าสู่ระบบด้วย Google IAP",
              },
          }
      ],
  }

  # 6-Card Unlocked Analytics Rich Menu (2500x1686)
  analytics_menu_spec = {
      "size": {"width": 2500, "height": 1686},
      "selected": True,
      "name": "Sales AI - 6-Card Analytics Menu",
      "chatBarText": "เมนูวิเคราะห์",  # <= 14 chars mandatory
      "areas": [
          {
              "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
              "action": {
                  "type": "message",
                  "label": "ภาพรวมยอดขาย",
                  "text": "สรุปยอดขายรวมและจำนวนขวดทั้งหมดในปี 2023",
              },
          },
          {
              "bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
              "action": {
                  "type": "message",
                  "label": "Top 5 สินค้า",
                  "text": (
                      "จัดอันดับ 5 สินค้าขายดีที่สุดตามยอดขายในปี 2023"
                      " พร้อมสร้างกราฟแท่งเปรียบเทียบ"
                  ),
              },
          },
          {
              "bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
              "action": {
                  "type": "message",
                  "label": "ยอดขายรายเมือง",
                  "text": (
                      "จัดอันดับ 5 เมือง (County) ที่มียอดขายสูงสุดในปี 2023"
                      " พร้อมสร้างกราฟแท่งเปรียบเทียบ"
                  ),
              },
          },
          {
              "bounds": {"x": 0, "y": 843, "width": 833, "height": 843},
              "action": {
                  "type": "message",
                  "label": "แนวโน้มรายเดือน",
                  "text": (
                      "แสดงแนวโน้มยอดขายรายเดือนในปี 2023"
                      " พร้อมสร้างกราฟเส้นเปรียบเทียบ"
                  ),
              },
          },
          {
              "bounds": {"x": 833, "y": 843, "width": 834, "height": 843},
              "action": {
                  "type": "message",
                  "label": "Top 5 ร้านค้า",
                  "text": (
                      "จัดอันดับ 5 ร้านค้า (Store) ที่มียอดขายสูงสุดในปี 2023"
                      " พร้อมสร้างกราฟแท่ง"
                  ),
              },
          },
          {
              "bounds": {"x": 1667, "y": 843, "width": 833, "height": 843},
              "action": {
                  "type": "message",
                  "label": "วิธีใช้งาน",
                  "text": "วิธีใช้งาน Sales AI",
              },
          },
      ],
  }

  with httpx.Client(timeout=30.0) as client:
    login_id = create_richmenu(
        client,
        token,
        login_menu_spec,
        assets_dir / "richmenu_1card_login_2500x843.png",
    )
    print(f"Created 1-Card Login Rich Menu     : {login_id}")

    unlocked_id = create_richmenu(
        client,
        token,
        analytics_menu_spec,
        assets_dir / "richmenu_6buttons_2500x1686.png",
    )
    print(f"Created 6-Card Analytics Rich Menu : {unlocked_id}")

    # Set 1-Card Login Menu as the global default for unauthenticated users
    r_def = client.post(
        f"{LINE_API}/user/all/richmenu/{login_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r_def.raise_for_status()
    print(f"Set Default Rich Menu (/user/all)  : {login_id}")
    print("\nUpdate your .env with:")
    print(f'LOGIN_RICHMENU_ID="{login_id}"')
    print(f'UNLOCKED_RICHMENU_ID="{unlocked_id}"')


if __name__ == "__main__":
  main()
