"""Anmeldung gegen Active Directory (Windows Server oder Samba AD) bzw. LDAP.

Ablauf
  1. Verbindung mit dem Dienstkonto (Bind-DN) – oder, falls keines hinterlegt ist,
     direkt als Benutzer per UPN (benutzer@domaene.local).
  2. Suche des Benutzers unterhalb der Basis-DN über das Login-Attribut
     (AD/Samba-AD: sAMAccountName, OpenLDAP/klassisches Samba: uid).
  3. Bind mit dem DN und Passwort des Benutzers (eigentliche Passwortprüfung).
  4. Ermitteln der Gruppen (memberOf, optional verschachtelt über
     LDAP_MATCHING_RULE_IN_CHAIN, Fallback member/uniqueMember/memberUid).
  5. Regeln anwenden. Jede Regel ist entweder eine OU/ein Container
     (der Benutzer liegt darunter) oder der DN einer Gruppe (der Benutzer ist Mitglied):
       - Admin-Regeln       -> Rolle Administrator
       - Verwalter-Regeln   -> Rolle Verwalter
       - Login-Regeln       -> Rolle Mitarbeiter
     Ist keine Regel erfüllt, wird die Anmeldung abgelehnt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .i18n import _
from .models import ROLE_ADMIN, ROLE_MANAGER, ROLE_READER, get_bool, get_int, get_setting

log = logging.getLogger(__name__)
IN_CHAIN = "1.2.840.113556.1.4.1941"


class LDAPError(Exception):
    """Fehler mit einer für Administratoren verständlichen Meldung."""


class LDAPUserNotFound(LDAPError):
    """Das Konto existiert (nicht mehr) im Verzeichnis."""


class _SizeLimit(Exception):
    """Der Server hat eine Suche am Größenlimit abgebrochen (intern)."""


class LDAPAmbiguous(LDAPError):
    """Der Benutzername passt auf mehrere Einträge im Verzeichnis."""


@dataclass
class LDAPUser:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    role: str | None = None
    matched_rule: str = ""
    group_info: dict = field(default_factory=dict)  # DN -> direkt | primär | verschachtelt
    guid: str = ""   # feste Kennung (AD: objectGUID, OpenLDAP: entryUUID) – bleibt bei Umbenennungen gleich


def normalize_dn(dn: str) -> str:
    return re.sub(r"\s*([,=])\s*", r"\1", (dn or "").strip()).lower()


def parse_rules(text: str) -> list[str]:
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def group_cn(dn: str) -> str:
    """'CN=IT-Admins,OU=Gruppen,DC=x' -> 'IT-Admins'"""
    m = re.match(r"\s*cn\s*=\s*((?:\\.|[^,])+)", dn or "", re.I)
    return re.sub(r"\\(.)", r"\1", m.group(1)).strip() if m else ""


def is_group_name(rule: str) -> bool:
    """Eine Regel ohne '=' ist ein Gruppenname (z. B. 'IT-Admins')."""
    return "=" not in rule


def rule_matches(rule: str, user_dn: str, group_dns: set[str]) -> bool:
    """Regel = AD-Gruppenname, Gruppen-DN oder OU/Container."""
    rule = (rule or "").strip()
    if not rule:
        return False
    if is_group_name(rule):
        def norm(x):  # gleiche Normalisierung wie bei DNs (Leerzeichen um Kommas)
            return re.sub(r"\s*,\s*", ",", x).lower()
        name = norm(rule)
        return any(norm(group_cn(g)) == name for g in group_dns)
    r = normalize_dn(rule)
    u = normalize_dn(user_dn)
    if r in group_dns:
        return True
    return u == r or u.endswith("," + r)


def resolve_role(user_dn: str, groups: list[str], cfg: dict | None = None) -> tuple[str | None, str]:
    cfg = cfg or {}
    gset = {normalize_dn(g) for g in groups}
    def rules(key):
        return cfg[key] if key in cfg else get_setting(key)

    checks = (
        (ROLE_ADMIN, rules("ldap_admin_rules")),
        (ROLE_MANAGER, rules("ldap_manager_rules")),
        (ROLE_READER, rules("ldap_login_rules")),
    )
    for role, text in checks:
        for rule in parse_rules(text):
            if rule_matches(rule, user_dn, gset):
                return role, rule
    return None, ""


def escape_filter(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(value)


class Directory:
    def __init__(self, cfg: dict | None = None):
        def s(key):
            return cfg[key] if cfg and key in cfg else get_setting(key)

        self.cfg = cfg or {}
        self.servers = [x.strip() for x in re.split(r"[,\s]+", s("ldap_server") or "") if x.strip()]
        self.security = s("ldap_security") or "none"
        self.verify = (s("ldap_verify_cert") or "1") in ("1", "true", "on")
        self.ca_file = s("ldap_ca_file") or None
        self.tls_certs = s("ldap_tls_certs") or ""
        self.check_host = (s("ldap_check_hostname") or "1") in ("1", "true", "on")
        self.bind_dn = s("ldap_bind_dn") or ""
        self.bind_password = s("ldap_bind_password") or ""
        self.upn_suffix = (s("ldap_upn_suffix") or "").lstrip("@")
        self.base_dn = s("ldap_base_dn") or ""
        self.user_attr = s("ldap_user_attr") or "sAMAccountName"
        self.user_filter = s("ldap_user_filter") or ""
        self.nested = (s("ldap_nested_groups") or "0") in ("1", "true", "on")
        try:
            self.timeout = int(s("ldap_timeout") or 8)
        except ValueError:
            self.timeout = 8
        if not self.servers:
            raise LDAPError(_("Kein LDAP-Server konfiguriert."))
        if not self.base_dn:
            raise LDAPError(_("Keine Basis-DN konfiguriert."))

    # ---------------------------------------------------------------- Verbindung
    def _server_pool(self):
        from ldap3 import FIRST, Server, ServerPool

        from .ldap_tls import make_tls

        tls = None
        if self.security in ("starttls", "ldaps") or any(h.lower().startswith("ldaps://") for h in self.servers):
            tls = make_tls(self.tls_certs, self.ca_file, self.verify, self.check_host)
        servers = []
        for host in self.servers:
            use_ssl = self.security == "ldaps" or host.lower().startswith("ldaps://")
            servers.append(Server(host, use_ssl=use_ssl, tls=tls, get_info=None,
                                  connect_timeout=self.timeout))
        return ServerPool(servers, FIRST, active=1, exhaust=True) if len(servers) > 1 else servers[0]

    def connect(self, user: str, password: str):
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException

        if not password:
            # Leeres Passwort = anonymer Bind -> niemals als Erfolg werten
            raise LDAPError(_("Leeres Passwort."))
        conn = None
        try:
            conn = Connection(self._server_pool(), user=user, password=password,
                              receive_timeout=self.timeout, raise_exceptions=False,
                              auto_referrals=False, read_only=True)
            conn.open(read_server_info=False)
            if self.security == "starttls" and not conn.start_tls(read_server_info=False):
                raise LDAPError(_("StartTLS fehlgeschlagen: {error}", error=(conn.result or {}).get('description')))
            if not conn.bind(read_server_info=False):
                desc = conn.result.get("description", "")
                msg = conn.result.get("message", "")
                raise LDAPError(_("Bind abgelehnt ({desc}). {msg}", desc=desc, msg=msg).strip())
            return conn
        except (LDAPException, LDAPError) as exc:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(exc, LDAPError):
                raise
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise LDAPError(_("Das Zertifikat des Domänencontrollers ist nicht vertrauenswürdig. "
                                  "Unter „TLS-Zertifikat“ auf „Zertifikat vom DC abrufen“ klicken und bestätigen.")) from exc
            raise LDAPError(_("LDAP-Fehler: {error}", error=exc)) from exc

    def service_connection(self, username: str | None = None, password: str | None = None):
        if self.bind_dn:
            return self.connect(self.bind_dn, self.bind_password)
        if username and password:
            principal = username if ("@" in username or "\\" in username or not self.upn_suffix) \
                else f"{username}@{self.upn_suffix}"
            return self.connect(principal, password)
        raise LDAPError(_("Weder Dienstkonto (Bind-DN) noch UPN-Suffix für direkten Bind konfiguriert."))

    # ---------------------------------------------------------------- Suche
    def find_user(self, conn, username: str) -> dict:
        from ldap3 import SUBTREE

        flt = f"({self.user_attr}={escape_filter(username)})"
        if self.user_filter:
            uf = self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})"
            flt = f"(&{uf}{flt})"
        conn.search(self.base_dn, flt, SUBTREE,
                    attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn",
                                "objectSid", "primaryGroupID", "userAccountControl", "accountExpires",
                                "objectClass", self.user_attr] + GUID_ATTRS, size_limit=2)
        entries = [e for e in conn.response if e.get("type") == "searchResEntry"]
        if not entries:
            raise LDAPUserNotFound(_("Benutzer '{username}' nicht unterhalb von {base} gefunden.", username=username, base=self.base_dn))
        if len(entries) > 1:
            raise LDAPAmbiguous(_("Benutzername '{username}' ist nicht eindeutig.", username=username))
        return entries[0]

    # ---------------------------------------------------------------- Gruppen
    def group_base(self, user_dn: str = "") -> str:
        """Suchbasis für Gruppen: die Domänenwurzel (DC=…), nicht nur die Benutzer-OU.

        Gruppen liegen oft außerhalb der Benutzer-OU (z. B. OU=Gruppen, CN=Users, CN=Builtin)."""
        for dn in (self.base_dn, user_dn):
            m = re.search(r"(?:^|,)\s*(dc\s*=.*)$", dn or "", re.I)
            if m:
                return m.group(1)
        return self.base_dn

    def _search_dns(self, conn, base: str, flt: str) -> list[str]:
        from ldap3 import SUBTREE
        if not conn.search(base, flt, SUBTREE, attributes=["1.1"]):
            return []
        return [e["dn"] for e in conn.response if e.get("type") == "searchResEntry"]

    def _sids_to_dns(self, conn, base: str, sids: list[bytes]) -> list[str]:
        dns = []
        for i in range(0, len(sids), 40):
            part = "".join(f"(objectSid={_escape_bytes(sid)})" for sid in sids[i:i + 40])
            dns += self._search_dns(conn, base, f"(|{part})")
        return dns

    def user_groups(self, conn, user_dn: str, username: str, entry: dict) -> dict[str, str]:
        """Alle Gruppen eines Benutzers als {DN: Herkunft}.

        Herkunft: 'direkt' (memberOf), 'primär' (primaryGroupID, z. B. Domänen-Benutzer –
        steht in AD NICHT in memberOf!) oder 'verschachtelt' (über andere Gruppen).
        Kombiniert mehrere Verfahren, damit nichts verloren geht, wenn eines davon
        vom Server nicht unterstützt oder durch Rechte eingeschränkt wird."""
        from ldap3 import BASE

        attrs = entry.get("attributes", {}) or {}
        raw = entry.get("raw_attributes", {}) or {}
        base = self.group_base(user_dn)
        found: dict[str, str] = {}

        def add(dns, source):
            for dn in dns:
                key = normalize_dn(dn)
                # bessere (direktere) Herkunft nicht überschreiben
                if key not in {normalize_dn(k) for k in found}:
                    found[dn] = source

        # 1) Direkte Mitgliedschaften
        add(_as_list(attrs.get("memberOf")), "direkt")

        # 2) Primäre Gruppe (AD/Samba: primaryGroupID + Domänen-SID des Benutzers)
        sid = (raw.get("objectSid") or [None])[0]
        pgid = _first(attrs.get("primaryGroupID"))
        primary_dns = []
        if sid and pgid.isdigit() and len(sid) >= 12:
            primary_sid = sid[:-4] + int(pgid).to_bytes(4, "little")
            primary_dns = self._sids_to_dns(conn, base, [primary_sid])
            add(primary_dns, "primär")

        if self.nested:
            # 3) AD/Samba-AD: alle (auch verschachtelten) Gruppen über LDAP_MATCHING_RULE_IN_CHAIN
            add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(user_dn)})"), "verschachtelt")
            for pdn in primary_dns:  # Gruppen, in denen die primäre Gruppe Mitglied ist
                add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(pdn)})"), "verschachtelt")

            # 4) AD/Samba-AD: tokenGroups – exakt die Sicherheitsgruppen aus dem Anmeldetoken
            if conn.search(user_dn, "(objectClass=*)", BASE, attributes=["tokenGroups"]):
                for e in conn.response:
                    if e.get("type") == "searchResEntry":
                        sids = (e.get("raw_attributes") or {}).get("tokenGroups") or []
                        if sids:
                            add(self._sids_to_dns(conn, base, list(sids)), "verschachtelt")

        # 5) OpenLDAP / klassische Samba-Domäne (kein memberOf, keine IN_CHAIN-Regel)
        if not found:
            flt = (f"(|(member={escape_filter(user_dn)})(uniqueMember={escape_filter(user_dn)})"
                   f"(memberUid={escape_filter(username)}))")
            add(self._search_dns(conn, base, flt), "direkt")
            if self.nested:
                queue, seen = list(found), {normalize_dn(d) for d in found}
                for _ in range(10):  # Ebene für Ebene nach oben
                    nxt = []
                    for gdn in queue:
                        for parent in self._search_dns(
                                conn, base, f"(|(member={escape_filter(gdn)})(uniqueMember={escape_filter(gdn)}))"):
                            if normalize_dn(parent) not in seen:
                                seen.add(normalize_dn(parent))
                                nxt.append(parent)
                    add(nxt, "verschachtelt")
                    if not nxt:
                        break
                    queue = nxt
        return found

    def list_groups(self, query: str = "", limit: int = 1000) -> list[dict]:
        """Gruppen aus dem Verzeichnis zur Auswahl in der Oberfläche."""
        from ldap3 import SUBTREE

        flt = ("(|(objectClass=group)(objectClass=groupOfNames)"
               "(objectClass=groupOfUniqueNames)(objectClass=posixGroup))")
        if query:
            q = escape_filter(query.strip())
            flt = f"(&{flt}(|(cn=*{q}*)(description=*{q}*)))"
        conn = self.service_connection()
        try:
            entries = conn.extend.standard.paged_search(self.group_base(), flt, SUBTREE,
                                                        attributes=["cn", "description"],
                                                        paged_size=500, generator=True)
            groups = []
            for e in entries:
                if e.get("type") != "searchResEntry":
                    continue
                attrs = e.get("attributes", {})
                groups.append({"name": _first(attrs.get("cn")) or group_cn(e["dn"]), "dn": e["dn"],
                               "description": _first(attrs.get("description"))})
                if len(groups) >= limit:
                    break
        finally:
            conn.unbind()
        return sorted(groups, key=lambda g: g["name"].lower())

    # ---------------------------------------------------------------- Login
    def authenticate(self, username: str, password: str) -> LDAPUser:
        username = (username or "").strip()
        if "\\" in username:  # DOMAENE\benutzer
            username = username.split("\\", 1)[1]
        if "@" in username and self.user_attr.lower() == "samaccountname":
            username = username.split("@", 1)[0]
        if not username or not password:
            raise LDAPError(_("Benutzername oder Passwort fehlt."))

        svc = self.service_connection(username, password)
        user_conn = None
        try:
            entry = self.find_user(svc, username)
            user_dn = entry["dn"]
            attrs = entry.get("attributes", {})
            # Passwort des Benutzers prüfen (bei direktem Bind bereits geschehen)
            if self.bind_dn:
                user_conn = self.connect(user_dn, password)
            # Gruppen bevorzugt als der Benutzer selbst lesen: jeder darf sein eigenes
            # tokenGroups lesen, ein Dienstkonto in gehärteten Domänen nicht immer.
            group_info = self.user_groups(user_conn or svc, user_dn, username, entry)
            if not group_info and user_conn is not None:
                group_info = self.user_groups(svc, user_dn, username, entry)
        finally:
            for c in (user_conn, svc):
                try:
                    if c is not None:
                        c.unbind()
                except Exception:
                    pass
        groups = list(group_info)

        display = _first(attrs.get("displayName")) or " ".join(
            x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x
        ) or _first(attrs.get("cn")) or username
        role, rule = resolve_role(user_dn, groups, self.cfg)
        return LDAPUser(username=_first(attrs.get(self.user_attr)) or username, dn=user_dn,
                        display_name=display, email=_first(attrs.get("mail")) or "",
                        groups=groups, role=role, matched_rule=rule, group_info=group_info, guid=_guid(entry))

    def check_account(self, username: str) -> "LDAPUser | None":
        """Ist das Konto noch vorhanden, aktiv und berechtigt? (für laufende Sitzungen, ohne Passwort)

        Liefert None, wenn das Konto fehlt oder im AD deaktiviert ist. Verbindungsprobleme lösen
        LDAPError aus – dann bleibt die Sitzung bestehen (ein AD-Ausfall soll niemanden aussperren)."""
        if not self.bind_dn:
            raise LDAPError(_("Ohne Dienstkonto kann eine Sitzung nicht nachgeprüft werden."))
        svc = self.service_connection()
        try:
            try:
                entry = self.find_user(svc, username)
            except LDAPUserNotFound:
                return None
            attrs = entry.get("attributes", {})
            try:
                uac = int(_first(attrs.get("userAccountControl")) or 0)
            except (TypeError, ValueError):
                uac = 0
            if uac & 2:   # ACCOUNTDISABLE
                return None
            if not _is_person_account(attrs, username):   # abgelaufen, Computer- oder Gruppenobjekt
                return None
            user_dn = entry["dn"]
            group_info = self.user_groups(svc, user_dn, username, entry)
        finally:
            try:
                svc.unbind()
            except Exception:  # noqa: BLE001
                pass
        groups = list(group_info)
        display = _first(attrs.get("displayName")) or " ".join(
            x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x) or _first(attrs.get("cn")) or username
        role, rule = resolve_role(user_dn, groups, self.cfg)
        return LDAPUser(username=_first(attrs.get(self.user_attr)) or username, dn=user_dn, display_name=display,
                        email=_first(attrs.get("mail")) or "", groups=groups, role=role, matched_rule=rule,
                        group_info=group_info, guid=_guid(entry))

    # ---------------------------------------------------------------- Personensuche (Ausgabetheke)
    @property
    def is_ad(self) -> bool:
        """Active Directory / Samba AD (Login über sAMAccountName) – dann gibt es userAccountControl usw."""
        return self.user_attr.lower() == "samaccountname"

    def search_users(self, query: str, exclude: set[str] | None = None, limit: int = 10,
                     candidates: int = 60, budget_s: float = 5.0) -> tuple[list[LDAPUser], bool]:
        """Personen im Verzeichnis suchen – für die Ausgabe an jemanden, der sich noch nie angemeldet hat.

        Gefunden wird nur, wer sich auch anmelden DÜRFTE: gleiche Basis-DN und Benutzerfilter wie beim Login,
        dieselben Rollen-Regeln (OUs/Gruppen für Mitarbeiter, Verwalter, Administratoren). Ausgeblendet werden
        im AD deaktivierte und abgelaufene Konten sowie Computer- und Gruppenobjekte. `exclude` (bekannte
        Benutzernamen) wird VOR der teuren Gruppenauflösung aussortiert.

        Liefert (Treffer, unvollständig). „unvollständig“ heißt: Kandidaten- oder Zeitbudget erschöpft –
        es könnte weitere Treffer geben, die Suche sollte genauer werden. Braucht das Dienstkonto."""
        import time
        from ldap3 import SUBTREE

        query = " ".join((query or "").split())
        if len(query) < 2:
            return [], False
        if not self.bind_dn:
            raise LDAPError(_("Für die Suche im Verzeichnis ist ein Dienstkonto (Bind-DN) nötig."))
        exclude = {x.lower() for x in (exclude or set())}
        q = escape_filter(query[:64])
        name_attrs = sorted({self.user_attr, "displayName", "cn", "givenName", "sn", "mail"})
        parts = ["(|" + "".join(f"({a}=*{q}*)" for a in name_attrs) + ")"]
        if self.user_filter:
            parts.insert(0, self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})")
        if self.is_ad:
            # nur Personen (keine Computer/Gruppen), nicht deaktiviert, nicht abgelaufen – schon auf dem Server
            parts += ["(objectCategory=person)(objectClass=user)",
                      "(!(userAccountControl:1.2.840.113556.1.4.803:=2))",
                      f"(|(!(accountExpires=*))(accountExpires=0)(accountExpires=9223372036854775807)"
                      f"(accountExpires>={_filetime_now()}))"]
        flt = "(&" + "".join(parts) + ")"
        deadline = time.monotonic() + budget_s
        conn = self.service_connection()
        found: list[LDAPUser] = []
        truncated = False
        try:
            conn.search(self.base_dn, flt, SUBTREE, size_limit=candidates + 1,
                        attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectClass",
                                    "objectSid", "primaryGroupID", "userAccountControl", "accountExpires",
                                    self.user_attr])
            entries = [e for e in conn.response if e.get("type") == "searchResEntry"]
            if len(entries) > candidates:
                entries, truncated = entries[:candidates], True
            for entry in entries:
                a = entry.get("attributes", {}) or {}
                username = _first(a.get(self.user_attr))
                if not username or username.lower() in exclude or not _is_person_account(a, username):
                    continue
                if time.monotonic() > deadline or len(found) >= limit:
                    truncated = True
                    break
                user_dn = entry["dn"]
                group_info = self.user_groups(conn, user_dn, username, entry)
                role, rule = resolve_role(user_dn, list(group_info), self.cfg)
                if role is None:     # dürfte sich nicht anmelden -> bleibt unsichtbar
                    continue
                found.append(LDAPUser(username=username, dn=user_dn, display_name=_display_name(a, username),
                                      email=_first(a.get("mail")) or "", groups=list(group_info), role=role,
                                      matched_rule=rule, group_info=group_info))
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
        return sorted(found, key=lambda u: (u.display_name or u.username).lower()), truncated

    # ---------------------------------------------------------------- Vollständiger Abgleich (Zwischenspeicher)
    def _person_filter(self) -> str:
        """Filter für „alle Personen, die sich anmelden könnten“ – wie bei der Personensuche, nur ohne Namen."""
        parts = [f"({self.user_attr}=*)"]
        if self.user_filter:
            parts.insert(0, self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})")
        if self.is_ad:
            parts += ["(objectCategory=person)(objectClass=user)",
                      "(!(userAccountControl:1.2.840.113556.1.4.803:=2))",
                      f"(|(!(accountExpires=*))(accountExpires=0)(accountExpires=9223372036854775807)"
                      f"(accountExpires>={_filetime_now()}))"]
        return "(&" + "".join(parts) + ")"

    def _paged_entries(self, conn, base: str, flt: str, attributes: list[str]):
        """Alle Treffer seitenweise (AD liefert sonst höchstens 1000 auf einmal).

        Wichtig: Mit raise_exceptions=False hört ldap3 bei einem Fehler (Größen-/Zeitlimit, Server beschäftigt …)
        einfach auf – ohne Ausnahme. Deshalb wird das Ergebnis danach geprüft: Größenlimit → `_SizeLimit`,
        alles andere → LDAPError. Eine abgeschnittene Liste darf nie als vollständig gelten."""
        from ldap3 import SUBTREE
        for e in conn.extend.standard.paged_search(base, flt, SUBTREE, attributes=attributes,
                                                   paged_size=500, generator=True):
            if e.get("type") == "searchResEntry":
                yield e
        res = conn.result if isinstance(getattr(conn, "result", None), dict) else {}
        code = res.get("result", 0) or 0
        if code == 4:            # sizeLimitExceeded
            raise _SizeLimit(res.get("description", "sizeLimitExceeded"))
        if code not in (0,):
            raise LDAPError(_("Lesen aus dem Verzeichnis abgebrochen: {error}",
                              error=f"{res.get('description', code)} {res.get('message', '')}".strip()))

    def all_users(self, max_users: int = 50000) -> tuple[list[LDAPUser], bool]:
        """Alle Personen, die sich nach den Regeln anmelden DÜRFTEN – für den Zwischenspeicher der Personensuche.

        Statt die Gruppen jeder Person einzeln abzufragen (wie bei Login und Suche), werden alle Gruppen und alle
        Personen je einmal seitenweise gelesen und die Mitgliedschaften im Speicher aufgelöst: direkt (memberOf
        bzw. member/uniqueMember/memberUid), primäre Gruppe (primaryGroupID) und – falls eingestellt –
        verschachtelt. Danach dieselben Rollen-Regeln wie beim Login. Deaktivierte, abgelaufene und gelöschte
        Konten sind nicht dabei.

        Liefert (Personen, vollständig). „vollständig“ ist False, wenn mehr als `max_users` Konten passen –
        dann darf der Aufrufer nichts löschen."""
        if not self.bind_dn:
            raise LDAPError(_("Für den Abgleich ist ein Dienstkonto (Bind-DN) nötig."))
        group_flt = ("(|(objectClass=group)(objectClass=groupOfNames)"
                     "(objectClass=groupOfUniqueNames)(objectClass=posixGroup))")
        group_attrs = ["cn", "memberOf", "objectSid"] + ([] if self.is_ad else ["member", "uniqueMember", "memberUid"])
        user_attrs = sorted({self.user_attr, "displayName", "cn", "givenName", "sn", "mail", "memberOf", "objectSid",
                             "primaryGroupID", "userAccountControl", "accountExpires", "objectClass", *GUID_ATTRS})
        conn = self.service_connection()
        try:
            # 1) Gruppen: DN, übergeordnete Gruppen, SID (für die primäre Gruppe), Mitglieder (OpenLDAP)
            parents: dict[str, set[str]] = {}       # Gruppe -> Gruppen, in denen sie Mitglied ist
            display_dn: dict[str, str] = {}
            by_sid: dict[bytes, str] = {}
            members_of: dict[str, set[str]] = {}    # Mitglied (DN oder memberUid) -> Gruppen (nur ohne memberOf)
            try:
                groups_read = list(self._paged_entries(conn, self.group_base(), group_flt, group_attrs))
            except _SizeLimit as exc:   # ohne alle Gruppen wären die Rollen falsch – dann lieber gar nicht
                raise LDAPError(_("Lesen der Gruppen am Größenlimit des Servers abgebrochen ({error}) – "
                                  "Limit für das Dienstkonto erhöhen.", error=exc)) from exc
            for e in groups_read:
                key = normalize_dn(e["dn"])
                display_dn[key] = e["dn"]
                a = e.get("attributes", {}) or {}
                parents.setdefault(key, set()).update(normalize_dn(g) for g in _as_list(a.get("memberOf")))
                sid = ((e.get("raw_attributes") or {}).get("objectSid") or [None])[0]
                if sid:
                    by_sid[bytes(sid)] = key
                for m in _as_list(a.get("member")) + _as_list(a.get("uniqueMember")):
                    members_of.setdefault(normalize_dn(m), set()).add(key)
                for uid in _as_list(a.get("memberUid")):
                    members_of.setdefault("uid:" + str(uid), set()).add(key)
            if members_of:      # OpenLDAP: Gruppe in Gruppe steht nur in member der übergeordneten Gruppe
                for child, groups in members_of.items():
                    if child in parents:
                        parents[child].update(groups)

            def closure(start: set[str]) -> set[str]:
                seen, todo = set(start), list(start)
                while todo:
                    for parent in parents.get(todo.pop(), ()):
                        if parent not in seen:
                            seen.add(parent)
                            todo.append(parent)
                return seen

            # 2) Personen
            found: list[LDAPUser] = []
            names_seen: dict[str, int] = {}
            complete = True
            entries = iter(self._paged_entries(conn, self.base_dn, self._person_filter(), user_attrs))
            while True:
                try:
                    e = next(entries)
                except StopIteration:
                    break
                except _SizeLimit:
                    complete = False    # Teilliste: nichts löschen oder deaktivieren
                    break
                a = e.get("attributes", {}) or {}
                username = _first(a.get(self.user_attr))
                if not username:
                    continue
                names_seen[username.lower()] = names_seen.get(username.lower(), 0) + 1
                if not _is_person_account(a, username):
                    continue
                if len(found) >= max_users:
                    complete = False
                    break
                user_dn = e["dn"]
                # wie beim Login (user_groups): memberOf und primäre Gruppe; die Mitgliederlisten der Gruppen
                # (member/uniqueMember/memberUid) nur, wenn es beides nicht gibt (OpenLDAP ohne memberOf)
                groups = {normalize_dn(g) for g in _as_list(a.get("memberOf"))}
                sid = ((e.get("raw_attributes") or {}).get("objectSid") or [None])[0]
                pgid = _first(a.get("primaryGroupID"))
                if sid and pgid.isdigit() and len(sid) >= 12:
                    primary = by_sid.get(bytes(sid[:-4]) + int(pgid).to_bytes(4, "little"))
                    if primary:
                        groups.add(primary)
                if not groups:
                    groups = set(members_of.get(normalize_dn(user_dn), set()) | members_of.get("uid:" + username, set()))
                if self.nested:
                    groups = closure(groups)
                group_dns = [display_dn.get(g, g) for g in groups]
                role, rule = resolve_role(user_dn, group_dns, self.cfg)
                if role is None:
                    continue
                found.append(LDAPUser(username=username, dn=user_dn, display_name=_display_name(a, username),
                                      email=_first(a.get("mail")) or "", groups=group_dns, role=role,
                                      matched_rule=rule, guid=_guid(e)))
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
        # Benutzername mehrfach (z. B. gleiche uid in zwei OUs): der Login lehnt so etwas als mehrdeutig ab –
        # also hier auch niemanden davon übernehmen
        return [u for u in found if names_seen.get(u.username.lower(), 0) == 1], complete

    def test_connection(self) -> str:
        conn = self.service_connection()
        who = conn.extend.standard.who_am_i() if hasattr(conn, "extend") else ""
        ok = conn.search(self.base_dn, "(objectClass=*)", "BASE", attributes=["objectClass"])
        conn.unbind()
        if not ok:
            raise LDAPError(_("Bind erfolgreich, aber Basis-DN '{base}' nicht lesbar.", base=self.base_dn))
        return _("Verbindung erfolgreich. Angemeldet als {who}. Basis-DN ist erreichbar.", who=who or self.bind_dn)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return v
    return [v]


def _escape_bytes(data: bytes) -> str:
    """Binärwert (z. B. SID) für einen LDAP-Filter kodieren."""
    return "".join(f"\\{b:02x}" for b in data)


_FILETIME_NEVER = (0, 9223372036854775807)


def _filetime_now() -> int:
    """Jetzt als Windows-FILETIME (100-ns-Schritte seit 1601) – Format von accountExpires."""
    import time
    return int((time.time() + 11644473600) * 10_000_000)


def _expired(value) -> bool:
    """accountExpires in der Vergangenheit? (0 und 0x7FFFFFFFFFFFFFFF = läuft nie ab)"""
    from datetime import datetime, timezone
    v = value[0] if isinstance(value, (list, tuple)) and value else value
    if v in (None, "", []):
        return False
    if isinstance(v, datetime):           # ldap3 mit Schema liefert ein Datum
        if v.year >= 9999 or v.year <= 1601:
            return False
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return False
    return n not in _FILETIME_NEVER and n < _filetime_now()


def _is_person_account(attrs: dict, username: str) -> bool:
    """Nur echte, nutzbare Benutzerkonten: keine Computer (Name endet auf $), keine Gruppen, im AD nicht
    deaktiviert und nicht abgelaufen. (Zusätzlich zum Suchfilter auf dem Server – doppelt hält besser.)"""
    if username.endswith("$"):
        return False
    try:
        if int(_first(attrs.get("userAccountControl")) or 0) & 2:   # ACCOUNTDISABLE
            return False
    except (TypeError, ValueError):
        pass
    classes = {str(c).lower() for c in _as_list(attrs.get("objectClass"))}
    if classes & {"computer", "group", "groupofnames", "groupofuniquenames", "posixgroup", "contact"}:
        return False
    if classes and not classes & {"user", "inetorgperson", "posixaccount", "person", "account"}:
        return False      # z. B. Organisationseinheiten oder Geräteobjekte: kein Anmeldekonto
    return not _expired(attrs.get("accountExpires"))


GUID_ATTRS = ["objectGUID", "entryUUID"]


def _guid(entry: dict) -> str:
    """Feste Kennung eines Eintrags als Text (objectGUID binär → hex, entryUUID wie geliefert)."""
    raw = (entry.get("raw_attributes") or {}).get("objectGUID") or []
    if raw and isinstance(raw[0], (bytes, bytearray)) and len(raw[0]) == 16:
        return bytes(raw[0]).hex()
    attrs = entry.get("attributes") or {}
    for key in GUID_ATTRS:
        v = _first(attrs.get(key))
        if v:
            return v.strip("{}").lower()
    return ""


def _display_name(attrs: dict, fallback: str) -> str:
    return _first(attrs.get("displayName")) or " ".join(
        x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x) or _first(attrs.get("cn")) or fallback


def _first(v) -> str:
    lst = _as_list(v)
    return str(lst[0]) if lst else ""


def ldap_enabled() -> bool:
    return get_bool("ldap_enabled")


def authenticate(username: str, password: str) -> LDAPUser:
    return Directory().authenticate(username, password)


__all__ = ["LDAPError", "LDAPUser", "Directory", "authenticate", "ldap_enabled",
           "resolve_role", "rule_matches", "group_cn", "is_group_name", "normalize_dn", "parse_rules", "get_int"]
