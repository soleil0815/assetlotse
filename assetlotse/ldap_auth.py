"""Authentication against Active Directory (Windows Server or Samba AD) or LDAP.

Flow
  1. Connect with the service account (bind DN) – or, if none is configured,
     directly as the user via UPN (user@domain.local).
  2. Search for the user below the base DN via the login attribute
     (AD/Samba AD: sAMAccountName, OpenLDAP/classic Samba: uid).
  3. Bind with the user's DN and password (the actual password check).
  4. Determine the groups (memberOf, optionally nested via
     LDAP_MATCHING_RULE_IN_CHAIN, fallback member/uniqueMember/memberUid).
  5. Apply the rules. Each rule is either an OU/container
     (the user is below it) or a group DN (the user is a member):
       - admin rules     -> role administrator
       - manager rules   -> role manager
       - login rules     -> role staff
     If no rule matches, the login is refused.
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
    """Error with a message that administrators can understand."""


class LDAPUserNotFound(LDAPError):
    """The account does not exist (any more) in the directory."""


class _SizeLimit(Exception):
    """The server stopped a search at its size limit (internal)."""


class LDAPAmbiguous(LDAPError):
    """The username matches several entries in the directory."""


@dataclass
class LDAPUser:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    role: str | None = None
    matched_rule: str = ""
    group_info: dict = field(default_factory=dict)  # DN -> direct | primary | nested
    guid: str = ""   # stable ID (AD: objectGUID, OpenLDAP: entryUUID) – survives renames


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
    """'CN=IT-Admins,OU=Groups,DC=x' -> 'IT-Admins'"""
    m = re.match(r"\s*cn\s*=\s*((?:\\.|[^,])+)", dn or "", re.I)
    return re.sub(r"\\(.)", r"\1", m.group(1)).strip() if m else ""


def is_group_name(rule: str) -> bool:
    """A rule without '=' is a group name (e.g. 'IT-Admins')."""
    return "=" not in rule


def rule_matches(rule: str, user_dn: str, group_dns: set[str]) -> bool:
    """Rule = AD group name, group DN or OU/container."""
    rule = (rule or "").strip()
    if not rule:
        return False
    if is_group_name(rule):
        def norm(x):  # same normalization as for DNs (spaces around commas)
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
            raise LDAPError(_("No LDAP server configured."))
        if not self.base_dn:
            raise LDAPError(_("No base DN configured."))

    # ---------------------------------------------------------------- Connection
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
            # Empty password = anonymous bind -> never treat as success
            raise LDAPError(_("Empty password."))
        conn = None
        try:
            conn = Connection(self._server_pool(), user=user, password=password,
                              receive_timeout=self.timeout, raise_exceptions=False,
                              auto_referrals=False, read_only=True)
            conn.open(read_server_info=False)
            if self.security == "starttls" and not conn.start_tls(read_server_info=False):
                raise LDAPError(_("StartTLS failed: {error}", error=(conn.result or {}).get('description')))
            if not conn.bind(read_server_info=False):
                desc = conn.result.get("description", "")
                msg = conn.result.get("message", "")
                # not "msg=": that is already the name of _()'s first parameter – it would raise a TypeError instead of the message
                raise LDAPError(_("Bind refused ({desc}). {detail}", desc=desc, detail=msg).strip())
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
                raise LDAPError(_("The domain controller's certificate is not trusted. Under “TLS "
                                  "certificate”, click “Fetch certificate from DC” and confirm.")) from exc
            raise LDAPError(_("LDAP error: {error}", error=exc)) from exc

    def service_connection(self, username: str | None = None, password: str | None = None):
        if self.bind_dn:
            return self.connect(self.bind_dn, self.bind_password)
        if username and password:
            principal = username if ("@" in username or "\\" in username or not self.upn_suffix) \
                else f"{username}@{self.upn_suffix}"
            return self.connect(principal, password)
        raise LDAPError(_("Neither a service account (bind DN) nor a UPN suffix for direct bind is configured."))

    # ---------------------------------------------------------------- Search
    # Transient server errors (operationsError, timeLimit, adminLimit, busy, unavailable, other): with
    # raise_exceptions=False ldap3 then simply returns no results – that must never count as "does not exist".
    TRANSIENT_CODES = {1, 3, 11, 51, 52, 80}

    @staticmethod
    def _search_failed(conn, ok_codes=(0, 4, 32)) -> tuple[int, str] | None:
        res = conn.result if isinstance(getattr(conn, "result", None), dict) else {}
        code = res.get("result", 0) or 0
        if code in ok_codes:
            return None
        return code, f"{res.get('description', code)} {res.get('message', '')}".strip()

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
        failed = self._search_failed(conn)
        if failed:
            raise LDAPError(_("Directory search failed: {error}", error=failed[1]))
        entries = [e for e in (conn.response or []) if e.get("type") == "searchResEntry"]
        if not entries:
            raise LDAPUserNotFound(_("User '{username}' not found below {base}.", username=username, base=self.base_dn))
        if len(entries) > 1:
            raise LDAPAmbiguous(_("Username '{username}' is not unique.", username=username))
        return entries[0]

    # ---------------------------------------------------------------- Groups
    def group_base(self, user_dn: str = "") -> str:
        """Search base for groups: the domain root (DC=…), not just the user OU.

        Groups often live outside the user OU (e.g. OU=Groups, CN=Users, CN=Builtin)."""
        for dn in (self.base_dn, user_dn):
            m = re.search(r"(?:^|,)\s*(dc\s*=.*)$", dn or "", re.I)
            if m:
                return m.group(1)
        return self.base_dn

    def _search_dns(self, conn, base: str, flt: str) -> list[str]:
        from ldap3 import SUBTREE
        if not conn.search(base, flt, SUBTREE, attributes=["1.1"]):
            failed = self._search_failed(conn)
            if failed and failed[0] in self.TRANSIENT_CODES:
                # server not ready right now: better to abort than to lose groups (and thus permissions)
                raise LDAPError(_("Directory search failed: {error}", error=failed[1]))
            return []      # e.g. search method not supported – the other methods cover it
        return [e["dn"] for e in conn.response if e.get("type") == "searchResEntry"]

    def _sids_to_dns(self, conn, base: str, sids: list[bytes]) -> list[str]:
        dns = []
        for i in range(0, len(sids), 40):
            part = "".join(f"(objectSid={_escape_bytes(sid)})" for sid in sids[i:i + 40])
            dns += self._search_dns(conn, base, f"(|{part})")
        return dns

    def user_groups(self, conn, user_dn: str, username: str, entry: dict) -> dict[str, str]:
        """All groups of a user as {DN: source}.

        Source: 'direct' (memberOf), 'primary' (primaryGroupID, e.g. Domain Users –
        in AD this is NOT in memberOf!) or 'nested' (via other groups).
        Combines several methods so nothing is lost when one of them is not
        supported by the server or is restricted by permissions."""
        from ldap3 import BASE

        attrs = entry.get("attributes", {}) or {}
        raw = entry.get("raw_attributes", {}) or {}
        base = self.group_base(user_dn)
        found: dict[str, str] = {}

        def add(dns, source):
            for dn in dns:
                key = normalize_dn(dn)
                # do not overwrite a better (more direct) source
                if key not in {normalize_dn(k) for k in found}:
                    found[dn] = source

        # 1) Direct memberships
        add(_as_list(attrs.get("memberOf")), "direct")

        # 2) Primary group (AD/Samba: primaryGroupID + the user's domain SID)
        sid = (raw.get("objectSid") or [None])[0]
        pgid = _first(attrs.get("primaryGroupID"))
        primary_dns = []
        if sid and pgid.isdigit() and len(sid) >= 12:
            primary_sid = sid[:-4] + int(pgid).to_bytes(4, "little")
            primary_dns = self._sids_to_dns(conn, base, [primary_sid])
            add(primary_dns, "primary")

        if self.nested:
            # 3) AD/Samba AD: all (including nested) groups via LDAP_MATCHING_RULE_IN_CHAIN
            add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(user_dn)})"), "nested")
            for pdn in primary_dns:  # groups the primary group is a member of
                add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(pdn)})"), "nested")

            # 4) AD/Samba AD: tokenGroups – exactly the security groups from the logon token
            if conn.search(user_dn, "(objectClass=*)", BASE, attributes=["tokenGroups"]):
                for e in conn.response:
                    if e.get("type") == "searchResEntry":
                        sids = (e.get("raw_attributes") or {}).get("tokenGroups") or []
                        if sids:
                            add(self._sids_to_dns(conn, base, list(sids)), "nested")

        # 5) OpenLDAP / classic Samba domain (no memberOf, no IN_CHAIN rule)
        if not found:
            flt = (f"(|(member={escape_filter(user_dn)})(uniqueMember={escape_filter(user_dn)})"
                   f"(memberUid={escape_filter(username)}))")
            add(self._search_dns(conn, base, flt), "direct")
            if self.nested:
                queue, seen = list(found), {normalize_dn(d) for d in found}
                for _ in range(10):  # upwards level by level
                    nxt = []
                    for gdn in queue:
                        for parent in self._search_dns(
                                conn, base, f"(|(member={escape_filter(gdn)})(uniqueMember={escape_filter(gdn)}))"):
                            if normalize_dn(parent) not in seen:
                                seen.add(normalize_dn(parent))
                                nxt.append(parent)
                    add(nxt, "nested")
                    if not nxt:
                        break
                    queue = nxt
        return found

    def list_groups(self, query: str = "", limit: int = 1000) -> list[dict]:
        """Groups from the directory for selection in the UI."""
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
        if "\\" in username:  # DOMAIN\user
            username = username.split("\\", 1)[1]
        if "@" in username and self.user_attr.lower() == "samaccountname":
            username = username.split("@", 1)[0]
        if not username or not password:
            raise LDAPError(_("Username or password missing."))

        svc = self.service_connection(username, password)
        user_conn = None
        try:
            entry = self.find_user(svc, username)
            user_dn = entry["dn"]
            attrs = entry.get("attributes", {})
            # check the user's password (already done with a direct bind)
            if self.bind_dn:
                user_conn = self.connect(user_dn, password)
            # prefer reading groups as the user: anyone may read their own
            # tokenGroups, a service account in hardened domains not always.
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
        """Does the account still exist, is it active and authorized? (for running sessions, no password)

        Returns None if the account is missing or disabled in AD. Connection problems raise
        LDAPError – the session then stays (an AD outage must not lock anyone out)."""
        if not self.bind_dn:
            raise LDAPError(_("A session cannot be re-checked without a service account."))
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
            if not _is_person_account(attrs, username):   # expired, computer or group object
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

    # ---------------------------------------------------------------- People search (checkout desk)
    @property
    def is_ad(self) -> bool:
        """Active Directory / Samba AD (login via sAMAccountName) – then userAccountControl etc. exist."""
        return self.user_attr.lower() == "samaccountname"

    def search_users(self, query: str, exclude: set[str] | None = None, limit: int = 10,
                     candidates: int = 60, budget_s: float = 5.0) -> tuple[list[LDAPUser], bool]:
        """Search people in the directory – for checking out to someone who has never logged in.

        Only people who WOULD be allowed to log in are found: same base DN and user filter as the login,
        the same role rules (OUs/groups for staff, managers, administrators). Accounts disabled or expired
        in AD as well as computer and group objects are hidden. `exclude` (known usernames) is filtered
        out BEFORE the expensive group resolution.

        Returns (matches, truncated). "truncated" means the candidate or time budget ran out –
        there may be more matches, the search should be narrowed. Requires the service account."""
        import time
        from ldap3 import SUBTREE

        query = " ".join((query or "").split())
        if len(query) < 2:
            return [], False
        if not self.bind_dn:
            raise LDAPError(_("Searching the directory requires a service account (bind DN)."))
        exclude = {x.lower() for x in (exclude or set())}
        q = escape_filter(query[:64])
        name_attrs = sorted({self.user_attr, "displayName", "cn", "givenName", "sn", "mail"})
        parts = ["(|" + "".join(f"({a}=*{q}*)" for a in name_attrs) + ")"]
        if self.user_filter:
            parts.insert(0, self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})")
        if self.is_ad:
            # only people (no computers/groups), not disabled, not expired – already on the server
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
                if role is None:     # would not be allowed to log in -> stays invisible
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

    # ---------------------------------------------------------------- Full sync (cache)
    def _person_filter(self) -> str:
        """Filter for "all people who could log in" – like the people search, just without a name."""
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
        """All results, page by page (otherwise AD returns at most 1000 at once).

        Important: with raise_exceptions=False ldap3 simply stops on an error (size/time limit, server busy …)
        – without an exception. So the result is checked afterwards: size limit → `_SizeLimit`,
        anything else → LDAPError. A truncated list must never count as complete."""
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
            raise LDAPError(_("Reading from the directory was aborted: {error}",
                              error=f"{res.get('description', code)} {res.get('message', '')}".strip()))

    def all_users(self, max_users: int = 50000) -> tuple[list[LDAPUser], bool]:
        """All people who WOULD be allowed to log in under the rules – for the people search cache.

        Instead of querying each person's groups individually (as login and search do), all groups and all
        people are each read once, page by page, and memberships are resolved in memory: direct (memberOf
        or member/uniqueMember/memberUid), primary group (primaryGroupID) and – if enabled – nested.
        Then the same role rules as for login. Disabled, expired and deleted accounts are not included.

        Returns (people, complete). "complete" is False if more than `max_users` accounts match –
        the caller must then not delete anything."""
        if not self.bind_dn:
            raise LDAPError(_("Synchronisation requires a service account (bind DN)."))
        group_flt = ("(|(objectClass=group)(objectClass=groupOfNames)"
                     "(objectClass=groupOfUniqueNames)(objectClass=posixGroup))")
        group_attrs = ["cn", "memberOf", "objectSid"] + ([] if self.is_ad else ["member", "uniqueMember", "memberUid"])
        user_attrs = sorted({self.user_attr, "displayName", "cn", "givenName", "sn", "mail", "memberOf", "objectSid",
                             "primaryGroupID", "userAccountControl", "accountExpires", "objectClass", *GUID_ATTRS})
        conn = self.service_connection()
        try:
            # 1) Groups: DN, parent groups, SID (for the primary group), members (OpenLDAP)
            parents: dict[str, set[str]] = {}       # group -> groups it is a member of
            display_dn: dict[str, str] = {}
            by_sid: dict[bytes, str] = {}
            members_of: dict[str, set[str]] = {}    # member (DN or memberUid) -> groups (only without memberOf)
            try:
                groups_read = list(self._paged_entries(conn, self.group_base(), group_flt, group_attrs))
            except _SizeLimit as exc:   # without all groups the roles would be wrong – better not at all
                raise LDAPError(_("Reading the groups stopped at the server's size limit "
                                  "({error}) – raise the limit for the service account.", error=exc)) from exc
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
            if members_of:      # OpenLDAP: group-in-group is only listed in the parent group's member
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

            # 2) People
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
                    complete = False    # partial list: do not delete or disable anything
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
                # as for login (user_groups): memberOf and primary group; the groups' member lists
                # (member/uniqueMember/memberUid) only if neither exists (OpenLDAP without memberOf)
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
        # duplicate username (e.g. same uid in two OUs): login refuses this as ambiguous –
        # so do not take over any of them here either
        return [u for u in found if names_seen.get(u.username.lower(), 0) == 1], complete

    def test_connection(self) -> str:
        conn = self.service_connection()
        who = conn.extend.standard.who_am_i() if hasattr(conn, "extend") else ""
        ok = conn.search(self.base_dn, "(objectClass=*)", "BASE", attributes=["objectClass"])
        conn.unbind()
        if not ok:
            raise LDAPError(_("Bind successful, but base DN '{base}' is not readable.", base=self.base_dn))
        return _("Connection successful. Signed in as {who}. Base DN is reachable.", who=who or self.bind_dn)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return v
    return [v]


def _escape_bytes(data: bytes) -> str:
    """Encode a binary value (e.g. SID) for an LDAP filter."""
    return "".join(f"\\{b:02x}" for b in data)


_FILETIME_NEVER = (0, 9223372036854775807)


def _filetime_now() -> int:
    """Now as Windows FILETIME (100 ns steps since 1601) – the format of accountExpires."""
    import time
    return int((time.time() + 11644473600) * 10_000_000)


def _expired(value) -> bool:
    """accountExpires in the past? (0 and 0x7FFFFFFFFFFFFFFF = never expires)"""
    from datetime import datetime, timezone
    v = value[0] if isinstance(value, (list, tuple)) and value else value
    if v in (None, "", []):
        return False
    if isinstance(v, datetime):           # ldap3 with schema returns a datetime
        if v.year >= 9999 or v.year <= 1601:
            return False
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return False
    return n not in _FILETIME_NEVER and n < _filetime_now()


def _is_person_account(attrs: dict, username: str) -> bool:
    """Only real, usable user accounts: no computers (name ends with $), no groups, not disabled
    in AD and not expired. (In addition to the server-side search filter – belt and braces.)"""
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
        return False      # e.g. organizational units or device objects: not a login account
    return not _expired(attrs.get("accountExpires"))


GUID_ATTRS = ["objectGUID", "entryUUID"]


def _guid(entry: dict) -> str:
    """Stable ID of an entry as text (binary objectGUID → hex, entryUUID as returned)."""
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
