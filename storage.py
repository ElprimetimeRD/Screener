"""
storage.py
==========
Persistencia del log de senales.

POR QUE GIST Y NO UN COMMIT AL REPO
------------------------------------
La opcion obvia seria que la app hiciera commit del log al mismo repo.
Es una trampa: Render tiene auto-deploy activado sobre `main`, asi que
cada commit dispararia un redeploy, y el redeploy borra el disco. El
log se destruiria a si mismo en bucle.

Un Gist es almacenamiento de GitHub que NO esta conectado al repo, asi
que no dispara nada. Gratis, sin base de datos, y encaja con tu setup
sin PC.

CONFIGURACION EN RENDER (Environment -> Add Environment Variable)
-----------------------------------------------------------------
  GITHUB_TOKEN   token personal con permiso de "gist"
  GIST_ID        opcional; si lo dejas vacio la app crea el gist en la
                 primera escritura y deja el id en los logs para que lo
                 fijes despues

Sin GITHUB_TOKEN el sistema cae a archivo local: sirve para probar,
pero en Render se borra en cada deploy.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

GIST_FILENAME = "signals.jsonl"
GIST_API = "https://api.github.com/gists"


class SignalStore:
    """Log de senales append-only. Usa Gist si hay token, si no archivo local."""

    def __init__(
        self,
        token: Optional[str] = None,
        gist_id: Optional[str] = None,
        local_path: str = "signals.jsonl",
    ):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.gist_id = gist_id if gist_id is not None else os.environ.get("GIST_ID", "")
        self.local_path = local_path
        self._last_error: Optional[str] = None

    # -- estado -------------------------------------------------------------

    @property
    def backend(self) -> str:
        return "gist" if self.token else "local"

    @property
    def persistent(self) -> bool:
        return bool(self.token)

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "persistent": self.persistent,
            "gist_id": self.gist_id or None,
            "last_error": self._last_error,
            "note": (
                "Las señales sobreviven a los redeploys."
                if self.persistent
                else "Sin GITHUB_TOKEN: el log se borra en cada redeploy de Render."
            ),
        }

    # -- transporte ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _gist_read(self) -> str:
        import requests

        if not self.gist_id:
            return ""
        r = requests.get(f"{GIST_API}/{self.gist_id}", headers=self._headers(), timeout=20)
        r.raise_for_status()
        files = r.json().get("files", {})
        f = files.get(GIST_FILENAME)
        if not f:
            return ""
        # Gists grandes vienen truncados; hay que ir al raw_url
        if f.get("truncated") and f.get("raw_url"):
            raw = requests.get(f["raw_url"], headers=self._headers(), timeout=30)
            raw.raise_for_status()
            return raw.text
        return f.get("content", "")

    def _gist_write(self, content: str) -> None:
        import requests

        payload = {"files": {GIST_FILENAME: {"content": content}}}
        if self.gist_id:
            r = requests.patch(
                f"{GIST_API}/{self.gist_id}",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
        else:
            payload["description"] = "Escalera ascendente - log de señales"
            payload["public"] = False
            r = requests.post(
                GIST_API, headers=self._headers(), json=payload, timeout=30
            )
        r.raise_for_status()
        if not self.gist_id:
            self.gist_id = r.json().get("id", "")
            print(
                f"[storage] Gist creado. Fija GIST_ID={self.gist_id} en Render "
                f"para reutilizarlo."
            )

    # -- API ----------------------------------------------------------------

    def append(self, records: list[dict[str, Any]]) -> int:
        """Anade registros al log. Devuelve cuantos se escribieron."""
        if not records:
            return 0
        lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"

        if not self.token:
            with open(self.local_path, "a", encoding="utf-8") as fh:
                fh.write(lines)
            return len(records)

        try:
            existing = self._gist_read()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            self._gist_write(existing + lines)
            self._last_error = None
            return len(records)
        except Exception as exc:
            # Nunca tumbar el scan por un fallo de escritura del log
            self._last_error = f"{type(exc).__name__}: {exc}"
            try:
                with open(self.local_path, "a", encoding="utf-8") as fh:
                    fh.write(lines)
            except Exception:
                pass
            return 0

    def read(self) -> pd.DataFrame:
        """Devuelve el log completo como DataFrame."""
        text = ""
        if self.token:
            try:
                text = self._gist_read()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
        if not text and os.path.exists(self.local_path):
            with open(self.local_path, encoding="utf-8") as fh:
                text = fh.read()

        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return pd.DataFrame(rows)

    def count(self) -> int:
        df = self.read()
        return 0 if df.empty else len(df)


# Instancia compartida
store = SignalStore()


def signal_record(verdict: dict[str, Any], ts: Optional[str] = None) -> dict[str, Any]:
    """Extrae de un veredicto solo lo necesario para medir el resultado despues."""
    s = verdict.get("streak") or {}
    return {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "symbol": verdict.get("symbol"),
        "verdict": verdict.get("verdict"),
        "close": s.get("last_close"),
        "streak_days": s.get("streak_days"),
        "avg_rvol": s.get("avg_rvol_streak"),
        "extension_atr": s.get("extension_atr"),
        "atr": s.get("atr"),
        "leverage_factor": verdict.get("leverage_factor"),
        "adv_usd": (verdict.get("liquidity") or {}).get("adv_usd"),
    }
