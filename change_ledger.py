# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

MAX_SOURCES = 4
MAX_FIELDS = 8
MAX_VALUE_LEN = 200

ACTIVE = "ACTIVE"
CLOSED = "CLOSED"

CONFIRMED = "CONFIRMED"
DISPUTED = "DISPUTED"
MISSING = "MISSING"


class ChangeLedger(gl.Contract):
    """Tracks a set of published fields on a changing source over time.

    Two things make this unlike a one-shot assessor: each observation is a node
    in a version chain whose diff is computed by the contract, and the model
    work (extracting one source) is a separate transaction from the decision
    (aggregating the readings), which is pure deterministic code."""

    watches: TreeMap[str, str]
    versions: TreeMap[str, str]
    readings: TreeMap[str, str]
    watch_count: u256

    def __init__(self):
        self.watch_count = 0

    # ---------------- helpers ----------------

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        return str(a)

    def _iso_to_epoch(self, s: str) -> int:
        s = s.strip()
        if len(s) < 19:
            return 0
        offset = 0
        tail = s[19:]
        if tail.endswith("Z"):
            tail = tail[:-1]
        if tail and (tail[0] == "+" or tail[0] == "-"):
            try:
                offset = (1 if tail[0] == "+" else -1) * (int(tail[1:3]) * 3600 + int(tail[4:6]) * 60)
            except (ValueError, IndexError):
                offset = 0
        try:
            y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
            hh, mm, ss = int(s[11:13]), int(s[14:16]), int(s[17:19])
        except (ValueError, IndexError):
            return 0
        if not (1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mm < 60 and ss < 61):
            return 0
        yy = y - 1 if mo <= 2 else y
        era = yy // 400
        yoe = yy - era * 400
        doy = (153 * (mo + (-3 if mo > 2 else 9)) + 2) // 5 + d - 1
        return (era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468) * 86400 \
            + hh * 3600 + mm * 60 + ss - offset

    def _now(self) -> int:
        raw = getattr(gl, "message_raw", None)
        d = None
        if raw is not None:
            try:
                d = raw["datetime"]
            except Exception:
                d = None
        return self._iso_to_epoch(str(d)) if d is not None else 0

    def _decode_body(self, content) -> str:
        body = getattr(content, "body", None)
        if body is None:
            return str(content)
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def _load(self, tree, key: str, what: str) -> dict:
        raw = tree.get(key, "")
        if not raw:
            raise gl.vm.UserError(what + " not found")
        return json.loads(raw)

    def _rkey(self, watch_id: str, rnd: int, source_index: int) -> str:
        return str(watch_id) + "|" + str(rnd) + "|" + str(source_index)

    def _vkey(self, watch_id: str, n: int) -> str:
        return str(watch_id) + "|" + str(n)

    def _str_list(self, v, what: str, limit: int) -> list:
        if not isinstance(v, list) or len(v) == 0:
            raise gl.vm.UserError(what + " must be a non-empty list")
        out = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise gl.vm.UserError(what + " entries must be non-empty strings")
            s = item.strip()
            if s in out:
                raise gl.vm.UserError(what + " must be unique: " + s)
            if len(s) > MAX_VALUE_LEN:
                raise gl.vm.UserError(what + " entry too long")
            out.append(s)
        if len(out) > limit:
            raise gl.vm.UserError(what + " exceeds " + str(limit))
        return out

    # ---------------- extraction: the model's only job ----------------

    def _extract(self, url: str, fields: list) -> dict:
        def check() -> dict:
            body = ""
            try:
                body = self._decode_body(gl.nondet.web.render(url))
            except Exception:
                body = ""
            if not body.strip():
                return {"retrieved": False, "values": {}}
            fmt = "{" + ", ".join('"' + f + '": "<value>"' for f in fields) + "}"
            task = (
                "Extract the listed fields from this source. JSON only.\n"
                "FIELDS: " + ", ".join(fields) + "\n"
                "SOURCE:\n" + body[:1500] + "\n"
                "Use an empty string for a field that is not present. " + fmt
            )
            result = gl.nondet.exec_prompt(task)
            if isinstance(result, str):
                result = json.loads(result.replace("```json", "").replace("```", ""))
            if not isinstance(result, dict):
                raise gl.vm.UserError("[LLM_ERROR] non-dict result")
            return {"retrieved": True, "values": result}

        principle = "Equivalent if retrieved matches and every field has the same value."
        return gl.eq_principle.prompt_comparative(check, principle)

    def _extraction_ok(self, fields: list, ex: dict) -> bool:
        if not isinstance(ex, dict) or not isinstance(ex.get("retrieved"), bool):
            return False
        vals = ex.get("values")
        if not isinstance(vals, dict) or set(vals.keys()) != set(fields):
            return False
        for v in vals.values():
            if not isinstance(v, str) or len(v) > MAX_VALUE_LEN:
                return False
        return True

    # ---------------- aggregation: pure deterministic code ----------------

    def _majority(self, readings: list, fields: list) -> tuple:
        """Majority value per field across the sources. Strict majority, so a
        split or an absent value becomes DISPUTED/MISSING rather than a guess."""
        values = {}
        status = {}
        agreed = {}
        total = len(readings)
        for f in fields:
            counts = {}
            for r in readings:
                v = r.get(f, "")
                counts[v] = counts.get(v, 0) + 1
            best, best_n, tie = "", 0, False
            for v, n in counts.items():
                if n > best_n:
                    best, best_n, tie = v, n, False
                elif n == best_n and v != best:
                    tie = True
            if best == "":
                values[f] = ""
                status[f] = MISSING
                agreed[f] = 0
            elif tie or best_n * 2 <= total:
                values[f] = ""
                status[f] = DISPUTED
                agreed[f] = best_n
            else:
                values[f] = best
                status[f] = CONFIRMED
                agreed[f] = best_n
        return values, status, agreed

    def _diff(self, old: dict, new: dict, fields: list) -> list:
        changed = []
        for f in fields:
            if old.get(f, "") != new.get(f, ""):
                changed.append(f)
        return changed

    # ---------------- lifecycle ----------------

    @gl.public.write
    def start_watch(self, subject: str, fields: list, sources: list, min_sources: int) -> str:
        if not subject or not subject.strip():
            raise gl.vm.UserError("Subject required")
        fields = self._str_list(fields, "fields", MAX_FIELDS)
        sources = self._str_list(sources, "sources", MAX_SOURCES)
        for u in sources:
            if not u.startswith("http://") and not u.startswith("https://"):
                raise gl.vm.UserError("sources must be http(s): " + u)
        if int(min_sources) < 2 or int(min_sources) > len(sources):
            raise gl.vm.UserError("min_sources must be between 2 and the source count")

        self.watch_count += 1
        watch_id = str(self.watch_count)
        self.watches[watch_id] = json.dumps({
            "watch_id": watch_id,
            "subject": subject.strip(),
            "owner": self._addr_hex(gl.message.sender_address),
            "fields": fields,
            "sources": sources,
            "min_sources": int(min_sources),
            "state": ACTIVE,
            "latest_version": 0,
            "values": {},
            "status": {},
            "created_at": str(self._now()),
        })
        return watch_id

    @gl.public.write
    def close_watch(self, watch_id: str):
        w = self._load(self.watches, str(watch_id), "Watch")
        if w["state"] != ACTIVE:
            raise gl.vm.UserError("Watch already closed")
        if self._addr_hex(gl.message.sender_address).lower() != str(w["owner"]).lower():
            raise gl.vm.UserError("Only the owner can close")
        w["state"] = CLOSED
        self.watches[str(watch_id)] = json.dumps(w)

    @gl.public.write
    def pull(self, watch_id: str, source_index: int) -> str:
        """One fetch, one extraction, one transaction. No decision is taken here."""
        w = self._load(self.watches, str(watch_id), "Watch")
        if w["state"] != ACTIVE:
            raise gl.vm.UserError("Watch is closed")
        idx = int(source_index)
        sources = w["sources"]
        if idx < 0 or idx >= len(sources):
            raise gl.vm.UserError("source_index out of range")
        rnd = int(w["latest_version"]) + 1
        key = self._rkey(watch_id, rnd, idx)
        if key in self.readings:
            raise gl.vm.UserError("Source already pulled for this round")

        ex = self._extract(sources[idx], w["fields"])
        if not self._extraction_ok(w["fields"], ex):
            raise gl.vm.UserError("Invalid extraction: field set or value types are wrong")
        if not ex.get("retrieved"):
            raise gl.vm.UserError("Source could not be retrieved")

        self.readings[key] = json.dumps({
            "watch_id": str(watch_id),
            "round": rnd,
            "source_index": idx,
            "url": sources[idx],
            "values": ex["values"],
            "pulled_at": str(self._now()),
        })
        return json.dumps(ex["values"])

    @gl.public.write
    def aggregate(self, watch_id: str) -> int:
        """Pure computation over the stored readings. No model call happens here:
        the majority value, the change set and the statuses are all derived."""
        w = self._load(self.watches, str(watch_id), "Watch")
        if w["state"] != ACTIVE:
            raise gl.vm.UserError("Watch is closed")
        rnd = int(w["latest_version"]) + 1
        fields = w["fields"]

        readings = []
        used = []
        for i in range(len(w["sources"])):
            raw = self.readings.get(self._rkey(watch_id, rnd, i), "")
            if raw:
                readings.append(json.loads(raw)["values"])
                used.append(i)
        if len(readings) < int(w["min_sources"]):
            raise gl.vm.UserError("Need at least " + str(w["min_sources"]) + " pulled sources")

        values, status, agreed = self._majority(readings, fields)
        parent = int(w["latest_version"])
        old = w.get("values", {}) if parent > 0 else {}
        changed = self._diff(old, values, fields)

        self.versions[self._vkey(watch_id, rnd)] = json.dumps({
            "watch_id": str(watch_id),
            "version": rnd,
            "parent": parent,
            "values": values,
            "status": status,
            "agreed": agreed,
            "changed_fields": changed,
            "sources_used": used,
            "state": "LIVE",
            "recorded_at": str(self._now()),
        })

        w["latest_version"] = rnd
        w["values"] = values
        w["status"] = status
        self.watches[str(watch_id)] = json.dumps(w)
        return rnd

    @gl.public.write
    def dispute(self, watch_id: str, version: int, source_index: int, field: str,
                claimed_value: str) -> bool:
        """A caller names a field and the value they believe a specific source
        carries. The source is re-read; the correction is applied only if the
        fresh reading agrees with the caller and differs from what was stored."""
        w = self._load(self.watches, str(watch_id), "Watch")
        v = self._load(self.versions, self._vkey(watch_id, int(version)), "Version")
        if int(version) != int(w["latest_version"]):
            raise gl.vm.UserError("Only the latest version can be disputed")
        if field not in w["fields"]:
            raise gl.vm.UserError("Unknown field: " + field)
        idx = int(source_index)
        if idx < 0 or idx >= len(w["sources"]):
            raise gl.vm.UserError("source_index out of range")
        if not isinstance(claimed_value, str) or len(claimed_value) > MAX_VALUE_LEN:
            raise gl.vm.UserError("claimed_value is invalid")

        rnd = int(version)
        key = self._rkey(watch_id, rnd, idx)
        stored = json.loads(self.readings.get(key, "{}"))
        if not stored:
            raise gl.vm.UserError("No reading from that source in this version")
        if stored["values"].get(field, "") == claimed_value:
            raise gl.vm.UserError("Stored reading already matches the claim")

        ex = self._extract(w["sources"][idx], w["fields"])
        if not self._extraction_ok(w["fields"], ex) or not ex.get("retrieved"):
            raise gl.vm.UserError("Could not re-read the source")
        if ex["values"].get(field, "") != claimed_value:
            raise gl.vm.UserError("Fresh reading does not support the claim")

        stored["values"][field] = claimed_value
        self.readings[key] = json.dumps(stored)

        readings = []
        for i in range(len(w["sources"])):
            raw = self.readings.get(self._rkey(watch_id, rnd, i), "")
            if raw:
                readings.append(json.loads(raw)["values"])
        values, status, agreed = self._majority(readings, w["fields"])
        old = self.versions[self._vkey(watch_id, int(v["parent"]))] if int(v["parent"]) > 0 else ""
        old_values = json.loads(old)["values"] if old else {}
        v.update(values=values, status=status, agreed=agreed,
                 changed_fields=self._diff(old_values, values, w["fields"]),
                 state="CORRECTED")
        self.versions[self._vkey(watch_id, rnd)] = json.dumps(v)

        w["values"] = values
        w["status"] = status
        self.watches[str(watch_id)] = json.dumps(w)
        return True

    # ---------------- views ----------------

    @gl.public.view
    def get_watch(self, watch_id: str) -> str:
        return self.watches.get(str(watch_id), "{}")

    @gl.public.view
    def get_version(self, watch_id: str, version: int) -> str:
        return self.versions.get(self._vkey(watch_id, int(version)), "{}")

    @gl.public.view
    def get_reading(self, watch_id: str, round_no: int, source_index: int) -> str:
        return self.readings.get(self._rkey(watch_id, int(round_no), int(source_index)), "{}")

    @gl.public.view
    def get_field(self, watch_id: str, field: str) -> dict:
        w = self.watches.get(str(watch_id), "")
        if not w:
            return {}
        rec = json.loads(w)
        return {"value": rec.get("values", {}).get(field, ""),
                "status": rec.get("status", {}).get(field, MISSING)}

    @gl.public.view
    def get_history(self, watch_id: str) -> list:
        w = self.watches.get(str(watch_id), "")
        if not w:
            return []
        rec = json.loads(w)
        out = []
        for n in range(1, int(rec["latest_version"]) + 1):
            raw = self.versions.get(self._vkey(watch_id, n), "")
            if raw:
                out.append(json.loads(raw))
        return out

    @gl.public.view
    def get_watch_count(self) -> int:
        return self.watch_count

    @gl.public.view
    def get_version_count(self, watch_id: str) -> int:
        w = self.watches.get(str(watch_id), "")
        return int(json.loads(w)["latest_version"]) if w else 0
