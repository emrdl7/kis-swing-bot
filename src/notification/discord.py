"""Discord Webhook 알림."""
from __future__ import annotations
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, webhook_url: str, enabled: bool = True):
        self.webhook_url = webhook_url
        self.enabled = enabled and bool(webhook_url)
        self._client = httpx.Client(timeout=10.0)

    def send(self, message: str, username: str = "KIS-Swing-Bot") -> bool:
        if not self.enabled:
            log.debug("Discord 알림 비활성화")
            return False
        try:
            resp = self._client.post(self.webhook_url, json={
                "username": username,
                "content": message,
            })
            resp.raise_for_status()
            return True
        except Exception as e:
            log.error("Discord 알림 전송 실패: %s", e)
            return False

    def send_embed(self, title: str, description: str, color: int = 0x00b894) -> bool:
        if not self.enabled:
            return False
        try:
            resp = self._client.post(self.webhook_url, json={
                "username": "KIS-Swing-Bot",
                "embeds": [{
                    "title": title,
                    "description": description,
                    "color": color,
                }],
            })
            resp.raise_for_status()
            return True
        except Exception as e:
            log.error("Discord embed 전송 실패: %s", e)
            return False

    def close(self) -> None:
        self._client.close()
