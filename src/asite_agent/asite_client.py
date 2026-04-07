from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


def _request_json_or_xml(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, data=body, headers=request_headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            return json.loads(raw)
        if raw.strip().startswith("<"):
            return {"_xml": raw}
        return {"_text": raw}


def _parse_login_response(data: dict[str, Any]) -> tuple[str, list[str]]:
    if "UserProfile" in data:
        profile = data["UserProfile"]
        session_id = profile.get("Sessionid") or profile.get("sessionID") or ""
        uris = profile.get("URI") or []
        if isinstance(uris, str):
            uris = [uris]
        return session_id, uris

    xml = data.get("_xml")
    if not xml:
        return "", []
    root = ET.fromstring(xml)
    session = root.findtext(".//Sessionid", default="")
    uris = [node.text or "" for node in root.findall(".//URI")]
    return session, [uri for uri in uris if uri]


@dataclass
class AsiteSession:
    session_id: str
    available_uris: list[str]


class AsiteClient:
    def __init__(self, login_url_template: str, email: str, password: str) -> None:
        self.login_url_template = login_url_template
        self.email = email
        self.password = password
        self.session: AsiteSession | None = None

    def login(self) -> AsiteSession:
        url = self.login_url_template.format(
            email=urllib.parse.quote(self.email), password=urllib.parse.quote(self.password)
        )
        data = _request_json_or_xml(url)
        session_id, uris = _parse_login_response(data)
        self.session = AsiteSession(session_id=session_id, available_uris=uris)
        return self.session

    def can_call_uri_hint(self, uri_hint: str) -> bool:
        if not self.session:
            raise RuntimeError("Asite session not established.")
        return any(uri_hint in uri for uri in self.session.available_uris)

    def pick_uri(self, uri_hint: str) -> str | None:
        if not self.session:
            raise RuntimeError("Asite session not established.")
        for uri in self.session.available_uris:
            if uri_hint in uri:
                return uri
        return None

    def call_uri(
        self, url: str, method: str = "GET", payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("Asite session not established.")
        headers = {"Cookie": f"ASessionID={self.session.session_id}"}
        return _request_json_or_xml(url=url, method=method, headers=headers, payload=payload)

